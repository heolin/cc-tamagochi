"""The game: hunger, happiness, lives, levels.

Lives on the host, not the stick, for three reasons: the stick's clock starts
at 1970, `run.sh` restarts it constantly, and nothing survives that restart.
Here there is a real clock and a file.

Everything is driven by `tick(now, tokens)`, and `now` is a parameter rather
than a call to `time.time()` so the decay, the daily rollover and death can be
tested against a fake clock instead of waiting a day.

## Hunger

A debt in tokens. Every hour adds `tokens_per_hour` to it; every token spent
pays it down. The bar is how far from starving the pet is:

    hunger = 1 - debt / (tokens_per_hour * hours_to_starve)

So a full bar means the debt is clear, and an idle machine empties it over
`hours_to_starve` hours whether or not anyone is watching.

## Happiness

Decays hourly and is topped up by petting. The mini-game is **always playable**
and the pet always reacts; only the reward is rate limited to once an hour.
Locking the interaction would punish affection, which is the wrong lesson.

## EXP

Three sources, in descending order of how much work they represent: tokens
spent, a completed petting, and a tap on the button. Taps are capped per day so
the level stays a record of use rather than of button pressing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "buddy.json")

DEFAULTS = {
    "hunger": {"tokens_per_hour": 2000, "hours_to_starve": 8},
    "happiness": {
        "petting_reward": 0.30,
        "decay_per_hour": 0.02,
        "reward_cooldown_minutes": 60,
    },
    "life": {"max_hearts": 5, "penalty_missed_goals": 1.0, "reward_met_goals": 0.5},
    "level": {
        "tokens_per_exp": 5000,
        "exp_per_petting": 20,
        "taps_per_day": 100,
        "titles": ["Hatchling", "Sparked", "Coder", "Integrator", "Builder", "Organiser", "Explainer", "Operator", "Architect", "Launched"],
    },
}

HOUR = 3600.0


def _merge(base: dict, override: dict) -> dict:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update({k: v for k, v in value.items() if not k.startswith("_")})
        elif not key.startswith("_"):
            out[key] = value
    return out


# A day away should cost a day's worth of neglect, but a stale clock or a
# forgotten laptop should not silently wipe the pet out on the first tick after
# a holiday. Elapsed time is honoured up to this, then capped.
MAX_CATCHUP_HOURS = 72.0
MAX_CATCHUP_DAYS = 7


def _day(timestamp: float) -> str:
    stamp = time.localtime(timestamp)
    return "%04d-%02d-%02d" % (stamp[0], stamp[1], stamp[2])


def _days_between(earlier: str, later: str) -> int:
    """Whole calendar days between two YYYY-MM-DD strings, 0 if unparseable."""
    from datetime import date

    try:
        a = date(*(int(p) for p in earlier.split("-")))
        b = date(*(int(p) for p in later.split("-")))
    except (ValueError, TypeError):
        return 0
    return max(0, (b - a).days)


@dataclass
class Snapshot:
    """Everything that must survive a restart - and everything `buddy.json` is.

    The file this serialises to is the only state the project keeps on disk, and
    this class is its whole schema: counters, two timestamps and a date string.
    No usage history, no session ids, no paths, nothing about what the tokens
    were spent on. Deleting the file loses a pet and nothing else, which is why
    `buddyctl.py reset` can simply rename it aside.
    """

    debt: float = 0.0
    happiness: float = 0.5
    lives: float = 5.0
    exp: int = 0
    dead: bool = False

    tokens_seen: int = 0  # last total, to turn a running count into a delta
    last_tick: float = 0.0
    last_pet_reward: float = 0.0

    day: str = ""
    tokens_today: int = 0
    petted_today: bool = False

    # Tap-for-EXP, capped per day. Reset by the same midnight rollover that
    # settles the goals, so there is no second clock to keep - and a daily
    # allowance is easier to hold in your head than a rolling hour.
    taps_today: int = 0

    # Kept so the level screen can show what today still needs.
    goals_met_yesterday: bool = False

    # Last level the device was told about, so a promotion can be announced
    # once. Persisted, or a restart would replay the celebration.
    level_seen: int = 0


class Game:
    def __init__(self, config: dict | None = None, path: str = STATE_PATH) -> None:
        self.config = _merge(DEFAULTS, config or {})
        self.path = path
        self.state = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> Snapshot:
        try:
            with open(self.path) as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return Snapshot()
        except (OSError, ValueError) as exc:
            log.warning("unreadable %s, starting fresh: %s", self.path, exc)
            return Snapshot()

        known = {f for f in Snapshot.__dataclass_fields__}
        return Snapshot(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        """Atomic: write a sibling then rename.

        A half-written file is a dead pet with no way back, and this is written
        every few seconds - the odds of being interrupted mid-write are not
        theoretical.
        """
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as handle:
                json.dump(asdict(self.state), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save %s: %s", self.path, exc)

    # -- mechanics ---------------------------------------------------------

    def tick(self, now: float, tokens_total: int) -> None:
        """Advance the world to `now`, having seen `tokens_total` output tokens."""
        state = self.state

        if not state.last_tick:
            state.last_tick = now
            state.day = _day(now)
            state.tokens_seen = tokens_total
            return

        # Tokens are reported as a running total that resets when sessions end,
        # so a drop means "new sessions", not "negative work".
        delta = max(0, tokens_total - state.tokens_seen)
        state.tokens_seen = tokens_total

        # Time keeps passing while the daemon is down. A night with the laptop
        # closed must arrive as a night of hunger, not as a pause - a pet that
        # freezes when nobody is looking is not a pet.
        #
        # Clamped at zero because a backwards clock (NTP correction, a timezone
        # change) would otherwise feed the buddy for free.
        elapsed = max(0.0, now - state.last_tick)
        state.last_tick = now
        hours = min(elapsed / HOUR, MAX_CATCHUP_HOURS)

        if elapsed > 4 * HOUR:
            log.info(
                "catching up %.1f h away (capped at %.0f): hunger and happiness decay",
                elapsed / HOUR, MAX_CATCHUP_HOURS,
            )

        self._roll_day(now)

        state.tokens_today += delta

        # Debt is capped at an empty bar. Past that it is invisible but still
        # has to be paid off, so a week away would leave a pet that eats a
        # week's tokens before the bar twitches - punishing the return rather
        # than the absence. The cap also bounds what happens when the feeding
        # requirement is retuned, since the debt is stored in absolute tokens.
        hunger_cfg = self.config["hunger"]
        full = hunger_cfg["tokens_per_hour"] * hunger_cfg["hours_to_starve"]
        state.debt = min(
            float(full),
            max(0.0, state.debt + hours * hunger_cfg["tokens_per_hour"] - delta),
        )

        happy_cfg = self.config["happiness"]
        state.happiness = max(0.0, state.happiness - hours * happy_cfg["decay_per_hour"])

        per_exp = self.config["level"]["tokens_per_exp"]
        if per_exp:
            state.exp += int(delta // per_exp)

        if state.lives <= 0:
            state.dead = True

    def _roll_day(self, now: float) -> None:
        """At midnight, settle yesterday's goals."""
        today = _day(now)
        if today == self.state.day:
            return

        state = self.state

        # A state file that predates day tracking has no yesterday to settle.
        # Judging it would cost a heart for a day nobody was scored on.
        if not state.day:
            state.day = today
            return
        life = self.config["life"]
        fed = state.tokens_today >= self.config["hunger"]["tokens_per_hour"]
        met = fed and state.petted_today

        if met:
            state.lives = min(life["max_hearts"], state.lives + life["reward_met_goals"])
        else:
            state.lives = max(0.0, state.lives - life["penalty_missed_goals"])

        # Days the machine was off are days nobody fed or petted anything. One
        # penalty for a week away would make a holiday cheaper than a bad
        # Tuesday, so every missed day is charged - up to a cap, because the
        # difference between two weeks and two months is not worth modelling.
        skipped = min(max(0, _days_between(state.day, today) - 1), MAX_CATCHUP_DAYS)
        if skipped:
            state.lives = max(0.0, state.lives - skipped * life["penalty_missed_goals"])

        log.info(
            "day rolled over: fed=%s petted=%s, %d unattended day(s) -> %.1f hearts",
            fed, state.petted_today, skipped, state.lives,
        )

        state.goals_met_yesterday = met
        state.day = today
        state.tokens_today = 0
        state.petted_today = False
        state.taps_today = 0

        if state.lives <= 0:
            state.dead = True

    def pet(self, now: float) -> dict:
        """A completed petting. Always accepted; the reward is what is limited.

        Returns what happened, so the device can react differently to a real
        reward than to a friendly no-op.
        """
        state = self.state
        if state.dead:
            return {"rewarded": False, "reason": "dead"}

        cfg = self.config["happiness"]
        cooldown = cfg["reward_cooldown_minutes"] * 60
        waited = now - state.last_pet_reward

        if state.last_pet_reward and waited < cooldown:
            return {
                "rewarded": False,
                "reason": "cooldown",
                "seconds_left": int(cooldown - waited),
            }

        state.happiness = min(1.0, state.happiness + cfg["petting_reward"])
        state.exp += self.config["level"]["exp_per_petting"]
        state.last_pet_reward = now
        state.petted_today = True
        return {"rewarded": True, "happiness": state.happiness}

    def reset(self, now: float) -> None:
        """Bring a dead pet back. Only ever from the host, on purpose."""
        self.state = Snapshot(last_tick=now, day=_day(now))
        self.save()
        log.info("buddy reset")

    # -- derived -----------------------------------------------------------

    @property
    def hunger(self) -> float:
        cfg = self.config["hunger"]
        full = cfg["tokens_per_hour"] * cfg["hours_to_starve"]
        if full <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - self.state.debt / full))

    @property
    def level(self) -> int:
        # Each level costs a little more than the last, so early progress is
        # quick and later levels mean something.
        exp, level = self.state.exp, 1
        cost = 100
        while exp >= cost:
            exp -= cost
            level += 1
            cost = int(cost * 1.35)
        return level

    @property
    def level_progress(self) -> float:
        exp, cost = self.state.exp, 100
        while exp >= cost:
            exp -= cost
            cost = int(cost * 1.35)
        return exp / cost if cost else 0.0

    @property
    def title(self) -> str:
        titles = self.config["level"]["titles"] or ["Buddy"]
        return titles[min(self.level - 1, len(titles) - 1)]

    def can_pet(self, now: float) -> bool:
        if self.state.dead:
            return False
        cooldown = self.config["happiness"]["reward_cooldown_minutes"] * 60
        return not self.state.last_pet_reward or (now - self.state.last_pet_reward) >= cooldown

    def take_levelup(self) -> int | None:
        """The new level, once, if it has gone up since the last ask.

        Consumed rather than exposed as a flag so two callers cannot announce
        the same promotion twice. A first run reports nothing: `level_seen`
        starts at 0 and is simply brought up to date, since arriving at level 1
        is not a promotion.
        """
        current = self.level
        if not self.state.level_seen:
            self.state.level_seen = current
            return None
        if current > self.state.level_seen:
            self.state.level_seen = current
            return current
        # Levels never fall, but a reset does - keep the two in step.
        self.state.level_seen = current
        return None

    @property
    def taps_total(self) -> int:
        return self.config["level"]["taps_per_day"]

    @property
    def taps_left(self) -> int:
        return max(0, self.taps_total - self.state.taps_today)

    def award_exp(self, now: float, amount: int = 1) -> dict:
        """A tap on the buddy. Small, immediate, and capped per day.

        Without a cap the level is just a button-press counter; with one, taps
        are the small change on top of the tokens and petting that do the real
        work. The counter is cleared by the midnight rollover in _roll_day.
        """
        if self.state.dead:
            return {"granted": 0, "reason": "dead"}

        if self.taps_left <= 0:
            return {"granted": 0, "reason": "capped"}

        self.state.taps_today += amount
        self.state.exp += amount
        return {
            "granted": amount,
            "exp": self.state.exp,
            "level": self.level,
            "taps_left": self.taps_left,
        }

    def goals(self) -> dict:
        return {
            "fed": self.state.tokens_today >= self.config["hunger"]["tokens_per_hour"],
            "petted": self.state.petted_today,
        }

    def view(self, now: float) -> dict:
        """The game's contribution to the device snapshot."""
        return {
            "lives": self.state.lives,
            "hunger": self.hunger,
            "happiness": self.state.happiness,
            "level": self.level,
            "level_progress": self.level_progress,
            "title": self.title,
            "dead": self.state.dead,
            "can_pet": self.can_pet(now),
            "goals": self.goals(),
            "tokens_today": self.state.tokens_today,
            "debt": int(self.state.debt),
            "taps_left": self.taps_left,
            "taps_total": self.taps_total,
        }
