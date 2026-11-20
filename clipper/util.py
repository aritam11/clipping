"""Small helpers shared by every pipeline stage."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".flv", ".m4v", ".ts", ".avi"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class CmdError(RuntimeError):
    pass


def run(cmd, cwd=None, capture=True, timeout=None, input=None) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    res = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True, timeout=timeout, input=input)
    if res.returncode != 0:
        tail = ((res.stderr or "") + (res.stdout or ""))[-2000:] if capture else ""
        raise CmdError(f"command failed ({res.returncode}): {' '.join(cmd[:6])} ...\n{tail}")
    return res


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        sys.exit(f"Missing required tools: {', '.join(missing)}. Install with: brew install ffmpeg")


def ytdlp_cmd() -> list[str]:
    """yt-dlp from this environment, so it never depends on PATH."""
    return [sys.executable, "-m", "yt_dlp"]


def media_info(path: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    data = json.loads(r.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(data.get("format", {}).get("duration") or 0)
    info = {"duration": dur, "has_audio": a is not None, "has_video": v is not None, "width": 0, "height": 0}
    if v:
        info["width"], info["height"] = int(v["width"]), int(v["height"])
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        if abs(rot) in (90, 270):
            info["width"], info["height"] = info["height"], info["width"]
    return info


def slugify(s: str, n: int = 48) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return (s[:n].rstrip("-")) or "video"


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:8]


def fmt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m)}:{s:04.1f}"


def parse_ts(s: str) -> float:
    """'1:02:03.5', '62:03', '3723' -> seconds."""
    parts = [float(p) for p in s.strip().split(":")]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"bad timestamp: {s!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)
