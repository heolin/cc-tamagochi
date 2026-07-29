#!/usr/bin/env python3
"""Draw the buddy's screens on a laptop, for the README.

    python3 tools/screenshot.py            # -> docs/screens/*.png
    python3 tools/screenshot.py --scale 3  # bigger, for a close look

Needs Pillow. The stick is not involved.

**These are renders, not photographs.** They come from `device/main.py` itself:
this module stubs out `M5`, `Lcd` and `bluetooth`, imports the real drawing
functions, and feeds them made-up state. So the layout, colours, sprites and
every coordinate are exactly what the device does - a screenshot cannot drift
from the code, because it is produced by the code.

The one approximation is the font. LovyanGFX's built-in face is not available
here, so text is drawn with Pillow's default bitmap font while `fontHeight()`
and `textWidth()` return the device's real metrics (8 px and 6 px per character
at size 1). Layout is therefore accurate; letterforms are close, not identical.
"""

from __future__ import annotations

import argparse
import os
import sys
import types

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("needs Pillow:  uv pip install pillow")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEVICE = os.path.join(REPO, "device")
OUT = os.path.join(REPO, "docs", "screens")

WIDTH, HEIGHT = 135, 240

# The device's own metrics, not Pillow's. Everything in main.py positions itself
# from these, so they have to match the hardware or the layout would be a
# fiction.
CHAR_W, CHAR_H = 6, 8


class Canvas:
    """Enough of M5.Lcd to draw every screen."""

    def __init__(self):
        self.image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)
        self.size = 1
        self.fg = (255, 255, 255)
        self.bg = (0, 0, 0)
        self.font = ImageFont.load_default()

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _rgb(value):
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)

    def width(self):
        return WIDTH

    def height(self):
        return HEIGHT

    def setBrightness(self, _level):
        pass

    def setTextSize(self, size):
        self.size = int(size)

    def setTextColor(self, fg, bg=0x000000):
        self.fg, self.bg = self._rgb(fg), self._rgb(bg)

    def fontHeight(self):
        return CHAR_H * self.size

    def textWidth(self, text):
        return CHAR_W * self.size * len(text)

    # -- shapes ------------------------------------------------------------

    def fillScreen(self, colour):
        self.draw.rectangle([0, 0, WIDTH, HEIGHT], fill=self._rgb(colour))

    def fillRect(self, x, y, w, h, colour):
        if w <= 0 or h <= 0:
            return
        self.draw.rectangle([x, y, x + w - 1, y + h - 1], fill=self._rgb(colour))

    def drawLine(self, x0, y0, x1, y1, colour):
        self.draw.line([x0, y0, x1, y1], fill=self._rgb(colour))

    def fillCircle(self, cx, cy, r, colour):
        self.draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=self._rgb(colour))

    def drawCircle(self, cx, cy, r, colour):
        self.draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=self._rgb(colour))

    def fillTriangle(self, x0, y0, x1, y1, x2, y2, colour):
        self.draw.polygon([(x0, y0), (x1, y1), (x2, y2)], fill=self._rgb(colour))

    def drawRoundRect(self, x, y, w, h, r, colour):
        self.draw.rounded_rectangle(
            [x, y, x + w - 1, y + h - 1], radius=r, outline=self._rgb(colour)
        )

    # -- text --------------------------------------------------------------

    def _blit_text(self, text, x, y):
        """Render at 1x then scale by the text size, which is what the device
        does - so a size-2 string is genuinely blocky rather than smoothly
        larger."""
        if not text:
            return
        w = CHAR_W * len(text)
        tile = Image.new("RGB", (w, CHAR_H), self.bg)
        ImageDraw.Draw(tile).text((0, -2), text, font=self.font, fill=self.fg)
        if self.size != 1:
            tile = tile.resize((w * self.size, CHAR_H * self.size), Image.NEAREST)
        self.image.paste(tile, (int(x), int(y)))

    def drawString(self, text, x, y):
        self._blit_text(str(text), x, y)

    def drawCenterString(self, text, cx, y):
        text = str(text)
        self._blit_text(text, cx - self.textWidth(text) // 2, y)

    def drawRightString(self, text, right, y):
        text = str(text)
        self._blit_text(text, right - self.textWidth(text), y)


CANVAS = Canvas()


def install_stubs():
    """Make `import M5` and friends work on a laptop."""
    lcd = CANVAS

    m5 = types.ModuleType("M5")
    m5.Lcd = lcd
    m5.begin = lambda: None
    m5.update = lambda: None

    class _Btn:
        @staticmethod
        def wasClicked():
            return False

        @staticmethod
        def isPressed():
            return False

    m5.BtnA = m5.BtnB = _Btn()

    class _Power:
        @staticmethod
        def getBatteryLevel():
            return 78

        @staticmethod
        def isCharging():
            return False

    m5.Power = _Power()

    class _Imu:
        @staticmethod
        def getAccel():
            return (0.0, 0.0, 1.0)

    m5.Imu = _Imu()

    bt = types.ModuleType("bluetooth")
    bt.UUID = lambda value: value
    bt.BLE = object

    mp = types.ModuleType("micropython")
    mp.const = lambda value: value

    sys.modules["M5"] = m5
    sys.modules["bluetooth"] = bt
    sys.modules["micropython"] = mp


install_stubs()
sys.path.insert(0, DEVICE)

import main as buddy  # noqa: E402  - needs the stubs in place first

buddy.SPRITE_DIR = os.path.join(DEVICE, "sprites")


# ---------------------------------------------------------------------------
# The states worth showing
# ---------------------------------------------------------------------------

BASE = {
    "clock": "14:32",
    "battery": 78,
    "charging": False,
    "name": "KLAUDIUSZ",
    "level": 4,
    "level_progress": 0.42,
    "title": "Integrator",
    "lives": 4.5,
    "hunger": 0.72,
    "happiness": 0.61,
    "tokens": 184502,
    "tokens_today": 41200,
    "cost": 0.83,
    "debt": 4480,
    "five_hour": 23,
    "seven_day": 41,
    "sessions": {"total": 3, "busy": 1, "idle": 2},
    "taps_left": 53,
    "taps_total": 100,
    "dead": False,
    "can_pet": True,
    "goals": {"fed": True, "petted": False},
    "pose": "api",
    "level_pose": "api",
}


def shot(name, render):
    CANVAS.image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    CANVAS.draw = ImageDraw.Draw(CANVAS.image)
    render()
    return name, CANVAS.image.copy()


def sprite(pose):
    return buddy.Sprite(os.path.join(buddy.SPRITE_DIR, pose + ".spr"))


def all_screens():
    cache = {}

    def get(pose):
        if pose not in cache:
            cache[pose] = sprite(pose)
        return cache[pose]

    yield shot("main", lambda: buddy.draw_main(BASE, get(BASE["pose"])))

    hungry = dict(BASE, hunger=0.12, lives=2.0, happiness=0.22, pose="food")
    yield shot("main-hungry", lambda: buddy.draw_main(hungry, get("food")))

    yield shot("main-tap", lambda: buddy.draw_main(BASE, get(BASE["pose"]), "+1 EXP"))

    yield shot("claude", lambda: buddy.draw_claude(BASE))

    yield shot("feeding", lambda: buddy.draw_feed(BASE, get(buddy.feed_pose(BASE))))

    petting = buddy.Petting()
    petting.position, petting.strokes = 0.95, 2
    yield shot(
        "petting", lambda: buddy.draw_pet_screen(BASE, get("normal"), petting, True)
    )

    yield shot("level", lambda: buddy.draw_level(BASE, get(BASE["level_pose"])))

    yield shot(
        "disconnected", lambda: buddy.draw_disconnected(BASE, get("disconnected"))
    )

    dead = dict(BASE, dead=True, lives=0.0)
    yield shot("dead", lambda: buddy.draw_dead(dead, get("heart_broken")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--scale", type=int, default=2, help="pixels per screen pixel")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    for name, image in all_screens():
        if args.scale != 1:
            image = image.resize(
                (WIDTH * args.scale, HEIGHT * args.scale), Image.NEAREST
            )
        path = os.path.join(args.out, name + ".png")
        image.save(path)
        print(f"{name:16} {image.size[0]}x{image.size[1]}  {path}")


if __name__ == "__main__":
    main()
