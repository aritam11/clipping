"""What the audience is looking for right now, for one streamer.

* YouTube search suggestions for "<streamer> <game>": the phrases people actually type (e.g. "speed gta 6
  cops", "speed gta 6 rage"). These are moments people go looking for.
* The most-viewed recent Shorts about the streamer: which kinds of moments already get millions of views.

The picker gets both as context, to judge which moments people want and to title clips with the words
they search. Public data only, no API key; a failure just means the picker goes without it.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request

from .util import log, ytdlp_cmd

GAMES = [(r"gta ?(6|vi)\b", "gta 6"), (r"gta ?(5|v)\b|gta online|gta rp", "gta 5"), (r"minecraft", "minecraft"),
         (r"fortnite", "fortnite"), (r"valorant", "valorant"), (r"warzone|call of duty|\bcod\b", "warzone"),
         (r"\bfifa\b|ea ?fc", "ea fc"), (r"\bnba ?2k", "nba 2k"), (r"roblox", "roblox")]
GENERIC = {"live", "stream", "livestream", "gameplay", "full", "full stream", "vod", "today", "now", "new",
           "part 1", "part 2", "episode 1", "2025", "2026", "shorts", "video", "videos", "clips", "youtube", "twitch"}
_cache: dict[str, tuple[float, str]] = {}


def game_from(*texts: str) -> str:
    t = " ".join(texts).lower()
    return next((name for pat, name in GAMES if re.search(pat, t)), "")


def suggestions(query: str) -> list[str]:
    url = "https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    q = query.lower().strip()
    out = []
    for s in data[1]:
        words = s.lower().removeprefix(q).split()
        if words and words[-1] not in GENERIC and " ".join(words) not in GENERIC:  # "... live" says nothing
            out.append(s)
    return out[:10]


def top_shorts(query: str, n: int = 12) -> list[tuple[str, int]]:
    """Most-viewed clip-length videos (Shorts and short clips) for the query uploaded this month."""
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
           + "&sp=CAMSBAgEEAE%253D")  # sort by view count, this month, videos only
    r = subprocess.run([*ytdlp_cmd(), "--flat-playlist", "--dump-single-json", "--no-warnings",
                        "--playlist-end", "60", url], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    entries = json.loads(r.stdout or "{}").get("entries") or []
    out = []
    for e in entries:
        title, dur = e.get("title") or "", e.get("duration") or 0
        if not title or not (0 < dur <= 600) or re.search(r"\btrailer\b|\blive\b|🔴|full stream", title, re.I):
            continue  # full streams and trailers say nothing about which moments get views
        out.append((title, int(e.get("view_count") or 0)))
    return sorted(out, key=lambda x: -x[1])[:n]


def _views(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else (f"{n / 1e3:.0f}K" if n >= 1e3 else str(n))


def brief(streamer: str, game: str = "", ttl: float = 3600) -> str:
    """A short block for the picker's prompt ('' if nothing could be fetched). Cached for `ttl` seconds."""
    if not streamer:
        return ""
    key = f"{streamer}|{game}"
    if key in _cache and time.time() - _cache[key][0] < ttl:
        return _cache[key][1]
    base = f"{streamer} {game}".strip()
    sugg: list[str] = []
    for q in dict.fromkeys([base, f"{base} funny", streamer]):
        try:
            sugg += [s for s in suggestions(q) if s not in sugg and s.lower() != base.lower()]
        except Exception as exc:  # noqa: BLE001
            log(f"  audience demand: search suggestions failed ({str(exc)[:80]})")
            break
    try:
        shorts = top_shorts(base)
    except Exception as exc:  # noqa: BLE001
        log(f"  audience demand: top Shorts search failed ({str(exc)[:80]})")
        shorts = []
    lines = []
    if sugg:
        lines.append("What viewers are searching for on YouTube right now: " + "; ".join(sugg[:12]))
    names = [w for w in re.findall(r"[a-z0-9]+", streamer.lower()) if len(w) >= 3]
    about = [x for x in shorts if any(w in x[0].lower().replace(" ", "") for w in names)]
    other = [x for x in shorts if x not in about][:3]
    if about:
        lines.append("Most-viewed short videos about this streamer this month (what already gets views): "
                     + "; ".join(f'"{t[:70]}" ({_views(v)})' for t, v in about))
    if other and game:
        lines.append(f"Most-viewed short {game} videos this month: "
                     + "; ".join(f'"{t[:70]}" ({_views(v)})' for t, v in other))
    text = ""
    if lines:
        text = ("\n".join(lines) + "\nUse these to judge which moments people want and to title clips with the "
                "words they search. Don't copy those titles, and only claim what happens in your clip.")
    _cache[key] = (time.time(), text)
    return text
