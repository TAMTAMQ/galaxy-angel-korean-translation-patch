"""Dense review pages over whatever is still `uninvestigated`.

Groups by family so a page can be decided in blocks, and packs many families
per page so the whole remainder fits in a readable number of pages. Rerun
after each `apply_decisions.py` to get pages for what is still open.
"""

import collections
import json
import os
import shutil

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAT = os.path.join(ROOT, "assets", "image_extraction", "catalog")
PNG = os.path.join(ROOT, "assets", "image_extraction", "GADAT032", "png")
PAGES = os.path.join(CAT, "pass_pages")

CELL = 132
COLS = 8
ROWS = 7
LABEL = 13
HDR = 15
BG = (26, 26, 30)
CHK = ((58, 58, 64), (42, 42, 48))


def checker(size):
    im = Image.new("RGB", size, CHK[0])
    d = ImageDraw.Draw(im)
    for y in range(0, size[1], 8):
        for x in range(0, size[0], 8):
            if (x // 8 + y // 8) % 2:
                d.rectangle([x, y, x + 7, y + 7], fill=CHK[1])
    return im


def thumb(path, box):
    im = Image.open(path).convert("RGBA")
    s = min(box / im.width, box / im.height, 5.0)
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))),
                   Image.NEAREST if s >= 1 else Image.LANCZOS)
    back = checker(im.size)
    back.paste(im, (0, 0), im)
    return back


def main():
    doc = json.load(open(os.path.join(CAT, "image_catalog.json"), encoding="utf-8"))
    todo = [r for r in doc["items"] if r["catalog_state"] == "uninvestigated"]
    fam = collections.defaultdict(list)
    for r in todo:
        fam[r["family"]].append(r)
    order = sorted(fam, key=lambda k: (-len(fam[k]), k))
    flat = [r for k in order for r in sorted(fam[k], key=lambda r: r["source"]["name"])]

    if os.path.isdir(PAGES):
        shutil.rmtree(PAGES)
    os.makedirs(PAGES)

    per = COLS * ROWS
    index = []
    for pi in range(0, len(flat), per):
        grp = flat[pi:pi + per]
        page = Image.new("RGB", (COLS * CELL, HDR + ROWS * (CELL + LABEL)), BG)
        d = ImageDraw.Draw(page)
        d.text((4, 3), "page %02d   %d items   families: %s"
               % (pi // per + 1, len(grp),
                  ", ".join(sorted({r["family"] for r in grp}))),
               fill=(255, 215, 120))
        for i, r in enumerate(grp):
            cx = (i % COLS) * CELL
            cy = HDR + (i // COLS) * (CELL + LABEL)
            t = thumb(os.path.join(PNG, r["format"]["decoded_png"]), CELL - 4)
            page.paste(t, (cx + (CELL - t.width) // 2, cy + (CELL - t.height) // 2))
            d.rectangle([cx, cy, cx + CELL - 1, cy + CELL - 1], outline=(72, 72, 82))
            d.text((cx + 2, cy + CELL + 1), r["source"]["name"].replace(".tex", ""),
                   fill=(205, 205, 215))
        name = "p%02d.png" % (pi // per + 1)
        page.save(os.path.join(PAGES, name))
        index.append({"page": name,
                      "names": [r["source"]["name"] for r in grp]})

    json.dump({"schema": "galaxy-angel-pass-page-index/v1",
               "uninvestigated": len(flat), "pages": len(index), "index": index},
              open(os.path.join(CAT, "pass_page_index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("%d uninvestigated -> %d pages" % (len(flat), len(index)))


if __name__ == "__main__":
    main()
