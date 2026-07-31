#!/usr/bin/env python3
"""Claude Code status line that also feeds the buddy.

Register in `~/.claude/settings.json`:

    "statusLine": {
      "type": "command",
      "command": "/usr/bin/python3 /home/wojciech/repo/m5stack/claude/cc-bridge/statusline.py"
    }

This is the **only** place rate limits are exposed. They arrive as HTTP
response headers, live in memory, and never reach the JSONL transcripts - the
status line's stdin is the one documented way out. Everything else the pet
needs comes from `sessions.py`.

Two jobs, in this order of importance:

1. print a status line, because the user sees it every turn
2. forward the numbers to the bridge daemon

So the socket write is best effort with a very short timeout, and every failure
path still prints. A status line that hangs would stall the terminal on every
redraw; one that raises would leave it blank.

Standard library only, and run by the system interpreter - a broken venv must
not be able to break the prompt.

## The one piece of this project that Claude Code runs

Everything else here watches from outside; this file is invoked by the CLI
itself, so it is the only place that could slow anyone down. Three deliberate
limits keep that from mattering:

* It **forwards a fixed subset** of the event, chosen by `pick()`. Not the
  event, not whatever a future CLI adds to it.
* It **never waits**: `SEND_TIMEOUT` is 0.15 s and the socket is connect-and-
  forget. A daemon that is down, wedged or missing costs one status line's worth
  of latency at most.
* It **cannot fail loudly**: every path prints something, including the
  catch-all in `__main__`. A status line that raises leaves the prompt blank.

It has no side effects beyond that write - nothing is stored, nothing is
appended, no file is opened for writing anywhere in this file.
"""

from __future__ import annotations

import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from paths import socket_path
except ImportError:  # pragma: no cover - keeps the status line alive regardless
    def socket_path() -> str:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and os.path.isdir(runtime):
            return os.path.join(runtime, "cc-buddy.sock")
        return "/tmp/cc-buddy-%d.sock" % os.getuid()


# Deliberately tiny. This runs on every status-line redraw, and the pet losing
# one sample matters far less than the prompt stuttering.
SEND_TIMEOUT = 0.15


def forward(payload: dict) -> None:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SEND_TIMEOUT)
        sock.connect(socket_path())
        sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
    except OSError:
        pass  # daemon down, socket gone - the pet just misses this update
    else:
        try:
            sock.close()
        except OSError:
            pass


def pick(event: dict) -> dict:
    """The subset the game needs, flattened.

    `rate_limits` is absent for non-subscription accounts and until the first
    API response of a session, and each window can be missing on its own. Every
    field here is therefore optional - the pet must show "unknown", never a
    confident 0.

    This is an allowlist, not a filter: the daemon gets exactly the keys built
    below and nothing else, so a field added to the status-line event upstream
    stays where it is until someone here decides the pet needs it.

    What is deliberately left behind, though the event offers it: the transcript
    path, the working directory, the session's version and output style, and
    everything else about the workspace. `cwd` used to be forwarded and nothing
    ever read it - `Usage` has no field for it - so it went out of the payload
    rather than sitting in a socket for no reason. `sessions.py` gets its own
    project names from the session files, and neither name reaches the stick.

    The `session_id` is here because usage has to be accumulated per session (a
    single shared counter would double-count two terminals); it is the CLI's own
    UUID and identifies nothing else.
    """
    limits = event.get("rate_limits") or {}
    five = limits.get("five_hour") or {}
    seven = limits.get("seven_day") or {}
    context = event.get("context_window") or {}
    cost = event.get("cost") or {}
    model = event.get("model") or {}

    out = {
        "kind": "usage",
        "session_id": event.get("session_id") or "",
        "model": model.get("display_name") or model.get("id") or "",
        "output_tokens": context.get("total_output_tokens"),
        "input_tokens": context.get("total_input_tokens"),
        "context_pct": context.get("used_percentage"),
        "cost_usd": cost.get("total_cost_usd"),
        "five_hour_pct": five.get("used_percentage"),
        "five_hour_reset": five.get("resets_at"),
        "seven_day_pct": seven.get("used_percentage"),
        "seven_day_reset": seven.get("resets_at"),
    }
    return {k: v for k, v in out.items() if v is not None}


def render(data: dict) -> str:
    parts = []

    model = data.get("model")
    if model:
        parts.append(model)

    context_pct = data.get("context_pct")
    if context_pct is not None:
        parts.append(f"ctx {context_pct:.0f}%")

    five = data.get("five_hour_pct")
    if five is not None:
        parts.append(f"5h {five:.0f}%")

    seven = data.get("seven_day_pct")
    if seven is not None:
        parts.append(f"7d {seven:.0f}%")

    cost = data.get("cost_usd")
    if cost:
        parts.append(f"${cost:.2f}")

    return "  ".join(parts) if parts else "buddy"


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (ValueError, UnicodeDecodeError, OSError):
        print("buddy")
        return

    if not isinstance(event, dict):
        print("buddy")
        return

    data = pick(event)
    forward(data)
    print(render(data))


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - the prompt must always get a line
        print("buddy")
