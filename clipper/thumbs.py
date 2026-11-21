"""Designed thumbnails for Shorts: the best reaction frame + big punchy text.

The frame: candidates at the loudest moments of the clip (the reaction) plus its middle, scored on
loudness, a visible face (the facecam in a stack layout), and sharpness (no motion blur).
The design: that frame in the clip's own 9:16 layout, a little extra contrast and colour, and 1-4
words of text in big outlined letters, kept inside the centre area that YouTube's 4:5 crops keep.
"""

from __future__ import annotations

import subprocess
import unicodedata
from pathlib import Path

import numpy as np

from .captions import _ts
from .reframe import DET_W, OUT_H, OUT_W, Layout, _detect, _even, video_filter
from .util import run

TEXT_Y = {"stack": 1060, "face": 1330, "crop": 1330, "zoom": 1390, "blur": 1420}
YELLOW = "&H0000E5FF&"


def _clean(text: str) -> str:
    """Upper-case, single-line, without emoji (the caption font can't draw them)."""
    keep = [c for c in text if not (unicodedata.category(c) in ("So", "Cs", "Co", "Sk") or ord(c) >= 0x1F000)]
    return " ".join("".join(keep).replace("{", "").replace("}", "").replace("\\", "").upper().split())


def thumb_text(thumb: str, hook: str, title: str) -> str:
    for cand in (thumb, hook, title):
        t = _clean(cand or "")
        if t:
            words = t.split()
            return " ".join(words[:5]) if len(words) > 5 else t
    return ""


def _grab(path: Path, t: float, W: int, H: int) -> np.ndarray | None:
    h = _even(DET_W * H / W)
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-an",
                        "-vf", f"scale={DET_W}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True)
    size = DET_W * h * 3
    return np.frombuffer(r.stdout[:size], dtype=np.uint8).reshape(h, DET_W, 3) if len(r.stdout) >= size else None


def pick_time(path: Path, start: float, end: float, lay: Layout, z: np.ndarray | None, hop: float,
              z_offset: float = 0.0) -> float:
    """Best thumbnail moment, as seconds from `start`. z is loudness on a timeline where index i is
    second z_offset + i*hop (pass the stream's curve and the clip's stream start)."""
    import cv2

    dur = end - start
    lo, hi = min(0.6, dur / 4), max(dur - 0.6, dur * 3 / 4)
    grid = np.arange(lo, hi, 0.5)
    if grid.size == 0:
        return dur / 2

    def loud(t: float) -> float:
        if z is None or z.size == 0:
            return 0.0
        i = int((z_offset + t) / hop)
        return float(np.max(z[max(0, i - 1):i + 2])) if 0 <= i < z.size else 0.0

    by_loud = sorted(grid, key=lambda t: -loud(t))
    cands: list[float] = []
    for t in by_loud:
        if all(abs(t - c) >= 1.5 for c in cands):
            cands.append(float(t))
        if len(cands) >= 6:
            break
    cands.append(dur / 2)

    W, H = lay.W, lay.H
    frames, times = [], []
    for t in cands:
        fr = _grab(path, start + t, W, H)
        if fr is not None:
            frames.append(fr)
            times.append(t)
    if not frames:
        return dur / 2
    if lay.mode == "stack" and lay.box:  # judge the facecam, which fills the top of the frame
        x, y, w, h = lay.box
        sx = DET_W / W
        crops = [f[int(y * sx):int((y + h) * sx), int(x * sx):int((x + w) * sx)] for f in frames]
        crops = [cv2.resize(c, (DET_W, _even(DET_W * c.shape[0] / max(c.shape[1], 1)))) for c in crops]
    else:
        crops = frames
    dets = _detect(crops)
    # Sharpness of the gameplay (the centre of the frame; in a stack layout the facecam is sharp anyway),
    # so motion blur and scene-transition fades lose.
    fw = frames[0].shape[1]
    sharp = np.array([cv2.Laplacian(cv2.cvtColor(f[:, fw // 5:fw * 4 // 5], cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                      for f in frames])
    sharp = sharp / (sharp.max() + 1e-9)
    louds = np.array([loud(t) for t in times])
    louds = (louds - louds.min()) / (np.ptp(louds) + 1e-9) if louds.size > 1 else np.zeros(1)
    best, best_score = times[0], -1e9
    for t, d, s, l in zip(times, dets, sharp, louds):
        face = max((f[2] * f[3] * f[4] for f in d), default=0.0)
        score = 1.0 * l + (1.0 + min(face * 20, 1.0) if face else 0.0) + 1.2 * s - (0.8 if s < 0.45 else 0.0)
        if score > best_score:
            best, best_score = t, score
    return best


def _wrap(text: str, max_chars: int = 11) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > 3:  # squeeze into three lines
        return _wrap(text, max_chars + 4)
    return lines


def _ass(text: str, out: Path, y: int, font: str) -> Path:
    lines = _wrap(text)
    longest = max((len(l) for l in lines), default=1)
    size = int(min(165, 960 / (0.66 * max(longest, 4))))
    step = int(size * 1.02)
    y0 = y - step * (len(lines) - 1) // 2
    ev = []
    for i, line in enumerate(lines):
        colour = YELLOW if i == len(lines) - 1 and len(lines) > 1 else "&H00FFFFFF&"
        ev.append(f"Dialogue: 0,{_ts(0)},{_ts(30)},T,,0,0,0,,{{\\pos(540,{y0 + i * step})\\frz2\\c{colour}}}{line}")
    out.write_text("\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: T,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,2,0,1,"
        f"{max(8, size // 12)},5,5,40,40,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text", *ev,
    ]) + "\n", encoding="utf-8")
    return out


def render_thumb(path: Path, start: float, t: float, lay: Layout, text: str, out: Path, font: str) -> Path:
    """Write a 1080x1920 JPEG of the frame at start+t in the clip's layout, with `text` on it.
    The layout's crop path is a function of clip time, so the frame's timestamp is shifted to t."""
    base = video_filter(lay, None).replace("[0:v]", f"[0:v]setpts=PTS+{t:.3f}/TB,", 1).replace("[vout]", "[vr]")
    ass = _ass(text, out.with_suffix(".thumb.ass"), TEXT_Y.get(lay.mode, 1300), font) if text else None
    post = "eq=contrast=1.07:saturation=1.3:brightness=0.01,unsharp=5:5:0.5"
    if ass:
        post += f",ass={ass.name}"
    try:
        for q in (3, 6, 10):  # YouTube takes thumbnails up to 2 MB
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start + t:.3f}", "-i", str(path.resolve()),
                 "-filter_complex", f"{base};[vr]{post}[v]", "-map", "[v]", "-frames:v", "1",
                 "-q:v", str(q), str(out.resolve())], cwd=out.parent)
            if out.stat().st_size < 1.9e6:
                break
    finally:
        if ass:
            ass.unlink(missing_ok=True)
    return out
