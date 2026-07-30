"""
Claude Buddy - the device half.

A Tamagotchi whose hunger, happiness and life come from real Claude Code usage.
The host (`cc-bridge`) owns the game and sends state; this draws it and reads
the buttons.

    ./deploy_sprites.sh    # once: copy sprites to /flash/buddy/
    ./run.sh               # from RAM, Ctrl-C to stop
    ./deploy_app.sh        # into flash, so it runs without a terminal

Five screens, cycled with BtnB: buddy, Claude, feeding, petting, level. BtnA
taps the pet for a point of EXP on the main screen. Petting is a tilt gesture -
bring the paw down onto its head three times.

Navigation is blocked in two states, because nothing on the other screens can
be acted on: when the bridge is away, and when the buddy has died.
"""

import json
import os
import struct
import time

import bluetooth
import M5
from M5 import Lcd
from micropython import const

# --------------------------------------------------------------------------
# Sprites
# --------------------------------------------------------------------------

SPRITE_DIR = "/flash/buddy"

# Produced by tools/sprite_convert.py:
#
#   "BSP1" | W | H | ncolours | ncolours * RGB888 | rows
#   row    := nruns | nruns * (count, index)
#
# Index 0 is transparent, 1..n index the palette. Run-length because the
# alternative - one fillRect per pixel - is over a thousand calls per frame, and
# pixel art is almost all long runs. Measured: 29 poses, 8772 bytes total.
MAGIC = b"BSP1"

# The art is 43 px wide, so 3x fits the 135 px screen with three pixels to spare
# on each side. The height is whatever the converter produced - it grew from 26
# to 27 when new poses were added - so nothing here may assume it. Screens that
# stack content below the mascot measure `sprite.screen_height` instead.
ZOOM = const(3)

# Where a secondary screen puts the mascot, and how much room to leave under it.
SUB_PET_Y = const(28)
SUB_GAP = const(6)

# Drops the mascot without dragging the layout with it. Applied to the draw
# position only: the rows below are still measured from the unshifted anchor, so
# nudging the animal does not restack everything beneath it.
PET_NUDGE = const(4)


class Sprite:
    __slots__ = ("width", "height", "palette", "rows")

    def __init__(self, path):
        with open(path, "rb") as handle:
            data = handle.read()

        if data[:4] != MAGIC:
            raise ValueError("%s: not a sprite" % path)

        self.width = data[4]
        self.height = data[5]
        count = data[6]

        offset = 7
        self.palette = []
        for _ in range(count):
            self.palette.append(
                (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]
            )
            offset += 3

        # Rows are kept as raw run pairs; unpacking them into pixels would
        # cost about a kilobyte of RAM per frame for no gain, since drawing
        # walks runs anyway.
        self.rows = []
        for _ in range(self.height):
            nruns = data[offset]
            offset += 1
            self.rows.append(data[offset : offset + nruns * 2])
            offset += nruns * 2

    def draw(self, ox, oy, zoom=ZOOM):
        """Blit at (ox, oy). Transparent runs are skipped, not painted."""
        palette = self.palette
        y = oy
        for row in self.rows:
            x = ox
            for i in range(0, len(row), 2):
                count = row[i]
                index = row[i + 1]
                span = count * zoom
                if index:
                    Lcd.fillRect(x, y, span, zoom, palette[index - 1])
                x += span
            y += zoom

    @property
    def screen_width(self):
        return self.width * ZOOM

    @property
    def screen_height(self):
        return self.height * ZOOM


def available_poses():
    try:
        return sorted(f[:-4] for f in os.listdir(SPRITE_DIR) if f.endswith(".spr"))
    except OSError:
        return []


class SpriteCache:
    """Loads on demand and keeps a few. Holding every pose would be wasteful: the
    game shows one at a time and pose changes are rare."""

    LIMIT = const(4)

    def __init__(self):
        self._loaded = {}
        self._order = []

    def get(self, name):
        sprite = self._loaded.get(name)
        if sprite is not None:
            return sprite

        try:
            sprite = Sprite("%s/%s.spr" % (SPRITE_DIR, name))
        except (OSError, ValueError) as exc:
            print("buddy: cannot load %r: %s" % (name, exc))
            return None

        if len(self._order) >= self.LIMIT:
            del self._loaded[self._order.pop(0)]
        self._loaded[name] = sprite
        self._order.append(name)
        return sprite


# --------------------------------------------------------------------------
# Link to the bridge
# --------------------------------------------------------------------------

# We are the peripheral; the host connects to us. Nordic UART, newline-delimited
# JSON, the same transport test2 and test3 use.
_UART_UUID = bluetooth.UUID("6e400001-b5a3-f393-e0a9-e50e24dcca9e")
_RX_UUID = bluetooth.UUID("6e400002-b5a3-f393-e0a9-e50e24dcca9e")  # host -> us
_TX_UUID = bluetooth.UUID("6e400003-b5a3-f393-e0a9-e50e24dcca9e")  # us -> host

_FLAG_WRITE_NO_RESPONSE = const(0x0004)
_FLAG_WRITE = const(0x0008)
_FLAG_NOTIFY = const(0x0010)

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_MTU_EXCHANGED = const(21)

NAME_PREFIX = "Claude-"

# MicroPython's GATT value buffer defaults to **20 bytes** and truncates
# silently. A state snapshot is several hundred, so without this every message
# arrives as unparseable fragments and the device looks like it is ignoring a
# host that is plainly transmitting.
RX_BUFFER = const(1024)

PREFERRED_MTU = const(247)
MAX_ADV_PAYLOAD = const(31)

# No snapshot for this long means the host is gone even if BLE still claims a
# link, so the pet goes to sleep rather than showing stale numbers forever.
STALE_MS = const(30000)


def _adv_element(adv_type, value):
    return struct.pack("BB", len(value) + 1, adv_type) + value


class Link:
    """NUS peripheral. Receives state, sends input events."""

    def __init__(self, on_message):
        self._on_message = on_message
        self._ble = bluetooth.BLE()

        try:
            self._ble.config(mtu=PREFERRED_MTU)
        except Exception:  # noqa: BLE001 - not every port accepts it
            pass

        self._ble.active(True)
        self._ble.irq(self._irq)

        ((self._rx, self._tx),) = self._ble.gatts_register_services(
            (
                (
                    _UART_UUID,
                    (
                        (_RX_UUID, _FLAG_WRITE_NO_RESPONSE | _FLAG_WRITE),
                        (_TX_UUID, _FLAG_NOTIFY),
                    ),
                ),
            )
        )
        self._ble.gatts_set_buffer(self._rx, RX_BUFFER)

        _kind, addr = self._ble.config("mac")
        self.name = NAME_PREFIX + "%02X%02X" % (addr[4], addr[5])

        self._conn = None
        self._mtu = 23
        self._line = bytearray()
        self.last_message = 0

        self._advertise()

    def _advertise(self):
        # Flags 3 + an 11-character name 13 = 16 of 31 bytes. The 128-bit NUS
        # UUID would take 18 on its own, so it goes in the scan response; the
        # name is what the host filters on.
        payload = _adv_element(0x01, b"\x06") + _adv_element(0x09, self.name.encode())
        self._ble.gap_advertise(
            100_000,
            adv_data=payload,
            resp_data=_adv_element(0x07, bytes(_UART_UUID)),
            connectable=True,
        )
        print("buddy: advertising as %r" % self.name)

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            self._conn, _, _ = data
            self._mtu = 23
            print("buddy: host connected")
            self.send({"kind": "hello", "name": self.name})

        elif event == _IRQ_CENTRAL_DISCONNECT:
            print("buddy: host disconnected")
            self._conn = None
            self._line = bytearray()
            self.last_message = 0
            self._advertise()

        elif event == _IRQ_MTU_EXCHANGED:
            _, self._mtu = data

        elif event == _IRQ_GATTS_WRITE:
            _, handle = data
            if handle == self._rx:
                self._feed(self._ble.gatts_read(self._rx))

    def _feed(self, chunk):
        for byte in chunk:
            if byte in (0x0A, 0x0D):
                if self._line:
                    line = bytes(self._line)
                    self._line = bytearray()
                    self._dispatch(line)
            elif len(self._line) < RX_BUFFER:
                self._line.append(byte)
            else:
                self._line = bytearray()  # overlong: drop, do not desynchronise

    def _dispatch(self, line):
        if not line.startswith(b"{"):
            return
        try:
            message = json.loads(line)
        except (ValueError, MemoryError) as exc:
            print("buddy: bad JSON (%s)" % exc)
            return
        self.last_message = time.ticks_ms()
        self._on_message(message)

    @property
    def connected(self):
        return self._conn is not None

    @property
    def fresh(self):
        if not self.connected or not self.last_message:
            return False
        return time.ticks_diff(time.ticks_ms(), self.last_message) < STALE_MS

    def send(self, obj):
        """One JSON line, split at the MTU - gatts_notify sends a single
        packet and silently drops anything past it."""
        if self._conn is None:
            return False
        data = json.dumps(obj).encode() + b"\n"
        limit = max(1, self._mtu - 3)
        try:
            for start in range(0, len(data), limit):
                self._ble.gatts_notify(self._conn, self._tx, data[start : start + limit])
            return True
        except OSError as exc:
            print("buddy: notify failed: %s" % exc)
            return False


def apply_time(payload):
    """{"time": [epoch_seconds, tz_offset_seconds]} from the bridge.

    The board has no RTC of its own and machine.RTC starts at 1970, so the
    clock on screen is wrong until this arrives.
    """
    try:
        epoch, offset = payload[0], payload[1]
        import machine

        stamp = time.localtime(epoch + offset)
        machine.RTC().datetime(
            (stamp[0], stamp[1], stamp[2], stamp[6] + 1, stamp[3], stamp[4], stamp[5], 0)
        )
        print("buddy: clock set to %04d-%02d-%02d %02d:%02d" % stamp[:5])
        return True
    except Exception as exc:  # noqa: BLE001 - a bad payload must not stop the loop
        print("buddy: time sync failed: %s" % exc)
        return False


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

# The UI is English, so the built-in font covers it. This exists for one input
# that a human edits by hand: the pet's name in buddy_config.json.
#
# No built-in face carries anything outside 0x20-0x7F, and a missing glyph draws
# as a replacement box rather than reporting an error (CLAUDE.md section 7). So
# a name with diacritics is folded rather than shown as squares - wrong
# spelling is at least readable. If proper diacritics ever matter, section 7 and
# the root README document loading a custom .vlw font instead.
_FOLD = (
    ("ą", "a"), ("ć", "c"), ("ę", "e"), ("ł", "l"), ("ń", "n"),
    ("ó", "o"), ("ś", "s"), ("ź", "z"), ("ż", "z"),
    ("Ą", "A"), ("Ć", "C"), ("Ę", "E"), ("Ł", "L"), ("Ń", "N"),
    ("Ó", "O"), ("Ś", "S"), ("Ź", "Z"), ("Ż", "Z"),
)


def t(text):
    """Text as the built-in font can actually draw it."""
    for accented, plain in _FOLD:
        text = text.replace(accented, plain)
    return text


# --------------------------------------------------------------------------
# Palette and layout
# --------------------------------------------------------------------------

BLACK = 0x000000
WHITE = 0xFFFFFF
GREY = 0x707078
DARK = 0x2F2F38
BODY = 0xD97757
RED = 0xFF3B5C
GREEN = 0x4CD964
YELLOW = 0xFFD93D
BLUE = 0x4CA8FF

WIDTH = const(135)

PET_Y = const(23)
FOOT_Y = const(224)

# The top bar. The battery icon sits a shade below the text because its outline
# reads as taller than the characters beside it, but the percentage it prints
# shares the clock's line - two numbers at two heights looked like a mistake.
TOP_TEXT_Y = const(3)
BATTERY_Y = const(4)

# Rows are stacked from measured heights rather than fixed offsets. The first
# version hardcoded a y for every element and the name landed on top of both
# the level line and the hearts - guessing font metrics does not survive a
# change of text size.
GAP_TIGHT = const(4)
BAR_H = const(13)

# Widest the name may be before it drops to the smaller size. At size 2 a
# character is 12 px, so this allows eight of them.
NAME_MAX_W = const(100)


# Every icon here is drawn rather than typed: no built-in face carries anything
# outside 0x20-0x7F, so there is no heart, apple or smiley glyph to reach for
# (CLAUDE.md section 7).
#
# The heart is a bitmap rather than circles-plus-triangle. Two arcs meeting a
# point look lumpy at seven pixels across, and a smooth heart would sit oddly
# next to a pixel-art mascot anyway. Drawn on a grid it reads as deliberate.
HEART = (
    ".##.##.",
    "#######",
    "#######",
    ".#####.",
    "..###..",
    "...#...",
)
HEART_W = const(7)
HEART_H = const(6)
HEART_SCALE = const(2)


def draw_heart(x, y, fill, scale=HEART_SCALE):
    """Top-left anchored. `fill` is 1.0, 0.5 or 0.0.

    A half heart fills the left columns, so a row of them drains right to left
    the way the count does.
    """
    cut = HEART_W * fill
    for row_index, row in enumerate(HEART):
        run_start = None
        for col in range(HEART_W + 1):
            solid = col < HEART_W and row[col] == "#"
            colour = RED if (solid and col < cut) else DARK

            if solid and run_start is None:
                run_start, run_colour = col, colour
            elif run_start is not None and (not solid or colour != run_colour):
                Lcd.fillRect(
                    x + run_start * scale, y + row_index * scale,
                    (col - run_start) * scale, scale, run_colour,
                )
                run_start = col if solid else None
                run_colour = colour


def draw_apple(cx, cy, colour):
    Lcd.fillCircle(cx - 2, cy + 1, 4, colour)
    Lcd.fillCircle(cx + 2, cy + 1, 4, colour)
    Lcd.fillRect(cx - 1, cy - 6, 2, 4, 0x6B4A2F)


def draw_face(cx, cy, colour):
    Lcd.fillCircle(cx, cy, 6, colour)
    Lcd.fillCircle(cx - 2, cy - 2, 1, BLACK)
    Lcd.fillCircle(cx + 2, cy - 2, 1, BLACK)
    Lcd.drawLine(cx - 3, cy + 2, cx, cy + 3, BLACK)
    Lcd.drawLine(cx, cy + 3, cx + 3, cy + 2, BLACK)


def draw_battery(x, y, percent, charging):
    """Icon at (x, y), with the reading printed to its left.

    The number earns its place: the bar alone answers "roughly how full" but
    not "will this last the afternoon", and there is room for both.

    `y` positions the icon only - the percentage is pinned to TOP_TEXT_Y so it
    lines up with the clock across the bar. That ties this to the top bar, which
    is the only place it is used.
    """
    w, h = 22, 11

    colour = GREEN if percent >= 40 else (YELLOW if percent >= 15 else RED)
    Lcd.setTextSize(1)
    Lcd.setTextColor(colour, BLACK)
    Lcd.drawRightString("%d%%" % max(0, min(100, percent)), x - 4, TOP_TEXT_Y)

    Lcd.drawRoundRect(x, y, w, h, 2, GREY)
    Lcd.fillRect(x + w, y + 3, 2, h - 6, GREY)

    fill = int((w - 4) * max(0, min(100, percent)) / 100)
    if fill:
        Lcd.fillRect(x + 2, y + 2, fill, h - 4, colour)
    if charging:
        Lcd.fillTriangle(x + 9, y + 2, x + 13, y + 5, x + 10, y + 5, WHITE)
        Lcd.fillTriangle(x + 12, y + 9, x + 8, y + 6, x + 11, y + 6, WHITE)


def draw_dead(state, sprite):
    """Shown instead of everything else once the buddy has died.

    Navigation is blocked here for the same reason as when the bridge is away:
    hunger, limits and levels are all still rendered truthfully, but none of
    them can be acted on any more, so five screens of accurate numbers would
    only bury the one fact that matters and its one remedy.
    """
    Lcd.fillScreen(BLACK)
    centre = WIDTH // 2

    Lcd.setTextSize(1)
    Lcd.setTextColor(WHITE, BLACK)
    Lcd.drawString(state["clock"], 4, TOP_TEXT_Y)
    draw_battery(WIDTH - 28, BATTERY_Y, state["battery"], state["charging"])

    if sprite is not None:
        sprite.draw((WIDTH - sprite.screen_width) // 2, 56)

    name = t(state["name"])[:14]
    Lcd.setTextSize(name_size(name))
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString(name, centre, 150)

    Lcd.setTextSize(2)
    Lcd.setTextColor(RED, BLACK)
    Lcd.drawCenterString("died", centre, 174)

    Lcd.setTextSize(1)
    Lcd.setTextColor(DARK, BLACK)
    Lcd.drawCenterString("on the host, run", centre, 202)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString("buddyctl.py reset", centre, 216)


def draw_disconnected(state, sprite):
    """The only screen shown when the bridge is gone.

    Everything else on the device is a view of host state, so with the host
    away every other screen would be a museum of numbers that stopped being
    true. One honest screen beats five stale ones - and blocking the navigation
    while offline removes any doubt about which it is.
    """
    Lcd.fillScreen(BLACK)
    centre = WIDTH // 2

    Lcd.setTextSize(1)
    Lcd.setTextColor(WHITE, BLACK)
    Lcd.drawString(state["clock"], 4, TOP_TEXT_Y)
    draw_battery(WIDTH - 28, BATTERY_Y, state["battery"], state["charging"])

    if sprite is not None:
        sprite.draw((WIDTH - sprite.screen_width) // 2, 60)

    name = t(state["name"])[:14]
    Lcd.setTextSize(name_size(name))
    Lcd.setTextColor(BODY, BLACK)
    Lcd.drawCenterString(name, centre, 156)

    Lcd.setTextSize(1)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString("disconnected", centre, 182)
    Lcd.setTextColor(DARK, BLACK)
    Lcd.drawCenterString("waiting for the bridge", centre, 200)


def draw_bar(x, y, w, h, fraction, colour):
    Lcd.drawRoundRect(x, y, w, h, 2, GREY)
    inner = int((w - 4) * max(0.0, min(1.0, fraction)))
    if inner:
        Lcd.fillRect(x + 2, y + 2, inner, h - 4, colour)


def draw_hearts(cx, y, lives, maximum=5):
    """A row of `maximum` hearts, centred on cx, top-aligned at y."""
    width = HEART_W * HEART_SCALE
    gap = 4
    step = width + gap
    left = cx - (maximum * step - gap) // 2

    for i in range(maximum):
        remaining = lives - i
        fill = 1.0 if remaining >= 1 else (0.5 if remaining >= 0.5 else 0.0)
        draw_heart(left + i * step, y, fill)


def hearts_height():
    return HEART_H * HEART_SCALE


def compact(number):
    """1234567 -> '1.2M'. The bottom row has room for about eight characters."""
    if number >= 1000000:
        return "%.1fM" % (number / 1000000)
    if number >= 1000:
        return "%.0fk" % (number / 1000)
    return str(number)


def hours_left(seconds):
    """A countdown as '3:42h'. None when the bridge has no reset time.

    Hours and minutes, never seconds: the window is five hours long, and a
    ticking seconds field on a screen that repaints every few seconds would read
    as noise rather than as information.
    """
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds <= 0:
        return "0:00h"
    return "%d:%02dh" % (seconds // 3600, (seconds % 3600) // 60)


def days_left(seconds):
    """The weekly window as '4 days'.

    Below a day it falls back to hours, because '0 days' is the one answer that
    reads as broken rather than as nearly-there.
    """
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds < 86400:
        return hours_left(seconds)
    days = seconds // 86400
    return "%d day%s" % (days, "" if days == 1 else "s")


def name_size(name):
    """Largest text size the name fits in, capped at 2."""
    Lcd.setTextSize(2)
    return 2 if Lcd.textWidth(name) <= NAME_MAX_W else 1


def draw_main(state, sprite, exp_popup=""):
    Lcd.fillScreen(BLACK)
    centre = WIDTH // 2

    Lcd.setTextSize(1)
    Lcd.setTextColor(WHITE, BLACK)
    Lcd.drawString(state["clock"], 4, TOP_TEXT_Y)
    draw_battery(WIDTH - 28, BATTERY_Y, state["battery"], state["charging"])

    if sprite is not None:
        sprite.draw((WIDTH - sprite.screen_width) // 2, PET_Y + PET_NUDGE)
        y = PET_Y + sprite.screen_height
    else:
        Lcd.setTextColor(RED, BLACK)
        Lcd.drawCenterString("no sprites", centre, PET_Y + 30)
        y = PET_Y + 78

    # Floats over the pet for a moment after BtnA. Drawn last of the top half
    # so it sits on top of the sprite rather than being painted over by it.
    if exp_popup:
        Lcd.setTextSize(2)
        if exp_popup.startswith("+"):
            colour = GREEN
        elif exp_popup.startswith("LEVEL"):
            colour = YELLOW
        else:
            colour = GREY
        Lcd.setTextColor(colour, BLACK)
        Lcd.drawCenterString(exp_popup, centre, PET_Y + 4)

    # Measure every block first, then share whatever is left over between them.
    # Packing from the top instead left 42 px dead at the bottom and crowded
    # everything under the pet.
    name = t(state["name"])[:14]
    size = name_size(name)
    Lcd.setTextSize(size)
    name_h = Lcd.fontHeight()

    Lcd.setTextSize(1)
    line_h = Lcd.fontHeight()

    heights = (name_h, line_h, hearts_height(), BAR_H, BAR_H)
    spacing = max(GAP_TIGHT, (FOOT_Y - y - sum(heights)) // (len(heights) + 1))

    y += spacing
    Lcd.setTextSize(size)
    Lcd.setTextColor(BODY, BLACK)
    Lcd.drawCenterString(name, centre, y)
    y += name_h + spacing

    Lcd.setTextSize(1)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString(
        t("Lv.%d  %s" % (state["level"], state["title"]))[:20], centre, y
    )
    y += line_h + spacing

    draw_hearts(centre, y, state["lives"])
    y += hearts_height() + spacing

    draw_apple(12, y + BAR_H // 2, GREEN)
    draw_bar(24, y, WIDTH - 32, BAR_H, state["hunger"], GREEN)
    y += BAR_H + spacing

    draw_face(12, y + BAR_H // 2, YELLOW)
    draw_bar(24, y, WIDTH - 32, BAR_H, state["happiness"], YELLOW)

    # The rate limit is absent for non-subscription accounts and until the
    # first API response of a session, so "--" is a real state, not a bug.
    five = state.get("five_hour")
    Lcd.setTextColor(BLUE if five is not None else GREY, BLACK)
    Lcd.drawString("5h %s" % ("%d%%" % five if five is not None else "--"), 4, FOOT_Y)

    # Today's total, not the raw sum across sessions: that one falls whenever a
    # context is compacted or a terminal closes, which reads as work being
    # undone.
    Lcd.setTextColor(YELLOW, BLACK)
    Lcd.drawRightString(compact(state.get("tokens_today", 0)), WIDTH - 4, FOOT_Y)


# --------------------------------------------------------------------------
# The other screens
# --------------------------------------------------------------------------

SCREEN_MAIN = const(0)
SCREEN_CLAUDE = const(1)
SCREEN_FEED = const(2)
SCREEN_PET = const(3)
SCREEN_LEVEL = const(4)
SCREEN_COUNT = const(5)

SCREEN_NAMES = ("BUDDY", "CLAUDE", "FEEDING", "PETTING", "LEVEL")

# How long "+1 EXP" stays on screen after a tap.
EXP_POPUP_MS = const(900)

# A promotion is rarer and worth dwelling on.
LEVELUP_MS = const(4000)


def draw_header(index):
    Lcd.fillScreen(BLACK)
    Lcd.setTextSize(1)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString(SCREEN_NAMES[index], WIDTH // 2, 4)

    # Which of the five screens this is, as dots - cheaper to read at a glance
    # than a number, and it shows how many there are.
    #
    # The title above is 8 px tall from y=4, so it ends at 12; sitting the dots
    # at 16 put their top edge one pixel below it and the two read as one
    # smudged block.
    dot_y = 22
    step = 10
    left = WIDTH // 2 - (SCREEN_COUNT * step) // 2 + step // 2
    for i in range(SCREEN_COUNT):
        if i == index:
            Lcd.fillCircle(left + i * step, dot_y, 3, BODY)
        else:
            Lcd.fillCircle(left + i * step, dot_y, 2, DARK)


def draw_labelled_bar(y, label, value, colour, suffix="%", resets=None):
    """One limit: its name and number, when it rolls over, then the bar.

    Everything hangs off the measured text height rather than fixed offsets, so
    the block keeps its spacing if the captions ever change size.

    The countdown sits between the caption and the bar on purpose. A limit is
    two facts - how much is gone and how long until it comes back - and the
    second is useless anywhere but next to the first.
    """
    # The caption sits five pixels below the block's reference line, which pulls
    # it clear of whatever is above and closer to the bar it names - the three
    # rows belong together, and the gap upwards is what separates one limit from
    # the next.
    Lcd.setTextSize(1)
    label_y = y + 5
    reset_y = label_y + Lcd.fontHeight() + 2
    bar_y = reset_y + Lcd.fontHeight() + 4

    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawString(label, 6, label_y)

    if value is None:
        Lcd.setTextColor(GREY, BLACK)
        Lcd.drawRightString("--", WIDTH - 6, label_y)
        draw_bar(6, bar_y, WIDTH - 12, BAR_H, 0.0, DARK)
    else:
        Lcd.setTextColor(colour, BLACK)
        Lcd.drawRightString("%d%s" % (value, suffix), WIDTH - 6, label_y)
        draw_bar(6, bar_y, WIDTH - 12, BAR_H, value / 100.0, colour)

    # Absent for the same reasons the percentage is - no subscription, or no API
    # response yet - and absent on its own once a window has rolled over and
    # nothing has reported since. "--" is a real state here too.
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawString("resets in", 6, reset_y)
    Lcd.setTextColor(colour if resets else GREY, BLACK)
    Lcd.drawRightString(resets or "--", WIDTH - 6, reset_y)


def draw_claude(state):
    draw_header(SCREEN_CLAUDE)

    draw_labelled_bar(
        30, "5 hour limit", state.get("five_hour"), BLUE,
        resets=hours_left(state.get("five_hour_reset_in")),
    )
    draw_labelled_bar(
        86, "7 day limit", state.get("seven_day"), BODY,
        resets=days_left(state.get("seven_day_reset_in")),
    )

    sessions = state.get("sessions") or {}
    Lcd.setTextSize(1)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString("sessions", WIDTH // 2, 146)

    # Measured rather than hardcoded, same as the main screen: the caption sits
    # a fixed gap below whatever the big digit's height turns out to be.
    #
    # The gap is zero because the size-2 digits carry their own padding - the
    # glyphs do not reach the bottom of their 16 px cell - so a metric gap on top
    # of that read as a gulf between the number and its name.
    number_y = 160
    Lcd.setTextSize(2)
    label_y = number_y + Lcd.fontHeight()

    for i, (label, key, colour) in enumerate(
        (("all", "total", WHITE), ("busy", "busy", GREEN), ("idle", "idle", GREY))
    ):
        x = 8 + i * 44
        value = sessions.get(key, 0)

        Lcd.setTextSize(2)
        Lcd.setTextColor(colour if value else DARK, BLACK)
        Lcd.drawString(str(value), x, number_y)

        Lcd.setTextSize(1)
        Lcd.setTextColor(GREY, BLACK)
        Lcd.drawString(label, x, label_y)

    # Today's total, same figure as the main screen's corner. The raw
    # cross-session sum falls whenever a context is compacted, so it is not
    # shown anywhere.
    #
    # No cost line: on a Max plan the dollar figure is noise - the limits above
    # are what actually constrains the day.
    Lcd.setTextSize(1)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawString("tokens today", 6, 212)
    Lcd.setTextColor(YELLOW, BLACK)
    Lcd.drawRightString(compact(state.get("tokens_today", 0)), WIDTH - 6, 212)


# Below this the buddy is hungry enough to look it.
FEED_HUNGRY_AT = 0.5


def feed_pose(state):
    return "food" if state.get("hunger", 0.0) >= FEED_HUNGRY_AT else "dizzy"


def draw_feed(state, sprite):
    draw_header(SCREEN_FEED)

    y = SUB_PET_Y + 78 + SUB_GAP
    if sprite is not None:
        sprite.draw((WIDTH - sprite.screen_width) // 2, SUB_PET_Y)
        y = SUB_PET_Y + sprite.screen_height + SUB_GAP
    # The caption sits five pixels above the bar's reference line rather than on
    # it: the sprite above ends close by, and the two were reading as one block.
    label_y = y - 5
    Lcd.setTextSize(1)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawString("hunger", 6, label_y)
    Lcd.setTextColor(GREEN, BLACK)
    Lcd.drawRightString("%d%%" % int(state["hunger"] * 100), WIDTH - 6, label_y)
    draw_bar(6, y + 12, WIDTH - 12, BAR_H, state["hunger"], GREEN)

    # The debt is the point of this screen: how much work will fill the bar.
    debt = state.get("debt", 0)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawString("to feed", 6, y + 38)
    Lcd.setTextColor(BODY if debt else GREEN, BLACK)
    Lcd.drawRightString(
        "%s tok" % compact(debt) if debt else "full", WIDTH - 6, y + 38
    )

    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawString("spent today", 6, y + 56)
    Lcd.setTextColor(YELLOW, BLACK)
    Lcd.drawRightString(compact(state.get("tokens_today", 0)), WIDTH - 6, y + 56)

    Lcd.setTextColor(DARK, BLACK)
    Lcd.drawCenterString("spend tokens to feed", WIDTH // 2, y + 82)


def draw_tick(x, y, ok):
    """A tick or a cross, drawn - there is no glyph for either."""
    colour = GREEN if ok else RED
    if ok:
        Lcd.drawLine(x, y + 4, x + 3, y + 7, colour)
        Lcd.drawLine(x + 1, y + 4, x + 4, y + 7, colour)
        Lcd.drawLine(x + 3, y + 7, x + 9, y, colour)
        Lcd.drawLine(x + 4, y + 7, x + 10, y, colour)
    else:
        Lcd.drawLine(x, y, x + 8, y + 8, colour)
        Lcd.drawLine(x + 1, y, x + 9, y + 8, colour)
        Lcd.drawLine(x + 8, y, x, y + 8, colour)
        Lcd.drawLine(x + 9, y, x + 1, y + 8, colour)


def draw_level(state, sprite):
    draw_header(SCREEN_LEVEL)

    y = SUB_PET_Y + 78
    if sprite is not None:
        sprite.draw((WIDTH - sprite.screen_width) // 2, SUB_PET_Y + PET_NUDGE)
        y = SUB_PET_Y + sprite.screen_height

    centre = WIDTH // 2
    base = y  # bottom of the sprite; everything below is measured from here

    # The level sits slightly above the sprite's lower edge, and the block below
    # clears it by six pixels. Offsets are relative to the sprite rather than
    # absolute, so a taller pose pushes the whole group down with it.
    Lcd.setTextSize(2)
    Lcd.setTextColor(BODY, BLACK)
    Lcd.drawCenterString("Lv.%d" % state["level"], centre, base - 5)

    Lcd.setTextSize(1)
    Lcd.setTextColor(GREY, BLACK)
    y = base + 23
    Lcd.drawCenterString(t(state["title"])[:20], centre, y)
    y += Lcd.fontHeight() + 6

    progress = state.get("level_progress", 0.0)
    draw_bar(6, y, WIDTH - 12, BAR_H, progress, BODY)
    y += BAR_H + 5
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString("%d%% to next" % int(progress * 100), centre, y)

    goals = state.get("goals") or {}
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawString("today", 6, 190)
    Lcd.drawRightString("clicks", WIDTH - 6, 190)

    # Used, not remaining: it counts up with the day like everything else on
    # this screen, and the cap is right beside it.
    total = state.get("taps_total", 0)
    used = total - state.get("taps_left", 0)
    Lcd.setTextColor(GREY if used >= total else GREEN, BLACK)
    Lcd.drawRightString("%d/%d" % (used, total), WIDTH - 6, 204)

    for i, (label, key) in enumerate((("fed", "fed"), ("petted", "petted"))):
        y = 204 + i * 14
        ok = bool(goals.get(key))
        draw_tick(8, y, ok)
        Lcd.setTextColor(WHITE if ok else GREY, BLACK)
        Lcd.drawString(label, 24, y)

    # Missing either costs a heart at midnight, so it is worth saying plainly.
    if not (goals.get("fed") and goals.get("petted")):
        Lcd.setTextColor(RED, BLACK)
        Lcd.drawRightString("-1 heart", WIDTH - 6, 218)


# --------------------------------------------------------------------------
# Petting
# --------------------------------------------------------------------------

# Tilt drives a hand down onto the pet's head and back up. Constants rather
# than NVS settings: unlike test3, which ran from flash, ./run.sh redeploys this
# file in one command, so editing here is already the fast path.
STROKES_NEEDED = const(3)

# Which accelerometer axis the gesture uses, and which way round.
#
# Measured, not reasoned about: the first build read **ax** and the hand tracked
# an up/down tilt correctly - it was only drawn sideways. Switching to ay to
# match the new vertical drawing broke it, because ay is the *sideways* tilt.
# So ax it is.
#
# This is the third axis mistake in this repo and the lesson from sections 9 and
# 14 of CLAUDE.md holds: pick the axis from what the device actually reports,
# with tools/tilt_probe.py, rather than from reasoning about orientation.
TILT_AXIS = const(0)  # 0 = ax, 1 = ay, 2 = az - confirmed on the device
TILT_SIGN = -1  # confirmed by hand: +1 sent the paw the wrong way
TILT_GAIN = 2.2

# Travel, as a fraction of the way down from the calibrated rest pose. Contact
# and release are far apart on purpose: with a single threshold a hand held
# near the edge chatters between states and counts strokes nobody made.
#
# RELEASE_AT is low because the rest pose is now the top of the travel - the
# hand returns all the way to 0 between strokes, so there is no need to leave
# room above the release point.
CONTACT_AT = 0.82
RELEASE_AT = 0.25

HAND_TOP = const(58)
HAND_BOTTOM = const(140)
HAND_HALF_W = const(16)


class Petting:
    """Stroke counter driven by tilt, zeroed on however the stick is held.

    Only a lifted-to-touching crossing counts, so a stick held tilted racks up
    nothing: the hand has to come back up and down again, three times, which is
    what makes it feel like stroking rather than leaning.
    """

    def __init__(self):
        self.strokes = 0
        self.position = 0.0  # 0.0 lifted, 1.0 on the head
        self._rest = None
        self._touching = False

    @staticmethod
    def _read():
        try:
            return M5.Imu.getAccel()[TILT_AXIS]
        except Exception:  # noqa: BLE001 - IMU absent or busy
            return None

    def reset(self):
        """Take the current pose as "hand up".

        Calibrating on entry rather than assuming a resting value means it does
        not matter whether the stick is upright, flat on the desk or somewhere
        between - only the direction of the lean does. Fixing a rest value in
        code would have made "normal position" mean one specific orientation,
        and there is no reason it should.
        """
        self.strokes = 0
        self.position = 0.0
        self._touching = False
        self._rest = self._read()

    @property
    def y(self):
        return int(HAND_TOP + self.position * (HAND_BOTTOM - HAND_TOP))

    def update(self):
        """Read the IMU. Returns True on the stroke that completes a petting."""
        reading = self._read()
        if reading is None:
            return False

        if self._rest is None:
            self._rest = reading

        # Zero at the calibrated pose, so the hand starts at the top and only a
        # lean in the stroking direction brings it down; the other way clamps.
        self.position = max(
            0.0, min(1.0, TILT_SIGN * (reading - self._rest) * TILT_GAIN)
        )

        if self._touching:
            if self.position <= RELEASE_AT:
                self._touching = False
        elif self.position >= CONTACT_AT:
            self._touching = True
            self.strokes += 1
            if self.strokes >= STROKES_NEEDED:
                self.strokes = 0
                return True

        return False


def draw_hand(x, y, colour=BODY):
    """A paw reaching down.

    Built from fillRect and fillCircle only. Both are proven on this board;
    fillRoundRect is not on the verified list in CLAUDE.md and an AttributeError
    here would fire every frame of the mini-game.
    """
    Lcd.fillRect(x - 3, y - 7, 6, 8, colour)  # wrist
    Lcd.fillRect(x - 9, y, 18, 9, colour)  # palm
    Lcd.fillCircle(x - 9, y + 4, 4, colour)
    Lcd.fillCircle(x + 9, y + 4, 4, colour)
    for i in range(3):  # toes
        Lcd.fillCircle(x - 6 + i * 6, y + 11, 3, colour)


# The pet sits low so the hand has room to come down on its head. Drawn at 2x
# rather than the usual 3x for the same reason - at 3x it is 78 px tall and
# leaves no travel.
PET_SCREEN_ZOOM = const(2)
PET_SCREEN_Y = const(150)


def draw_pet_screen(state, sprite, petting, full):
    """Only the hand moves, so a full repaint is reserved for entering the
    screen - repainting everything at tilt rate would flicker badly."""
    centre = WIDTH // 2

    if full:
        draw_header(SCREEN_PET)
        if sprite is not None:
            sprite.draw(
                (WIDTH - sprite.width * PET_SCREEN_ZOOM) // 2,
                PET_SCREEN_Y,
                PET_SCREEN_ZOOM,
            )

        ready = state.get("can_pet", True)
        Lcd.setTextSize(1)
        Lcd.setTextColor(GREEN if ready else GREY, BLACK)
        Lcd.drawCenterString(
            "ready to pet" if ready else "already petted", centre, 210
        )
        Lcd.setTextColor(DARK, BLACK)
        Lcd.drawCenterString(
            "tilt down to stroke" if ready else "come back in an hour", centre, 224
        )

    # The hand only moves vertically now, so a narrow column is all that needs
    # clearing - a full-width wipe would be five times the pixels per frame.
    Lcd.fillRect(
        centre - HAND_HALF_W, HAND_TOP - 8,
        HAND_HALF_W * 2, (HAND_BOTTOM - HAND_TOP) + 26, BLACK,
    )
    draw_hand(centre, petting.y)

    # Three dots, one per stroke still needed.
    step = 14
    left = centre - (STROKES_NEEDED * step) // 2 + step // 2
    Lcd.fillRect(0, 36, WIDTH, 12, BLACK)
    for i in range(STROKES_NEEDED):
        if i < petting.strokes:
            Lcd.fillCircle(left + i * step, 42, 5, GREEN)
        else:
            Lcd.drawCircle(left + i * step, 42, 5, GREY)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

# What the bridge last told us. Defaults are what a buddy looks like before
# anyone has ever connected - not zeros, which would read as a starving pet.
STATE = {
    "name": "BUDDY",
    "level": 1,
    # Only ever seen if a snapshot arrives without a title; while the bridge is
    # away the device shows draw_disconnected() instead of any of this.
    "title": "Hatchling",
    "lives": 5.0,
    "hunger": 0.5,
    "happiness": 0.5,
    "tokens": 0,
    "tokens_today": 0,
    "cost": 0,
    "debt": 0,
    "five_hour": None,
    "seven_day": None,
    "sessions": {"total": 0, "busy": 0, "idle": 0},
    "pose": "sleeping",
    "level_pose": "normal",
    "level_progress": 0.0,
    "dead": False,
    "can_pet": True,
    "goals": {"fed": False, "petted": False},
    "taps_left": 100,
    "taps_total": 100,
}


def device_readings():
    try:
        battery = int(M5.Power.getBatteryLevel())
    except Exception:  # noqa: BLE001 - absent on some builds
        battery = 100
    try:
        charging = bool(M5.Power.isCharging())
    except Exception:  # noqa: BLE001
        charging = False

    now = time.localtime()
    return {
        "clock": "%02d:%02d" % (now[3], now[4]),
        "battery": battery,
        "charging": charging,
    }


def main():
    M5.begin()
    Lcd.setBrightness(90)
    Lcd.fillScreen(BLACK)

    poses = available_poses()
    print("buddy: %d sprites in %s" % (len(poses), SPRITE_DIR))
    if not poses:
        print("buddy: none found - run ./deploy_sprites.sh first")

    cache = SpriteCache()
    dirty = [True]
    flash = [None, 0]  # (pose, ticks_ms deadline) - a brief reaction

    def on_message(message):
        kind = message.get("kind")
        if kind == "state":
            # Copy everything the bridge sent, not just the keys that happen to
            # be in STATE already. The earlier version iterated over STATE and
            # so silently dropped every field added to the snapshot after it
            # was written - can_pet, goals, level_progress, debt - which left
            # those screens showing their defaults forever while the bridge
            # was plainly sending the real values.
            for key, value in message.items():
                if key != "kind":
                    STATE[key] = value
            dirty[0] = True
        elif kind == "petted":
            # The bridge answers every petting, rewarded or not. Show the
            # difference: affection is never refused, but only the first one
            # each hour earns anything.
            if message.get("rewarded"):
                flash[0], flash[1] = "love", time.ticks_add(time.ticks_ms(), 2500)
                print("buddy: petted, happiness %.2f" % message.get("happiness", 0))
            else:
                flash[0], flash[1] = "happy", time.ticks_add(time.ticks_ms(), 1500)
                print("buddy: petted, no reward (%s)" % message.get("reason"))
            dirty[0] = True
        elif kind == "levelup":
            # Overrides the pose everywhere, unlike the petting reaction: a
            # promotion is about the buddy, not about the screen you happen to
            # be on.
            deadline = time.ticks_add(time.ticks_ms(), LEVELUP_MS)
            levelup[0] = deadline
            exp_popup[0], exp_popup[1] = deadline, "LEVEL UP"
            print("buddy: level %s (%s)" % (message.get("level"), message.get("title")))
            dirty[0] = True
        elif "time" in message:
            if apply_time(message["time"]):
                dirty[0] = True
        else:
            print("buddy: unhandled message %s" % kind)

    link = Link(on_message)
    petting = Petting()

    screen = 0
    last_clock = ""
    last_pose = None
    entered = True  # the pet screen needs one full repaint when it opens
    was_online = [False]
    last_pet = ["", None]  # pose and readiness the pet screen last drew
    exp_popup = [0, ""]  # deadline and text of the tap popup
    levelup = [0]  # deadline of the promotion celebration

    try:
        while True:
            M5.update()

            # Buttons debounce themselves on this firmware and report edges
            # directly - the hand-rolled class in test2/test3 predates checking
            # that (CLAUDE.md section 15).
            online = link.fresh
            dead = bool(STATE.get("dead"))
            playable = online and not dead

            # A tap on the buddy. Only on the main screen, where the pet is the
            # thing being tapped; elsewhere BtnA would have no obvious target.
            if playable and screen == SCREEN_MAIN and M5.BtnA.wasClicked():
                # The bridge is the authority on the hourly cap, but waiting for
                # its reply would put a visible lag on every tap. The snapshot
                # already carries the remaining count, so decide locally and let
                # the next snapshot correct any drift.
                if STATE.get("taps_left", 0) > 0:
                    STATE["taps_left"] -= 1
                    link.send({"kind": "input", "event": "exp"})
                    exp_popup[1] = "+1 EXP"
                else:
                    exp_popup[1] = "daily cap"
                exp_popup[0] = time.ticks_add(time.ticks_ms(), EXP_POPUP_MS)
                dirty[0] = True

            # Navigation is disabled while the host is away or the buddy is
            # dead: every other screen is a view of state that can no longer be
            # acted on. The button going quiet is itself a signal.
            if playable and M5.BtnB.wasClicked():
                screen = (screen + 1) % SCREEN_COUNT
                dirty[0] = True
                entered = True
                if screen == SCREEN_PET:
                    petting.reset()

            if online != was_online[0]:
                was_online[0] = online
                dirty[0] = True
                entered = True
                if online:
                    print("buddy: bridge back")
                else:
                    print("buddy: bridge gone")

            readings = device_readings()

            pose = STATE["pose"] if online else "disconnected"
            if levelup[0]:
                if time.ticks_diff(levelup[0], time.ticks_ms()) > 0:
                    pose = "love"
                else:
                    levelup[0] = 0
                    dirty[0] = True

            # The petting reaction is scoped to the petting screen. It used to
            # replace `pose` outright, which leaked a happy mascot onto the main
            # and level screens seconds after a stroke - those show how the day
            # is going, not what the hand just did.
            if flash[0] and time.ticks_diff(flash[1], time.ticks_ms()) <= 0:
                flash[0] = None
                dirty[0] = True

            if readings["clock"] != last_clock or pose != last_pose:
                dirty[0] = True
                last_clock = readings["clock"]
                last_pose = pose

            view = dict(STATE)
            view.update(readings)
            view["pose"] = pose

            sprite = cache.get(pose) if poses else None

            # Expire the "+1 EXP" popup; one redraw on, one off, not per frame.
            showing_exp = exp_popup[0] and time.ticks_diff(exp_popup[0], time.ticks_ms()) > 0
            if exp_popup[0] and not showing_exp:
                exp_popup[0], exp_popup[1] = 0, ""
                dirty[0] = True

            if not online:
                if dirty[0]:
                    draw_disconnected(view, sprite)
                    dirty[0] = False
                    entered = False
            elif dead:
                if dirty[0]:
                    draw_dead(view, cache.get("heart_broken") if poses else None)
                    dirty[0] = False
                    entered = False
            elif screen == SCREEN_PET:
                # This screen picks its own pose: it is about the interaction,
                # not about how the session is going, so the bridge's choice
                # (busy, burning, sleeping) would be noise here.
                pet_pose = "happy" if flash[0] else "normal"
                ready = view.get("can_pet", True)

                # The static half - sprite and status text - is only painted on
                # a full repaint, so anything that changes it has to ask for
                # one. Without this the status stayed on "ready to pet" for as
                # long as the screen was open, whatever the host said.
                if pet_pose != last_pet[0] or ready != last_pet[1]:
                    last_pet[0], last_pet[1] = pet_pose, ready
                    entered = True

                # Runs every frame: the hand has to track the tilt smoothly.
                if petting.update():
                    link.send({"kind": "input", "event": "pet"})

                draw_pet_screen(
                    view, cache.get(pet_pose) if poses else None, petting, entered
                )
                entered = False
                dirty[0] = False
            elif dirty[0]:
                if screen == SCREEN_MAIN:
                    draw_main(view, sprite, exp_popup[1] if showing_exp else "")
                elif screen == SCREEN_CLAUDE:
                    draw_claude(view)
                elif screen == SCREEN_FEED:
                    # Local pose again: this screen is about the bar underneath
                    # it, so it shows wanting food or having eaten, not the
                    # mood the bridge picked.
                    draw_feed(
                        view,
                        cache.get(feed_pose(view)) if poses else None,
                    )
                elif screen == SCREEN_LEVEL:
                    # Always the level's own pose. This screen is about
                    # progress, so a hungry or busy mascot here would be
                    # answering a question nobody asked on it.
                    draw_level(
                        view,
                        cache.get(view.get("level_pose", "normal")) if poses else None,
                    )
                dirty[0] = False
                entered = False

            time.sleep_ms(20)

    except KeyboardInterrupt:
        print("buddy: stopped via Ctrl-C")


# Guarded so the module can be imported without starting the loop, which is how
# tools/screenshot.py draws the real screens on a laptop. MicroPython sets
# __name__ to "__main__" for the script it runs, so `mpremote run main.py` and a
# deployed /main.py both still start normally.
if __name__ == "__main__":
    main()
