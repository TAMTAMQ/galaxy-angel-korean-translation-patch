#!/usr/bin/env python3
"""Exercise every extracted GADAT032 TEX through decode/encode/decode."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from PIL import Image

from galaxy_angel_gadat032 import decode_tex, encode_tex, pixel_hash, tex_info


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extraction", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.extraction / "manifest.json").read_text(encoding="utf-8"))
    counts: Counter[str] = Counter()
    verified = 0
    for entry in manifest:
        if not entry.get("png") or not entry["name"].lower().endswith(".tex"):
            continue
        raw = (args.extraction / "raw" / entry["name"]).read_bytes()
        info = tex_info(raw)
        png_path = args.extraction / "png" / entry["png"]
        with Image.open(png_path) as opened:
            png = opened.convert("RGBA")
        decoded = decode_tex(raw)
        if pixel_hash(decoded) != pixel_hash(png):
            raise SystemExit(f"extractor/codec decode mismatch: {entry['name']}")
        rebuilt = encode_tex(raw, png)
        if len(rebuilt) != len(raw):
            raise SystemExit(f"codec raw size changed: {entry['name']}")
        if pixel_hash(decode_tex(rebuilt)) != pixel_hash(png):
            raise SystemExit(f"codec round-trip pixel mismatch: {entry['name']}")
        counts[info.kind] += 1
        verified += 1
    summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    print(f"verified {verified} TEX codec round-trips: {summary}")


if __name__ == "__main__":
    main()
