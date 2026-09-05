"""Snapshot approved PNGs without modifying originals; split occurrence-specific art."""
from pathlib import Path
import copy
import hashlib
import json
import shutil
import sys
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / 'tools'))
from galaxy_angel_verify_minigame_translation_inputs import rgba_hash

def main():
    dest = Path(sys.argv[1]).resolve()
    if dest.exists():
        raise SystemExit('Snapshot already exists')
    mini = ROOT / 'assets/image_extraction/MINI'
    # The release build consumes MINI/translated_png. Snapshot that exact live
    # input tree so a frozen build cannot silently fall back to an older
    # japanese_images/translated_png staging area.
    approved = mini / 'translated_png'
    ui = ROOT / 'assets/image_extraction/japanese_images/translated_png'
    records = []
    for folder in (approved, ui):
        for path in sorted(folder.rglob('*.png')):
            records.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    staged = dest / 'MINI'
    staged.mkdir(parents=True)
    shutil.copy2(mini / 'manifest.json', staged / 'manifest.json')
    shutil.copytree(ui, dest / 'UI')
    covered = set()
    for filename in ('mini_image_localization.json', 'mini_image_direct_additions.json'):
        doc = json.loads((ROOT / 'build' / filename).read_text(encoding='utf-8'))
        items = []
        for item in doc['items']:
            for occ in item['occurrences']:
                rel = occ['png']
                if rel in covered:
                    raise ValueError('Duplicate occurrence: ' + rel)
                source, target = mini / 'png' / rel, approved / rel
                source_hash, size = rgba_hash(source)
                target_hash, target_size = rgba_hash(target)
                if size != target_size:
                    raise ValueError(f'Canvas mismatch: {rel}: {size} != {target_size}')
                if source_hash != occ['pixel_sha256']:
                    raise ValueError('Source changed: ' + rel)
                fresh = copy.deepcopy(item)
                fresh.update(source_png=rel, translated_png=rel, source_path=occ['source_path'],
                             original_pixel_sha256=source_hash, translated_pixel_sha256=target_hash,
                             width=size[0], height=size[1], occurrences=[occ])
                items.append(fresh)
                for path, subdir in ((source, 'png'), (target, 'translated_png')):
                    out = staged / subdir / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, out)
                covered.add(rel)
        doc.update(items=items, translated_unique=len(items), translated_occurrences=len(items))
        (dest / filename).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    recipe = 'mini/mini00/resipi.png'
    out = staged / 'translated_png' / recipe
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(approved / recipe, out)
    actual = {p.relative_to(approved).as_posix() for p in (approved / 'mini').rglob('*.png')}
    extra = sorted(actual - covered - {recipe})
    if extra:
        raise ValueError('Unmapped approved images: ' + repr(extra))
    (dest / 'approved_hashes.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Frozen: {len(records)} PNGs; MINI occurrences={len(covered)} plus recipe; UI={len(list(ui.glob("*.png")))}')

if __name__ == '__main__':
    main()
