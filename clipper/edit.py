"""Tighter edits: keep the reaction at the end, and cut dead air.

A clip picked from the transcript ends on the last word, which often cuts off the laugh or scream
that follows (Whisper doesn't transcribe laughter). And it keeps every pause. Shorts are judged on
the first seconds and on how much of the clip people watch, so:

  * the end is pushed past the last word while the audio is still loud (the reaction), up to 3 s;
  * silent, still stretches are cut out: a lead-in before the first word, pauses longer than
    MIN_GAP between words, and a quiet tail.

"Dead" is strict on purpose, because in gameplay a silent stretch can be the moment itself
(the car flying off the bridge before the scream). A pause is only cut if it has no words, the audio
is no louder than usual, the picture is nearly still, and it isn't the build-up right before a loud
reaction.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

MIN_GAP = 0.8        # pauses shorter than this are left alone (natural speech rhythm)
KEEP = 0.15          # seconds kept on each side of the words around a cut
QUIET_Z = 1.0        # audio no louder than usual (robust z-score of loudness)
LOUD_Z = 2.5         # a reaction
STILL = 3.0          # mean frame difference (0-255 grey, 64x36 at 5 fps) below this = nearly still
MOTION_FPS = 5.0
TAIL_MAX = 3.0


def motion(path: Path, start: float, dur: float) -> np.ndarray:
    """How much the picture changes, per 1/MOTION_FPS s (mean absolute grey difference)."""
    w, h = 64, 36
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path), "-an",
         "-vf", f"fps={MOTION_FPS},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    n = len(r.stdout) // (w * h)
    if n < 2:
        return np.zeros(0)
    fr = np.frombuffer(r.stdout[:n * w * h], dtype=np.uint8).reshape(n, h, w).astype(np.int16)
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
    return np.concatenate([d[:1], d])  # one value per frame


def _window(arr: np.ndarray, rate: float, a: float, b: float, fn=np.max, default: float = 0.0) -> float:
    if arr is None or arr.size == 0:
        return default
    i0, i1 = max(0, int(a * rate)), min(arr.size, int(np.ceil(b * rate)))
    return float(fn(arr[i0:i1])) if i1 > i0 else default


def extend_end(z: np.ndarray, hop: float, end: float, limit: float) -> float:
    """Push the clip end past the last word while the reaction (loud audio) is still going."""
    if z.size == 0 or _window(z, 1 / hop, end, end + 1.5) < 1.5:
        return end
    t = end
    while t + hop <= min(limit, end + TAIL_MAX) and _window(z, 1 / hop, t, t + 1.0) >= 1.0:
        t += hop
    return min(limit, t + 0.3)


def plan(words: list[dict], dur: float, z: np.ndarray, hop: float, mot: np.ndarray,
         min_keep: float = 10.0) -> list[tuple[float, float]]:
    """Ranges of the clip to keep (clip-relative seconds). z is loudness per hop from the clip
    start, mot is motion() from the clip start. Returns [(0, dur)] when nothing should be cut."""
    if not words:
        return [(0.0, dur)]
    zr, mr = 1 / hop, MOTION_FPS

    def dead(a: float, b: float) -> bool:
        return (b - a >= MIN_GAP
                and _window(z, zr, a, b) < QUIET_Z
                and _window(mot, mr, a, b, np.mean, default=99) < STILL
                and _window(z, zr, b, b + 1.5) < LOUD_Z)  # not the build-up to a reaction

    cuts: list[tuple[float, float]] = []
    first, last = words[0]["start"], words[-1]["end"]
    if dead(0.0, first):
        cuts.append((0.0, max(0.0, first - 0.25)))
    for w0, w1 in zip(words, words[1:]):
        a, b = w0["end"], w1["start"]
        if dead(a, b):
            cuts.append((a + KEEP, b - KEEP))
    if dur - last > 1.0 and dead(last, dur):
        cuts.append((last + 0.5, dur))
    cuts = [(a, b) for a, b in cuts if b - a > 0.2]

    # Never cut a clip down below min_keep: undo the smallest cuts first.
    while cuts and dur - sum(b - a for a, b in cuts) < min(min_keep, dur):
        cuts.remove(min(cuts, key=lambda c: c[1] - c[0]))
    keep, t = [], 0.0
    for a, b in sorted(cuts):
        if a - t > 0.1:
            keep.append((round(float(t), 3), round(float(a), 3)))
        t = b
    if dur - t > 0.1:
        keep.append((round(float(t), 3), round(float(dur), 3)))
    return keep or [(0.0, dur)]


def remap(t: float, keep: list[tuple[float, float]]) -> float | None:
    """Where clip time t lands after the cuts (None if it was cut out)."""
    out = 0.0
    for a, b in keep:
        if t < a:
            return None
        if t <= b:
            return out + t - a
        out += b - a
    return None


def remap_words(words: list[dict], keep: list[tuple[float, float]]) -> list[dict]:
    if not keep or (len(keep) == 1 and keep[0][0] == 0.0):
        return words
    out = []
    for w in words:
        s, e = remap(w["start"], keep), remap(w["end"], keep)
        if s is None and e is None:
            continue
        s = e - min(0.3, w["end"] - w["start"]) if s is None else s
        e = s + min(0.3, w["end"] - w["start"]) if e is None else e
        out.append({**w, "start": round(s, 3), "end": round(max(e, s + 0.05), 3)})
    return out


def kept(keep: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in keep)
