#!/usr/bin/env python3
"""Render every OhmzAI brand asset OpenWebUI serves out of /app/build/static.

The mark is the Greek capital omega from Space Grotesk — the brand text face —
so the logo and the UI are drawn with the same pen. Space Grotesk is variable
(wght 300..700); we instantiate it at 500, the weight the brand sheet uses.

    python3 branding/build_assets.py

Writes branding/assets/. Re-runnable; output is deterministic.
"""

from __future__ import annotations

import io
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools.pens.svgPathPen import SVGPathPen
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
CACHE = ROOT / ".cache"

FONT_URL = "https://github.com/google/fonts/raw/main/ofl/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf"
WEIGHT = 500

# Brand sheet — "OhmzAI Brand.dc.html", panel 1a.
AMBER = "#e0913f"
ON_AMBER = "#241f18"
CANVAS = "#1a1917"

# The glyph occupies this fraction of the tile's height. The brand sheet sets
# 38px type on a 64px tile; Space Grotesk's cap height is ~0.70em, so the ink
# lands at ~0.42 of the tile. Matching that keeps the mark from going chunky.
GLYPH_RATIO = 0.44
# Maskable icons get masked to a circle by the OS, so the mark has to sit
# inside the safe zone (the centre 80%) — draw it full-bleed and no larger.
GLYPH_RATIO_MASKABLE = 0.40
# Corner radius as a fraction of tile size. Small tiles read as rounder, which
# is why the brand sheet uses 16/64 at 64px but 5/16 at 16px.
RADIUS_RATIO = 0.25

SS = 8  # supersampling factor for the rasteriser


def _font_path() -> Path:
    CACHE.mkdir(exist_ok=True)
    ttf = CACHE / "SpaceGrotesk[wght].ttf"
    if not ttf.exists():
        print(f"fetching {FONT_URL}")
        with urllib.request.urlopen(FONT_URL, timeout=60) as r:  # noqa: S310 - pinned google/fonts URL
            ttf.write_bytes(r.read())
    return ttf


def _static_font() -> tuple[TTFont, bytes]:
    """Space Grotesk pinned to wght=500, as both a TTFont and raw bytes."""
    font = instancer.instantiateVariableFont(TTFont(_font_path()), {"wght": WEIGHT})
    buf = io.BytesIO()
    font.save(buf)
    return font, buf.getvalue()


def _omega_svg_path(font: TTFont) -> tuple[str, tuple[float, float, float, float], int]:
    """The omega outline as an SVG path, plus its bbox and the font's upem."""
    glyph_name = font.getBestCmap()[0x03A9]
    glyphs = font.getGlyphSet()
    pen = SVGPathPen(glyphs)
    glyphs[glyph_name].draw(pen)
    xmin, ymin, xmax, ymax = font["glyf"][glyph_name].xMin, font["glyf"][glyph_name].yMin, \
        font["glyf"][glyph_name].xMax, font["glyf"][glyph_name].yMax
    return pen.getCommands(), (xmin, ymin, xmax, ymax), font["head"].unitsPerEm


def render_svg(font: TTFont, size: int = 64) -> str:
    """Vector favicon — the omega as a real outline, no text element."""
    path, (xmin, ymin, xmax, ymax), _upem = _omega_svg_path(font)
    gw, gh = xmax - xmin, ymax - ymin
    scale = (size * GLYPH_RATIO) / gh
    # Flip y (font space is y-up, SVG is y-down) and centre on the glyph bbox.
    tx = (size - gw * scale) / 2 - xmin * scale
    ty = (size + gh * scale) / 2 + ymin * scale
    r = round(size * RADIUS_RATIO, 2)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size}" height="{size}" role="img" aria-label="OhmzAI">\n'
        f'  <rect width="{size}" height="{size}" rx="{r}" ry="{r}" fill="{AMBER}"/>\n'
        f'  <g transform="translate({tx:.3f} {ty:.3f}) scale({scale:.6f} {-scale:.6f})">\n'
        f'    <path fill="{ON_AMBER}" d="{path}"/>\n'
        f'  </g>\n'
        f'</svg>\n'
    )


def render_tile(
    size: int,
    font_bytes: bytes,
    *,
    bg: str = AMBER,
    fg: str = ON_AMBER,
    glyph_ratio: float = GLYPH_RATIO,
    radius_ratio: float = RADIUS_RATIO,
) -> Image.Image:
    """The omega mark on a rounded tile, supersampled then downsampled."""
    n = size * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, n - 1, n - 1], radius=n * radius_ratio, fill=bg)

    # Size the glyph by its own ink box, not the font's line metrics, so the
    # omega is optically centred rather than baseline-placed.
    probe = ImageFont.truetype(io.BytesIO(font_bytes), 100)
    l, t, r, b = probe.getbbox("Ω")
    px = (n * glyph_ratio) * 100 / (b - t)
    face = ImageFont.truetype(io.BytesIO(font_bytes), round(px))
    l, t, r, b = face.getbbox("Ω")
    draw.text(((n - (r + l)) / 2, (n - (b + t)) / 2), "Ω", font=face, fill=fg)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    font, font_bytes = _static_font()

    (ASSETS / "favicon.svg").write_text(render_svg(font))
    print("favicon.svg")

    # Rounded amber tile. favicon.png doubles as the in-app logo, and
    # favicon-dark.png is what OWUI swaps in under html.dark — the brand sheet
    # keeps the amber tile on dark surfaces, so both are the same mark.
    tiles = {
        "favicon.png": 512,
        "favicon-dark.png": 512,
        "favicon-96x96.png": 96,
        "apple-touch-icon.png": 180,
        "logo.png": 512,
        "splash.png": 512,
        "splash-dark.png": 512,
    }
    for name, size in tiles.items():
        render_tile(size, font_bytes).save(ASSETS / name)
        print(f"{name} {size}x{size}")

    # Maskable: full-bleed, mark inside the safe zone.
    for name, size in (("web-app-manifest-192x192.png", 192), ("web-app-manifest-512x512.png", 512)):
        render_tile(
            size, font_bytes, glyph_ratio=GLYPH_RATIO_MASKABLE, radius_ratio=0.0
        ).save(ASSETS / name)
        print(f"{name} {size}x{size} (maskable)")

    # Multi-resolution .ico, each size rendered at its own radius so the 16px
    # entry keeps its corners.
    base = render_tile(256, font_bytes)
    base.save(
        ASSETS / "favicon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("favicon.ico")

    (ASSETS / "site.webmanifest").write_text(
        '{\n'
        '  "name": "OhmzAI",\n'
        '  "short_name": "OhmzAI",\n'
        '  "icons": [\n'
        '    {\n'
        '      "src": "/static/web-app-manifest-192x192.png",\n'
        '      "sizes": "192x192",\n'
        '      "type": "image/png",\n'
        '      "purpose": "maskable"\n'
        '    },\n'
        '    {\n'
        '      "src": "/static/web-app-manifest-512x512.png",\n'
        '      "sizes": "512x512",\n'
        '      "type": "image/png",\n'
        '      "purpose": "maskable"\n'
        '    }\n'
        '  ],\n'
        f'  "theme_color": "{CANVAS}",\n'
        f'  "background_color": "{CANVAS}",\n'
        '  "display": "standalone"\n'
        '}\n'
    )
    print("site.webmanifest")


if __name__ == "__main__":
    main()
