"""Verify primary textures and runtime copies through stable FSTS records."""
import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

from PIL import Image

import galaxy_angel_build as builder
import ikusa_lz
from galaxy_angel_extract_minigame_images import read_resource
from galaxy_angel_gadat032 import decode_tex, named_records, pixel_hash
from galaxy_angel_patch_minigame_runtime_images import iter_fsts_entries


def digest(data):
    return hashlib.sha256(data).hexdigest()


def container(image, files, stem):
    item = builder.resolve_iso_file(files, stem)
    start = item.extent * builder.SECTOR
    return image[start:start + item.size]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-iso", type=Path, required=True)
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--image-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repair", action="store_true", help="Restore mismatched copies from current primary TEX and synchronize FSTS sizes")
    args = parser.parse_args()
    source_report = json.loads(args.image_report.read_text(encoding="utf-8"))
    result = {"primary": 0, "runtime": {}, "mismatches": [], "size_mismatches": [],
              "comparison": "current primary native TEX pixels; stable original FSTS record positions"}
    writes = []
    with args.original_iso.open("rb") as old_file, args.iso.open("r+b" if args.repair else "rb") as new_file:
        with mmap.mmap(old_file.fileno(), 0, access=mmap.ACCESS_READ) as old, mmap.mmap(new_file.fileno(), 0, access=mmap.ACCESS_WRITE if args.repair else mmap.ACCESS_READ) as new:
            old_files, new_files = builder.iso_files(old), builder.iso_files(new)
            original = container(old, old_files, "GADAT032")
            patched = container(new, new_files, "GADAT032")
            old_named = named_records(original, builder.records(original))
            new_named = named_records(patched, builder.records(patched))
            by_raw, by_pixels, raw_sizes = {}, {}, set()
            for entry in source_report["entries"]:
                name = entry["name"].lower()
                offset, _, size, compressed = old_named[name]
                old_raw = read_resource(original, offset, size, compressed)
                offset, _, size, compressed = new_named[name]
                raw = read_resource(patched, offset, size, compressed)
                expected = pixel_hash(decode_tex(raw))
                if digest(raw) != entry["raw_sha256"] or expected != entry["expected_pixel_sha256"]:
                    raise ValueError(f"primary report mismatch: {name}")
                with Image.open(entry["png"]) as png:
                    if pixel_hash(png) != entry["source_pixel_sha256"]:
                        raise ValueError(f"authority PNG changed: {name}")
                target = (name, expected, patched[offset:offset + compressed], size)
                by_raw[digest(old_raw)] = target
                by_pixels[pixel_hash(decode_tex(old_raw))] = target
                raw_sizes.add(len(old_raw))
                result["primary"] += 1
            for stem in ("SLGRES", "MISC", "SLGINIT", "ADV", "ALBUM"):
                original = container(old, old_files, stem)
                patched = container(new, new_files, stem)
                current = {item["record"]: item for item in iter_fsts_entries(patched)}
                boundaries = sorted({item["absolute_offset"] for item in current.values()} | {item["base"] for item in current.values()} | {len(patched)})
                iso_start = builder.resolve_iso_file(new_files, stem).extent * builder.SECTOR
                seen, count = set(), 0
                for item in iter_fsts_entries(original):
                    if item["raw_size"] not in raw_sizes:
                        continue
                    raw = read_resource(original, item["absolute_offset"], item["raw_size"], item["compressed_size"])
                    target = by_raw.get(digest(raw))
                    if target is None and raw.startswith(b"TEX"):
                        target = by_pixels.get(pixel_hash(decode_tex(raw)))
                    if target is None:
                        continue
                    actual_item = current[item["record"]]
                    if actual_item["resource_id"] != item["resource_id"]:
                        raise ValueError(f"stable FSTS identity changed: {stem}:{item['record']:#x}")
                    identity = actual_item["absolute_offset"]
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if struct.unpack_from("<I", patched, identity)[0] == ikusa_lz.MAGIC:
                        actual, consumed = ikusa_lz.decompress(patched, identity)
                        if len(actual) != actual_item["raw_size"] or consumed != actual_item["compressed_size"]:
                            result["size_mismatches"].append({"container": stem, "record": item["record"], "png": target[0], "declared": [actual_item["raw_size"], actual_item["compressed_size"]], "actual": [len(actual), consumed]})
                    else:
                        actual = read_resource(patched, identity, actual_item["raw_size"], actual_item["compressed_size"])
                        consumed = actual_item["compressed_size"]
                    final_size = len(actual)
                    try:
                        matches = pixel_hash(decode_tex(actual)) == target[1]
                        error = None
                    except ValueError as exc:
                        matches, error = False, str(exc)
                    if not matches:
                        result["mismatches"].append({"container": stem, "record": item["record"], "offset": identity, "png": target[0], "error": error})
                        end = next(value for value in boundaries if value > identity)
                        if len(target[2]) > end - identity:
                            raise ValueError(f"native primary copy exceeds current slot: {stem}:{identity:#x}")
                        writes.append((iso_start + identity, target[2]))
                        final_size, consumed = target[3], len(target[2])
                    if args.repair:
                        for alias in current.values():
                            if alias["absolute_offset"] == identity:
                                writes.append((iso_start + alias["record"] + 8, struct.pack("<II", final_size, consumed)))
                    count += 1
                result["runtime"][stem] = count
                print(f"{stem}: {count} live runtime copies", flush=True)
            if args.repair:
                for offset, payload in writes:
                    new[offset:offset + len(payload)] = payload
                new.flush()
                result["repair_writes"] = len(writes)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"primary": result["primary"], "runtime": result["runtime"], "pixel_mismatches": len(result["mismatches"]), "size_mismatches": len(result["size_mismatches"]), "repair": args.repair}))
    if not args.repair and (result["mismatches"] or result["size_mismatches"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
