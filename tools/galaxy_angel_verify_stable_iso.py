#!/usr/bin/env python3
"""Verify a stable-base Galaxy Angel ISO without copying its huge logical extent."""

from __future__ import annotations

import argparse
import mmap
import re
import struct
from pathlib import Path

import galaxy_angel_build as builder
import ikusa_lz


NAME = re.compile(r"^GADAT001_DAT_([0-9A-Fa-f]{8})\.txt$")
EXTERNAL_CALL = re.compile(
    rb"(?m)^<(?P<group>999[5-9])->(?P<id>ID[A-Z0-9]+)\r?$"
)
ROUTINE = re.compile(rb"(?m)^(?P<id>ID[A-Z0-9]+):\{")
LOCAL_ROUTINE_CALL = re.compile(rb"(?m)^<(?P<id>ID[A-Z0-9]+)\r?$")
LABEL_DEFINITION = re.compile(rb"(?m)^:(?P<label>[LR][A-Z0-9]+)\r?$")
LOCAL_LABEL_REFERENCE = re.compile(
    rb"(?m)(?<!-)[<>](?P<label>L[A-Z0-9]+)\r?$"
)
RAND_LABEL_REFERENCE = re.compile(rb"\\randjump\((?P<label>R[A-Z0-9]+),")
CROSS_SECTION_REFERENCE = re.compile(
    rb"(?m)^[<>](?P<section>[A-Z0-9]+)->(?P<target>(?:ID|L)[A-Z0-9]+)\r?$"
)
INDEX_VALUE = re.compile(rb"(?m)^((?:ID|[LR])[A-Z0-9]+)=(\d+)\r?$")
SELECTION_START = re.compile(rb"(?m)^\\SelStart\(")
MANAGEMENT_BLOCKS = {
    "9995": 0x13E000,
    "9996": 0x143000,
    "9997": 0x143800,
    "9998": 0x148800,
    "9999": 0x149800,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--patched-iso", type=Path, required=True)
    parser.add_argument("--built-scenario", type=Path, required=True)
    args = parser.parse_args()

    with (
        args.original_iso.open("rb") as original_stream,
        mmap.mmap(original_stream.fileno(), 0, access=mmap.ACCESS_READ) as original,
        args.patched_iso.open("rb") as patched_stream,
        mmap.mmap(patched_stream.fileno(), 0, access=mmap.ACCESS_READ) as patched,
    ):
        original_files = builder.iso_files(original)
        patched_files = builder.iso_files(patched)
        original_file = builder.resolve_iso_file(original_files, "GADAT001")
        patched_file = builder.resolve_iso_file(patched_files, "GADAT001")
        if patched_file.extent != original_file.extent:
            raise SystemExit(
                f"GADAT001 extent moved: {original_file.extent}->{patched_file.extent}"
            )

        begin = original_file.extent * builder.SECTOR
        original_header = bytearray(original[begin : begin + original_file.size])
        # All PIDX table records reside in the original physical allocation.
        patched_header = patched[begin : begin + original_file.size]
        original_records = builder.records(original_header)

        checked = 0
        redirected = 0
        scenario_calls: list[tuple[str, str, str]] = []
        cross_section_calls: list[tuple[str, str, str]] = []
        selection_blocks = 0
        local_routine_calls = 0
        local_label_references = 0
        original_exported_targets: dict[str, list[tuple[str, int]]] = {}
        built_exported_targets: dict[str, list[tuple[str, int]]] = {}
        for path in sorted(args.built_scenario.glob("GADAT001_DAT_*.txt")):
            match = NAME.match(path.name)
            if not match:
                continue
            old_offset = int(match.group(1), 16)
            record, original_raw_size, original_compressed_size = original_records[old_offset]
            original_raw, original_consumed = ikusa_lz.decompress(
                original, begin + old_offset
            )
            if (
                len(original_raw) != original_raw_size
                or original_consumed != original_compressed_size
            ):
                raise SystemExit(f"original scenario record corrupt: {path.name}")
            offset, raw_size, compressed_size = struct.unpack_from(
                "<III", patched_header, record
            )
            if offset != old_offset:
                raise SystemExit(
                    f"scenario physical offset changed from Japanese layout: "
                    f"{path.name} {old_offset:#x}->{offset:#x}"
                )
            absolute = begin + offset
            if absolute + compressed_size > len(patched):
                raise SystemExit(
                    f"record outside ISO: {path.name} {absolute}+{compressed_size}"
                )
            raw, consumed = ikusa_lz.decompress(patched, absolute)
            expected = path.read_bytes()
            if raw != expected or len(raw) != raw_size or consumed != compressed_size:
                raise SystemExit(
                    f"scenario mismatch: {path.name} raw={len(raw)}/{raw_size} "
                    f"compressed={consumed}/{compressed_size}"
                )
            redirected += int(offset >= original_file.size)
            for source_raw, target_map in (
                (original_raw, original_exported_targets),
                (raw, built_exported_targets),
            ):
                for target in builder.SCRIPT_TARGET_RE.finditer(source_raw):
                    key = (target.group(1) or target.group(2)).decode("ascii")
                    target_map.setdefault(key, []).append((path.name, target.start()))

            cross_section_calls.extend(
                (
                    match.group("section").decode("ascii"),
                    match.group("target").decode("ascii"),
                    path.name,
                )
                for match in CROSS_SECTION_REFERENCE.finditer(raw)
            )
            if old_offset not in MANAGEMENT_BLOCKS.values():
                scenario_calls.extend(
                    (match.group("group").decode(), match.group("id").decode(), path.name)
                    for match in EXTERNAL_CALL.finditer(raw)
                )
                local_targets = {
                    match.group("id").decode("ascii")
                    for match in ROUTINE.finditer(raw)
                }
                missing_local_routines = [
                    match.group("id").decode("ascii")
                    for match in LOCAL_ROUTINE_CALL.finditer(raw)
                    if match.group("id").decode("ascii") not in local_targets
                ]
                if missing_local_routines:
                    raise SystemExit(
                        f"unresolved local routines in {path.name}: "
                        f"{missing_local_routines[:10]}"
                    )
                local_routine_calls += len(LOCAL_ROUTINE_CALL.findall(raw))
                labels = {
                    match.group("label").decode("ascii")
                    for match in LABEL_DEFINITION.finditer(raw)
                }
                label_refs = [
                    match.group("label").decode("ascii")
                    for pattern in (LOCAL_LABEL_REFERENCE, RAND_LABEL_REFERENCE)
                    for match in pattern.finditer(raw)
                ]
                missing_labels = [label for label in label_refs if label not in labels]
                if missing_labels:
                    raise SystemExit(
                        f"unresolved local labels in {path.name}: {missing_labels[:10]}"
                    )
                local_label_references += len(label_refs)
            starts = len(SELECTION_START.findall(raw))
            if starts != raw.count(b"@(ss") or starts != raw.count(b"@)ss"):
                raise SystemExit(
                    f"selection structure mismatch: {path.name} "
                    f"start={starts} open={raw.count(b'@(ss')} close={raw.count(b'@)ss')}"
                )
            selection_blocks += starts
            checked += 1

        # Every choice, battle and common management group must be reachable from
        # the fixed GADAT001 base.  Validate every direct and nested external call
        # target, not just the first observed IDS3100 failure.  Management dialogue
        # may be translated, so compare it to the built scenario rather than the
        # Japanese original.
        routines: dict[tuple[str, str], bytes] = {}
        nested_calls: list[tuple[str, str, str]] = []
        for group, old_offset in MANAGEMENT_BLOCKS.items():
            if old_offset not in original_records:
                raise SystemExit(f"management record missing: {group}/{old_offset:#x}")
            record = original_records[old_offset][0]
            offset, raw_size, compressed_size = struct.unpack_from(
                "<III", patched_header, record
            )
            management, consumed = ikusa_lz.decompress(patched, begin + offset)
            built_management = (
                args.built_scenario / f"GADAT001_DAT_{old_offset:08X}.txt"
            )
            if built_management.exists():
                expected_management = built_management.read_bytes()
            else:
                expected_management, _ = ikusa_lz.decompress(
                    original, begin + old_offset
                )
            if (
                management != expected_management
                or len(management) != raw_size
                or consumed != compressed_size
            ):
                raise SystemExit(f"management group changed or corrupt: {group}")
            for match in ROUTINE.finditer(management):
                routines[(group, match.group("id").decode("ascii"))] = management
            nested_calls.extend(
                (match.group("group").decode(), match.group("id").decode(), group)
                for match in EXTERNAL_CALL.finditer(management)
            )
            local_targets = {
                match.group("id").decode("ascii")
                for match in ROUTINE.finditer(management)
            }
            missing_local_routines = [
                match.group("id").decode()
                for match in LOCAL_ROUTINE_CALL.finditer(management)
                if match.group("id").decode() not in local_targets
            ]
            if missing_local_routines:
                raise SystemExit(
                    f"unresolved local routines in {group}: {missing_local_routines[:10]}"
                )
            local_routine_calls += len(LOCAL_ROUTINE_CALL.findall(management))
            labels = {
                match.group("label").decode()
                for match in LABEL_DEFINITION.finditer(management)
            }
            label_refs = [
                match.group("label").decode()
                for pattern in (LOCAL_LABEL_REFERENCE, RAND_LABEL_REFERENCE)
                for match in pattern.finditer(management)
            ]
            missing_labels = [label for label in label_refs if label not in labels]
            if missing_labels:
                raise SystemExit(
                    f"unresolved local labels in {group}: {missing_labels[:10]}"
                )
            local_label_references += len(label_refs)
        unresolved = [
            call for call in scenario_calls + nested_calls
            if (call[0], call[1]) not in routines
        ]
        if unresolved:
            raise SystemExit(f"unresolved external calls: {unresolved[:10]}")
        group_stats = {
            group: (
                sum(call_group == group for call_group, _target, _source in scenario_calls),
                len({target for call_group, target, _source in scenario_calls if call_group == group}),
                sum(routine_group == group for routine_group, _target in routines),
            )
            for group in MANAGEMENT_BLOCKS
        }

        # Locate the original flat IDX mirror, then ensure every tuple equals
        # the final local PIDX tuple in the patched image.
        idx_original = builder.resolve_iso_file(original_files, "IDX")
        idx_patched = builder.resolve_iso_file(patched_files, "IDX")
        old_by_record = {
            record: (offset, raw_size, compressed_size)
            for offset, (record, raw_size, compressed_size) in original_records.items()
        }
        candidates: dict[int, dict[int, int]] = {}
        idx_begin = idx_original.extent * builder.SECTOR
        for relative in range(0, idx_original.size - 15, 4):
            file_id, offset, raw_size, compressed_size = struct.unpack_from(
                "<IIII", original, idx_begin + relative
            )
            if offset % builder.SECTOR or compressed_size < 8:
                continue
            for record, old_tuple in old_by_record.items():
                if (offset, raw_size, compressed_size) == old_tuple:
                    candidates.setdefault(file_id, {})[record] = relative
        file_id, positions = max(candidates.items(), key=lambda item: len(item[1]))
        if len(positions) != len(old_by_record):
            raise SystemExit(
                f"original IDX mirror incomplete: {len(positions)}/{len(old_by_record)}"
            )
        patched_idx_begin = idx_patched.extent * builder.SECTOR
        for record, relative in positions.items():
            local_tuple = struct.unpack_from("<III", patched_header, record)
            flat_tuple = struct.unpack_from("<III", patched, patched_idx_begin + relative + 4)
            if local_tuple != flat_tuple:
                raise SystemExit(
                    f"IDX/PIDX mismatch at record {record:#x}: {flat_tuple}!={local_tuple}"
                )

        # ADV keeps a compressed runtime copy of the label/IDS byte-offset
        # index and reloads it after transitions such as battle results.  It
        # must match the rebuilt GADAT001 index, or execution can resume in the
        # middle of commands (for example `oice(GA104007,3)`).
        index_record = original_records[builder.GADAT001_INDEX_OFFSET][0]
        original_index_raw, _ = ikusa_lz.decompress(
            original, begin + builder.GADAT001_INDEX_OFFSET
        )
        index_offset, _index_raw_size, _index_compressed_size = struct.unpack_from(
            "<III", patched_header, index_record
        )
        patched_index_raw, _ = ikusa_lz.decompress(patched, begin + index_offset)

        # The scenario index contains every externally addressable ID/L/R
        # target, including alphanumeric labels such as L04BL003.  Validate
        # every exported target against its translated byte position.  This is
        # intentionally broader than the original IDS/L-only check: a stale
        # index can enter the middle of a command and display fragments such as
        # `er(005)` from `\\Speaker(005)`.
        original_index_values = {
            match.group(1).decode("ascii"): int(match.group(2))
            for match in INDEX_VALUE.finditer(original_index_raw)
        }
        patched_index_values = {
            match.group(1).decode("ascii"): int(match.group(2))
            for match in INDEX_VALUE.finditer(patched_index_raw)
        }
        if original_index_values.keys() != patched_index_values.keys():
            raise SystemExit("scenario index key set changed")

        # The game's numeric parser is sensitive to leading-zero offsets.
        # A value such as `09471` can resolve as zero and restart the current
        # scenario section, so require canonical base-10 text for every index
        # entry rather than merely comparing its Python integer value.
        noncanonical_index_values = []
        for line in patched_index_raw.splitlines():
            value_match = INDEX_VALUE.fullmatch(line)
            if not value_match:
                continue
            digits = value_match.group(2)
            canonical = str(int(digits)).encode("ascii")
            if digits != canonical:
                noncanonical_index_values.append(line)
        if noncanonical_index_values:
            raise SystemExit(
                f"noncanonical scenario-index offsets: "
                f"{noncanonical_index_values[:10]}"
            )

        index_sections: dict[str, str] = {}
        current_section = ""
        for line in patched_index_raw.splitlines():
            section_match = re.fullmatch(rb"\[([A-Z0-9]+)\]", line)
            if section_match:
                current_section = section_match.group(1).decode("ascii")
                continue
            value_match = INDEX_VALUE.fullmatch(line)
            if value_match:
                index_sections[value_match.group(1).decode("ascii")] = current_section

        indexed_exported_targets = 0
        rand_index_targets = 0
        for key, old_offset in sorted(original_index_values.items()):
            candidates = [
                item
                for item in original_exported_targets.get(key, [])
                if item[1] == old_offset
            ]
            if not candidates:
                # The extraction set intentionally omits unchanged/non-dialogue
                # blocks. Their byte positions are untouched by this build.
                continue
            if len(candidates) != 1:
                raise SystemExit(
                    f"ambiguous original indexed target {key}={old_offset}: {candidates}"
                )
            source_name, _ = candidates[0]
            original_in_file = [
                position
                for name, position in original_exported_targets.get(key, [])
                if name == source_name
            ]
            occurrence_index = original_in_file.index(old_offset)
            translated = [
                position
                for name, position in built_exported_targets.get(key, [])
                if name == source_name
            ]
            if occurrence_index >= len(translated):
                raise SystemExit(
                    f"translated indexed target missing: {key}[{occurrence_index}] "
                    f"in {source_name}"
                )
            expected_offset = translated[occurrence_index]
            actual_offset = patched_index_values[key]
            if actual_offset != expected_offset:
                raise SystemExit(
                    f"stale scenario index {key}: {actual_offset}!={expected_offset} "
                    f"in {source_name}"
                )
            indexed_exported_targets += 1
            rand_index_targets += int(key.startswith("R"))

        bad_cross_section_calls = [
            (section, target, source, index_sections.get(target))
            for section, target, source in cross_section_calls
            if target not in patched_index_values or index_sections.get(target) != section
        ]
        if bad_cross_section_calls:
            raise SystemExit(
                f"invalid cross-section jumps: {bad_cross_section_calls[:10]}"
            )

        runtime_index_copies = 0
        for stem in ("ADV", "MISC"):
            original_runtime = builder.resolve_iso_file(original_files, stem)
            patched_runtime = builder.resolve_iso_file(patched_files, stem)
            original_begin = original_runtime.extent * builder.SECTOR
            patched_begin = patched_runtime.extent * builder.SECTOR
            cursor = original_begin
            original_end = original_begin + original_runtime.size
            while True:
                position = original.find(b" 3;1", cursor, original_end)
                if position < 0:
                    break
                cursor = position + 4
                try:
                    original_raw, _ = ikusa_lz.decompress(original, position)
                except (IndexError, struct.error, ValueError):
                    continue
                if original_raw != original_index_raw:
                    continue
                relative = position - original_begin
                runtime_raw, _ = ikusa_lz.decompress(patched, patched_begin + relative)
                if runtime_raw != patched_index_raw:
                    raise SystemExit(
                        f"stale runtime scenario index in {stem} at {relative:#x}"
                    )
                runtime_index_copies += 1
        if runtime_index_copies == 0:
            raise SystemExit("runtime scenario-index copy verification found no copies")

        print(
            f"verified extent={patched_file.extent} logical_size={patched_file.size} "
            f"scenarios={checked} redirected={redirected} "
            f"idx_records={len(positions)} file_id={file_id} "
            f"external_calls={len(scenario_calls)} nested_calls={len(nested_calls)} "
            f"local_calls={local_routine_calls} label_refs={local_label_references} "
            f"cross_jumps={len(cross_section_calls)} "
            f"indexed_targets={indexed_exported_targets} "
            f"selections={selection_blocks} rand_indexes={rand_index_targets} "
            f"runtime_indexes={runtime_index_copies} groups={group_stats}"
        )


if __name__ == "__main__":
    main()
