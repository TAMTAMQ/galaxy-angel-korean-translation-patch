#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont


def load_rgba(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def alpha_bbox(arr: np.ndarray, threshold: int = 4):
    mask = arr[..., 3] > threshold
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)]


def content_bbox(arr: np.ndarray, threshold: int = 12):
    h, w = arr.shape[:2]
    border = np.concatenate(
        [arr[0, :, :], arr[-1, :, :], arr[:, 0, :], arr[:, -1, :]], axis=0
    )
    bg = np.median(border[:, :3].astype(np.int16), axis=0)
    bg_alpha = int(np.median(border[:, 3].astype(np.int16)))
    rgb_delta = np.max(np.abs(arr[..., :3].astype(np.int16) - bg), axis=2)
    alpha_delta = np.abs(arr[..., 3].astype(np.int16) - bg_alpha)
    mask = (rgb_delta > threshold) | (alpha_delta > threshold)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)]


def bbox_shrink(expected, actual):
    if expected is None or actual is None:
        return None
    return {
        "left": int(actual[0] - expected[0]),
        "top": int(actual[1] - expected[1]),
        "right": int(expected[2] - actual[2]),
        "bottom": int(expected[3] - actual[3]),
    }


def gray(arr: np.ndarray) -> np.ndarray:
    rgb = arr[..., :3].astype(np.float32)
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def edge_map(arr: np.ndarray, threshold: float = 24.0) -> np.ndarray:
    g = gray(arr)
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[:, 1:] = np.abs(g[:, 1:] - g[:, :-1])
    gy[1:, :] = np.abs(g[1:, :] - g[:-1, :])
    return (np.maximum(gx, gy) >= threshold)


def edge_stats(expected: np.ndarray, actual: np.ndarray, strip: int = 16):
    h, w = expected.shape[:2]
    s = max(1, min(strip, h, w))
    ee = edge_map(expected)
    ae = edge_map(actual)
    out = {}
    for name, e, a in [
        ("left", ee[:, :s], ae[:, :s]),
        ("right", ee[:, -s:], ae[:, -s:]),
        ("top", ee[:s, :], ae[:s, :]),
        ("bottom", ee[-s:, :], ae[-s:, :]),
    ]:
        ec = int(e.sum())
        ac = int(a.sum())
        ratio = float(ac / ec) if ec else None
        overlap = int((e & a).sum())
        union = int((e | a).sum())
        iou = float(overlap / union) if union else 1.0
        out[name] = {"expected_edges": ec, "readback_edges": ac, "ratio": ratio, "iou": iou}
    return out


def best_edge_shift(expected: np.ndarray, actual: np.ndarray, limit: int = 4):
    e = edge_map(expected)
    a = edge_map(actual)
    h, w = e.shape
    best = (0, 0, -1.0)
    zero_score = 0.0
    for dy in range(-limit, limit + 1):
        for dx in range(-limit, limit + 1):
            y0e, y1e = max(0, dy), min(h, h + dy)
            x0e, x1e = max(0, dx), min(w, w + dx)
            y0a, y1a = max(0, -dy), min(h, h - dy)
            x0a, x1a = max(0, -dx), min(w, w - dx)
            if y1e <= y0e or x1e <= x0e:
                continue
            es = e[y0e:y1e, x0e:x1e]
            aa = a[y0a:y1a, x0a:x1a]
            union = int((es | aa).sum())
            score = float((es & aa).sum() / union) if union else 1.0
            if dx == 0 and dy == 0:
                zero_score = score
            if score > best[2]:
                best = (dx, dy, score)
    return {
        "dx": int(best[0]),
        "dy": int(best[1]),
        "score": float(best[2]),
        "zero_score": float(zero_score),
        "improvement": float(best[2] - zero_score),
    }


def diff_metrics(expected: np.ndarray, actual: np.ndarray):
    if expected.shape != actual.shape:
        return None
    d = np.abs(expected.astype(np.int16) - actual.astype(np.int16))
    px = np.max(d, axis=2)
    changed = px > 12
    ys, xs = np.where(changed)
    bbox = None if len(xs) == 0 else [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)]
    return {
        "changed_pixels_gt12": int(changed.sum()),
        "changed_fraction_gt12": float(changed.mean()),
        "mean_abs_rgba": float(d.mean()),
        "p95_max_channel": float(np.percentile(px, 95)),
        "max_channel": int(px.max()),
        "diff_bbox_gt12": bbox,
    }


def infer_header_size(raw: bytes):
    if len(raw) < 80 or raw[:4] != b"TEX ":
        return 0
    width, height = struct.unpack_from("<II", raw, 20)
    sw, sh = struct.unpack_from("<HH", raw, 56)
    if len(raw) in (64 + sw * sh * 4, 64 + sw * sh * 3):
        return 64
    if len(raw) == 80 + width * height * 3:
        return 80
    return 80


def raw_header_info(path: Path):
    raw = path.read_bytes()
    if len(raw) < 64 or raw[:4] != b"TEX ":
        return {"valid": False, "size": len(raw)}
    width, height = struct.unpack_from("<II", raw, 20)
    sw, sh = struct.unpack_from("<HH", raw, 56)
    pf = struct.unpack_from("<H", raw, 46)[0]
    hs = infer_header_size(raw)
    return {
        "valid": True,
        "size": len(raw),
        "width": int(width),
        "height": int(height),
        "storage_width": int(sw),
        "storage_height": int(sh),
        "pixel_format": int(pf),
        "header_size": hs,
        "header_hex": raw[:hs].hex(),
    }


def is_priority(name: str, size: tuple[int, int]):
    low = name.lower()
    w, h = size
    tokens = ("base", "panel", "help", "mes", "dialog", "result", "menu", "oper", "btn", "window", "waku", "map")
    return (w == 640 and h == 480) or (w * h >= 120000) or any(t in low for t in tokens)


def make_diff_image(expected: Image.Image, actual: Image.Image):
    if expected.size != actual.size:
        canvas = Image.new("RGBA", expected.size, (255, 0, 255, 255))
        return canvas
    a = np.array(expected.convert("RGBA"), dtype=np.int16)
    b = np.array(actual.convert("RGBA"), dtype=np.int16)
    d = np.max(np.abs(a - b), axis=2).astype(np.uint8)
    heat = np.zeros((d.shape[0], d.shape[1], 4), dtype=np.uint8)
    heat[..., 0] = np.clip(d * 3, 0, 255)
    heat[..., 1] = np.where(d <= 12, 40, 0)
    heat[..., 2] = np.where(d <= 12, 40, 0)
    heat[..., 3] = 255
    return Image.fromarray(heat, "RGBA")


def fit_thumb(im: Image.Image, box=(230, 172)):
    out = Image.new("RGBA", box, (25, 25, 25, 255))
    cp = im.copy()
    cp.thumbnail(box, Image.Resampling.LANCZOS)
    x = (box[0] - cp.width) // 2
    y = (box[1] - cp.height) // 2
    out.alpha_composite(cp, (x, y))
    return out


def draw_bbox_thumb(im: Image.Image, bbox, box=(230, 172)):
    base = im.copy().convert("RGBA")
    if bbox:
        d = ImageDraw.Draw(base)
        d.rectangle((bbox[0], bbox[1], max(bbox[0], bbox[2] - 1), max(bbox[1], bbox[3] - 1)), outline=(255, 0, 0, 255), width=max(1, min(base.size) // 120))
    return fit_thumb(base, box)


def build_sheets(entries, source_dir: Path, translated_dir: Path, readback_dir: Path, output_dir: Path, per_sheet=6):
    chosen = [e for e in entries if e["status"] != "ok" or e["priority"]]
    # Keep every suspect/fix candidate. For otherwise-OK priority entries, keep translated or 640x480 ones.
    chosen = [e for e in chosen if e["status"] != "ok" or e["translated_exists"] or e["size"] == [640, 480]]
    font = ImageFont.load_default()
    paths = []
    for sheet_i in range(math.ceil(len(chosen) / per_sheet)):
        batch = chosen[sheet_i * per_sheet : (sheet_i + 1) * per_sheet]
        width = 4 * 230 + 30
        row_h = 225
        sheet = Image.new("RGB", (width, 38 + row_h * len(batch)), (235, 235, 235))
        draw = ImageDraw.Draw(sheet)
        draw.text((8, 8), "original | translated/expected | ISO readback | diff", fill=(0, 0, 0), font=font)
        for row, e in enumerate(batch):
            y = 38 + row * row_h
            name = e["png"]
            op = source_dir / name
            tp = translated_dir / name
            rp = readback_dir / name
            o = load_rgba(op)
            t = load_rgba(tp) if tp.exists() else o
            r = load_rgba(rp)
            diff = make_diff_image(t, r)
            imgs = [
                draw_bbox_thumb(o, e.get("original", {}).get("alpha_bbox")),
                draw_bbox_thumb(t, e.get("expected", {}).get("alpha_bbox")),
                draw_bbox_thumb(r, e.get("readback", {}).get("alpha_bbox")),
                fit_thumb(diff),
            ]
            for col, thumb in enumerate(imgs):
                sheet.paste(thumb.convert("RGB"), (5 + col * 230, y))
            note = f"{name}  {e['status']}  {e['note']}"
            b1 = e.get("expected", {}).get("alpha_bbox")
            b2 = e.get("readback", {}).get("alpha_bbox")
            draw.text((8, y + 176), note[:150], fill=(160, 0, 0) if e["status"] != "ok" else (0, 0, 0), font=font)
            draw.text((8, y + 191), f"bbox exp={b1} rb={b2} size={e['size']}", fill=(0, 0, 0), font=font)
        out = output_dir / f"runtime_crop_sheet_{sheet_i:03d}.png"
        sheet.save(out)
        paths.append(str(out))
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--original", type=Path, required=True)
    ap.add_argument("--translated", type=Path, required=True)
    ap.add_argument("--readback", type=Path, required=True)
    ap.add_argument("--original-raw", type=Path, required=True)
    ap.add_argument("--readback-raw", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    names = sorted(p.name for p in args.original.glob("*.png"))
    entries = []
    for name in names:
        op = args.original / name
        tp = args.translated / name
        rp = args.readback / name
        tex = Path(name).with_suffix(".tex").name
        oraw = args.original_raw / tex
        rraw = args.readback_raw / tex
        e = {
            "png": name,
            "tex": tex,
            "translated_exists": tp.exists(),
            "readback_exists": rp.exists(),
        }
        if not rp.exists():
            e.update({"size": list(load_rgba(op).size), "priority": True, "status": "fix_required", "note": "missing ISO readback PNG"})
            entries.append(e)
            continue
        oi = load_rgba(op)
        ti = load_rgba(tp) if tp.exists() else oi
        ri = load_rgba(rp)
        oa = np.array(oi)
        ta = np.array(ti)
        ra = np.array(ri)
        e["size"] = list(oi.size)
        e["priority"] = is_priority(name, oi.size)
        e["original"] = {"size": list(oi.size), "alpha_bbox": alpha_bbox(oa), "content_bbox": content_bbox(oa)}
        e["expected"] = {"size": list(ti.size), "alpha_bbox": alpha_bbox(ta), "content_bbox": content_bbox(ta)}
        e["readback"] = {"size": list(ri.size), "alpha_bbox": alpha_bbox(ra), "content_bbox": content_bbox(ra)}
        e["source_vs_translated_dimensions_equal"] = oi.size == ti.size
        e["expected_vs_readback_dimensions_equal"] = ti.size == ri.size
        e["alpha_bbox_shrink"] = bbox_shrink(e["expected"]["alpha_bbox"], e["readback"]["alpha_bbox"])
        e["content_bbox_shrink"] = bbox_shrink(e["expected"]["content_bbox"], e["readback"]["content_bbox"])
        e["diff"] = diff_metrics(ta, ra)
        if ti.size == ri.size:
            e["edge"] = edge_stats(ta, ra)
            e["alignment"] = best_edge_shift(ta, ra)
        else:
            e["edge"] = None
            e["alignment"] = None

        if oraw.exists() and rraw.exists():
            oh = raw_header_info(oraw)
            rh = raw_header_info(rraw)
            e["tex_original"] = {k: v for k, v in oh.items() if k != "header_hex"}
            e["tex_readback"] = {k: v for k, v in rh.items() if k != "header_hex"}
            e["tex_header_equal"] = oh.get("header_hex") == rh.get("header_hex")
        else:
            e["tex_header_equal"] = None

        reasons = []
        severe = False
        if not e["source_vs_translated_dimensions_equal"]:
            reasons.append("source/translated dimensions differ")
            severe = True
        if not e["expected_vs_readback_dimensions_equal"]:
            reasons.append("ISO readback dimensions differ")
            severe = True
        if e["tex_header_equal"] is False:
            reasons.append("TEX header changed")
            severe = True
        shrink = e.get("alpha_bbox_shrink") or {}
        if any(shrink.get(k, 0) > 2 for k in ("right", "bottom", "left", "top")):
            reasons.append(f"alpha bbox shrank {shrink}")
            severe = True
        cshrink = e.get("content_bbox_shrink") or {}
        if any(cshrink.get(k, 0) > 4 for k in ("right", "bottom", "left", "top")):
            reasons.append(f"content bbox shrank {cshrink}")
        edge = e.get("edge") or {}
        for side in ("right", "bottom"):
            st = edge.get(side) or {}
            ec = st.get("expected_edges") or 0
            ratio = st.get("ratio")
            if ec >= 24 and ratio is not None and ratio < 0.55:
                reasons.append(f"{side} edge loss ratio={ratio:.2f}")
        al = e.get("alignment")
        if al and (al["dx"] or al["dy"]) and al["improvement"] > 0.08 and al["score"] > 0.20:
            reasons.append(f"offset suspect dx={al['dx']} dy={al['dy']} improvement={al['improvement']:.3f}")
        if reasons:
            e["status"] = "fix_required" if severe else "suspect"
            e["note"] = "; ".join(reasons)
        else:
            e["status"] = "ok"
            e["note"] = "no crop/layout mismatch detected"
        entries.append(e)

    counts = {
        "total": len(entries),
        "ok": sum(e["status"] == "ok" for e in entries),
        "suspect": sum(e["status"] == "suspect" for e in entries),
        "fix_required": sum(e["status"] == "fix_required" for e in entries),
        "translated": sum(e["translated_exists"] for e in entries),
        "priority": sum(e["priority"] for e in entries),
        "full_640x480": sum(e["size"] == [640, 480] for e in entries),
        "tex_header_mismatch": sum(e.get("tex_header_equal") is False for e in entries),
        "dimension_mismatch": sum(not e.get("expected_vs_readback_dimensions_equal", False) for e in entries),
    }
    problems = [e for e in entries if e["status"] != "ok"]
    priority_problems = sorted(
        problems,
        key=lambda e: (
            0 if e["status"] == "fix_required" else 1,
            0 if e["size"] == [640, 480] else 1,
            0 if e["priority"] else 1,
            e["png"],
        ),
    )
    report = {
        "scope": "GADAT032 full TEX PNG audit against current ISO readback",
        "counts": counts,
        "entries": entries,
    }
    (args.output_dir / "runtime_crop_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "counts": counts,
        "problem_files": [{"png": e["png"], "status": e["status"], "size": e["size"], "note": e["note"]} for e in problems],
        "priority_fix_order": [{"png": e["png"], "status": e["status"], "size": e["size"], "note": e["note"]} for e in priority_problems[:100]],
        "method": {
            "original": str(args.original),
            "translated": str(args.translated),
            "iso_readback": str(args.readback),
            "checks": [
                "PNG dimensions",
                "alpha bbox",
                "content bbox",
                "TEX header byte equality",
                "edge density loss on right/bottom",
                "edge alignment shift",
                "pixel diff metrics",
            ],
            "runtime_note": "This report verifies runtime-bound TEX structure and current ISO readback. Interactive emulator screen evidence is recorded separately when a usable save-state/screenshot is available.",
        },
    }
    sheet_paths = build_sheets(entries, args.original, args.translated, args.readback, args.output_dir)
    summary["sheets"] = sheet_paths
    (args.output_dir / "runtime_crop_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False))
    for e in priority_problems[:40]:
        print(e["status"], e["png"], e["size"], e["note"])
    print("sheets", len(sheet_paths))


if __name__ == "__main__":
    main()
