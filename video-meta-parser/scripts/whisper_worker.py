#!/usr/bin/env python3
"""Persistent faster-whisper worker using a JSON-lines stdin/stdout protocol."""

import argparse
import json
import sys


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='base')
    args = parser.parse_args()

    try:
        # HF_ENDPOINT is set by the parent before this process starts and imports
        # faster_whisper/huggingface_hub.
        from faster_whisper import WhisperModel
        model = WhisperModel(args.model, device='cpu', compute_type='int8')
    except Exception as e:
        emit({'ready': False, 'error': str(e)})
        return 1

    emit({'ready': True})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            segments, info = model.transcribe(
                request['audio_path'],
                language='zh',
                beam_size=5,
                vad_filter=True,
            )
            results = [
                {
                    'start': round(segment.start, 1),
                    'end': round(segment.end, 1),
                    'text': segment.text.strip(),
                }
                for segment in segments
            ]
            emit({
                'language': info.language,
                'language_prob': info.language_probability,
                'segments': results,
            })
        except Exception as e:
            emit({'error': str(e)})

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
