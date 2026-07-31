"""Which Claude Code sessions are alive right now.

`~/.claude/sessions/<pid>.json` holds one file per session:

    {"pid": 13388, "sessionId": "fd9a00ae-...", "cwd": "/home/wojciech/repo/m5stack",
     "startedAt": 1785316082611, "procStart": "436235", "version": "2.1.220",
     "kind": "interactive", "name": "claude-hardware-buddy-design",
     "status": "busy", "updatedAt": 1785324714837}

Read-only, no hooks, no cooperation from the CLI needed. This is where the
session count and the busy/idle split on the buddy's Claude screen come from.

**This is an index, not a transcript.** The file above is the whole of what
Claude Code writes here: which process, in which directory, busy or idle. The
conversations live elsewhere, under `~/.claude/projects/`, and nothing in this
project opens them - the pet is counting windows, not reading over your
shoulder. `cwd` is used for one thing, the project name in this module's own
`__main__` printout; `summarise()` offers it and `state.snapshot()` does not
take it, so no path ever reaches the stick.

**Files outlive their process.** A crashed or SIGKILLed session leaves its file
behind, so liveness has to be checked rather than assumed - otherwise the pet
would show a permanent crowd of sessions that ended days ago.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import host

log = logging.getLogger(__name__)

SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# A session that has not updated its file in this long is treated as gone even
# if the pid still exists - a wedged process should not read as working.
STALE_SECONDS = 15 * 60


@dataclass
class Session:
    session_id: str
    pid: int
    cwd: str
    name: str
    status: str
    started_at: float  # epoch seconds
    updated_at: float

    @property
    def project(self) -> str:
        return os.path.basename(self.cwd.rstrip("/")) or self.cwd

    @property
    def busy(self) -> bool:
        return self.status == "busy"


def _alive(pid: int, proc_start: str | None) -> bool:
    """Is this session's process still the one that wrote the file?

    Two questions, and the second is the one that matters. A pid that exists is
    not proof: after a reboot or a busy day some unrelated process inherits the
    number, and a pet reporting a crowd of sessions that are really someone
    else's compiler is worse than a pet reporting none.

    Claude Code records the process's start time beside the pid for exactly this
    reason. `host.process_started_at` knows how to ask the local OS - the format
    differs per platform and is compared as opaque text, never interpreted.
    """
    if pid <= 0:
        return False
    actual = host.process_started_at(pid)
    if actual is None:
        return False
    if proc_start and actual != str(proc_start):
        return False  # pid was reused by something else
    return True


def read_sessions(directory: Path | None = None, now: float | None = None) -> list[Session]:
    """Every live session, newest first. Never raises - a missing directory or
    a half-written file yields fewer sessions, not an exception."""
    import time

    directory = directory or SESSIONS_DIR
    now = now if now is not None else time.time()

    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return []

    out: list[Session] = []
    for path in entries:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            # Being written right now, or truncated by a crash. Skip quietly:
            # this runs every couple of seconds and a warning would be noise.
            continue

        if not isinstance(data, dict):
            continue

        pid = data.get("pid") or 0
        if not _alive(pid, data.get("procStart")):
            continue

        updated = (data.get("updatedAt") or 0) / 1000.0
        if updated and now - updated > STALE_SECONDS:
            continue

        out.append(
            Session(
                session_id=str(data.get("sessionId") or ""),
                pid=int(pid),
                cwd=str(data.get("cwd") or ""),
                name=str(data.get("name") or ""),
                status=str(data.get("status") or "idle"),
                started_at=(data.get("startedAt") or 0) / 1000.0,
                updated_at=updated,
            )
        )

    out.sort(key=lambda s: s.updated_at, reverse=True)
    return out


def summarise(sessions: list[Session]) -> dict:
    """The counts the buddy's Claude screen shows."""
    return {
        "total": len(sessions),
        "busy": sum(1 for s in sessions if s.busy),
        "idle": sum(1 for s in sessions if not s.busy),
        "projects": sorted({s.project for s in sessions if s.project}),
    }


if __name__ == "__main__":
    live = read_sessions()
    counts = summarise(live)
    print(f"{counts['total']} live, {counts['busy']} busy, {counts['idle']} idle")
    for session in live:
        label = session.name or session.session_id[:8]
        print(f"  {session.pid:7} {session.status:6} {session.project:24} {label}")
