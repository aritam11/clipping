"""Who is talking in each transcript line: the streamer, the game (NPC dialogue), or someone else.

Whisper hears everything in the stream audio, so without this, a story-mode cutscene's dialogue is
captioned as if the streamer said it (on top of the game's own subtitles), and the picker can credit
an NPC's line to the streamer in a title. Three signals, strongest first:

1. Game subtitles. Story-mode games print their dialogue on screen. A line whose words match the
   subtitle text (OCR of the subtitle band, lower middle of the frame) is game dialogue. This is
   precise, and it never confuses NPCs with friends on a call or RP players, who get no subtitles.
2. Cutscene bars. Black cinematic bars at the top and bottom mean a cutscene.
3. Voice prints (WeSpeaker ResNet34). The streamer's voice is learned during the stream from lines
   with no game subtitle and no cutscene; NPC voices from subtitle-matched lines. During a cutscene or
   while subtitles are up, a line that sounds more like the NPCs than the streamer is game dialogue.
   Outside them, a clearly different voice is "other": friends on call, RP players, text-to-speech.

Voice prints only break ties: a line is never dropped as game dialogue on voice alone without a
cutscene or subtitles around it, and "other" only changes the caption colour.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import urllib.request
import wave
from pathlib import Path

import numpy as np

from .config import ROOT
from .util import log

STREAMER, GAME, OTHER = "streamer", "game", "other"
MODEL = ROOT / "models" / "voxceleb_resnet34_LM.onnx"
MODEL_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"
SR = 16000
FPS = 2.0                    # subtitle / cutscene samples per second
BAND = (0.74, 0.98)          # subtitle band, as fractions of the frame height
BAR = 0.07                   # cinematic bar height checked at the top and bottom
CENTER = (0.26, 0.64)        # where games put big event text (WASTED, MISSION FAILED, ...)
EVENT_EVERY = 4              # read the centre every 4th sample (every 2 s)
EVENTS = re.compile(r"\b(wasted|busted|mission (failed|passed|complete)|failed|passed|eliminated|knocked out|"
                    r"you died|game over|victory|winner|arrested|new record|level up|achievement)\b", re.I)
MATCH = 0.6                  # share of a line's letters found in the on-screen subtitles = game dialogue
MATURE_STREAMER = 45.0       # seconds of streamer speech before voice prints are trusted
MATURE_GAME = 20.0
OTHER_SIM = 0.15             # below this similarity to the streamer (outside cutscenes) = another person
KEEP_SECS = 2400


def _letters(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_share(line: str, subtitles: list[str]) -> float:
    """Share of the line's letters that appear, in order, in the subtitle text (OCR often drops spaces)."""
    seg, pool = _letters(line), _letters(" ".join(dict.fromkeys(subtitles)))
    if not seg or not pool:
        return 0.0
    m = difflib.SequenceMatcher(None, seg, pool, autojunk=False)
    return sum(b.size for b in m.get_matching_blocks() if b.size >= 3) / len(seg)


def fbank(x: np.ndarray) -> np.ndarray:
    """80-band log mel filterbank, Kaldi-style (what WeSpeaker models are trained on)."""
    x = x.astype(np.float64) * 32768
    fl, fs, nfft, nb = 400, 160, 512, 80
    n = 1 + (len(x) - fl) // fs
    if n < 1:
        return np.zeros((0, nb))
    fr = x[np.arange(fl)[None, :] + fs * np.arange(n)[:, None]]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.concatenate([fr[:, :1] * (1 - 0.97), fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)
    fr = fr * (0.54 - 0.46 * np.cos(2 * np.pi * np.arange(fl) / (fl - 1)))
    spec = np.abs(np.fft.rfft(fr, nfft)) ** 2
    mel = lambda f: 1127 * np.log(1 + f / 700)  # noqa: E731
    lo, hi = mel(20), mel(SR / 2)
    d = (hi - lo) / (nb + 1)
    fm = mel(np.arange(nfft // 2) * SR / nfft)
    left = lo + d * np.arange(nb)[:, None]
    w = np.maximum(0, np.minimum((fm - left) / d, (left + 2 * d - fm) / d))
    w = np.concatenate([w, np.zeros((nb, 1))], axis=1)
    return np.log(np.maximum(spec @ w.T, np.finfo(np.float32).eps))


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768


class Speakers:
    """Labels transcript lines as streamer / game / other, as a live stream comes in."""

    def __init__(self, W: int, H: int):
        self.W, self.H = W, H
        self._ocr = None
        self._sess = None
        self.voice_ok = True
        self.subs: list[tuple[float, str]] = []    # (stream time, centred text in the subtitle band)
        self.bars: list[tuple[float, bool]] = []   # (stream time, cinematic bars on screen)
        self.events: list[tuple[float, str]] = []  # (stream time, big centre text such as WASTED)
        self.c_streamer, self.n_streamer = np.zeros(256), 0.0
        self.c_game, self.n_game = np.zeros(256), 0.0

    # ------------------------------------------------------------ models

    def ocr(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            # Limit by the longest side (the subtitle band is wide and short), no rotation check, 2 threads
            # so transcription keeps the rest of the CPU.
            self._ocr = RapidOCR(det_limit_side_len=960, det_limit_type="max", use_cls=False,
                                 det_intra_op_num_threads=2, rec_intra_op_num_threads=2)
        return self._ocr

    def sess(self):
        if self._sess is None and self.voice_ok:
            try:
                if not MODEL.exists():
                    log("Downloading the voice model (26 MB, once)")
                    tmp = MODEL.with_suffix(".part")
                    urllib.request.urlretrieve(MODEL_URL, tmp)
                    tmp.replace(MODEL)
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 2
                self._sess = ort.InferenceSession(str(MODEL), opts, providers=["CPUExecutionProvider"])
            except Exception as exc:  # noqa: BLE001 - subtitles still work without voice prints
                log(f"  voice model unavailable ({str(exc)[:120]}); using subtitles and cutscene bars only")
                self.voice_ok = False
        return self._sess

    def embed(self, x: np.ndarray) -> np.ndarray | None:
        if len(x) < 0.8 * SR or self.sess() is None:
            return None
        f = fbank(x)
        f = f - f.mean(axis=0)
        e = self._sess.run(None, {"feats": f[None].astype(np.float32)})[0][0]
        n = np.linalg.norm(e)
        return e / n if n > 0 else None

    # ------------------------------------------------------------ video: subtitles and cutscene bars

    def scan(self, paths: list[Path], g0: float) -> None:
        """Read subtitles and cutscene bars from new stream segments (g0 = stream time of the first)."""
        W, H = self.W, self.H
        b0, b1 = int(H * BAND[0]) // 2 * 2, int(H * BAND[1]) // 2 * 2
        bar = max(2, int(H * BAR) // 2 * 2)
        c0, c1 = int(H * CENTER[0]) // 2 * 2, int(H * CENTER[1]) // 2 * 2
        lst = paths[0].parent / "scan.txt"
        lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in paths), encoding="utf-8")
        try:
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-an", "-filter_complex",
                 f"[0:v]fps={FPS},split=4[a][b][c][d];[a]crop={W}:{b1 - b0}:0:{b0}[band];"
                 f"[b]crop={W}:{bar}:0:0[top];[c]crop={W}:{bar}:0:{H - bar}[bot];[d]crop={W}:{c1 - c0}:0:{c0}[mid];"
                 f"[band][top][bot][mid]vstack=4[v]",
                 "-map", "[v]", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, timeout=300)
        finally:
            lst.unlink(missing_ok=True)
        rows = (b1 - b0) + 2 * bar + (c1 - c0)
        size = W * rows * 3
        prev, last = None, []
        for k in range(len(r.stdout) // size):
            fr = np.frombuffer(r.stdout[k * size:(k + 1) * size], np.uint8).reshape(rows, W, 3)
            t = g0 + k / FPS
            n0 = b1 - b0
            band, top, bot, mid = fr[:n0], fr[n0:n0 + bar], fr[n0 + bar:n0 + 2 * bar], fr[n0 + 2 * bar:]
            self.bars.append((t, self._is_bar(top) and self._is_bar(bot)))
            if k % EVENT_EVERY == 0:
                self._event_text(mid, t)
            mask = band.min(axis=2) > 190  # subtitle text is white (greyish after stream compression)
            if mask.mean() < 0.0003:  # no white text at all (a subtitle line is ~0.1-0.5% of the band)
                prev, last = None, []
                continue
            if prev is None or (mask & prev).sum() / max((mask | prev).sum(), 1) <= 0.8:  # subtitle changed
                prev = mask
                try:
                    res, _ = self.ocr()(band)
                except Exception:  # noqa: BLE001
                    res = None
                last = [txt for box, txt, conf in res or []
                        if conf > 0.6 and 0.3 < (min(p[0] for p in box) + max(p[0] for p in box)) / 2 / W < 0.7]
            self.subs += [(t, txt) for txt in last]
        cutoff = g0 - KEEP_SECS
        self.subs = [s for s in self.subs if s[0] >= cutoff]
        self.bars = [b for b in self.bars if b[0] >= cutoff]
        self.events = [x for x in self.events if x[0] >= cutoff]

    def _event_text(self, mid: np.ndarray, t: float) -> None:
        """Big game text in the middle of the screen: deaths, arrests, mission results."""
        try:
            res, _ = self.ocr()(mid)
        except Exception:  # noqa: BLE001
            return
        for box, txt, conf in res or []:
            tall = max(p[1] for p in box) - min(p[1] for p in box)
            m = EVENTS.search(txt)
            if m and conf > 0.7 and tall >= 0.035 * self.H:  # big text, not HUD or chat
                if not any(abs(t - t2) < 6 and txt2 == m.group(0).upper() for t2, txt2 in self.events[-5:]):
                    self.events.append((t, m.group(0).upper()))

    @staticmethod
    def _is_bar(strip: np.ndarray) -> bool:
        """A black cinematic bar: the middle of the strip is near-black and flat (overlays sit at the edges)."""
        w = strip.shape[1]
        mid = strip[:, w // 4:w * 3 // 4].astype(np.int16)
        return float(mid.mean()) < 14 and float(mid.std()) < 6

    def _subs_near(self, a: float, b: float) -> list[str]:
        return [txt for t, txt in self.subs if a - 1.5 <= t <= b + 1.5]

    def _cutscene(self, a: float, b: float) -> bool:
        v = [on for t, on in self.bars if a - 0.5 <= t <= b + 0.5]
        return bool(v) and sum(v) >= 0.6 * len(v)

    # ------------------------------------------------------------ labelling

    def _learn(self, who: str, e: np.ndarray, secs: float) -> None:
        if who == STREAMER:
            self.c_streamer, self.n_streamer = self.c_streamer + e * secs, self.n_streamer + secs
        else:
            self.c_game, self.n_game = self.c_game + e * secs, self.n_game + secs

    def _sim(self, e: np.ndarray, who: str) -> float | None:
        c, n, mature = ((self.c_streamer, self.n_streamer, MATURE_STREAMER) if who == STREAMER
                        else (self.c_game, self.n_game, MATURE_GAME))
        if n < mature:
            return None
        return float(e @ (c / (np.linalg.norm(c) + 1e-9)))

    def label(self, segments: list[dict], audio: np.ndarray, a0: float) -> dict[str, int]:
        """Set seg['speaker'] and each word's 'speaker'. audio: 16 kHz mono starting at stream time a0."""
        counts = {STREAMER: 0, GAME: 0, OTHER: 0}
        for seg in segments:
            a, b = seg["start"], seg["end"]
            text = " ".join(w["word"] for w in seg.get("words", [])) or seg.get("text", "")
            near, cut = self._subs_near(a, b), self._cutscene(a, b)
            share = match_share(text, near) if near else 0.0
            e = self.embed(audio[max(0, int((a - a0) * SR)):max(0, int((b - a0) * SR))]) if self.voice_ok else None
            s_str = self._sim(e, STREAMER) if e is not None else None
            s_game = self._sim(e, GAME) if e is not None else None
            short = len(_letters(text)) < 6  # "What?", "Yeah.": only if that exact subtitle is up right then
            if (share >= MATCH and not short) or (short and _letters(text) and match_share(text, [
                    txt for t, txt in self.subs if a - 0.5 <= t <= b + 0.5 and len(_letters(txt)) <= 12]) == 1.0):
                who = GAME
            elif (near or cut) and s_str is not None and s_game is not None and s_game > s_str + 0.05:
                who = GAME   # sounds like the NPCs, while the game is talking
            elif cut and s_str is not None and s_str < OTHER_SIM:
                who = GAME   # a cutscene, and not the streamer's voice
            elif not (near or cut) and s_str is not None and s_str < OTHER_SIM and b - a >= 1.5:
                who = OTHER  # a different person: friend on call, RP player, text-to-speech
            else:
                who = STREAMER
            if e is not None and b - a >= 1.0:
                if who == GAME and share >= MATCH:
                    self._learn(GAME, e, b - a)
                elif who == STREAMER and not near and not cut and (s_str is None or s_str >= 0.3):
                    self._learn(STREAMER, e, b - a)
            seg["speaker"] = who
            for w in seg.get("words", []):
                w["speaker"] = who
            counts[who] += 1
        return counts
