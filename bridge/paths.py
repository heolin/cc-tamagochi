"""Where the status line and the daemon meet.

One rule, in one place, so the three programs that need the socket cannot
disagree about where it is: `statusline.py` writes to it, `buddyctl.py` asks
questions on it, `bridge.py` listens on it.

Deliberately stdlib-only. `statusline.py` runs under the system interpreter -
whatever Claude Code's `statusLine` command points at - and importing anything
from a venv here would let a broken venv blank the prompt.
"""

import os


def socket_path() -> str:
    """AF_UNIX path for the statusline <-> daemon channel.

    `$XDG_RUNTIME_DIR` is per-user, tmpfs-backed, mode 0700 and cleaned up at
    logout, which is what this wants: the directory alone already keeps other
    users out, and `ipc.py` adds 0600 on the socket itself. Nothing here ever
    reaches the disk, so there is no file to forget about tomorrow.

    The `/tmp` fallback carries the uid so two users on one machine cannot
    collide - or squat on each other's path.

    `$CC_BUDDY_SOCKET` overrides both. That is what lets `smoke.py` run against
    its own socket while a real daemon is live, rather than demanding the pet
    be switched off before its tests can pass.
    """
    override = os.environ.get("CC_BUDDY_SOCKET")
    if override:
        return override

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "cc-buddy.sock")
    return "/tmp/cc-buddy-%d.sock" % os.getuid()
