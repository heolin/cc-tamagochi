"""The daemon's only local entrance: an AF_UNIX socket.

One connection per caller, one JSON line in, at most one JSON line back. The
handler is an async callable returning a dict to reply with, or None to say
nothing and close.

Two callers, and no others:

* `statusline.py`, once per status-line redraw, with `{"kind": "usage", ...}`.
  It never waits for an answer.
* `buddyctl.py`, with `{"kind": "admin", "action": "status"|"reset"}`, which
  does wait.

`bridge.py` ignores every other `kind`, so an unknown message is dropped rather
than interpreted.

## What writing to this socket can do

Everything that reaches the daemon is a *report about* Claude Code, never a
command to it. The worst a hostile writer could do is lie about token counts,
read the pet's stats, or reset the pet - annoying, and confined to the game. It
cannot approve a tool call, change a permission, read a conversation or reach
the CLI at all: nothing in this process talks back to Claude Code.

The socket is still created with mode 0600, inside `$XDG_RUNTIME_DIR` which is
itself 0700 and per-user. Two locks for a small blast radius is the right way
round; the default umask would have been the wrong one.

An earlier version of this file claimed the socket could "approve tool calls on
your behalf". That was true of a design that used Claude Code hooks and was
abandoned before it shipped. The comment outlived the code, which is exactly how
a security note becomes a lie - if this docstring and `bridge.py::_on_message`
ever disagree again, the code is the truth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable

from paths import socket_path

log = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[dict | None]]

MAX_REQUEST = 64 * 1024


class ControlServer:
    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._server: asyncio.Server | None = None
        self._path = socket_path()

    async def start(self) -> None:
        # A leftover socket from a killed daemon would make bind() fail with
        # EADDRINUSE even though nobody is listening. Probing it first tells
        # the two cases apart instead of unlinking someone else's live socket.
        if os.path.exists(self._path):
            if await self._someone_listening():
                raise RuntimeError(f"another bridge is already on {self._path}")
            log.warning("removing stale socket %s", self._path)
            os.unlink(self._path)

        self._server = await asyncio.start_unix_server(self._serve, path=self._path)
        os.chmod(self._path, 0o600)
        log.info("listening on %s", self._path)

    async def _someone_listening(self) -> bool:
        try:
            _, writer = await asyncio.open_unix_connection(self._path)
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw or len(raw) > MAX_REQUEST:
                return

            try:
                request = json.loads(raw)
            except ValueError:
                log.warning("unparseable request: %r", raw[:120])
                return

            if not isinstance(request, dict):
                return

            reply = await self._handler(request)
            if reply is not None:
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()

        except (ConnectionResetError, BrokenPipeError):
            # The hook gave up first - its own timeout, or the CLI was killed.
            # Normal, not worth a stack trace.
            log.debug("hook disconnected before the reply")
        except Exception:
            log.exception("handler failed")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionResetError, BrokenPipeError):
                pass

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
