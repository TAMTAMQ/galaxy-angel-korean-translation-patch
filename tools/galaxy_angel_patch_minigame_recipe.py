#!/usr/bin/env python3
"""Patch Milfeulle's sandwich mini-game recipe label inside MINI.DAT.

The recipe cards render the Japanese ``レシピ`` label from the 4bpp AGI texture
``mini/mini00/resipi.agi`` and draw the number separately from ``resip_no.agi``.
This patch keeps the original 64x32 texture, palette, placement, and physical
PIDX slot, replacing only the label bitmap with ``레시피``.
"""

from __future__ import annotations

import argparse
import mmap
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import galaxy_angel_build as builder
import ikusa_lz


TARGET_PATH = "mini/mini00/resipi.agi"
TEXT = "레시피"
HEADER_SIZE = 0x30
EXPECTED_SIZE = (64, 32)
TRANSPARENT_INDEX = 15
FONT_SIZE = 18
SUPERSAMPLE = 4
STROKE_WIDTH = 2


def parse_named_nodes(container: bytes) -> tuple[list[tuple[int, int, int, int, int, int]], int, dict[str, int]]:
    if container[:8] != b"PIDX0\0\0\0":
        raise SystemExit("MINI.DAT is not a PIDX0 container")
    count = struct.unpack_from("<I", container, 0x10)[0]
    string_base = struct.unpack_from("<I", container, 0x20)[0]
    if count <= 0 or 0x50 + count * 24 > len(container):
        raise SystemExit("invalid MINI.DAT PIDX node table")
    nodes = [struct.unpack_from("<6I", container, 0x50 + index * 24) for index in range(count)]

    def node_name(name_offset: int) -> str:
        start = string_base + name_offset
        if start < string_base or start >= len(container):
            raise SystemExit(f"invalid MINI.DAT name offset: {name_offset:#x}")
        end = container.find(b"\0", start)
        if end < 0:
            raise SystemExit("unterminated MINI.DAT name")
        return container[start:end].decode("ascii")

    paths: dict[str, int] = {}
    visiting: set[int] = set()

    def walk(index: int, prefix: str = "") -> None:
        if index in visiting:
            raise SystemExit("cyclic MINI.DAT PIDX directory tree")
        if not 0 <= index < len(nodes):
            raise SystemExit(f"MINI.DAT child index out of range: {index}")
        visiting.add(index)
        kind, name_offset, child_count, first_child, _raw_size, _compressed_size = nodes[index]
        name = node_name(name_offset)
        path = f"{prefix}/{name}".strip("/")
        paths[path] = index
        if kind == 1:
            if first_child + child_count > len(nodes):
                raise SystemExit(f"MINI.DAT child range out of bounds: {path}")
            for child in range(first_child, first_child + child_count):
                walk(child, path)
        elif kind != 0:
            raise SystemExit(f"unknown MINI.DAT node type {kind} at {path}")
        visiting.remove(index)

    walk(0)
    return nodes, string_base, paths


def decode_palette(raw: bytes, pixel_bytes: int) -> list[tuple[int, int, int, int]]:
    palette_raw = raw[HEADER_SIZE + pixel_bytes : HEADER_SIZE + pixel_bytes + 64]
    if len(palette_raw) != 64:
        raise SystemExit("recipe AGI palette is incomplete")
    palette = []
    for index in range(16):
        red, green, blue, alpha = palette_raw[index * 4 : index * 4 + 4]
        palette.append((red, green, blue, min(255, alpha * 2)))
    if palette[TRANSPARENT_INDEX][3] != 0:
        raise SystemExit("recipe AGI transparent palette index changed")
    return palette


def render_recipe_pixels(raw: bytes, font_path: Path, png_path: Path | None = None) -> bytes:
    if png_path is not None:
        from galaxy_angel_patch_minigame_images import encode_agi
        with Image.open(png_path) as source:
            return encode_agi(raw, source.convert("RGBA"))[0]
    if len(raw) < HEADER_SIZE + 64:
        raise SystemExit("recipe AGI is too small")
    width, height = struct.unpack_from("<HH", raw, 0x18)
    if (width, height) != EXPECTED_SIZE:
        raise SystemExit(f"unexpected recipe AGI dimensions: {width}x{height}")
    pixel_bytes = width * height // 2
    expected_size = HEADER_SIZE + pixel_bytes + 64
    if len(raw) != expected_size:
        raise SystemExit(f"unexpected recipe AGI size: {len(raw)} != {expected_size}")

    palette = decode_palette(raw, pixel_bytes)
    scale = SUPERSAMPLE
    canvas = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(font_path), FONT_SIZE * scale)
    stroke = STROKE_WIDTH * scale
    bbox = draw.textbbox((0, 0), TEXT, font=font, stroke_width=stroke)
    # The original レシピ bitmap occupies x=0..49, y=0..20.  With Pretendard
    # Bold 18px + 2px outline, 레시피 produces the same 50x21 occupied box.
    x = -bbox[0]
    y = -bbox[1]
    draw.text(
        (x, y),
        TEXT,
        font=font,
        fill=(254, 254, 254, 255),
        stroke_width=stroke,
        stroke_fill=(92, 76, 83, 255),
    )
    canvas = canvas.resize((width, height), Image.Resampling.LANCZOS)

    indices: list[int] = []
    for red, green, blue, alpha in canvas.getdata():
        if alpha < 8:
            indices.append(TRANSPARENT_INDEX)
            continue
        best = min(
            range(TRANSPARENT_INDEX),
            key=lambda index: (
                (palette[index][0] - red) ** 2
                + (palette[index][1] - green) ** 2
                + (palette[index][2] - blue) ** 2
                + 2 * (palette[index][3] - alpha) ** 2
            ),
        )
        indices.append(best)

    occupied = [
        (xpos, ypos)
        for ypos in range(height)
        for xpos in range(width)
        if indices[ypos * width + xpos] != TRANSPARENT_INDEX
    ]
    rendered_bbox = (
        min(x for x, _y in occupied),
        min(y for _x, y in occupied),
        max(x for x, _y in occupied),
        max(y for _x, y in occupied),
    )
    if rendered_bbox != (0, 0, 49, 20):
        raise SystemExit(f"recipe label placement changed: {rendered_bbox}")

    packed = bytes(
        (indices[index] & 0x0F) | ((indices[index + 1] & 0x0F) << 4)
        for index in range(0, len(indices), 2)
    )
    rebuilt = bytearray(raw)
    rebuilt[HEADER_SIZE : HEADER_SIZE + pixel_bytes] = packed
    return bytes(rebuilt)


def patch_iso(iso_path: Path, font_path: Path, png_path: Path | None = None) -> None:
    image = bytearray(iso_path.read_bytes())
    files = builder.iso_files(image)
    mini_file = builder.resolve_iso_file(files, "MINI")
    mini_begin = mini_file.extent * builder.SECTOR
    mini_end = mini_begin + mini_file.size
    container = bytearray(image[mini_begin:mini_end])
    original_records = builder.records(container)
    if not original_records:
        raise SystemExit("MINI.DAT has no PIDX leaves")

    nodes, _string_base, paths = parse_named_nodes(container)
    target_index = paths.get(TARGET_PATH)
    if target_index is None:
        raise SystemExit(f"MINI.DAT target not found: {TARGET_PATH}")
    kind, _name_offset, _child_count, data_offset, raw_size, compressed_size = nodes[target_index]
    if kind != 0:
        raise SystemExit(f"MINI.DAT target is not a file: {TARGET_PATH}")

    raw, consumed = ikusa_lz.decompress(container, data_offset)
    if len(raw) != raw_size or consumed != compressed_size:
        raise SystemExit(
            f"MINI.DAT source mismatch: raw {len(raw)}/{raw_size}, compressed {consumed}/{compressed_size}"
        )
    patched_raw = render_recipe_pixels(raw, font_path, png_path)
    # Use the long-established greedy IkusaLib encoder for this tiny runtime
    # sprite.  The translated payload still fits comfortably in the original
    # allocation, and this avoids relying on optimal-token streams for a MINI
    # texture that the game uploads directly at runtime.
    compressed = ikusa_lz.compress(patched_raw)
    leaf_offsets = sorted(
        node[3] for node in nodes if node[0] == 0 and node[3] > data_offset
    )
    slot_end = leaf_offsets[0] if leaf_offsets else len(container)
    slot_capacity = slot_end - data_offset
    if len(compressed) > slot_capacity:
        raise SystemExit(
            f"translated recipe texture exceeds physical PIDX slot: "
            f"{len(compressed)}>{slot_capacity}"
        )

    # Keep the original physical PIDX slot.  A repeated patch may legitimately
    # grow from an earlier, more tightly compressed translated stream while
    # still remaining well before the next aligned asset.
    clear_size = max(compressed_size, len(compressed))
    container[data_offset : data_offset + clear_size] = bytes(clear_size)
    container[data_offset : data_offset + len(compressed)] = compressed
    record_pos = 0x50 + target_index * 24 + 12
    local_tuple = struct.unpack_from("<III", container, record_pos)
    if local_tuple != (data_offset, raw_size, compressed_size):
        raise SystemExit(f"MINI.DAT target record mismatch: {local_tuple}")
    struct.pack_into("<III", container, record_pos, data_offset, raw_size, len(compressed))
    image[mini_begin:mini_end] = container

    file_id, central_patched = builder.patch_central_idx_records(
        image,
        files,
        original_records,
        {record_pos: (data_offset, raw_size, len(compressed))},
    )
    # A repeated build can encounter an already-localized payload whose local
    # PIDX tuple and central IDX.DAT mirror already contain the shorter size.
    # In that idempotent case no central tuple needs changing; a first-time
    # patch still changes exactly one tuple.
    if central_patched not in (0, 1):
        raise SystemExit(f"expected zero or one MINI IDX.DAT mirror update, got {central_patched}")

    # Verify the exact target bytes after all local/central index updates.
    verify_container = image[mini_begin:mini_end]
    check, check_consumed = ikusa_lz.decompress(verify_container, data_offset)
    if check != patched_raw or check_consumed != len(compressed):
        raise SystemExit("MINI recipe texture round-trip verification failed")
    if struct.unpack_from("<III", verify_container, record_pos) != (
        data_offset,
        raw_size,
        len(compressed),
    ):
        raise SystemExit("MINI recipe PIDX record verification failed")

    iso_path.write_bytes(image)
    print(
        f"patched Milfeulle recipe label {TARGET_PATH}: 레시피, "
        f"64x32 bbox=0,0-49,20 compressed={compressed_size}->{len(compressed)} "
        f"IDX file_id={file_id}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--recipe-png", type=Path)
    args = parser.parse_args()
    patch_iso(args.iso, args.font, args.recipe_png)


if __name__ == "__main__":
    main()
