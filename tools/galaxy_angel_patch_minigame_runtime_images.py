#!/usr/bin/env python3
"""Patch the unindexed runtime copies of localized MINI.DAT image blocks.

MINI.DAT contains a named PIDX source area and a second, 16-byte-aligned block
pool used by the game at runtime.  The normal MINI image patcher updates the
named records; this tool mirrors those rebuilt blocks into the runtime pool.
Normally each resource keeps its original slot.  With --preserve-pixels, an
oversized translated stream may exchange existing slots with another resource
inside the same FSTS table so the canonical pixels stay byte-for-byte intact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

from galaxy_angel_extract_minigame_images import AGI_HEADER_SIZE, decode_ps2_palette, read_resource
from galaxy_angel_patch_minigame_images import encode_block
from galaxy_angel_patch_minigame_recipe import TARGET_PATH as RECIPE_PATH, parse_named_nodes
import galaxy_angel_build as builder
import ikusa_lz


ALIGNMENT = 16
FSTS_HEADER_SIZE = 32
FSTS_RECORD_SIZE = 16


def iter_fsts_entries(container: bytes | bytearray) -> list[dict]:
    """Return validated FSTS runtime records embedded in MINI.DAT."""
    data = bytes(container)
    entries: list[dict] = []
    cursor = 0
    while True:
        base = data.find(b"FSTS", cursor)
        if base < 0:
            break
        cursor = base + 4
        if base + FSTS_HEADER_SIZE > len(data):
            continue
        _magic, count, header_size, table_size = struct.unpack_from("<4I", data, base)
        if header_size != FSTS_HEADER_SIZE:
            continue
        if table_size != FSTS_HEADER_SIZE + count * FSTS_RECORD_SIZE:
            continue
        if base + table_size > len(data):
            continue
        for index in range(count):
            record = base + FSTS_HEADER_SIZE + index * FSTS_RECORD_SIZE
            resource_id, relative_offset, raw_size, compressed_size = struct.unpack_from(
                "<4I", data, record
            )
            absolute_offset = base + relative_offset
            if absolute_offset + compressed_size > len(data):
                raise ValueError(
                    f"FSTS resource outside MINI.DAT: base={base:#x} "
                    f"offset={relative_offset:#x} size={compressed_size:#x}"
                )
            entries.append(
                {
                    "base": base,
                    "record": record,
                    "resource_id": resource_id,
                    "relative_offset": relative_offset,
                    "absolute_offset": absolute_offset,
                    "raw_size": raw_size,
                    "compressed_size": compressed_size,
                }
            )
    return entries


def aligned(value: int) -> int:
    return (value + ALIGNMENT - 1) & -ALIGNMENT


def mini_container(image: bytes) -> tuple[builder.IsoFile, bytes]:
    files = builder.iso_files(image)
    mini_file = builder.resolve_iso_file(files, "MINI")
    begin = mini_file.extent * builder.SECTOR
    return mini_file, image[begin : begin + mini_file.size]


def agi_indices(raw: bytes) -> tuple[int, int, list[tuple[int, int, int, int]], list[int], int]:
    width, height = struct.unpack_from("<HH", raw, 0x18)
    count = width * height
    packed = (count + 1) // 2
    if len(raw) != AGI_HEADER_SIZE + packed + 16 * 4:
        raise ValueError("runtime size fitting currently supports 4bpp AGI only")
    palette = [tuple(value) for value in decode_ps2_palette(raw[AGI_HEADER_SIZE + packed :])]
    indices: list[int] = []
    for value in raw[AGI_HEADER_SIZE : AGI_HEADER_SIZE + packed]:
        indices.extend((value & 15, value >> 4))
    return width, height, palette, indices[:count], packed


def replace_agi_indices(raw: bytes, indices: list[int], packed: int) -> bytes:
    out = bytearray(raw)
    payload = bytes(
        (indices[index] & 15) | ((indices[index + 1] & 15) << 4)
        for index in range(0, len(indices), 2)
    )
    if len(payload) != packed:
        raise ValueError("AGI packed pixel size changed")
    out[AGI_HEADER_SIZE : AGI_HEADER_SIZE + packed] = payload
    return bytes(out)


def fit_agi_block(
    raw: bytes, magic: int, capacity: int, initial_block: bytes
) -> tuple[bytes, bytes, list[dict]]:
    """Reduce only near-colour antialiasing levels until the block fits.

    The canvas, palette, transparency, text geometry, and block start remain
    unchanged.  Candidate palette merges are scored against the desired
    translated pixels, so the smallest weighted RGBA error is chosen for each
    byte reduction.
    """
    _width, _height, palette, desired, packed = agi_indices(raw)
    transparent = min(range(len(palette)), key=lambda index: palette[index][3])
    current = list(desired)
    merges: list[dict] = []

    def encoded(values: list[int]) -> tuple[bytes, bytes]:
        rebuilt = replace_agi_indices(raw, values, packed)
        return rebuilt, encode_block(rebuilt, magic)

    rebuilt, block = raw, initial_block
    while len(block) > capacity:
        used = sorted(set(current) - {transparent})
        # The optimal compressor is deliberately expensive.  Rank all palette
        # pairs with the fast greedy compressor first, then run the optimal
        # compressor only for the most promising low-error candidates.
        fast_baseline = len(ikusa_lz.compress(replace_agi_indices(raw, current, packed)))
        ranked: list[tuple[float, int, int, int, list[int]]] = []
        for source in used:
            count = current.count(source)
            for target in used:
                if source == target:
                    continue
                trial = [target if value == source else value for value in current]
                trial_raw = replace_agi_indices(raw, trial, packed)
                fast_size = len(ikusa_lz.compress(trial_raw))
                saved = fast_baseline - fast_size
                if saved <= 0:
                    continue
                colour_error = sum(
                    (palette[source][channel] - palette[target][channel]) ** 2
                    for channel in range(3)
                ) + 2 * (palette[source][3] - palette[target][3]) ** 2
                weighted_error = colour_error * count
                ranked.append((weighted_error / saved, weighted_error, source, target, trial))
        if not ranked:
            raise ValueError(f"cannot fit translated AGI block into {capacity} bytes")
        candidates: list[tuple[float, int, int, int, bytes, bytes, list[int]]] = []
        for score, error, source, target, trial in sorted(ranked)[:3]:
            trial_raw, trial_block = encoded(trial)
            if len(trial_block) < len(block):
                candidates.append(
                    (score, error, source, target, trial_raw, trial_block, trial)
                )
        if not candidates:
            raise ValueError(f"fast candidates did not reduce optimal block below {len(block)} bytes")
        _score, error, source, target, rebuilt, block, current = min(
            candidates, key=lambda item: (item[0], item[1], len(item[5]))
        )
        merges.append(
            {
                "source_palette_index": source,
                "target_palette_index": target,
                "weighted_rgba_error": error,
                "compressed_size": len(block),
            }
        )
    return rebuilt, block, merges


def repair_fsts_only(iso_path: Path, report_path: Path) -> None:
    """Repair stale FSTS compressed sizes on an already-patched ISO.

    Older runtime-patch reports contain the exact translated stream length and
    decoded raw SHA for every runtime image.  Reuse those facts so a repaired
    ISO does not need to recompress all 62 resources just to fix the lookup
    table.
    """
    report = json.loads(report_path.read_text(encoding="utf-8"))
    resources = report.get("resources", [])
    if not resources:
        raise SystemExit("runtime image report has no resources to repair")

    image = bytearray(iso_path.read_bytes())
    mini_file, current_bytes = mini_container(image)
    current = bytearray(current_bytes)
    entries = iter_fsts_entries(current)
    by_offset: dict[int, list[dict]] = {}
    for entry in entries:
        by_offset.setdefault(int(entry["absolute_offset"]), []).append(entry)

    repaired = 0
    augmented: list[dict] = []
    for item in resources:
        runtime_offset = int(item["runtime_offset"])
        raw_size = int(item["raw_size"])
        original_size = int(item["compressed_original"])
        translated_size = int(item["compressed_translated"])
        matches = by_offset.get(runtime_offset, [])
        if len(matches) != 1:
            raise SystemExit(
                f"expected one FSTS record at {runtime_offset:#x}, got {len(matches)}"
            )
        entry = matches[0]
        record = int(entry["record"])
        resource_id, relative_offset, table_raw, table_compressed = struct.unpack_from(
            "<4I", current, record
        )
        if table_raw != raw_size:
            raise SystemExit(
                f"FSTS raw size mismatch at {runtime_offset:#x}: {table_raw}!={raw_size}"
            )
        if table_compressed not in (original_size, translated_size):
            raise SystemExit(
                f"FSTS compressed size mismatch at {runtime_offset:#x}: "
                f"{table_compressed} not in ({original_size}, {translated_size})"
            )

        decoded, consumed = ikusa_lz.decompress(current, runtime_offset)
        if len(decoded) != raw_size or consumed != translated_size:
            raise SystemExit(
                f"runtime stream mismatch at {runtime_offset:#x}: "
                f"raw={len(decoded)}/{raw_size} compressed={consumed}/{translated_size}"
            )
        expected_sha = item.get("raw_sha256")
        actual_sha = hashlib.sha256(decoded).hexdigest()
        if expected_sha and actual_sha != expected_sha:
            raise SystemExit(
                f"runtime raw SHA mismatch at {runtime_offset:#x}: "
                f"{actual_sha}!={expected_sha}"
            )

        if table_compressed != translated_size:
            struct.pack_into("<I", current, record + 12, translated_size)
            repaired += 1
        augmented.append(
            {
                **item,
                "fsts_base": int(entry["base"]),
                "fsts_record": record,
                "fsts_resource_id": resource_id,
                "fsts_compressed_before": table_compressed,
                "fsts_compressed_after": translated_size,
            }
        )

    # Verify every repaired FSTS record against the actual stream length.
    for item in augmented:
        record = int(item["fsts_record"])
        runtime_offset = int(item["runtime_offset"])
        translated_size = int(item["compressed_translated"])
        _resource_id, _relative, raw_size, compressed_size = struct.unpack_from(
            "<4I", current, record
        )
        decoded, consumed = ikusa_lz.decompress(current, runtime_offset)
        if raw_size != len(decoded) or compressed_size != consumed or consumed != translated_size:
            raise SystemExit(
                f"FSTS repair verification failed at {runtime_offset:#x}: "
                f"table=({raw_size},{compressed_size}) actual=({len(decoded)},{consumed})"
            )

    begin = mini_file.extent * builder.SECTOR
    image[begin : begin + mini_file.size] = current
    iso_path.write_bytes(image)

    repaired_report = {
        **report,
        "schema": "galaxy-angel-mini-runtime-image-patch/v2",
        "fsts_records_patched": repaired,
        "resources": augmented,
    }
    report_path.write_text(
        json.dumps(repaired_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"MINI FSTS runtime repair: resources={len(augmented)} "
        f"records_patched={repaired}",
        flush=True,
    )


def patch_runtime(
    original_iso: Path,
    iso_path: Path,
    mini_patch_report: Path,
    report_path: Path,
    seed_runtime_iso: Path | None = None,
    reuse_fit_report: Path | None = None,
    recipe_only: bool = False,
    preserve_pixels: bool = False,
) -> None:
    original_image = original_iso.read_bytes()
    _original_file, original = mini_container(original_image)
    del original_image
    original_nodes, _strings, original_paths = parse_named_nodes(original)
    original_fsts = iter_fsts_entries(original)

    seed_runtime: bytes | None = None
    seed_nodes = None
    seed_paths = None
    seed_fsts_by_offset: dict[int, dict] = {}
    if seed_runtime_iso is not None:
        seed_image = seed_runtime_iso.read_bytes()
        _seed_file, seed_runtime = mini_container(seed_image)
        seed_nodes, _seed_strings, seed_paths = parse_named_nodes(seed_runtime)
        seed_fsts_by_offset = {
            int(entry["absolute_offset"]): entry
            for entry in iter_fsts_entries(seed_runtime)
        }

    reusable_merges: dict[str, list[dict]] = {}
    if reuse_fit_report is not None and reuse_fit_report.is_file():
        prior = json.loads(reuse_fit_report.read_text(encoding="utf-8"))
        reusable_merges = {
            str(item.get("source_path")): list(item.get("palette_merges") or [])
            for item in prior.get("resources", [])
            if item.get("source_path") and item.get("palette_merges")
        }

    image = bytearray(iso_path.read_bytes())
    mini_file, current_bytes = mini_container(image)
    current = bytearray(current_bytes)
    current_nodes, _strings, current_paths = parse_named_nodes(current)

    patch_report = json.loads(mini_patch_report.read_text(encoding="utf-8"))
    if recipe_only:
        targets = [RECIPE_PATH]
    else:
        targets = [item["source_path"] for item in patch_report["resources"]]
        targets.append(RECIPE_PATH)

    # Resolve each target's FSTS record and physical runtime slot before writing
    # anything.  In preserve-pixels builds, translated streams that outgrow
    # their original fixed slot may exchange physical slots with another target
    # in the same FSTS table.  Only the record's relative_offset changes; the
    # translated compressed bytes and decoded pixels remain byte-for-byte the
    # canonical named resource.
    runtime_meta: dict[str, dict] = {}
    for source_path in targets:
        original_node = original_nodes[original_paths[source_path]]
        current_node = current_nodes[current_paths[source_path]]
        _kind, _name, _children, original_offset, original_raw_size, original_size = original_node
        _kind, _name, _children, _current_offset, current_raw_size, current_size = current_node
        if original_raw_size != current_raw_size:
            raise SystemExit(f"MINI raw size changed for {source_path}")
        original_block = original[original_offset : original_offset + original_size]
        runtime_matches = [
            entry
            for entry in original_fsts
            if entry["absolute_offset"] != original_offset
            and entry["raw_size"] == original_raw_size
            and entry["compressed_size"] == original_size
            and original[
                entry["absolute_offset"] : entry["absolute_offset"] + original_size
            ] == original_block
        ]
        if len(runtime_matches) != 1:
            raise SystemExit(
                f"expected one FSTS runtime copy for {source_path}, "
                f"got {[entry['absolute_offset'] for entry in runtime_matches]}"
            )
        runtime_meta[source_path] = {
            "original_node": original_node,
            "current_node": current_node,
            "original_block": original_block,
            "runtime_entry": runtime_matches[0],
            "capacity": aligned(original_size),
            "translated_size": current_size,
        }

    slot_owner = {source_path: source_path for source_path in targets}
    if preserve_pixels:
        grouped: dict[int, list[str]] = {}
        for source_path, meta in runtime_meta.items():
            base = int(meta["runtime_entry"]["base"])
            grouped.setdefault(base, []).append(source_path)
        for base, group in grouped.items():
            overflow = sorted(
                (
                    source_path
                    for source_path in group
                    if int(runtime_meta[source_path]["translated_size"])
                    > int(runtime_meta[source_path]["capacity"])
                ),
                key=lambda source_path: (
                    int(runtime_meta[source_path]["translated_size"])
                    - int(runtime_meta[source_path]["capacity"])
                ),
                reverse=True,
            )
            used: set[str] = set()
            for source_path in overflow:
                if source_path in used:
                    continue
                source_size = int(runtime_meta[source_path]["translated_size"])
                source_capacity = int(runtime_meta[source_path]["capacity"])
                candidates = [
                    donor
                    for donor in group
                    if donor not in used
                    and donor != source_path
                    and int(runtime_meta[donor]["translated_size"])
                    <= int(runtime_meta[donor]["capacity"])
                    and int(runtime_meta[donor]["capacity"]) >= source_size
                    and int(runtime_meta[donor]["translated_size"]) <= source_capacity
                ]
                if not candidates:
                    continue
                donor = min(
                    candidates,
                    key=lambda path: (
                        int(runtime_meta[path]["capacity"]),
                        int(runtime_meta[path]["translated_size"]),
                        path,
                    ),
                )
                slot_owner[source_path] = donor
                slot_owner[donor] = source_path
                used.update((source_path, donor))
                print(
                    f"MINI lossless runtime slot swap base={base:#x}: "
                    f"{source_path} ({source_size}/{source_capacity}) <-> "
                    f"{donor} ({runtime_meta[donor]['translated_size']}/"
                    f"{runtime_meta[donor]['capacity']})",
                    flush=True,
                )
        unresolved = [
            source_path
            for source_path in targets
            if int(runtime_meta[source_path]["translated_size"])
            > int(runtime_meta[slot_owner[source_path]]["capacity"])
        ]
        if unresolved:
            details = [
                (
                    source_path,
                    int(runtime_meta[source_path]["translated_size"]),
                    int(runtime_meta[slot_owner[source_path]]["capacity"]),
                )
                for source_path in unresolved
            ]
            raise SystemExit(
                f"lossless MINI runtime slot assignment failed: {details}"
            )

    results: list[dict] = []
    for source_path in targets:
        meta = runtime_meta[source_path]
        original_node = meta["original_node"]
        current_node = meta["current_node"]
        original_block = meta["original_block"]
        runtime_entry = meta["runtime_entry"]
        slot_source_path = slot_owner[source_path]
        slot_meta = runtime_meta[slot_source_path]
        slot_entry = slot_meta["runtime_entry"]
        _kind, _name, _children, original_offset, original_raw_size, original_size = original_node
        _kind, _name, _children, current_offset, current_raw_size, current_size = current_node
        fsts_record = int(runtime_entry["record"])
        fsts_base = int(runtime_entry["base"])
        resource_id = int(runtime_entry["resource_id"])
        original_relative_offset = int(runtime_entry["relative_offset"])
        runtime_offset = int(slot_entry["absolute_offset"])
        if int(slot_entry["base"]) != fsts_base:
            raise SystemExit(
                f"MINI runtime slot crossed FSTS tables: {source_path} -> {slot_source_path}"
            )
        relative_offset = runtime_offset - fsts_base
        capacity = int(slot_meta["capacity"])

        translated_raw = read_resource(current, current_offset, current_raw_size, current_size)
        magic = struct.unpack_from("<I", original_block, 0)[0]
        # The named MINI image pass has already produced and round-tripped the
        # canonical translated block.  Reuse those exact bytes instead of
        # recompressing every large TAG resource a second time.
        translated_block = bytes(current[current_offset : current_offset + current_size])
        if struct.unpack_from("<I", translated_block, 0)[0] != magic:
            raise SystemExit(f"MINI runtime codec changed: {source_path}")
        fitted_raw = translated_raw
        merges: list[dict] = []
        if len(translated_block) > capacity and preserve_pixels:
            raise SystemExit(
                f"{source_path}: lossless runtime slot assignment left "
                f"{len(translated_block)} bytes in a {capacity}-byte slot"
            )

        if len(translated_block) > capacity:
            reused_seed = False
            prior_merges = reusable_merges.get(source_path, [])
            if prior_merges:
                try:
                    _width, _height, _palette, desired, packed = agi_indices(translated_raw)
                    reused_indices = list(desired)
                    for merge in prior_merges:
                        source = int(merge["source_palette_index"])
                        target = int(merge["target_palette_index"])
                        reused_indices = [
                            target if value == source else value
                            for value in reused_indices
                        ]
                    candidate_raw = replace_agi_indices(translated_raw, reused_indices, packed)
                    candidate_block = encode_block(candidate_raw, magic)
                    if len(candidate_block) <= capacity:
                        fitted_raw = candidate_raw
                        translated_block = candidate_block
                        merges = [
                            {**merge, "reused_palette_merge": True}
                            for merge in prior_merges
                        ]
                        reused_seed = True
                except (KeyError, TypeError, ValueError):
                    reused_seed = False
            if not reused_seed and seed_runtime is not None and seed_nodes is not None and seed_paths is not None:
                seed_index = seed_paths.get(source_path)
                seed_entry = seed_fsts_by_offset.get(runtime_offset)
                if seed_index is not None and seed_entry is not None:
                    (
                        seed_kind,
                        _seed_name,
                        _seed_children,
                        seed_named_offset,
                        seed_named_raw_size,
                        seed_named_compressed_size,
                    ) = seed_nodes[seed_index]
                    if seed_kind == 0 and seed_named_raw_size == current_raw_size:
                        seed_named_raw = read_resource(
                            seed_runtime,
                            seed_named_offset,
                            seed_named_raw_size,
                            seed_named_compressed_size,
                        )
                        seed_runtime_raw, seed_runtime_consumed = ikusa_lz.decompress(
                            seed_runtime, runtime_offset
                        )
                        if (
                            seed_named_raw == translated_raw
                            and seed_runtime_consumed <= capacity
                            and int(seed_entry["raw_size"]) == len(seed_runtime_raw)
                            and int(seed_entry["compressed_size"]) == seed_runtime_consumed
                        ):
                            fitted_raw = seed_runtime_raw
                            translated_block = bytes(
                                seed_runtime[
                                    runtime_offset : runtime_offset + seed_runtime_consumed
                                ]
                            )
                            merges = [
                                {
                                    "reused_verified_seed": True,
                                    "seed_iso": str(seed_runtime_iso),
                                    "compressed_size": seed_runtime_consumed,
                                }
                            ]
                            reused_seed = True
            if not reused_seed:
                try:
                    fitted_raw, translated_block, merges = fit_agi_block(
                        translated_raw, magic, capacity, translated_block
                    )
                except ValueError as exc:
                    raise SystemExit(f"{source_path}: {exc}") from exc

        current_record = struct.unpack_from("<4I", current, fsts_record)
        if (
            current_record[0] != resource_id
            or current_record[1] not in (original_relative_offset, relative_offset)
            or current_record[2] != original_raw_size
        ):
            raise SystemExit(
                f"MINI FSTS record identity changed for {source_path}: {current_record}"
            )
        if current_record[3] not in (original_size, len(translated_block)):
            try:
                current_runtime_raw, current_runtime_consumed = ikusa_lz.decompress(
                    current, runtime_offset
                )
            except (IndexError, struct.error, ValueError) as exc:
                raise SystemExit(
                    f"MINI FSTS compressed size changed unexpectedly for {source_path}: "
                    f"{current_record[3]} not in ({original_size}, {len(translated_block)})"
                ) from exc
            if (
                len(current_runtime_raw) != original_raw_size
                or current_runtime_consumed != current_record[3]
            ):
                raise SystemExit(
                    f"MINI existing runtime stream is inconsistent for {source_path}: "
                    f"table={current_record[3]} actual={current_runtime_consumed}"
                )

        expected_block = translated_block
        already_patched = (
            bytes(current[runtime_offset : runtime_offset + len(expected_block)])
            == expected_block
        )
        if already_patched:
            if current[
                runtime_offset + len(expected_block) : runtime_offset + capacity
            ] != bytes(capacity - len(expected_block)):
                raise SystemExit(f"MINI runtime alignment padding changed: {source_path}")
        else:
            current[runtime_offset : runtime_offset + capacity] = bytes(capacity)
            current[runtime_offset : runtime_offset + len(expected_block)] = expected_block
        # FSTS is the actual runtime lookup table.  Preserve-pixels builds may
        # reassign a stream to another existing slot in the same FSTS table, so
        # both the relative offset and compressed-size fields must follow the
        # physical stream selected above.
        struct.pack_into("<I", current, fsts_record + 4, relative_offset)
        struct.pack_into("<I", current, fsts_record + 12, len(expected_block))

        check, consumed = ikusa_lz.decompress(current, runtime_offset)
        if check != fitted_raw or consumed != len(expected_block):
            raise SystemExit(f"MINI runtime round-trip failed: {source_path}")
        verify_record = struct.unpack_from("<4I", current, fsts_record)
        if verify_record != (
            resource_id,
            relative_offset,
            len(fitted_raw),
            len(expected_block),
        ):
            raise SystemExit(
                f"MINI FSTS runtime record verification failed for {source_path}: "
                f"{verify_record}"
            )
        results.append(
            {
                "source_path": source_path,
                "runtime_offset": runtime_offset,
                "original_runtime_offset": int(runtime_entry["absolute_offset"]),
                "slot_source_path": slot_source_path,
                "slot_reassigned": slot_source_path != source_path,
                "raw_size": len(fitted_raw),
                "compressed_original": original_size,
                "slot_capacity": capacity,
                "compressed_translated": len(expected_block),
                "fsts_base": fsts_base,
                "fsts_record": fsts_record,
                "fsts_resource_id": resource_id,
                "fsts_relative_before": current_record[1],
                "fsts_relative_after": relative_offset,
                "fsts_compressed_before": current_record[3],
                "fsts_compressed_after": len(expected_block),
                "already_patched": already_patched,
                "palette_merges": merges,
                "raw_sha256": hashlib.sha256(fitted_raw).hexdigest(),
            }
        )
        print(
            f"patched MINI runtime {source_path}: {original_size}->{len(translated_block)}"
            f"/{capacity} slot={slot_source_path} merges={len(merges)}",
            flush=True,
        )

    # The Japanese runtime blocks must no longer occur anywhere in MINI.DAT.
    remaining = []
    for source_path in targets:
        node = original_nodes[original_paths[source_path]]
        block = original[node[3] : node[3] + node[5]]
        if current.find(block) >= 0:
            remaining.append(source_path)
    if remaining:
        raise SystemExit(f"Japanese MINI runtime blocks remain: {remaining}")

    begin = mini_file.extent * builder.SECTOR
    image[begin : begin + mini_file.size] = current
    iso_path.write_bytes(image)

    report = {
        "schema": "galaxy-angel-mini-runtime-image-patch/v2",
        "patched_resources": len(results),
        "fsts_records_patched": sum(
            item["fsts_compressed_before"] != item["fsts_compressed_after"]
            for item in results
        ),
        "palette_fitted_resources": sum(bool(item["palette_merges"]) for item in results),
        "lossless_slot_reassigned_resources": sum(
            bool(item.get("slot_reassigned")) for item in results
        ),
        "fsts_offsets_reassigned": sum(
            item["fsts_relative_before"] != item["fsts_relative_after"]
            for item in results
        ),
        "remaining_japanese_blocks": 0,
        "resources": results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"MINI runtime image patch: resources={len(results)} "
        f"FSTS={report['fsts_records_patched']} "
        f"palette_fitted={report['palette_fitted_resources']} "
        f"lossless_slot_reassigned={report['lossless_slot_reassigned_resources']} "
        f"remaining_japanese=0",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--mini-patch-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--seed-runtime-iso",
        type=Path,
        help="Reuse a previously verified runtime stream only when its named translated raw matches the current target",
    )
    parser.add_argument(
        "--reuse-fit-report",
        type=Path,
        help="Reuse only prior palette-merge decisions, then recompress with the current codec",
    )
    parser.add_argument(
        "--repair-fsts-only",
        action="store_true",
        help="Repair stale FSTS compressed sizes using an existing runtime report",
    )
    parser.add_argument(
        "--recipe-only",
        action="store_true",
        help="Patch only mini/mini00/resipi.agi and its FSTS runtime copy",
    )
    parser.add_argument("--preserve-pixels", action="store_true")
    args = parser.parse_args()
    if args.repair_fsts_only:
        repair_fsts_only(args.iso, args.report)
    else:
        patch_runtime(
            args.original_iso,
            args.iso,
            args.mini_patch_report,
            args.report,
            seed_runtime_iso=args.seed_runtime_iso,
            reuse_fit_report=args.reuse_fit_report,
            recipe_only=args.recipe_only,
            preserve_pixels=args.preserve_pixels,
        )


if __name__ == "__main__":
    main()
