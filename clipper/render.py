"""Final encode: reframe + captions + loudness normalisation -> upload-ready MP4."""

from __future__ import annotations

from pathlib import Path

from .reframe import Layout, video_filter
from .util import run


def render(src: Path, out: Path, lay: Layout, ass: Path | None, has_audio: bool,
           start: float | None = None, end: float | None = None) -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start is not None and end is not None:
        cmd += ["-ss", f"{start:.3f}", "-to", f"{end:.3f}"]
    cmd += ["-i", str(src.resolve())]
    # ass= takes a filename; running in its folder avoids FFmpeg filter-path escaping.
    cmd += ["-filter_complex", video_filter(lay, ass.name if ass else None), "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-fps_mode", "cfr", "-r", "30", "-movflags", "+faststart", str(out.resolve())]
    run(cmd, cwd=ass.parent if ass else None)
    return out
