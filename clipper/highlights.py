"""Choosing which moments become clips.

Two pickers:
  * heuristic - loudness peaks + reaction words + speech density. Free, offline.
  * Claude    - reads the timestamped transcript (with loudness markers) and picks
                self-contained moments with a hook, plus titles and hashtags.
Claude runs through the Anthropic API when ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN is
set, otherwise through the local `claude` CLI (your Claude Code login). If both are
unavailable or fail, the heuristic picker is used.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field

import numpy as np

from .config import Settings
from .signals import loud_regions, window_peak
from .util import log

EXCITE = re.compile(
    r"\b(oh my god|omg|no way|what the|wtf|holy|let'?s go+|insane|crazy|bro+|dude|are you (kidding|serious)"
    r"|clip (it|that)|oh no|no no|ha(ha)+|lmao|lol|bruh|sheesh|yo+|wait|run|help|get out|yes+)\b",
    re.I,
)


@dataclass
class Unit:
    start: float
    end: float
    text: str
    nwords: int


@dataclass
class Clip:
    start: float
    end: float
    title: str = ""
    hook: str = ""
    hashtags: list[str] = field(default_factory=list)
    score: float = 0.0
    reason: str = ""
    picked_by: str = "heuristic"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = round(self.start, 2), round(self.end, 2)
        return d


# ---------------------------------------------------------------- transcript units

def build_units(transcript: dict) -> list[Unit]:
    """Sentence-sized pieces with exact word timings; clip boundaries snap to these."""
    words = [w for s in transcript.get("segments", []) for w in s.get("words", []) if w["word"].strip()]
    if not words:
        return [Unit(s["start"], s["end"], s["text"], len(s["text"].split()))
                for s in transcript.get("segments", []) if s["text"].strip()]
    units, cur = [], []

    def flush():
        if cur:
            text = "".join(w["word"] for w in cur).strip()
            units.append(Unit(cur[0]["start"], cur[-1]["end"], text, len(cur)))
            cur.clear()

    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > 0.7 or w["end"] - cur[0]["start"] > 12):
            flush()
        cur.append(w)
        if w["word"].strip().endswith((".", "?", "!")):
            flush()
    flush()
    return units


def auto_clip_count(duration: float) -> int:
    return int(min(12, max(1, math.ceil(duration / 600))))


# ---------------------------------------------------------------- heuristic picker

def _standardize(col: np.ndarray) -> np.ndarray:
    sd = col.std()
    return (col - col.mean()) / sd if sd > 1e-9 else np.zeros_like(col)


def _greedy(cands: list[tuple[float, float, float]], n: int, gap: float = 3.0) -> list[tuple[float, float, float]]:
    chosen: list[tuple[float, float, float]] = []
    for s, e, sc in sorted(cands, key=lambda c: -c[2]):
        if all(e + gap <= cs or s >= ce + gap for cs, ce, _ in chosen):
            chosen.append((s, e, sc))
        if len(chosen) >= n:
            break
    return chosen


def heuristic_pick(units: list[Unit], z: np.ndarray, duration: float, n: int, st: Settings) -> list[Clip]:
    speech_secs = sum(u.end - u.start for u in units)
    if len(units) < 5 or speech_secs < 0.1 * duration:
        return _loudness_only(z, duration, n, st)

    targets = sorted({st.min_len + (st.max_len - st.min_len) * f for f in (0.15, 0.5, 0.9)})
    pairs = set()
    for i in range(len(units)):
        j = i
        for t in targets:
            while j < len(units) - 1 and units[j].end - units[i].start < t:
                j += 1
            d = units[j].end - units[i].start
            if st.min_len <= d <= st.max_len:
                pairs.add((i, j))
    if not pairs:
        return _loudness_only(z, duration, n, st)

    pairs = sorted(pairs)
    feats, spans = [], []
    for i, j in pairs:
        s, e = units[i].start, units[j].end
        d = e - s
        text = " ".join(u.text for u in units[i:j + 1])
        nw = sum(u.nwords for u in units[i:j + 1])
        feats.append([
            window_peak(z, s, e),                        # loud moments
            len(EXCITE.findall(text)) / d * 60,          # reaction words per minute
            (text.count("!") + text.count("?")) / d * 60,
            window_peak(z, s, min(e, s + 3)),            # loud opening = hook
            min(nw / d, 3.5),                            # keeps talking, no dead air
        ])
        spans.append((s, e, nw / d, i))
    F = np.array(feats, dtype=np.float64)
    weights = np.array([1.0, 0.8, 0.3, 0.4, 0.3])
    score = np.column_stack([_standardize(F[:, k]) for k in range(F.shape[1])]) @ weights
    cands = []
    for (s, e, wps, i), sc in zip(spans, score):
        if wps < 1.0:
            sc -= 1.5
        cands.append((s, e, float(sc)))

    clips = []
    for s, e, sc in _greedy(cands, n):
        first = next(u for u in units if u.start >= s - 1e-6)
        title = first.text if len(first.text) <= 70 else first.text[:67].rsplit(" ", 1)[0] + "..."
        clips.append(Clip(s, e, title=title, hashtags=list(st.hashtags), score=round(sc, 2),
                          reason="loudness + reaction-word score"))
    return clips


def _loudness_only(z: np.ndarray, duration: float, n: int, st: Settings) -> list[Clip]:
    """No commentary (pure gameplay): pick the loudest stretches."""
    length = min(st.max_len, max(st.min_len, 30.0))
    cands, t = [], 0.0
    while t + length <= duration:
        cands.append((t, t + length, window_peak(z, t, t + length) + 0.5 * window_peak(z, t, t + 3)))
        t += 5.0
    if not cands and duration > 0:
        cands = [(0.0, min(duration, st.max_len), 0.0)]
    return [Clip(s, e, title="", hashtags=list(st.hashtags), score=round(sc, 2), reason="loudest stretch")
            for s, e, sc in _greedy(cands, n)]


# ---------------------------------------------------------------- Claude picker

SYSTEM = """You are a senior short-form video editor. You cut long gameplay videos and \
livestream VODs into clips for TikTok, Instagram Reels and YouTube Shorts.

What makes a clip work:
- It opens on a hook: the first one or two seconds grab attention (a shout, a funny line, \
a surprising event, a question). Never open on filler ("so", "um", "anyway") or mid-thought.
- It is self-contained: a viewer with no context understands it.
- It ends on a payoff: the punchline, reaction, result or twist. Never cut mid-sentence.
- High energy beats slow explanation. Strong reactions, chaos, funny fails, clutch plays, \
and surprising moments do best.

Rules:
- Use only start and end times that appear in the transcript lines (in seconds).
- Respect the requested minimum and maximum length for every clip.
- Clips must not overlap. Rank them best first. Return fewer clips if there are not enough \
genuinely good moments; do not pad with weak ones.
- title: under 70 characters, curiosity-driven, accurate to what happens (no fake clickbait).
- hook: 2 to 6 words of on-screen text for the first seconds, in the creator's voice.
- hashtags: 3 to 6 relevant tags, each starting with #.
- score: 1-100, your estimate of how well the clip will perform.
- reason: one short sentence on why it works."""

SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["start", "end", "title", "hook", "hashtags", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def transcript_for_llm(units: list[Unit], z: np.ndarray, a: float = 0.0, b: float = float("inf"),
                       chat: np.ndarray | None = None) -> str:
    def tags(s: float, e: float) -> str:
        t = " (LOUD)" if window_peak(z, s, e) > 2.0 else ""
        if chat is not None and chat.size and window_peak(chat, s, e + 4) > 2.5:  # chat reacts a few s late
            t += " (CHAT SPIKE)"
        return t

    rows = [(u.start, f"[{u.start:.1f}-{u.end:.1f}]{tags(u.start, u.end)} {u.text}") for u in units]
    # Loud stretches with no speech (gunfire, crashes, explosions) are moments too.
    spans = [(u.start, u.end) for u in units]
    k = 0
    for s, e in loud_regions(z):
        if s < a or s >= b:
            continue
        while k < len(spans) and spans[k][1] < s:
            k += 1
        overlaps = k < len(spans) and spans[k][0] < e
        if not overlaps:
            rows.append((s, f"[{s:.1f}-{e:.1f}] (LOUD, no speech)"))
    rows.sort(key=lambda r: r[0])
    return "\n".join(r[1] for r in rows)


CHUNK_SECS = 2.5 * 3600     # the claude CLI has a 200k-token window; ~2.5h of talk is ~45k tokens
CHUNK_CHARS = 350_000


def _chunks(units: list[Unit]) -> list[list[Unit]]:
    out, cur, chars = [], [], 0
    for u in units:
        if cur and (u.end - cur[0].start > CHUNK_SECS or chars + len(u.text) + 20 > CHUNK_CHARS):
            out.append(cur)
            cur, chars = [], 0
        cur.append(u)
        chars += len(u.text) + 20
    if cur:
        out.append(cur)
    return out


def _prompt(title: str, duration: float, n: int, st: Settings, transcript_text: str, part: str = "") -> str:
    return (
        f"Video title: {title or 'unknown'}\n"
        f"Niche: {st.topic}\n"
        f"Video length: {duration:.0f} seconds{part}\n"
        f"Pick up to {n} clips. Each clip must be {st.min_len:.0f} to {st.max_len:.0f} seconds long.\n\n"
        "Transcript, one line per sentence, times in seconds. (LOUD) marks unusually loud audio "
        "such as yelling, laughter, gunfire or explosions. (CHAT SPIKE), when present, marks a burst "
        "of live-chat messages, usually a few seconds after something big happened:\n\n"
        f"{transcript_text}"
    )


USAGE = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def _record(inp: int, outp: int, cost: float | None) -> None:
    USAGE["requests"] += 1
    USAGE["input_tokens"] += int(inp)
    USAGE["output_tokens"] += int(outp)
    USAGE["cost_usd"] += float(cost or 0)
    log(f"  Claude usage: {inp:,} input + {outp:,} output tokens"
        + (f" (≈ ${cost:.3f} at API prices)" if cost else ""))


def _via_api(prompt: str, st: Settings) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=st.api_model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    u = resp.usage
    cost = (u.input_tokens * 4 + u.output_tokens * 20) / 1e6 if st.api_model == "claude-opus-5-5" else None
    _record(u.input_tokens, u.output_tokens, cost)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens)")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["clips"]


def _via_cli(prompt: str, st: Settings) -> list[dict]:
    cmd = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--tools", "", "--model", st.cli_model, "--system-prompt", SYSTEM,
           "--no-session-persistence", "--strict-mcp-config"]
    with tempfile.TemporaryDirectory() as tmp:  # neutral cwd: no project CLAUDE.md
        res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp, timeout=1800)
    try:
        out = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude CLI failed: {(res.stderr or res.stdout)[-800:]}") from None
    u = out.get("usage") or {}
    _record((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0), u.get("output_tokens") or 0, out.get("total_cost_usd"))
    # The CLI can flag is_error / exit 1 even when it returns a valid structured answer.
    data = out.get("structured_output")
    if not data and (res.returncode != 0 or out.get("is_error")):
        raise RuntimeError(f"claude CLI error: {str(out.get('result') or res.stderr)[:800]}")
    if not data:
        txt = out.get("result") or ""
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise RuntimeError("claude CLI returned no JSON")
        data = json.loads(m.group(0))
    return data["clips"]


def choose_backend(st: Settings) -> str:
    if st.llm != "auto":
        return st.llm
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "api"
    if shutil.which("claude"):
        return "claude-cli"
    return "none"


def _snap(raw: dict, units: list[Unit], duration: float, st: Settings) -> Clip | None:
    try:
        s, e = float(raw["start"]), float(raw["end"])
    except (KeyError, TypeError, ValueError):
        return None
    s, e = max(0.0, min(s, duration)), max(0.0, min(e, duration))
    if e <= s:
        return None
    if units:
        starts = np.array([u.start for u in units])
        ends = np.array([u.end for u in units])
        i = int(np.argmin(np.abs(starts - s)))
        j = int(np.argmin(np.abs(ends - e)))
        if abs(starts[i] - s) <= 3:
            s = float(starts[i])
        if abs(ends[j] - e) <= 3:
            e = float(ends[j])
    if e - s > st.max_len:
        e = s + st.max_len
    if e - s < st.min_len:
        e = min(duration, s + st.min_len)
    if e - s < min(st.min_len, 5.0):
        return None
    tags = [t if t.startswith("#") else f"#{t}" for t in raw.get("hashtags", []) if t.strip()]
    return Clip(s, e, title=str(raw.get("title", ""))[:100], hook=str(raw.get("hook", ""))[:60],
                hashtags=tags or list(st.hashtags), score=float(raw.get("score", 0)),
                reason=str(raw.get("reason", "")), picked_by="claude")


# ---------------------------------------------------------------- entry point

def pick_clips(transcript: dict, z: np.ndarray, duration: float, st: Settings, title: str = "") -> list[Clip]:
    USAGE.update(requests=0, input_tokens=0, output_tokens=0, cost_usd=0.0)
    units = build_units(transcript)
    n = st.clips or auto_clip_count(duration)
    backend = choose_backend(st)
    clips: list[Clip] = []

    if backend in ("api", "claude-cli") and units:
        call = _via_api if backend == "api" else _via_cli
        chunks = _chunks(units)
        log(f"Asking Claude ({'API' if backend == 'api' else 'claude CLI'}) to pick up to {n} clips"
            + (f" across {len(chunks)} parts" if len(chunks) > 1 else ""))
        try:
            raw = []
            for k, cu in enumerate(chunks, 1):
                a = cu[0].start
                b = chunks[k][0].start if k < len(chunks) else duration
                if len(chunks) == 1:
                    raw += call(_prompt(title, duration, n, st, transcript_for_llm(cu, z)), st)
                    continue
                share = max(2, math.ceil(n * (b - a) / duration) + 1)  # a little extra; best overall win
                part = f" (this is part {k} of {len(chunks)}: seconds {a:.0f} to {b:.0f})"
                log(f"  part {k}/{len(chunks)}")
                raw += call(_prompt(title, duration, share, st, transcript_for_llm(cu, z, a, b), part), st)
            cands = [c for c in (_snap(r, units, duration, st) for r in raw) if c]
            for c in sorted(cands, key=lambda c: -c.score):  # clip_01 = best
                if all(c.end <= o.start or c.start >= o.end for o in clips):
                    clips.append(c)
            clips = clips[:n]
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the heuristic
            log(f"Claude picker failed ({exc}); falling back to heuristic picker")
            clips = []

    if not clips:
        clips = heuristic_pick(units, z, duration, n, st)

    # Breathing room: don't clip the first syllable or the last word.
    for c in clips:
        c.start = max(0.0, c.start - 0.15)
        c.end = min(duration, c.end + 0.35)
    return clips
