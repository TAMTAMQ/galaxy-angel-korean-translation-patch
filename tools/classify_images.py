"""Classify every extracted image asset into resolved / excluded / unresolved.

Follows the create-kr-patch graphics-text method:
  - strategy/graphics-text.md    §1  declare a denominator, decide every item
  - strategy/text-extraction.md  §1.4 resolved / excluded / unresolved,
                                      unresolved is never merged into excluded
  - conventions/project-records.md §5 graphics-text catalog fields

Heuristics only ever produce *candidates* (text-extraction.md §1.3): every
screened item stays `uninvestigated` until a visual pass decides it. The
score exists to order the contact sheets, not to adopt a result.
"""

import collections
import json
import os
import re

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "assets", "image_extraction")
OUT = os.path.join(EX, "catalog")

GADAT = os.path.join(EX, "GADAT032")
JP = os.path.join(EX, "japanese_images")
VERIFY = ["v51_verify", "v52_verify", "v53_verify"]


def load(p):
    return json.load(open(p, encoding="utf-8"))


def family(name):
    return re.match(r"[a-zA-Z_]+", name.rsplit(".", 1)[0]).group(0)


def features(path):
    """Cheap shape statistics that separate lettering from artwork.

    Text is many short strokes with a long perimeter for its area and few
    distinct colours; artwork is long runs, large area, many colours.
    """
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    px = im.load()
    opaque = 0
    runs = []
    edge = 0
    colors = set()
    for y in range(h):
        run = 0
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 128:
                opaque += 1
                run += 1
                if len(colors) < 64:
                    colors.add((r >> 4, g >> 4, b >> 4))
                if (
                    x == 0 or y == 0 or x == w - 1 or y == h - 1
                    or px[x - 1, y][3] <= 128 or px[x + 1, y][3] <= 128
                    or px[x, y - 1][3] <= 128 or px[x, y + 1][3] <= 128
                ):
                    edge += 1
            else:
                if run:
                    runs.append(run)
                run = 0
        if run:
            runs.append(run)
    area = w * h
    ink = opaque / area if area else 0.0
    mean_run = (sum(runs) / len(runs)) if runs else 0.0
    perim = (edge / opaque) if opaque else 0.0
    return {
        "w": w, "h": h, "ink": round(ink, 4),
        "mean_run": round(mean_run, 2), "perimeter_ratio": round(perim, 4),
        "colors": len(colors),
    }


def score(f):
    """Higher = more likely to be lettering. Ordering only, never a verdict."""
    if f["w"] * f["h"] < 64:
        return 0.0
    s = 0.0
    s += min(f["perimeter_ratio"] / 0.6, 1.0) * 3.0      # thin strokes
    if 0 < f["mean_run"] <= 6:
        s += 2.0
    elif f["mean_run"] <= 12:
        s += 1.0
    if 0.03 <= f["ink"] <= 0.55:
        s += 1.5
    if f["colors"] <= 12:
        s += 1.0
    if f["h"] <= 48 and f["w"] >= 3 * f["h"]:
        s += 1.5                                          # wide text strip
    return round(s, 2)


def main():
    os.makedirs(OUT, exist_ok=True)
    gad = load(os.path.join(GADAT, "manifest.json"))
    jp_names = {e["name"] for e in load(os.path.join(JP, "manifest.json"))}
    jp_cat = {e["name"]: e["category"] for e in load(os.path.join(JP, "manifest.json"))}
    ui = load(os.path.join(JP, "ui_translations.json"))
    ui_status = {i["filename"]: i for i in ui["images"]}
    pngs = set(os.listdir(os.path.join(GADAT, "png")))

    # Duplicate populations: the verify dumps must be shown to be re-dumps of
    # the same archive before they can be excluded from the denominator.
    dup = {}
    for v in VERIFY:
        m = load(os.path.join(EX, v, "manifest.json"))
        dup[v] = {
            "entries": len(m),
            "identical_to_GADAT032": (
                {e["name"]: e["block_offset"] for e in m}
                == {e["name"]: e["block_offset"] for e in gad}
            ),
        }

    catalog = []
    for e in gad:
        name = e["name"]
        rec = {
            "block_id": "GADAT032/" + name,
            "source": {
                "container": "GADAT032",
                "name": name,
                "block_offset": e["block_offset"],
                "raw_size": e["raw_size"],
                "compressed_size": e.get("compressed_size"),
            },
            "bounds": (
                {"x": 0, "y": 0, "w": e["width"], "h": e["height"]}
                if e.get("width") else None
            ),
            "format": {"decoded_png": e.get("png"), "ext": name.rsplit(".", 1)[-1]},
            "family": family(name),
            "text": None,
            "priority": None,
            "catalog_state": None,
            "classification": None,
            "evidence": None,
        }

        if name in jp_names:
            st = ui_status.get(e.get("png"), {})
            rec["classification"] = "resolved"
            rec["catalog_state"] = (
                "verified" if st.get("status") == "localized" else "no text"
            )
            rec["text"] = {"translation": st.get("translation")}
            rec["format"]["category"] = jp_cat[name]
            rec["evidence"] = (
                "japanese_images/ui_translations.json status=%s; "
                "localized_verification.json" % st.get("status")
            )
        elif e.get("png") not in pngs:
            # No decoded pixels: encoding and boundaries are not established,
            # so this cannot be called excluded (text-extraction.md §1.4).
            rec["classification"] = "unresolved"
            rec["catalog_state"] = "unresolved"
            rec["evidence"] = (
                "manifest carries no width/height and no PNG was produced; "
                "container format for .%s not established"
                % name.rsplit(".", 1)[-1]
            )
        else:
            f = features(os.path.join(GADAT, "png", e["png"]))
            rec["format"]["features"] = f
            rec["priority"] = score(f)
            rec["classification"] = "unresolved"
            rec["catalog_state"] = "uninvestigated"
            rec["evidence"] = "decoded PNG present; awaiting visual decision"
        catalog.append(rec)

    counts = collections.Counter(r["classification"] for r in catalog)
    states = collections.Counter(r["catalog_state"] for r in catalog)
    doc = {
        "schema": "galaxy-angel-graphics-text-catalog/v1",
        "denominator": {
            "GADAT032": len(gad),
            "japanese_images_subset": len(jp_names),
            "remainder": len(gad) - len(jp_names),
            "duplicate_populations": dup,
        },
        "counts": dict(counts),
        "states": dict(states),
        "items": catalog,
    }
    json.dump(doc, open(os.path.join(OUT, "image_catalog.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("denominator GADAT032 = %d" % len(gad))
    print("classification:", dict(counts))
    print("catalog_state :", dict(states))
    for v, d in dup.items():
        print("  %s: %d entries, identical to GADAT032 = %s"
              % (v, d["entries"], d["identical_to_GADAT032"]))


if __name__ == "__main__":
    main()
