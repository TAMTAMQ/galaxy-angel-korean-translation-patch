from __future__ import annotations

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
MINI = ROOT / "assets" / "image_extraction" / "MINI"
SRC = MINI / "png"
TRANSLATED = MINI / "translated_png"
DEST = MINI / "japanese_images"
DEST_SRC = DEST / "png"
DEST_TRANSLATED = DEST / "translated_png"


def main() -> None:
    translated_files = sorted(p for p in TRANSLATED.rglob("*.png") if p.is_file())
    rel_paths = [p.relative_to(TRANSLATED) for p in translated_files]

    missing_source = [rel for rel in rel_paths if not (SRC / rel).is_file()]
    if missing_source:
        raise RuntimeError(
            "matching source PNG missing:\n" + "\n".join(str(p) for p in missing_source)
        )

    if DEST.exists():
        shutil.rmtree(DEST)

    copied_source: list[str] = []
    copied_translated: list[str] = []

    for rel in rel_paths:
        src_file = SRC / rel
        translated_file = TRANSLATED / rel
        dst_src = DEST_SRC / rel
        dst_translated = DEST_TRANSLATED / rel

        dst_src.parent.mkdir(parents=True, exist_ok=True)
        dst_translated.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(src_file, dst_src)
        shutil.copy2(translated_file, dst_translated)
        copied_source.append(rel.as_posix())
        copied_translated.append(rel.as_posix())

    print(f"source_png={len(copied_source)}")
    print(f"translated_png={len(copied_translated)}")
    print(f"destination={DEST}")


if __name__ == "__main__":
    main()
