from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

SECTOR = 2048


def both_u32(data: bytes, off: int) -> int:
    le = int.from_bytes(data[off : off + 4], "little")
    be = int.from_bytes(data[off + 4 : off + 8], "big")
    if le != be:
        raise ValueError(f"both-endian mismatch at 0x{off:x}: {le} != {be}")
    return le


def both_u32_bytes(value: int) -> bytes:
    return value.to_bytes(4, "little") + value.to_bytes(4, "big")


def find_record(metadata: bytes, identifier: str) -> dict[str, int | str]:
    needle = identifier.encode("ascii")
    pos = metadata.find(needle)
    if pos < 0:
        raise ValueError(f"ISO9660 identifier not found: {identifier}")
    if metadata.find(needle, pos + 1) >= 0:
        raise ValueError(f"ISO9660 identifier is not unique: {identifier}")
    start = pos - 33
    if start < 0:
        raise ValueError("invalid directory record offset")
    rec_len = metadata[start]
    name_len = metadata[start + 32]
    name = metadata[start + 33 : start + 33 + name_len].decode("ascii")
    if name != identifier or pos + name_len > start + rec_len:
        raise ValueError(f"invalid directory record for {identifier}")
    return {
        "record_offset": start,
        "record_length": rec_len,
        "extent": both_u32(metadata, start + 2),
        "size": both_u32(metadata, start + 10),
        "name": name,
    }


def sha256_region(path: Path, offset: int, size: int) -> str:
    h = hashlib.sha256()
    remaining = size
    with path.open("rb") as f:
        f.seek(offset)
        while remaining:
            chunk = f.read(min(16 * 1024 * 1024, remaining))
            if not chunk:
                raise ValueError("unexpected EOF while hashing region")
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Make one ISO9660 movie filename point to another movie's extent/size for playback testing.")
    ap.add_argument("--iso", type=Path, required=True)
    ap.add_argument("--target", required=True, help="Target filename to trigger, e.g. GADAT101.PSS;1")
    ap.add_argument("--source", required=True, help="Movie content to alias, e.g. GADAT132.PSS;1")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    with args.iso.open("rb") as f:
        metadata = f.read(4 * 1024 * 1024)
    target_before = find_record(metadata, args.target)
    source_rec = find_record(metadata, args.source)

    source_hash = sha256_region(args.iso, int(source_rec["extent"]) * SECTOR, int(source_rec["size"]))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.iso, args.output)
    target_off = int(target_before["record_offset"])
    with args.output.open("r+b") as f:
        f.seek(target_off + 2)
        f.write(both_u32_bytes(int(source_rec["extent"])))
        f.seek(target_off + 10)
        f.write(both_u32_bytes(int(source_rec["size"])))

    with args.output.open("rb") as f:
        out_metadata = f.read(4 * 1024 * 1024)
    target_after = find_record(out_metadata, args.target)
    source_after = find_record(out_metadata, args.source)

    if target_after["name"] != args.target or source_after["name"] != args.source:
        raise ValueError("ISO9660 identifiers changed unexpectedly")
    if int(target_after["extent"]) != int(source_rec["extent"]) or int(target_after["size"]) != int(source_rec["size"]):
        raise ValueError("target record does not alias source extent/size")
    if source_after != source_rec:
        raise ValueError("source record changed unexpectedly")

    target_hash = sha256_region(args.output, int(target_after["extent"]) * SECTOR, int(target_after["size"]))
    if target_hash != source_hash:
        raise ValueError("aliased target readback does not match source movie")

    report = {
        "schema": "galaxy-angel-movie-alias-test/v1",
        "base_iso": str(args.iso.resolve()),
        "output_iso": str(args.output.resolve()),
        "target": args.target,
        "source": args.source,
        "target_before": target_before,
        "source_record": source_rec,
        "target_after": target_after,
        "source_sha256": source_hash,
        "target_readback_sha256": target_hash,
        "readback_matches": True,
        "output_size": args.output.stat().st_size,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
