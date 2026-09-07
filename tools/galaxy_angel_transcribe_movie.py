from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe a Galaxy Angel synced MKV to Japanese timestamp JSON.")
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--channel", choices=("mono", "left", "right"), default="mono")
    args = ap.parse_args()

    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    if args.channel == "mono":
        audio = decode_audio(str(args.input), sampling_rate=16000, split_stereo=False)
    else:
        left, right = decode_audio(str(args.input), sampling_rate=16000, split_stereo=True)
        audio = left if args.channel == "left" else right

    segments_iter, info = model.transcribe(
        audio,
        language="ja",
        beam_size=5,
        best_of=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        condition_on_previous_text=True,
        temperature=0.0,
    )

    segments = []
    for seg in segments_iter:
        words = []
        for w in seg.words or []:
            words.append(
                {
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                    "word": w.word,
                    "probability": round(float(w.probability), 4),
                }
            )
        segments.append(
            {
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": seg.text.strip(),
                "avg_logprob": round(float(seg.avg_logprob), 4),
                "no_speech_prob": round(float(seg.no_speech_prob), 4),
                "words": words,
            }
        )
        print(f"[{seg.start:7.2f} -> {seg.end:7.2f}] {seg.text.strip()}")

    payload = {
        "language": info.language,
        "language_probability": round(float(info.language_probability), 4),
        "duration": round(float(info.duration), 6),
        "duration_after_vad": round(float(info.duration_after_vad), 6),
        "model": args.model,
        "channel": args.channel,
        "segments": segments,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
