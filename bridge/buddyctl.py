#!/usr/bin/env python3
"""Talk to a running bridge.

    ./buddyctl.py status     # what the buddy is doing
    ./buddyctl.py reset      # bring a dead one back

Standard library only, so it works without the venv.

Both commands go over the daemon's local socket (`paths.socket_path()`, mode
0600) and both stay inside the game: `status` reads the pet's numbers back out,
`reset` starts it over. There is no command here that touches Claude Code,
because the daemon has none to offer - see `ipc.py`.

Reset goes through the daemon rather than editing `buddy.json` directly. The
daemon holds the game in memory and saves every thirty seconds, so a file edited
underneath it would be overwritten within the minute - the fix has to reach the
process that owns the state. When no daemon is running there is no such race,
so the file is rewritten in place instead.
"""

from __future__ import annotations

import json
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paths import socket_path  # noqa: E402

TIMEOUT = 5.0
STATE_FILE = os.path.join(HERE, "buddy.json")


def ask(action: str) -> tuple[bool, dict | None]:
    """One request to the daemon.

    Returns (reached, reply). The two failures are worth telling apart: a
    refused connection means no daemon, while a connection that succeeds and
    then goes quiet means a daemon that does not understand the request - which
    in practice means one running older code and needing a restart. Reporting
    both as "not running" sent me looking for a process that was right there.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect(socket_path())
    except OSError:
        return False, None

    try:
        sock.sendall(
            (json.dumps({"kind": "admin", "action": action}) + "\n").encode()
        )
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
    except (OSError, socket.timeout):
        return True, None
    finally:
        sock.close()

    line = bytes(buf).split(b"\n", 1)[0].strip()
    if not line:
        return True, None
    try:
        return True, json.loads(line)
    except ValueError:
        return True, None


def reset_file() -> bool:
    """Fallback when nothing is listening: start the state file over."""
    try:
        if os.path.exists(STATE_FILE):
            os.replace(STATE_FILE, STATE_FILE + ".bak")
            print(f"previous state kept as {os.path.basename(STATE_FILE)}.bak")
        else:
            print("no state file - nothing to reset")
        return True
    except OSError as exc:
        print(f"could not reset: {exc}", file=sys.stderr)
        return False


def show(state: dict) -> None:
    hearts = state["lives"]
    print(f"  {'DEAD' if state['dead'] else 'alive':8} {hearts:.1f} hearts")
    print(f"  level    {state['level']} ({state['title']})")
    print(f"  hunger   {state['hunger'] * 100:.0f}%")
    print(f"  happy    {state['happiness'] * 100:.0f}%")
    print(f"  petting  {'ready' if state['can_pet'] else 'on cooldown'}")
    goals = state["goals"]
    print(f"  goals    fed={goals['fed']} petted={goals['petted']}")
    print(f"  tokens   {state['tokens_today']} today")

    def limit(value, resets_in):
        if value is None:
            return "unknown"
        if resets_in is None:
            return f"{value:.0f}%"
        hours, minutes = divmod(int(resets_in) // 60, 60)
        return f"{value:.0f}%, resets in {hours}:{minutes:02d}h"

    print(f"  5h limit {limit(state.get('five_hour'), state.get('five_hour_reset_in'))}")
    print(f"  7d limit {limit(state.get('seven_day'), state.get('seven_day_reset_in'))}")
    print(f"  usage    reported by {state.get('usage_seen', 0)} session(s)")
    print(f"  pose     {state.get('pose')}")
    print(f"  device   {'connected' if state['connected'] else 'not connected'}")

    if state.get("five_hour") is None:
        print()
        print("  Limits unknown. They only reach us through the statusLine command,")
        print("  they need a Pro or Max plan, and they appear after the first API")
        print("  response of a session - so a freshly started session has none yet.")
        if not state.get("usage_seen"):
            print("  Nothing has reported usage at all: check that statusLine is")
            print("  registered in ~/.claude/settings.json and points at this repo.")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("status", "reset"):
        print(__doc__.strip().split("\n\n")[1])
        return 2

    action = sys.argv[1]
    reached, reply = ask(action)

    if reached and reply is None:
        print(
            "the bridge is running but did not answer - it is probably an older\n"
            "build without the admin commands. Restart it:\n"
            "  systemctl --user restart cc-tamagochi    (or Ctrl-C and rerun bridge.py)",
            file=sys.stderr,
        )
        return 1

    if reply is None:
        if action == "status":
            print("bridge is not running")
            return 1
        print("bridge is not running - resetting the state file directly")
        return 0 if reset_file() else 1

    if not reply.get("ok"):
        print(reply.get("message", "failed"), file=sys.stderr)
        return 1

    if action == "status":
        show(reply["state"])
    else:
        print(reply.get("message", "done"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
