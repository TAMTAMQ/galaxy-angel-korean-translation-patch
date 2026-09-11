from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from pathlib import Path

SECTOR = 2048
META_BYTES = 8 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_region(path: Path, offset: int, size: int) -> str:
    h = hashlib.sha256()
    remaining = size
    with path.open("rb") as f:
        f.seek(offset)
        while remaining:
            chunk = f.read(min(16 * 1024 * 1024, remaining))
            if not chunk:
                raise ValueError("unexpected EOF while hashing ISO region")
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def both_u32(data: bytes | bytearray, off: int) -> int:
    le = int.from_bytes(data[off : off + 4], "little")
    be = int.from_bytes(data[off + 4 : off + 8], "big")
    if le != be:
        raise ValueError(f"ISO9660 both-endian mismatch at 0x{off:x}: {le} != {be}")
    return le


def both_u32_bytes(value: int) -> bytes:
    return value.to_bytes(4, "little") + value.to_bytes(4, "big")


def find_record(meta: bytes, identifier: str) -> dict[str, int | str]:
    needle = identifier.encode("ascii")
    pos = meta.find(needle)
    if pos < 0:
        raise ValueError(f"ISO9660 identifier not found: {identifier}")
    if meta.find(needle, pos + 1) >= 0:
        raise ValueError(f"ISO9660 identifier is not unique: {identifier}")
    start = pos - 33
    if start < 0:
        raise ValueError(f"invalid directory record start for {identifier}")
    record_len = meta[start]
    name_len = meta[start + 32]
    if pos + name_len > start + record_len:
        raise ValueError(f"invalid directory record bounds for {identifier}")
    name = meta[pos : pos + name_len].decode("ascii")
    if name != identifier:
        raise ValueError(f"identifier mismatch: {name!r} != {identifier!r}")
    return {
        "identifier": identifier,
        "record_offset": start,
        "record_length": record_len,
        "extent": both_u32(meta, start + 2),
        "size": both_u32(meta, start + 10),
    }


def contiguous_runs(records: list[dict[str, object]]) -> list[dict[str, object]]:
    ordered = sorted(records, key=lambda r: int(r["old_extent"]))
    runs: list[dict[str, object]] = []
    for rec in ordered:
        start = int(rec["old_extent"])
        sectors = int(rec["old_sectors"])
        end = start + sectors
        if runs and start == int(runs[-1]["end"]):
            runs[-1]["end"] = end
            runs[-1]["records"].append(str(rec["name"]))  # type: ignore[index]
        else:
            runs.append({"start": start, "end": end, "records": [str(rec["name"])]})
    for run in runs:
        run["sectors"] = int(run["end"]) - int(run["start"])
    return runs


def choose_relocations(records: list[dict[str, object]], capacity_sectors: int) -> set[str]:
    total_new = sum(int(r["new_sectors"]) for r in records)
    excess = total_new - capacity_sectors
    if excess <= 0:
        return set()
    # Prefer a single smallest replacement that covers the excess. If none can,
    # greedily add the largest files until enough room is freed.
    covering = sorted(
        (r for r in records if int(r["new_sectors"]) >= excess),
        key=lambda r: int(r["new_sectors"]),
    )
    if covering:
        return {str(covering[0]["name"])}
    chosen: set[str] = set()
    freed = 0
    for rec in sorted(records, key=lambda r: int(r["new_sectors"]), reverse=True):
        chosen.add(str(rec["name"]))
        freed += int(rec["new_sectors"])
        if freed >= excess:
            break
    if freed < excess:
        raise ValueError("unable to free enough movie-pool sectors")
    return chosen


def patch_volume_space_size(f, sectors: int) -> list[int]:
    patched: list[int] = []
    for lba in range(16, 64):
        f.seek(lba * SECTOR)
        vd = f.read(SECTOR)
        if len(vd) != SECTOR:
            break
        if vd[1:6] != b"CD001":
            continue
        vd_type = vd[0]
        if vd_type == 255:
            break
        if vd_type in (1, 2):
            f.seek(lba * SECTOR + 80)
            f.write(both_u32_bytes(sectors))
            patched.append(lba)
    return patched


def main() -> int:
    ap = argparse.ArgumentParser(description="Repack all translated GADAT PSS files into the Korean Galaxy Angel ISO.")
    ap.add_argument("--iso", type=Path, default=Path("build/Galaxy Angel (Korean).iso"))
    ap.add_argument("--pss-dir", type=Path, default=Path("movie/subtitled/final"))
    ap.add_argument("--subtitles-dir", type=Path, default=Path("movie/subtitles"))
    ap.add_argument("--output", type=Path, default=Path("build/Galaxy Angel (Korean)_SUBTITLED_MOVIES.iso"))
    ap.add_argument("--report", type=Path, default=Path("build/all_movies_iso_patch.json"))
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    all_subtitle_names = {
        p.name.removesuffix(".ko.ass")
        for p in args.subtitles_dir.glob("GADAT*.ko.ass")
    }
    nonempty_names = {
        p.name.removesuffix(".ko.ass")
        for p in args.subtitles_dir.glob("GADAT*.ko.ass")
        if any(line.startswith("Dialogue:") for line in p.read_text(encoding="utf-8").splitlines())
    }
    replacements = sorted(
        p for p in args.pss_dir.glob("GADAT*.PSS")
        if p.stem in nonempty_names
    )
    if len(replacements) != len(nonempty_names):
        raise ValueError(
            f"expected {len(nonempty_names)} non-empty-subtitle PSS files, found {len(replacements)}"
        )

    with args.iso.open("rb") as f:
        meta = f.read(META_BYTES)

    records: list[dict[str, object]] = []
    for replacement in replacements:
        name = replacement.stem
        subtitle = args.subtitles_dir / f"{name}.ko.ass"
        if not subtitle.is_file():
            raise FileNotFoundError(f"{name}: subtitle source is missing: {subtitle}")
        if subtitle.stat().st_mtime_ns > replacement.stat().st_mtime_ns:
            raise ValueError(
                f"{name}: subtitle is newer than the PSS; rebuild the subtitled movie first"
            )
        rec = find_record(meta, f"{name}.PSS;1")
        old_size = int(rec["size"])
        new_size = replacement.stat().st_size
        records.append(
            {
                "name": name,
                "identifier": rec["identifier"],
                "record_offset": rec["record_offset"],
                "old_extent": rec["extent"],
                "old_size": old_size,
                "old_sectors": math.ceil(old_size / SECTOR),
                "new_size": new_size,
                "new_sectors": math.ceil(new_size / SECTOR),
                "delta_bytes": new_size - old_size,
                "replacement": str(replacement.resolve()),
                "replacement_sha256": sha256_file(replacement),
                "subtitle": str(subtitle.resolve()),
                "subtitle_sha256": sha256_file(subtitle),
            }
        )

    runs = contiguous_runs(records)
    relocated: set[str] = set()
    placements: dict[str, int] = {}
    run_plans: list[dict[str, object]] = []

    for run in runs:
        start = int(run["start"])
        end = int(run["end"])
        members = [r for r in records if start <= int(r["old_extent"]) < end]
        capacity = int(run["sectors"])
        run_relocated = choose_relocations(members, capacity)
        relocated.update(run_relocated)

        cursor = start
        for rec in sorted(members, key=lambda r: str(r["name"])):
            name = str(rec["name"])
            if name in run_relocated:
                continue
            placements[name] = cursor
            cursor += int(rec["new_sectors"])
        if cursor > end:
            raise AssertionError(f"packed movie files exceed original movie pool {start}:{end}")
        run_plans.append(
            {
                **run,
                "new_sectors_before_relocation": sum(int(r["new_sectors"]) for r in members),
                "relocated": sorted(run_relocated),
                "packed_end": cursor,
                "free_sectors_after_pack": end - cursor,
            }
        )

    source_sectors = math.ceil(args.iso.stat().st_size / SECTOR)
    append_cursor = source_sectors
    for rec in sorted(records, key=lambda r: str(r["name"])):
        name = str(rec["name"])
        if name not in relocated:
            continue
        placements[name] = append_cursor
        append_cursor += int(rec["new_sectors"])

    final_sectors = max(source_sectors, append_cursor)
    plan = {
        "schema": "galaxy-angel-all-movies-iso-patch/v2",
        "source_iso": str(args.iso.resolve()),
        "source_iso_bytes": args.iso.stat().st_size,
        "source_iso_sha256": sha256_file(args.iso),
        "source_iso_sectors": source_sectors,
        "movie_pools": run_plans,
        "old_movie_sectors": sum(int(r["old_sectors"]) for r in records),
        "new_movie_sectors": sum(int(r["new_sectors"]) for r in records),
        "movie_sector_delta": sum(int(r["new_sectors"]) for r in records) - sum(int(r["old_sectors"]) for r in records),
        "relocated_to_append": sorted(relocated),
        "append_sectors": final_sectors - source_sectors,
        "final_iso_sectors": final_sectors,
        "final_iso_bytes": final_sectors * SECTOR,
        "records": [],
    }
    for rec in records:
        item = dict(rec)
        item["new_extent"] = placements[str(rec["name"])]
        item["relocated"] = str(rec["name"]) in relocated
        plan["records"].append(item)  # type: ignore[index]

    args.report.parent.mkdir(parents=True, exist_ok=True)
    if args.plan_only:
        args.report.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.iso, args.output)
    with args.output.open("r+b") as out:
        out.truncate(final_sectors * SECTOR)
        for rec in records:
            name = str(rec["name"])
            replacement = Path(str(rec["replacement"]))
            extent = placements[name]
            out.seek(extent * SECTOR)
            with replacement.open("rb") as src:
                shutil.copyfileobj(src, out, length=16 * 1024 * 1024)
            pad = int(rec["new_sectors"]) * SECTOR - int(rec["new_size"])
            if pad:
                out.write(b"\x00" * pad)
            record_offset = int(rec["record_offset"])
            out.seek(record_offset + 2)
            out.write(both_u32_bytes(extent))
            out.seek(record_offset + 10)
            out.write(both_u32_bytes(int(rec["new_size"])))
        volume_descriptors = patch_volume_space_size(out, final_sectors)

    with args.output.open("rb") as f:
        out_meta = f.read(META_BYTES)
    verification: list[dict[str, object]] = []
    ranges: list[tuple[int, int, str]] = []
    for rec in records:
        name = str(rec["name"])
        expected_extent = placements[name]
        expected_size = int(rec["new_size"])
        current = find_record(out_meta, str(rec["identifier"]))
        if int(current["extent"]) != expected_extent or int(current["size"]) != expected_size:
            raise ValueError(f"{name}: ISO9660 directory record verification failed: {current}")
        actual_hash = sha256_region(args.output, expected_extent * SECTOR, expected_size)
        if actual_hash != rec["replacement_sha256"]:
            raise ValueError(f"{name}: readback SHA-256 mismatch")
        ranges.append((expected_extent, expected_extent + int(rec["new_sectors"]), name))
        verification.append(
            {
                "name": name,
                "extent": expected_extent,
                "size": expected_size,
                "sha256": actual_hash,
                "readback_matches": True,
            }
        )
    ranges.sort()
    for a, b in zip(ranges, ranges[1:]):
        if a[1] > b[0]:
            raise ValueError(f"movie extents overlap: {a} vs {b}")

    untouched_verification: list[dict[str, object]] = []
    for name in sorted(all_subtitle_names - nonempty_names):
        identifier = f"{name}.PSS;1"
        source_rec = find_record(meta, identifier)
        output_rec = find_record(out_meta, identifier)
        if (
            int(output_rec["extent"]) != int(source_rec["extent"])
            or int(output_rec["size"]) != int(source_rec["size"])
        ):
            raise ValueError(f"{name}: untouched ISO9660 directory record changed")
        extent = int(source_rec["extent"])
        size = int(source_rec["size"])
        source_hash = sha256_region(args.iso, extent * SECTOR, size)
        output_hash = sha256_region(args.output, extent * SECTOR, size)
        if output_hash != source_hash:
            raise ValueError(f"{name}: untouched PSS region changed")
        untouched_verification.append(
            {
                "name": name,
                "extent": extent,
                "size": size,
                "sha256": output_hash,
                "matches_source_iso": True,
            }
        )

    plan["volume_descriptors_updated"] = volume_descriptors
    plan["verification"] = verification
    plan["untouched_verification"] = untouched_verification
    plan["output_iso"] = str(args.output.resolve())
    plan["output_iso_size"] = args.output.stat().st_size
    plan["output_iso_sha256"] = sha256_file(args.output)
    args.report.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
