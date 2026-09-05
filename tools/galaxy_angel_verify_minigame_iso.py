#!/usr/bin/env python3
"""Verify every localized MINI.DAT image directly from a built ISO."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path

from PIL import Image

import galaxy_angel_build as builder
import ikusa_lz
from galaxy_angel_extract_minigame_images import (
    decode_agi,
    decode_tag_transfer,
    parse_tag_transfers,
    read_resource,
)
from galaxy_angel_patch_minigame_images import encode_resource, pixel_hash
from galaxy_angel_patch_minigame_recipe import (
    TARGET_PATH as RECIPE_PATH,
    parse_named_nodes,
    render_recipe_pixels,
)
from galaxy_angel_patch_minigame_runtime_images import iter_fsts_entries


def verify_iso(
    iso_path: Path,
    extraction: Path,
    translations: Path,
    font_path: Path,
    report_path: Path,
    runtime_report_path: Path | None = None,
    recipe_png: Path | None = None,
) -> None:
    manifest = json.loads((extraction / "manifest.json").read_text(encoding="utf-8"))
    localization = json.loads(translations.read_text(encoding="utf-8"))
    resources = {item["source_path"]: item for item in manifest["resources"]}

    patches_by_source: dict[str, list[dict]] = defaultdict(list)
    for item in localization["items"]:
        translated = extraction / "translated_png" / Path(*item["translated_png"].split("/"))
        target = Image.open(translated).convert("RGBA")
        for occurrence in item["occurrences"]:
            key = f"{occurrence['source_path']}#{occurrence.get('image_index')}"
            patches_by_source[occurrence["source_path"]].append(
                {
                    "key": key,
                    "image_index": occurrence.get("image_index"),
                    "transfer_index": occurrence.get("transfer_index"),
                    "target": target,
                }
            )

    image = iso_path.read_bytes()
    files = builder.iso_files(image)
    mini_file = builder.resolve_iso_file(files, "MINI")
    begin = mini_file.extent * builder.SECTOR
    container = image[begin : begin + mini_file.size]
    nodes, _string_base, paths = parse_named_nodes(container)

    mismatches: list[dict] = []
    verified = 0
    for source_path, patches in sorted(patches_by_source.items()):
        node_index = paths.get(source_path)
        if node_index is None:
            mismatches.append({"source_path": source_path, "error": "path missing"})
            continue
        kind, _name, _children, data_offset, raw_size, compressed_size = nodes[node_index]
        if kind != 0:
            mismatches.append({"source_path": source_path, "error": "not a file"})
            continue
        raw = read_resource(container, data_offset, raw_size, compressed_size)
        resource = resources[source_path]

        if resource["type"] == "agi":
            actual = {None: decode_agi(raw)}
        else:
            transfers = {item.index: item for item in parse_tag_transfers(raw)}
            by_image = {int(item["image_index"]): item for item in resource["images"]}
            actual = {}
            for patch in patches:
                image_index = int(patch["image_index"])
                meta = by_image[image_index]
                actual[image_index] = decode_tag_transfer(
                    raw, transfers[int(meta["transfer_index"])]
                )

        _expected_raw, expected_hashes = encode_resource(raw, resource, patches)
        for patch in patches:
            image_index = patch["image_index"]
            actual_hash = pixel_hash(actual[image_index])
            expected_hash = expected_hashes[patch["key"]]
            verified += 1
            if actual_hash != expected_hash:
                mismatches.append(
                    {
                        "source_path": source_path,
                        "image_index": image_index,
                        "actual_pixel_sha256": actual_hash,
                        "expected_pixel_sha256": expected_hash,
                    }
                )

    recipe_index = paths.get(RECIPE_PATH)
    if recipe_index is None:
        mismatches.append({"source_path": RECIPE_PATH, "error": "path missing"})
    else:
        kind, _name, _children, data_offset, raw_size, compressed_size = nodes[recipe_index]
        if kind != 0:
            mismatches.append({"source_path": RECIPE_PATH, "error": "not a file"})
        else:
            raw = read_resource(container, data_offset, raw_size, compressed_size)
            expected = render_recipe_pixels(raw, font_path, recipe_png)
            verified += 1
            if raw != expected:
                mismatches.append({"source_path": RECIPE_PATH, "error": "recipe pixels differ"})

    runtime_verified = 0
    runtime_fsts_verified = 0
    if runtime_report_path is not None:
        runtime_report = json.loads(runtime_report_path.read_text(encoding="utf-8"))
        entries_by_offset: dict[int, list[dict]] = defaultdict(list)
        for entry in iter_fsts_entries(container):
            entries_by_offset[int(entry["absolute_offset"])].append(entry)
        for item in runtime_report.get("resources", []):
            source_path = str(item["source_path"])
            runtime_offset = int(item["runtime_offset"])
            expected_raw_size = int(item["raw_size"])
            expected_compressed = int(item["compressed_translated"])
            matches = entries_by_offset.get(runtime_offset, [])
            if len(matches) != 1:
                mismatches.append(
                    {
                        "source_path": source_path,
                        "runtime_offset": runtime_offset,
                        "error": f"expected one FSTS record, got {len(matches)}",
                    }
                )
                continue
            entry = matches[0]
            resource_id, relative_offset, table_raw, table_compressed = struct.unpack_from(
                "<4I", container, int(entry["record"])
            )
            decoded, consumed = ikusa_lz.decompress(container, runtime_offset)
            actual_sha = hashlib.sha256(decoded).hexdigest()
            expected_sha = item.get("raw_sha256")
            runtime_verified += 1
            if (
                table_raw != expected_raw_size
                or table_compressed != expected_compressed
                or len(decoded) != expected_raw_size
                or consumed != expected_compressed
                or (expected_sha and actual_sha != expected_sha)
            ):
                mismatches.append(
                    {
                        "source_path": source_path,
                        "runtime_offset": runtime_offset,
                        "fsts_resource_id": resource_id,
                        "fsts_relative_offset": relative_offset,
                        "table_raw_size": table_raw,
                        "table_compressed_size": table_compressed,
                        "actual_raw_size": len(decoded),
                        "actual_compressed_size": consumed,
                        "actual_raw_sha256": actual_sha,
                        "expected_raw_size": expected_raw_size,
                        "expected_compressed_size": expected_compressed,
                        "expected_raw_sha256": expected_sha,
                        "error": "runtime FSTS/stream mismatch",
                    }
                )
            else:
                runtime_fsts_verified += 1

    report = {
        "schema": "galaxy-angel-mini-image-iso-verification/v2",
        "iso": str(iso_path),
        "translated_unique": int(localization["translated_unique"]),
        "translated_occurrences": int(localization["translated_occurrences"]),
        "verified_occurrences": verified,
        "verified_resources": len(patches_by_source),
        "recipe_verified": not any(item["source_path"] == RECIPE_PATH for item in mismatches),
        "runtime_report": str(runtime_report_path) if runtime_report_path is not None else None,
        "runtime_verified_resources": runtime_verified,
        "runtime_fsts_verified": runtime_fsts_verified,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if mismatches:
        raise SystemExit(
            f"MINI ISO image verification failed: {len(mismatches)} mismatches; {report_path}"
        )
    print(
        f"MINI ISO image verification: unique={report['translated_unique']} "
        f"occurrences={verified} resources={len(patches_by_source)} recipe=1 "
        f"runtime={runtime_fsts_verified}/{runtime_verified} mismatches=0",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--extraction", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--runtime-report", type=Path)
    parser.add_argument("--recipe-png", type=Path)
    args = parser.parse_args()
    verify_iso(
        args.iso,
        args.extraction,
        args.translations,
        args.font,
        args.report,
        args.runtime_report,
        args.recipe_png,
    )


if __name__ == "__main__":
    main()
