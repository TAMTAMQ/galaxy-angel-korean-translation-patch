"""Directly localize MINI images missed by the prior pass.

This script is deliberately deterministic and uses no OCR, VLM, LLM, or
other local AI.  The Japanese strings and Korean translations below were
verified manually from the extracted PNGs.

Source PNGs remain read-only.  Outputs are written only under
assets/image_extraction/MINI/translated_png.
"""

from __future__ import annotations

from pathlib import Path
import shutil

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
MINI = ROOT / "assets" / "image_extraction" / "MINI"
SRC = MINI / "png"
OUT = MINI / "translated_png"

FONT_REGULAR = Path.home() / "AppData/Local/Microsoft/Windows/Fonts/Pretendard-Regular.otf"
FONT_BOLD = Path.home() / "AppData/Local/Microsoft/Windows/Fonts/Pretendard-Bold.otf"
FONT_BLACK = Path.home() / "AppData/Local/Microsoft/Windows/Fonts/Pretendard-Black.otf"


def out_path(rel: str) -> Path:
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def fit_font(text: str, max_w: int, max_h: int, start: int, font_path: Path, stroke: int = 0) -> ImageFont.FreeTypeFont:
    for size in range(start, 5, -1):
        font = ImageFont.truetype(str(font_path), size)
        box = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=font, stroke_width=stroke)
        if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
            return font
    return ImageFont.truetype(str(font_path), 6)


def render_centered_transparent(size: tuple[int, int], text: str, font_path: Path, start: int,
                                fill=(255, 255, 255, 255), stroke_fill=(40, 40, 40, 255),
                                stroke_width: int = 1, pad_x: int = 2, pad_y: int = 1) -> Image.Image:
    w, h = size
    scale = 4
    canvas = Image.new("RGBA", (w * scale, h * scale), (255, 255, 255, 0))
    draw = ImageDraw.Draw(canvas)
    font = fit_font(text, (w - pad_x * 2) * scale, (h - pad_y * 2) * scale,
                    start * scale, font_path, stroke_width * scale)
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width * scale)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = (w * scale - tw) // 2 - box[0]
    y = (h * scale - th) // 2 - box[1]
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width * scale,
              stroke_fill=stroke_fill)
    return canvas.resize((w, h), Image.Resampling.LANCZOS)


def save_rules_panel() -> None:
    rel = "mini/mini01/m2d/transfer_017_256x152_psm14.png"
    im = Image.open(SRC / rel).convert("RGBA")
    px = im.load()

    # The source word ルール occupies the upper-right 74x28 area.  Restore its
    # horizontally-striped panel background from the same scanline immediately
    # to the left, then draw Korean 규칙 in the same white heading style.
    x0, y0, x1, y1 = 172, 2, 247, 29
    for y in range(y0, y1):
        samples = [px[x, y] for x in range(100, 160) if px[x, y][3] > 0]
        if samples:
            # The panel background is horizontally uniform; median-ish center
            # sample preserves alternating scanline brightness.
            bg = samples[len(samples) // 2]
        else:
            bg = (0, 0, 0, 255)
        for x in range(x0, x1):
            px[x, y] = bg

    draw = ImageDraw.Draw(im)
    text = "규칙"
    font = fit_font(text, 66, 23, 20, FONT_BOLD, 0)
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = 240 - tw
    y = 4 - box[1]
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    im.save(out_path(rel))


def save_dealer_ranpha() -> None:
    rel = "mini/mini02/mg02spr/transfer_000_160x36_psm14.png"
    # Existing localized dealer labels use '선 <이름>'.  Match that treatment.
    im = render_centered_transparent((160, 36), "선 란파", FONT_BOLD, 28,
                                     fill=(245, 245, 245, 255),
                                     stroke_fill=(62, 62, 62, 255), stroke_width=2,
                                     pad_x=2, pad_y=2)
    im.save(out_path(rel))


def save_counter() -> None:
    rels = [
        "mini/mini02/mg02spt/transfer_017_16x24_psm14.png",
        "mini/mini03/mg03nttl/transfer_015_16x24_psm14.png",
        "mini/mini05/m05title/transfer_015_16x24_psm14.png",
    ]
    scale = 4
    im4 = Image.new("RGBA", (16 * scale, 24 * scale), (255, 255, 255, 0))
    d4 = ImageDraw.Draw(im4)
    font = ImageFont.truetype(str(FONT_BOLD), 12 * scale)
    d4.text((9 * scale, 12 * scale), "회", font=font, anchor="mm", fill=(255, 255, 255, 255))
    im = im4.resize((16, 24), Image.Resampling.LANCZOS)
    for rel in rels:
        im.save(out_path(rel))


def erase_title_text_region(im: Image.Image) -> Image.Image:
    """Remove the white/green Japanese title glyphs without touching lower UI."""
    im = im.copy().convert("RGBA")
    # Title text lives only in y=28..103.  Its fill is pure white.  Use that
    # as the seed and dilate far enough to consume the thick green outline and
    # antialiasing.  This is safer than classifying green pixels directly,
    # because the character line art is also green.
    roi = im.crop((0, 24, im.width, 142))
    data = roi.load()
    mask = Image.new("L", roi.size, 0)
    mp = mask.load()
    for y in range(roi.height):
        for x in range(roi.width):
            r, g, b, a = data[x, y]
            if a and r >= 248 and g >= 248 and b >= 248:
                mp[x, y] = 255
    mask = mask.filter(ImageFilter.MaxFilter(17)).filter(ImageFilter.GaussianBlur(0.6))
    mp = mask.load()

    # Reconstruct the green horizontal background row-by-row from clean pixels
    # near the right side of each segment.  This intentionally affects only the
    # title mask, leaving the character art outside glyph footprints untouched.
    clean = roi.copy()
    cp = clean.load()
    for y in range(roi.height):
        candidates = []
        for x in range(20, roi.width - 20):
            if mp[x, y] < 32:
                r, g, b, a = data[x, y]
                if a and g > 100 and b > 80:
                    candidates.append((r, g, b, a))
        bg = candidates[len(candidates) // 2] if candidates else (55, 211, 176, 255)
        for x in range(roi.width):
            if mp[x, y] > 0:
                cp[x, y] = bg
    im.paste(clean, (0, 24))
    return im


def save_vanilla_title() -> None:
    left_rel = "mini/mini04/title/transfer_018_256x480_psm13.png"
    mid_rel = "mini/mini04/title/transfer_019_256x480_psm13.png"
    right_rel = "mini/mini04/title/transfer_020_128x480_psm13.png"
    left = Image.open(SRC / left_rel).convert("RGBA")
    mid = Image.open(SRC / mid_rel).convert("RGBA")
    right = Image.open(SRC / right_rel).convert("RGBA")

    full = Image.new("RGBA", (640, 480))
    full.paste(left, (0, 0))
    full.paste(mid, (256, 0))
    full.paste(right, (512, 0))
    full = erase_title_text_region(full)

    draw = ImageDraw.Draw(full)
    outline = (0, 92, 76, 255)
    white = (255, 255, 255, 255)

    small = "바닐라의"
    small_font = fit_font(small, 110, 22, 18, FONT_BOLD, 2)
    draw.text((86, 31), small, font=small_font, fill=white, stroke_width=2, stroke_fill=outline)

    big = "기다려! 우기우기☆"
    big_font = fit_font(big, 410, 47, 39, FONT_BLACK, 3)
    box = draw.textbbox((0, 0), big, font=big_font, stroke_width=3)
    tw = box[2] - box[0]
    draw.text((72, 51), big, font=big_font, fill=white, stroke_width=3, stroke_fill=outline)

    full.crop((0, 0, 256, 480)).save(out_path(left_rel))
    full.crop((256, 0, 512, 480)).save(out_path(mid_rel))


def save_recipe() -> None:
    rel = "mini/mini00/resipi.png"
    proven = ROOT / "build" / "resipi_current_iso.png"
    if not proven.exists():
        raise FileNotFoundError(proven)
    shutil.copy2(proven, out_path(rel))


def main() -> None:
    for font in (FONT_REGULAR, FONT_BOLD, FONT_BLACK):
        if not font.exists():
            raise FileNotFoundError(font)
    save_recipe()
    save_rules_panel()
    save_dealer_ranpha()
    save_counter()
    save_vanilla_title()
    print("wrote direct MINI translations:")
    print("  mini00/resipi.png                         레시피")
    print("  mini01/m2d/transfer_017...                규칙")
    print("  mini02/mg02spr/transfer_000...            선 란파")
    print("  counter 回 x3                            회")
    print("  mini04/title/transfer_018,019...          바닐라의 기다려! 우기우기☆")


if __name__ == "__main__":
    main()
