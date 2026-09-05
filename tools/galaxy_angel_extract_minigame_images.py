#!/usr/bin/env python3
"""Extract every decodable 2D image from Galaxy Angel MINI.DAT.

The mini-game archive does not use the GADAT032 TEX format. It stores two kinds
of 2D assets that matter for UI translation:

* ``.agi``: one 4bpp indexed image with a 0x30-byte header and a 16-colour CLUT.
* ``.tag``: a PS2 DMA/GIF upload stream.  The image payloads live behind DMA REF
  tags and are described by BITBLTBUF/TRXREG commands in the preceding CNT
  packet.  These streams contain most title/tutorial/selection art for mini01-05.

The extractor mirrors the existing ``assets/image_extraction/GADAT032`` layout:
``raw/`` keeps the decompressed source resource, ``png/`` contains images, and
``manifest.json`` records exact source paths/offsets/transfer metadata.  A
contact sheet is generated for each top-level mini-game group so Japanese text
can be reviewed visually before any patch is made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import galaxy_angel_build as builder
import galaxy_angel_gadat032 as texcodec
import ikusa_lz
from galaxy_angel_patch_minigame_recipe import parse_named_nodes


STORED_MAGIC = 0x303B3320
AGI_HEADER_SIZE = 0x30
AGI_PALETTE_COLOURS = 16
PSMCT32 = 0x00
PSMCT24 = 0x01
PSMCT16 = 0x02
PSMT8 = 0x13
PSMT4 = 0x14
DMA_REFE = 0
DMA_CNT = 1
DMA_NEXT = 2
DMA_REF = 3
DMA_REFS = 4
DMA_CALL = 5
DMA_RET = 6
DMA_END = 7
GS_BITBLTBUF = 0x50
GS_TRXPOS = 0x51
GS_TRXREG = 0x52
GS_TRXDIR = 0x53


@dataclass
class Transfer:
    index: int
    psm: int
    width: int
    height: int
    data_offset: int
    data_size: int
    dma_size: int
    palette_entries: int = 0
    palette_offset: int | None = None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_resource(container: bytes, data_offset: int, raw_size: int, compressed_size: int) -> bytes:
    if data_offset + 8 > len(container):
        raise ValueError(f"resource outside MINI.DAT: {data_offset:#x}")
    magic, declared = struct.unpack_from("<II", container, data_offset)
    if declared != raw_size:
        raise ValueError(
            f"MINI.DAT raw-size mismatch at {data_offset:#x}: {declared}!={raw_size}"
        )
    if magic == ikusa_lz.MAGIC:
        raw, consumed = ikusa_lz.decompress(container, data_offset)
        if consumed != compressed_size:
            raise ValueError(
                f"MINI.DAT compressed-size mismatch at {data_offset:#x}: "
                f"{consumed}!={compressed_size}"
            )
        return raw
    if magic == STORED_MAGIC:
        if compressed_size != raw_size + 8:
            raise ValueError(
                f"stored MINI.DAT block size mismatch at {data_offset:#x}: "
                f"{compressed_size}!={raw_size + 8}"
            )
        end = data_offset + 8 + raw_size
        if end > len(container):
            raise ValueError(f"stored MINI.DAT block truncated at {data_offset:#x}")
        # Ikusa's uncompressed variant still XORs every payload byte with the
        # same 0x72 key used by the LZ stream.  A run of zeroes is therefore
        # stored as 0x72 bytes.
        return bytes(value ^ ikusa_lz.KEY for value in container[data_offset + 8 : end])
    raise ValueError(f"unknown MINI.DAT block codec {magic:#x} at {data_offset:#x}")


def decode_ps2_palette(data: bytes) -> list[bytes]:
    rgba = texcodec.decode_ps2_rgba(data)
    return [rgba[index : index + 4] for index in range(0, len(rgba), 4)]


def decode_agi(raw: bytes) -> Image.Image:
    if len(raw) < AGI_HEADER_SIZE + AGI_PALETTE_COLOURS * 4:
        raise ValueError("AGI is too small")
    width, height = struct.unpack_from("<HH", raw, 0x18)
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid AGI dimensions: {width}x{height}")
    pixel_count = width * height
    packed4_size = (pixel_count + 1) // 2
    expected4 = AGI_HEADER_SIZE + packed4_size + 16 * 4
    expected8 = AGI_HEADER_SIZE + pixel_count + 256 * 4
    if len(raw) == expected4:
        packed = raw[AGI_HEADER_SIZE : AGI_HEADER_SIZE + packed4_size]
        palette = decode_ps2_palette(raw[AGI_HEADER_SIZE + packed4_size : expected4])
        indices = bytes(
            nibble for value in packed for nibble in (value & 0x0F, value >> 4)
        )[:pixel_count]
    elif len(raw) == expected8:
        indices = raw[AGI_HEADER_SIZE : AGI_HEADER_SIZE + pixel_count]
        palette = decode_ps2_palette(raw[AGI_HEADER_SIZE + pixel_count : expected8])
        palette = texcodec.swizzle_ps2_clut(palette)
    else:
        raise ValueError(
            f"unsupported AGI layout: {width}x{height}, {len(raw)} bytes "
            f"not in ({expected4}, {expected8})"
        )
    pixels = bytearray()
    for index in indices:
        pixels.extend(palette[index])
    return Image.frombytes("RGBA", (width, height), bytes(pixels))


def _expected_transfer_bytes(psm: int, width: int, height: int) -> int | None:
    if width <= 0 or height <= 0:
        return None
    pixels = width * height
    if psm == PSMCT32:
        return pixels * 4
    if psm == PSMCT24:
        return pixels * 3
    if psm == PSMCT16:
        return pixels * 2
    if psm == PSMT8:
        return pixels
    if psm == PSMT4:
        return (pixels + 1) // 2
    return None


def _scan_gs_registers(payload: bytes, start: int, end: int, state: dict[str, int | None]) -> None:
    """Find A+D GS register packets embedded after a VIF DIRECT command.

    MINI ``.tag`` streams put the GIF payload four bytes after a VIF command, so
    A+D packets are not necessarily 16-byte aligned to the file.  Scanning at a
    four-byte cadence and requiring an exact 64-bit register address is both
    robust and strict enough for these resources.
    """
    end = min(end, len(payload))
    for pos in range(start, max(start, end - 15), 4):
        if pos + 16 > end:
            break
        register = struct.unpack_from("<Q", payload, pos + 8)[0]
        if register not in (GS_BITBLTBUF, GS_TRXPOS, GS_TRXREG, GS_TRXDIR):
            continue
        value = struct.unpack_from("<Q", payload, pos)[0]
        if register == GS_BITBLTBUF:
            state["psm"] = (value >> 56) & 0x3F
        elif register == GS_TRXREG:
            state["width"] = value & 0x0FFF
            state["height"] = (value >> 32) & 0x0FFF


def parse_tag_transfers(raw: bytes) -> list[Transfer]:
    """Follow the PS2 DMA chain and return all validated host->local image uploads."""
    transfers: list[Transfer] = []
    state: dict[str, int | None] = {"psm": None, "width": None, "height": None}
    offset = 0
    seen: set[int] = set()
    stack: list[int] = []
    steps = 0

    while 0 <= offset <= len(raw) - 16:
        if offset in seen:
            raise ValueError(f"cyclic TAG DMA chain at {offset:#x}")
        seen.add(offset)
        steps += 1
        if steps > 100000:
            raise ValueError("TAG DMA chain is unreasonably long")

        tag_word, address_word = struct.unpack_from("<II", raw, offset)
        qwc = tag_word & 0xFFFF
        tag_id = (tag_word >> 28) & 0x7
        address = address_word & 0x7FFFFFFF
        inline_size = qwc * 16

        if tag_id in (DMA_CNT, DMA_NEXT, DMA_CALL, DMA_END):
            inline_start = offset + 16
            inline_end = inline_start + inline_size
            if inline_end > len(raw):
                raise ValueError(f"TAG inline DMA payload truncated at {offset:#x}")
            _scan_gs_registers(raw, inline_start, inline_end, state)

        if tag_id in (DMA_REFE, DMA_REF, DMA_REFS):
            psm = state["psm"]
            width = state["width"]
            height = state["height"]
            if psm is not None and width is not None and height is not None:
                expected = _expected_transfer_bytes(int(psm), int(width), int(height))
                dma_size = qwc * 16
                if (
                    expected is not None
                    and dma_size >= expected
                    and dma_size - expected < 16
                    and address + expected <= len(raw)
                ):
                    transfers.append(
                        Transfer(
                            index=len(transfers),
                            psm=int(psm),
                            width=int(width),
                            height=int(height),
                            data_offset=address,
                            data_size=expected,
                            dma_size=dma_size,
                        )
                    )

        if tag_id == DMA_REFE:
            break
        if tag_id == DMA_CNT:
            offset = offset + 16 + inline_size
        elif tag_id == DMA_NEXT:
            offset = address
        elif tag_id in (DMA_REF, DMA_REFS):
            offset = offset + 16
        elif tag_id == DMA_CALL:
            stack.append(offset + 16 + inline_size)
            offset = address
        elif tag_id == DMA_RET:
            if not stack:
                break
            offset = stack.pop()
        elif tag_id == DMA_END:
            break
        else:
            raise ValueError(f"unsupported TAG DMA id {tag_id} at {offset:#x}")

    # Link indexed transfers to the immediately preceding suitable CLUT upload.
    last_16: Transfer | None = None
    last_256: Transfer | None = None
    for transfer in transfers:
        if transfer.psm == PSMCT32 and transfer.width * transfer.height == 16:
            transfer.palette_entries = 16
            last_16 = transfer
        elif transfer.psm == PSMCT32 and transfer.width * transfer.height == 256:
            transfer.palette_entries = 256
            last_256 = transfer
        elif transfer.psm == PSMT4 and last_16 is not None:
            transfer.palette_entries = 16
            transfer.palette_offset = last_16.data_offset
        elif transfer.psm == PSMT8 and last_256 is not None:
            transfer.palette_entries = 256
            transfer.palette_offset = last_256.data_offset
    return transfers


def decode_tag_transfer(raw: bytes, transfer: Transfer) -> Image.Image | None:
    data = raw[transfer.data_offset : transfer.data_offset + transfer.data_size]
    width, height = transfer.width, transfer.height
    if transfer.psm == PSMCT32:
        # Small 8x2 / 16x16 uploads are CLUTs, not user-facing images.
        if transfer.width * transfer.height in (16, 256):
            return None
        rgba = texcodec.decode_ps2_rgba(data)
        return Image.frombytes("RGBA", (width, height), rgba)
    if transfer.psm == PSMCT24:
        return Image.frombytes("RGB", (width, height), data).convert("RGBA")
    if transfer.psm == PSMCT16:
        # Rare in MINI; decode standard PS2 RGB5A1 for completeness.
        pixels = bytearray()
        for value in struct.unpack(f"<{width * height}H", data):
            red = (value & 0x1F) * 255 // 31
            green = ((value >> 5) & 0x1F) * 255 // 31
            blue = ((value >> 10) & 0x1F) * 255 // 31
            alpha = 255 if value & 0x8000 else 0
            pixels.extend((red, green, blue, alpha))
        return Image.frombytes("RGBA", (width, height), bytes(pixels))

    if transfer.palette_offset is None or transfer.palette_entries == 0:
        return None
    palette_bytes = raw[
        transfer.palette_offset : transfer.palette_offset + transfer.palette_entries * 4
    ]
    palette = decode_ps2_palette(palette_bytes)
    if transfer.palette_entries == 256:
        palette = texcodec.swizzle_ps2_clut(palette)
    if transfer.psm == PSMT8:
        indices = data[: width * height]
    elif transfer.psm == PSMT4:
        indices = bytes(nibble for value in data for nibble in (value & 0x0F, value >> 4))[
            : width * height
        ]
    else:
        return None
    pixels = bytearray()
    for index in indices:
        pixels.extend(palette[index])
    return Image.frombytes("RGBA", (width, height), bytes(pixels))


def safe_rel(path: str) -> Path:
    result = Path(*path.split("/"))
    if result.is_absolute() or ".." in result.parts:
        raise ValueError(f"unsafe MINI path: {path}")
    return result


def save_contact_sheet(group: str, items: list[tuple[str, Path]], output: Path) -> None:
    if not items:
        return
    thumb_w, thumb_h = 220, 170
    label_h = 42
    columns = 5
    rows = (len(items) + columns - 1) // columns
    sheet = Image.new("RGBA", (columns * thumb_w, rows * (thumb_h + label_h)), (32, 32, 32, 255))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (label, png_path) in enumerate(items):
        image = Image.open(png_path).convert("RGBA")
        image.thumbnail((thumb_w - 12, thumb_h - 12), Image.Resampling.LANCZOS)
        col = index % columns
        row = index // columns
        x = col * thumb_w + (thumb_w - image.width) // 2
        y = row * (thumb_h + label_h) + (thumb_h - image.height) // 2
        checker = Image.new("RGBA", image.size, (80, 80, 80, 255))
        sheet.alpha_composite(checker, (x, y))
        sheet.alpha_composite(image, (x, y))
        label_y = row * (thumb_h + label_h) + thumb_h + 2
        text = label if len(label) <= 36 else label[:33] + "..."
        draw.text((col * thumb_w + 4, label_y), text, font=font, fill=(255, 255, 255, 255))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(output, quality=92)


def extract(iso_path: Path, output_dir: Path, dry_run: bool = False) -> dict:
    with iso_path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
        files = builder.iso_files(image)
        mini_file = builder.resolve_iso_file(files, "MINI")
        begin = mini_file.extent * builder.SECTOR
        container = bytes(image[begin : begin + mini_file.size])

    nodes, _string_base, paths = parse_named_nodes(container)
    raw_root = output_dir / "raw"
    png_root = output_dir / "png"
    sheet_root = output_dir / "contact_sheets"
    source_sheet_root = output_dir / "source_sheets"
    if not dry_run:
        raw_root.mkdir(parents=True, exist_ok=True)
        png_root.mkdir(parents=True, exist_ok=True)
        sheet_root.mkdir(parents=True, exist_ok=True)
        source_sheet_root.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    group_images: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    agi_group_images: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    source_images: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    stats = Counter()

    for source_path, node_index in sorted(paths.items()):
        kind, _name_offset, _child_count, data_offset, raw_size, compressed_size = nodes[node_index]
        if kind != 0:
            continue
        suffix = Path(source_path).suffix.lower()
        if suffix not in (".agi", ".tag"):
            continue
        raw = read_resource(container, data_offset, raw_size, compressed_size)
        rel = safe_rel(source_path)
        if not dry_run:
            raw_path = raw_root / rel
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw)

        entry = {
            "source_path": source_path,
            "node_index": node_index,
            "block_offset": data_offset,
            "raw_size": raw_size,
            "compressed_size": compressed_size,
            "raw_sha256": sha256(raw),
            "type": suffix.lstrip("."),
            "images": [],
        }

        group = source_path.split("/")[1] if source_path.startswith("mini/") else "root"
        if suffix == ".agi":
            try:
                decoded = decode_agi(raw)
            except ValueError as exc:
                entry["decode_error"] = str(exc)
                stats["agi_unsupported"] += 1
            else:
                png_rel = rel.with_suffix(".png")
                png_path = png_root / png_rel
                if not dry_run:
                    png_path.parent.mkdir(parents=True, exist_ok=True)
                    decoded.save(png_path)
                    group_images[group].append((source_path, png_path))
                    agi_group_images[group].append((source_path, png_path))
                entry["images"].append(
                    {
                        "png": png_rel.as_posix(),
                        "width": decoded.width,
                        "height": decoded.height,
                        "psm": "indexed4",
                        "transfer_index": None,
                    }
                )
                stats["agi_images"] += 1
        else:
            try:
                transfers = parse_tag_transfers(raw)
            except ValueError as exc:
                entry["decode_error"] = str(exc)
                stats["tag_unsupported"] += 1
                transfers = []
            image_number = 0
            for transfer in transfers:
                decoded = decode_tag_transfer(raw, transfer)
                if decoded is None:
                    continue
                stem = rel.with_suffix("")
                filename = (
                    f"transfer_{image_number:03d}_{transfer.width}x{transfer.height}_"
                    f"psm{transfer.psm:02x}.png"
                )
                png_rel = stem / filename
                png_path = png_root / png_rel
                if not dry_run:
                    png_path.parent.mkdir(parents=True, exist_ok=True)
                    decoded.save(png_path)
                    label = f"{source_path}#{image_number:03d}"
                    group_images[group].append((label, png_path))
                    source_images[source_path].append((f"#{image_number:03d}", png_path))
                entry["images"].append(
                    {
                        "png": png_rel.as_posix(),
                        "width": decoded.width,
                        "height": decoded.height,
                        "psm": f"0x{transfer.psm:02x}",
                        "transfer_index": transfer.index,
                        "image_index": image_number,
                        "data_offset": transfer.data_offset,
                        "data_size": transfer.data_size,
                        "dma_size": transfer.dma_size,
                        "palette_entries": transfer.palette_entries,
                        "palette_offset": transfer.palette_offset,
                    }
                )
                image_number += 1
                stats["tag_images"] += 1
        manifest.append(entry)

    if not dry_run:
        for group, items in sorted(group_images.items()):
            # Very large groups are split into pages so the sheets stay usable.
            page_size = 100
            for page_start in range(0, len(items), page_size):
                page = page_start // page_size + 1
                suffix = f"_{page:02d}" if len(items) > page_size else ""
                save_contact_sheet(
                    group,
                    items[page_start : page_start + page_size],
                    sheet_root / f"{group}{suffix}.jpg",
                )
        agi_sheet_root = output_dir / "agi_sheets"
        agi_sheet_root.mkdir(parents=True, exist_ok=True)
        agi_sheet_index: dict[str, list[str]] = {}
        for group, items in sorted(agi_group_images.items()):
            indexed_items = [(f"#{index:03d}", path) for index, (_label, path) in enumerate(items)]
            agi_sheet_index[group] = [label for label, _path in items]
            page_size = 40
            for page_start in range(0, len(indexed_items), page_size):
                page = page_start // page_size + 1
                suffix = f"_{page:02d}" if len(indexed_items) > page_size else ""
                save_contact_sheet(
                    group,
                    indexed_items[page_start : page_start + page_size],
                    agi_sheet_root / f"{group}{suffix}.jpg",
                )
        for source_path, items in sorted(source_images.items()):
            source_rel = safe_rel(source_path).with_suffix("")
            page_size = 40
            for page_start in range(0, len(items), page_size):
                page = page_start // page_size + 1
                suffix = f"_{page:02d}" if len(items) > page_size else ""
                save_contact_sheet(
                    source_path,
                    items[page_start : page_start + page_size],
                    source_sheet_root / source_rel.parent / f"{source_rel.name}{suffix}.jpg",
                )
        payload = {
            "schema": "galaxy-angel-mini-image-extraction/v1",
            "source_iso": str(iso_path),
            "mini_extent": mini_file.extent,
            "mini_size": mini_file.size,
            "stats": dict(stats),
            "agi_sheet_index": agi_sheet_index,
            "resources": manifest,
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(
        f"MINI image extraction: AGI={stats['agi_images']} TAG={stats['tag_images']} "
        f"unsupported_agi={stats['agi_unsupported']} unsupported_tag={stats['tag_unsupported']} "
        f"resources={len(manifest)} dry_run={dry_run}",
        flush=True,
    )
    return {"stats": dict(stats), "resources": manifest}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    extract(args.iso, args.output, args.dry_run)


if __name__ == "__main__":
    main()
