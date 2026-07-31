#!/usr/bin/env python3
"""Set up the buddy by answering four questions.

    ./configure.py            # ask, then write buddy_config.json
    ./configure.py --show     # print the current settings and leave

Everything here can be done by editing `buddy_config.json` by hand. This exists
because the file is written in the game's units - tokens per *hour*, hours to
starve, hearts per missed day - and nobody thinks in those. People think "I do
about 50k tokens a day" and "it should survive a long weekend", so those are the
questions, and the arithmetic between them and the file happens here.

Standard library only, like `statusline.py` and `buddyctl.py`: this is the first
thing a new user runs and it must not need the venv to exist yet.

Nothing is written until the summary is confirmed, and the previous file is kept
as `buddy_config.json.bak`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paths import socket_path  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "buddy_config.json")

HOURS_PER_DAY = 24

# The device draws exactly five hearts (`draw_hearts` in device/main.py takes
# `maximum=5` and is called without it), so the number of hearts is not a knob -
# a six-heart pet would show five full ones and lie about its own health. "How
# long can it be neglected" is therefore tuned with the daily penalty instead.
HEARTS = 5

RECOMMENDED = {
    "name": "KLAUDIUSZ",
    "daily_tokens": 48_000,
    "hours_to_starve": 12,
    "days_of_neglect": 5,
}


# --------------------------------------------------------------------------
# Asking
# --------------------------------------------------------------------------

def ask(question: str, note: str, default, parse):
    """One question, repeated until the answer parses. Enter takes the default.

    `note` is printed above the prompt every time rather than once, because a
    rejected answer scrolls the explanation off the top of a short terminal and
    the second attempt is exactly when it is wanted.
    """
    while True:
        print()
        print(f"\033[1m{question}\033[0m")
        for line in note.splitlines():
            print(f"  {line}")
        raw = input(f"\n  [{default}] > ").strip()

        if not raw:
            return parse(str(default))
        try:
            return parse(raw)
        except ValueError as exc:
            print(f"\n  {exc}")


def parse_tokens(raw: str) -> int:
    """`50k`, `1.2M`, `48000`, `50 000` - the ways someone writes a token count.

    The comma is the awkward one: `50,000` is fifty thousand and `1,5k` is one
    and a half, and both are things people type. Digits in groups of three read
    as separators, anything else reads as a decimal point - which is the rule
    both conventions already follow, so neither has to be asked about.
    """
    text = raw.strip().lower().replace(" ", "").replace("_", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?[km]?", text):
        text = text.replace(",", "")
    else:
        text = text.replace(",", ".")

    scale = 1
    if text.endswith("k"):
        scale, text = 1_000, text[:-1]
    elif text.endswith("m"):
        scale, text = 1_000_000, text[:-1]

    try:
        value = int(float(text) * scale)
    except ValueError:
        raise ValueError("Write it as a number: 50000, or 50k, or 1.2M.") from None

    # The floor is not arbitrary: the daily figure is divided by 24 to get the
    # hourly appetite, and anything under a day's worth of small change rounds
    # to an hourly appetite of zero, which would leave the pet permanently full.
    if value < HOURS_PER_DAY:
        raise ValueError("That is too small to divide across a day - try at least 1k.")
    if value > 100_000_000:
        raise ValueError("That is more tokens than a day has hours for. Try something smaller.")
    return value


def parse_hours(raw: str) -> int:
    try:
        value = int(float(raw))
    except ValueError:
        raise ValueError("Give it in whole hours, for example 8.") from None
    if not 1 <= value <= 72:
        raise ValueError("Pick something between 1 and 72 hours.")
    return value


def parse_days(raw: str) -> int:
    try:
        value = int(float(raw))
    except ValueError:
        raise ValueError("Give it in whole days, for example 5.") from None
    if not 1 <= value <= 30:
        raise ValueError("Pick something between 1 and 30 days.")
    return value


def parse_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise ValueError("It needs a name.")
    if len(name) > 16:
        raise ValueError("Sixteen characters at most - the screen is 135 px wide.")
    return name


# --------------------------------------------------------------------------
# Context: what the buddy is actually seeing
# --------------------------------------------------------------------------

def todays_tokens() -> int | None:
    """What the running bridge has counted today, or None if it is not running.

    A guess anchored on the reader's own numbers beats a guess anchored on mine,
    and this is free: `buddyctl.py status` asks the same question over the same
    socket.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(socket_path())
        sock.sendall(b'{"kind":"admin","action":"status"}\n')
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        sock.close()
        reply = json.loads(bytes(buf).split(b"\n", 1)[0])
        return int(reply["state"]["tokens_today"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def compact(number: int) -> str:
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.0f}k"
    return str(number)


# --------------------------------------------------------------------------
# Reading and writing the file
# --------------------------------------------------------------------------

def load() -> dict:
    try:
        with open(CONFIG_PATH) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"Could not read {CONFIG_PATH}: {exc}")
        print("Answering the questions will write a fresh one.")
        return {}


def current(config: dict) -> dict:
    """The file's values expressed as the questions ask them.

    The defaults repeated here match `game.DEFAULTS`; a config file is allowed
    to be partial, and a missing section should offer the same recommendation a
    missing file does.
    """
    hunger = config.get("hunger") or {}
    life = config.get("life") or {}

    per_hour = hunger.get("tokens_per_hour", 2000)
    penalty = life.get("penalty_missed_goals", 1.0)

    return {
        "name": config.get("name", RECOMMENDED["name"]),
        "daily_tokens": int(per_hour * HOURS_PER_DAY),
        "hours_to_starve": hunger.get("hours_to_starve", RECOMMENDED["hours_to_starve"]),
        "days_of_neglect": round(HEARTS / penalty) if penalty else HEARTS,
    }


def apply(config: dict, answers: dict) -> dict:
    """Answers back into the game's own units.

    Two conversions, and they are the whole reason this script exists:

    * a **daily** token target becomes an **hourly** appetite, because hunger is
      a debt that grows every hour and is paid down by tokens spent;
    * **days of neglect** become **hearts lost per missed day**, since hearts are
      settled at midnight and the screen always shows five of them.

    The reward for a good day keeps its ratio to the penalty - half - so a
    forgiving pet does not also become one that heals implausibly fast.
    """
    out = json.loads(json.dumps(config))  # deep copy, so a failure changes nothing

    out["name"] = answers["name"]

    hunger = out.setdefault("hunger", {})
    hunger["tokens_per_hour"] = max(1, round(answers["daily_tokens"] / HOURS_PER_DAY))
    hunger["hours_to_starve"] = answers["hours_to_starve"]

    life = out.setdefault("life", {})
    life["max_hearts"] = HEARTS
    life["penalty_missed_goals"] = round(HEARTS / answers["days_of_neglect"], 2)
    life["reward_met_goals"] = round(life["penalty_missed_goals"] / 2, 2)

    return out


def summarise(answers: dict, config: dict) -> None:
    hunger, life = config["hunger"], config["life"]
    per_hour = hunger["tokens_per_hour"]
    penalty = life["penalty_missed_goals"]

    print()
    print("\033[1mWhat this means in the game\033[0m")
    print(f"  Name           {answers['name']}")
    print(f"  Appetite       {compact(per_hour)} tokens an hour "
          f"({compact(answers['daily_tokens'])} a day keeps the bar full)")
    print(f"  Hunger         a quiet {hunger['hours_to_starve']} h empties the bar from full")

    # The daily goal is an hour's appetite, not a day's, and it looks like a
    # typo unless the difference is named. It is deliberate: hearts are about
    # showing up, the hunger bar is about volume. Two goals, two scales.
    print(f"  Daily goal     spend {compact(per_hour)} tokens and pet it once, before midnight")
    print("                 (an hour's worth, not a day's - hearts ask for attendance,")
    print("                  the hunger bar is what asks for the whole day)")
    def hearts(value):
        return f"{value} heart" if value == 1 else f"{value} hearts"

    print(f"  Missed day     -{hearts(penalty)} of {HEARTS}, so {answers['days_of_neglect']} "
          f"neglected days in a row are fatal")
    print(f"  Good day       +{hearts(life['reward_met_goals'])}, up to {HEARTS}")


def write(config: dict) -> None:
    if os.path.exists(CONFIG_PATH):
        shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".bak")
        print(f"\nPrevious settings kept as {os.path.basename(CONFIG_PATH)}.bak")

    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, CONFIG_PATH)
    print(f"Wrote {CONFIG_PATH}")


# --------------------------------------------------------------------------

def show(config: dict) -> None:
    answers = current(config)
    print("Current settings, in the units this script asks about:\n")
    print(f"  name                 {answers['name']}")
    print(f"  tokens a day         {compact(answers['daily_tokens'])}")
    print(f"  hours to starve      {answers['hours_to_starve']}")
    print(f"  days of neglect      {answers['days_of_neglect']}")
    print(f"\nThe file itself is {CONFIG_PATH}")


def interview(config: dict) -> dict:
    now = current(config)
    seen_today = todays_tokens()

    print("\033[1mThe buddy, in four questions\033[0m")
    print("Enter accepts the value in brackets. Nothing is written until the end.")

    name = ask(
        "What is it called?",
        "Shown on the main screen. Plain ASCII only: the stick's built-in fonts\n"
        "carry nothing outside 0x20-0x7F, so accented letters are folded to their\n"
        "closest match rather than drawn as boxes.",
        now["name"], parse_name,
    )

    # A number from the reader's own day beats a number from mine - but it is
    # today *so far*, so it is offered as evidence rather than as the default.
    # Answering this at 09:00 with a morning's tokens would set an appetite the
    # afternoon then trivially beats.
    if seen_today is not None:
        anchor = (
            f"For reference, your bridge has counted {compact(seen_today)} output tokens "
            "so far today\n(so far - it is worth rounding up if the day is young).\n"
            "Recommended: 48k, a steady day of ordinary use."
        )
    else:
        anchor = "Recommended: 48k, which is a steady day of ordinary use."

    daily = ask(
        "How many tokens do you want to spend on a normal day?",
        f"{anchor}\n"
        "This is the appetite, not a limit: spend that much and the hunger bar\n"
        "stays full. Set it low and the pet is easy to please; set it near your\n"
        "real output and a slow day shows on its face.\n"
        "  20k  light use      50k  a steady day      150k  heavy use",
        now["daily_tokens"], parse_tokens,
    )

    hours = ask(
        "How many idle hours should it take to go from full to starving?",
        "Only about how fast the bar drains when nothing is happening.\n"
        "  4 h  strict - an afternoon away is already visible\n"
        "  8 h  a night's sleep arrives hungry\n"
        " 12 h  recommended - a night away shows, without starving by breakfast",
        now["hours_to_starve"], parse_hours,
    )

    days = ask(
        "How many days of complete neglect should it survive?",
        "Hearts are settled at midnight: a day with both goals met earns some\n"
        "back, a day with either missed costs some. Five hearts are drawn on the\n"
        "screen, so this decides how much each missed day costs.\n"
        "  3 days  strict      5 days  recommended      10 days  a holiday is survivable",
        now["days_of_neglect"], parse_days,
    )

    return {
        "name": name,
        "daily_tokens": daily,
        "hours_to_starve": hours,
        "days_of_neglect": days,
    }


def main() -> int:
    config = load()

    if "--show" in sys.argv:
        show(config)
        return 0

    if not sys.stdin.isatty():
        print("This asks questions, so it needs a terminal.", file=sys.stderr)
        print("Edit bridge/buddy_config.json directly, or run ./configure.py --show.",
              file=sys.stderr)
        return 2

    try:
        answers = interview(config)
    except (EOFError, KeyboardInterrupt):
        print("\n\nNothing was written.")
        return 1

    updated = apply(config, answers)
    summarise(answers, updated)

    try:
        confirm = input("\nWrite this to buddy_config.json? [Y/n] > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n\nNothing was written.")
        return 1

    if confirm and confirm not in ("y", "yes"):
        print("Nothing was written.")
        return 1

    write(updated)

    # The daemon reads this file once, at startup. Saying so is the difference
    # between "the configurator did nothing" and "the configurator worked".
    print()
    print("The bridge reads this at startup, so it needs a restart to take effect:")
    print("    systemctl --user restart cc-tamagochi")
    print()
    print("Hunger and hearts carry on from where they were - the pet is in")
    print("buddy.json and this file only changes the rules. To start the pet")
    print("itself over: ./buddyctl.py reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
