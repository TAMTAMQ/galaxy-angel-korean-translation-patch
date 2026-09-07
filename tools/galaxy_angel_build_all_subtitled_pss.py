from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path

from galaxy_angel_build_subtitled_pss import get_ffmpeg, parse_pictures, reconstruct_video_es
from galaxy_angel_pss_analyze import parse_program_stream
from galaxy_angel_pssplex_mux import frame_rate_from_es, mux


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def encode_m2v_matching_rate(
    ffmpeg: str,
    source: Path,
    subtitle: Path,
    output: Path,
    frame_rate: Fraction,
    bitrate_kbps: int,
    vbv_bytes: int,
) -> list[str]:
    movie_root = source.parent.parent
    sub_arg = subtitle.resolve().relative_to(movie_root.resolve()).as_posix()
    src_arg = source.resolve().relative_to(movie_root.resolve()).as_posix()
    out_arg = output.resolve().relative_to(movie_root.resolve()).as_posix()
    rate_arg = (
        str(frame_rate.numerator)
        if frame_rate.denominator == 1
        else f"{frame_rate.numerator}/{frame_rate.denominator}"
    )
    gop = max(12, round(float(frame_rate) / 2))
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        src_arg,
        "-map",
        "0:v:0",
        "-vf",
        f"ass={sub_arg}",
        "-an",
        "-c:v",
        "mpeg2video",
        "-pix_fmt",
        "yuv420p",
        "-r",
        rate_arg,
        "-aspect",
        "4:3",
        "-g",
        str(gop),
        "-bf",
        "2",
        "-b:v",
        f"{bitrate_kbps}k",
        "-minrate",
        f"{bitrate_kbps}k",
        "-maxrate",
        "6000k",
        "-bufsize",
        str(vbv_bytes),
        "-qmin",
        "2",
        "-qmax",
        "31",
        "-trellis",
        "1",
        "-f",
        "mpeg2video",
        out_arg,
    ]
    subprocess.run(cmd, cwd=movie_root, check=True)
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description="Build all Galaxy Angel subtitled movies as PS2 PSS Plex-compatible streams.")
    ap.add_argument("--movie-root", type=Path, default=Path("movie"))
    ap.add_argument("--output-dir", type=Path, default=Path("movie/subtitled/final"))
    ap.add_argument("--report", type=Path, default=Path("build/all_subtitled_pss_report.json"))
    ap.add_argument("--bitrate-kbps", type=int, default=5950)
    ap.add_argument("--vbv-bytes", type=int, default=1835008)
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--skip-encode", action="store_true")
    args = ap.parse_args()

    root = args.movie_root
    synced = root / "synced"
    subtitles = root / "subtitles"
    originals = root / "original"
    wavs = root / "output"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    names = args.names or sorted(p.stem for p in synced.glob("GADAT*.mkv"))
    ffmpeg = get_ffmpeg()
    results: list[dict[str, object]] = []

    for index, name in enumerate(names, 1):
        source = synced / f"{name}.mkv"
        subtitle = subtitles / f"{name}.ko.ass"
        original = originals / f"{name}.PSS"
        wav = wavs / f"{name}_pcm.wav"
        m2v = args.output_dir / f"{name}.m2v"
        pss = args.output_dir / f"{name}.PSS"
        pss_report = args.output_dir / f"{name}.pss.json"

        missing = [str(p) for p in (source, subtitle, original, wav) if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"{name}: missing inputs: {missing}")

        original_demux_m2v = wavs / f"{name}.m2v"
        if not original_demux_m2v.is_file():
            raise FileNotFoundError(f"{name}: original demux M2V missing: {original_demux_m2v}")
        frame_rate = frame_rate_from_es(original_demux_m2v.read_bytes())

        print(f"[{index}/{len(names)}] {name}: encode {frame_rate.numerator}/{frame_rate.denominator} fps", flush=True)
        encode_cmd = None
        if not args.skip_encode:
            encode_cmd = encode_m2v_matching_rate(
                ffmpeg,
                source,
                subtitle,
                m2v,
                frame_rate,
                args.bitrate_kbps,
                args.vbv_bytes,
            )
        if not m2v.is_file():
            raise FileNotFoundError(f"{name}: encoded M2V missing: {m2v}")

        original_data, original_packets = parse_program_stream(original)
        original_es, _ranges = reconstruct_video_es(original_data, original_packets)
        original_pictures = parse_pictures(original_es)
        encoded = m2v.read_bytes()
        encoded_pictures = parse_pictures(encoded)
        if len(encoded_pictures) != len(original_pictures):
            raise ValueError(
                f"{name}: picture count mismatch: original={len(original_pictures)} encoded={len(encoded_pictures)}"
            )

        print(f"[{index}/{len(names)}] {name}: mux", flush=True)
        mux_report = mux(m2v, wav, pss)
        pss_report.write_text(json.dumps(mux_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        result = {
            "name": name,
            "source": str(source.resolve()),
            "subtitle": str(subtitle.resolve()),
            "original_pss": str(original.resolve()),
            "m2v": str(m2v.resolve()),
            "pss": str(pss.resolve()),
            "original_pss_bytes": original.stat().st_size,
            "pss_bytes": pss.stat().st_size,
            "size_delta": pss.stat().st_size - original.stat().st_size,
            "frame_rate": f"{frame_rate.numerator}/{frame_rate.denominator}",
            "original_pictures": len(original_pictures),
            "encoded_pictures": len(encoded_pictures),
            "m2v_sha256": sha256_file(m2v),
            "pss_sha256": sha256_file(pss),
            "encode_command": encode_cmd,
            "mux_verification": mux_report["verification"],
        }
        results.append(result)
        args.report.write_text(
            json.dumps(
                {
                    "schema": "galaxy-angel-all-subtitled-pss/v1",
                    "bitrate_kbps": args.bitrate_kbps,
                    "vbv_bytes": args.vbv_bytes,
                    "completed": len(results),
                    "requested": len(names),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"[{index}/{len(names)}] {name}: OK m2v={m2v.stat().st_size} pss={pss.stat().st_size} delta={result['size_delta']}",
            flush=True,
        )

    print(f"Completed {len(results)}/{len(names)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
