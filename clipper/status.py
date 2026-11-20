"""Progress reporting for the dashboard.

Every running job (live session, VOD run, watch pass) keeps a small JSON file in
work/status/ up to date: what stage it's in, progress, recent events and, for live
sessions, the hype-signal telemetry. `clip dashboard` reads these files.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import ROOT

STATUS_DIR = ROOT / "work" / "status"
KEEP_DAYS = 7
MAX_EVENTS = 300


class Job:
    def __init__(self, kind: str, title: str, **fields):
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        _prune()
        self.id = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        self.path = STATUS_DIR / f"{self.id}.json"
        now = time.time()
        self.data = {"id": self.id, "kind": kind, "title": title, "pid": os.getpid(), "state": "running",
                     "started": now, "updated": now, "stage": "", "progress": None, "events": [], **fields}
        self._last_write = 0.0
        self.flush(force=True)

    def update(self, force: bool = False, **fields) -> None:
        self.data.update(fields)
        self.flush(force)

    def event(self, kind: str, text: str, **extra) -> None:
        self.data["events"].append({"t": time.time(), "kind": kind, "text": text, **extra})
        del self.data["events"][:-MAX_EVENTS]
        self.flush(force=True)

    def finish(self, state: str = "done", text: str = "") -> None:
        self.data["state"] = state
        self.data["stage"] = text or state
        self.data["finished"] = time.time()
        if text:
            self.event("done" if state == "done" else "error", text)
        self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        self.data["updated"] = now
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.data, ensure_ascii=False))
            tmp.replace(self.path)
        except OSError:
            pass  # a full disk must never take the pipeline down with it


class NullJob(Job):
    """Stand-in when a caller doesn't track status."""

    def __init__(self):  # noqa: D107 - intentionally skips the file
        self.data = {"events": []}

    def flush(self, force: bool = False) -> None:
        pass


def _prune() -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for p in STATUS_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def read_all() -> list[dict]:
    jobs = []
    for p in STATUS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("state") in ("running", "waiting"):
            try:
                os.kill(int(d.get("pid", 0)), 0)
            except (OSError, ValueError):
                d["state"] = "stopped"
                d["stage"] = "stopped unexpectedly"
        jobs.append(d)
    jobs.sort(key=lambda d: (d.get("state") not in ("running", "waiting"), -d.get("updated", 0)))
    return jobs
