"""Run settings. Every field maps to a CLI flag in cli.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    out_dir: Path = ROOT / "output"
    work_dir: Path = ROOT / "work"

    # Clip selection
    clips: int | None = None          # None = scale with video length
    min_len: float = 15.0
    max_len: float = 60.0
    topic: str = "GTA gameplay and streamer moments"
    hashtags: list[str] = field(default_factory=lambda: ["#shorts", "#gaming"])

    # Highlight picker backend: auto | api | claude-cli | codex | none
    llm: str = "auto"
    api_model: str = "claude-opus-5-5"
    cli_model: str = "opus"
    picker_prompt: str = ""           # path to a skill/prompt file that replaces the built-in picker prompt
    codex_model: str = ""             # for --llm codex; empty = the model set in your Codex config

    # Transcription
    whisper_model: str = "large-v3-turbo"
    language: str | None = None

    # Reframing: auto | stack | face | zoom | blur | crop
    layout: str = "auto"
    fallback_layout: str = "zoom"     # used by auto when no facecam/face is found
    facecam: tuple[float, float, float, float] | None = None  # manual x,y,w,h (0-1)

    # Captions
    hook_text: bool = True
    font: str = "Arial Black"

    # Editing (live mode): cut dead air / keep the reaction, and a designed thumbnail
    tighten: bool = True
    thumbnail: bool = True
    thumb_frame: bool = True          # also show the thumbnail as the clip's first frame
    speakers: bool = True             # tell the streamer apart from game dialogue / other voices
    caption_voices: str = "auto"      # auto (streamer + real people) | streamer | all
    min_score: float = 70             # live: a clip's combined score must reach this to be made
    frame_check: bool = True          # live: show the picker frames of each candidate before making it
    demand: bool = True               # live: tell the picker what viewers search for / watch

    keep_work: bool = False
    max_height: int = 1080
    range: tuple[float, float] | None = None  # only process this part of the source (seconds)

    # YouTube auto-upload (see clipper/youtube.py)
    upload: bool = False
    privacy: str = "public"
    streamer: str = ""
    uploads_per_day: int = 50
    yt_category: str = "20"
