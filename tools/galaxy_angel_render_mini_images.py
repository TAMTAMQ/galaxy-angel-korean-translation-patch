#!/usr/bin/env python3
"""Redraw the MINI.DAT minigame images in the lettering the originals use.

The first localization pass replaced this artwork with flat type: the bubble letters of
``しゅーりょー`` became plain orange text with no outline, the vertical ``だんご`` on a sweet-shop
card became a horizontal caption drawn over the dango, and several speech bubbles lost the
bubble.  The wording was fine; only the drawing was wrong.

This tool draws each label the way the source draws it — same ink and outline colour, same
outline weight, same box, same reading direction — and never paints over the picture beside it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import eternal_lovers_render_candidate_images as base

FONT_BOLD = Path("C:/Windows/Fonts/malgunbd.ttf")


def ink_colours(rgb: np.ndarray, mask: np.ndarray) -> tuple[tuple, tuple | None]:
    """The label's own fill colour, and the outline colour when it has one."""
    ink = rgb[mask].astype(np.float32)
    brightness = ink.mean(axis=1)
    bright = ink[brightness >= np.percentile(brightness, 70)]
    dark = ink[brightness <= np.percentile(brightness, 25)]
    fill = tuple(int(v) for v in np.median(bright if len(bright) else ink, axis=0))
    if len(dark) and float(np.median(bright.mean(axis=1)) - np.median(dark.mean(axis=1))) > 55:
        return fill, tuple(int(v) for v in np.median(dark, axis=0))
    return fill, None


def fit(text: str, width: int, height: int, stroke: int) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    for size in range(height + 6, 5, -1):
        font = ImageFont.truetype(str(FONT_BOLD), size=size)
        box = probe.textbbox((0, 0), text, font=font, stroke_width=stroke)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return font
    return ImageFont.truetype(str(FONT_BOLD), size=6)


def fit_vertical(text: str, width: int, height: int, stroke: int) -> ImageFont.FreeTypeFont:
    """Size a stack of single characters to the column it has to sit in."""
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    for size in range(height + 6, 5, -1):
        font = ImageFont.truetype(str(FONT_BOLD), size=size)
        widest = max(probe.textbbox((0, 0), ch, font=font, stroke_width=stroke)[2] for ch in text)
        total = sum(size + stroke for _ in text)
        if widest <= width and total <= height:
            return font
    return ImageFont.truetype(str(FONT_BOLD), size=6)


def draw_vertical(canvas: Image.Image, box, text: str, fill, stroke: int, outline) -> None:
    # Vertical Japanese has no spaces, and giving one a cell of its own leaves a hole in the
    # column and shrinks every other character to pay for it.
    text = text.replace(" ", "")
    left, top, right, bottom = box
    font = fit_vertical(text, right - left, bottom - top, stroke)
    draw = ImageDraw.Draw(canvas)
    step = (bottom - top) / len(text)
    for index, character in enumerate(text):
        cell = draw.textbbox((0, 0), character, font=font, stroke_width=stroke)
        x = round((left + right) / 2 - (cell[2] - cell[0]) / 2 - cell[0])
        y = round(top + step * (index + 0.5) - (cell[3] - cell[1]) / 2 - cell[1])
        draw.text((x, y), character, font=font, fill=fill, stroke_width=stroke, stroke_fill=outline)


def default_mode(rgb: np.ndarray, alpha: np.ndarray) -> str:
    """Decide how a label is mounted, from how its texture is built.

    A caption that floats over the game's own background has nothing but glyphs in it, so the
    whole texture can be redrawn.  A caption on a button or a card has a plate under it and a
    button icon beside it, and redrawing the texture would throw those away — there only the
    glyphs themselves may be repainted.
    """
    edge = np.concatenate([alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]])
    if float((edge <= 16).mean()) <= 0.75:
        return "panel"
    # A transparent border is not enough: a speech balloon floats on transparency too, and
    # redrawing it would throw the balloon away.  What separates them is that a balloon is
    # mostly one flat fill, while a caption is nothing but strokes and their anti-aliasing.
    visible = alpha > 16
    if not visible.any():
        return "glyph"
    import cv2
    colours, counts = np.unique(rgb[visible].reshape(-1, 3), axis=0, return_counts=True)
    if float(counts.max()) / float(visible.sum()) < 0.5:
        return "glyph"
    # A thick outline around a caption can be half its pixels too, so the share alone is not
    # enough: what makes a balloon a balloon is that its fill is a broad body, and an outline
    # is thin.  Eroding the dominant colour tells them apart.
    flat = np.all(rgb == colours[int(np.argmax(counts))], axis=2) & visible
    core = cv2.erode(flat.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=3).astype(bool)
    return "panel" if float(core.sum()) / float(visible.sum()) >= 0.15 else "glyph"


def repeat_period(values: np.ndarray, known: np.ndarray) -> int | None:
    """How far a repeating background repeats horizontally.

    The blank card's brush stroke sits on a checkerboard, and interpolating across a stroke that
    wide leaves streaks.  A checkerboard repeats, though, so the pixels one period away say
    exactly what belongs under the stroke.  The period is measured over the pixels that are
    still known on both sides of the shift, because the stroke covers most rows outright.
    """
    grey = values.astype(np.float32).mean(axis=2)
    spread = float(grey[known].std()) if known.any() else 0.0
    if spread < 8:
        return None
    best, score = None, None
    for period in range(4, min(49, values.shape[1] // 2)):
        pair = known[:, period:] & known[:, :-period]
        if int(pair.sum()) < 200:
            continue
        error = float(np.abs(grey[:, period:][pair] - grey[:, :-period][pair]).mean())
        if score is None or error < score:
            best, score = period, error
    return best if score is not None and score < spread * 0.35 else None


def fill_by_repeat(rgb: np.ndarray, mask: np.ndarray, known: np.ndarray, period: int) -> np.ndarray:
    plate = rgb.copy()
    height, width = mask.shape
    ys, xs = np.where(mask)
    for y, x in zip(ys, xs):
        for step in (period, -period, 2 * period, -2 * period, 3 * period, -3 * period):
            sx = x + step
            if 0 <= sx < width and known[y, sx]:
                plate[y, x] = rgb[y, sx]
                break
            sy = y + step
            if 0 <= sy < height and known[sy, x]:
                plate[y, x] = rgb[sy, x]
                break
    return plate


def dark_mask(rgb: np.ndarray, alpha: np.ndarray, within: np.ndarray) -> np.ndarray:
    """Find dark lettering on a light card.

    The sweet-shop cards are the other way round from most of this game's UI: the label is a
    heavy black brush stroke on cream, so the label is the *dark* population, not the bright one.
    """
    import cv2
    visible = (alpha > 16) & within
    if not visible.any():
        return np.zeros(alpha.shape, dtype=bool)
    luminance = rgb.astype(np.float32).mean(axis=2)
    values = np.clip(luminance[visible], 0, 255).astype(np.uint8)
    level, _ = cv2.threshold(values, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = visible & (luminance < float(level))
    return cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8), 1).astype(bool) & visible


def imported(source: Path, artwork: Path, entry: dict) -> Image.Image:
    """Bring in hand-drawn artwork saved at a larger size than the texture.

    The hand-drawn files are flattened onto an opaque background, so the shape the texture is
    actually allowed to fill comes from the original: where the original is transparent, so is
    the result.  Where the original is opaque everywhere the drawing was keyed instead, against
    the flat colour the artist left around the lettering.
    """
    original = Image.open(source).convert("RGBA")
    drawn = Image.open(artwork).convert("RGBA").resize(original.size, Image.LANCZOS)
    arr = np.array(original)
    out = np.array(drawn)
    key = entry.get("key_colour")
    if key:
        rgb = out[:, :, :3].astype(int)
        distance = np.abs(rgb - np.array(key)).sum(axis=2)
        out[:, :, 3] = np.clip(distance * 255 // max(1, int(entry.get("key_tolerance", 90))), 0, 255)
    else:
        out[:, :, 3] = arr[:, :, 3]
    return Image.fromarray(out, "RGBA")


def render_each(source: Path, entry: dict, project: Path) -> Image.Image:
    """Replace several labels on one texture, each inside its own box.

    A texture like the throw-direction gauge carries a word in each top corner with the gauge
    itself between them.  Each corner is replaced on its own so the gauge is never touched.
    """
    canvas = None
    for box, text in zip(entry["regions"], entry["labels"]):
        step = dict(entry)
        step.pop("regions"); step.pop("labels")
        step["region"], step["ko"] = box, text
        drawn = render(source, step, project, canvas)
        canvas = drawn
    return canvas


def render(source: Path, entry: dict, project: Path = Path("."),
           canvas_in: Image.Image | None = None) -> Image.Image:
    if entry.get("regions") and entry.get("labels"):
        return render_each(source, entry, project)
    if entry.get("import_from"):
        return imported(source, project / entry["import_from"], entry)
    original = canvas_in if canvas_in is not None else Image.open(source).convert("RGBA")
    arr = np.array(original)
    rgb, alpha = arr[:, :, :3], arr[:, :, 3]
    text = entry["ko"]
    layout = entry.get("layout", "horizontal")
    mode = entry.get("mode") or default_mode(rgb, alpha)

    # The label's own pixels: on these textures the text either owns the whole visible area or
    # sits inside a box the entry names.
    box = entry.get("region")
    height, width = alpha.shape
    area = np.zeros(alpha.shape, dtype=bool)
    if box:
        area[int(height*box[1]):int(height*box[3]), int(width*box[0]):int(width*box[2])] = True
    else:
        area[:] = True

    if mode == "cover":
        # The blank card's brush stroke is one heavy character inside a thick white halo, drawn
        # straight onto a checkerboard.  Nothing can repaint that checkerboard convincingly, and
        # nothing has to: the Korean is one character too, so drawn at the same size in the same
        # halo it covers the Japanese outright.
        mask = dark_mask(rgb, alpha, area)
        rows, columns = np.where(mask)
        ink_box = (int(columns.min()), int(rows.min()), int(columns.max()) + 1, int(rows.max()) + 1)
        # The stroke and its halo come out of the threshold together, because the halo is what
        # separates the stroke from the checkerboard.  So both colours are read from inside it:
        # the dark end is the brush, the light end is the halo.
        light = rgb[mask].astype(np.float32)
        halo_colour = tuple(int(v) for v in np.median(
            light[light.mean(axis=1) >= np.percentile(light.mean(axis=1), 90)], axis=0))
        ink = rgb[mask].astype(np.float32)
        colour = tuple(int(v) for v in np.median(
            ink[ink.mean(axis=1) <= np.percentile(ink.mean(axis=1), 40)], axis=0))
        canvas = original.copy()
        pad = int(entry.get("stroke", 4))
        shape_rows, shape_columns = np.where(alpha > 16)
        box = (max(ink_box[0] - pad, int(shape_columns.min())),
               max(ink_box[1] - pad, int(shape_rows.min())),
               min(ink_box[2] + pad, int(shape_columns.max()) + 1),
               min(ink_box[3] + pad, int(shape_rows.max()) + 1))
        draw = ImageDraw.Draw(canvas)
        font = fit(text, box[2] - box[0], box[3] - box[1], pad)
        cell = draw.textbbox((0, 0), text, font=font, stroke_width=pad)
        x = round((box[0] + box[2]) / 2 - (cell[2] - cell[0]) / 2 - cell[0])
        y = round((box[1] + box[3]) / 2 - (cell[3] - cell[1]) / 2 - cell[1])
        draw.text((x, y), text, font=font, fill=colour + (255,),
                  stroke_width=pad, stroke_fill=halo_colour + (255,))
        return canvas

    if mode == "glyph":
        mask = (alpha > 16) & area
    elif mode == "dark":
        mask = dark_mask(rgb, alpha, area)
    else:
        mask = base.panel_mask(rgb, alpha, entry.get("metric", "whiteness"), area)
    if not mask.any():
        raise ValueError(f"{source.name}: nothing to replace inside the named box")

    fill, outline = ink_colours(rgb, mask)
    if mode == "dark":
        # On a card the brush stroke is the dark ink and its edge is the light card, so the two
        # roles are the other way round from a glowing caption.
        ink = rgb[mask].astype(np.float32)
        brightness = ink.mean(axis=1)
        fill = tuple(int(v) for v in np.median(ink[brightness <= np.percentile(brightness, 40)], axis=0))
        outline = None
    stroke = int(entry.get("stroke", 2 if outline else 0))

    rows, columns = np.where(mask)
    ink_box = (int(columns.min()), int(rows.min()), int(columns.max()) + 1, int(rows.max()) + 1)

    if mode == "glyph":
        # Everything the label owns is redrawn from scratch; the picture beside it — the dango,
        # the button icon — is outside the box and carried over untouched.
        out = arr.copy()
        out[mask] = 0
        canvas = Image.fromarray(out, "RGBA")
    else:
        # What runs behind the label: a card column or a button plate is one flat colour, and
        # painting the label out with it is exact.  Interpolating across the label instead would
        # drag in whatever sits past the box — the picture on a sweet-shop card, the button icon
        # on a call button.  A patterned plate has no such colour and does get interpolated.
        import cv2
        # Sample what runs behind the label from the pixels that touch it, not from the whole
        # box: a button's plate is only flat right around the letters, and its rim and the
        # shadow under it are a different colour entirely.
        ring = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=3).astype(bool)
        behind = ring & (alpha > 16) & area & ~mask
        if not behind.any():
            behind = (alpha > 16) & area & ~mask
        if not behind.any():
            behind = (alpha > 16) & ~mask
        colours, counts = np.unique(rgb[behind].reshape(-1, 3), axis=0, return_counts=True)
        plate_alpha = alpha.copy()
        if float(counts.max()) / float(behind.sum()) >= 0.5:
            plate = rgb.copy()
            plate[mask] = colours[int(np.argmax(counts))]
            # The label is in the alpha as well as the colour: on a half-transparent plate the
            # glyphs are the opaque part, and leaving that behind prints the Japanese as a
            # solid silhouette over the plate.  So the plate's own alpha goes back too.
            levels, seen = np.unique(alpha[behind], return_counts=True)
            plate_alpha[mask] = levels[int(np.argmax(seen))]
        else:
            known = (alpha > 16) & ~mask
            period = repeat_period(rgb, known)
            plate = (fill_by_repeat(rgb, mask, known, period) if period
                     else base.erase_label(rgb, mask, alpha > 16))
        canvas = Image.fromarray(np.dstack([plate, plate_alpha]), "RGBA")

    stroke_fill = (outline + (255,)) if outline else None
    if layout == "vertical":
        draw_vertical(canvas, ink_box, text, fill + (255,), stroke, stroke_fill)
    else:
        draw = ImageDraw.Draw(canvas)
        font = fit(text, ink_box[2] - ink_box[0], ink_box[3] - ink_box[1], stroke)
        cell = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        x = round((ink_box[0] + ink_box[2]) / 2 - (cell[2] - cell[0]) / 2 - cell[0])
        y = round((ink_box[1] + ink_box[3]) / 2 - (cell[3] - cell[1]) / 2 - cell[1])
        draw.text((x, y), text, font=font, fill=fill + (255,), stroke_width=stroke, stroke_fill=stroke_fill)
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path("work/galaxy_angel"))
    parser.add_argument("--decisions", type=Path,
                        default=Path("work/galaxy_angel/assets/translation/mini_image_redraw.json"))
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--only", action="append", default=None)
    args = parser.parse_args()

    root = args.project / "assets/image_extraction/MINI"
    entries = json.loads(args.decisions.read_text(encoding="utf-8"))["entries"]
    done, failures = 0, []
    for name, entry in sorted(entries.items()):
        if entry.get("skip") or (args.only and name not in args.only):
            continue
        if entry.get("keep_current"):
            # Copying rather than skipping keeps the folder a complete set, so a later build
            # driven from it does not silently fall back to the Japanese.
            target = (args.preview or (root / "japanese_images/translated_png")) / Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.open(root / "translated_png" / name).convert("RGBA").save(target)
            done += 1
            continue
        try:
            image = render(root / "png" / name, entry, args.project)
        except Exception as error:  # noqa: BLE001 - reported per image, nothing written for it
            failures.append({"png": name, "error": str(error)})
            continue
        target = (args.preview or (root / "japanese_images/translated_png")) / Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target)
        done += 1
    print(json.dumps({"rendered": done, "failures": failures}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
