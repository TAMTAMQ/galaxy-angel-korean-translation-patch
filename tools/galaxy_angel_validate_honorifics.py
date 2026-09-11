#!/usr/bin/env python3
"""Validate Galaxy Angel names, titles, and Japanese honorific grades.

Project rule: preserve the source form instead of silently raising/lowering a
person's honorific grade in Korean.  Explicit Japanese suffixes are mapped
one-for-one, and bare ranks/roles stay bare unless the source itself contains
an honorific suffix.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Iterable


SUFFIXES = {
    "さん": "씨",
    "君": "군",
    "くん": "군",
    "ちゃん": "쨩",
    "様": "님",
    "さま": "님",
    "どの": "나리",
    "殿": "나리",
    "氏": "씨",
    "嬢": "양",
}

# Fixed honorific titles that are not suffix-grade variants but still must be
# preserved when attached to a person's name.
PERSON_TITLES = {
    "先生": "선생님",
    "陛下": "폐하",
    "閣下": "각하",
}

# Non-name appellations that deliberately carry a Japanese honorific suffix in
# dialogue.  These are not lexicalized kinship/customer words such as お父さん
# or お客様; the suffix itself is part of the speaker's address and must survive.
ADDRESS_TERMS = {
    "店員": "점원",
    "侍女": "시녀",
    "整備員": "정비사",
    "軍人": "군인",
    "患者": "환자",
    "ボーイ": "보이",
    "ハムスター": "햄스터",
    "反逆者": "반역자",
    "子クジラ": "새끼 고래",
    "宇宙ウサギ": "우주 토끼",
    "敵": "적군",
    "カップル": "커플",
    "働きバチ": "일벌",
    "王子": "왕자",
    "大統領": "대통령",
    "英雄": "영웅",
}

# Longest source forms must come first because several contain shorter titles.
TITLES = (
    ("副司令官", "부사령관"),
    ("総司令官", "총사령관"),
    ("営業部長", "영업부장"),
    ("副司令", "부사령관"),
    ("総司令", "총사령관"),
    ("司令官", "사령관"),
    ("司令", "사령관"),
    ("准将", "준장"),
    ("大将", "대장"),
    ("大佐", "대령"),
    ("少佐", "소령"),
    ("班長", "반장"),
    ("会長", "회장"),
    ("部長", "부장"),
    ("隊長", "대장"),
    ("皇子", "황자"),
)

# These contain a title substring but are ordinary compound nouns.
NON_TITLE_COMPOUNDS = (
    "司令部",
    "司令塔",
)

KOREAN_SUFFIX_FORMS = tuple(sorted(set(SUFFIXES.values()), key=len, reverse=True))


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def iter_units(path: Path) -> Iterable[tuple[str, str, str]]:
    payload = load_json(path)
    if isinstance(payload, list):
        units = payload
    elif isinstance(payload, dict) and isinstance(payload.get("units"), list):
        units = payload["units"]
    elif isinstance(payload, dict) and isinstance(payload.get("translations"), dict):
        for original, translation in payload["translations"].items():
            yield original, original, translation
        return
    else:
        return

    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        original = unit.get("original")
        translation = unit.get("translation")
        if not isinstance(original, str) or not isinstance(translation, str):
            continue
        unit_id = str(unit.get("id", f"#{index}"))
        yield unit_id, original, translation


def authority_files(asset_dir: Path) -> list[Path]:
    paths = sorted((asset_dir / "segments").glob("*.json"))
    for relative in (
        "selection_translations.json",
        "battle/battle_unique.json",
        "remaining/remaining_compressed_unique.json",
        "remaining/remaining_direct_review.json",
    ):
        path = asset_dir / relative
        if path.is_file():
            paths.append(path)
    return paths


def normalize_display(text: str) -> str:
    return re.sub(r"[ \t\r\n　]+", " ", text)


def person_honorific_form(korean_base: str, korean_suffix: str) -> str:
    # Personal names use a space for 씨/군/님/나리/양; 쨩 is attached.
    return korean_base + korean_suffix if korean_suffix == "쨩" else korean_base + " " + korean_suffix


def title_honorific_form(korean_base: str, korean_suffix: str) -> str:
    # Korean 직책+님 is lexicalized without a space (사령관님, 황자님), while
    # 씨/군/나리/양 remain separate (사령관 씨, 사령관 나리).
    return korean_base + korean_suffix if korean_suffix in {"님", "쨩"} else korean_base + " " + korean_suffix


def load_person_terms(asset_dir: Path) -> list[tuple[str, str]]:
    glossary = asset_dir / "glossary.tsv"
    if not glossary.is_file():
        return []
    with glossary.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        terms = [
            (row["원문"], row["번역"])
            for row in rows
            if "인물" in row.get("분류", "") and row.get("원문") and row.get("번역")
        ]
    # Longer names first so full names are reported before nested surnames/given names.
    return sorted(set(terms), key=lambda item: len(item[0]), reverse=True)


def validate_person_suffixes(
    path: Path,
    unit_id: str,
    original: str,
    translation: str,
    person_terms: list[tuple[str, str]],
) -> list[str]:
    problems: list[str] = []
    normalized = normalize_display(translation)

    for source_name, korean_name in person_terms:
        if source_name not in original:
            continue
        for source_suffix, korean_suffix in SUFFIXES.items():
            pattern = re.compile(
                re.escape(source_name) + r"[ \t\r\n　]*" + re.escape(source_suffix)
            )
            count = len(pattern.findall(original))
            if not count:
                continue
            expected = person_honorific_form(korean_name, korean_suffix)
            actual_count = normalized.count(expected)
            if actual_count < count:
                problems.append(
                    f"person honorific mismatch: {source_name}+{source_suffix} "
                    f"must contain {expected!r} {count} time(s), found {actual_count}"
                )

        for source_title, korean_title in PERSON_TITLES.items():
            pattern = re.compile(
                re.escape(source_name) + r"[ \t\r\n　]*" + re.escape(source_title)
            )
            count = len(pattern.findall(original))
            if not count:
                continue
            expected = korean_name + " " + korean_title
            actual_count = normalized.count(expected)
            if actual_count < count:
                problems.append(
                    f"person title mismatch: {source_name}+{source_title} "
                    f"must contain {expected!r} {count} time(s), found {actual_count}"
                )

    return [f"{path}:{unit_id}: {problem}" for problem in problems]


def title_form_present(text: str, korean_title: str, form: str) -> bool:
    if korean_title == "사령관":
        return re.search(r"(?<![부총])" + re.escape(form), text) is not None
    return form in text


def bare_title_present(text: str, korean_title: str) -> bool:
    if korean_title == "사령관":
        return re.search(r"(?<![부총])사령관", text) is not None
    return korean_title in text


def validate_address_terms(path: Path, unit_id: str, original: str, translation: str) -> list[str]:
    problems: list[str] = []
    normalized = normalize_display(translation)
    for source_base, korean_base in ADDRESS_TERMS.items():
        if source_base not in original:
            continue
        for source_suffix, korean_suffix in SUFFIXES.items():
            pattern = re.compile(
                re.escape(source_base) + r"[ \t\r\n　]*" + re.escape(source_suffix)
            )
            count = len(pattern.findall(original))
            if not count:
                continue
            expected = title_honorific_form(korean_base, korean_suffix)
            actual_count = normalized.count(expected)
            if actual_count < count:
                problems.append(
                    f"address honorific mismatch: {source_base}+{source_suffix} "
                    f"must contain {expected!r} {count} time(s), found {actual_count}"
                )
    return [f"{path}:{unit_id}: {problem}" for problem in problems]


def validate_titles(path: Path, unit_id: str, original: str, translation: str) -> list[str]:
    problems: list[str] = []
    normalized = normalize_display(translation)
    scrubbed = original

    for compound in NON_TITLE_COMPOUNDS:
        scrubbed = scrubbed.replace(compound, "")

    # Work longest-to-shortest and remove handled source spans so overlapping title
    # names (e.g. 営業部長 vs 部長, 副司令 vs 司令) are not double-counted.
    for source_title, korean_title in TITLES:
        explicit_expected: dict[str, int] = {}
        for source_suffix, korean_suffix in SUFFIXES.items():
            pattern = re.compile(
                re.escape(source_title) + r"[ \t\r\n　]*" + re.escape(source_suffix)
            )
            count = len(pattern.findall(scrubbed))
            if not count:
                continue
            expected = title_honorific_form(korean_title, korean_suffix)
            explicit_expected[expected] = explicit_expected.get(expected, 0) + count
            actual_count = normalized.count(expected)
            if actual_count < count:
                problems.append(
                    f"title honorific mismatch: {source_title}+{source_suffix} "
                    f"must contain {expected!r} {count} time(s), found {actual_count}"
                )
            scrubbed = pattern.sub("", scrubbed)

        bare_count = scrubbed.count(source_title)
        if not bare_count:
            continue

        if not bare_title_present(normalized, korean_title):
            problems.append(f"missing title translation: {source_title} -> {korean_title}")

        # For bare source titles, a Korean suffix is an unauthorized grade change.
        # Explicit source-suffix occurrences were removed above and are therefore
        # allowed up to their exact source counts.
        for korean_suffix in KOREAN_SUFFIX_FORMS:
            form = title_honorific_form(korean_title, korean_suffix)
            actual = normalized.count(form)
            allowed = explicit_expected.get(form, 0)
            if actual > allowed:
                problems.append(
                    f"added honorific on bare title: {source_title} must not add {form!r} "
                    f"(found {actual}, explicit source allows {allowed})"
                )
        scrubbed = scrubbed.replace(source_title, "")

    return [f"{path}:{unit_id}: {problem}" for problem in problems]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()

    files = authority_files(args.assets)
    person_terms = load_person_terms(args.assets)
    violations: list[str] = []
    units_checked = 0

    for path in files:
        for unit_id, original, translation in iter_units(path):
            units_checked += 1
            violations.extend(
                validate_person_suffixes(path, unit_id, original, translation, person_terms)
            )
            violations.extend(validate_titles(path, unit_id, original, translation))
            violations.extend(validate_address_terms(path, unit_id, original, translation))

    if violations:
        print("name/title/honorific validation failed:")
        for violation in violations:
            print(f"- {violation}")
        raise SystemExit(1)

    print(
        f"validated names/titles/honorifics: {units_checked} units across "
        f"{len(files)} authority files; {len(person_terms)} person-name forms"
    )


if __name__ == "__main__":
    main()
