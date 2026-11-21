"""Word-by-word animated captions (ASS subtitles, burned in by FFmpeg/libass)."""

from __future__ import annotations

from pathlib import Path

HIGHLIGHT = "&H0000FFFF&"   # ASS colours are &HAABBGGRR -> yellow
OTHER_BASE = "&H00FFD27A&"  # light blue: someone other than the streamer (friend on call, RP player)
OTHER_HIGHLIGHT = "&H00FFFFFF&"
MAX_WORDS = 3
MAX_CHARS = 16


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    cs = int(round((s - int(s)) * 100))
    if cs == 100:
        s, cs = s + 1, 0
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{cs:02d}"


def _clean(w: str) -> str:
    return w.strip().replace("{", "").replace("}", "").replace("\\", "").upper()


def words_in_range(transcript: dict, start: float, end: float) -> list[dict]:
    out = []
    for seg in transcript.get("segments", []):
        if seg["end"] < start or seg["start"] > end:
            continue
        for w in seg.get("words", []):
            if w["start"] >= start - 0.05 and w["end"] <= end + 0.05 and _clean(w["word"]):
                out.append({"start": w["start"] - start, "end": w["end"] - start, "word": _clean(w["word"]),
                            "speaker": w.get("speaker", "streamer")})
    return out


def for_captions(words: list[dict], voices: str = "auto") -> list[dict]:
    """Which words get captions. auto: the streamer and other real people, not game dialogue (the game
    shows its own subtitles); streamer: the streamer only; all: everything Whisper heard."""
    if voices == "all":
        return words
    keep = {"streamer"} if voices == "streamer" else {"streamer", "other"}
    return [w for w in words if w.get("speaker", "streamer") in keep]


def _groups(words: list[dict]) -> list[list[dict]]:
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= MAX_WORDS
                    or len(" ".join(x["word"] for x in cur + [w])) > MAX_CHARS
                    or w["start"] - cur[-1]["end"] > 0.45
                    or w.get("speaker") != cur[-1].get("speaker")):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["word"].endswith((".", "?", "!", ",")):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], duration: float, out: Path, caption_y: int, font: str,
              hook: str = "", hook_secs: float = 2.8) -> Path:
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},86,&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1",
        f"Style: Hook,{font},62,&H00000000,&H000000FF,&H00FFFFFF,&H00000000,-1,0,0,0,100,100,0,0,3,16,0,5,80,80,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook.strip():
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(hook_secs, duration))},Hook,,0,0,0,,"
                     f"{{\\pos(540,300)\\fad(120,200)}}{_clean(hook)}")

    groups = _groups(words)
    for gi, g in enumerate(groups):
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else duration
        g_end = min(g[-1]["end"] + 0.3, next_start, duration)
        for wi, w in enumerate(g):
            ev_start = w["start"]
            ev_end = g[wi + 1]["start"] if wi + 1 < len(g) else g_end
            if ev_end <= ev_start:
                continue
            other = g[0].get("speaker") == "other"
            base, hi = (OTHER_BASE, OTHER_HIGHLIGHT) if other else ("", HIGHLIGHT)
            restore = f"{{\\c{base}}}" if base else ""   # \\r resets to the style's white
            parts = []
            for k, x in enumerate(g):
                shown = x["word"].rstrip(",.")
                if k == wi:
                    parts.append(f"{{\\c{hi}\\fscx112\\fscy112}}{shown}{{\\r}}{restore}")
                else:
                    parts.append(shown)
            pop = "\\t(0,80,\\fscx104\\fscy104)\\t(80,160,\\fscx100\\fscy100)" if wi == 0 else ""
            colour = f"\\c{base}" if base else ""
            lines.append(f"Dialogue: 0,{_ts(ev_start)},{_ts(ev_end)},Cap,,0,0,0,,"
                         f"{{\\pos(540,{caption_y}){colour}{pop}}}{' '.join(parts)}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
