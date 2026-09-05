#!/usr/bin/env python3
"""Verify GADAT032 image patches and their runtime copies from a build report."""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

from PIL import Image

import galaxy_angel_build as builder
import ikusa_lz
from galaxy_angel_gadat032 import decode_tex, named_records, pixel_hash
from galaxy_angel_patch_gadat032_images import central_idx_positions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    entries = report.get("entries", [])
    if report.get("changed_count") != len(entries):
        raise SystemExit("image report changed_count does not match entries")
    if not entries:
        print("verified image patch report: no changed images")
        return

    with args.iso.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as image:
        files = builder.iso_files(image)
        item = builder.resolve_iso_file(files, "GADAT032")
        begin = item.extent * builder.SECTOR
        container = bytearray(image[begin : begin + item.size])
        records = builder.records(container)
        by_name = named_records(container, records)
        file_id, idx_positions = central_idx_positions(image, files, records)
        runtime_count = 0
        quantized_count = 0
        for entry in entries:
            name = entry["name"].lower()
            if name not in by_name:
                raise SystemExit(f"reported texture is missing: {name}")
            offset, record, raw_size, compressed_size = by_name[name]
            if offset != entry["primary_offset"]:
                raise SystemExit(f"primary offset changed: {name}")
            if raw_size != entry["new_raw_size"]:
                raise SystemExit(f"primary raw size mismatch: {name}")
            if compressed_size != entry["new_compressed_size"]:
                raise SystemExit(f"primary compressed size mismatch: {name}")
            raw, consumed = ikusa_lz.decompress(container, offset)
            if consumed != compressed_size:
                raise SystemExit(f"primary compressed stream length mismatch: {name}")
            if hashlib.sha256(raw).hexdigest() != entry["raw_sha256"]:
                raise SystemExit(f"primary raw hash mismatch: {name}")
            if pixel_hash(decode_tex(raw)) != entry["expected_pixel_sha256"]:
                raise SystemExit(f"primary rendered pixel hash mismatch: {name}")
            idx_tuple = struct.unpack_from("<IIII", image, idx_positions[record])
            if idx_tuple != (file_id, offset, raw_size, compressed_size):
                raise SystemExit(f"IDX.DAT central record mismatch: {name}")
            png_path = Path(entry["png"])
            if png_path.is_file():
                with Image.open(png_path) as png:
                    if pixel_hash(png) != entry["source_pixel_sha256"]:
                        raise SystemExit(f"source PNG changed after image patch: {name}")
            for runtime in entry["runtime_copies"]:
                runtime_raw, runtime_used = ikusa_lz.decompress(
                    image, runtime["iso_offset"]
                )
                if runtime_used != compressed_size or runtime_raw != raw:
                    raise SystemExit(
                        f"runtime copy mismatch: {name} at {runtime['iso_offset']:#x}"
                    )
                if compressed_size > runtime["capacity"]:
                    raise SystemExit(f"runtime copy exceeds its slot: {name}")
                runtime_count += 1
            if entry["quantized_colours"] is not None:
                quantized_count += 1
    print(
        f"verified {len(entries)} image patches, {runtime_count} runtime copies, "
        f"{quantized_count} quantized textures"
    )


if __name__ == "__main__":
    main()
