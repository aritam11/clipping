"""Hands-off mode: poll channels / folders and clip every new video once."""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import source
from .config import Settings
from .pipeline import process
from .status import Job
from .util import log, read_json, write_json

MAX_ATTEMPTS = 3


def _lock(path: Path):
    """Exclusive lock so overlapping scheduled runs don't both work (works on macOS, Linux, Windows)."""
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _read_sources(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split(" #", 1)[0].strip()  # "url  # note" -> "url"
        if line and not line.startswith("#"):
            out.append(line)
    return out


def run_once(sources_file: Path, st: Settings, latest: int, min_dur: float, max_dur: float | None) -> int:
    state_path = st.work_dir / "state.json"
    state = read_json(state_path) if state_path.exists() else {"videos": {}}
    done = 0
    job = Job("watch", f"Watch pass: {sources_file.name}", source=str(sources_file))
    for src in _read_sources(sources_file):
        job.update(stage=f"Checking {src}", force=True)
        try:
            entries = source.list_source(src, latest)
        except Exception as exc:  # noqa: BLE001
            log(f"Could not list {src}: {exc}")
            job.event("error", f"Could not list {src}: {str(exc)[:150]}")
            continue
        for e in entries:
            rec = state["videos"].get(e["id"], {})
            if rec.get("status") == "done" or rec.get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            if e.get("is_live"):
                continue
            d = e.get("duration") or 0
            if d and (d < min_dur or (max_dur and d > max_dur)):
                state["videos"][e["id"]] = {"status": "skipped", "reason": f"duration {d:.0f}s", "title": e.get("title")}
                continue
            job.event("info", f"New video: {e.get('title') or e['source']}")
            job.update(stage=f"Clipping {e.get('title') or e['source']}", force=True)
            try:
                files = process(e["source"], st)
                state["videos"][e["id"]] = {"status": "done", "clips": len(files), "title": e.get("title"),
                                            "source": e["source"], "at": time.strftime("%Y-%m-%d %H:%M")}
                done += 1
            except source.StillLive:
                # e.g. Twitch lists the running stream as a VOD: not a failure, check again next pass
                log(f"Still live, will clip it after it ends: {e.get('title') or e['source']}")
                job.event("info", f"Still live, skipped for now: {e.get('title') or e['source']}")
                continue
            except Exception as exc:  # noqa: BLE001
                log(f"Failed {e['source']}: {exc}")
                state["videos"][e["id"]] = {"status": "failed", "error": str(exc)[:500], "title": e.get("title"),
                                            "attempts": rec.get("attempts", 0) + 1, "source": e["source"]}
            write_json(state_path, state)
    write_json(state_path, state)
    job.finish("done", f"{done} new video(s) clipped")
    return done


def watch(sources_file: Path, st: Settings, latest: int = 3, interval: float = 0,
          min_dur: float = 60, max_dur: float | None = None) -> None:
    st.work_dir.mkdir(parents=True, exist_ok=True)
    lock = _lock(st.work_dir / ".watch.lock")
    if lock is None:
        log("Another watch run is still working; exiting")
        return
    while True:
        n = run_once(sources_file, st, latest, min_dur, max_dur)
        log(f"Watch pass finished: {n} new video(s) clipped")
        if interval <= 0:
            break
        log(f"Next check in {interval / 60:.0f} min")
        time.sleep(interval)
