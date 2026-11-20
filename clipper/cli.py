"""`clip run <url|file>...` and `clip watch sources.txt`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .util import log, parse_ts, require_tools, utf8_stdio


def _common(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("clip selection")
    g.add_argument("--clips", type=int, help="clips per video (default: 1 per 10 min, max 12)")
    g.add_argument("--min-len", type=float, default=15)
    g.add_argument("--max-len", type=float, default=60)
    g.add_argument("--topic", default=Settings.topic, help="niche hint for Claude, e.g. 'GTA VI roleplay'")
    g.add_argument("--hashtags", default="#shorts #gaming", help="default hashtags for heuristic picks")
    g.add_argument("--llm", choices=["auto", "api", "claude-cli", "codex", "none"], default="auto",
                   help="auto: Anthropic API key, else the claude CLI, else the codex CLI, else heuristic")
    g.add_argument("--codex-model", default="", help="model for --llm codex (default: your Codex config's model)")
    g.add_argument("--picker-prompt", default="",
                   help="skill/prompt file for the clip picker, e.g. skills/clip-picker/SKILL.md")
    g.add_argument("--api-model", default=Settings.api_model)
    g.add_argument("--cli-model", default=Settings.cli_model)
    g = p.add_argument_group("transcription")
    g.add_argument("--whisper-model", default=Settings.whisper_model, help="e.g. large-v3-turbo, small, medium")
    g.add_argument("--language", help="force a language code, e.g. en")
    g = p.add_argument_group("look")
    g.add_argument("--layout", choices=["auto", "stack", "face", "zoom", "blur", "crop"], default="auto")
    g.add_argument("--fallback-layout", choices=["zoom", "blur", "crop"], default="zoom",
                   help="what auto uses when no facecam/face is found")
    g.add_argument("--facecam", help="manual facecam box as x,y,w,h fractions, e.g. 0,0.7,0.25,0.3")
    g.add_argument("--no-hook", action="store_true", help="no hook text banner at the start")
    g.add_argument("--font", default=Settings.font)
    g = p.add_argument_group("YouTube upload")
    g.add_argument("--upload", action="store_true", help="upload each clip to your YouTube channel as it's made")
    g.add_argument("--privacy", choices=["public", "unlisted", "private"], default="public")
    g.add_argument("--streamer", default="", help="streamer name for titles/hashtags (default: from the source)")
    g.add_argument("--uploads-per-day", type=int, default=50, help="daily upload cap (YouTube allows ~100)")
    g.add_argument("--yt-category", default="20", help="YouTube category id (20 = Gaming)")
    g = p.add_argument_group("files")
    g.add_argument("--out", type=Path, default=Settings.out_dir)
    g.add_argument("--work", type=Path, default=Settings.work_dir)
    g.add_argument("--keep-work", action="store_true", help="keep downloaded media after rendering")
    g.add_argument("--max-height", type=int, default=1080)


def _settings(a: argparse.Namespace) -> Settings:
    facecam = None
    if a.facecam:
        vals = [float(v) for v in a.facecam.split(",")]
        if len(vals) != 4 or not all(0 <= v <= 1 for v in vals):
            sys.exit("--facecam needs four numbers between 0 and 1: x,y,w,h")
        facecam = tuple(vals)
    return Settings(
        out_dir=a.out, work_dir=a.work, clips=a.clips, min_len=a.min_len, max_len=a.max_len,
        topic=a.topic, hashtags=a.hashtags.split(), llm=a.llm, api_model=a.api_model, cli_model=a.cli_model,
        picker_prompt=a.picker_prompt, codex_model=a.codex_model,
        whisper_model=a.whisper_model, language=a.language, layout=a.layout,
        fallback_layout=a.fallback_layout, facecam=facecam, hook_text=not a.no_hook, font=a.font,
        keep_work=a.keep_work, max_height=a.max_height,
        upload=a.upload, privacy=a.privacy, streamer=a.streamer, uploads_per_day=a.uploads_per_day,
        yt_category=a.yt_category,
    )


def _youtube(a: argparse.Namespace) -> None:
    from . import youtube as yt
    cfg = yt.UploadConfig(privacy=a.privacy, streamer=a.streamer, per_day=a.uploads_per_day, category=a.yt_category)
    try:
        if a.action == "auth":
            print(f"Signed in. Clips will upload to: {yt.auth()}")
        elif a.action == "status":
            if not yt.signed_in():
                print("Not signed in. See 'YouTube uploads' in README.md, then run: uv run clip youtube auth")
            else:
                ch = yt.channel()
                print(f"Channel: {ch['title'] if ch else '?'}")
            entries = yt.ledger().values()
            count = lambda s: sum(1 for e in entries if e.get("status") == s)  # noqa: E731
            print(f"Uploaded in the last 24h: {yt.uploads_last_24h()} · total uploaded: {count('uploaded')} · "
                  f"queued: {count('queued')} · failed: {count('failed')}")
        elif a.action == "retry":
            done = yt.retry_pending(cfg)
            print(f"Processed {len(done)} queued/failed upload(s).")
        else:  # upload
            files = []
            for p in a.paths:
                files += sorted(p.glob("clip_*.mp4")) if p.is_dir() else [p]
            if not files:
                sys.exit("Give clip .mp4 files or output folders to upload.")
            for f in files:
                r = yt.upload(f, yt.meta_for(f), cfg, dry_run=a.dry_run)
                if a.dry_run:
                    sn = r["body"]["snippet"]
                    print(f"\n=== {f}\nTITLE: {sn['title']}\nTAGS: {', '.join(sn['tags'])}\n"
                          f"PRIVACY: {r['body']['status']['privacyStatus']}\nDESCRIPTION:\n{sn['description']}")
                else:
                    print(f"{f.name}: {r.get('status')} {r.get('url', r.get('reason', ''))}")
    except yt.NotSignedIn as exc:
        sys.exit(str(exc))


def main(argv: list[str] | None = None) -> None:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="clip", description="Turn long videos into captioned vertical shorts.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="clip one or more videos (URLs or files)")
    r.add_argument("sources", nargs="+")
    r.add_argument("--range", help="only clip part of the video, e.g. 2:00:00-3:00:00 (for very long VODs)")
    _common(r)

    w = sub.add_parser("watch", help="clip every new video from channels/folders listed in a file")
    w.add_argument("sources_file", type=Path)
    w.add_argument("--latest", type=int, default=3, help="how many recent videos to check per source")
    w.add_argument("--interval", type=float, default=0, help="minutes between checks (0 = one pass, for cron)")
    w.add_argument("--min-duration", type=float, default=60, help="skip videos shorter than this (seconds)")
    w.add_argument("--max-duration", type=float, help="skip videos longer than this (seconds)")
    _common(w)

    lv = sub.add_parser("live", help="clip a stream while it is live (YouTube, Twitch, Kick, ...)")
    lv.add_argument("url", help="live stream or channel URL, e.g. https://www.twitch.tv/name")
    lv.add_argument("--wait", action="store_true", help="if not live yet, wait for the stream to start")
    lv.add_argument("--review-every", type=float, default=10, help="minutes between Claude passes")
    lv.add_argument("--lookback", type=float, default=12, help="minutes of transcript per Claude pass")
    lv.add_argument("--segment", type=int, default=30, help="buffer segment length in seconds")
    lv.add_argument("--keep-minutes", type=float, default=20, help="minutes of video kept in the buffer")
    lv.add_argument("--quality", default="1080p60,1080p,720p60,720p,best")
    lv.add_argument("--spike-threshold", type=float, default=3.5, help="lower = more hype triggers")
    lv.add_argument("--no-spikes", action="store_true", help="only use the periodic Claude passes")
    lv.add_argument("--max-per-hour", type=int, default=12, help="cap on clips per hour")
    lv.add_argument("--max-minutes", type=float, help="stop after this many minutes")
    _common(lv)

    au = sub.add_parser("autolive", help="clip a channel automatically every time it goes live (background job)")
    au.add_argument("action", choices=["install", "remove", "status"])
    au.add_argument("url", nargs="?", help="channel live URL, e.g. https://www.youtube.com/@Name/live")
    au.add_argument("live_args", nargs=argparse.REMAINDER,
                    help="extra `clip live` options, after --  (e.g. -- --max-per-hour 6)")

    yt = sub.add_parser("youtube", help="YouTube channel: sign in, upload clips, retry the queue")
    yt.add_argument("action", choices=["auth", "status", "upload", "retry"])
    yt.add_argument("paths", nargs="*", type=Path, help="for upload: clip .mp4 files or output folders")
    yt.add_argument("--dry-run", action="store_true", help="show the title/description/tags without uploading")
    yt.add_argument("--privacy", choices=["public", "unlisted", "private"], default="public")
    yt.add_argument("--streamer", default="")
    yt.add_argument("--uploads-per-day", type=int, default=50)
    yt.add_argument("--yt-category", default="20")

    db = sub.add_parser("dashboard", help="open a live web dashboard of everything clipper is doing")
    db.add_argument("--port", type=int, default=8765)
    db.add_argument("--open", action="store_true", help="open it in your browser")

    a = ap.parse_args(argv)
    if a.cmd == "youtube":
        _youtube(a)
        return
    if a.cmd == "dashboard":
        from .dashboard import serve
        serve(a.port, a.open)
        return
    if a.cmd == "autolive":
        from . import autolive
        if a.action == "status":
            autolive.status()
        elif not a.url:
            sys.exit("give the channel URL, e.g. https://www.youtube.com/@Name/live")
        elif a.action == "install":
            autolive.install(a.url, [x for x in a.live_args if x != "--"])
        else:
            autolive.remove(a.url)
        return
    require_tools()
    st = _settings(a)
    if getattr(a, "range", None):
        try:
            s, e = (parse_ts(p) for p in a.range.split("-", 1))
        except ValueError:
            sys.exit("--range must look like 2:00:00-3:00:00")
        if e <= s:
            sys.exit("--range end must be after its start")
        st.range = (s, e)
    st.out_dir.mkdir(parents=True, exist_ok=True)
    st.work_dir.mkdir(parents=True, exist_ok=True)

    if a.cmd == "run":
        from .pipeline import process
        failed = 0
        for s in a.sources:
            try:
                process(s, st)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log(f"FAILED {s}: {exc}")
        sys.exit(1 if failed else 0)
    elif a.cmd == "live":
        from .live import Live
        try:
            Live(a.url, st, seg_secs=a.segment, review_every=a.review_every * 60, lookback=a.lookback * 60,
                 keep_minutes=a.keep_minutes, quality=a.quality, spikes=not a.no_spikes,
                 spike_threshold=a.spike_threshold, max_per_hour=a.max_per_hour,
                 max_minutes=a.max_minutes, wait=a.wait).run()
        except RuntimeError as exc:
            sys.exit(str(exc))
    else:
        from .watch import watch
        if not a.sources_file.exists():
            sys.exit(f"{a.sources_file} not found")
        watch(a.sources_file, st, a.latest, a.interval * 60, a.min_duration, a.max_duration)


if __name__ == "__main__":
    main()
