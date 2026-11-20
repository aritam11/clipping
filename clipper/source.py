"""Getting media in: yt-dlp for URLs (YouTube, Twitch, Kick, ...) or local files."""

from __future__ import annotations

import json
from pathlib import Path

from .util import VIDEO_EXTS, log, run, short_hash, slugify, ytdlp_cmd


class StillLive(RuntimeError):
    """The video is a stream that hasn't ended yet (e.g. Twitch's in-progress VOD)."""


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def is_youtube(url: str) -> bool:
    return any(h in url for h in ("youtube.com", "youtu.be"))


def probe(url: str) -> dict:
    r = run([*ytdlp_cmd(), "-J", "--no-playlist", "--no-warnings", url], timeout=180)
    info = json.loads(r.stdout)
    return {
        "id": f"{(info.get('extractor_key') or 'web').lower()}-{info.get('id') or short_hash(url)}",
        "title": info.get("title") or "",
        "uploader": info.get("channel") or info.get("uploader") or "",
        "duration": float(info.get("duration") or 0),
        "is_live": bool(info.get("is_live")) or info.get("live_status") in ("is_live", "is_upcoming"),
        "url": info.get("webpage_url") or url,
    }


def local_info(path: Path) -> dict:
    path = path.resolve()
    return {
        "id": f"{slugify(path.stem)}-{short_hash(str(path))}",
        "title": path.stem,
        "uploader": "",
        "url": str(path),
    }


def download_audio(url: str, dest_dir: Path, section: tuple[float, float] | None = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(dest_dir.glob("audio.*"))
    existing = [p for p in existing if not p.name.endswith((".part", ".ytdl"))]
    if existing:
        return existing[0]
    # Speech-quality audio is plenty for transcription and loudness (~22 MB per hour);
    # rendered clips take their audio from the full-quality video download instead.
    # Always use yt-dlp's own downloader: its chunked requests are fast, while
    # --download-sections goes through ffmpeg, which YouTube throttles to ~real time.
    log("Downloading speech-quality audio (video is fetched later, only for chosen clips)")
    if section and not is_youtube(url):
        # Twitch and most other sites don't throttle, so fetch just the range.
        run([*ytdlp_cmd(), "-f", "ba[abr<=80]/wa/ba/b", "--no-playlist", "--no-warnings", "--no-progress",
             "--download-sections", f"*{section[0]:.2f}-{section[1]:.2f}", "--force-keyframes-at-cuts",
             "-o", str(dest_dir / "audio.%(ext)s"), url], timeout=None)
        found = [p for p in dest_dir.glob("audio.*") if not p.name.endswith((".part", ".ytdl"))]
        if not found:
            raise RuntimeError("yt-dlp finished but no audio file was written")
        return found[0]
    full = "full" if section else "audio"
    run([*ytdlp_cmd(), "-f", "ba[abr<=80]/wa/ba/b", "--no-playlist", "--no-warnings", "--no-progress",
         "-o", str(dest_dir / f"{full}.%(ext)s"), url], timeout=None)
    found = [p for p in dest_dir.glob(f"{full}.*") if not p.name.endswith((".part", ".ytdl"))]
    if not found:
        raise RuntimeError("yt-dlp finished but no audio file was written")
    if not section:
        return found[0]
    cut = extract_audio(found[0], dest_dir, section)
    found[0].unlink(missing_ok=True)
    return cut


def extract_audio(path: Path, dest_dir: Path, section: tuple[float, float]) -> Path:
    dest = dest_dir / "audio.m4a"
    if not dest.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{section[0]:.3f}", "-to", f"{section[1]:.3f}",
             "-i", str(path), "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "96k", str(dest)])
    return dest


def download_section(url: str, start: float, end: float, dest: Path, max_height: int = 1080) -> Path:
    """Fetch only [start, end] of the video, re-cut on exact frames so t=0 == start."""
    if dest.exists():
        return dest
    fmt = (f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b")
    run([*ytdlp_cmd(), "-f", fmt, "--no-playlist", "--no-warnings", "--no-progress",
         "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
         "--merge-output-format", "mp4", "-o", str(dest), url], timeout=None)
    if not dest.exists():
        cands = sorted(dest.parent.glob(dest.stem + ".*"))
        if not cands:
            raise RuntimeError(f"section download failed for {start:.1f}-{end:.1f}")
        cands[0].rename(dest)
    return dest


def list_source(src: str, latest: int, min_age_s: float = 90.0) -> list[dict]:
    """New-video candidates from a channel/playlist URL or a local folder."""
    p = Path(src).expanduser()
    if not is_url(src) and p.is_dir():
        import time
        now = time.time()
        files = [f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS and f.is_file()]
        # Skip files still being written (e.g. an OBS recording in progress).
        files = [f for f in files if now - f.stat().st_mtime > min_age_s]
        files.sort(key=lambda f: f.stat().st_mtime)
        return [{**local_info(f), "source": str(f)} for f in files[-latest:]]
    if not is_url(src):
        f = p.resolve()
        return [{**local_info(f), "source": str(f)}] if f.exists() else []

    r = run([*ytdlp_cmd(), "--flat-playlist", "--playlist-end", str(latest), "-J", "--no-warnings", src],
            timeout=300)
    info = json.loads(r.stdout)
    entries = info.get("entries")
    if entries is None:  # a single video URL
        entries = [info]
    out = []
    for e in entries:
        if not e:
            continue
        url = e.get("webpage_url") or e.get("url")
        if not url:
            continue
        if not is_url(url):
            url = f"https://www.youtube.com/watch?v={url}"
        out.append({
            "id": f"{(e.get('ie_key') or e.get('extractor_key') or 'web').lower()}-{e.get('id') or short_hash(url)}",
            "title": e.get("title") or "",
            "duration": float(e.get("duration") or 0),
            "is_live": e.get("live_status") in ("is_live", "is_upcoming") or bool(e.get("is_live")),
            "source": url,
        })
    out.reverse()  # oldest first
    return out
