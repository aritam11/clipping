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

    # Highlight picker backend: auto | api | claude-cli | none
    llm: str = "auto"
    api_model: str = "claude-opus-5-5"
    cli_model: str = "opus"

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

    keep_work: bool = False
    max_height: int = 1080
    range: tuple[float, float] | None = None  # only process this part of the source (seconds)
