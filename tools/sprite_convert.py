#!/usr/bin/env python3
"""Turn the mascot PNGs into sprites the stick can draw.

    python3 tools/sprite_convert.py --ascii            # look before you upload
    python3 tools/sprite_convert.py --anchors          # alignment table only
    python3 tools/sprite_convert.py --write out/       # emit .spr files

Runs on the host, needs PIL. The stick never sees a PNG.

## Why this is not just a resize

The source art is pixel art that was scaled up by a **non-integer** factor, so
its blocks alternate between 10 and 11 pixels. Two consequences:

* Averaging (any normal resize filter) blends neighbouring art pixels and turns
  a crisp 2-colour sprite into mush. This samples the **centre of each cell**
  instead, which recovers the original art exactly.
* The grid *phase* differs per frame - measured offsets of 0.00, 4.00, 5.25,
  10.00 px - so one global grid cannot fit them all. Fitting each frame to its
  own grid gives a relative error of 0.02; a shared grid gives 0.25, which is
  what you would get from random noise. Hence `fit_axis` runs per file.

Measured across all 21 frames: cell 10.69-10.75 px, normalised resolution
39 x 34 logical pixels.

## Alignment

The poses were not drawn on a common baseline - the body sits at heights
spanning ~11 cells. Left alone, the buddy jumps every time its mood changes.
Each frame is therefore anchored on its **body rectangle**: the largest solid
block of body colour, whose bottom edge and centre column are the feet-line and
the spine. Two frames find a prop instead and are corrected in EXCEPTIONS.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("needs Pillow:  uv pip install pillow")

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "device", "raw_images")

BODY = (217, 119, 87)  # #D97757, the mascot's fill - present in every frame
ALPHA_FLOOR = 16

# Search bounds for the per-frame cell size, in source pixels. Measured values
# cluster at 10.7; the range is wide enough to catch a re-export at a different
# scale and narrow enough to stay fast.
CELL_MIN, CELL_MAX = 6.0, 20.0

MAGIC = b"BSP1"

# Frames where the largest solid body-coloured block is a prop rather than the
# mascot. The value is a (dx, dy) nudge in logical pixels applied after the
# automatic anchor, measured by eye from --anchors output.
EXCEPTIONS: dict[str, tuple[int, int]] = {
    # filled in once --anchors has been eyeballed; see README
}


# ---------------------------------------------------------------------------
# Grid fitting
# ---------------------------------------------------------------------------


def transitions(px, w: int, h: int) -> tuple[list[int], list[int]]:
    """Coordinates where the colour changes - the art's cell boundaries."""
    tx, ty = set(), set()
    for y in range(h):
        for x in range(1, w):
            if px[x, y] != px[x - 1, y]:
                tx.add(x)
    for x in range(w):
        for y in range(1, h):
            if px[x, y] != px[x, y - 1]:
                ty.add(y)
    return sorted(tx), sorted(ty)


def fit_axis(points: list[int]) -> tuple[float, float, float]:
    """Best (cell, origin, error) for one axis.

    Scored on error **relative to the cell size**. Absolute error is
    degenerate: a finer grid always fits better, so minimising it just returns
    the smallest candidate. Relative error is ~0.25 for an arbitrary grid and
    near 0 for the real one, which makes the answer unambiguous.
    """
    if not points:
        return 1.0, 0.0, 1.0

    best = (float("inf"), 1.0, 0.0)
    for hundredths in range(int(CELL_MIN * 100), int(CELL_MAX * 100)):
        cell = hundredths / 100.0
        for quarter in range(int(cell * 4)):
            origin = quarter / 4.0
            err = sum(
                abs((p - origin) - round((p - origin) / cell) * cell) for p in points
            ) / len(points) / cell
            if err < best[0]:
                best = (err, cell, origin)
    err, cell, origin = best
    return cell, origin, err


def normalise(path: str):
    """Source PNG -> grid of RGBA tuples at the art's true resolution."""
    image = Image.open(path).convert("RGBA")
    w, h = image.size
    px = image.load()

    tx, ty = transitions(px, w, h)
    cell_x, origin_x, err_x = fit_axis(tx)
    cell_y, origin_y, err_y = fit_axis(ty)

    cols = int((w - origin_x) // cell_x)
    rows = int((h - origin_y) // cell_y)

    grid = []
    for gy in range(rows):
        row = []
        for gx in range(cols):
            sx = min(int(origin_x + (gx + 0.5) * cell_x), w - 1)
            sy = min(int(origin_y + (gy + 0.5) * cell_y), h - 1)
            r, g, b, a = px[sx, sy]
            row.append(None if a < ALPHA_FLOOR else (r, g, b))
        grid.append(row)

    return grid, {"cell": (cell_x, cell_y), "error": (err_x, err_y), "size": (cols, rows)}


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def body_rect(grid) -> tuple[int, int, int, int] | None:
    """Bounding box of the mascot: the largest connected blob of body colour.

    An earlier version took the largest solid *rectangle*, which was wrong in a
    way worth recording. A rectangle finds whichever block happens to have the
    greatest area, and that is a different feature in different poses - the
    torso (14 wide) in most, the outstretched arms (20) in `sleeping`, a narrow
    slice (7) in `food` and `warning`. Three features means three baselines,
    and the mascot visibly hopped between them.

    Connected components are stable because `#D97757` appears **only** on the
    mascot: every prop - the bowl, the fire, the hard hat, the warning dialog -
    is drawn in other colours. The blob is therefore the whole animal, whatever
    pose it is in, and its bottom edge is always the feet.

    It also handles `children_agents`, where the small companions are separate
    blobs and the parent wins on size.
    """
    if not grid:
        return None
    rows, cols = len(grid), len(grid[0])

    seen = [[False] * cols for _ in range(rows)]
    best = (0, None)

    for sy in range(rows):
        for sx in range(cols):
            if seen[sy][sx] or grid[sy][sx] != BODY:
                continue

            # Iterative flood fill; recursion would hit MicroPython-sized
            # limits on the larger blobs and this file should stay portable.
            stack = [(sx, sy)]
            seen[sy][sx] = True
            count = 0
            x0 = x1 = sx
            y0 = y1 = sy

            while stack:
                x, y = stack.pop()
                count += 1
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < cols and 0 <= ny < rows:
                        if not seen[ny][nx] and grid[ny][nx] == BODY:
                            seen[ny][nx] = True
                            stack.append((nx, ny))

            if count > best[0]:
                best = (count, (x0, y0, x1, y1))

    return best[1]


def anchor_of(grid, name: str):
    """(spine column, feet row) that this frame should be aligned on.

    Feet come from the blob's bottom edge. The spine is the **median** x of the
    mascot's pixels, not the middle of its bounding box: an outstretched arm
    widens the box on one side only and drags its centre with it, which slid
    poses sideways relative to each other. The median is dominated by the
    torso, which is where most of the pixels are, so it stays put.
    """
    rect = body_rect(grid)
    if rect is None:
        return None
    _x0, _y0, _x1, y1 = rect

    xs = sorted(x for row in grid for x, v in enumerate(row) if v == BODY)
    spine = xs[len(xs) // 2]

    dx, dy = EXCEPTIONS.get(name, (0, 0))
    return spine + dx, y1 + dy


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def pose_name(filename: str) -> str:
    """Sprite name from a source filename.

    Whitespace is stripped before the prefix, because a stray space in the
    middle of `claude_app.png` once produced a sprite called `c laude_app` that
    nothing referenced - and a source file named by hand will collect one again.
    """
    return filename[:-4].replace(" ", "").replace("claude_", "")


def load_all(directory: str):
    frames = {}
    try:
        names = sorted(f for f in os.listdir(directory) if f.endswith(".png"))
    except OSError:
        names = []

    # The artwork is not redistributable, so a clean checkout has the converted
    # sprites but none of the sources. That is a normal state, not a broken
    # install - say so rather than dying on an empty dict three functions later.
    if not names:
        raise SystemExit(
            f"no PNGs in {directory}\n"
            "The mascot pack is not committed - see device/raw_images/README.md.\n"
            "device/sprites/*.spr is committed, so ./deploy.sh sprites works without it."
        )

    for filename in names:
        name = pose_name(filename)
        grid, info = normalise(os.path.join(directory, filename))
        frames[name] = {"grid": grid, "info": info, "anchor": anchor_of(grid, name)}
    return frames


def align(frames):
    """Shift every frame so all anchors coincide, then crop to a shared window.

    A shared window is the point: crop each frame to its own content and the
    mascot drifts, because a pose with a raised arm has a taller bounding box
    than one without.
    """
    anchored = [f for f in frames.values() if f["anchor"]]
    if not anchored:
        raise SystemExit("no frame had a detectable body - check BODY colour")

    ax = max(f["anchor"][0] for f in anchored)
    ay = max(f["anchor"][1] for f in anchored)

    # Extent needed around the shared anchor, over every frame.
    left = right = top = bottom = 0
    for frame in frames.values():
        if not frame["anchor"]:
            continue
        cx, cy = frame["anchor"]
        rows, cols = len(frame["grid"]), len(frame["grid"][0])
        for y in range(rows):
            for x in range(cols):
                if frame["grid"][y][x] is None:
                    continue
                left = min(left, x - cx)
                right = max(right, x - cx)
                top = min(top, y - cy)
                bottom = max(bottom, y - cy)

    width, height = right - left + 1, bottom - top + 1

    for frame in frames.values():
        if not frame["anchor"]:
            frame["aligned"] = None
            continue
        cx, cy = frame["anchor"]
        out = [[None] * width for _ in range(height)]
        rows, cols = len(frame["grid"]), len(frame["grid"][0])
        for y in range(rows):
            for x in range(cols):
                value = frame["grid"][y][x]
                if value is None:
                    continue
                nx, ny = x - cx - left, y - cy - top
                if 0 <= nx < width and 0 <= ny < height:
                    out[ny][nx] = value
        frame["aligned"] = out

    return (width, height), (-left, -top), (ax, ay)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def encode(aligned) -> bytes:
    """RLE rows over a per-frame palette. Index 0 is transparent.

    Run-length because the device redraws by calling fillRect per run: pixel
    art is mostly long runs, so this is a few hundred calls instead of the
    thousand-plus that per-pixel drawing would need.
    """
    height, width = len(aligned), len(aligned[0])

    palette: list[tuple[int, int, int]] = []
    index_of: dict[tuple[int, int, int] | None, int] = {None: 0}
    for row in aligned:
        for value in row:
            if value is not None and value not in index_of:
                index_of[value] = len(palette) + 1
                palette.append(value)

    if len(palette) > 254:
        raise SystemExit("more than 254 colours in one frame")

    body = bytearray()
    for row in aligned:
        runs = bytearray()
        count = 0
        current = index_of[row[0]]
        for value in row:
            index = index_of[value]
            if index == current and count < 255:
                count += 1
            else:
                runs += bytes((count, current))
                current, count = index, 1
        runs += bytes((count, current))
        body += struct.pack("B", len(runs) // 2) + runs

    header = MAGIC + struct.pack("BBB", width, height, len(palette))
    for r, g, b in palette:
        header += bytes((r, g, b))
    return bytes(header) + bytes(body)


GLYPHS = {BODY: "#", (47, 47, 56): "@", (255, 255, 255): "o"}


def to_ascii(grid) -> list[str]:
    lines = []
    for row in grid:
        lines.append("".join("." if v is None else GLYPHS.get(v, "+") for v in row))
    return lines


SCREEN_BG = (16, 16, 20)  # roughly what the stick shows: near-black
GUIDE_FEET = (255, 60, 90)
GUIDE_SPINE = (60, 170, 255)


def render(grid, zoom: int, background=SCREEN_BG, guides=None):
    """One aligned frame as an image, at integer zoom.

    `guides` is (spine_column, feet_row): a blue vertical and a red horizontal
    line through the anchor. Without them a single frame is unjudgeable - the
    eye has nothing to compare against, and horizontal drift in particular is
    invisible until two frames are put side by side.
    """
    height, width = len(grid), len(grid[0])
    image = Image.new("RGB", (width * zoom, height * zoom), background)
    px = image.load()
    for y, row in enumerate(grid):
        for x, value in enumerate(row):
            if value is None:
                continue
            for dy in range(zoom):
                for dx in range(zoom):
                    px[x * zoom + dx, y * zoom + dy] = value

    if guides:
        from PIL import ImageDraw

        spine, feet = guides
        draw = ImageDraw.Draw(image)
        draw.line([(0, feet * zoom), (width * zoom - 1, feet * zoom)], fill=GUIDE_FEET)
        draw.line(
            [(spine * zoom, 0), (spine * zoom, height * zoom - 1)], fill=GUIDE_SPINE
        )
    return image


def contact_sheet(frames, zoom: int, feet_row: int, spine_col: int):
    """All frames side by side with a shared baseline drawn through them.

    This is the check that matters. Misalignment is obvious the moment the
    mascots do not stand on one line, and no amount of re-measuring the
    detector can substitute for looking - the detector is what is in doubt.
    """
    from PIL import ImageDraw

    usable = [(n, f["aligned"]) for n, f in frames.items() if f["aligned"]]
    if not usable:
        return None

    height, width = len(usable[0][1]), len(usable[0][1][0])
    cell_w, cell_h = width * zoom, height * zoom
    label_h = 14
    columns = 5
    rows = (len(usable) + columns - 1) // columns

    sheet = Image.new(
        "RGB", (columns * cell_w, rows * (cell_h + label_h)), (8, 8, 10)
    )
    draw = ImageDraw.Draw(sheet)

    for index, (name, grid) in enumerate(usable):
        cx = (index % columns) * cell_w
        cy = (index // columns) * (cell_h + label_h)
        sheet.paste(render(grid, zoom, guides=(spine_col, feet_row)), (cx, cy))
        draw.text((cx + 3, cy + cell_h + 2), name, fill=(200, 200, 210))

    return sheet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--raw", default=RAW, help="directory of source PNGs")
    parser.add_argument("--ascii", action="store_true", help="print every frame")
    parser.add_argument("--anchors", action="store_true", help="print the alignment table")
    parser.add_argument("--write", metavar="DIR", help="write .spr files here")
    parser.add_argument("--png", metavar="DIR", help="write PNGs and a contact sheet here")
    parser.add_argument("--zoom", type=int, default=6, help="pixels per art pixel in --png")
    args = parser.parse_args()

    frames = load_all(args.raw)
    (width, height), (ox, oy), _ = align(frames)

    print(f"{len(frames)} frames, aligned canvas {width} x {height} logical pixels\n")

    if args.anchors or not (args.ascii or args.write):
        print(f"{'frame':32} {'source':9} {'cell':13} {'fit err':13} {'anchor'}")
        for name, frame in frames.items():
            cx, cy = frame["info"]["cell"]
            ex, ey = frame["info"]["error"]
            cols, rows = frame["info"]["size"]
            mark = "" if frame["anchor"] else "   NO BODY FOUND"
            anchor = frame["anchor"] or ("-", "-")
            print(
                f"{name:32} {cols:3}x{rows:<5} {cx:5.2f}x{cy:<6.2f} "
                f"{ex:.3f}/{ey:<7.3f} {anchor[0]:3},{anchor[1]:<3}{mark}"
            )
        feet = {f["anchor"][1] for f in frames.values() if f["anchor"]}
        print(f"\nfeet rows before alignment: {sorted(feet)}")

    if args.ascii:
        for name, frame in frames.items():
            if frame["aligned"] is None:
                print(f"-- {name}: SKIPPED, no body found\n")
                continue
            print(f"-- {name}")
            for line in to_ascii(frame["aligned"]):
                print("   " + line)
            print()

    if args.png:
        os.makedirs(args.png, exist_ok=True)
        for name, frame in frames.items():
            if frame["aligned"] is None:
                continue
            out = os.path.join(args.png, name + ".png")
            # ox, oy are where the shared anchor landed in the aligned canvas.
            render(frame["aligned"], args.zoom, guides=(ox, oy)).save(out)

        sheet = contact_sheet(frames, args.zoom, oy, ox)
        if sheet is not None:
            path = os.path.join(args.png, "_sheet.png")
            sheet.save(path)
            print(f"contact sheet: {path}  ({sheet.size[0]}x{sheet.size[1]})")
        print(f"guides at spine column {ox}, feet row {oy}")
        print("  red horizontal = feet baseline, every mascot should stand on it")
        print("  blue vertical  = spine, the torso should straddle it evenly")

    if args.write:
        os.makedirs(args.write, exist_ok=True)
        total = 0
        for name, frame in frames.items():
            if frame["aligned"] is None:
                continue
            blob = encode(frame["aligned"])
            path = os.path.join(args.write, name + ".spr")
            with open(path, "wb") as handle:
                handle.write(blob)
            total += len(blob)
            print(f"{os.path.basename(path):28} {len(blob):6} B")
        print(f"\n{total} bytes total")


if __name__ == "__main__":
    main()
