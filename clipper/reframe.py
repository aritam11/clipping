"""16:9 -> 9:16. Finds a streamer facecam or on-screen faces and picks a layout.

Layouts (output is always 1080x1920):
  stack - facecam on top, gameplay below (classic streamer short)
  face  - full-height crop that follows the main face, shot by shot
  zoom  - centre square of the frame, filling the middle, over a blurred copy
  blur  - whole frame, small in the middle, over a blurred copy
  crop  - plain centre crop
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .util import log

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

MODEL = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
OUT_W, OUT_H = 1080, 1920
TOP_H = 640            # facecam panel height in the stack layout
SAMPLE_FPS = 2.5       # face-detection samples per second
DET_W = 512
CAPTION_Y = {"stack": TOP_H + 10, "face": 1300, "crop": 1300, "zoom": 1380, "blur": 1420}


@dataclass
class Layout:
    mode: str
    W: int
    H: int
    box: tuple[int, int, int, int] | None = None   # stack: facecam crop in source pixels
    x_expr: str | None = None                      # face: crop x as an FFmpeg expression of t
    note: str = ""

    @property
    def caption_y(self) -> int:
        return CAPTION_Y[self.mode]


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


# ---------------------------------------------------------------- analysis inputs

def _frames(path: Path, start: float, dur: float, W: int, H: int) -> tuple[list[np.ndarray], list[float]]:
    h = _even(DET_W * H / W)
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
         "-an", "-vf", f"fps={SAMPLE_FPS},scale={DET_W}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True,
    )
    size = DET_W * h * 3
    raw = r.stdout
    frames = [np.frombuffer(raw[i:i + size], dtype=np.uint8).reshape(h, DET_W, 3)
              for i in range(0, len(raw) - size + 1, size)]
    return frames, [k / SAMPLE_FPS for k in range(len(frames))]


def _scene_cuts(path: Path, start: float, dur: float) -> list[float]:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path), "-an",
         "-vf", "scale=320:-2,scdet=threshold=10:sc_pass=1,metadata=print:file=-", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    cuts = sorted({float(m) for m in re.findall(r"lavfi\.scd\.time=([\d.]+)", r.stdout)})
    return [c for c in cuts if 0.3 < c < dur - 0.3]


def _detect(frames: list[np.ndarray]) -> list[list[tuple[float, float, float, float, float]]]:
    """Per frame: faces as (cx, cy, w, h, score), all 0-1 fractions of the frame."""
    import cv2

    if not frames or not MODEL.exists():
        return [[] for _ in frames]
    h, w = frames[0].shape[:2]
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (w, h), 0.7, 0.3, 50)
    out = []
    for fr in frames:
        _, faces = det.detect(fr)
        cur = []
        for f in faces if faces is not None else []:
            x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
            if fw >= 0.025 * w:  # tiny faces are usually background people or in-game noise
                cur.append(((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, score))
        out.append(cur)
    return out


# ---------------------------------------------------------------- facecam (stream overlay)

def _facecam(dets: list[list[tuple]]) -> dict | None:
    """A small face that sits still near an edge in most frames = streamer facecam overlay."""
    flat = [(fi, *d) for fi, ds in enumerate(dets) for d in ds]
    clusters: list[list] = []
    for d in sorted(flat, key=lambda d: -d[5]):
        for c in clusters:
            cx = np.median([e[1] for e in c]); cy = np.median([e[2] for e in c]); ch = np.median([e[4] for e in c])
            if abs(d[1] - cx) < 0.05 and abs(d[2] - cy) < 0.05 and 0.7 < d[4] / ch < 1.4:
                c.append(d)
                break
        else:
            clusters.append([d])
    for c in sorted(clusters, key=lambda c: -len({e[0] for e in c})):
        frac = len({e[0] for e in c}) / max(len(dets), 1)
        if frac < 0.45:
            return None
        arr = np.array([e[1:5] for e in c])
        cx, cy, fw, fh = np.median(arr, axis=0)
        edge = cx < 0.33 or cx > 0.67 or cy < 0.3 or cy > 0.72
        if fh < 0.30 and edge and arr[:, :2].std(axis=0).max() < 0.02:
            return {"cx": cx, "cy": cy, "w": fw, "h": fh, "frac": frac}
    return None


def _stack_box(W, H, cam: dict) -> tuple[int, int, int, int]:
    cx, cy, fw, fh = cam["cx"] * W, cam["cy"] * H, cam["w"] * W, cam["h"] * H
    cam_w = min(W * 0.5, fw * 3.2)
    cam_h = cam_w * TOP_H / OUT_W
    if cam_h > H:
        cam_h, cam_w = H, H * OUT_W / TOP_H
    x = min(max(cx - cam_w / 2, 0), W - cam_w)
    y = min(max(cy + 0.15 * fh - cam_h / 2, 0), H - cam_h)
    return _even(x), _even(y), _even(cam_w), _even(cam_h)


# ---------------------------------------------------------------- face tracking (edited video)

def _main_face(faces: list[tuple]) -> tuple | None:
    return max(faces, key=lambda f: f[2] * f[3] * f[4]) if faces else None


def _track(dets, times, cuts, dur, W, cw) -> tuple[list[tuple[float, float, float, float]], float]:
    """Crop path as segments (t0, t1, x0, x1): hold when x0 == x1, pan otherwise.

    Each shot gets its own framing. Inside a shot the crop holds still and only
    pans when the face drifts out of a comfort zone, like a camera operator.
    """
    def to_x(cx_frac: float) -> float:
        return float(min(max(cx_frac * W - cw / 2, 0), W - cw))

    centre = (W - cw) / 2
    bounds = [0.0, *cuts, dur]
    segs, covered = [], 0.0
    for a, b in zip(bounds, bounds[1:]):
        inside = [(t, _main_face(d)) for t, d in zip(times, dets) if a + 0.15 <= t < b - 0.15]
        found = [(t, f) for t, f in inside if f]
        if not found or len(found) < 0.4 * len(inside):
            segs.append((a, b, None, None))
            continue
        covered += b - a
        ts = [t for t, _ in found]
        xs = [to_x(f[0]) for _, f in found]
        xs = [float(np.median(xs[max(0, i - 2):i + 3])) for i in range(len(xs))]  # drop flicker
        cur, seg_start = xs[0], a
        for t, x in zip(ts, xs):
            if t < seg_start:
                continue
            if abs(x - cur) > 0.10 * W:  # face left the comfort zone: pan over 0.6s
                ramp_end = min(t + 0.6, b)
                if t > seg_start:
                    segs.append((seg_start, t, cur, cur))
                segs.append((t, ramp_end, cur, x))
                cur, seg_start = x, ramp_end
        if seg_start < b:
            segs.append((seg_start, b, cur, cur))

    # Shots without a face are framed on the centre.
    filled = [(t0, t1, centre, centre) if x0 is None else (t0, t1, x0, x1) for t0, t1, x0, x1 in segs]
    return filled, covered


def _expr(segs: list[tuple[float, float, float, float]]) -> str:
    merged: list[list[float]] = []
    for t0, t1, x0, x1 in segs:
        if merged and merged[-1][2] == merged[-1][3] == x0 == x1:
            merged[-1][1] = t1
        else:
            merged.append([t0, t1, x0, x1])
    def val(t0, t1, x0, x1):
        if x0 == x1:
            return f"{x0:.0f}"
        return f"({x0:.0f}+({x1 - x0:.0f})*min(1,max(0,(t-{t0:.3f})/{max(t1 - t0, 0.01):.3f})))"

    expr = val(*merged[-1])
    for seg in reversed(merged[:-1]):
        expr = f"if(lt(t,{seg[1]:.3f}),{val(*seg)},{expr})"
    return expr


# ---------------------------------------------------------------- entry points

def analyze(path: Path, start: float, end: float, W: int, H: int, layout: str = "auto",
            fallback: str = "zoom", facecam: tuple[float, float, float, float] | None = None) -> Layout:
    dur = end - start
    if facecam:  # manual facecam region (0-1 fractions of the frame)
        fx, fy, fw, fh = facecam
        return Layout("stack", W, H, (_even(fx * W), _even(fy * H), _even(fw * W), _even(fh * H)),
                      note="manual facecam region")
    if W / H <= 0.65:  # already vertical: just fit it
        return Layout("blur", W, H, note="source is already vertical")
    if layout in ("zoom", "blur", "crop"):
        return Layout(layout, W, H, note="forced")

    frames, times = _frames(path, start, dur, W, H)
    dets = _detect(frames)

    cam = _facecam(dets) if layout in ("auto", "stack") else None
    if cam:
        return Layout("stack", W, H, _stack_box(W, H, cam), note=f"facecam in {cam['frac']:.0%} of frames")

    if layout in ("auto", "face"):
        cw = _even(min(W, H * 9 / 16))
        cuts = _scene_cuts(path, start, dur)
        segs, covered = _track(dets, times, cuts, dur, W, cw)
        if segs and (layout == "face" or covered >= 0.35 * dur):
            return Layout("face", W, H, x_expr=_expr(segs),
                          note=f"tracking faces, {len(cuts) + 1} shot(s), faces in {covered / dur:.0%} of clip")

    if layout in ("stack", "face"):
        log(f"    no usable face for '{layout}', using '{fallback}'")
    return Layout(fallback, W, H, note="no facecam or main face")


def video_filter(lay: Layout, ass_name: str | None) -> str:
    W, H = lay.W, lay.H
    subs = f",ass={ass_name}" if ass_name else ""
    bg = (f"scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=12,"
          f"scale={OUT_W}:{OUT_H},eq=brightness=-0.08")
    if lay.mode == "stack":
        x, y, w, h = lay.box
        gh = _even(H)
        gw = _even(min(W, H * OUT_W / (OUT_H - TOP_H)))
        gx = _even((W - gw) / 2)
        return (f"[0:v]split=2[a][b];"
                f"[a]crop={w}:{h}:{x}:{y},scale={OUT_W}:{TOP_H}:flags=lanczos,setsar=1[top];"
                f"[b]crop={gw}:{gh}:{gx}:0,scale={OUT_W}:{OUT_H - TOP_H}:flags=lanczos,setsar=1[bot];"
                f"[top][bot]vstack=inputs=2{subs}[vout]")
    if lay.mode == "face":
        cw = _even(min(W, H * 9 / 16))
        return (f"[0:v]crop={cw}:{_even(H)}:'{lay.x_expr}':0,"
                f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1{subs}[vout]")
    if lay.mode == "crop":
        cw = _even(min(W, H * 9 / 16))
        return (f"[0:v]crop={cw}:{_even(H)}:{_even((W - cw) / 2)}:0,"
                f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1{subs}[vout]")
    if lay.mode == "zoom":
        side = _even(min(W, H))
        return (f"[0:v]split=2[a][b];[a]{bg}[bg];"
                f"[b]crop={side}:{side}:{_even((W - side) / 2)}:{_even((H - side) / 2)},"
                f"scale={OUT_W}:{OUT_W}:flags=lanczos[fg];"
                f"[bg][fg]overlay=0:{(OUT_H - OUT_W) // 2},setsar=1{subs}[vout]")
    # blur
    return (f"[0:v]split=2[a][b];[a]{bg}[bg];"
            f"[b]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{subs}[vout]")
