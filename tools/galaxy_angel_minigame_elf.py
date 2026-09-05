#!/usr/bin/env python3
"""Translate and patch Galaxy Angel mini-game text embedded in the main ELF.

The mini-games do not use GADAT001 scenario dialogue. Their visible text is a
set of NUL-terminated CP932 strings stored directly in SLPM_652.54.  This tool
keeps a generated translation cache in the build directory, so the local Gemma
model is only needed when the cache is missing or incomplete.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

import galaxy_angel_translation as scenario_text


JAPANESE_RE = re.compile(r"[ぁ-ゖァ-ヺ一-鿿]")
MINIGAME_RANGES = (
    ("sandwich", 0x224630, 0x225100),
    ("fight", 0x225D78, 0x226520),
    ("cards", 0x227940, 0x22A790),
    ("shooter", 0x22A970, 0x22D410),
    ("catch", 0x22E2B8, 0x22E7D0),
    ("disc", 0x230670, 0x2314E0),
)

GLOSSARY = (
    "タクト=택트, ミルフィー=밀피, ミルフィーユ=밀피유, "
    "ランファ=란파, ミント=민트, フォルテ=포르테, ヴァニラ=바닐라"
)

# Gemma occasionally mistranslates a short fragment because the Japanese text
# is physically split across several fixed ELF slots.  These are small,
# reviewed corrections that also keep the resulting byte strings compact.
REVIEWED_OVERRIDES = {
    0x224920: "샌드위치 재료는 토마토, 달",
    0x224948: "걀, 참치, 양상추 4종류예요.",
    0x224C80: "제가 떨어뜨린 재료가 굴러오",
    0x224CA8: "면 방향키 위를 눌러서",
    0x224EF8: "꺄아, 양, 양상추가 굴러가",
    0x224F30: "버렸어요! 택트 씨, 정말 고마워",
    0x224F58: "요!",
    0x227AA8: "『선』이 되고",
    0x227D78: "모으면 완성돼요.",
    0x228770: "점점 감이 오는데!",
    0x228F60: "다음에도 한판 부탁드려요★",
    0x22A240: "빨강·파랑·노랑 3색 카드입니다. 각 색은",
    0x22A3E8: "조건이 되면 △ 버튼으로 리치를 선언할 수",
    0x22ABB0: "가 1마리 나와. 쓰러뜨리면",
    0x22B148: "걸리긴 하지만.",
    0x22E640: "이걸로 좌우 시점을 움직여.",
    0x2308B0: "니다.",
    0x230DD0: "특수한 궤도로 날기도 한다고",
}


def request_text(endpoint: str, model: str, prompt: str, timeout: int) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 7000,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return payload["choices"][0]["message"]["content"]


def request_json(endpoint: str, model: str, prompt: str, timeout: int) -> list[dict]:
    content = request_text(endpoint, model, prompt, timeout)
    start = content.find("[")
    end = content.rfind("]")
    if start < 0 or end < start:
        raise ValueError("Gemma response did not contain a JSON array")
    result = json.loads(content[start : end + 1])
    if not isinstance(result, list):
        raise ValueError("Gemma JSON result is not a list")
    return result


def extract_entries(elf: bytes) -> list[dict]:
    entries: list[dict] = []
    for game, begin, end in MINIGAME_RANGES:
        cursor = begin
        while cursor < end:
            nul = elf.find(b"\0", cursor, end)
            if nul < 0:
                break
            raw = elf[cursor:nul]
            text = ""
            if 4 <= len(raw) <= 256:
                try:
                    text = raw.decode("cp932")
                except UnicodeDecodeError:
                    text = ""
            if (
                text
                and JAPANESE_RE.search(text)
                and all(ord(char) >= 0x20 or char in "\r\n\t" for char in text)
            ):
                padding_end = nul + 1
                while padding_end < len(elf) and elf[padding_end] == 0:
                    padding_end += 1
                capacity = padding_end - cursor - 1
                entries.append(
                    {
                        "game": game,
                        "offset": cursor,
                        "jp": text,
                        "original_bytes": len(raw),
                        "capacity": capacity,
                    }
                )
            cursor = nul + 1
    return entries


def load_cache(path: Path) -> dict[int, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(item["offset"], 16): scenario_text.normalize_display_punctuation(item["ko"])
        for item in payload["entries"]
    }


def save_cache(path: Path, entries: list[dict], translations: dict[int, str]) -> None:
    payload = {
        "model_generated": True,
        "entries": [
            {
                "game": item["game"],
                "offset": f"0x{item['offset']:x}",
                "jp": item["jp"],
                "capacity": item["capacity"],
                "ko": scenario_text.normalize_display_punctuation(
                    translations[item["offset"]]
                ),
            }
            for item in entries
            if item["offset"] in translations
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_translations(
    entries: list[dict], cache: dict[int, str], endpoint: str, model: str,
    timeout: int, batch_size: int, cache_path: Path | None = None,
) -> dict[int, str]:
    translations = dict(cache)
    missing = [item for item in entries if item["offset"] not in translations]
    for batch_start in range(0, len(missing), batch_size):
        batch = missing[batch_start : batch_start + batch_size]
        input_rows = [
            {
                "offset": f"0x{item['offset']:x}",
                "game": item["game"],
                "jp": item["jp"],
                "capacity_bytes": item["capacity"],
            }
            for item in batch
        ]
        prompt = (
            "Galaxy Angel PS2 미니게임에서 실제 화면에 표시되는 일본어 문자열을 "
            "자연스러운 한국어로 번역해. " + GLOSSARY + ". "
            "문자열은 ELF의 고정 슬롯에 있으므로 아주 간결하게 번역해야 한다. "
            "앞뒤 offset의 문자열이 한 문장으로 이어질 수 있으니 반드시 연속 문맥을 "
            "보고 번역하되 각 offset 문자열은 따로 유지해. ○△×□, L1/R1 같은 버튼, "
            "숫자, ☆, ♪는 보존해. 물결표는 ASCII 반각 ~가 아니라 반드시 "
            "일본어 전각 ～를 사용해. 원문 정보를 빼거나 새 내용을 만들지 마. "
            "빈 번역은 금지. 출력은 다른 설명 없이 한 줄에 한 항목씩 "
            "0xOFFSET<TAB>한국어 형식으로만 작성해. 입력:\n"
            + json.dumps(input_rows, ensure_ascii=False)
        )
        mapped: dict[int, str] = {}
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                content = request_text(endpoint, model, prompt, timeout)
                mapped = {}
                for line in content.splitlines():
                    line = line.strip().strip("`")
                    if not line.lower().startswith("0x") or "\t" not in line:
                        continue
                    key, value = line.split("\t", 1)
                    mapped[int(key.strip(), 16)] = (
                        scenario_text.normalize_display_punctuation(value.strip())
                    )
                if mapped:
                    break
            except Exception as exc:
                last_error = exc
            print(
                f"Gemma mini-game batch retry {attempt + 1}/5: "
                f"{type(last_error).__name__ if last_error else 'parse'}: {last_error or 'no TSV rows'}",
                flush=True,
            )
        if not mapped:
            raise SystemExit(f"Gemma mini-game batch failed after retries: {last_error}")
        absent = [item for item in batch if item["offset"] not in mapped or not mapped[item["offset"]]]
        if absent:
            raise SystemExit(
                "Gemma omitted mini-game offsets: "
                + ", ".join(f"0x{item['offset']:x}" for item in absent[:10])
            )
        translations.update(mapped)
        translations.update(REVIEWED_OVERRIDES)
        if cache_path is not None:
            save_cache(cache_path, entries, translations)
        print(
            f"translated mini-game strings: {min(batch_start + len(batch), len(missing))}/{len(missing)}",
            flush=True,
        )
    translations.update(REVIEWED_OVERRIDES)
    return translations


def encode_text(text: str, mapping: dict) -> bytes:
    return scenario_text.encode_scenario(text, mapping)


def shorten_overflows(
    entries: list[dict], translations: dict[int, str], mapping: dict,
    endpoint: str, model: str, timeout: int,
) -> None:
    by_offset = {item["offset"]: item for item in entries}
    for _round in range(4):
        overflow = []
        for offset, text in translations.items():
            item = by_offset.get(offset)
            if item is None:
                continue
            encoded = encode_text(text, mapping)
            if len(encoded) > item["capacity"]:
                overflow.append(
                    {
                        "offset": f"0x{offset:x}",
                        "jp": item["jp"],
                        "ko": text,
                        "capacity_bytes": item["capacity"],
                        "encoded_bytes": len(encoded),
                    }
                )
        if not overflow:
            return
        prompt = (
            "다음 Galaxy Angel 미니게임 번역은 고정 ELF 슬롯보다 길다. 의미, 말투, "
            "고유명사, 버튼 기호를 유지하면서 더 짧고 자연스러운 한국어로 줄여. "
            "한글은 대체로 2바이트이므로 capacity_bytes 안에 충분히 여유 있게 맞춰. "
            "출력은 JSON 배열만 [{\"offset\":\"0x...\",\"ko\":\"...\"}]. 입력:\n"
            + json.dumps(overflow, ensure_ascii=False)
        )
        response = request_json(endpoint, model, prompt, timeout)
        for item in response:
            if "offset" in item and "ko" in item:
                translations[int(str(item["offset"]), 16)] = (
                    scenario_text.normalize_display_punctuation(str(item["ko"]))
                )
        translations.update(REVIEWED_OVERRIDES)
    remaining = []
    for item in entries:
        encoded = encode_text(translations[item["offset"]], mapping)
        if len(encoded) > item["capacity"]:
            remaining.append(
                f"0x{item['offset']:x}:{len(encoded)}>{item['capacity']}"
            )
    if remaining:
        raise SystemExit("mini-game translated strings still overflow: " + ", ".join(remaining[:20]))


def patch_elf(input_elf: Path, output_elf: Path, cache_path: Path, map_path: Path,
              endpoint: str, model: str, timeout: int) -> None:
    original = input_elf.read_bytes()
    entries = extract_entries(original)
    translations = load_cache(cache_path)
    missing = [item for item in entries if item["offset"] not in translations]
    if missing:
        raise SystemExit(
            f"mini-game translation cache is incomplete: {len(missing)} missing; run translate first"
        )
    translations.update(REVIEWED_OVERRIDES)
    mapping = scenario_text.load_custom_map(map_path)
    shorten_overflows(entries, translations, mapping, endpoint, model, timeout)
    save_cache(cache_path, entries, translations)

    data = bytearray(original)
    patched = 0
    for item in entries:
        offset = item["offset"]
        expected = item["jp"].encode("cp932")
        if data[offset : offset + len(expected)] != expected:
            raise SystemExit(f"mini-game source mismatch at 0x{offset:x}")
        encoded = encode_text(translations[offset], mapping)
        capacity = item["capacity"]
        if len(encoded) > capacity:
            raise SystemExit(
                f"mini-game string overflow at 0x{offset:x}: {len(encoded)}>{capacity}"
            )
        data[offset : offset + capacity + 1] = bytes(capacity + 1)
        data[offset : offset + len(encoded)] = encoded
        patched += 1
    output_elf.write_bytes(data)
    print(f"patched {patched} mini-game ELF strings", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    translate = sub.add_parser("translate")
    translate.add_argument("--input-elf", type=Path, required=True)
    translate.add_argument("--cache", type=Path, required=True)
    translate.add_argument("--endpoint", default="http://127.0.0.1:1234")
    translate.add_argument("--model", default="gemma-4-26b-a4b-it-qat")
    translate.add_argument("--timeout", type=int, default=180)
    translate.add_argument("--batch-size", type=int, default=35)

    patch = sub.add_parser("patch")
    patch.add_argument("--input-elf", type=Path, required=True)
    patch.add_argument("--output-elf", type=Path, required=True)
    patch.add_argument("--cache", type=Path, required=True)
    patch.add_argument("--font-map", type=Path, required=True)
    patch.add_argument("--endpoint", default="http://127.0.0.1:1234")
    patch.add_argument("--model", default="gemma-4-26b-a4b-it-qat")
    patch.add_argument("--timeout", type=int, default=180)

    args = parser.parse_args()
    if args.command == "translate":
        data = args.input_elf.read_bytes()
        entries = extract_entries(data)
        cache = load_cache(args.cache)
        translations = generate_translations(
            entries, cache, args.endpoint, args.model, args.timeout, args.batch_size,
            args.cache,
        )
        save_cache(args.cache, entries, translations)
        print(f"mini-game translation cache ready: {len(entries)} strings", flush=True)
    else:
        patch_elf(
            args.input_elf, args.output_elf, args.cache, args.font_map,
            args.endpoint, args.model, args.timeout,
        )


if __name__ == "__main__":
    main()
