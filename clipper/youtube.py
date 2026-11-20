"""Upload finished clips to a YouTube channel as Shorts.

Setup (once per device): put an OAuth "Desktop app" client from Google Cloud in
credentials/client_secret.json, then run `clip youtube auth` and sign in.

Every upload is recorded in work/uploads.json, so a clip is never uploaded twice.
When YouTube's daily upload limit is hit, clips wait in that file as "queued" and
go up later (`clip youtube retry`, or automatically by the next run).
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT
from .util import log, read_json, write_json

CRED_DIR = ROOT / "credentials"           # git-ignored: each device signs in on its own
CLIENT_SECRET = CRED_DIR / "client_secret.json"
TOKEN = CRED_DIR / "youtube_token.json"
LEDGER = ROOT / "work" / "uploads.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
TITLE_MAX = 100
MAX_ATTEMPTS = 3
_lock = threading.Lock()


@dataclass
class UploadConfig:
    privacy: str = "public"               # public | unlisted | private
    streamer: str = ""                    # overrides the channel name from the source
    per_day: int = 50                     # stay under YouTube's 100/day upload allowance
    category: str = "20"                  # 20 = Gaming
    language: str = "en"
    extra_tags: list[str] = field(default_factory=list)


class NotSignedIn(RuntimeError):
    pass


def config_from(st) -> UploadConfig:
    """UploadConfig from the run's Settings."""
    return UploadConfig(privacy=st.privacy, streamer=st.streamer, per_day=st.uploads_per_day,
                        category=st.yt_category, language=st.language or "en")


# ---------------------------------------------------------------- auth

def auth() -> str:
    """Interactive, once per device: opens the browser for the channel owner to sign in."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_SECRET.exists():
        raise NotSignedIn(f"Missing {CLIENT_SECRET}. See 'YouTube uploads' in README.md for the 5 setup steps.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  authorization_prompt_message="Opening your browser to sign in to YouTube…\n",
                                  success_message="Signed in. You can close this tab and go back to the terminal.")
    _save_token(creds)
    ch = channel()
    return ch["title"] if ch else "(unknown channel)"


def _save_token(creds) -> None:
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN, 0o600)
    except OSError:
        pass


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN.exists():
        raise NotSignedIn("Not signed in to YouTube. Run: uv run clip youtube auth")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001 - revoked or expired refresh token
                raise NotSignedIn(f"YouTube sign-in expired ({exc}). Run: uv run clip youtube auth") from exc
            _save_token(creds)
        else:
            raise NotSignedIn("YouTube sign-in expired. Run: uv run clip youtube auth")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def channel() -> dict | None:
    items = _service().channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        return None
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def signed_in() -> bool:
    return TOKEN.exists()


# ---------------------------------------------------------------- metadata

def _tagify(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "", s.replace(" ", ""))


def _game_tags(text: str) -> list[str]:
    t = text.lower()
    if "gta" in t or "grand theft auto" in t:
        if re.search(r"gta\s*(6|vi)\b|grand theft auto vi\b", t):
            return ["#GTA6", "#GTAVI"]
        return ["#GTA"]
    return []


def streamer_from_url(url: str) -> str:
    """'youtube.com/@IShowSpeed/live' -> 'IShowSpeed', 'twitch.tv/kaicenat' -> 'kaicenat', 'kick.com/x' -> 'x'."""
    m = re.search(r"youtube\.com/@([A-Za-z0-9_.-]+)", url or "") or \
        re.search(r"(?:twitch\.tv|kick\.com)/([A-Za-z0-9_]+)", url or "")
    name = m.group(1) if m else ""
    return "" if name.lower() in ("videos", "watch", "live") else name


def build_metadata(meta: dict, cfg: UploadConfig) -> dict:
    """Title, description, hashtags and tags tuned for Shorts discovery, crediting the streamer."""
    streamer = (cfg.streamer or meta.get("streamer") or streamer_from_url(meta.get("source_url", ""))).strip()
    meta = {**meta, "source_title": re.sub(r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "",  # yt-dlp's live-title stamp
                                          meta.get("source_title", "")).strip()}
    base = (meta.get("title") or meta.get("hook") or "").strip() or (meta.get("transcript") or "Stream moment")[:80]
    base = base.replace("<", "").replace(">", "")  # not allowed in YouTube titles

    # Hashtags: streamer first (people search the name), then game, Claude's picks, then #shorts.
    tags_text = " ".join([base, meta.get("source_title", ""), " ".join(meta.get("hashtags", [])),
                          " ".join(cfg.extra_tags)])
    hashtags: list[str] = []
    for h in ([f"#{_tagify(streamer)}"] if streamer else []) + _game_tags(tags_text) + \
            [h if h.startswith("#") else f"#{h}" for h in meta.get("hashtags", []) + cfg.extra_tags] + ["#shorts"]:
        clean = "#" + _tagify(h[1:])
        if len(clean) > 2 and clean.lower() not in {x.lower() for x in hashtags}:
            hashtags.append(clean[:40])
    hashtags = hashtags[:10]  # YouTube ignores all hashtags past ~15; fewer, relevant ones work better

    title = base
    if streamer and _tagify(streamer).lower() not in _tagify(title).lower():  # "Kai Cenat" == "kaicenat"
        title = f"{title} | {streamer}"
    if len(title) + len(" #shorts") <= TITLE_MAX:
        title += " #shorts"
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1].rsplit(" ", 1)[0] + "…"

    lines = []
    if meta.get("hook") and meta["hook"].lower() not in base.lower():
        lines.append(meta["hook"].strip().rstrip(".") + "…")
    if streamer:
        lines.append(f"🎮 {streamer}{' - ' + meta['source_title'] if meta.get('source_title') else ''}")
    if meta.get("source_url", "").startswith("http"):
        lines.append(f"📺 Full stream: {meta['source_url']}")
    if streamer:
        lines.append(f"All credit to {streamer}. Clip from their livestream.")
    lines += ["", " ".join(hashtags)]
    description = "\n".join(lines)[:4900]

    tags, total = [], 0
    for t in ([streamer, f"{streamer} clips", f"{streamer} live"] if streamer else []) + \
            [h[1:] for h in hashtags] + ["shorts", "gaming clips", "stream highlights"]:
        t = t.strip()
        if t and t.lower() not in {x.lower() for x in tags} and total + len(t) + 3 <= 480:
            tags.append(t)
            total += len(t) + 3
    return {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": cfg.category,
                    "defaultLanguage": cfg.language, "defaultAudioLanguage": cfg.language},
        "status": {"privacyStatus": cfg.privacy, "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": False},
    }


# ---------------------------------------------------------------- ledger (what's been uploaded)

def _key(mp4: Path) -> str:
    try:
        return mp4.resolve().relative_to((ROOT / "output").resolve()).as_posix()
    except ValueError:
        return mp4.resolve().as_posix()


def ledger() -> dict:
    with _lock:
        return read_json(LEDGER) if LEDGER.exists() else {}


def _record(key: str, **fields) -> dict:
    with _lock:
        data = read_json(LEDGER) if LEDGER.exists() else {}
        entry = {**data.get(key, {}), **fields, "updated": time.time()}
        data[key] = entry
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        write_json(LEDGER, data)
        return entry


def uploads_last_24h() -> int:
    cutoff = time.time() - 86400
    return sum(1 for e in ledger().values() if e.get("status") == "uploaded" and e.get("at", 0) >= cutoff)


# ---------------------------------------------------------------- uploading

def upload(mp4: Path, meta: dict, cfg: UploadConfig, dry_run: bool = False) -> dict:
    """Upload one clip. Returns its ledger entry (status uploaded / queued / failed / dry-run)."""
    key = _key(mp4)
    prev = ledger().get(key, {})
    if prev.get("status") == "uploaded":
        return prev
    body = build_metadata(meta, cfg)
    if dry_run:
        return {"status": "dry-run", "body": body}
    if uploads_last_24h() >= cfg.per_day:
        log(f"  YouTube: daily cap of {cfg.per_day} reached; queued {mp4.name}")
        return _record(key, status="queued", reason="daily cap", file=str(mp4), title=body["snippet"]["title"])

    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    try:
        yt = _service()
        req = yt.videos().insert(part="snippet,status", body=body, notifySubscribers=True,
                                 media_body=MediaFileUpload(str(mp4), mimetype="video/mp4",
                                                            chunksize=8 * 1024 * 1024, resumable=True))
        resp, tries = None, 0
        while resp is None:
            try:
                _, resp = req.next_chunk()
            except HttpError as exc:
                if exc.resp.status in (500, 502, 503, 504) and tries < 5:  # transient: resume the upload
                    tries += 1
                    time.sleep(2 ** tries)
                    continue
                raise
        vid = resp["id"]
        # YouTube can still lock an upload to private (unaudited API project); report what it really is.
        st = yt.videos().list(part="status", id=vid).execute().get("items", [{}])[0].get("status", {})
        actual = st.get("privacyStatus", cfg.privacy)
        note = ""
        if cfg.privacy != "private" and actual == "private":
            note = ("YouTube locked this upload to private. Your Google Cloud project may need the YouTube API "
                    "audit before uploads can be public; meanwhile you can make it public in YouTube Studio.")
            log(f"  YouTube: {note}")
        entry = _record(key, status="uploaded", video_id=vid, url=f"https://www.youtube.com/shorts/{vid}",
                        privacy=actual, note=note, at=time.time(), title=body["snippet"]["title"], file=str(mp4))
        log(f"  YouTube: uploaded {mp4.name} → {entry['url']} ({actual})")
        return entry
    except NotSignedIn as exc:
        log(f"  YouTube: {exc}")
        return _record(key, status="queued", reason="not signed in", file=str(mp4), title=body["snippet"]["title"])
    except HttpError as exc:
        reason = ""
        try:
            reason = json.loads(exc.content.decode())["error"]["errors"][0].get("reason", "")
        except Exception:  # noqa: BLE001
            pass
        if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "userRequestsExceedRateLimit"):
            log(f"  YouTube: limit reached ({reason}); queued {mp4.name} for later")
            return _record(key, status="queued", reason=reason, file=str(mp4), title=body["snippet"]["title"])
        attempts = prev.get("attempts", 0) + 1
        log(f"  YouTube: upload failed ({exc.resp.status} {reason or str(exc)[:150]})")
        return _record(key, status="failed", reason=f"{exc.resp.status} {reason}".strip(), attempts=attempts,
                       file=str(mp4), title=body["snippet"]["title"])
    except Exception as exc:  # noqa: BLE001 - network etc.: keep it for a retry
        attempts = prev.get("attempts", 0) + 1
        log(f"  YouTube: upload failed ({str(exc)[:200]})")
        return _record(key, status="failed", reason=str(exc)[:200], attempts=attempts, file=str(mp4),
                       title=body["snippet"]["title"])


def meta_for(mp4: Path) -> dict:
    j = mp4.with_suffix(".json")
    return read_json(j) if j.exists() else {}


def retry_pending(cfg: UploadConfig) -> list[dict]:
    """Upload everything queued (daily cap / limits / not signed in) or failed fewer than 3 times."""
    done = []
    for key, e in ledger().items():
        if e.get("status") == "queued" or (e.get("status") == "failed" and e.get("attempts", 0) < MAX_ATTEMPTS):
            mp4 = Path(e.get("file") or (ROOT / "output" / key))
            if mp4.exists():
                r = upload(mp4, meta_for(mp4), cfg)
                done.append(r)
                if r.get("status") == "queued":
                    break  # still capped / not signed in: stop for now
    return done


class Uploader:
    """Background uploads, so live clipping never waits on YouTube."""

    def __init__(self, cfg: UploadConfig, on_done=None):
        self.cfg, self.on_done = cfg, on_done
        self.q: queue.Queue = queue.Queue()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def enqueue(self, mp4: Path, meta: dict) -> None:
        _record(_key(mp4), status="queued", reason="uploading", file=str(mp4),
                title=build_metadata(meta, self.cfg)["snippet"]["title"])
        self.q.put((mp4, meta))

    def _run(self) -> None:
        while True:
            mp4, meta = self.q.get()
            try:
                r = upload(mp4, meta, self.cfg)
                if self.on_done:
                    self.on_done(mp4, r)
            finally:
                self.q.task_done()

    def drain(self, timeout: float = 600) -> None:
        """Give queued uploads time to finish before exiting (whatever is left stays queued on disk)."""
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(1)
