from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio


def is_repetition_hallucination(text: str) -> bool:
    s = ''.join(ch for ch in text.strip() if not ch.isspace())
    if len(s) < 8:
        return False
    run = 1
    max_run = 1
    for a, b in zip(s, s[1:]):
        if a == b:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 1
    if max_run >= 5:
        return True
    if len(s) >= 16 and len(set(s)) / len(s) < 0.22:
        return True
    return False


def transcribe_file(model: WhisperModel, path: Path, core_seconds: float, context_seconds: float, include_words: bool) -> dict:
    audio = decode_audio(str(path), sampling_rate=16000, split_stereo=False)
    sr = 16000
    duration = len(audio) / sr
    kept: list[dict] = []

    core_start = 0.0
    while core_start < duration:
        core_end = min(duration, core_start + core_seconds)
        clip_start = max(0.0, core_start - context_seconds)
        clip_end = min(duration, core_end + context_seconds)
        start_i = int(round(clip_start * sr))
        end_i = int(round(clip_end * sr))
        clip = audio[start_i:end_i]

        segments, _info = model.transcribe(
            clip,
            language='ja',
            beam_size=5,
            best_of=5,
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=False,
            temperature=0.0,
            repetition_penalty=1.05,
            no_repeat_ngram_size=3,
            hallucination_silence_threshold=1.0,
        )

        for seg in segments:
            text = seg.text.strip()
            if not text or is_repetition_hallucination(text):
                continue
            # Independent short chunks can still hallucinate on pure music/effects.
            # Low-confidence long strings are much more likely to be such false positives.
            if float(seg.avg_logprob) < -0.85 and len(text) >= 8:
                continue
            gstart = clip_start + float(seg.start)
            gend = clip_start + float(seg.end)
            midpoint = (gstart + gend) / 2.0
            # Context exists only to avoid cutting speech at chunk boundaries.
            # Keep a segment exactly once, according to its midpoint in the core window.
            if midpoint < core_start or midpoint >= core_end:
                continue
            item = {
                'start': round(max(0.0, gstart), 3),
                'end': round(min(duration, gend), 3),
                'text': text,
                'avg_logprob': round(float(seg.avg_logprob), 4),
                'no_speech_prob': round(float(seg.no_speech_prob), 4),
            }
            if include_words:
                item['words'] = [
                    {
                        'start': round(clip_start + float(w.start), 3),
                        'end': round(clip_start + float(w.end), 3),
                        'word': w.word,
                        'probability': round(float(w.probability), 4),
                    }
                    for w in (seg.words or [])
                ]
            kept.append(item)

        core_start = core_end

    kept.sort(key=lambda x: (x['start'], x['end']))

    # Merge only exact duplicate text produced at nearly the same time.
    deduped: list[dict] = []
    for seg in kept:
        if deduped and seg['text'] == deduped[-1]['text'] and abs(seg['start'] - deduped[-1]['start']) < 1.0:
            if seg['avg_logprob'] > deduped[-1]['avg_logprob']:
                deduped[-1] = seg
            continue
        deduped.append(seg)

    return {
        'name': path.stem,
        'duration': round(duration, 3),
        'segments': deduped,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('inputs', nargs='+', type=Path)
    ap.add_argument('--model', default='large-v3-turbo')
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--compute-type', default='int8')
    ap.add_argument('--core-seconds', type=float, default=10.0)
    ap.add_argument('--context-seconds', type=float, default=1.5)
    ap.add_argument('--include-words', action='store_true')
    args = ap.parse_args()

    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    for path in args.inputs:
        result = transcribe_file(model, path, args.core_seconds, args.context_seconds, args.include_words)
        print('@@RESULT@@' + json.dumps(result, ensure_ascii=False, separators=(',', ':')), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
