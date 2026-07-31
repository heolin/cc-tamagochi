#!/usr/bin/env python3
"""Draw the architecture diagram the README embeds.

    python3 tools/diagram.py            # -> docs/architecture-{light,dark}.png
    python3 tools/diagram.py --scale 3  # bigger, for a poster

Needs Pillow. Nothing else is involved - no hardware, no network.

Two versions come out, because GitHub renders READMEs in whichever theme the
reader picked and a diagram with a baked-in white background glares in the dark
one. The README picks between them with a `<picture>` element.

Committed as PNGs on purpose: a reader should see the picture without running
anything, and the script is here so the picture cannot quietly stop matching
the code. Re-run it when the data flow changes.
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("needs Pillow:  uv pip install pillow")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(REPO, "docs")

# The mascot's own colours, so the diagram belongs to the same project as the
# screens beside it in the README.
BODY = "#D97757"
BLUE = "#4CA8FF"
GREEN = "#4CD964"

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


class Theme:
    def __init__(self, name, bg, card, border, ink, dim, rule):
        self.name = name
        self.bg = bg
        self.card = card
        self.border = border
        self.ink = ink
        self.dim = dim
        self.rule = rule


# Backgrounds match GitHub's own two canvases, so the image reads as part of the
# page rather than as a rectangle pasted onto it.
LIGHT = Theme("light", "#FFFFFF", "#F6F8FA", "#D8DEE4", "#1F2328", "#6E7781", "#C9D1D9")
DARK = Theme("dark", "#0D1117", "#161B22", "#30363D", "#E6EDF3", "#8B949E", "#3D444D")


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        # Any machine without DejaVu still gets a diagram, just an uglier one.
        return ImageFont.load_default()


def mono(size, bold=False):
    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.load_default()


# Logical canvas. Everything below is in these units and multiplied by --scale
# on the way out, so the layout is written once and rendered at any size.
W, H = 1240, 640

# The three columns, and the gaps between them. The gaps are wide enough to
# carry the arrow captions: they are the labels that explain the interfaces, and
# an arrow with its caption tucked under a neighbouring card is worse than no
# caption at all.
COL1_X, COL1_W = 40, 280
COL2_X, COL2_W = 500, 320
COL3_X, COL3_W = 900, 300
BOUNDARY_X = 862  # dotted rule: host on the left, the desk on the right


class Sheet:
    def __init__(self, theme, scale):
        self.theme = theme
        self.scale = scale
        self.image = Image.new("RGB", (W * scale, H * scale), theme.bg)
        self.draw = ImageDraw.Draw(self.image)

    def s(self, *values):
        return [v * self.scale for v in values]

    def card(self, x, y, w, h, accent=None):
        """A panel. The accent is a 3 px bar down its left edge - the one piece
        of colour that says which of the three parties this box belongs to."""
        x0, y0, x1, y1 = self.s(x, y, x + w, y + h)
        self.draw.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=10 * self.scale,
            fill=self.theme.card,
            outline=self.theme.border,
            width=max(1, self.scale),
        )
        if accent:
            self.draw.rounded_rectangle(
                (x0, y0 + 10 * self.scale, x0 + 3 * self.scale, y1 - 10 * self.scale),
                radius=2 * self.scale,
                fill=accent,
            )

    def text(self, x, y, string, size=15, bold=False, colour=None, code=False, centre=False):
        face = (mono if code else font)(size * self.scale, bold)
        colour = colour or self.theme.ink
        px, py = self.s(x, y)
        if centre:
            px -= self.draw.textlength(string, font=face) / 2
        self.draw.text((px, py), string, font=face, fill=colour)

    def width_of(self, string, size=15, bold=False, code=False):
        face = (mono if code else font)(size * self.scale, bold)
        return self.draw.textlength(string, font=face) / self.scale

    def arrow(self, x0, y0, x1, y1, colour, dashed=False):
        """A straight arrow. Only horizontal ones are needed, so the head is
        always drawn on the horizontal axis."""
        width = max(1, round(2 * self.scale))
        if dashed:
            step, x = 9, x0
            while x < x1:
                self.draw.line(self.s(x, y0, min(x + 5, x1), y1), fill=colour, width=width)
                x += step
        else:
            self.draw.line(self.s(x0, y0, x1, y1), fill=colour, width=width)

        head = 6
        tip_x = x1 if x1 > x0 else x1
        direction = 1 if x1 > x0 else -1
        self.draw.polygon(
            self.s(
                tip_x, y1,
                tip_x - head * direction, y1 - head * 0.6,
                tip_x - head * direction, y1 + head * 0.6,
            ),
            fill=colour,
        )

    def rule(self, x, y0, y1):
        """A dotted vertical divider - the machine boundary."""
        y = y0
        while y < y1:
            self.draw.line(self.s(x, y, x, min(y + 4, y1)), fill=self.theme.rule,
                           width=max(1, self.scale))
            y += 10


def compose(theme, scale):
    sheet = Sheet(theme, scale)
    dim, ink = theme.dim, theme.ink

    sheet.text(40, 34, "How it fits together", size=22, bold=True)
    sheet.text(
        40, 68,
        "Two read-only sources on your machine, one daemon that owns the game, "
        "one screen on the desk.",
        size=14, colour=dim,
    )

    # The dotted rule stops above the BLE caption rather than running the full
    # height: the caption describes the crossing, so a line through it would be
    # drawing the boundary twice.
    sheet.rule(BOUNDARY_X, 128, 400)

    # -- column 1: what Claude Code exposes ---------------------------------
    sheet.text(COL1_X, 112, "CLAUDE CODE", size=11, bold=True, colour=dim)

    sheet.card(COL1_X, 136, COL1_W, 108, accent=BLUE)
    sheet.text(62, 154, "statusLine", size=16, bold=True)
    sheet.text(62, 180, "bridge/statusline.py", size=11, code=True, colour=dim)
    sheet.text(62, 202, "tokens · 5 h and 7 d limits · cost", size=11, colour=dim)
    sheet.text(62, 220, "the only place limits exist at all", size=11, colour=BLUE)

    sheet.card(COL1_X, 268, COL1_W, 96, accent=BLUE)
    sheet.text(62, 286, "~/.claude/sessions/", size=13, bold=True, code=True)
    sheet.text(62, 310, "one file per session", size=11, colour=dim)
    sheet.text(62, 330, "who is alive, who is busy", size=11, colour=dim)

    # -- column 2: the bridge ----------------------------------------------
    sheet.text(COL2_X, 112, "BRIDGE  ·  systemd user unit, no root",
               size=11, bold=True, colour=dim)

    sheet.card(COL2_X, 136, COL2_W, 268, accent=BODY)
    sheet.text(524, 154, "bridge.py", size=16, bold=True, code=True)
    sheet.text(524, 184, "state.py", size=12, bold=True, code=True, colour=BODY)
    sheet.text(524, 204, "usage per session, summed", size=11, colour=dim)
    sheet.text(524, 230, "sessions.py", size=12, bold=True, code=True, colour=BODY)
    sheet.text(524, 250, "liveness by pid + start time", size=11, colour=dim)
    sheet.text(524, 276, "game.py", size=12, bold=True, code=True, colour=BODY)
    sheet.text(524, 296, "hunger, hearts, levels, midnight", size=11, colour=dim)
    sheet.text(524, 322, "buddy.json", size=12, bold=True, code=True, colour=BODY)
    sheet.text(524, 342, "saved every 30 s, stays on this disk", size=11, colour=dim)
    sheet.text(524, 368, "ble.py", size=12, bold=True, code=True, colour=BODY)
    sheet.text(524, 388, "one link, rebuilt when it wedges", size=11, colour=dim)

    # -- column 3: the stick ------------------------------------------------
    sheet.text(COL3_X, 112, "M5STICKC S3", size=11, bold=True, colour=dim)

    sheet.card(COL3_X, 136, COL3_W, 216, accent=GREEN)
    sheet.text(924, 154, "device/main.py", size=15, bold=True, code=True)
    sheet.text(924, 182, "MicroPython on UIFlow2", size=11, colour=dim)
    sheet.text(924, 208, "five screens", size=12, bold=True)
    sheet.text(924, 230, "buddy · Claude · feeding", size=11, colour=dim)
    sheet.text(924, 248, "petting · level", size=11, colour=dim)
    sheet.text(924, 276, "two buttons + tilt", size=12, bold=True)
    sheet.text(924, 298, "BtnA taps, BtnB switches screen", size=11, colour=dim)
    sheet.text(924, 316, "tilt strokes the animal", size=11, colour=dim)

    # -- arrows -------------------------------------------------------------
    # Captions live in the gap they belong to, centred on the arrow: what the
    # interface is above the line, what carries it below.
    gap = (COL1_X + COL1_W + COL2_X) / 2

    sheet.arrow(COL1_X + COL1_W + 6, 190, COL2_X - 6, 190, BLUE)
    sheet.text(gap, 158, "one line per prompt redraw", size=10, colour=dim, centre=True)
    sheet.text(gap, 200, "UNIX socket, mode 0600", size=10, code=True, colour=dim, centre=True)

    sheet.arrow(COL1_X + COL1_W + 6, 312, COL2_X - 6, 312, BLUE)
    sheet.text(gap, 280, "polled every 2 s", size=10, colour=dim, centre=True)
    sheet.text(gap, 322, "read-only, no cooperation", size=10, colour=dim, centre=True)

    sheet.arrow(COL2_X + COL2_W + 6, 200, COL3_X - 6, 200, BODY)
    sheet.arrow(COL3_X - 6, 300, COL2_X + COL2_W + 6, 300, GREEN)

    sheet.text(BOUNDARY_X, 424, "BLE · Nordic UART · UTF-8 JSON lines",
               size=12, bold=True, centre=True)
    sheet.text(BOUNDARY_X, 448, "full snapshot every 3 s, and again on every change",
               size=11, colour=dim, centre=True)
    sheet.text(BOUNDARY_X, 468, "taps and tilt come back the same way",
               size=11, colour=dim, centre=True)

    # -- the footer: what does not happen -----------------------------------
    sheet.draw.line(sheet.s(40, 508, W - 40, 508), fill=theme.border, width=max(1, scale))

    sheet.text(40, 530, "What never leaves this picture", size=14, bold=True)
    notes = (
        ("no network", "the bridge speaks BLE to one known address, nothing else"),
        ("no transcripts", "conversations, prompts and tool calls are never read"),
        ("no hooks", "nothing here can slow, approve or block a tool call"),
    )
    x = 40
    for label, detail in notes:
        sheet.text(x, 562, label, size=13, bold=True, colour=GREEN)
        sheet.text(x, 586, detail, size=11, colour=dim)
        x += 400

    return sheet.image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--scale", type=int, default=2, help="pixels per logical unit")
    parser.add_argument("--out", default=OUT, help="where to write the PNGs")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    for theme in (LIGHT, DARK):
        image = compose(theme, args.scale)
        path = os.path.join(args.out, f"architecture-{theme.name}.png")
        image.save(path)
        print(f"{theme.name:6} {image.width}x{image.height}  {path}")


if __name__ == "__main__":
    main()
