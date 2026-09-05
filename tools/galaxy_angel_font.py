#!/usr/bin/env python3
"""Build and patch Galaxy Angel's embedded 24x24, 4-bpp glyph table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


GLYPH_WIDTH = 24
GLYPH_HEIGHT = 24
GLYPH_BYTES = GLYPH_WIDTH * GLYPH_HEIGHT // 2
FONT_VADDR = 0x0033D710
FONT_FILE_OFFSET = 0x0023E710
ORIGINAL_GLYPHS = 7045
FIRST_REPLACEMENT_INDEX = 5000


def is_script_safe_sjis(code: bytes) -> bool:
    # This engine tokenizes scenario commands byte-by-byte. Any ASCII trail
    # byte can therefore be mistaken for script syntax or an identifier byte.
    # Restrict custom glyphs to Shift-JIS codes whose trail is non-ASCII.
    return not 0x40 <= code[1] <= 0x7E


def render_glyph(char: str, font_path: Path, font_size: int = 20,
                 stroke_width: int = 0, stroke_level: int = 160,
                 font_index: int = 0) -> bytes:
    # Match the proven Ar nosurge patch rasterizer: draw at 4x resolution with
    # the glyph centered by its font metrics, then downsample with LANCZOS.  A
    # 20x20 drawing box inside the game's 24x24 cell leaves the same two-pixel
    # inset used by that patch.  Drawing directly at 20 px made Pretendard's
    # small-size hinting look smeared and horizontally swollen in PCSX2.
    scale = 4
    box_size = min(font_size, GLYPH_WIDTH, GLYPH_HEIGHT)
    high = Image.new("L", (box_size * scale, box_size * scale), 0)
    font = ImageFont.truetype(
        str(font_path), int(box_size * scale * 1.06), index=font_index
    )
    ImageDraw.Draw(high).text(
        (box_size * scale / 2, box_size * scale / 2), char,
        font=font, fill=255, anchor="mm",
        stroke_width=stroke_width * scale, stroke_fill=stroke_level,
    )
    glyph = high.resize((box_size, box_size), Image.Resampling.LANCZOS)
    image = Image.new("L", (GLYPH_WIDTH, GLYPH_HEIGHT), 0)
    image.paste(
        glyph,
        ((GLYPH_WIDTH - box_size) // 2, (GLYPH_HEIGHT - box_size) // 2),
    )
    pixels = list(image.getdata())
    quantized = [(value * 15 + 127) // 255 for value in pixels]
    # The embedded table stores the left pixel in the low nibble and the right
    # pixel in the high nibble.  Reversing this order swaps every adjacent pair
    # of pixels and makes strokes look horizontally scattered in game.
    return bytes(quantized[i] | (quantized[i + 1] << 4)
                 for i in range(0, len(quantized), 2))


def index_to_sjis(index: int) -> bytes:
    if index < 690:
        raw_index = index
    elif index < 3655:
        raw_index = index + 720
    elif index < ORIGINAL_GLYPHS:
        raw_index = index + 763
    else:
        raise ValueError(f"compact glyph index {index} is outside the embedded table")
    row, column = divmod(raw_index, 188)
    if row < 31:
        lead = 0x81 + row
    else:
        lead = 0xE0 + row - 31
    if not 0x81 <= lead <= 0xFC:
        raise ValueError(f"glyph index {index} is outside the renderer's Shift-JIS space")
    trail = 0x40 + column if column < 63 else 0x41 + column
    return bytes((lead, trail))


def collect_custom_chars(asset_dir: Path, extra_translations: Path | None = None) -> list[str]:
    index = json.loads((asset_dir / "index.json").read_text(encoding="utf-8"))
    chars: set[str] = set()
    for item in index["segments"]:
        segment = json.loads((asset_dir / item["path"]).read_text(encoding="utf-8"))
        for unit in segment["units"]:
            if not unit["use_translation"]:
                continue
            for char in unit["translation"]:
                try:
                    char.encode("cp932")
                except UnicodeEncodeError:
                    chars.add(char)
    speaker_path = asset_dir / "speaker_names.json"
    if speaker_path.exists():
        speakers = json.loads(speaker_path.read_text(encoding="utf-8"))
        for name in speakers["names"].values():
            for char in name:
                try:
                    char.encode("cp932")
                except UnicodeEncodeError:
                    chars.add(char)
    selection_path = asset_dir / "selection_translations.json"
    if selection_path.exists():
        selections = json.loads(selection_path.read_text(encoding="utf-8"))
        for value in selections["translations"].values():
            for char in value:
                try:
                    char.encode("cp932")
                except UnicodeEncodeError:
                    chars.add(char)
    # Battle and remaining compressed-text worklists are separate from the
    # main scenario index, but use the same embedded renderer at runtime.
    for extra_path in (
        asset_dir / "battle" / "battle_unique.json",
        asset_dir / "remaining" / "remaining_compressed_unique.json",
    ):
        if not extra_path.exists():
            continue
        payload = json.loads(extra_path.read_text(encoding="utf-8"))
        for unit in payload.get("units", []):
            if not unit.get("use_translation") or not unit.get("translation"):
                continue
            for char in unit["translation"]:
                try:
                    char.encode("cp932")
                except UnicodeEncodeError:
                    chars.add(char)
    if extra_translations is not None and extra_translations.exists():
        payload = json.loads(extra_translations.read_text(encoding="utf-8"))
        for entry in payload.get("entries", []):
            for char in entry.get("ko", ""):
                try:
                    char.encode("cp932")
                except UnicodeEncodeError:
                    chars.add(char)
    return sorted(chars)


def build_font(asset_dir: Path, input_elf: Path, output_elf: Path, map_output: Path,
               font_path: Path, font_size: int, stroke_width: int,
               stroke_level: int, font_index: int,
               extra_translations: Path | None = None) -> None:
    chars = collect_custom_chars(asset_dir, extra_translations)
    available_indices = [
        index for index in range(ORIGINAL_GLYPHS - 1, FIRST_REPLACEMENT_INDEX - 1, -1)
        if is_script_safe_sjis(index_to_sjis(index))
    ]
    capacity = len(available_indices)
    if len(chars) > capacity:
        raise SystemExit(f"custom glyph capacity exceeded: {len(chars)} > {capacity}")
    data = bytearray(input_elf.read_bytes())
    mapping = {}
    for char, index in zip(chars, available_indices, strict=False):
        offset = FONT_FILE_OFFSET + index * GLYPH_BYTES
        data[offset : offset + GLYPH_BYTES] = render_glyph(
            char, font_path, font_size, stroke_width, stroke_level, font_index
        )
        mapping[char] = {"hex": index_to_sjis(index).hex(), "glyph_index": index}

    # Save-slot rows compose the chapter marker from two fixed ELF strings
    # around the numeric field.  Replace only those format-string occurrences;
    # the large SAVE/LOAD headings and buttons are texture assets.
    chapter_glyphs = {
        "第".encode("cp932"): bytes.fromhex(mapping["제"]["hex"]),
        "章".encode("cp932"): bytes.fromhex(mapping["장"]["hex"]),
    }
    save_ui_patterns = (
        (b"%4d/%2.2d/%2.2d %2.2d:%2.2d " + "第".encode("cp932"), "第", "제"),
        ("章".encode("cp932") + b" %1.24s", "章", "장"),
        (b"----/--/-- --:-- " + "第".encode("cp932"), "第", "제"),
        ("章".encode("cp932") + b" ------------------------", "章", "장"),
    )
    save_ui_patched = 0
    for pattern, old_char, _new_char in save_ui_patterns:
        position = data.find(pattern)
        if position < 0:
            raise SystemExit(f"save chapter format string missing: {pattern!r}")
        char_offset = position + pattern.find(old_char.encode("cp932"))
        data[char_offset:char_offset + 2] = chapter_glyphs[old_char.encode("cp932")]
        save_ui_patched += 1
    output_elf.parent.mkdir(parents=True, exist_ok=True)
    output_elf.write_bytes(data)
    map_output.parent.mkdir(parents=True, exist_ok=True)
    runtime_start_index = min(item["glyph_index"] for item in mapping.values())
    runtime_start = FONT_FILE_OFFSET + runtime_start_index * GLYPH_BYTES
    runtime_blob = map_output.with_name("font_runtime.bin")
    runtime_blob.write_bytes(data[runtime_start:FONT_FILE_OFFSET + ORIGINAL_GLYPHS * GLYPH_BYTES])
    map_output.write_text(json.dumps({
        "schema": "galaxy-angel-font-map/v1",
        "font": str(font_path),
        "font_size": font_size,
        "font_index": font_index,
        "stroke_width": stroke_width,
        "stroke_level": stroke_level,
        "replacement_index_floor": FIRST_REPLACEMENT_INDEX,
        "runtime_blob": str(runtime_blob),
        "runtime_address": FONT_VADDR + runtime_start_index * GLYPH_BYTES,
        "characters": mapping,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"patched {len(chars)} custom glyphs and {save_ui_patched} save UI markers")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--char", default="한")
    probe.add_argument(
        "--font", type=Path,
        default=Path(
            r"vendor\pretendard\packages\pretendard\dist\public\static\alternative"
            r"\Pretendard-Bold.ttf"
        ),
    )
    probe.add_argument("--output", type=Path, required=True)
    probe.add_argument("--index", type=int, default=ORIGINAL_GLYPHS - 1)
    build = sub.add_parser("build")
    build.add_argument("--assets", type=Path, required=True)
    build.add_argument("--input-elf", type=Path, required=True)
    build.add_argument("--output-elf", type=Path, required=True)
    build.add_argument("--map-output", type=Path, required=True)
    build.add_argument("--extra-translations", type=Path)
    build.add_argument(
        "--font", type=Path,
        default=Path(
            r"vendor\pretendard\packages\pretendard\dist\public\static\alternative"
            r"\Pretendard-Bold.ttf"
        ),
    )
    build.add_argument("--font-size", type=int, default=20)
    build.add_argument("--font-index", type=int, default=0,
                       help="face index inside a TTC; standalone TTF uses 0")
    build.add_argument("--stroke-width", type=int, default=0)
    build.add_argument("--stroke-level", type=int, default=160)
    args = parser.parse_args()
    if args.command == "probe":
        glyph = render_glyph(args.char, args.font)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(glyph)
        print(f"index={args.index} sjis={index_to_sjis(args.index).hex()} vaddr={FONT_VADDR + args.index * GLYPH_BYTES:#x}")
    else:
        build_font(args.assets, args.input_elf, args.output_elf, args.map_output,
                   args.font, args.font_size, args.stroke_width, args.stroke_level,
                   args.font_index, args.extra_translations)


if __name__ == "__main__":
    main()
