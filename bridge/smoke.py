#!/usr/bin/env python3
"""Exercise the host half. No BLE, no hardware, no stick.

    python3 smoke.py

Covers the path a real status line takes - stdin, socket, state, snapshot -
plus the two behaviours that are easy to get subtly wrong: absent rate limits
must stay absent rather than becoming a confident 0, and dead sessions must not
be counted.

Deliberately does not import `bridge.py`, which pulls in bleak.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Own socket, so these tests run happily while a real bridge is live. Set before
# importing anything that resolves the path, and inherited by the statusline.py
# subprocesses this spawns.
os.environ["CC_BUDDY_SOCKET"] = "/tmp/cc-buddy-smoke-%d.sock" % os.getpid()

import sessions as sessions_mod  # noqa: E402
import state as st  # noqa: E402
from ipc import ControlServer  # noqa: E402
from state import State  # noqa: E402

STATUSLINE = os.path.join(HERE, "statusline.py")

# The reset stamps are relative to now on purpose: the countdown the Claude
# screen shows is the difference from the current time, so a fixed epoch would
# only ever test the expired branch.
FIVE_HOUR_RESET_IN = 3 * 3600 + 42 * 60
SEVEN_DAY_RESET_IN = 4 * 86400

FULL_EVENT = {
    "session_id": "sess-1",
    "cwd": "/home/wojciech/repo/m5stack",
    "model": {"display_name": "Opus 5"},
    "context_window": {
        "total_output_tokens": 1200,
        "total_input_tokens": 15500,
        "used_percentage": 8,
    },
    "cost": {"total_cost_usd": 0.0123},
    "rate_limits": {
        "five_hour": {
            "used_percentage": 23.5,
            "resets_at": int(time.time()) + FIVE_HOUR_RESET_IN,
        },
        "seven_day": {
            "used_percentage": 41.2,
            "resets_at": int(time.time()) + SEVEN_DAY_RESET_IN,
        },
    },
}

# What a non-subscription account sends: no rate_limits key at all.
BARE_EVENT = {
    "session_id": "sess-2",
    "model": {"display_name": "Opus 5"},
    "context_window": {"total_output_tokens": 400, "used_percentage": 3},
}

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}  {detail}")


def run_statusline(event: dict) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, STATUSLINE],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode, proc.stdout.strip()


async def collect(events: list[dict]) -> list[dict]:
    """Run statusline.py for each event and return what the daemon received."""
    seen: list[dict] = []

    async def handler(message: dict):
        seen.append(message)
        return None

    server = ControlServer(handler)
    await server.start()
    try:
        loop = asyncio.get_running_loop()
        for event in events:
            code, line = await loop.run_in_executor(None, run_statusline, event)
            check(f"statusline exit 0 ({event['session_id']})", code == 0, f"line={line!r}")
    finally:
        await server.stop()
    return seen


async def main() -> int:
    print("\n-- 1. statusline -> socket --")
    seen = await collect([FULL_EVENT, BARE_EVENT])
    check("both messages arrived", len(seen) == 2, f"got {len(seen)}")

    if len(seen) == 2:
        full, bare = seen
        check("kind=usage", full.get("kind") == "usage", f"got {full.get('kind')!r}")
        check("five_hour forwarded", full.get("five_hour_pct") == 23.5, f"got {full.get('five_hour_pct')!r}")
        check("seven_day forwarded", full.get("seven_day_pct") == 41.2, f"got {full.get('seven_day_pct')!r}")
        check("tokens forwarded", full.get("output_tokens") == 1200, f"got {full.get('output_tokens')!r}")
        check(
            "absent limits stay absent",
            "five_hour_pct" not in bare,
            f"got {sorted(bare)}",
        )

    print("\n-- 2. state aggregation --")
    state = State({"name": "TESTNIK"})
    for message in seen:
        state.observe_usage(message)

    check("tokens summed across sessions", state.tokens == 1600, f"got {state.tokens}")
    check("limit taken from the session that has one", state._limit("five_hour_pct") == 23.5,
          f"got {state._limit('five_hour_pct')}")

    snap = state.snapshot()
    check("snapshot names the pet", snap["name"] == "TESTNIK", f"got {snap['name']!r}")
    check("snapshot carries a pose", bool(snap.get("pose")), f"got {snap.get('pose')!r}")
    check("five_hour present", snap["five_hour"] == 23.5, f"got {snap['five_hour']}")

    # A couple of seconds of slack: the statusline subprocesses above are slow
    # enough that an exact match would be flaky.
    left = snap["five_hour_reset_in"]
    check(
        "five_hour counts down",
        left is not None and abs(left - FIVE_HOUR_RESET_IN) <= 10,
        f"got {left}, wanted about {FIVE_HOUR_RESET_IN}",
    )
    week = snap["seven_day_reset_in"]
    check(
        "seven_day counts down",
        week is not None and abs(week - SEVEN_DAY_RESET_IN) <= 10,
        f"got {week}, wanted about {SEVEN_DAY_RESET_IN}",
    )

    print("\n-- 3. unknown limits are None, never 0 --")
    empty = State()
    empty_snap = empty.snapshot()
    check("five_hour is None", empty_snap["five_hour"] is None, f"got {empty_snap['five_hour']!r}")
    check("seven_day is None", empty_snap["seven_day"] is None, f"got {empty_snap['seven_day']!r}")
    check(
        "five_hour_reset_in is None",
        empty_snap["five_hour_reset_in"] is None,
        f"got {empty_snap['five_hour_reset_in']!r}",
    )

    # A window that rolled over with nothing reporting since: the percentage
    # beside it is stale too, so the countdown must not show 0 as if it were a
    # fresh reading.
    expired = State()
    expired.observe_usage({
        "session_id": "old",
        "five_hour_pct": 50.0,
        "five_hour_reset": int(time.time()) - 120,
    })
    check(
        "a past reset reads as unknown",
        expired.snapshot()["five_hour_reset_in"] is None,
        f"got {expired.snapshot()['five_hour_reset_in']!r}",
    )

    print("\n-- 4. mood selection --")
    cases = (
        ({"lives": 0, "five_hour": 0, "happiness": 1, "hunger": 1,
          "sessions": {"total": 0, "busy": 0}}, "dead"),
        ({"lives": 5, "five_hour": 0, "happiness": 0.1, "hunger": 1,
          "sessions": {"total": 1, "busy": 0}}, "sad"),
        ({"lives": 5, "five_hour": 0, "happiness": 1, "hunger": 0.1,
          "sessions": {"total": 1, "busy": 0}}, "hungry"),
        ({"lives": 5, "five_hour": 95, "happiness": 1, "hunger": 1,
          "sessions": {"total": 1, "busy": 0}}, "burning"),
        ({"lives": 5, "five_hour": 10, "happiness": 0.5, "hunger": 0.5,
          "sessions": {"total": 4, "busy": 3}}, "working"),
        ({"lives": 5, "five_hour": 10, "happiness": 0.95, "hunger": 0.95,
          "sessions": {"total": 1, "busy": 0}}, "delighted"),
        ({"lives": 5, "five_hour": 10, "happiness": 0.5, "hunger": 0.5,
          "sessions": {"total": 0, "busy": 0}}, "asleep"),
        ({"lives": 5, "five_hour": 10, "happiness": 0.5, "hunger": 0.5,
          "sessions": {"total": 1, "busy": 0}}, "idle"),
    )
    for snapshot, expected in cases:
        got = State.mood_for(snapshot)
        check(f"mood -> {expected}", got == expected, f"got {got!r}")

    print("\n-- 5. working rotates through four poses --")
    busy = {"lives": 5, "five_hour": 10, "happiness": 0.5, "hunger": 0.5,
            "sessions": {"total": 4, "busy": 3}}
    seen_poses = {State.pose_for(busy, now=i * st.WORKING_ROTATE_SECONDS) for i in range(8)}
    check(
        "all four working sprites appear",
        seen_poses == set(st.WORKING_SPRITES),
        f"got {sorted(seen_poses)}",
    )

    print("\n-- 6. every pose has a sprite --")
    sprite_dir = os.path.join(HERE, "..", "device", "sprites")
    try:
        have = {f[:-4] for f in os.listdir(sprite_dir) if f.endswith(".spr")}
    except OSError:
        have = set()

    # Host-side moods and levels, plus the poses the device picks for itself.
    # MOOD_SPRITES["idle"] is None on purpose - the idle pose comes from the
    # level - so Nones are dropped rather than looked for on disk.
    wanted = {s for s in st.MOOD_SPRITES.values() if s}
    wanted |= set(st.WORKING_SPRITES) | set(st.LEVEL_SPRITES)
    wanted |= {"normal", "happy", "dizzy", "food", "disconnected", "love"}
    missing = wanted - have
    check("no pose maps to a missing sprite", not missing, f"missing {sorted(missing)}")

    titles = json.load(open(os.path.join(HERE, "buddy_config.json")))["level"]["titles"]
    check(
        "one title per level sprite",
        len(titles) == len(st.LEVEL_SPRITES),
        f"{len(titles)} titles, {len(st.LEVEL_SPRITES)} sprites",
    )

    print("\n-- 7. live sessions --")
    live = sessions_mod.read_sessions()
    counts = sessions_mod.summarise(live)
    check("this session is visible", counts["total"] >= 1, f"got {counts}")
    check("counts add up", counts["busy"] + counts["idle"] == counts["total"], f"got {counts}")

    stale = sessions_mod.read_sessions(now=time.time() + 10 * 3600)
    check("stale sessions are dropped", stale == [], f"got {len(stale)}")

    failed = [r for r in results if not r[1]]
    print(f"\n== {len(results) - len(failed)}/{len(results)} passed ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
