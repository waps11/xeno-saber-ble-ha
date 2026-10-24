#!/usr/bin/env python3
"""Build the brand images Home Assistant shows for this integration.

Writes the SVG sources to ``assets/`` and the PNGs to
``custom_components/xeno_saber_ble/brand/`` (read by Home Assistant 2026.3+):

    icon.png / icon@2x.png            256 / 512 px square, for light backgrounds
    dark_icon.png / dark_icon@2x.png  same, for dark backgrounds
    logo.png / logo@2x.png            icon + wordmark, 256 / 512 px tall
    dark_logo.png / dark_logo@2x.png  same, for dark backgrounds

Images are trimmed to their content on a transparent background, as the
Home Assistant brand guidelines ask.

    pip install cairosvg pillow pyoxipng
    python tools/make_brand.py

The wordmark uses the Inter Display Bold font (SIL Open Font License); pass
``--font PATH`` if it is installed elsewhere.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path
import sys

import cairosvg
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from brand_svg import BLADE, BLADE_DEEP, icon_svg  # noqa: E402

BRAND_DIR = ROOT / "custom_components" / "xeno_saber_ble" / "brand"
ASSETS_DIR = ROOT / "assets"
DEFAULT_FONT = "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf"
RENDER = 2048  # internal render size before downscaling


def _trimmed(svg: str) -> Image.Image:
    """Render at high resolution and crop to the visible subject."""
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=RENDER, output_height=RENDER)
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # Ignore the faintest edge of the glow so the subject fills the frame.
    mask = img.getchannel("A").point(lambda a: 255 if a > 8 else 0)
    return img.crop(mask.getbbox())


def _square(img: Image.Image) -> Image.Image:
    side = max(img.size)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.alpha_composite(img, ((side - img.width) // 2, (side - img.height) // 2))
    return out


def _logo(icon: Image.Image, dark: bool, height: int, font_path: str) -> Image.Image:
    """Icon on the left, "Xeno Saber BLE" on the right, trimmed."""
    ink = (240, 243, 246, 255) if dark else (31, 35, 40, 255)
    accent = BLADE if dark else BLADE_DEEP
    accent_rgba = tuple(int(accent[i : i + 2], 16) for i in (1, 3, 5)) + (255,)

    scale = 4  # draw large, then downscale for clean edges
    h = height * scale
    icon_img = icon.resize((h, h), Image.LANCZOS)
    font = ImageFont.truetype(font_path, int(h * 0.40))
    gap = int(h * 0.16)
    space = int(h * 0.12)
    words = [("Xeno Saber", ink), ("BLE", accent_rgba)]
    widths = [font.getbbox(w)[2] - font.getbbox(w)[0] for w, _ in words]
    width = h + gap + sum(widths) + space + int(h * 0.05)

    canvas = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    canvas.alpha_composite(icon_img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    x = h + gap
    for (word, colour), w in zip(words, widths, strict=True):
        bbox = font.getbbox(word)
        draw.text((x - bbox[0], h // 2), word, font=font, fill=colour, anchor="lm")
        x += w + space
    canvas = canvas.crop(canvas.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox())
    new_w = round(canvas.width * height / canvas.height)
    return canvas.resize((new_w, height), Image.LANCZOS)


def _save(img: Image.Image, path: Path) -> None:
    img.save(path, optimize=True)
    try:
        import oxipng

        oxipng.optimize(path, level=4, interlace=oxipng.Interlacing.Adam7)
    except ImportError:  # still a valid PNG, just larger
        pass
    print(f"{path.relative_to(ROOT)}  {img.width}x{img.height}  {path.stat().st_size // 1024} KB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--font", default=DEFAULT_FONT)
    args = parser.parse_args()

    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for dark in (False, True):
        svg = icon_svg(dark)
        (ASSETS_DIR / ("icon_dark.svg" if dark else "icon.svg")).write_text(svg)
        icon = _square(_trimmed(svg))
        prefix = "dark_" if dark else ""
        for suffix, size in (("", 256), ("@2x", 512)):
            _save(icon.resize((size, size), Image.LANCZOS), BRAND_DIR / f"{prefix}icon{suffix}.png")
            _save(_logo(icon, dark, size, args.font), BRAND_DIR / f"{prefix}logo{suffix}.png")


if __name__ == "__main__":
    main()
