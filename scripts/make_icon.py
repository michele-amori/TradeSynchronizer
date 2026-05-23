"""
Generate AppIcon.icns for the macOS .app bundle.

Designs a 1024×1024 icon programmatically (deep-blue rounded-square
background with a vertical gradient + two white "sync" arrows
forming a circle), exports every resolution that macOS expects
into a temporary .iconset directory, then uses /usr/bin/iconutil
to compile the .icns container.

Run from the project root:

    .venv/bin/python scripts/make_icon.py

After generation, re-run ./build_app.sh — the build script
automatically picks up AppIcon.icns when present.

Requires Pillow (`pip install Pillow`). The PIL dep is NOT in
requirements.txt because the icon is regenerated rarely — the
.icns is committed and reused as-is between builds.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Pillow is required. Install with:\n    pip install Pillow")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICONSET_DIR  = PROJECT_ROOT / "AppIcon.iconset"
OUTPUT_ICNS  = PROJECT_ROOT / "AppIcon.icns"


# ─── Design parameters ────────────────────────────────────────────── #

BG_TOP        = (37, 99, 235)      # bright blue (Tailwind blue-600)
BG_BOTTOM     = (24, 60, 150)      # deeper blue
ARROW_COLOR   = (255, 255, 255, 255)
CORNER_RATIO  = 0.22               # rounded-square corner, % of CONTENT side
RING_RADIUS   = 0.30               # arc radius, % of CONTENT side
RING_THICK    = 0.10               # arc thickness, % of CONTENT side

# macOS Icon Grid: the visible content (rounded square + arrows) must
# occupy ~80% of the canvas, leaving roughly 10% padding on each side,
# or the icon will appear visibly larger than every other Dock icon.
INSET_RATIO   = 0.10

# Each arrow covers 140° with a 20° gap on each side, drawn clockwise.
TOP_ARC    = (200, 340)            # passes through 270° (visual top)
BOTTOM_ARC = (20, 160)             # passes through 90°  (visual bottom)


def _make_gradient(size: int) -> Image.Image:
    """Vertical linear gradient from BG_TOP to BG_BOTTOM."""
    grad = Image.new("RGB", (size, size), BG_TOP)
    px = grad.load()
    for y in range(size):
        t = y / max(1, size - 1)
        r = round(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = round(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = round(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return grad


def _rounded_mask(size: int, radius: int) -> Image.Image:
    """Anti-aliased rounded-square mask. Drawn at 4× then downscaled
    so the corner radius doesn't show stair-stepping at small sizes."""
    scale = 4
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, size * scale, size * scale),
        radius=radius * scale,
        fill=255,
    )
    return big.resize((size, size), Image.LANCZOS)


def _draw_arrow_head(
    draw: ImageDraw.ImageDraw,
    cx: float, cy: float, R: float,
    angle_deg: float,
    thickness: float,
):
    """
    Triangle arrowhead at the end of an arc that swept clockwise to
    `angle_deg`. The tip extends along the clockwise tangent; the
    base spans the arc's thickness radially.
    """
    angle = math.radians(angle_deg)

    # Point on the arc (centre-line of the stroke)
    px = cx + R * math.cos(angle)
    py = cy + R * math.sin(angle)

    # Clockwise tangent unit vector at that point
    tan_x = -math.sin(angle)
    tan_y =  math.cos(angle)

    # Radial unit vector (outward)
    rad_x = math.cos(angle)
    rad_y = math.sin(angle)

    # Tip extends forward; base width = thickness (so head matches stroke)
    tip_len = thickness * 1.1
    half_w  = thickness * 1.0
    tip = (px + tan_x * tip_len, py + tan_y * tip_len)
    outer = (px + rad_x * half_w, py + rad_y * half_w)
    inner = (px - rad_x * half_w, py - rad_y * half_w)
    draw.polygon([tip, outer, inner], fill=ARROW_COLOR)


def _render_content(size: int) -> Image.Image:
    """Draw the icon's actual content (rounded square + arrows) into
    a `size`×`size` image, with the rounded square filling that
    entire space — no outer padding. The outer padding is added by
    render()."""
    grad = _make_gradient(size)
    mask = _rounded_mask(size, radius=int(size * CORNER_RATIO))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    R = size * RING_RADIUS
    thick = size * RING_THICK
    bbox = (cx - R, cy - R, cx + R, cy + R)

    draw.arc(bbox, start=TOP_ARC[0], end=TOP_ARC[1],
             fill=ARROW_COLOR, width=int(round(thick)))
    _draw_arrow_head(draw, cx, cy, R, TOP_ARC[1], thick)

    draw.arc(bbox, start=BOTTOM_ARC[0], end=BOTTOM_ARC[1],
             fill=ARROW_COLOR, width=int(round(thick)))
    _draw_arrow_head(draw, cx, cy, R, BOTTOM_ARC[1], thick)

    return img


def render(size: int) -> Image.Image:
    """
    Render the icon at the given square size with macOS-style outer
    padding (~10% on each side), so the content occupies ~80% of the
    canvas and matches the Dock's visual sizing of system icons.
    """
    inset_px = int(round(size * INSET_RATIO))
    content_size = size - 2 * inset_px
    if content_size <= 0:
        return _render_content(size)   # safety net for very small renders
    content = _render_content(content_size)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(content, (inset_px, inset_px), content)
    return canvas


# Standard macOS .iconset entries: (pixel size, file name)
# Apple expects exactly these 10 PNGs for a complete icon set.
_ICONSET_ENTRIES = [
    (16,    "icon_16x16.png"),
    (32,    "icon_16x16@2x.png"),
    (32,    "icon_32x32.png"),
    (64,    "icon_32x32@2x.png"),
    (128,   "icon_128x128.png"),
    (256,   "icon_128x128@2x.png"),
    (256,   "icon_256x256.png"),
    (512,   "icon_256x256@2x.png"),
    (512,   "icon_512x512.png"),
    (1024,  "icon_512x512@2x.png"),
]


def main() -> None:
    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir()

    # Render the largest version once, then downsample for every other
    # size — Lanczos resampling beats re-rasterising vector primitives
    # at small sizes because PIL's arc anti-aliasing is mediocre.
    print(f"Rendering 1024×1024 master…")
    master = render(1024)

    for size, name in _ICONSET_ENTRIES:
        img = master if size == 1024 else master.resize(
            (size, size), Image.LANCZOS)
        out = ICONSET_DIR / name
        img.save(out)
        print(f"  ✓ {name:<26} ({size}×{size}, {out.stat().st_size:>6} B)")

    print(f"\nBundling into {OUTPUT_ICNS.name} via iconutil…")
    res = subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET_DIR),
         "-o", str(OUTPUT_ICNS)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        sys.exit("iconutil failed.")

    shutil.rmtree(ICONSET_DIR)
    print(f"\n✓ {OUTPUT_ICNS} "
          f"({OUTPUT_ICNS.stat().st_size:,} bytes)")
    print("\nNext step: ./build_app.sh   (picks up AppIcon.icns automatically)")


if __name__ == "__main__":
    main()
