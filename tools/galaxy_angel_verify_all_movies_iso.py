from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SECTOR = 2048


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for start, end in sorted(ranges):
        if start >= end:
            continue
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b) for a, b in out]


def hash_range(path: Path, start: int, end: int) -> str:
    h = hashlib.sha256()
    remaining = end - start
    with path.open("rb") as f:
        f.seek(start)
        while remaining:
            chunk = f.read(min(16 * 1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"unexpected EOF in {path} at {f.tell()}")
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def both_u32(data: bytes, off: int) -> int:
    le = int.from_bytes(data[off : off + 4], "little")
    be = int.from_bytes(data[off + 4 : off + 8], "big")
    if le != be:
        raise ValueError(f"both-endian mismatch at 0x{off:x}: {le} != {be}")
    return le


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify that the all-movie ISO patch changed only intended movie/ISO9660 ranges.")
    ap.add_argument("--source", type=Path, default=Path("build/Galaxy Angel (Korean).iso"))
    ap.add_argument("--output", type=Path, default=Path("build/Galaxy Angel (Korean)_SUBTITLED_MOVIES.iso"))
    ap.add_argument("--patch-report", type=Path, default=Path("build/all_movies_iso_patch.json"))
    ap.add_argument("--report", type=Path, default=Path("build/all_movies_iso_verify.json"))
    args = ap.parse_args()

    patch = json.loads(args.patch_report.read_text(encoding="utf-8"))
    source_size = args.source.stat().st_size
    output_size = args.output.stat().st_size
    source_sha256 = hash_range(args.source, 0, source_size)
    output_sha256 = hash_range(args.output, 0, output_size)
    expected_source_sha256 = patch.get("source_iso_sha256")
    expected_output_sha256 = patch.get("output_iso_sha256")
    if not expected_source_sha256 or source_sha256 != expected_source_sha256:
        raise ValueError(
            f"movie patch report does not belong to the current source ISO: "
            f"{source_sha256} != {expected_source_sha256}"
        )
    if not expected_output_sha256 or output_sha256 != expected_output_sha256:
        raise ValueError(
            f"movie patch report does not belong to the current output ISO: "
            f"{output_sha256} != {expected_output_sha256}"
        )
    if output_size != int(patch["final_iso_bytes"]):
        raise ValueError(f"output size mismatch: {output_size} != {patch['final_iso_bytes']}")

    for rec in patch["records"]:
        replacement = Path(str(rec["replacement"]))
        subtitle = Path(str(rec["subtitle"]))
        if not replacement.is_file():
            raise ValueError(f"movie replacement is missing: {replacement}")
        if not subtitle.is_file():
            raise ValueError(f"movie subtitle source is missing: {subtitle}")
        replacement_sha256 = hash_range(replacement, 0, replacement.stat().st_size)
        subtitle_sha256 = hash_range(subtitle, 0, subtitle.stat().st_size)
        if replacement_sha256 != rec["replacement_sha256"]:
            raise ValueError(
                f"movie replacement changed after ISO assembly: {replacement.name}: "
                f"{replacement_sha256} != {rec['replacement_sha256']}"
            )
        if subtitle_sha256 != rec["subtitle_sha256"]:
            raise ValueError(
                f"subtitle changed after movie assembly: {subtitle.name}: "
                f"{subtitle_sha256} != {rec['subtitle_sha256']}"
            )
        if subtitle.stat().st_mtime_ns > replacement.stat().st_mtime_ns:
            raise ValueError(
                f"subtitle is newer than the PSS: {subtitle.name}; rebuild the movie"
            )

    allowed: list[tuple[int, int]] = []
    # ISO9660 primary volume descriptor's both-endian volume-space-size field.
    allowed.append((16 * SECTOR + 80, 16 * SECTOR + 88))
    for rec in patch["records"]:
        ro = int(rec["record_offset"])
        allowed.append((ro + 2, ro + 18))
        if not bool(rec["relocated"]):
            start = int(rec["new_extent"]) * SECTOR
            end = start + int(rec["new_sectors"]) * SECTOR
            allowed.append((start, end))
    allowed = merge_ranges(allowed)

    unchanged_ranges: list[dict[str, object]] = []
    cursor = 0
    for start, end in allowed:
        start = min(start, source_size)
        end = min(end, source_size)
        if cursor < start:
            src_hash = hash_range(args.source, cursor, start)
            out_hash = hash_range(args.output, cursor, start)
            if src_hash != out_hash:
                raise ValueError(f"unexpected change outside allowed ranges: 0x{cursor:x}-0x{start:x}")
            unchanged_ranges.append({"start": cursor, "end": start, "sha256": src_hash})
        cursor = max(cursor, end)
    if cursor < source_size:
        src_hash = hash_range(args.source, cursor, source_size)
        out_hash = hash_range(args.output, cursor, source_size)
        if src_hash != out_hash:
            raise ValueError(f"unexpected change outside allowed ranges: 0x{cursor:x}-0x{source_size:x}")
        unchanged_ranges.append({"start": cursor, "end": source_size, "sha256": src_hash})

    with args.output.open("rb") as f:
        f.seek(16 * SECTOR)
        pvd = f.read(SECTOR)
    if pvd[0] != 1 or pvd[1:6] != b"CD001":
        raise ValueError("primary volume descriptor not found at sector 16")
    volume_sectors = both_u32(pvd, 80)
    if volume_sectors != output_size // SECTOR:
        raise ValueError(f"PVD volume space mismatch: {volume_sectors} != {output_size // SECTOR}")

    result = {
        "schema": "galaxy-angel-all-movies-iso-verify/v1",
        "source_size": source_size,
        "output_size": output_size,
        "pvd_volume_sectors": volume_sectors,
        "allowed_changed_ranges": allowed,
        "unchanged_ranges_verified": len(unchanged_ranges),
        "unchanged_bytes_verified": sum(int(r["end"]) - int(r["start"]) for r in unchanged_ranges),
        "outside_allowed_ranges_byte_exact": True,
        "iso9660_movie_readbacks": len(patch["verification"]),
        "iso9660_movie_readbacks_all_match": all(bool(v["readback_matches"]) for v in patch["verification"]),
        "ok": True,
    }
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
