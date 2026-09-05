#!/usr/bin/env python3
"""Verify the 1:1 localized Galaxy Angel UI PNG set."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
GAME_ROOT = ROOT / "work" / "galaxy_angel"
IMAGE_ROOT = GAME_ROOT / "assets" / "image_extraction"
DEFAULT_SOURCE = IMAGE_ROOT / "japanese_images" / "png"
DEFAULT_OUTPUT = IMAGE_ROOT / "japanese_images" / "translated_png"
DEFAULT_BASELINE = IMAGE_ROOT / "GADAT032" / "png"
DEFAULT_REPORT = IMAGE_ROOT / "japanese_images" / "localized_verification.json"
DEFAULT_RUNTIME_AUDIT = IMAGE_ROOT / "japanese_images" / "runtime_audit.json"
ALLOWED_UNCHANGED = {
    "pobtn19.png", "pobtn19b.png", "pobtn19f.png", "pobtn19l.png",
    "pobtn25.png", "pobtn25b.png", "pobtn25f.png", "pobtn25l.png",
    "pxttl67.png",
}


def rgba_digest(path: Path) -> tuple[tuple[int, int], str, str]:
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        return rgba.size, rgba.mode, hashlib.sha256(rgba.tobytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--runtime-audit", type=Path, default=DEFAULT_RUNTIME_AUDIT)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    source_names = {path.name for path in args.source.glob("*.png")}
    output_names = {path.name for path in args.output.glob("*.png")}
    if source_names != output_names:
        raise SystemExit(
            f"file set mismatch: missing={sorted(source_names - output_names)}, "
            f"extra={sorted(output_names - source_names)}"
        )
    manifest_path = args.manifest or args.source.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_names = {
        Path(item["png"]).name for item in manifest if item.get("png")
    }
    if source_names != expected_names:
        raise SystemExit(
            f"source/manifest mismatch: missing={sorted(expected_names - source_names)}, "
            f"extra={sorted(source_names - expected_names)}"
        )

    rows = []
    source_changed = []
    unchanged_output = []
    for name in sorted(source_names):
        source_path = args.source / name
        output_path = args.output / name
        baseline_path = args.baseline / name
        if not baseline_path.is_file():
            raise SystemExit(f"baseline missing: {baseline_path}")

        source_size, _, source_hash = rgba_digest(source_path)
        baseline_size, _, baseline_hash = rgba_digest(baseline_path)
        output_size, output_mode, output_hash = rgba_digest(output_path)
        if source_size != baseline_size or source_hash != baseline_hash:
            source_changed.append(name)
        if output_size != source_size:
            raise SystemExit(f"size changed: {name}: {output_size} != {source_size}")
        with Image.open(output_path) as image:
            if image.mode != "RGBA":
                raise SystemExit(f"output is not RGBA: {name}: {image.mode}")
            output_rgba = image.convert("RGBA")
        with Image.open(source_path) as image:
            source_rgba = image.convert("RGBA")

        source_alpha = source_rgba.getchannel("A")
        output_alpha = output_rgba.getchannel("A")
        source_extrema = source_alpha.getextrema()
        output_extrema = output_alpha.getextrema()
        if source_extrema[0] == 0 and output_extrema[0] != 0:
            raise SystemExit(f"transparent background lost: {name}")
        if source_extrema[0] > 0 and output_extrema[0] == 0:
            raise SystemExit(f"unexpected transparent pixels introduced: {name}")
        for point in ((0, 0), (source_size[0] - 1, 0), (0, source_size[1] - 1), (source_size[0] - 1, source_size[1] - 1)):
            if source_rgba.getpixel(point)[3] == 0 and output_rgba.getpixel(point)[3] != 0:
                raise SystemExit(f"transparent corner replaced by backdrop: {name} at {point}")

        new_opaque_black = 0
        for source_pixel, output_pixel in zip(source_rgba.get_flattened_data(), output_rgba.get_flattened_data()):
            if source_pixel[3] == 0 and output_pixel[3] > 240 and max(output_pixel[:3]) < 4:
                new_opaque_black += 1
        if new_opaque_black:
            raise SystemExit(f"opaque black backdrop introduced: {name}: {new_opaque_black} pixels")

        changed = output_hash != source_hash
        if not changed:
            unchanged_output.append(name)
        rows.append(
            {
                "filename": name,
                "width": source_size[0],
                "height": source_size[1],
                "mode": output_mode,
                "source_alpha": list(source_extrema),
                "output_alpha": list(output_extrema),
                "changed": changed,
                "source_rgba_sha256": source_hash,
                "output_rgba_sha256": output_hash,
            }
        )

    if source_changed:
        raise SystemExit(f"original working PNGs differ from baseline: {source_changed}")
    unexpected_unchanged = sorted(set(unchanged_output) - ALLOWED_UNCHANGED)
    missing_unchanged = sorted(ALLOWED_UNCHANGED - set(unchanged_output))
    if unexpected_unchanged or missing_unchanged:
        raise SystemExit(
            f"unexpected unchanged set: unexpected={unexpected_unchanged}, "
            f"expected-but-changed={missing_unchanged}"
        )

    runtime_data = json.loads(args.runtime_audit.read_text(encoding="utf-8"))
    shared_slots: dict[int, list[str]] = {}
    for entry in runtime_data["entries"]:
        for slot in entry["runtime_slots"]:
            shared_slots.setdefault(slot["iso_offset"], []).append(entry["name"])
    checked_shared_slots = 0
    for offset, names in shared_slots.items():
        if len(names) < 2:
            continue
        checked_shared_slots += 1
        hashes = set()
        for tex_name in names:
            png_name = Path(tex_name).with_suffix(".png").name
            hashes.add(rgba_digest(args.output / png_name)[2])
        if len(hashes) != 1:
            raise SystemExit(f"shared runtime block conflict at {offset:#x}: {names}")

    report = {
        "schema": "galaxy-angel-localized-ui-verification/v1",
        "count": len(rows),
        "changed": sum(row["changed"] for row in rows),
        "unchanged_ui_only": unchanged_output,
        "originals_match_baseline": True,
        "all_sizes_match": True,
        "all_outputs_rgba": True,
        "transparent_corners_preserved": True,
        "opaque_black_backdrops_introduced": 0,
        "shared_runtime_slots_verified": checked_shared_slots,
        "images": rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"verified {len(rows)} localized PNGs; changed={report['changed']}; "
        f"unchanged_ui_only={len(unchanged_output)}; originals_match_baseline=yes"
    )
    print(args.report)


if __name__ == "__main__":
    main()
