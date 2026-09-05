"""Apply visual-pass decisions to the graphics-text catalog.

Reads `catalog/decisions.tsv` (`name<TAB>catalog_state<TAB>evidence`) and
writes the states back into `catalog/image_catalog.json`. Items too small to
hold a glyph are decided from their measured bounds, which is evidence rather
than a heuristic guess.
"""

import collections
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAT = os.path.join(ROOT, "assets", "image_extraction", "catalog")

# A CJK glyph cannot be drawn in fewer than 8 px on either axis.
MIN_GLYPH = 8

EXCLUDED_STATES = {"no text", "latin only"}


def main():
    doc = json.load(open(os.path.join(CAT, "image_catalog.json"), encoding="utf-8"))
    by_name = {r["source"]["name"]: r for r in doc["items"]}

    dec = {}
    path = os.path.join(CAT, "decisions.tsv")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if line.startswith("#") or not line.strip():
                continue
            f = line.rstrip("\n").split("\t")
            dec[f[0]] = (f[1], f[2] if len(f) > 2 else "visual pass")

    applied = 0
    for r in doc["items"]:
        if r["catalog_state"] != "uninvestigated":
            continue
        b = r["bounds"]
        if b and (b["w"] < MIN_GLYPH or b["h"] < MIN_GLYPH):
            r["catalog_state"] = "no text"
            r["classification"] = "excluded"
            r["evidence"] = ("measured bounds %dx%d are below the %d px minimum "
                             "for a CJK glyph" % (b["w"], b["h"], MIN_GLYPH))
            applied += 1

    for name, (state, ev) in dec.items():
        r = by_name.get(name)
        if r is None:
            print("  unknown name in decisions.tsv:", name)
            continue
        r["catalog_state"] = state
        r["classification"] = (
            "excluded" if state in EXCLUDED_STATES
            else "resolved" if state in ("found", "verified")
            else "unresolved"
        )
        r["evidence"] = ev
        applied += 1

    doc["counts"] = dict(collections.Counter(r["classification"] for r in doc["items"]))
    doc["states"] = dict(collections.Counter(r["catalog_state"] for r in doc["items"]))
    json.dump(doc, open(os.path.join(CAT, "image_catalog.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("applied %d decisions" % applied)
    print("classification:", doc["counts"])
    print("catalog_state :", doc["states"])


if __name__ == "__main__":
    main()
