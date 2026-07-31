#!/usr/bin/env python3
"""Exercise the host half. No BLE, no hardware, no stick.

    python3 smoke.py

Covers the path a real status line takes - stdin, socket, state, snapshot -
plus the behaviours that are easy to get subtly wrong: absent rate limits must
stay absent rather than becoming a confident 0, dead sessions must not be
counted, and the configurator's two unit conversions (a day's tokens to an
hour's appetite, days of neglect to hearts per day) must not drift or invert.

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

import configure  # noqa: E402
import host  # noqa: E402
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

    print("\n-- 8. the configurator's arithmetic --")
    # The two conversions in configure.py are the reason it exists, and both are
    # the kind of thing that can invert or drift by a factor of 24 without
    # anything looking wrong until a pet starves in an afternoon.
    # The comma cuts both ways: a thousands separator in one convention, a
    # decimal point in another, and both get typed into this prompt.
    for raw, expected in (("50k", 50_000), ("1.2M", 1_200_000), ("48000", 48_000),
                          ("50 000", 50_000), ("50,000", 50_000), ("1,5k", 1_500),
                          ("1.5k", 1_500)):
        try:
            got = configure.parse_tokens(raw)
        except ValueError as exc:
            got = f"rejected: {exc}"
        check(f"parse_tokens({raw!r})", got == expected, f"got {got!r}")

    for raw in ("abc", "5", ""):
        rejected = False
        try:
            configure.parse_tokens(raw)
        except ValueError:
            rejected = True
        check(f"parse_tokens rejects {raw!r}", rejected)

    answers = {"name": "TESTNIK", "daily_tokens": 120_000,
               "hours_to_starve": 12, "days_of_neglect": 10}
    written = configure.apply({}, answers)

    check("daily target becomes an hourly appetite",
          written["hunger"]["tokens_per_hour"] == 5000,
          f"got {written['hunger']['tokens_per_hour']}")
    check("days of neglect become a per-day penalty",
          written["life"]["penalty_missed_goals"] == 0.5,
          f"got {written['life']['penalty_missed_goals']}")
    check("five hearts, always - the device draws five",
          written["life"]["max_hearts"] == configure.HEARTS,
          f"got {written['life']['max_hearts']}")
    check("recovery stays half the penalty",
          written["life"]["reward_met_goals"] == 0.25,
          f"got {written['life']['reward_met_goals']}")

    # Reading the file back must offer the same answers, or the second run of
    # the configurator would quietly propose different defaults than the first
    # one wrote.
    check("answers survive a round trip", configure.current(written) == answers,
          f"got {configure.current(written)}")

    # A config file is allowed to be partial, and the game fills the rest in
    # from DEFAULTS. The questions have to do the same or a missing section
    # would read as zero.
    check("a missing section falls back to the game's defaults",
          configure.current({}) == {"name": "KLAUDIUSZ", "daily_tokens": 48_000,
                                    "hours_to_starve": 12, "days_of_neglect": 5},
          f"got {configure.current({})}")

    print("\n-- 9. both platforms, from one machine --")
    # The point of host.py is that the branch which does not run here is still
    # tested here. These call the macOS side on Linux and the Linux side
    # unconditionally: what cannot be checked without a Mac is whether the real
    # `ps` prints what the fixture says, not whether the code handles it.

    # A real line, with the trap in it: `comm` is parenthesised and can contain
    # both spaces and brackets, so field 22 cannot be found by counting from
    # the left.
    stat_line = (
        b"13388 (claude (dev)) S 1 13388 13388 0 -1 4194560 12345 0 0 0 "
        b"120 34 0 0 20 0 12 0 436235 " + b"0 " * 30
    )
    check("proc stat: field 22 past a bracketed comm",
          host._parse_proc_stat(stat_line) == "436235",
          f"got {host._parse_proc_stat(stat_line)!r}")
    check("proc stat: junk is None, not a crash",
          host._parse_proc_stat(b"nonsense") is None,
          f"got {host._parse_proc_stat(b'nonsense')!r}")

    # BSD `ps -o lstart=` pads short day numbers, so the same process would
    # otherwise compare unequal to itself across a month boundary.
    check("ps lstart: padding collapses",
          host._parse_ps_lstart("Thu Jul  3 09:14:22 2026") == "Thu Jul 3 09:14:22 2026",
          f"got {host._parse_ps_lstart('Thu Jul  3 09:14:22 2026')!r}")
    check("ps lstart: no output is None",
          host._parse_ps_lstart("  \n") is None,
          f"got {host._parse_ps_lstart('  ')!r}")

    check("this process is alive to the local OS",
          host.process_started_at(os.getpid()) is not None)
    check("pid 0 is nobody", host.process_started_at(0) is None)

    check("linux runtime dir is XDG",
          host.runtime_dir({"XDG_RUNTIME_DIR": HERE}, macos=False) == HERE,
          f"got {host.runtime_dir({'XDG_RUNTIME_DIR': HERE}, macos=False)!r}")
    check("macos runtime dir is TMPDIR",
          host.runtime_dir({"TMPDIR": HERE}, macos=True) == HERE,
          f"got {host.runtime_dir({'TMPDIR': HERE}, macos=True)!r}")
    # A TMPDIR of /tmp is the shared directory wearing the variable's name.
    check("macos ignores a TMPDIR of /tmp",
          host.runtime_dir({"TMPDIR": "/tmp"}, macos=True) is None,
          f"got {host.runtime_dir({'TMPDIR': '/tmp'}, macos=True)!r}")
    check("no variable set means the /tmp fallback",
          host.runtime_dir({}, macos=False) is None and host.runtime_dir({}, macos=True) is None)

    check("macos serial ports are cu, not tty",
          host.serial_patterns(macos=True) == ("/dev/cu.usbmodem*",),
          f"got {host.serial_patterns(macos=True)}")
    check("linux serial ports are ttyACM",
          host.serial_patterns(macos=False) == ("/dev/ttyACM*",),
          f"got {host.serial_patterns(macos=False)}")

    failed = [r for r in results if not r[1]]
    print(f"\n== {len(results) - len(failed)}/{len(results)} passed ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
