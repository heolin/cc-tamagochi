"""What the buddy is told, and where each number comes from.

Two sources, both read-only:

* `statusline.py` pushes a `usage` message per status-line redraw - the only
  place rate limits exist. One message per session, so they are kept per
  `session_id` and summed.
* `sessions.py` polls `~/.claude/sessions/` for who is alive and busy.

Game values (hunger, happiness, lives, level) are placeholders here. They move
to `game.py` in the next step; the snapshot shape is already their home so the
device does not change when they become real.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import sessions as sessions_mod


@dataclass
class Usage:
    """One session's usage, accumulated rather than sampled.

    `context_window.total_output_tokens` is **not** a running total: it counts
    what is in the context window right now, so it drops to near zero whenever
    the conversation is compacted. Reading it directly produced a token count
    that jumped around and shrank - 1k, 8, 2k - which is not what anyone means
    by "used today".

    So each reading is turned into growth: normally the difference from the
    last one, and after a drop the whole new value, since a smaller number
    means the window was cleared rather than that work was undone.
    """

    session_id: str
    accumulated: int = 0
    last_output: int = 0
    output_tokens: int = 0
    input_tokens: int = 0
    context_pct: float | None = None
    cost_usd: float = 0.0
    five_hour_pct: float | None = None
    five_hour_reset: int | None = None
    seven_day_pct: float | None = None
    seven_day_reset: int | None = None
    seen_at: float = field(default_factory=time.time)


# The mascot's *mood*, which is what the main and level screens show. The
# petting and feeding screens pick their own poses on the device, because there
# the pose is about the interaction rather than about how the day is going.
#
# Kept host-side deliberately: retuning this should never mean regenerating
# sprites or touching the device.
#
# First match wins, so the order is the priority: distress first, then what the
# machine is doing, then mood, then nothing-in-particular.
MOODS = (
    ("dead", lambda s: s.get("dead") or s["lives"] <= 0),
    ("sad", lambda s: s["lives"] <= 1 or s["happiness"] < 0.3),
    ("hungry", lambda s: s["hunger"] < 0.3),
    ("burning", lambda s: (s["five_hour"] or 0) >= 90),
    ("working", lambda s: s["sessions"]["busy"] >= 1),
    ("delighted", lambda s: s["happiness"] >= 0.8 and s["hunger"] >= 0.8),
    ("asleep", lambda s: s["sessions"]["total"] == 0),
    ("idle", lambda s: True),
)

MOOD_SPRITES = {
    "dead": "heart_broken",
    "sad": "heart_broken",
    "hungry": "food",
    "burning": "everything_is_burning",
    "delighted": "really_happy",
    "asleep": "sleeping",
    "idle": None,  # depends on the level - see LEVEL_SPRITES
}

# The idle pose is the one you see most, so it is where levelling shows. The
# buddy picks up props as it goes and ends up with helpers of its own, which
# gives the level a face rather than only a word.
#
# Indexed by level, clamped at both ends, so levels past the list keep the last
# pose instead of falling back to a plain one. Must line up with the `titles`
# list in buddy_config.json.
#
# `reading` is deliberately absent: it belongs to the working rotation, and a
# pose that means two things at once makes both harder to read. `drawer` and
# `server` are both racks and look alike, so they are kept apart.
LEVEL_SPRITES = (
    "normal",           #  1  Hatchling      nothing but the animal
    "idea",             #  2  Sparked        lightbulb
    "coding",           #  3  Coder          at the keyboard
    "api",              #  4  Integrator     wiring things together
    "app",              #  5  Builder        shipped something
    "drawer",           #  6  Organiser      put it away tidily
    "talking",          #  7  Explainer      says what it did
    "server",           #  8  Operator       runs it in production
    "children_agents",  #  9  Architect      has agents of its own
    "rocket",           # 10+ Launched       ship it
)

# "Working" is not one picture. Rotating through these makes a busy session
# look busy instead of frozen, and it costs no state: the index comes from the
# clock, so every caller agrees without anything being stored.
WORKING_SPRITES = ("working_hard", "reading", "fixing", "cleaning")
WORKING_ROTATE_SECONDS = 20

# How long a session's usage is remembered after its last status line. Long
# enough to cover a day, so closing a terminal in the morning does not erase
# its tokens from the afternoon's total.
USAGE_TTL = 26 * 3600


class State:
    def __init__(self, config: dict | None = None, game=None) -> None:
        config = config or {}
        self.name = config.get("name", "KLAUDIUSZ")
        self.game = game

        self._usage: dict[str, Usage] = {}
        self.sessions: list = []

        # Used only when no Game is attached, which is the case in tests that
        # exercise usage aggregation on its own.
        self._fallback = {
            "lives": 5.0,
            "hunger": 0.5,
            "happiness": 0.5,
            "level": 1,
            "level_progress": 0.0,
            "title": "Hatchling",
            "dead": False,
            "can_pet": True,
            "goals": {"fed": False, "petted": False},
            "debt": 0,
            "tokens_today": 0,
            "taps_left": 100,
            "taps_total": 100,
        }

    # -- ingest ------------------------------------------------------------

    def observe_usage(self, message: dict) -> None:
        session_id = str(message.get("session_id") or "")
        if not session_id:
            return

        entry = self._usage.get(session_id) or Usage(session_id)

        reading = message.get("output_tokens")
        if reading is not None:
            reading = int(reading)
            if reading >= entry.last_output:
                entry.accumulated += reading - entry.last_output
            else:
                entry.accumulated += reading  # window was compacted
            entry.last_output = reading

        for name in (
            "output_tokens", "input_tokens", "context_pct", "cost_usd",
            "five_hour_pct", "five_hour_reset", "seven_day_pct", "seven_day_reset",
        ):
            if name in message:
                setattr(entry, name, message[name])
        entry.seen_at = time.time()
        self._usage[session_id] = entry

    def poll_sessions(self) -> None:
        self.sessions = sessions_mod.read_sessions()

        # Usage for finished sessions is **kept**. Dropping it made the total
        # fall every time a terminal closed, which is the opposite of what a
        # day's usage should do. Entries expire on their own age instead.
        cutoff = time.time() - USAGE_TTL
        self._usage = {k: v for k, v in self._usage.items() if v.seen_at >= cutoff}

    # -- derived -----------------------------------------------------------

    @property
    def tokens(self) -> int:
        """Output tokens seen since the daemon started, monotonic.

        The game turns this into `tokens_today`, which is the number worth
        showing: this one keeps climbing across midnight.
        """
        return sum(u.accumulated for u in self._usage.values())

    @property
    def cost(self) -> float:
        return sum(u.cost_usd for u in self._usage.values())

    def _limit(self, attribute: str):
        """Rate limits are account-wide, so any session's copy will do - take
        the freshest. Absent stays absent: the pet shows unknown, never 0."""
        best = None
        for entry in self._usage.values():
            value = getattr(entry, attribute)
            if value is None:
                continue
            if best is None or entry.seen_at > best[0]:
                best = (entry.seen_at, value)
        return best[1] if best else None

    def _reset_in(self, attribute: str, now: float) -> int | None:
        """Seconds until a limit window rolls over, or None if not known.

        `resets_at` is an absolute epoch second, but the device shows a
        countdown, so the subtraction happens here: the stick has no clock of
        its own beyond what the bridge sets, and one side doing the arithmetic
        means the two can never disagree about the hour.

        A stamp already in the past means the window rolled over and no status
        line has been drawn since - the percentage beside it is stale too, so
        this reads as unknown rather than as zero.
        """
        stamp = self._limit(attribute)
        if stamp is None:
            return None
        try:
            remaining = int(stamp) - int(now)
        except (TypeError, ValueError):
            return None
        return remaining if remaining > 0 else None

    def snapshot(self, now: float | None = None) -> dict:
        """Everything the device is told, and the only thing that crosses BLE.

        Worth reading as a list rather than as code, because this dict *is* the
        privacy boundary: numbers, three booleans and the names of sprites. The
        pet's name comes from `buddy_config.json`, which the user wrote
        themselves. What is conspicuously absent is anything identifying - no
        paths, no project names, no session ids, no model name, nothing typed
        and nothing generated.

        `counts` is the one place that has to be trimmed rather than merely
        copied: `summarise()` also returns the list of project names, which is
        useful in a log and has no business on a radio.
        """
        counts = sessions_mod.summarise(self.sessions)
        moment = now if now is not None else time.time()
        game = self.game.view(now or time.time()) if self.game else self._fallback

        snap = {
            "kind": "state",
            "name": self.name,
            "level": game["level"],
            "level_progress": game["level_progress"],
            "title": game["title"],
            "lives": game["lives"],
            "hunger": game["hunger"],
            "happiness": game["happiness"],
            "dead": game["dead"],
            "can_pet": game["can_pet"],
            "goals": game["goals"],
            "debt": game["debt"],
            "taps_left": game["taps_left"],
            "taps_total": game["taps_total"],
            "tokens_today": game["tokens_today"],
            "tokens": self.tokens,
            "cost": round(self.cost, 2),
            "five_hour": self._limit("five_hour_pct"),
            "seven_day": self._limit("seven_day_pct"),
            "five_hour_reset_in": self._reset_in("five_hour_reset", moment),
            "seven_day_reset_in": self._reset_in("seven_day_reset", moment),
            # Three numbers, not counts["projects"] - see the docstring.
            "sessions": {
                "total": counts["total"],
                "busy": counts["busy"],
                "idle": counts["idle"],
            },
        }
        snap["mood"] = self.mood_for(snap)
        snap["pose"] = self.pose_for(snap, now)

        # The level screen shows the level, always - never the mood. Sent
        # separately rather than reproducing LEVEL_SPRITES on the device, so the
        # ladder stays defined in exactly one place.
        snap["level_pose"] = self.level_sprite(game["level"])
        return snap

    @staticmethod
    def mood_for(snap: dict) -> str:
        for name, matches in MOODS:
            try:
                if matches(snap):
                    return name
            except (KeyError, TypeError):
                continue
        return "idle"

    @staticmethod
    def level_sprite(level: int) -> str:
        index = max(0, min(int(level) - 1, len(LEVEL_SPRITES) - 1))
        return LEVEL_SPRITES[index]

    @staticmethod
    def pose_for(snap: dict, now: float | None = None) -> str:
        mood = State.mood_for(snap)

        if mood == "idle":
            return State.level_sprite(snap.get("level", 1))

        if mood != "working":
            return MOOD_SPRITES.get(mood) or "normal"

        stamp = now if now is not None else time.time()
        index = int(stamp // WORKING_ROTATE_SECONDS) % len(WORKING_SPRITES)
        return WORKING_SPRITES[index]
