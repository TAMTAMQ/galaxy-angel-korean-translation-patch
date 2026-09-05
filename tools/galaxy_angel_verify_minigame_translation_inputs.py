#!/usr/bin/env python3
"""Verify existing MINI translated PNGs without regenerating them."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image


def rgba_hash(path: Path) -> tuple[str, tuple[int, int]]:
    with Image.open(path) as source:
        image = source.convert("RGBA")
        digest = hashlib.sha256()
        digest.update(image.width.to_bytes(4, "little"))
        digest.update(image.height.to_bytes(4, "little"))
        digest.update(image.tobytes())
        return digest.hexdigest(), image.size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extraction", type=Path, required=True)
    parser.add_argument("--translations", type=Path, action="append", required=True)
    parser.add_argument("--allow-extra", action="append", default=[])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    source_root = args.extraction / "png"
    translated_root = args.extraction / "translated_png"
    expected_paths: dict[str, str] = {}
    problems: list[dict] = []
    report_summaries: list[dict] = []

    for report_path in args.translations:
        document = json.loads(report_path.read_text(encoding="utf-8"))
        item_count = 0
        occurrence_count = 0
        for item in document.get("items", []):
            item_count += 1
            rel = str(item["translated_png"])
            source_rel = str(item["source_png"])
            source_path = source_root / Path(*source_rel.split("/"))
            translated_path = translated_root / Path(*rel.split("/"))

            if not source_path.is_file():
                problems.append({"path": source_rel, "error": "source missing"})
            else:
                source_hash, source_size = rgba_hash(source_path)
                expected_source_hash = str(item["original_pixel_sha256"])
                expected_size = (int(item["width"]), int(item["height"]))
                if source_hash != expected_source_hash or source_size != expected_size:
                    problems.append(
                        {
                            "path": source_rel,
                            "error": "source hash/size mismatch",
                            "actual_hash": source_hash,
                            "expected_hash": expected_source_hash,
                            "actual_size": list(source_size),
                            "expected_size": list(expected_size),
                        }
                    )

            if not translated_path.is_file():
                problems.append({"path": rel, "error": "translated PNG missing"})
                translated_hash = None
            else:
                translated_hash, translated_size = rgba_hash(translated_path)
                expected_translated_hash = str(item["translated_pixel_sha256"])
                expected_size = (int(item["width"]), int(item["height"]))
                if translated_hash != expected_translated_hash or translated_size != expected_size:
                    problems.append(
                        {
                            "path": rel,
                            "error": "translated hash/size mismatch",
                            "actual_hash": translated_hash,
                            "expected_hash": expected_translated_hash,
                            "actual_size": list(translated_size),
                            "expected_size": list(expected_size),
                        }
                    )

            for occurrence in item.get("occurrences", []):
                occurrence_count += 1
                occurrence_rel = str(occurrence["png"])
                expected_paths[occurrence_rel] = rel
                occurrence_source = source_root / Path(*occurrence_rel.split("/"))
                occurrence_output = translated_root / Path(*occurrence_rel.split("/"))
                if not occurrence_source.is_file():
                    problems.append({"path": occurrence_rel, "error": "occurrence source missing"})
                    continue
                occurrence_source_hash, occurrence_size = rgba_hash(occurrence_source)
                expected_occurrence_hash = str(occurrence["pixel_sha256"])
                expected_occurrence_size = (
                    int(occurrence["width"]),
                    int(occurrence["height"]),
                )
                if (
                    occurrence_source_hash != expected_occurrence_hash
                    or occurrence_size != expected_occurrence_size
                ):
                    problems.append(
                        {
                            "path": occurrence_rel,
                            "error": "occurrence source hash/size mismatch",
                        }
                    )
                if not occurrence_output.is_file():
                    problems.append({"path": occurrence_rel, "error": "occurrence translated PNG missing"})
                elif translated_hash is not None:
                    occurrence_output_hash, occurrence_output_size = rgba_hash(occurrence_output)
                    if occurrence_output_hash != translated_hash or occurrence_output_size != occurrence_size:
                        problems.append(
                            {
                                "path": occurrence_rel,
                                "error": "duplicate translated occurrence differs from canonical output",
                                "canonical": rel,
                            }
                        )

        report_summaries.append(
            {
                "path": str(report_path),
                "items": item_count,
                "occurrences": occurrence_count,
            }
        )

    actual_paths = {
        path.relative_to(translated_root).as_posix()
        for path in translated_root.rglob("*.png")
        if path.is_file()
    }
    allowed_extras = set(args.allow_extra)
    expected_set = set(expected_paths)
    unexpected = sorted(actual_paths - expected_set - allowed_extras)
    missing_expected = sorted(expected_set - actual_paths)
    if unexpected:
        problems.append({"error": "unexpected translated PNGs", "paths": unexpected})
    if missing_expected:
        problems.append({"error": "expected translated PNGs missing", "paths": missing_expected})

    report = {
        "schema": "galaxy-angel-mini-translation-input-verification/v1",
        "reports": report_summaries,
        "expected_occurrence_paths": len(expected_set),
        "actual_translated_pngs": len(actual_paths),
        "allowed_extra_paths": sorted(allowed_extras),
        "problem_count": len(problems),
        "problems": problems,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if problems:
        raise SystemExit(
            f"MINI translation input verification failed: {len(problems)} problems; {args.report}"
        )
    print(
        f"MINI translation inputs verified: reports={len(report_summaries)} "
        f"occurrence_paths={len(expected_set)} translated_pngs={len(actual_paths)} problems=0",
        flush=True,
    )


if __name__ == "__main__":
    main()
