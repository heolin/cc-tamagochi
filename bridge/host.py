"""Everything that differs between one operating system and the next.

Three questions the rest of the bridge asks without wanting to know the answer's
shape: when did a process start, where do runtime sockets live, and what is a
serial port called here. Elsewhere the code is portable, and gathering the
exceptions in one file is what keeps it that way - `sessions.py` should not have
to know that `/proc` exists any more than it knows about `ps`.

## Why this is not called platform.py

Because that name breaks the interpreter. Every script here puts this directory
first on `sys.path`, so a module called `platform` shadows the standard
library's for the whole process - and `uuid`, which `bleak` imports, calls
`platform.system()` at import time. The first draft was called `platform.py` and
`import bleak` died with `module 'platform' has no attribute 'system'`. A local
module named after a stdlib one is a trap with a very confusing stack trace.

## Decision and logic are separated on purpose

The branch that does not run on your machine is the branch that quietly rots. So
each platform's *logic* lives in a pure function that turns text into a value -
`_parse_proc_stat`, `_parse_ps_lstart` - and only the *decision* between them
reads `sys.platform`. The parsers can then be tested from fixture strings on any
OS, and `smoke.py` does exactly that: the macOS path is exercised on Linux every
time the tests run.

What that cannot cover is whether the real `ps` on a real Mac prints what the
fixture says. Anything below marked **UNVERIFIED** is written from the tools'
documented behaviour and has never run on the hardware; see the notes on each.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys

MACOS = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")


# --------------------------------------------------------------------------
# When did a process start
# --------------------------------------------------------------------------
#
# Claude Code records a start time beside each session's pid, and the reason is
# pid reuse: after a reboot or a busy day some unrelated process inherits the
# number, and a pet showing "3 sessions" because three strangers hold those pids
# is worse than no check at all. Comparing start times is what makes liveness
# trustworthy.
#
# The *value* is opaque here. Linux counts clock ticks since boot, macOS prints
# a date; both are compared as strings against what the CLI wrote, so this
# module never has to convert either into the other's units. That comparison is
# only meaningful if Claude Code and this file read the same source on the same
# machine - which is exactly the thing to check first if macOS liveness
# misbehaves.


def _parse_proc_stat(raw: bytes) -> str | None:
    """Field 22 of `/proc/<pid>/stat`: start time in clock ticks since boot.

    The `comm` field is parenthesised and may itself contain spaces - a process
    called `(my prog)` is legal - so the split has to start after the *last*
    closing bracket rather than at the beginning of the line.
    """
    try:
        tail = raw[raw.rindex(b")") + 2 :].split()
        return tail[19].decode()
    except (ValueError, IndexError):
        return None


def _parse_ps_lstart(raw: str) -> str | None:
    """The start time `ps -o lstart=` prints, normalised.

    **UNVERIFIED on hardware.** Written from the BSD `ps` documentation, where
    `lstart` is a full date - `Thu Jul 31 09:14:22 2026` - padded so that short
    day numbers line up. That padding is why this collapses runs of whitespace:
    `Jul  1` and `Jul 31` must not compare unequal to themselves.

    Only the text matters. Whatever Claude Code stored for the session is
    compared against this verbatim, so the format is a shared secret between the
    two, not something this file interprets.
    """
    text = " ".join(raw.split())
    return text or None


def _linux_started_at(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return _parse_proc_stat(handle.read())
    except OSError:
        return None


def _macos_started_at(pid: int) -> str | None:
    """**UNVERIFIED on hardware.** `ps` is the documented way to ask; there is no
    `/proc` on macOS and the alternative is a `libproc` call through ctypes,
    which is a lot of machinery for one string.

    A subprocess per session per poll would be extravagant at a two-second
    interval, so this stays cheap by being asked once per session file that
    exists, not once per session that ever existed.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if out.returncode != 0:
        return None  # no such process
    return _parse_ps_lstart(out.stdout)


def process_started_at(pid: int) -> str | None:
    """When `pid` started, as an opaque string, or None if it is not running.

    None is the honest answer for a dead process *and* for a platform this does
    not know: `sessions.py` treats it as "not alive", so an unsupported OS shows
    an empty crowd rather than inventing one.
    """
    if pid <= 0:
        return None
    if MACOS:
        return _macos_started_at(pid)
    return _linux_started_at(pid)


# --------------------------------------------------------------------------
# Where runtime sockets live
# --------------------------------------------------------------------------


def runtime_dir(environ: dict | None = None, macos: bool | None = None) -> str | None:
    """A private, per-user, memory-backed directory - or None if there is none.

    * Linux: `$XDG_RUNTIME_DIR`, which is 0700 and cleared at logout.
    * macOS: `$TMPDIR`, which is *not* `/tmp` - launchd hands each user their own
      `/var/folders/...` directory, also private and also cleaned up. That makes
      it the real equivalent, and better than the shared `/tmp` fallback.

    Returning None sends `paths.py` to its uid-suffixed `/tmp` path, which works
    everywhere and is merely less tidy.

    `environ` and `macos` are parameters for one reason: without them the branch
    that is not this machine's could never be run, and an untested branch is a
    branch that has already broken. `smoke.py` calls both ways round.
    """
    env = os.environ if environ is None else environ
    macos = MACOS if macos is None else macos

    if macos:
        candidate = env.get("TMPDIR")
        # An explicit /tmp is the shared directory wearing the variable's name,
        # which defeats the point of asking. Fall through to the uid-suffixed
        # path instead: it at least cannot be squatted by another user.
        if candidate and candidate.rstrip("/") != "/tmp" and os.path.isdir(candidate):
            return candidate
        return None

    candidate = env.get("XDG_RUNTIME_DIR")
    if candidate and os.path.isdir(candidate):
        return candidate
    return None


# --------------------------------------------------------------------------
# What a serial port is called
# --------------------------------------------------------------------------

# The stick appears as a native USB-CDC device, so the name is the platform's
# convention rather than anything about the board.
#
# macOS offers each port twice: `/dev/tty.usbmodem*` blocks on open until the
# carrier is asserted, `/dev/cu.usbmodem*` does not. Only `cu` is usable here -
# the classic symptom of picking `tty` is a tool that hangs forever with no
# error, which reads exactly like a dead board.
SERIAL_PATTERNS = {
    "darwin": ("/dev/cu.usbmodem*",),
    "linux": ("/dev/ttyACM*",),
}


def serial_patterns(macos: bool | None = None) -> tuple[str, ...]:
    macos = MACOS if macos is None else macos
    return SERIAL_PATTERNS["darwin" if macos else "linux"]


def default_serial_port() -> str | None:
    """The first port that looks like the stick, or None if nothing is plugged in.

    The number is not stable on either platform - it follows enumeration order,
    and the port disappears and comes back on every reset - so this globs rather
    than hardcoding. `$M5_PORT` still overrides everything, and the shell
    scripts do the same thing with `uname`.
    """
    for pattern in serial_patterns():
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


if __name__ == "__main__":
    print(f"platform         {sys.platform}")
    print(f"runtime dir      {runtime_dir()}")
    print(f"serial patterns  {' '.join(serial_patterns())}")
    print(f"serial port      {default_serial_port() or '(nothing plugged in)'}")
    print(f"this process     started at {process_started_at(os.getpid())!r}")
