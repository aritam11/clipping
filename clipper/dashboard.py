"""`clip dashboard`: a local web page showing everything clipper is doing.

Read-only. Serves on 127.0.0.1 only. The page polls /api/state every 2 seconds.
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import autolive, status, youtube
from .config import ROOT, Settings

PAGE = Path(__file__).resolve().parent / "static" / "dashboard.html"
OUT = Settings().out_dir.resolve()
MAX_CLIPS = 60
_agent_cache: dict = {"t": 0.0, "data": []}


def _agents() -> list[dict]:
    if time.time() - _agent_cache["t"] > 5:  # launchctl is slowish; refresh every 5 s
        _agent_cache.update(t=time.time(), data=autolive.agents())
    return _agent_cache["data"]


def _uploads() -> dict:
    try:
        return youtube.ledger()
    except (OSError, ValueError):
        return {}


def _clips() -> list[dict]:
    items = []
    ups = _uploads()
    for meta in OUT.glob("*/clip_*.json"):
        mp4 = meta.with_suffix(".mp4")
        if not mp4.exists():
            continue
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append({
            "src": f"{meta.parent.name}/{mp4.name}", "folder": meta.parent.name, "name": mp4.stem,
            "title": d.get("title") or "", "caption": d.get("caption") or "", "score": d.get("score"),
            "by": d.get("picked_by") or "", "layout": d.get("layout") or "",
            "duration": round(float(d.get("end", 0)) - float(d.get("start", 0)), 1),
            "source_title": d.get("source_title") or "", "reason": d.get("reason") or "",
            "made": mp4.stat().st_mtime, "size": mp4.stat().st_size,
            "yt": {k: v for k, v in ups.get(f"{meta.parent.name}/{mp4.name}", {}).items()
                   if k in ("status", "url", "privacy", "reason", "note")},
        })
    items.sort(key=lambda c: -c["made"])
    return items[:MAX_CLIPS]


def _state() -> dict:
    du = shutil.disk_usage(ROOT)
    jobs = status.read_all()
    day_ago = time.time() - 86400
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "requests": 0}
    for j in jobs:
        if j.get("updated", 0) >= day_ago:
            for k in usage:
                usage[k] += (j.get("usage") or {}).get(k, 0) or 0
    for j in jobs[12:]:  # older jobs: keep the list light
        j.pop("telemetry", None)
        j["events"] = j.get("events", [])[-5:]
    ups = _uploads().values()
    yt = {"signed_in": youtube.signed_in(),
          "uploaded_24h": sum(1 for e in ups if e.get("status") == "uploaded" and e.get("at", 0) >= day_ago),
          "queued": sum(1 for e in ups if e.get("status") == "queued"),
          "failed": sum(1 for e in ups if e.get("status") == "failed")}
    return {"now": time.time(), "disk": {"free": du.free, "total": du.total}, "usage_24h": usage, "youtube": yt,
            "jobs": jobs[:40], "agents": _agents(), "clips": _clips()}


class Handler(BaseHTTPRequestHandler):
    server_version = "clipper"

    def log_message(self, *_):  # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(_state()).encode(), "application/json")
            elif path.startswith("/media/"):
                self._media(path[len("/media/"):])
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _media(self, rel: str) -> None:
        f = (OUT / rel).resolve()
        if not f.is_relative_to(OUT) or not f.is_file():  # never serve outside output/
            self._send(404, b"not found", "text/plain")
            return
        size = f.stat().st_size
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):  # video seeking needs byte ranges
            a, _, b = rng[6:].split(",")[0].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def serve(port: int = 8765, open_browser: bool = False) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Clipper dashboard: {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
