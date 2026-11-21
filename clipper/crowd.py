"""Crowd signals for live mode: what chat is saying, and the clips viewers make.

Chat. Every message is sorted into reaction kinds (laughing, hype, shock, "L", F/RIP, "clip it"),
so the picker learns *what kind* of moment happened, not just that chat got busy, and the most
repeated messages around a spike ("HE CRASHED", "KEKW") are shown to it as context.

Twitch viewer clips (optional). With a free Twitch app's client id + secret, clipper polls the clips
viewers make of the stream. Each one is a real person saying "this moment is worth clipping".
Set TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET, or put {"client_id": ..., "client_secret": ...}
in credentials/twitch.json.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import datetime, timezone

from .config import ROOT
from .util import log

KINDS = ("laugh", "hype", "shock", "fail", "rip", "clip")
BIT = {k: 1 << i for i, k in enumerate(KINDS)}
LABEL = {"laugh": "laughing", "hype": "hyped", "shock": "shocked", "fail": "calling it an L",
         "rip": "F / RIP", "clip": "asking to clip it"}

# Strong tokens count in any message; weak ones only in short spam-style messages ("W", "L", "WHAT").
_STRONG = {
    "laugh": {"KEKW", "KEKL", "LUL", "LULW", "OMEGALUL", "LMAO", "LMFAO", "ICANT", "PEPELAUGH", "XD", "XDD",
              "KEKWAIT", "LOLOL", "HAHAHA", "JAJAJA", "ROFL"},
    "hype": {"POG", "POGGERS", "POGCHAMP", "POGU", "PAGMAN", "LETSGO", "LETSGOO", "SHEESH", "GOATED", "HYPE"},
    "shock": {"WTF", "NOWAY", "AINTNOWAY", "MONKAS", "MONKAW", "OMEGALULIGUESS", "WHAAAT"},
    "fail": {"RATIO", "CRINGE", "SELLING", "THROWING", "FAILED", "FAIL"},
    "rip": {"RIP", "SADGE", "PEPEHANDS", "BIBLETHUMP"},
}
_WEAK = {
    "laugh": {"LOL", "LMAO", "XD", "HAHA", "DYING", "CRYING"},
    "hype": {"W", "WW", "WWW", "GOAT", "HUGE", "INSANE", "EZ", "CLUTCH", "LETS", "GG"},
    "shock": {"WHAT", "OMG", "HUH", "BRO", "NAH", "WAIT", "?", "NO", "NOO", "EW", "EWW", "AYO"},
    "fail": {"L", "LL", "LLL", "BRUH", "OOF", "SOLD", "UNLUCKY"},
    "rip": {"F", "FF", "FFF", "O7"},
}
_EMOJI = {
    "laugh": "😂🤣💀😭😹",
    "hype": "🔥🐐💯🚀👑",
    "shock": "😳😱🤯😮😲🫨",
    "fail": "🤡🗑🚮👎",
    "rip": "🪦⚰😢🥀",
}
_CLIP = re.compile(r"(?:^|\W)CLIP+(?:ED|PED|PING|\s*(?:IT|THAT|THIS))?(?:\W|$)|📸|🎬")
# YouTube channel emojis arrive as :_name: shortcuts.
_SHORTCUT = [("laugh", ("LAUGH", "LOL", "LMAO", "KEK", "CRY", "HAHA", "DEAD")),
             ("hype", ("FIRE", "GOAT", "HYPE", "POG", "LETSGO")),
             ("shock", ("SHOCK", "WOW", "OMG", "WHAT", "SCARED")),
             ("rip", ("RIP", "SAD"))]
_URL = re.compile(r"https?://|www\.|\.com\b|\.tv\b", re.I)


def _token(t: str) -> str:
    t = t.strip(".,!:;\"'()[]*~")
    if re.fullmatch(r"\?+", t):
        return "?"
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)  # LOOOOL -> LOOL, WWWWW -> WW
    return t


def classify(text: str) -> int:
    """Bitmask of reaction kinds in one chat message (0 = ordinary chat)."""
    up = text.upper()
    raw = up.split()
    if not raw:
        return 0
    toks = {_token(t) for t in raw} | {re.sub(r"(.)\1{2,}", r"\1", t.strip(".,!?")) for t in raw}
    joined = re.sub(r"[^A-Z]", "", up)
    short = len(raw) <= 3
    mask = 0
    for kind in ("laugh", "hype", "shock", "fail", "rip"):
        if toks & _STRONG[kind] or (short and toks & _WEAK[kind]) or any(ch in text for ch in _EMOJI[kind]):
            mask |= BIT[kind]
    if re.fullmatch(r"(?:HA){2,}H?|(?:JA){2,}|L+O+L+|LMA+O+|KEK\w*", joined):
        mask |= BIT["laugh"]
    if re.fullmatch(r"(?:W+|LE?T'?S+GO+)", joined) and short:
        mask |= BIT["hype"]
    if re.fullmatch(r"\?{2,}", text.strip()) or "NO WAY" in up or "AINT NO WAY" in up:
        mask |= BIT["shock"]
    if re.fullmatch(r"L+", joined) and short:
        mask |= BIT["fail"]
    if re.fullmatch(r"F+", joined) and short:
        mask |= BIT["rip"]
    if _CLIP.search(up):
        mask |= BIT["clip"]
    for t in raw:
        if t.startswith(":") and t.endswith(":"):
            for kind, keys in _SHORTCUT:
                if any(k in t for k in keys):
                    mask |= BIT[kind]
    return mask


def normalize(text: str) -> str | None:
    """Form used to count repeated messages: 'kekw KEKW kekw' and 'KEKWWWW' both -> 'KEKW'.
    None for messages that shouldn't be shown to the picker (links, @mentions, long sentences)."""
    text = text.strip()
    if not text or _URL.search(text) or "@" in text:
        return None
    toks = [re.sub(r"(.)\1{2,}", r"\1\1", t) for t in text.upper().split()]
    if len(toks) > 8:
        return None
    out: list[str] = []
    for t in toks:
        if not out or out[-1] != t:
            out.append(t)
    s = " ".join(out)
    return s[:40] if s else None


def youtube_text(line: bytes) -> str | None:
    """The message text (with emoji) from one line of yt-dlp's live_chat JSON."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    for act in d.get("replayChatItemAction", {}).get("actions", []):
        item = act.get("addChatItemAction", {}).get("item", {})
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        parts = []
        for run in r.get("message", {}).get("runs", []):
            if "text" in run:
                parts.append(run["text"])
            elif "emoji" in run:
                e = run["emoji"]
                eid = e.get("emojiId", "")
                parts.append(eid if len(eid) <= 4 else (e.get("shortcuts") or [""])[0])
        return " ".join(p for p in parts if p).strip()
    return None


def twitch_text(line: bytes) -> str | None:
    """Message text from a raw Twitch IRC line (':nick!nick@nick.tmi.twitch.tv PRIVMSG #chan :text')."""
    i = line.find(b" PRIVMSG ")
    if i < 0:
        return None
    j = line.find(b" :", i)
    return line[j + 2:].decode("utf-8", "replace") if j >= 0 else None


class Messages:
    """Recent message texts (for 'what was chat typing around this moment')."""

    def __init__(self, keep_secs: float = 2400):
        self.keep = keep_secs
        self.items: deque[tuple[float, str]] = deque()
        self.lock = threading.Lock()

    def add(self, ts: float, text: str) -> None:
        norm = normalize(text)
        if norm is None:
            return
        with self.lock:
            self.items.append((ts, norm))
            cutoff = ts - self.keep
            while self.items and self.items[0][0] < cutoff:
                self.items.popleft()

    def top(self, w0: float, w1: float, k: int = 6, min_count: int = 3) -> list[tuple[str, int]]:
        """Most repeated messages; spelling variants (LMAO / LMAOO / LMAOOO) count as one."""
        with self.lock:
            c = Counter(m for t, m in self.items if w0 <= t <= w1)
        groups: dict[str, Counter] = {}
        for m, n in c.items():
            groups.setdefault(re.sub(r"(.)\1+", r"\1", m), Counter())[m] = n
        merged = [(g.most_common(1)[0][0], sum(g.values())) for g in groups.values()]
        merged.sort(key=lambda x: -x[1])
        return [(m, n) for m, n in merged[:k] if n >= min_count]


# ---------------------------------------------------------------- Twitch viewer clips

def twitch_credentials() -> tuple[str, str] | None:
    cid, secret = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    f = ROOT / "credentials" / "twitch.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("client_id") and d.get("client_secret"):
                return d["client_id"], d["client_secret"]
        except ValueError:
            pass
    return None


def _iso_to_epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class TwitchClips:
    """Polls the Twitch API for clips viewers make of this stream (Helix /clips)."""

    POLL = 60

    def __init__(self, login: str, creds: tuple[str, str], since: float):
        self.login, (self.cid, self.secret) = login, creds
        self.since = since
        self.token = ""
        self.broadcaster = ""
        self.seen: set[str] = set()
        self.new: list[dict] = []
        self.lock = threading.Lock()
        self.stopping = False
        self.error = ""

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self.stopping = True

    def take(self) -> list[dict]:
        with self.lock:
            out, self.new = self.new, []
        return out

    def _auth(self) -> None:
        q = urllib.parse.urlencode({"client_id": self.cid, "client_secret": self.secret,
                                    "grant_type": "client_credentials"})
        with urllib.request.urlopen(urllib.request.Request(f"https://id.twitch.tv/oauth2/token?{q}", method="POST"),
                                    timeout=30) as r:
            self.token = json.load(r)["access_token"]

    def _get(self, path: str, params: dict) -> dict:
        if not self.token:
            self._auth()
        url = f"https://api.twitch.tv/helix/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:  # token expired
                self.token = ""
            raise

    def poll(self) -> list[dict]:
        if not self.broadcaster:
            users = self._get("users", {"login": self.login}).get("data", [])
            if not users:
                raise RuntimeError(f"Twitch user {self.login!r} not found")
            self.broadcaster = users[0]["id"]
        started = datetime.fromtimestamp(self.since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        found, cursor = [], None
        for _ in range(5):
            params = {"broadcaster_id": self.broadcaster, "started_at": started, "first": 100}
            if cursor:
                params["after"] = cursor
            d = self._get("clips", params)
            for c in d.get("data", []):
                if c["id"] in self.seen:
                    continue
                self.seen.add(c["id"])
                found.append({"id": c["id"], "title": c.get("title", ""), "views": c.get("view_count", 0),
                              "url": c.get("url", ""), "duration": float(c.get("duration") or 30),
                              "created": _iso_to_epoch(c["created_at"])})
            cursor = d.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return found

    def _loop(self) -> None:
        while not self.stopping:
            try:
                found = self.poll()
                self.error = ""
                if found:
                    with self.lock:
                        self.new += found
            except Exception as exc:  # noqa: BLE001 - optional signal; never stop the session for it
                if str(exc) != self.error:
                    log(f"  Twitch clips: {str(exc)[:160]}")
                self.error = str(exc)
            for _ in range(self.POLL):
                if self.stopping:
                    return
                time.sleep(1)
