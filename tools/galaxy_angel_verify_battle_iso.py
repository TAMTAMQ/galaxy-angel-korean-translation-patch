#!/usr/bin/env python3
"""Verify translated GADAT002 blocks and all redirected SLGINIT FSTS copies."""

from __future__ import annotations

import argparse
import hashlib
import mmap
import struct
from pathlib import Path

import galaxy_angel_build as builder
import galaxy_angel_build_battle as battle
import galaxy_angel_translation as translation
import ikusa_lz


def expected_blocks(
    original: mmap.mmap,
    files: dict[str, builder.IsoFile],
    assets: Path,
    custom_map: dict[str, bytes],
) -> tuple[dict[int, bytes], dict[str, dict[int, set[int]]]]:
    item = builder.resolve_iso_file(files, "GADAT002")
    begin = item.extent * builder.SECTOR
    container = original[begin : begin + item.size]
    (
        battle_units,
        remaining_units,
        battle_runtime_copies,
        runtime_occurrences,
    ) = battle.collect_assets(assets, container)
    expected = {}
    source_by_hash = {}
    for offset in sorted(set(battle_units) | set(remaining_units)):
        raw, _consumed = ikusa_lz.decompress(original, begin + offset)
        source_by_hash.setdefault(hashlib.sha256(raw).digest(), offset)
        rebuilt, _battle_count, _remaining_count = battle.rebuild_raw(
            raw,
            battle_units.get(offset, []),
            remaining_units.get(offset, []),
            custom_map,
        )
        expected[offset] = rebuilt
    mapped_runtime = battle.map_runtime_occurrences(
        original, files, runtime_occurrences, source_by_hash
    )
    slginit = mapped_runtime.setdefault("SLGINIT", {})
    for source_offset, copies in battle_runtime_copies.items():
        slginit.setdefault(source_offset, set()).update(copies)
    return expected, mapped_runtime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--patched-iso", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--encoding-map", type=Path, required=True)
    args = parser.parse_args()
    custom_map = translation.load_custom_map(args.encoding_map)
    assert custom_map is not None

    with (
        args.original_iso.open("rb") as original_stream,
        mmap.mmap(original_stream.fileno(), 0, access=mmap.ACCESS_READ) as original,
        args.patched_iso.open("rb") as patched_stream,
        mmap.mmap(patched_stream.fileno(), 0, access=mmap.ACCESS_READ) as patched,
    ):
        original_files = builder.iso_files(original)
        patched_files = builder.iso_files(patched)
        expected, runtime_by_container = expected_blocks(
            original, original_files, args.assets, custom_map
        )

        original_gadat = builder.resolve_iso_file(original_files, "GADAT002")
        patched_gadat = builder.resolve_iso_file(patched_files, "GADAT002")
        if patched_gadat.extent != original_gadat.extent:
            raise SystemExit("GADAT002 base extent moved")
        gadat_begin = original_gadat.extent * builder.SECTOR
        original_header = bytearray(
            original[gadat_begin : gadat_begin + original_gadat.size]
        )
        patched_header = patched[gadat_begin : gadat_begin + original_gadat.size]
        original_records = builder.records(original_header)
        redirected = 0
        for old_offset, expected_raw in expected.items():
            record = original_records[old_offset][0]
            offset, raw_size, compressed_size = struct.unpack_from(
                "<III", patched_header, record
            )
            raw, consumed = ikusa_lz.decompress(patched, gadat_begin + offset)
            if (
                raw != expected_raw
                or raw_size != len(expected_raw)
                or compressed_size != consumed
            ):
                raise SystemExit(f"GADAT002 translated block mismatch: {old_offset:#x}")
            redirected += int(offset >= original_gadat.size)

        # Validate the complete local PIDX mirror against flat IDX.DAT.
        original_idx = builder.resolve_iso_file(original_files, "IDX")
        patched_idx = builder.resolve_iso_file(patched_files, "IDX")
        original_idx_begin = original_idx.extent * builder.SECTOR
        tuple_to_record = {
            (offset, raw_size, compressed_size): record
            for offset, (record, raw_size, compressed_size) in original_records.items()
        }
        candidates: dict[int, dict[int, int]] = {}
        for relative in range(0, original_idx.size - 15, 4):
            file_id, offset, raw_size, compressed_size = struct.unpack_from(
                "<IIII", original, original_idx_begin + relative
            )
            record = tuple_to_record.get((offset, raw_size, compressed_size))
            if record is not None:
                candidates.setdefault(file_id, {})[record] = relative
        file_id, positions = max(candidates.items(), key=lambda item: len(item[1]))
        if len(positions) != len(original_records):
            raise SystemExit(
                f"GADAT002 IDX mirror incomplete: {len(positions)}/{len(original_records)}"
            )
        patched_idx_begin = patched_idx.extent * builder.SECTOR
        for record, relative in positions.items():
            local_tuple = struct.unpack_from("<III", patched_header, record)
            flat_tuple = struct.unpack_from(
                "<III", patched, patched_idx_begin + relative + 4
            )
            if local_tuple != flat_tuple:
                raise SystemExit(f"GADAT002 IDX/PIDX mismatch: record {record:#x}")

        runtime_checked = 0
        unique_streams = set()
        runtime_counts = {}
        for stem, runtime_copies in sorted(runtime_by_container.items()):
            original_runtime = builder.resolve_iso_file(original_files, stem)
            patched_runtime = builder.resolve_iso_file(patched_files, stem)
            if patched_runtime.extent != original_runtime.extent:
                raise SystemExit(f"{stem} base extent moved")
            if patched_runtime.size != original_runtime.size:
                raise SystemExit(
                    f"{stem} logical size changed: "
                    f"{original_runtime.size}->{patched_runtime.size}"
                )
            runtime_begin = original_runtime.extent * builder.SECTOR
            original_runtime_data = original[
                runtime_begin : runtime_begin + original_runtime.size
            ]
            patched_runtime_data = patched[
                runtime_begin : runtime_begin + original_runtime.size
            ]
            original_fsts = battle.fsts_runtime_records(original_runtime_data)
            container_checked = 0
            for source_offset, copies in runtime_copies.items():
                for copy_offset in copies:
                    fsts_base, record, _old_raw, _old_comp = original_fsts[copy_offset]
                    offset, raw_size, compressed_size = struct.unpack_from(
                        "<III", patched_runtime_data, record + 4
                    )
                    absolute = runtime_begin + fsts_base + offset
                    raw, consumed = ikusa_lz.decompress(patched, absolute)
                    if (
                        raw != expected[source_offset]
                        or raw_size != len(raw)
                        or compressed_size != consumed
                    ):
                        raise SystemExit(
                            f"{stem} translated stream mismatch: {copy_offset:#x}"
                        )
                    unique_streams.add(absolute)
                    runtime_checked += 1
                    container_checked += 1
            runtime_counts[stem] = container_checked

        print(
            f"verified battle blocks={len(expected)} redirected={redirected} "
            f"idx_records={len(positions)} file_id={file_id} "
            f"runtime_copies={runtime_checked} runtime_unique={len(unique_streams)} "
            f"runtime={runtime_counts}"
        )


if __name__ == "__main__":
    main()
