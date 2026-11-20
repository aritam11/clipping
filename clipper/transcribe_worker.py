"""Runs faster-whisper in its own process.

Kept separate because PyAV (used by faster-whisper) and OpenCV each bundle their
own FFmpeg libraries, and loading both in one process on macOS is unsafe.

Two modes:
  python -m clipper.transcribe_worker MEDIA OUT.json   - one file, then exit
  python -m clipper.transcribe_worker --serve          - keep the model loaded; read
      {"id", "media", "offset"} JSON lines on stdin, answer {"id", "segments"} on stdout
      (live mode sends a new chunk every ~30s)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ATTEMPTS = {"auto": [("mps", "float16"), ("cpu", "int8")], "mps": [("mps", "float16")], "cpu": [("cpu", "int8")]}


def _load(WhisperModel, model_name: str, device: str, ctype: str):
    return WhisperModel(model_name, device=device, compute_type=ctype, cpu_threads=os.cpu_count() or 4)


def _segments(model, media: str, language: str | None, offset: float = 0.0, progress: str | None = None):
    segments, info = model.transcribe(
        media,
        language=language,
        word_timestamps=True,
        vad_filter=True,  # Silero VAD: skips music/silence, cuts hallucinations
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
        beam_size=1,
    )
    out, last = [], time.time()
    for s in segments:
        words = [
            {"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "word": w.word,
             "p": round(w.probability, 3)}
            for w in (s.words or [])
        ]
        out.append({"start": round(s.start + offset, 3), "end": round(s.end + offset, 3),
                    "text": s.text.strip(), "words": words})
        if progress and time.time() - last > 20:
            print(f"    transcribed {s.end / 60:.1f} / {info.duration / 60:.1f} min ({progress})", flush=True)
            last = time.time()
    return out, info


def _one_file(a, WhisperModel) -> None:
    attempts = ATTEMPTS[a.device]
    t0 = time.time()
    for i, (device, ctype) in enumerate(attempts):
        try:
            out, info = _segments(_load(WhisperModel, a.model, device, ctype), a.media, a.language, progress=device)
            break
        except Exception as exc:  # noqa: BLE001
            if i == len(attempts) - 1:
                raise
            print(f"    {device} transcription failed ({str(exc)[:120]}); retrying on {attempts[i + 1][0]}", flush=True)

    data = {"language": info.language, "duration": info.duration, "segments": out, "device": device,
            "seconds_taken": round(time.time() - t0, 1)}
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, a.out)


def _serve(a, WhisperModel) -> None:
    attempts = list(ATTEMPTS[a.device])
    device, ctype = attempts.pop(0)
    model = _load(WhisperModel, a.model, device, ctype)
    print(json.dumps({"ready": True, "device": device}), flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        while True:
            try:
                segs, _ = _segments(model, req["media"], a.language, float(req.get("offset", 0)))
                print(json.dumps({"id": req["id"], "segments": segs}, ensure_ascii=False), flush=True)
                break
            except Exception as exc:  # noqa: BLE001
                if not attempts:
                    print(json.dumps({"id": req["id"], "error": str(exc)[:500]}), flush=True)
                    break
                print(f"{device} failed ({str(exc)[:120]}); switching to {attempts[0][0]}", file=sys.stderr, flush=True)
                device, ctype = attempts.pop(0)
                model = _load(WhisperModel, a.model, device, ctype)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("media", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--language", default=None)
    ap.add_argument("--device", default="auto", choices=list(ATTEMPTS))
    a = ap.parse_args()

    from faster_whisper import WhisperModel

    if a.serve:
        _serve(a, WhisperModel)
    else:
        if not (a.media and a.out):
            ap.error("MEDIA and OUT are required unless --serve is given")
        _one_file(a, WhisperModel)


if __name__ == "__main__":
    main()
