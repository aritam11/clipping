"""Audio loudness curve: yelling, laughter, gunfire and explosions show up as peaks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

HOP = 0.5  # seconds per loudness sample
SR = 8000


def loudness_curve(media: Path, cache: Path | None = None) -> np.ndarray:
    if cache and cache.exists():
        return np.load(cache)
    n = int(SR * HOP)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "fatal", "-i", str(media), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    vals, buf = [], b""
    assert proc.stdout
    while True:
        chunk = proc.stdout.read(n * 2 * 2000)
        if not chunk:
            break
        buf += chunk
        k = len(buf) // (n * 2)
        if k:
            arr = np.frombuffer(buf[: k * n * 2], dtype=np.int16).astype(np.float32).reshape(k, n)
            rms = np.sqrt((arr ** 2).mean(axis=1)) / 32768.0
            vals.append(20 * np.log10(rms + 1e-6))
            buf = buf[k * n * 2:]
    proc.wait()
    curve = np.concatenate(vals) if vals else np.zeros(0, dtype=np.float32)
    if cache:
        np.save(cache, curve)
    return curve


def zscore(curve: np.ndarray) -> np.ndarray:
    """Robust z-score so one very loud stream doesn't flatten everything else."""
    if curve.size == 0:
        return curve
    c = np.maximum(curve, -70.0)
    med = np.median(c)
    mad = np.median(np.abs(c - med)) + 1e-6
    return (c - med) / (1.4826 * mad)


def window_peak(z: np.ndarray, start: float, end: float) -> float:
    """Mean of the loudest quarter of samples inside [start, end]."""
    a = int(start / HOP)
    b = max(a + 1, int(end / HOP))
    seg = z[a:b]
    if seg.size == 0:
        return 0.0
    k = max(1, seg.size // 4)
    return float(np.mean(np.sort(seg)[-k:]))


def loud_regions(z: np.ndarray, thresh: float = 2.5, min_len: float = 1.0) -> list[tuple[float, float]]:
    out, start = [], None
    for i, v in enumerate(z):
        if v >= thresh and start is None:
            start = i
        elif v < thresh and start is not None:
            if (i - start) * HOP >= min_len:
                out.append((start * HOP, i * HOP))
            start = None
    if start is not None and (len(z) - start) * HOP >= min_len:
        out.append((start * HOP, len(z) * HOP))
    return out
