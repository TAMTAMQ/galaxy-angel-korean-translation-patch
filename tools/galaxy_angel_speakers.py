#!/usr/bin/env python3
"""Patch the compressed GADAT000 speaker-name table."""

from __future__ import annotations

import argparse
import json
import re
import struct
from pathlib import Path

import galaxy_angel_build as builder
import galaxy_angel_translation as translation
import ikusa_lz


ENTRY_RE = re.compile(r"(?m)^(\d{3})=([^\r\n]*)(?=\r?\n|$)")
SAVELOAD_OFFSET = 0x1B800
SAVELOAD_RE = re.compile(r"(?m)^(#\d{3}\s*=)(.*?)(\s*//.*)$")
# SAVELOAD values are parsed as whitespace-delimited fields.  A normal ASCII
# space therefore truncates Korean multi-word chapter titles (for example
# "강운의 천칭" became only "강운의").  Use the same parser-safe half-width
# blank byte used by the battle text path, but only inside translated titles.
SAVELOAD_HALF_SPACE = b"\xa0"
SAVELOAD_SPACE_MARKER = "\ue000"
SAVE_TITLE_BASES = {
    "エンジェル隊登場": "엔젤대 등장",
    "強運の天秤": "강운의 천칭",
    "占い大騒動": "점술 대소동",
    "お嬢様のユーウツ": "아가씨의 우울",
    "月夜の相談": "달밤의 상담",
    "癒しの力": "치유의 힘",
    "ラストダンス": "라스트 댄스",
    "黒い絶望": "검은 절망",
    "運の道標": "운명의 이정표",
    "扉越しの言葉": "문 너머의 말",
    "伝わる気持ち": "전해지는 마음",
    "背中の信頼": "등 뒤의 신뢰",
    "生命の行方": "생명의 행방",
    "最終決戦": "최종 결전",
    "光の天使": "빛의 천사",
}


def replace_compressed_block(
    container: bytearray, offset: int, rebuilt_raw: bytes, *, optimal: bool = False
) -> tuple[int, int]:
    raw, used = ikusa_lz.decompress(container, offset)
    if len(rebuilt_raw) > len(raw):
        raise SystemExit(f"GADAT000 block overflow at {offset:#x}: {len(rebuilt_raw)}>{len(raw)}")
    rebuilt_raw += b" " * (len(raw) - len(rebuilt_raw))
    compressed = (
        ikusa_lz.compress_optimal(rebuilt_raw)
        if optimal else ikusa_lz.compress(rebuilt_raw)
    )
    recs = builder.records(container)
    if offset not in recs:
        raise SystemExit(f"PIDX record not found for GADAT000 block {offset:#x}")
    record, _raw_size, _compressed_size = recs[offset]
    next_offset = min((item for item in recs if item > offset), default=len(container))
    if len(compressed) > next_offset - offset:
        raise SystemExit(
            f"compressed GADAT000 block overflow at {offset:#x}: "
            f"{len(compressed)}>{next_offset-offset}"
        )
    container[offset:next_offset] = compressed + bytes(next_offset - offset - len(compressed))
    struct.pack_into("<III", container, record, offset, len(rebuilt_raw), len(compressed))
    decoded, consumed = ikusa_lz.decompress(container, offset)
    if decoded != rebuilt_raw or consumed != len(compressed):
        raise SystemExit(f"GADAT000 block verification failed at {offset:#x}")
    return used, len(compressed)


def encode_saveload_text(text: str, custom_map: dict[str, bytes]) -> bytes:
    output = bytearray()
    for char in text:
        if char == SAVELOAD_SPACE_MARKER:
            output.extend(SAVELOAD_HALF_SPACE)
        else:
            output.extend(translation.encode_text(char, custom_map))
    return bytes(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--names", type=Path, required=True)
    parser.add_argument("--encoding-map", type=Path, required=True)
    args = parser.parse_args()

    spec = json.loads(args.names.read_text(encoding="utf-8"))
    names = spec["names"]
    offset = int(spec["block_offset"])
    custom_map = translation.load_custom_map(args.encoding_map)
    container = bytearray(args.input.read_bytes())
    raw, used = ikusa_lz.decompress(container, offset)
    text = raw.decode("cp932")
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        speaker_id = match.group(1)
        if speaker_id not in names:
            return match.group(0)
        seen.add(speaker_id)
        return f"{speaker_id}={names[speaker_id]}"

    rebuilt_text = ENTRY_RE.sub(replace, text)
    missing = sorted(set(names) - seen)
    if missing:
        raise SystemExit(f"speaker IDs missing from source table: {', '.join(missing)}")
    rebuilt_raw = translation.encode_text(rebuilt_text, custom_map)
    speaker_old, speaker_new = replace_compressed_block(container, offset, rebuilt_raw)

    save_raw, _ = ikusa_lz.decompress(container, SAVELOAD_OFFSET)
    save_text = save_raw.decode("cp932")
    save_seen = 0

    def replace_save_title(match: re.Match[str]) -> str:
        nonlocal save_seen
        value = match.group(2).rstrip()
        for japanese, korean in SAVE_TITLE_BASES.items():
            if not value.startswith(japanese):
                continue
            suffix = value[len(japanese):].replace("（", "(").replace("）", ")")
            suffix = suffix.replace("　", "").replace("クリア", "클리어")
            save_seen += 1
            safe_korean = korean.replace(" ", SAVELOAD_SPACE_MARKER)
            return match.group(1) + safe_korean + suffix + match.group(3)
        return match.group(0)

    rebuilt_save_text = SAVELOAD_RE.sub(replace_save_title, save_text)
    if save_seen != 105:
        raise SystemExit(f"save title count mismatch: {save_seen}/105")
    save_old, save_new = replace_compressed_block(
        container, SAVELOAD_OFFSET,
        encode_saveload_text(rebuilt_save_text, custom_map),
        optimal=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(container)
    print(
        f"patched {len(seen)} speaker names ({speaker_old}->{speaker_new}) and "
        f"{save_seen} save titles ({save_old}->{save_new})"
    )


if __name__ == "__main__":
    main()
