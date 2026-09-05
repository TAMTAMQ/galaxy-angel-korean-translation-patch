#!/usr/bin/env python3
"""Insert translated Galaxy Angel battle text into GADAT002 and SLGINIT."""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import os
import struct
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import galaxy_angel_build as builder
import galaxy_angel_translation as translation
import ikusa_lz


# The battle/config parser treats ASCII 0x20 as the end of a value.  U+3000
# avoids that parser delimiter, but the renderer advances it by a full CJK
# cell.  CP932 0xA0 is an otherwise-unused single-byte blank slot in this
# game's font path, so it preserves a half-width word-space without ending
# the parsed value.
BATTLE_HALF_SPACE = b"\xa0"
# The character the original indents with; see encode_battle_text.
FULL_SPACE = "　".encode("cp932")


def encode_battle_text(text: str, custom_map: dict[str, bytes]) -> bytes:
    """Encode battle text with renderer-safe punctuation and half-width spaces.

    A line must not *open* with 0xA0.  Eternal Lovers freezes on such a line -
    the renderer stops part-way through the message and the advance button then
    never fires - and this game shares the renderer, so keep an indent as the
    full-width space the original uses and drop a leading word gap.
    """
    output = bytearray()
    text = translation.normalize_display_punctuation(text)
    indent = 0
    while indent < len(text) and text[indent] == "　":
        output.extend(FULL_SPACE)
        indent += 1
    parts = text[indent:].replace("　", " ").split(" ")
    for index, part in enumerate(parts):
        if index:
            output.extend(BATTLE_HALF_SPACE)
        output.extend(translation.encode_text(part, custom_map))
    while output[:1] == BATTLE_HALF_SPACE:
        del output[0]
    return bytes(output)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fit_lines(text: str, count: int) -> list[str]:
    lines = text.rstrip("\r\n").splitlines()
    if len(lines) == count:
        return lines
    flattened = " ".join(part.strip() for part in lines if part.strip())
    if count == 1:
        return [flattened]
    result: list[str] = []
    remaining = flattened
    for index in range(count - 1):
        slots = count - index
        ideal = max(1, (len(remaining) + slots - 1) // slots)
        spaces = [position for position, char in enumerate(remaining) if char == " "]
        split = min(spaces, key=lambda position: abs(position - ideal)) if spaces else ideal
        split = max(1, min(split, len(remaining)))
        result.append(remaining[:split].rstrip())
        remaining = remaining[split:].lstrip()
    result.append(remaining)
    return result


def collect_assets(assets: Path, gadat002: bytes | None = None) -> tuple[
    dict[int, list[dict]],
    dict[int, list[dict]],
    dict[int, set[int]],
    dict[str, set[int]],
]:
    battle_by_block: dict[int, list[dict]] = {}
    runtime_copies: dict[int, set[int]] = defaultdict(set)
    for path in sorted((assets / "battle" / "segments").glob("*.json")):
        payload = load_json(path)
        offset = int(payload["block_offset"])
        units = payload["units"]
        battle_by_block[offset] = units
        for unit in units:
            for copy in unit.get("context", {}).get("runtime_copies", []):
                if copy.get("container") == "SLGINIT":
                    runtime_copies[offset].add(int(copy["block_offset"]))

    remaining_by_block: dict[int, list[dict]] = defaultdict(list)
    runtime_occurrences: dict[str, set[int]] = defaultdict(set)
    remaining_path = assets / "remaining" / "by_container" / "GADAT002.DAT.json"
    remaining_units = load_json(remaining_path).get("units", [])
    translated_by_value: dict[bytes, dict] = {}
    for unit in remaining_units:
        if not unit.get("use_translation") or not unit.get("translation"):
            continue
        value = unit["original"].rstrip("\r\n").encode("cp932")
        previous = translated_by_value.get(value)
        if previous and previous["translation"] != unit["translation"]:
            raise SystemExit(
                f"conflicting remaining translations: {unit['original']!r}"
            )
        translated_by_value.setdefault(value, unit)
        for occurrence in unit.get("occurrences", []):
            if occurrence.get("file") == "GADAT002.DAT":
                remaining_by_block[int(occurrence["block_offset"])].append(
                    {**unit, "occurrence": occurrence}
                )
            elif occurrence.get("file") in ("SLGINIT.DAT", "SLGRES.DAT"):
                runtime_occurrences[occurrence["file"]].add(
                    int(occurrence["block_offset"])
                )

    # The original coverage inventory omitted repeated values in a number of
    # GADAT002 configuration blocks (for example, only 5 of 12 occurrences of
    # ・敵艦隊の全滅 were recorded).  Discover additional exact field-value
    # matches across every block.  Matching only a complete line or the value
    # after '=' avoids replacing short Japanese fragments inside other text.
    if gadat002 is not None:
        known = {
            (offset, int(item["occurrence"]["line"]), item["id"])
            for offset, items in remaining_by_block.items()
            for item in items
        }
        discovered = 0
        for offset in sorted(builder.records(gadat002)):
            try:
                raw, _consumed = ikusa_lz.decompress(gadat002, offset)
            except Exception:
                continue
            battle_ranges = [
                (int(start), int(start) + int(size))
                for battle_unit in battle_by_block.get(offset, [])
                for start, size in zip(
                    battle_unit.get("context", {}).get("raw_offsets", []),
                    battle_unit.get("context", {}).get("byte_lengths", []),
                    strict=True,
                )
            ]
            cursor = 0
            for line_number, line in enumerate(raw.splitlines(keepends=True), 1):
                body = line.rstrip(b"\r\n\t ")
                separator = body.find(b"=")
                value_start = cursor + separator + 1 if separator >= 0 else cursor
                value = body[separator + 1 :] if separator >= 0 else body
                value_end = value_start + len(value)
                cursor += len(line)
                unit = translated_by_value.get(value)
                if unit is None:
                    continue
                if any(
                    value_start < battle_end and battle_start < value_end
                    for battle_start, battle_end in battle_ranges
                ):
                    continue
                key = (offset, line_number, unit["id"])
                if key in known:
                    continue
                occurrence = {
                    "file": "GADAT002.DAT",
                    "block_offset": offset,
                    "line": line_number,
                    "section": "AUTO_EXACT_FIELD",
                    "coverage": "auto_discovered_exact_field_value",
                }
                remaining_by_block[offset].append(
                    {**unit, "occurrence": occurrence}
                )
                known.add(key)
                discovered += 1
        print(f"discovered {discovered} additional translated GADAT002 field values")
    return (
        battle_by_block,
        dict(remaining_by_block),
        runtime_copies,
        dict(runtime_occurrences),
    )


def line_ranges(raw: bytes) -> list[tuple[int, int]]:
    ranges = []
    cursor = 0
    for line in raw.splitlines(keepends=True):
        body_end = cursor + len(line.rstrip(b"\r\n"))
        ranges.append((cursor, body_end))
        cursor += len(line)
    return ranges


def rebuild_raw(
    raw: bytes,
    battle_units: list[dict],
    remaining_units: list[dict],
    custom_map: dict[str, bytes],
) -> tuple[bytes, int, int]:
    replacements: list[tuple[int, int, bytes, str]] = []
    battle_count = 0
    for unit in battle_units:
        if not unit.get("use_translation") or not unit.get("translation"):
            continue
        context = unit["context"]
        offsets = context["raw_offsets"]
        lengths = context["byte_lengths"]
        original_lines = unit["original"].rstrip("\r\n").splitlines()
        translated_lines = fit_lines(unit["translation"], len(offsets))
        if len(original_lines) != len(offsets) or len(translated_lines) != len(offsets):
            raise SystemExit(f"battle field count mismatch: {unit['id']}")
        for start, size, original, rendered in zip(
            offsets, lengths, original_lines, translated_lines, strict=True
        ):
            old = raw[start : start + size]
            if old.decode("cp932", errors="surrogateescape") != original:
                raise SystemExit(f"battle source mismatch: {unit['id']} at {start:#x}")
            replacements.append(
                (
                    start,
                    start + size,
                    encode_battle_text(rendered, custom_map),
                    unit["id"],
                )
            )
        battle_count += 1

    ranges = line_ranges(raw)
    remaining_count = 0
    for unit in remaining_units:
        occurrence = unit["occurrence"]
        line_number = int(occurrence["line"])
        if not 1 <= line_number <= len(ranges):
            raise SystemExit(f"remaining line outside block: {unit['id']} line {line_number}")
        start, end = ranges[line_number - 1]
        old_value = unit["original"].rstrip("\r\n").encode("cp932")
        position = raw.find(old_value, start, end)
        if position < 0:
            raise SystemExit(f"remaining source mismatch: {unit['id']} line {line_number}")
        rendered = " ".join(unit["translation"].rstrip("\r\n").splitlines())
        replacements.append(
            (
                position,
                position + len(old_value),
                encode_battle_text(rendered, custom_map),
                unit["id"],
            )
        )
        remaining_count += 1

    replacements.sort(key=lambda item: (item[0], item[1]))
    for left, right in zip(replacements, replacements[1:]):
        if left[1] > right[0]:
            raise SystemExit(f"overlapping text replacements: {left[3]} / {right[3]}")
    rebuilt = bytearray(raw)
    for start, end, rendered, _unit_id in reversed(replacements):
        rebuilt[start:end] = rendered
    return bytes(rebuilt), battle_count, remaining_count


def compress_raw(raw: bytes) -> bytes:
    return ikusa_lz.compress(raw)


def build_replacements(
    image: mmap.mmap,
    files: dict[str, builder.IsoFile],
    assets: Path,
    custom_map: dict[str, bytes],
    cache_dir: Path,
) -> tuple[
    dict[int, tuple[bytes, int]],
    dict[int, set[int]],
    dict[str, set[int]],
    dict[int, bytes],
    dict[bytes, int],
    int,
    int,
]:
    item = builder.resolve_iso_file(files, "GADAT002")
    begin = item.extent * builder.SECTOR
    container = image[begin : begin + item.size]
    battle, remaining, runtime_copies, runtime_occurrences = collect_assets(
        assets, container
    )
    rebuilt_by_offset: dict[int, bytes] = {}
    raw_by_offset: dict[int, bytes] = {}
    source_by_hash: dict[bytes, int] = {}
    battle_count = remaining_count = 0
    for offset in sorted(set(battle) | set(remaining)):
        raw, _consumed = ikusa_lz.decompress(container, offset)
        digest = hashlib.sha256(raw).digest()
        previous = source_by_hash.setdefault(digest, offset)
        rebuilt, applied_battle, applied_remaining = rebuild_raw(
            raw, battle.get(offset, []), remaining.get(offset, []), custom_map
        )
        if previous != offset and rebuilt_by_offset[previous] != rebuilt:
            raise SystemExit(f"ambiguous GADAT002 source hash: {previous:#x}/{offset:#x}")
        rebuilt_by_offset[offset] = rebuilt
        raw_by_offset[offset] = rebuilt
        battle_count += applied_battle
        remaining_count += applied_remaining
    cache_dir.mkdir(parents=True, exist_ok=True)
    compressed_by_digest: dict[str, bytes] = {}
    pending: dict[str, bytes] = {}
    for rebuilt in rebuilt_by_offset.values():
        digest = hashlib.sha256(rebuilt).hexdigest()
        cache_path = cache_dir / f"v{ikusa_lz.COMPRESSOR_VERSION}_{digest}.bin"
        if cache_path.exists():
            compressed_by_digest[digest] = cache_path.read_bytes()
        else:
            pending.setdefault(digest, rebuilt)
    if pending:
        workers = min(4, os.cpu_count() or 1)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for number, (digest, compressed) in enumerate(
                zip(pending, pool.map(compress_raw, pending.values()), strict=True), 1
            ):
                compressed_by_digest[digest] = compressed
                (cache_dir / f"v{ikusa_lz.COMPRESSOR_VERSION}_{digest}.bin").write_bytes(compressed)
                if number % 10 == 0 or number == len(pending):
                    print(f"compressed battle blocks: {number}/{len(pending)}", flush=True)
    replacements: dict[int, tuple[bytes, int]] = {}
    for offset, rebuilt in rebuilt_by_offset.items():
        digest = hashlib.sha256(rebuilt).hexdigest()
        compressed = compressed_by_digest[digest]
        decoded, consumed = ikusa_lz.decompress(compressed)
        if decoded != rebuilt or consumed != len(compressed):
            raise SystemExit(f"battle compressor round-trip failed: {offset:#x}")
        replacements[offset] = (compressed, len(rebuilt))
    return (
        replacements,
        runtime_copies,
        runtime_occurrences,
        raw_by_offset,
        source_by_hash,
        battle_count,
        remaining_count,
    )


def patch_container(
    image: mmap.mmap,
    files: dict[str, builder.IsoFile],
    replacements: dict[int, tuple[bytes, int]],
    stem: str = "GADAT002",
) -> tuple[int, int, int, int]:
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    original = bytearray(image[begin : begin + item.size])
    rebuilt = bytearray(original)
    recs = builder.records(original)
    offsets = sorted(recs)
    append_cursor = builder.align(len(rebuilt), builder.SECTOR)
    updated: dict[int, tuple[int, int, int]] = {}
    relocated = 0
    for old_offset, (compressed, raw_size) in sorted(replacements.items()):
        record, _old_raw, _old_compressed = recs[old_offset]
        next_offsets = [value for value in offsets if value > old_offset]
        slot_end = next_offsets[0] if next_offsets else len(original)
        if old_offset + len(compressed) <= slot_end:
            rebuilt[old_offset:slot_end] = bytes(slot_end - old_offset)
            rebuilt[old_offset:old_offset + len(compressed)] = compressed
            new_offset = old_offset
        else:
            if len(rebuilt) < append_cursor:
                rebuilt.extend(bytes(append_cursor - len(rebuilt)))
            new_offset = append_cursor
            rebuilt.extend(compressed)
            append_cursor = builder.align(len(rebuilt), builder.SECTOR)
            relocated += 1
        struct.pack_into("<III", rebuilt, record, new_offset, raw_size, len(compressed))
        updated[record] = (new_offset, raw_size, len(compressed))

    required = builder.align(len(rebuilt), builder.SECTOR)
    rebuilt.extend(bytes(required - len(rebuilt)))
    redirected = 0
    if required <= item.size:
        rebuilt.extend(bytes(item.size - len(rebuilt)))
        image[begin : begin + item.size] = rebuilt
    else:
        old_image_size = len(image)
        new_begin = builder.align(old_image_size, builder.SECTOR)
        image.resize(new_begin + len(rebuilt))
        if new_begin > old_image_size:
            image[old_image_size:new_begin] = bytes(new_begin - old_image_size)
        image[new_begin:new_begin + len(rebuilt)] = rebuilt
        legacy = bytearray(rebuilt[: item.size])
        logical_size = item.size
        for record, (offset, raw_size, compressed_size) in list(updated.items()):
            if offset + compressed_size <= item.size:
                continue
            backing_offset = new_begin + offset - begin
            if backing_offset + compressed_size > 0xFFFFFFFF:
                raise SystemExit(f"{stem} backing offset exceeds unsigned 32-bit")
            struct.pack_into(
                "<III", legacy, record, backing_offset, raw_size, compressed_size
            )
            updated[record] = (backing_offset, raw_size, compressed_size)
            logical_size = max(logical_size, backing_offset + compressed_size)
            redirected += 1
        image[begin : begin + item.size] = legacy
        builder.patch_iso_file_record(image, item, item.extent, logical_size)
    file_id, idx_patched = builder.patch_central_idx_records(image, files, recs, updated)
    return relocated, redirected, idx_patched, file_id


def assert_pidx_replacements_fit_original_slots(
    image: mmap.mmap,
    files: dict[str, builder.IsoFile],
    stem: str,
    replacements: dict[int, tuple[bytes, int]],
) -> None:
    """Refuse non-battle PIDX relocation so original runtime routing stays intact."""
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    container = image[begin : begin + item.size]
    recs = builder.records(container)
    offsets = sorted(recs)
    for offset, (compressed, _raw_size) in sorted(replacements.items()):
        if offset not in recs:
            raise SystemExit(f"{stem} PIDX record missing: {offset:#x}")
        next_offset = min((value for value in offsets if value > offset), default=len(container))
        capacity = next_offset - offset
        if len(compressed) > capacity:
            raise SystemExit(
                f"{stem} non-battle translation would relocate original block "
                f"{offset:#x}: {len(compressed)}>{capacity}"
            )


def fsts_runtime_records(data: bytes | mmap.mmap) -> dict[int, tuple[int, int, int, int]]:
    """Map SLGINIT stream offsets to (FSTS base, record, raw size, comp size)."""
    result = {}
    cursor = 0
    while True:
        base = data.find(b"FSTS", cursor)
        if base < 0:
            return result
        cursor = base + 4
        if base + 32 > len(data):
            continue
        _magic, count, header_size, table_size = struct.unpack_from("<4I", data, base)
        if header_size != 32 or table_size != 32 + count * 16:
            continue
        if base + table_size > len(data):
            continue
        for index in range(count):
            record = base + 32 + index * 16
            _resource_id, offset, raw_size, compressed_size = struct.unpack_from(
                "<4I", data, record
            )
            result[base + offset] = (base, record, raw_size, compressed_size)


def map_runtime_occurrences(
    image: bytes | mmap.mmap,
    files: dict[str, builder.IsoFile],
    occurrences: dict[str, set[int]],
    source_by_hash: dict[bytes, int],
) -> dict[str, dict[int, set[int]]]:
    mapped: dict[str, dict[int, set[int]]] = {}
    for file_name, offsets in sorted(occurrences.items()):
        stem = file_name.removesuffix(".DAT")
        item = builder.resolve_iso_file(files, stem)
        begin = item.extent * builder.SECTOR
        by_source: dict[int, set[int]] = defaultdict(set)
        for offset in sorted(offsets):
            raw, _consumed = ikusa_lz.decompress(image, begin + offset)
            source_offset = source_by_hash.get(hashlib.sha256(raw).digest())
            if source_offset is None:
                raise SystemExit(
                    f"{stem} runtime block has no GADAT002 source: {offset:#x}"
                )
            by_source[source_offset].add(offset)
        mapped[stem] = dict(by_source)

    # Also discover every FSTS copy whose decompressed source hash matches a
    # translated GADAT002 block.  The old occurrence inventory missed repeated
    # stage/config blocks, so relying on its offsets alone leaves visible
    # Japanese strings in otherwise translated screens.
    for stem in ("SLGINIT", "SLGRES"):
        item = builder.resolve_iso_file(files, stem)
        begin = item.extent * builder.SECTOR
        data = image[begin : begin + item.size]
        by_source = mapped.setdefault(stem, {})
        for offset in fsts_runtime_records(data):
            try:
                raw, _consumed = ikusa_lz.decompress(data, offset)
            except Exception:
                continue
            source_offset = source_by_hash.get(hashlib.sha256(raw).digest())
            if source_offset is not None:
                by_source.setdefault(source_offset, set()).add(offset)
    return mapped


def patch_runtime_copies(
    image: mmap.mmap,
    files: dict[str, builder.IsoFile],
    stem: str,
    replacements: dict[int, tuple[bytes, int]],
    runtime_copies: dict[int, set[int]],
    cache_dir: Path,
) -> tuple[int, int]:
    runtime = builder.resolve_iso_file(files, stem)
    begin = runtime.extent * builder.SECTOR
    runtime_data = image[begin : begin + runtime.size]
    records = fsts_runtime_records(runtime_data)
    targets_by_bank: dict[int, list[tuple[int, int, int, bytes]]] = defaultdict(list)
    for source_offset, copies in sorted(runtime_copies.items()):
        compressed, raw_size = replacements[source_offset]
        for copy_offset in sorted(copies):
            if copy_offset not in records:
                raise SystemExit(f"{stem} FSTS record missing: {copy_offset:#x}")
            fsts_base, record, old_raw_size, _old_compressed = records[copy_offset]
            if old_raw_size <= 0:
                raise SystemExit(f"invalid {stem} raw size: {copy_offset:#x}")
            targets_by_bank[fsts_base].append(
                (copy_offset, record, raw_size, compressed)
            )

    bank_bases = sorted({item[0] for item in records.values()})
    total_targets = total_unique = 0
    for fsts_base, targets in sorted(targets_by_bank.items()):
        _magic, count, header_size, table_size = struct.unpack_from(
            "<4I", runtime_data, fsts_base
        )
        if header_size != 32 or table_size != 32 + count * 16:
            raise SystemExit(f"invalid {stem} FSTS table at {fsts_base:#x}")
        replacement_by_record = {
            record: (raw_size, compressed)
            for _copy_offset, record, raw_size, compressed in targets
        }
        entries = []
        for index in range(count):
            record = fsts_base + 32 + index * 16
            _resource_id, old_offset, old_raw_size, old_compressed_size = (
                struct.unpack_from("<4I", runtime_data, record)
            )
            if record in replacement_by_record:
                raw_size, compressed = replacement_by_record[record]
            else:
                raw_size = old_raw_size
                compressed = runtime_data[
                    fsts_base + old_offset :
                    fsts_base + old_offset + old_compressed_size
                ]
            entries.append((record, raw_size, compressed))

        original_offsets = [
            struct.unpack_from("<I", runtime_data, record + 4)[0]
            for record, _raw_size, _compressed in entries
        ]
        data_start = min(original_offsets)
        bank_index = bank_bases.index(fsts_base)
        bank_end = (
            bank_bases[bank_index + 1]
            if bank_index + 1 < len(bank_bases)
            else runtime.size
        )
        capacity = bank_end - fsts_base

        def layout(current_entries):
            unique_streams: dict[bytes, bytes] = {}
            for _record, _raw_size, compressed in current_entries:
                unique_streams.setdefault(
                    hashlib.sha256(compressed).digest(), compressed
                )
            positions: dict[bytes, int] = {}
            end = data_start
            for digest, compressed in unique_streams.items():
                end = builder.align(end, 16)
                positions[digest] = end
                end += len(compressed)
            return unique_streams, positions, end

        unique, allocated, cursor = layout(entries)
        if cursor > capacity:
            translated_digests = {
                hashlib.sha256(compressed).digest()
                for _raw_size, compressed in replacement_by_record.values()
            }
            candidates = sorted(
                unique,
                key=lambda digest: (
                    digest not in translated_digests,
                    abs(len(unique[digest]) - 6000),
                ),
            )
            for digest in candidates:
                compressed = unique[digest]
                raw, consumed = ikusa_lz.decompress(compressed)
                if consumed != len(compressed):
                    continue
                cache_path = cache_dir / (
                    f"optimal_v{ikusa_lz.COMPRESSOR_VERSION}_" + hashlib.sha256(raw).hexdigest() + ".bin"
                )
                if cache_path.exists():
                    optimal = cache_path.read_bytes()
                else:
                    optimal = ikusa_lz.compress_optimal(raw)
                    cache_path.write_bytes(optimal)
                if len(optimal) >= len(compressed):
                    continue
                entries = [
                    (
                        record,
                        raw_size,
                        optimal
                        if hashlib.sha256(entry_compressed).digest() == digest
                        else entry_compressed,
                    )
                    for record, raw_size, entry_compressed in entries
                ]
                unique, allocated, cursor = layout(entries)
                print(
                    f"optimized {stem} FSTS stream {len(compressed)}->{len(optimal)}; "
                    f"bank overflow now {max(0, cursor-capacity)}",
                    flush=True,
                )
                if cursor <= capacity:
                    break
        if cursor > capacity:
            raise SystemExit(
                f"repacked {stem} FSTS bank overflow {fsts_base:#x}: "
                f"{cursor}>{capacity} (+{cursor-capacity})"
            )

        image[
            begin + fsts_base + data_start : begin + fsts_base + capacity
        ] = bytes(capacity - data_start)
        for digest, offset in allocated.items():
            compressed = unique[digest]
            image[
                begin + fsts_base + offset : begin + fsts_base + offset + len(compressed)
            ] = compressed
        for record, raw_size, compressed in entries:
            offset = allocated[hashlib.sha256(compressed).digest()]
            struct.pack_into(
                "<III", image, begin + record + 4,
                offset, raw_size, len(compressed),
            )
        total_targets += len(targets)
        total_unique += len(unique)
    return total_targets, total_unique


def patch_runtime_copies_in_place(
    image: mmap.mmap,
    files: dict[str, builder.IsoFile],
    stem: str,
    replacements: dict[int, tuple[bytes, int]],
) -> tuple[int, int]:
    """Patch non-battle FSTS streams without changing any original stream offset.

    MISC/ADV contain event-routing data whose consumers can bypass the FSTS
    table and rely on original stream positions. Repacking an entire FSTS bank
    made room for longer Korean text but could send a room/choice to the wrong
    event. For these containers we now keep every original offset unchanged.
    If a translated stream does not fit its original gap, try optimal LZ once;
    if it still does not fit, leave that block in the original Japanese form.
    """
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    data = image[begin : begin + item.size]
    records = fsts_runtime_records(data)
    bank_bases = sorted({base for base, _record, _raw, _comp in records.values()})
    patched = skipped = 0

    for target_offset, (compressed, raw_size) in sorted(replacements.items()):
        if target_offset not in records:
            raise SystemExit(f"{stem} FSTS record missing: {target_offset:#x}")
        fsts_base, _record, _old_raw, _old_comp = records[target_offset]
        bank_index = bank_bases.index(fsts_base)
        bank_end = bank_bases[bank_index + 1] if bank_index + 1 < len(bank_bases) else len(data)
        distinct_offsets = sorted(
            offset
            for offset, (base, _rec, _raw, _comp) in records.items()
            if base == fsts_base and offset > target_offset
        )
        slot_end = distinct_offsets[0] if distinct_offsets else bank_end
        capacity = slot_end - target_offset

        if len(compressed) > capacity:
            rebuilt, consumed = ikusa_lz.decompress(compressed)
            if consumed != len(compressed):
                raise SystemExit(f"{stem} replacement stream is invalid: {target_offset:#x}")
            optimal = ikusa_lz.compress_optimal(rebuilt)
            if len(optimal) < len(compressed):
                compressed = optimal
        if len(compressed) > capacity:
            print(
                f"skipping {stem} non-battle block {target_offset:#x}: "
                f"translated {len(compressed)}>{capacity}; preserving original layout",
                flush=True,
            )
            skipped += 1
            continue

        relative_offset = target_offset - fsts_base
        _magic, count, header_size, table_size = struct.unpack_from("<4I", data, fsts_base)
        if header_size != 32 or table_size != 32 + count * 16:
            raise SystemExit(f"invalid {stem} FSTS table at {fsts_base:#x}")
        matching_records = []
        for index in range(count):
            record = fsts_base + 32 + index * 16
            _resource_id, offset, _record_raw, _record_comp = struct.unpack_from(
                "<4I", data, record
            )
            if offset == relative_offset:
                matching_records.append(record)
        if not matching_records:
            raise SystemExit(f"{stem} FSTS target has no table record: {target_offset:#x}")

        image[begin + target_offset : begin + slot_end] = bytes(capacity)
        image[
            begin + target_offset : begin + target_offset + len(compressed)
        ] = compressed
        for record in matching_records:
            # Keep resource id and stream offset exactly as in the Japanese ISO.
            struct.pack_into("<II", image, begin + record + 8, raw_size, len(compressed))
        patched += 1

    return patched, skipped


def collect_nonbattle_remaining_targets(
    assets: Path,
) -> dict[str, dict[int, list[dict]]]:
    """Collect GADAT000/MISC remaining text by its actual runtime container.

    GADAT002 is handled by the battle path above. Save-title strings in the
    GADAT000 0x1B800 block are also skipped here because `galaxy_angel_speakers`
    already translates that complete block before ISO construction.
    """
    path = assets / "remaining" / "remaining_compressed_unique.json"
    targets: dict[str, dict[int, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for unit in load_json(path).get("units", []):
        if not unit.get("use_translation") or not unit.get("translation"):
            continue
        occurrences = unit.get("occurrences", [])
        if not occurrences:
            continue
        primary = occurrences[0]
        primary_file = primary.get("file")
        if primary_file not in ("GADAT000.DAT", "MISC.DAT"):
            continue
        if (
            primary_file == "GADAT000.DAT"
            and int(primary.get("block_offset", -1)) == builder.GADAT000_SAVELOAD_OFFSET
        ):
            continue
        for occurrence in occurrences:
            file_name = occurrence.get("file")
            if file_name not in ("GADAT000.DAT", "MISC.DAT", "ADV.DAT"):
                continue
            targets[file_name][int(occurrence["block_offset"])].append(
                {**unit, "occurrence": occurrence}
            )
    return {
        file_name: dict(blocks)
        for file_name, blocks in targets.items()
    }


def build_runtime_remaining_replacements(
    image: mmap.mmap,
    files: dict[str, builder.IsoFile],
    stem: str,
    by_block: dict[int, list[dict]],
    custom_map: dict[str, bytes],
    cache_dir: Path,
) -> tuple[dict[int, tuple[bytes, int]], int]:
    """Build translated replacements for one non-battle target container."""
    if not by_block:
        return {}, 0
    item = builder.resolve_iso_file(files, stem)
    begin = item.extent * builder.SECTOR
    container = image[begin : begin + item.size]
    replacements: dict[int, tuple[bytes, int]] = {}
    applied = 0
    for offset, units in sorted(by_block.items()):
        raw, _consumed = ikusa_lz.decompress(container, offset)
        rebuilt, _battle_count, remaining_count = rebuild_raw(
            raw, [], units, custom_map
        )
        digest = hashlib.sha256(rebuilt).hexdigest()
        cache_path = cache_dir / f"remaining_{stem}_v{ikusa_lz.COMPRESSOR_VERSION}_{digest}.bin"
        if cache_path.exists():
            compressed = cache_path.read_bytes()
        else:
            compressed = ikusa_lz.compress(rebuilt)
            cache_path.write_bytes(compressed)
        decoded, consumed = ikusa_lz.decompress(compressed)
        if decoded != rebuilt or consumed != len(compressed):
            raise SystemExit(
                f"remaining compressor round-trip failed: {stem} {offset:#x}"
            )
        replacements[offset] = (compressed, len(rebuilt))
        applied += remaining_count
    return replacements, applied


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--encoding-map", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    custom_map = translation.load_custom_map(args.encoding_map)
    assert custom_map is not None
    with args.iso.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
        files = builder.iso_files(image)
        cache_dir = args.cache_dir or args.iso.parent / "battle_compressed_cache"
        (
            replacements,
            battle_runtime_copies,
            runtime_occurrences,
            _raw_by_offset,
            source_by_hash,
            battle_count,
            remaining_count,
        ) = (
            build_replacements(image, files, args.assets, custom_map, cache_dir)
        )
        mapped_runtime = map_runtime_occurrences(
            image, files, runtime_occurrences, source_by_hash
        )
        slginit_copies = mapped_runtime.setdefault("SLGINIT", {})
        for source_offset, copies in battle_runtime_copies.items():
            slginit_copies.setdefault(source_offset, set()).update(copies)
        relocated, redirected, idx_patched, file_id = patch_container(
            image, files, replacements
        )
        runtime_results = {}
        for stem, copies in sorted(mapped_runtime.items()):
            runtime_results[stem] = patch_runtime_copies(
                image, files, stem, replacements, copies, cache_dir
            )

        # Apply the remaining non-battle text that lives in GADAT000/MISC and
        # their ADV/MISC runtime copies. These assets were translated but the
        # old build path only consumed the GADAT002 subset.
        nonbattle_targets = collect_nonbattle_remaining_targets(args.assets)
        nonbattle_replacements: dict[str, dict[int, tuple[bytes, int]]] = {}
        nonbattle_counts: dict[str, int] = {}
        for file_name, by_block in sorted(nonbattle_targets.items()):
            stem = file_name.removesuffix(".DAT")
            target_replacements, target_count = build_runtime_remaining_replacements(
                image, files, stem, by_block, custom_map, cache_dir
            )
            nonbattle_replacements[file_name] = target_replacements
            nonbattle_counts[file_name] = target_count

        nonbattle_pidx = {}
        gadat000_replacements = nonbattle_replacements.get("GADAT000.DAT", {})
        if gadat000_replacements:
            # GADAT000 may be translated only while every block remains at its
            # exact Japanese-ISO offset. Never relocate these non-battle tables.
            assert_pidx_replacements_fit_original_slots(
                image, files, "GADAT000", gadat000_replacements
            )
            nonbattle_pidx["GADAT000"] = patch_container(
                image, files, gadat000_replacements, "GADAT000"
            )

        nonbattle_runtime = {}
        for file_name in ("MISC.DAT", "ADV.DAT"):
            target_replacements = nonbattle_replacements.get(file_name, {})
            if not target_replacements:
                continue
            stem = file_name.removesuffix(".DAT")
            # Do not repack MISC/ADV. Their original stream positions are part
            # of event routing in practice, even when the FSTS table also names
            # the streams. Only same-offset replacements are allowed here.
            nonbattle_runtime[stem] = patch_runtime_copies_in_place(
                image, files, stem, target_replacements
            )
        final_size = builder.align(len(image), builder.SECTOR)
        if final_size > len(image):
            old_size = len(image)
            image.resize(final_size)
            image[old_size:final_size] = bytes(final_size - old_size)
        builder.patch_volume_size(image)
        image.flush()
    print(
        f"patched battle_units={battle_count} remaining_units={remaining_count} "
        f"nonbattle_remaining={nonbattle_counts} nonbattle_pidx={nonbattle_pidx} "
        f"nonbattle_runtime={nonbattle_runtime} "
        f"blocks={len(replacements)} runtime={runtime_results} "
        f"relocated={relocated} redirected={redirected} "
        f"idx_records={idx_patched} file_id={file_id}"
    )


if __name__ == "__main__":
    main()
