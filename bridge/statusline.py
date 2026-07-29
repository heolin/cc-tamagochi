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
        "cwd": event.get("cwd") or "",
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
