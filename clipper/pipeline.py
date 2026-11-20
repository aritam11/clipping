"""One video in -> finished vertical clips + post metadata out."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import source
from .captions import build_ass, words_in_range
from .config import Settings
from .highlights import USAGE, pick_clips
from .reframe import analyze
from .render import render
from .signals import loudness_curve, zscore
from .status import Job, NullJob
from .util import fmt_ts, log, media_info, read_json, write_json

PROGRESS = re.compile(r"transcribed ([\d.]+) / ([\d.]+) min")


def transcribe(media: Path, out_json: Path, st: Settings, job: Job | None = None) -> dict:
    job = job or NullJob()
    if out_json.exists():
        log("Using cached transcript")
        return read_json(out_json)
    log(f"Transcribing with faster-whisper ({st.whisper_model}); long VODs take a while")
    cmd = [sys.executable, "-m", "clipper.transcribe_worker", str(media), str(out_json), "--model", st.whisper_model]
    if st.language:
        cmd += ["--language", st.language]
    proc = subprocess.Popen(cmd, cwd=Path(__file__).resolve().parent.parent, stdout=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    assert proc.stdout
    for line in proc.stdout:  # echo progress and pass it to the dashboard
        print(line, end="", flush=True)
        m = PROGRESS.search(line)
        if m and float(m.group(2)) > 0:
            job.update(progress=min(1.0, float(m.group(1)) / float(m.group(2))),
                       stage=f"Transcribing ({m.group(1)} / {m.group(2)} min)")
    if proc.wait() != 0 or not out_json.exists():
        raise RuntimeError("transcription failed (see output above)")
    data = read_json(out_json)
    log(f"Transcribed {data['duration'] / 60:.1f} min in {data['seconds_taken'] / 60:.1f} min "
        f"({len(data['segments'])} segments, language={data['language']})")
    return data


def process(src: str, st: Settings) -> list[Path]:
    job = Job("vod", src, source=src)
    try:
        written = _process(src, st, job)
    except source.StillLive as exc:
        job.finish("skipped", str(exc)[:300])
        raise
    except Exception as exc:
        job.finish("failed", str(exc)[:300])
        raise
    job.finish("done", f"{len(written)} clip(s) made")
    return written


def _process(src: str, st: Settings, job: Job) -> list[Path]:
    t0 = time.time()
    url_mode = source.is_url(src)
    job.update(stage="Reading video info")
    if url_mode:
        info = source.probe(src)
        if info["is_live"]:
            raise source.StillLive("source is live or upcoming; process the VOD after the stream ends "
                                   "(or use `clip live`)")
    else:
        path = Path(src).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        info = source.local_info(path)

    rng = st.range
    vid = info["id"] + (f"_{int(rng[0])}-{int(rng[1])}" if rng else "")
    work = st.work_dir / vid
    out_dir = st.out_dir / vid
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = f"{info['title'] or vid}{f' [{fmt_ts(rng[0])} - {fmt_ts(rng[1])}]' if rng else ''}"
    log(f"=== {label} ===")
    job.update(title=label, out=str(out_dir), force=True)

    job.update(stage="Downloading audio", progress=None, force=True)
    if url_mode:
        if rng and info["duration"] and rng[0] >= info["duration"]:
            raise RuntimeError(f"--range starts after the video ends ({fmt_ts(info['duration'])})")
        audio = source.download_audio(info["url"], work, rng)
    else:
        audio = source.extract_audio(path, work, rng) if rng else path
    offset = rng[0] if rng else 0.0  # analysis times are relative to the range start
    duration = media_info(audio)["duration"]
    job.event("info", f"Audio ready ({duration / 60:.0f} min)")
    job.update(stage="Transcribing", progress=0.0, force=True)
    transcript = transcribe(audio, work / "transcript.json", st, job)
    job.event("info", f"Transcribed {len(transcript['segments'])} segments")
    job.update(stage="Measuring loudness", progress=None, force=True)
    z = zscore(loudness_curve(audio, work / "loudness.npy"))

    job.update(stage="Claude is picking clips", force=True)
    clips = pick_clips(transcript, z, duration, st, title=info["title"])
    log(f"Picked {len(clips)} clip(s)")
    job.event("claude", f"Picked {len(clips)} clip(s)", usage=dict(USAGE))
    job.update(usage=dict(USAGE))

    results, written = [], []
    for i, c in enumerate(clips, 1):
        name = f"clip_{i:02d}"
        abs_s, abs_e = c.start + offset, c.end + offset  # position in the original video
        log(f"[{i}/{len(clips)}] {fmt_ts(abs_s)}-{fmt_ts(abs_e)} ({c.end - c.start:.0f}s) {c.title!r}")
        job.update(stage=f"Clip {i}/{len(clips)}: {c.title or fmt_ts(abs_s)}", progress=(i - 1) / len(clips),
                   force=True)
        try:
            cdir = work / name
            cdir.mkdir(exist_ok=True)
            if url_mode:
                vsrc = source.download_section(info["url"], abs_s, abs_e, cdir / "source.mp4", st.max_height)
                ss = None
                a_start, a_end = 0.0, media_info(vsrc)["duration"]
            else:
                vsrc, ss = path, (abs_s, abs_e)
                a_start, a_end = abs_s, abs_e
            mi = media_info(vsrc)
            if not mi["has_video"]:
                raise RuntimeError("source has no video stream")
            lay = analyze(vsrc, a_start, a_end, mi["width"], mi["height"], st.layout, st.fallback_layout, st.facecam)
            log(f"    layout: {lay.mode} ({lay.note})")
            words = words_in_range(transcript, c.start, c.end)
            ass = build_ass(words, c.end - c.start, cdir / "captions.ass", lay.caption_y, st.font,
                            hook=c.hook if st.hook_text else "")
            out_mp4 = out_dir / f"{name}.mp4"
            render(vsrc, out_mp4, lay, ass, mi["has_audio"],
                   start=ss[0] if ss else None, end=ss[1] if ss else None)
            meta = {**c.to_dict(), "start": round(abs_s, 2), "end": round(abs_e, 2),
                    "file": out_mp4.name, "layout": lay.mode,
                    "source_title": info["title"], "source_url": info["url"],
                    "caption": f"{c.title} {' '.join(c.hashtags)}".strip(),
                    "transcript": " ".join(w["word"] for w in words).capitalize()}
            write_json(out_dir / f"{name}.json", meta)
            results.append(meta)
            written.append(out_mp4)
            job.event("clip", c.title or name, file=f"{out_dir.name}/{out_mp4.name}", layout=lay.mode)
        except Exception as exc:  # noqa: BLE001 - keep going with the other clips
            log(f"    clip failed: {exc}")
            job.event("error", f"{name} failed: {str(exc)[:200]}")

    write_json(out_dir / "index.json", {"source": info, "claude_usage": dict(USAGE), "clips": results})
    if USAGE["requests"] and results:
        log(f"Claude usage for this video: {USAGE['input_tokens']:,} in + {USAGE['output_tokens']:,} out tokens"
            + (f", ≈ ${USAGE['cost_usd']:.3f} at API prices (${USAGE['cost_usd'] / len(results):.3f} per clip)"
               if USAGE["cost_usd"] else ""))
    posts = [f"# {info['title'] or vid}\n"]
    for m in results:
        posts.append(f"## {m['file']}  ({m['end'] - m['start']:.0f}s, score {m['score']}, by {m['picked_by']})\n"
                     f"{m['caption']}\n\n> {m['reason']}\n")
    (out_dir / "posts.md").write_text("\n".join(posts), encoding="utf-8")

    if not st.keep_work:  # media is big; keep only the small transcript/loudness cache
        for p in work.iterdir():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.name.startswith("audio."):
                p.unlink(missing_ok=True)
    job.update(progress=1.0)
    log(f"Done: {len(written)}/{len(clips)} clips in {out_dir} ({(time.time() - t0) / 60:.1f} min)")
    return written
