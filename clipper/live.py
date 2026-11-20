"""Live mode: clip a stream while it is still live.

  streamlink ──► ffmpeg ──► rolling buffer of short .ts segments on disk
                               │ (each new segment, ~every 30 s)
                               ▼
        transcribe (model stays loaded) + loudness + live-chat rate
                               │
            ┌──────────────────┴───────────────────┐
     hype spike (loud audio or chat burst)    Claude pass every N minutes over
     → Claude confirms one clip around it      the last M minutes of transcript
            └──────────────────┬───────────────────┘
                               ▼
        cut from the buffer → reframe → captions → output/live-…/clip_NNN.mp4
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import source
from .captions import build_ass, words_in_range
from .config import ROOT, Settings
from .highlights import USAGE, SYSTEM, Clip, Unit, _snap, _via_api, _via_cli, build_units, choose_backend, transcript_for_llm
from .reframe import analyze
from .render import render
from .signals import HOP, loudness_curve
from .status import Job
from .util import CmdError, fmt_ts, log, media_info, run, slugify, write_json, ytdlp_cmd

CHAT_LAG = 10       # seconds of newest chat that are still incomplete
START_FREE = 1.5e9  # don't start recording with less free disk than this
CLIP_FREE = 1.0e9   # below this much free disk, stop saving new clips
STOP_FREE = 0.6e9   # below this, end the session so the disk never fills up

MARKERS = ("Transcript, one line per sentence, times in seconds. (LOUD) marks unusually loud audio such as "
           "yelling, laughter, gunfire or explosions. (CHAT SPIKE) marks a burst of live-chat messages, "
           "usually a few seconds after something big happened.")


@dataclass
class Seg:
    path: Path
    g0: float      # start on the stream timeline (seconds since recording began)
    g1: float
    wall: float    # wall-clock time this segment finished arriving


def robust_z(x: np.ndarray, tail: int = 7200) -> np.ndarray:
    """z-score against the last hour, robust to a streamer who is always loud."""
    if x.size == 0:
        return x.astype(np.float64)
    ref = x[-tail:]
    med = np.median(ref)
    mad = np.median(np.abs(ref - med)) * 1.4826
    if mad < 1e-6:
        mad = ref.std() + 1e-6
    return (x - med) / mad


def is_live(url: str) -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "streamlink", "--json", url],
                           capture_output=True, text=True, timeout=120)
        return bool(json.loads(r.stdout or "{}").get("streams"))
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return False


def _concat(paths: list[Path], dest: Path, args: list[str]) -> Path:
    lst = dest.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in paths))
    try:
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, *args, dest])
    finally:
        lst.unlink(missing_ok=True)
    return dest


def _reaper(pids: list[int]) -> None:
    """Kill these helpers if this process ever dies without cleaning up (e.g. force-quit),
    so a recorder can't keep filling the disk on its own."""
    subprocess.Popen(["/bin/sh", "-c", f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 5; done; "
                      f"kill {' '.join(map(str, pids))} 2>/dev/null"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


# ---------------------------------------------------------------- recording

class Recorder:
    def __init__(self, url: str, quality: str, seg_secs: int, buf: Path):
        self.url, self.quality, self.seg_secs, self.buf = url, quality, seg_secs, buf
        self.runs: list[Path] = []
        self.procs: list[subprocess.Popen] = []
        self.seen: set[tuple[str, str]] = set()

    def start(self) -> None:
        d = self.buf / f"run{len(self.runs) + 1}"
        d.mkdir(parents=True, exist_ok=True)
        self.runs.append(d)
        # start_new_session: Ctrl-C in the terminal shouldn't kill these mid-segment; we stop them.
        sl = subprocess.Popen([sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "error",
                               self.url, self.quality],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        ff = subprocess.Popen(["ffmpeg", "-v", "error", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?",
                               "-c", "copy", "-f", "segment", "-segment_time", str(self.seg_secs),
                               "-segment_format", "mpegts", "-segment_list", str(d / "segments.csv"),
                               "-segment_list_type", "csv", "-reset_timestamps", "1", str(d / "seg_%06d.ts")],
                              stdin=sl.stdout, stderr=subprocess.DEVNULL, start_new_session=True)
        assert sl.stdout
        sl.stdout.close()
        self.procs = [sl, ff]
        _reaper([sl.pid, ff.pid])

    def alive(self) -> bool:
        return bool(self.procs) and self.procs[-1].poll() is None

    def stop(self) -> None:
        _stop(self.procs)

    def new_segments(self) -> list[tuple[Path, float]]:
        out = []
        for d in self.runs:
            csv = d / "segments.csv"
            if not csv.exists():
                continue
            text = csv.read_text()
            lines = text.splitlines()
            if text and not text.endswith("\n"):
                lines = lines[:-1]  # last line still being written
            for line in lines:
                parts = line.split(",")
                if len(parts) < 3 or (d.name, parts[0]) in self.seen:
                    continue
                self.seen.add((d.name, parts[0]))
                dur = float(parts[2]) - float(parts[1])
                if dur > 0.2 and (d / parts[0]).exists():
                    out.append((d / parts[0], dur))
        return out


# ---------------------------------------------------------------- live chat

class Chat:
    """Collects the wall-clock time of every chat message. Twitch: anonymous IRC. YouTube: yt-dlp."""

    def __init__(self, url: str, buf: Path):
        self.url, self.buf = url, buf
        self.times: list[float] = []
        self.lock = threading.Lock()
        self.stopping = False
        m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url)
        self.twitch = m.group(1).lower() if m and m.group(1).lower() != "videos" else None
        self.kind = "twitch" if self.twitch else ("youtube" if "youtu" in url else None)

    def start(self) -> None:
        if self.kind:
            target = self._twitch if self.kind == "twitch" else self._youtube
            threading.Thread(target=target, daemon=True).start()

    def stop(self) -> None:
        self.stopping = True

    def _add(self, ts: float) -> None:
        with self.lock:
            self.times.append(ts)
            if len(self.times) > 400_000:
                self.times = self.times[-200_000:]

    def counts(self, offset: float, n: int) -> np.ndarray:
        """Messages per loudness hop on the stream timeline (stream time = wall - offset)."""
        with self.lock:
            ts = np.array(self.times, dtype=np.float64)
        if ts.size == 0 or n == 0:
            return np.zeros(n)
        idx = ((ts - offset) / HOP).astype(np.int64)
        idx = idx[(idx >= 0) & (idx < n)]
        return np.bincount(idx, minlength=n).astype(np.float64)

    def _twitch(self) -> None:
        while not self.stopping:
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=60)
                s.sendall(f"PASS SCHMOOPIIE\r\nNICK justinfan{random.randint(10000, 99999)}\r\n"
                          f"JOIN #{self.twitch}\r\n".encode())
                pending = b""
                while not self.stopping:
                    data = s.recv(65536)
                    if not data:
                        break
                    *lines, pending = (pending + data).split(b"\r\n")
                    now = time.time()
                    for ln in lines:
                        if ln.startswith(b"PING"):
                            s.sendall(b"PONG" + ln[4:] + b"\r\n")
                        elif b" PRIVMSG " in ln:
                            self._add(now)
                s.close()
            except OSError:
                time.sleep(5)

    def _youtube(self) -> None:
        # yt-dlp appends every message to a JSON-lines file (~75 MB/hour on a big stream),
        # so restart it every 10 minutes and delete the old file.
        while not self.stopping:
            stem = self.buf / f"chat{int(time.time())}"
            p = subprocess.Popen([*ytdlp_cmd(), "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                                  "--no-warnings", "-o", str(stem), self.url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            _reaper([p.pid])
            started, pos = time.time(), 0
            part, done = Path(f"{stem}.live_chat.json.part"), Path(f"{stem}.live_chat.json")
            while not self.stopping and time.time() - started < 600 and p.poll() is None:
                time.sleep(2)
                path = part if part.exists() else done
                if not path.exists():
                    continue
                with open(path, "rb") as fh:
                    fh.seek(pos)
                    data = fh.read()
                cut = data.rfind(b"\n")
                if cut < 0:
                    continue
                pos += cut + 1
                for ln in data[:cut + 1].splitlines():
                    m = re.search(rb'"timestampUsec":\s*"(\d+)"', ln)
                    if m:
                        self._add(int(m.group(1)) / 1e6)
            early_exit = p.poll() is not None and time.time() - started < 60
            _stop([p])
            for f in self.buf.glob(stem.name + "*"):
                f.unlink(missing_ok=True)
            if early_exit:
                time.sleep(30)


# ---------------------------------------------------------------- the live session

class Live:
    def __init__(self, url: str, st: Settings, *, seg_secs: int = 30, review_every: float = 600,
                 lookback: float = 720, keep_minutes: float = 20, quality: str = "1080p60,1080p,720p60,720p,best",
                 spikes: bool = True, spike_threshold: float = 3.5, max_per_hour: int = 12,
                 max_minutes: float | None = None, wait: bool = False):
        self.url, self.st = url, st
        self.seg_secs, self.review_every, self.lookback = seg_secs, review_every, lookback
        self.keep_secs, self.quality = keep_minutes * 60, quality
        self.spikes, self.threshold, self.max_per_hour = spikes, spike_threshold, max_per_hour
        self.max_secs = max_minutes * 60 if max_minutes else None
        self.wait = wait
        self.backend = choose_backend(st)

        self.title = ""
        self.segs: list[Seg] = []
        self.transcript: dict = {"segments": []}
        self.loud = np.zeros(0, dtype=np.float64)
        self.clipped: list[tuple[float, float]] = []
        self.pending: list[float] = []
        self.last_review = 0.0
        self.last_spike_wall = -1e9
        self.clip_walls: list[float] = []
        self.n = 0
        self.stopping = False
        self.worker: subprocess.Popen | None = None
        self.req = 0

        # Dashboard state
        self.job: Job | None = None
        self.activity: dict[str, dict] = {}
        self.spike_log: list[dict] = []
        self.reviews: list[dict] = []
        self.clip_log: list[dict] = []

    # ------------------------------------------------------------ setup

    @property
    def now(self) -> float:
        return self.segs[-1].g1 if self.segs else 0.0

    def _act(self, stage: str, detail: str, publish: bool = False) -> None:
        """Record what a pipeline stage just did (shown live on the dashboard)."""
        self.activity[stage] = {"t": time.time(), "detail": detail}
        if publish:
            self._publish(force=True)

    def _publish(self, force: bool = False) -> None:
        if not self.job:
            return
        zl, zc = self._hype() if self.loud.size else (np.zeros(0), None)
        start = max(0, len(zl) - int(900 / HOP))  # last 15 minutes

        def per_second(arr: np.ndarray) -> list[float]:
            seg = np.clip(arr[start:], -4, 15)
            m = len(seg) // 2 * 2
            return [round(float(v), 2) for v in seg[:m].reshape(-1, 2).max(axis=1)] if m else []

        live_segs = [s for s in self.segs if s.path.exists()]
        self.job.update(
            force=force, stream_time=round(self.now, 1), clips=self.n, usage=dict(USAGE),
            activity=self.activity, pending=len(self.pending),
            next_review=round(max(0.0, self.review_every - (self.now - self.last_review)), 0),
            buffer={"segments": len(live_segs), "bytes": sum(s.path.stat().st_size for s in live_segs),
                    "seconds": round(sum(s.g1 - s.g0 for s in live_segs), 0)},
            # Chat for the newest ~10 s hasn't arrived yet (it reaches us a few seconds late),
            # so leave it off instead of drawing a fake drop to zero.
            telemetry={"t0": start * HOP, "dt": 2 * HOP, "loud": per_second(zl),
                       "chat": per_second(zc)[:-CHAT_LAG] if zc is not None else None, "threshold": self.threshold,
                       "spikes": self.spike_log[-60:], "reviews": self.reviews[-30:], "clips": self.clip_log[-60:],
                       "transcript": [{"t": s["start"], "e": s["end"], "text": s["text"]}
                                      for s in self.transcript["segments"][-60:]]},
        )

    def _spike_outcome(self, t: float, outcome: str) -> None:
        for s in self.spike_log:
            if s["t"] == t:
                s["outcome"] = outcome

    def _dirs(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M")
        name = f"live-{slugify(self.title or self.url, 40)}-{stamp}"
        self.base = self.st.work_dir / name
        self.buf = self.base / "buffer"
        self.out = self.st.out_dir / name
        self.buf.mkdir(parents=True, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)

    def _start_worker(self) -> None:
        cmd = [sys.executable, "-m", "clipper.transcribe_worker", "--serve", "--model", self.st.whisper_model]
        if self.st.language:
            cmd += ["--language", self.st.language]
        self.worker = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, cwd=ROOT, start_new_session=True)
        _reaper([self.worker.pid])
        assert self.worker.stdout
        for line in self.worker.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("ready"):
                log(f"Transcriber ready ({msg.get('device')})")
                return
        raise RuntimeError("transcriber failed to start")

    def _transcribe(self, wav: Path, offset: float) -> list[dict]:
        if not self.worker or self.worker.poll() is not None:
            self._start_worker()
        assert self.worker and self.worker.stdin and self.worker.stdout
        self.req += 1
        self.worker.stdin.write(json.dumps({"id": self.req, "media": str(wav), "offset": offset}) + "\n")
        self.worker.stdin.flush()
        for line in self.worker.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == self.req:
                if "error" in msg:
                    log(f"  transcription error: {msg['error'][:200]}")
                    return []
                return msg["segments"]
        self.worker = None  # worker died; restarted on the next chunk
        return []

    # ------------------------------------------------------------ signals

    def _hype(self) -> tuple[np.ndarray, np.ndarray | None]:
        zl = robust_z(np.maximum(self.loud, -70.0))
        zc = None
        if self.chat.kind and self.segs:
            s = self.segs[-1]
            counts = self.chat.counts(s.wall - s.g1, len(self.loud))
            if counts.sum() >= 50:
                zc = robust_z(np.convolve(counts, np.ones(4) / 4, mode="same"))
        return zl, zc

    def _ingest(self, new: list[tuple[Path, float]]) -> None:
        first = len(self.segs)
        for path, dur in new:
            self.segs.append(Seg(path, self.now, self.now + dur, time.time()))
        a = self.segs[first].g0
        wav = self.base / "chunk.wav"
        try:
            _concat([s.path for s in self.segs[first:]], wav, ["-vn", "-ac", "1", "-ar", "16000"])
            curve = loudness_curve(wav)
            segs = self._transcribe(wav, a)
        except CmdError:
            curve, segs = np.zeros(0), []
        target = int(round(self.now / HOP))
        pad = np.full(max(0, target - len(self.loud) - len(curve)), -70.0)
        self.loud = np.concatenate([self.loud, curve, pad])[:target]
        self.transcript["segments"] += segs
        words = sum(len(s["text"].split()) for s in segs)
        log(f"Live {fmt_ts(self.now)}  +{self.now - a:.0f}s  {words} words"
            + (f"  chat {len(self.chat.times)} msgs" if self.chat.kind else ""))
        live_segs = sum(1 for s in self.segs if s.path.exists())
        self._act("recorder", f"{len(new)} new segment(s) · {live_segs} in buffer")
        self._act("transcriber", f"{words} words in the last {self.now - a:.0f}s")
        if self.chat.kind:
            with self.chat.lock:
                recent = sum(1 for t in self.chat.times[-5000:] if t > time.time() - 30)
            self._act("chat", f"{recent / 30:.1f} messages/s")
        if self.spikes:  # look back a little: late-arriving chat can complete a spike
            self._detect_spikes(max(0.0, a - 15), self.now)
        self._publish(force=True)

    def _detect_spikes(self, a: float, b: float) -> None:
        zl, zc = self._hype()
        hype = zl if zc is None else np.maximum(zl, zc - 0.5)
        i0, i1 = int(a / HOP), min(len(hype), int(b / HOP))
        run_len, best = 0, None
        for i in range(i0, i1 + 1):
            if i < i1 and hype[i] >= self.threshold:
                run_len += 1
                best = i if best is None or hype[i] > hype[best] else best
                continue
            if run_len >= 2 and best is not None:  # at least a second above threshold
                self._spike(best * HOP, float(hype[best]))
            run_len, best = 0, None

    def _spike(self, t: float, score: float) -> None:
        if any(s - 30 <= t <= e + 10 for s, e in self.clipped) or any(abs(t - p) < 45 for p in self.pending):
            return
        if time.time() - self.last_spike_wall < 90:
            return
        self.last_spike_wall = time.time()
        self.pending.append(t)
        self.spike_log.append({"t": t, "score": round(score, 1), "outcome": "pending"})
        log(f"Hype spike at {fmt_ts(t)} (score {score:.1f}); clipping once the next 20 s arrive")
        self._act("hype", f"spike at {fmt_ts(t)} (score {score:.1f})")
        if self.job:
            self.job.event("spike", f"Hype spike at {fmt_ts(t)} (score {score:.1f})", stream_t=t)

    # ------------------------------------------------------------ picking

    def _units(self, a: float, b: float) -> list[Unit]:
        segs = [s for s in self.transcript["segments"] if s["end"] >= a - 5 and s["start"] <= b]
        return [u for u in build_units({"segments": segs}) if u.start >= a and u.end <= b]

    def _call(self, prompt: str) -> list[dict] | None:
        """Claude's picks ([] = nothing worth clipping), or None if the call itself failed
        (e.g. usage limit reached), so callers can fall back instead of silently stopping."""
        try:
            return (_via_api if self.backend == "api" else _via_cli)(prompt, self.st)
        except Exception as exc:  # noqa: BLE001
            log(f"  Claude call failed: {str(exc)[:200]}")
            return None

    def _header(self) -> str:
        return f"Live stream: {self.title or self.url}\nNiche: {self.st.topic}\n"

    def _rate_ok(self) -> bool:
        recent = [w for w in self.clip_walls if time.time() - w < 3600]
        if len(recent) >= self.max_per_hour:
            log(f"  hourly cap reached ({self.max_per_hour} clips); skipping")
            return False
        return True

    def _handle_pending(self, final: bool = False) -> None:
        for t in sorted(self.pending):
            if self.now < t + 20 and not final:
                continue
            self.pending.remove(t)
            if not self._rate_ok():
                self._spike_outcome(t, "capped")
                continue
            a, b = max(0.0, t - 75), min(self.now, t + 25)
            units = self._units(a, b)
            clip = None
            if self.backend != "none" and units:
                self._act("claude", f"checking the spike at {fmt_ts(t)}", publish=True)
                zl, zc = self._hype()
                prompt = (self._header()
                          + f"A hype spike (sudden loud audio or a burst of chat) peaked at second {t:.1f}.\n"
                          f"If there is a clip-worthy moment that includes it, return exactly one clip "
                          f"({self.st.min_len:.0f}-{self.st.max_len:.0f} s) containing second {t:.0f}. "
                          "If it is not worth clipping, return an empty clips list.\n\n"
                          f"{MARKERS}\n\n{transcript_for_llm(units, zl, a, b, zc)}")
                raw = self._call(prompt)
                clip = _snap(raw[0], units, b, self.st) if raw else None
                if raw is None:  # Claude unavailable: keep clipping spikes without it
                    clip = Clip(max(0.0, t - 20), min(self.now, t + 12), hashtags=list(self.st.hashtags),
                                reason="hype spike (Claude unavailable)", picked_by="spike")
                elif clip is None:
                    log(f"  Claude passed on the spike at {fmt_ts(t)}")
                    self._act("claude", f"passed on the spike at {fmt_ts(t)}")
                    if self.job:
                        self.job.event("claude", f"Claude passed on the spike at {fmt_ts(t)}")
            elif self.backend == "none":
                clip = Clip(max(0.0, t - 20), min(self.now, t + 12), hashtags=list(self.st.hashtags),
                            reason="hype spike", picked_by="spike")
            if clip:
                clip.picked_by = "claude (spike)" if clip.picked_by == "claude" else clip.picked_by
                self._spike_outcome(t, "clipped" if self._make(clip) else "skipped")
            else:
                self._spike_outcome(t, "passed")

    def _review(self, final: bool = False) -> None:
        self.last_review = self.now
        b = self.now - (0 if final else 15)  # the last seconds may be a moment still unfolding
        a = max(0.0, b - self.lookback)
        units = self._units(a, b)
        if b - a < self.st.min_len or len(units) < 3:
            return
        taken = [(s, e) for s, e in self.clipped if e > a]
        k = 3
        zl, zc = self._hype()
        log(f"Claude pass over {fmt_ts(a)}-{fmt_ts(b)}")
        self._act("claude", f"reviewing {fmt_ts(a)}-{fmt_ts(b)}", publish=True)
        prompt = (self._header()
                  + f"This is the latest {(b - a) / 60:.0f} minutes of a stream that is still live "
                  f"(seconds {a:.0f} to {b:.0f}).\n"
                  f"Pick up to {k} clips ({self.st.min_len:.0f}-{self.st.max_len:.0f} s). Only pick moments "
                  "that are complete within this transcript. Return an empty list if nothing here is "
                  "genuinely clip-worthy.\n"
                  + (f"Already clipped, do not overlap: {', '.join(f'{s:.0f}-{e:.0f}' for s, e in taken)}\n"
                     if taken else "")
                  + f"\n{MARKERS}\n\n{transcript_for_llm(units, zl, a, b, zc)}")
        raw = self._call(prompt)
        picks = [c for c in (_snap(r, units, b, self.st) for r in (raw or [])) if c]
        if not picks:
            log("  nothing clip-worthy in this stretch")
        made = 0
        for c in sorted(picks, key=lambda c: -c.score):
            if c.end <= b + 0.5 and self._rate_ok():
                made += bool(self._make(c))
        result = "failed" if raw is None else f"{made} clip(s)"
        self.reviews.append({"a": round(a, 1), "b": round(b, 1), "result": result, "clips": made, "wall": time.time()})
        self._act("claude", f"pass over {fmt_ts(a)}-{fmt_ts(b)} → {result}")
        if self.job:
            self.job.event("claude", f"Claude pass over {fmt_ts(a)}-{fmt_ts(b)}: {result}")
        self._publish(force=True)

    # ------------------------------------------------------------ output

    def _make(self, clip: Clip) -> bool:
        clip.start = max(0.0, clip.start - 0.15)
        clip.end = min(self.now, clip.end + 0.35)
        if any(clip.start < e and clip.end > s for s, e in self.clipped):
            return False
        if shutil.disk_usage(self.base).free < CLIP_FREE:
            log(f"  disk nearly full (< 1 GB free); not saving the clip at {fmt_ts(clip.start)}")
            if self.job:
                self.job.event("error", f"Disk nearly full: skipped the clip at {fmt_ts(clip.start)}")
            return False
        segs = [s for s in self.segs if s.g1 > clip.start - 1 and s.g0 < clip.end + 1]
        if not segs or not all(s.path.exists() for s in segs) or segs[0].g0 > clip.start + 0.5:
            log(f"  buffer no longer has {fmt_ts(clip.start)}; skipped")
            return False
        self.n += 1
        name = f"clip_{self.n:03d}"
        tmp = self.base / f"{name}.mkv"
        ass = None
        ok = False
        self._act("renderer", f"rendering {name}", publish=True)
        try:
            _concat([s.path for s in segs], tmp, ["-c", "copy"])
            base = segs[0].g0
            s, e = clip.start - base, clip.end - base
            mi = media_info(tmp)
            lay = analyze(tmp, s, e, mi["width"], mi["height"], self.st.layout, self.st.fallback_layout,
                          self.st.facecam)
            words = words_in_range(self.transcript, clip.start, clip.end)
            ass = build_ass(words, e - s, self.base / f"{name}.ass", lay.caption_y, self.st.font,
                            hook=clip.hook if self.st.hook_text else "")
            out = self.out / f"{name}.mp4"
            render(tmp, out, lay, ass, mi["has_audio"], start=s, end=e)
            meta = {**clip.to_dict(), "file": out.name, "layout": lay.mode, "stream_time": fmt_ts(clip.start),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"), "source_title": self.title,
                    "source_url": self.url, "caption": f"{clip.title} {' '.join(clip.hashtags)}".strip(),
                    "transcript": " ".join(w["word"] for w in words).capitalize()}
            write_json(self.out / f"{name}.json", meta)
            with open(self.out / "posts.md", "a") as f:
                f.write(f"## {out.name}  (stream {fmt_ts(clip.start)}, {clip.end - clip.start:.0f}s, "
                        f"by {clip.picked_by})\n{meta['caption']}\n\n> {clip.reason}\n\n")
            self.clipped.append((clip.start, clip.end))
            self.clip_walls.append(time.time())
            log(f"  ✓ {name} [{fmt_ts(clip.start)}-{fmt_ts(clip.end)}] {lay.mode}: {clip.title!r}")
            rel = f"{self.out.name}/{out.name}"
            self.clip_log.append({"start": round(clip.start, 1), "end": round(clip.end, 1), "name": name,
                                  "title": clip.title, "file": rel, "layout": lay.mode, "by": clip.picked_by})
            self._act("renderer", f"{name} done ({lay.mode})")
            if self.job:
                self.job.event("clip", clip.title or name, file=rel, layout=lay.mode, stream_t=clip.start)
            ok = True
        except Exception as exc:  # noqa: BLE001
            log(f"  clip failed: {str(exc)[:300]}")
            if self.job:
                self.job.event("error", f"{name} failed: {str(exc)[:200]}")
        finally:
            tmp.unlink(missing_ok=True)
            if ass:
                ass.unlink(missing_ok=True)
        self._save()
        self._publish(force=True)
        return ok

    def _cleanup(self) -> None:
        keep_from = self.now - max(self.keep_secs, self.lookback + 60)
        if shutil.disk_usage(self.base).free < 1.0e9:  # disk nearly full: keep only what's still needed
            keep_from = self.now - (self.lookback + 60)
        if self.pending:
            keep_from = min(keep_from, min(self.pending) - 90)
        for s in self.segs:
            if s.g1 < keep_from and s.path.exists():
                s.path.unlink()

    def _save(self) -> None:
        write_json(self.base / "live_state.json", {
            "url": self.url, "title": self.title, "stream_seconds": round(self.now, 1),
            "clips": self.n, "clipped": self.clipped, "claude_usage": dict(USAGE)})

    # ------------------------------------------------------------ main loop

    def run(self) -> None:
        self.job = Job("live", self.url, url=self.url, state="waiting", stage="Waiting for the stream to go live")
        try:
            self._run()
        except Exception as exc:
            self.job.finish("failed", str(exc)[:300])
            raise

    def _run(self) -> None:
        assert self.job
        announced = False
        while not is_live(self.url):
            if not self.wait:
                raise RuntimeError("stream is not live (use --wait to wait for it)")
            if not announced:  # one line, not one every 2 minutes for days
                log(f"Waiting for {self.url} to go live (checking every 2 minutes)")
                announced = True
            self.job.update(force=True, last_check=time.time())
            time.sleep(120)
        # Don't start (and get restarted by launchd into a stop/start loop) on a nearly full disk.
        paused = False
        while shutil.disk_usage(self.st.work_dir).free < START_FREE:
            free = shutil.disk_usage(self.st.work_dir).free / 1e9
            if not paused:
                log(f"Only {free:.1f} GB free; waiting for at least {START_FREE / 1e9:.1f} GB before recording")
                self.job.event("error", f"Paused: only {free:.1f} GB free disk (needs {START_FREE / 1e9:.1f} GB)")
                paused = True
            self.job.update(force=True, state="waiting", stage=f"Paused: only {free:.1f} GB free disk",
                            last_check=time.time())
            time.sleep(120)
            if not is_live(self.url):
                return  # stream ended while paused; launchd restarts us to wait for the next one
        try:
            self.title = source.probe(self.url).get("title", "")
        except Exception:  # noqa: BLE001 - title is cosmetic
            self.title = ""
        self._dirs()
        self.job.update(force=True, title=self.title or self.url, state="running", stage="Recording live",
                        out=str(self.out), live_since=time.time(), backend=self.backend)
        self.job.event("info", f"Stream is live: {self.title or self.url}")
        # Keep a Mac from idle-sleeping while a stream is being recorded (macOS only).
        caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) \
            if shutil.which("caffeinate") else None
        USAGE.update(requests=0, input_tokens=0, output_tokens=0, cost_usd=0.0)
        log(f"Live clipping: {self.title or self.url}")
        log(f"Picker: {self.backend}. Clips go to {self.out}")
        self._start_worker()
        self.rec = Recorder(self.url, self.quality, self.seg_secs, self.buf)
        self.chat = Chat(self.url, self.buf)
        self.rec.start()
        self.chat.start()
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        restarts, t_start = 0, time.time()
        try:
            while True:
                new = self.rec.new_segments()
                if new:
                    self._ingest(new)
                self._handle_pending()
                if self.backend != "none" and self.now - self.last_review >= self.review_every:
                    self._review()
                self._cleanup()
                if self.stopping or (self.max_secs and time.time() - t_start >= self.max_secs):
                    log("Stopping")
                    break
                if shutil.disk_usage(self.base).free < STOP_FREE:
                    log("Disk almost full (< 0.6 GB free); stopping this session")
                    break
                if not self.rec.alive():
                    new = self.rec.new_segments()
                    if new:
                        self._ingest(new)
                    if restarts < 30 and is_live(self.url):
                        restarts += 1
                        log("Recorder dropped; reconnecting")
                        self.job.event("error", "Recorder dropped; reconnecting")
                        self.rec.start()
                        continue
                    log("Stream ended")
                    break
                time.sleep(2)
        finally:
            self.rec.stop()
            new = self.rec.new_segments()
            if new:
                self._ingest(new)
            self._handle_pending(final=True)
            if self.backend != "none" and self.now - self.last_review >= 60:
                self._review(final=True)
            self.chat.stop()
            if self.worker:
                _stop([self.worker])
            if caffeinate:
                _stop([caffeinate])
            self._save()
            if not self.st.keep_work:
                shutil.rmtree(self.buf, ignore_errors=True)
            log(f"Live session done: {self.n} clip(s) from {fmt_ts(self.now)} of stream in {self.out}")
            self._publish(force=True)
            self.job.finish("done", f"{self.n} clip(s) from {fmt_ts(self.now)} of stream")
            if USAGE["requests"]:
                log(f"Claude usage: {USAGE['requests']} requests, {USAGE['input_tokens']:,} in + "
                    f"{USAGE['output_tokens']:,} out tokens"
                    + (f", ≈ ${USAGE['cost_usd']:.2f} at API prices" if USAGE["cost_usd"] else ""))
