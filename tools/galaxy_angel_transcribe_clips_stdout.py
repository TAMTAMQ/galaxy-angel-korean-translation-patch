from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('clips', nargs='+', help='path|start|end')
    ap.add_argument('--model', default='large-v3-turbo')
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--compute-type', default='int8')
    args = ap.parse_args()

    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    for spec in args.clips:
        path_s, start_s, end_s = spec.split('|', 2)
        path = Path(path_s)
        start = float(start_s)
        end = float(end_s)
        audio = decode_audio(str(path), sampling_rate=16000, split_stereo=False)
        clip = audio[int(start * 16000):int(end * 16000)]
        segments, _ = model.transcribe(
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
        )
        out = []
        for seg in segments:
            out.append({
                'start': round(start + float(seg.start), 3),
                'end': round(start + float(seg.end), 3),
                'text': seg.text.strip(),
                'avg_logprob': round(float(seg.avg_logprob), 4),
                'words': [
                    {
                        'start': round(start + float(w.start), 3),
                        'end': round(start + float(w.end), 3),
                        'word': w.word,
                        'probability': round(float(w.probability), 4),
                    }
                    for w in (seg.words or [])
                ],
            })
        print('@@CLIP@@' + json.dumps({'name': path.stem, 'clip':[start,end], 'segments':out}, ensure_ascii=False, separators=(',',':')), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
