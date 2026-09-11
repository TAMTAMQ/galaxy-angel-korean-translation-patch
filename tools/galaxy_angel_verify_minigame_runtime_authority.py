#!/usr/bin/env python3
"""Verify every translated MINI runtime/FSTS copy against the current named resource.

The translated_png tree is the authority.  The normal named-image verifier checks
that the named MINI resources were rebuilt from those PNGs; this audit closes the
second half of the chain by proving that every runtime/FSTS copy decodes to the
same raw resource as the current named entry.  It derives runtime locations from
the original ISO instead of trusting a prior runtime report or seed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import galaxy_angel_build as builder
import ikusa_lz
from galaxy_angel_extract_minigame_images import read_resource
from galaxy_angel_patch_minigame_recipe import TARGET_PATH, parse_named_nodes
from galaxy_angel_patch_minigame_runtime_images import iter_fsts_entries


def mini_container(image: bytes) -> bytes:
    files = builder.iso_files(image)
    item = builder.resolve_iso_file(files, "MINI")
    begin = item.extent * builder.SECTOR
    return image[begin : begin + item.size]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--mini-patch-report", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    original = mini_container(args.original_iso.read_bytes())
    current = mini_container(args.iso.read_bytes())
    original_nodes, _strings, original_paths = parse_named_nodes(original)
    current_nodes, _strings, current_paths = parse_named_nodes(current)
    original_fsts = iter_fsts_entries(original)
    current_fsts_by_record = {
        int(entry["record"]): entry for entry in iter_fsts_entries(current)
    }

    patch_report = json.loads(args.mini_patch_report.read_text(encoding="utf-8"))
    targets = [str(item["source_path"]) for item in patch_report.get("resources", [])]
    if TARGET_PATH not in targets:
        targets.append(TARGET_PATH)

    mismatches: list[dict[str, object]] = []
    checked = 0
    for source_path in targets:
        if source_path not in original_paths or source_path not in current_paths:
            mismatches.append({"source_path": source_path, "error": "named path missing"})
            continue

        original_node = original_nodes[original_paths[source_path]]
        current_node = current_nodes[current_paths[source_path]]
        original_offset = int(original_node[3])
        original_raw_size = int(original_node[4])
        original_compressed_size = int(original_node[5])
        original_block = original[
            original_offset : original_offset + original_compressed_size
        ]

        matches = [
            entry
            for entry in original_fsts
            if int(entry["absolute_offset"]) != original_offset
            and int(entry["raw_size"]) == original_raw_size
            and int(entry["compressed_size"]) == original_compressed_size
            and original[
                int(entry["absolute_offset"]) : int(entry["absolute_offset"])
                + original_compressed_size
            ]
            == original_block
        ]
        if len(matches) != 1:
            mismatches.append(
                {
                    "source_path": source_path,
                    "error": "runtime mapping is not unique",
                    "matches": [int(item["absolute_offset"]) for item in matches],
                }
            )
            continue

        original_entry = matches[0]
        current_entry = current_fsts_by_record.get(int(original_entry["record"]))
        if current_entry is None:
            mismatches.append(
                {
                    "source_path": source_path,
                    "error": "current FSTS record missing",
                    "record": int(original_entry["record"]),
                }
            )
            continue
        runtime_offset = int(current_entry["absolute_offset"])
        runtime_raw, _consumed = ikusa_lz.decompress(current, runtime_offset)
        named_raw = read_resource(
            current,
            int(current_node[3]),
            int(current_node[4]),
            int(current_node[5]),
        )
        checked += 1
        if runtime_raw != named_raw:
            mismatches.append(
                {
                    "source_path": source_path,
                    "runtime_offset": runtime_offset,
                    "runtime_raw_sha256": hashlib.sha256(runtime_raw).hexdigest(),
                    "named_raw_sha256": hashlib.sha256(named_raw).hexdigest(),
                    "error": "runtime payload differs from current named resource",
                }
            )

    report = {
        "schema": "galaxy-angel-mini-runtime-authority/v1",
        "iso": str(args.iso),
        "checked": checked,
        "expected": len(targets),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if mismatches:
        raise SystemExit(
            f"MINI runtime authority verification failed: {len(mismatches)} mismatches"
        )
    print(f"verified MINI runtime authority: {checked}/{len(targets)} exact named/raw matches")


if __name__ == "__main__":
    main()
