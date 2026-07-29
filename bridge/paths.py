"""Where the hook and the daemon meet.

Imported by both `hook.py` and `bridge.py`, and deliberately stdlib-only: the
hook runs under the system interpreter so a broken venv cannot stop Claude Code
from asking its own permission question.
"""

import os


def socket_path() -> str:
    """AF_UNIX path for the statusline <-> daemon channel.

    `$XDG_RUNTIME_DIR` is per-user, tmpfs-backed and cleaned up at logout,
    which is what this wants. The `/tmp` fallback carries the uid so two users
    on one machine cannot collide - or squat on each other's path.

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
