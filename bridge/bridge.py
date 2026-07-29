#!/usr/bin/env python3
"""The daemon: Claude Code on one side, the buddy on the other.

    python bridge.py [--address AA:BB:...] [--log-level DEBUG]

Long-lived, because the BLE link is. Two inputs, neither of which can block
anything in the CLI:

* `statusline.py` connects to a UNIX socket once per status-line redraw and
  reports usage. Fire and forget - the daemon never replies.
* `sessions.py` is polled on a timer for who is alive.

The buddy is a BLE peripheral; this connects out to it and pushes state.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import time

from ble import DEFAULT_ADDRESS, BuddyLink
from game import Game
from ipc import HookServer
from state import State

log = logging.getLogger("bridge")

SESSION_POLL = 2.0

# The game only needs to move when time passes, and its resolution is hours.
# Saving on the same beat keeps the file at most this far behind reality.
GAME_TICK = 30.0
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "buddy_config.json")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", CONFIG_PATH, exc)
        return {}


class Bridge:
    def __init__(self, address: str, config: dict) -> None:
        self.game = Game(config)
        self.state = State(config, self.game)
        self.link = BuddyLink(self._on_device_line, self.state.snapshot, address)
        self.server = HookServer(self._on_message)
        self._stop = asyncio.Event()

    # -- from Claude Code --------------------------------------------------

    async def _on_message(self, message: dict) -> None:
        kind = message.get("kind")

        if kind == "usage":
            self.state.observe_usage(message)
            self.link.flush_soon()
            return None  # the status line is not waiting for us

        if kind == "admin":
            return self._admin(message)

        log.debug("ignoring message kind %r", kind)
        return None

    def _admin(self, message: dict) -> dict:
        """Commands from buddyctl.py. Replies, unlike the status line."""
        action = message.get("action")

        if action == "reset":
            self.game.reset(time.time())
            self.link.flush_soon()
            return {"ok": True, "message": "buddy reset"}

        if action == "status":
            view = self.game.view(time.time())
            snap = self.state.snapshot()
            return {
                "ok": True,
                "state": {
                    "lives": view["lives"],
                    "hunger": round(view["hunger"], 3),
                    "happiness": round(view["happiness"], 3),
                    "level": view["level"],
                    "title": view["title"],
                    "dead": view["dead"],
                    "can_pet": view["can_pet"],
                    "goals": view["goals"],
                    "tokens_today": view["tokens_today"],
                    "connected": self.link.connected,
                    # Included so "the limits are blank" can be answered without
                    # guessing: either the bridge never received them, or it has
                    # them and the device is not drawing them.
                    "five_hour": snap["five_hour"],
                    "seven_day": snap["seven_day"],
                    "usage_seen": len(self.state._usage),
                    "pose": snap["pose"],
                },
            }

        return {"ok": False, "message": f"unknown action {action!r}"}

    # -- from the buddy ----------------------------------------------------

    async def _on_device_line(self, message: dict) -> None:
        kind = message.get("kind")

        if kind == "input":
            event = message.get("event")

            if event == "exp":
                result = self.game.award_exp(time.time(), 1)
                self.game.save()
                if result["granted"]:
                    log.info("tapped: exp %d, level %d", result["exp"], result["level"])
                self.link.flush_soon()
                await self._announce_levelup()

            elif event == "pet":
                result = self.game.pet(time.time())
                self.game.save()
                if result["rewarded"]:
                    log.info("petted: happiness now %.2f", result["happiness"])
                else:
                    log.info("petted, no reward (%s)", result["reason"])
                # The device reacts differently to a reward than to a friendly
                # no-op, so the answer matters even when nothing was granted.
                await self.link.send({"kind": "petted", **result})
                self.link.flush_soon()
                await self._announce_levelup()
            else:
                log.info("device input: %s", event)

        elif kind == "hello":
            log.info("device says hello: %s", message)
            self.link.flush_soon()
        else:
            log.debug("unhandled from device: %s", message)

    async def _announce_levelup(self) -> None:
        """Tell the device once, if the buddy just gained a level."""
        level = self.game.take_levelup()
        if level is None:
            return
        log.info("level up: %d (%s)", level, self.game.title)
        await self.link.send(
            {"kind": "levelup", "level": level, "title": self.game.title}
        )
        self.link.flush_soon()

    # -- loops -------------------------------------------------------------

    async def _session_loop(self) -> None:
        previous = None
        while not self._stop.is_set():
            self.state.poll_sessions()

            # Only wake the radio when something actually changed; the poll is
            # cheap but a BLE write every two seconds is not.
            current = tuple(
                (s.session_id, s.status) for s in self.state.sessions
            )
            if current != previous:
                previous = current
                log.info(
                    "sessions: %d live, %d busy",
                    len(self.state.sessions),
                    sum(1 for s in self.state.sessions if s.busy),
                )
                self.link.flush_soon()

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=SESSION_POLL)

    async def _game_loop(self) -> None:
        """Advance and persist the game.

        Nothing else calls `tick`, so without this the pet is frozen: hunger
        never decays, tokens are never counted and midnight never arrives. The
        BLE heartbeat picks the new values up on its own, so there is no need
        to flush from here.
        """
        while not self._stop.is_set():
            self.game.tick(time.time(), self.state.tokens)
            self.game.save()

            await self._announce_levelup()

            if self.game.state.dead:
                log.warning("the buddy is dead - reset it to bring it back")

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=GAME_TICK)

    async def run(self) -> None:
        await self.server.start()
        self.state.poll_sessions()

        tasks = [
            asyncio.create_task(self.link.run(), name="ble"),
            asyncio.create_task(self._session_loop(), name="sessions"),
            asyncio.create_task(self._game_loop(), name="game"),
        ]

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        try:
            await self._stop.wait()
        finally:
            log.info("shutting down")
            await self.link.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude buddy bridge")
    parser.add_argument("--address", default=DEFAULT_ADDRESS, help="the buddy's BLE address")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()
    try:
        asyncio.run(Bridge(args.address, config).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
