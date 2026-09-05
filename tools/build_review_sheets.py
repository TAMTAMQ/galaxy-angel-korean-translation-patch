"""Contact sheets for the visual pass over the uninvestigated GADAT032 images.

One sheet per name family, families ordered by their best heuristic score so
the high-yield ones come first. Every uninvestigated item appears on exactly
one sheet, so the pass can reach a decision for the whole denominator.
"""

import collections
import json
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "assets", "image_extraction")
CAT = os.path.join(EX, "catalog")
SHEETS = os.path.join(CAT, "review_sheets")
PNG = os.path.join(EX, "GADAT032", "png")

CELL = 176          # cell side for the thumbnail
LABEL = 14
COLS = 6
MIN_FAMILY = 6      # smaller families are pooled into misc sheets
MISC_PER_SHEET = 36
BG = (28, 28, 32)
GRID = (70, 70, 80)
CHECKER = ((60, 60, 66), (44, 44, 50))


def checkerboard(size):
    im = Image.new("RGB", size, CHECKER[0])
    d = ImageDraw.Draw(im)
    for y in range(0, size[1], 8):
        for x in range(0, size[0], 8):
            if (x // 8 + y // 8) % 2:
                d.rectangle([x, y, x + 7, y + 7], fill=CHECKER[1])
    return im


def thumb(path):
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    s = min(CELL / w, CELL / h, 4.0)
    if s != 1.0:
        rs = Image.NEAREST if s >= 1 else Image.LANCZOS
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), rs)
    back = checkerboard(im.size)
    back.paste(im, (0, 0), im)
    return back


def main():
    os.makedirs(SHEETS, exist_ok=True)
    doc = json.load(open(os.path.join(CAT, "image_catalog.json"), encoding="utf-8"))
    todo = [r for r in doc["items"] if r["catalog_state"] == "uninvestigated"]

    fam = collections.defaultdict(list)
    for r in todo:
        fam[r["family"]].append(r)

    # A family of one or two makes a near-empty sheet. Pool the small ones by
    # priority instead, so the pass is a manageable number of full pages.
    groups, pool = [], []
    for k, v in fam.items():
        (groups if len(v) >= MIN_FAMILY else pool).append((k, v))
    groups.sort(key=lambda kv: -max(r["priority"] for r in kv[1]))
    pool = sorted((r for _, v in pool for r in v),
                  key=lambda r: (-r["priority"], r["source"]["name"]))
    for i in range(0, len(pool), MISC_PER_SHEET):
        groups.append(("misc%02d" % (i // MISC_PER_SHEET + 1), pool[i:i + MISC_PER_SHEET]))

    index = []
    for rank, (key, grp) in enumerate(groups, 1):
        items = sorted(grp, key=lambda r: (-r["priority"], r["source"]["name"]))
        rows = (len(items) + COLS - 1) // COLS
        sheet = Image.new("RGB", (COLS * CELL, rows * (CELL + LABEL) + LABEL), BG)
        d = ImageDraw.Draw(sheet)
        for i, r in enumerate(items):
            cx = (i % COLS) * CELL
            cy = (i // COLS) * (CELL + LABEL)
            t = thumb(os.path.join(PNG, r["format"]["decoded_png"]))
            sheet.paste(t, (cx + (CELL - t.width) // 2, cy + (CELL - t.height) // 2))
            d.rectangle([cx, cy, cx + CELL - 1, cy + CELL - 1], outline=GRID)
            d.text((cx + 3, cy + CELL + 1),
                   "%s  %.1f" % (r["source"]["name"], r["priority"]),
                   fill=(210, 210, 220))
        name = "%02d_%s_%d.png" % (rank, key.strip("_") or "misc", len(items))
        sheet.save(os.path.join(SHEETS, name))
        index.append({"sheet": name, "family": key, "count": len(items),
                      "max_priority": max(r["priority"] for r in items),
                      "names": [r["source"]["name"] for r in items]})

    json.dump({"schema": "galaxy-angel-review-sheet-index/v1",
               "uninvestigated": len(todo), "sheets": len(index),
               "index": index},
              open(os.path.join(CAT, "review_sheet_index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("%d uninvestigated items -> %d sheets" % (len(todo), len(index)))
    for e in index[:12]:
        print("  %-34s %4d items  max %.1f" % (e["sheet"], e["count"], e["max_priority"]))


if __name__ == "__main__":
    main()
