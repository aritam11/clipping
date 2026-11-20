# clipper

Turns long gameplay videos and stream VODs into captioned 9:16 shorts for TikTok, Reels and YouTube Shorts, automatically.

```
URL / file / watched folder
  → yt-dlp: audio only (video is fetched later, just for the chosen clips)
  → faster-whisper: transcript with a timestamp per word (Apple GPU, falls back to CPU)
  → loudness curve: yelling, laughter, gunfire, explosions
  → Claude picks the moments, plus a title, hook text and hashtags (offline heuristic fallback)
  → yt-dlp: downloads only those sections
  → reframe to 1080x1920: facecam on top + gameplay below / crop that follows faces shot by shot / zoomed centre
  → word-by-word animated captions + hook banner, loudness normalised to -14 LUFS
  → output/<video>/clip_01.mp4 … + posts.md (captions ready to paste)
```

## Set up on a new device

1. Install the tools: [uv](https://docs.astral.sh/uv/) and FFmpeg (with libass, for captions).
   - macOS: `brew install uv ffmpeg`
   - Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh` and `sudo apt install ffmpeg`
   - Windows: `winget install astral-sh.uv Gyan.FFmpeg`
2. Get the code and install dependencies:
   ```bash
   git clone https://github.com/aritam11/clipping.git
   cd clipping
   uv sync
   ```
3. Give it access to Claude for picking clips, either way works:
   - install [Claude Code](https://claude.com/claude-code) and sign in once (`claude`), or
   - set `ANTHROPIC_API_KEY` in your environment.
   Without either, it still runs with the simpler offline picker.
4. First run downloads the Whisper model (~1.6 GB, cached afterwards). Leave a few GB of free disk for the model, downloads and clips.

Platforms: developed and tested on macOS (Apple Silicon). The code is written to also run on Windows and Linux (no macOS-only calls on those paths, UTF-8 file handling, Windows-safe process handling), but it hasn't been run on them yet, so report anything that breaks.

- Transcription device is picked automatically: Apple GPU on M-series Macs, NVIDIA GPU (CUDA) on Windows/Linux when CUDA and cuDNN are installed, otherwise CPU. On CPU, `--whisper-model small` is much faster.
- `clip autolive` (start on every stream) is macOS-only. On Windows/Linux, run `clip live <url> --wait` from a terminal, Task Scheduler or systemd.
- Live mode keeps the computer awake while recording on macOS and Windows.

## Use it

```bash
# one video (YouTube, Twitch VOD, Kick, … or a local file)
uv run clip run "https://www.youtube.com/watch?v=VIDEO_ID"
uv run clip run ~/Movies/OBS/recording.mkv --clips 5

# part of a very long VOD (e.g. hour 3 of a 24-hour stream)
uv run clip run "https://www.youtube.com/watch?v=VIDEO_ID" --range 3:00:00-4:00:00

# hands-off: clip every new video from the channels/folders in sources.txt
cp sources.example.txt sources.txt   # then edit it
uv run clip watch sources.txt --interval 30     # checks every 30 min; Ctrl-C to stop
uv run clip watch sources.txt                   # one pass (for cron/launchd)

# live: clip a stream while it's happening (Ctrl-C to stop; finishes the last clips first)
uv run clip live "https://www.twitch.tv/STREAMER" --wait
uv run clip live "https://www.youtube.com/watch?v=LIVE_ID"
```

## Live mode

`clip live` records the stream into a rolling buffer (`--keep-minutes`, default 20) and handles each 30-second segment as it arrives:

- **Transcribes it** (the model stays loaded, so it keeps up with real time), and tracks loudness and the **live-chat message rate**: anonymous Twitch IRC, or YouTube live chat via yt-dlp.
- **Hype spikes**: a loudness or chat burst well above the stream's normal level triggers a quick Claude check of the surrounding ~90 s. If Claude agrees, the clip is cut about 20 s after the moment.
- **Claude pass every `--review-every` minutes** (default 10) over the last `--lookback` minutes (default 12), to catch moments the spikes missed.
- Clips are cut straight from the buffer and land in `output/live-<stream>-<time>/`, with a `posts.md` that grows as clips are made.
- Survives dropped connections (reconnects while the stream is live). Stops when the stream ends. `--wait` waits for the stream to start. `--max-per-hour` caps output.
- Unattended safety: keeps the Mac from idle-sleeping while recording, stops saving clips below 1 GB free disk and ends the session below 0.6 GB. If Claude is unavailable (e.g. usage limit), hype spikes are still clipped without it.

### YouTube uploads

Add `--upload` to `clip run`, `clip watch` or `clip live` (or to an `autolive install … --` line) and each clip goes to your channel as a Short the moment it's rendered:

```bash
uv run clip live "https://www.youtube.com/@IShowSpeed/live" --upload
uv run clip autolive install "https://www.youtube.com/@IShowSpeed/live" -- --upload --max-per-hour 6
```

What it sets on each upload:
- **Title:** Claude's hook title, plus the streamer's name if it isn't already in it, plus `#shorts` (at most 100 characters).
- **Description:** the hook line, a credit to the streamer with the stream's title and a link to the full stream, then the hashtags.
- **Hashtags and tags:** the streamer first (people search for the name), then the game (for example #GTA6), then Claude's picks and #shorts. Search tags include "<streamer> clips" and "<streamer> live".
- **Category and flags:** Gaming, not made for kids, and not AI-generated footage (it's real stream footage).

**Safety and limits:**
- No clip is uploaded twice; `work/uploads.json` records every upload.
- YouTube allows about 100 uploads a day per project. `--uploads-per-day` (default 50) keeps you under that. Clips past the cap, or made while you're signed out, wait as "queued" and go up on the next run or with `uv run clip youtube retry`.
- After each upload it checks the real privacy status. If YouTube locked the video to private (older, unaudited API projects), it tells you, and you can make the video public in YouTube Studio.
- Use `--privacy unlisted` or `--privacy private` to review clips before they go public.
- The dashboard shows each clip's YouTube link or queue state.

**One-time setup per device (about 5 minutes):**
1. In [Google Cloud Console](https://console.cloud.google.com/), create a project (any name, for example "clipper").
2. Go to **APIs & Services → Library**, find **YouTube Data API v3**, and click **Enable**.
3. Go to **APIs & Services → OAuth consent screen** (now called **Google Auth Platform**) and choose **External**.
   - **Branding:** app name, your support email, the homepage `https://github.com/aritam11/clipping` and the privacy policy `https://github.com/aritam11/clipping/blob/main/PRIVACY.md`. If it asks for **Authorized domains**, add `github.com`.
   - **Audience → Publish app → Confirm**, so the publishing status says **In production**. In Testing mode Google expires the sign-in every 7 days. You'll see an "unverified app" warning when you sign in (**Advanced → Go to … (unsafe)**); that's expected for your own app and needs no verification.
4. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**, choose **Desktop app**, and click **Download JSON**. Save the file as `credentials/client_secret.json` in this folder. `credentials/` is git-ignored, so it's never uploaded to GitHub.
5. Run `uv run clip youtube auth`. Your browser opens; sign in with the Google account that owns the channel (pick the brand channel if you have several) and allow access. Check it worked with `uv run clip youtube status`.

Other commands: `uv run clip youtube upload output/<folder> [--dry-run]` uploads clips you already have (`--dry-run` shows the title, description and tags without uploading). `uv run clip youtube retry` sends queued and failed uploads.

Reposting other people's streams can get Content ID claims or copyright strikes. Clip streamers who allow it, or join their clipping programs.

### Dashboard

```bash
uv run clip dashboard --open     # http://127.0.0.1:8765, local only
```

A live web page showing everything at once: the background jobs, each live session's pipeline (recorder → transcriber → chat → hype detector → Claude → renderer, with what each just did), a 15-minute chart of the loudness and chat-rate spikes against the threshold, with clips and Claude passes on lanes underneath, plus the transcript as it comes in, an activity feed, every VOD/watch job with progress, and a gallery to play clips and copy their captions. It reads the status files jobs write to `work/status/`, so it can be opened or closed at any time without affecting the jobs.

### Every stream, automatically

```bash
uv run clip autolive install "https://www.youtube.com/@IShowSpeed/live" -- --max-per-hour 6 --keep-minutes 14
uv run clip autolive status
uv run clip autolive remove "https://www.youtube.com/@IShowSpeed/live"
```

This installs a macOS background job (LaunchAgent) that runs `clip live --wait` for the channel. It starts at login, waits for the stream, clips it, and goes back to waiting when the stream ends. The log is in `work/autolive-*.log`. The Mac has to be awake and online to catch a stream starting; while waiting it doesn't keep the Mac awake.

Outputs land in `output/<video-id>/`:
- `clip_01.mp4` … (the best clip is `clip_01`)
- `clip_01.json`: start/end, title, hook, hashtags, score, transcript
- `posts.md`: title + hashtags for each clip, ready to paste

## Who picks the clips

`--llm auto` (default) uses the first available of:
1. **Anthropic API** (`claude-opus-5-5`) when `ANTHROPIC_API_KEY` is set.
2. **The `claude` CLI**, which uses your Claude Code login and counts against your plan's usage limits. No API key needed.
3. **Heuristic**: loudness peaks + reaction words ("no way", "let's go", …) + speech density. Free, offline, less smart.

If Claude fails, the run falls back to the heuristic rather than stopping.

**Using another model (GPT, Gemini, a local LLM, …):** [`skills/clip-picker/SKILL.md`](skills/clip-picker/SKILL.md) is a detailed, step-by-step skill for picking clips and writing titles, hooks and hashtags. It's written so that less capable models produce Claude-quality picks: a scoring rubric, hard timestamp rules, title and hook formulas, a worked example and a self-check, and its output matches clipper's JSON exactly.
- Drop the folder into your agent's skills directory, or paste it in as the system prompt.
- With clipper's own backends, pass it with `--picker-prompt skills/clip-picker/SKILL.md`.

**Cost** is per hour of footage, not per clip: Claude reads the transcript once and picks every clip in one go. Measured on an hour of non-stop IShowSpeed talking: 13.8k input + 0.8k output tokens, about $0.11 at API prices ($0.02 per clip at 5 clips). Every run prints its usage and saves it in `index.json`.

## Useful flags

| Flag | What it does |
|---|---|
| `--clips N` | clips per video (default: 1 per 10 min, max 12) |
| `--min-len / --max-len` | clip length bounds in seconds (default 15–60) |
| `--topic "GTA VI roleplay"` | niche hint that helps Claude judge what's good |
| `--layout auto\|stack\|face\|zoom\|blur\|crop` | `auto`: facecam on top if a stream facecam is found; a crop that follows faces (re-framed at every cut) if people are on screen; otherwise `--fallback-layout` (zoom: centre square, fills the middle) |
| `--facecam x,y,w,h` | manual facecam box as 0–1 fractions, e.g. `0,0.7,0.25,0.3` for bottom-left |
| `--whisper-model small` | faster, less accurate transcription |
| `--no-hook` | skip the hook text banner |
| `--keep-work` | keep downloaded media (otherwise deleted after rendering to save disk) |

## Automating it

`clip watch` only processes each video once (state in `work/state.json`; failures retry up to 3 times) and takes a lock, so overlapping scheduled runs are safe. It skips streams that are still live until the VOD exists; use `clip live` to clip during the stream. For a schedule, either leave `--interval 30` running in a terminal, or add a cron/launchd job that runs `uv run clip watch sources.txt` from this folder.

## Measured on this Mac (M4)

- Transcription: about 24x real time on the Apple GPU (1 hour of stream took 2.5 min). Expect about 10–15 min for a 4–6 hour VOD.
- One hour of a 24-hour YouTube VOD, start to finish: 9.5 min (audio download 2.3 min, transcription 2.5 min, 5 clips about 50 s each, mostly downloading the 1080p sections).
- Captions stay within 0.03 s of the spoken words.

## Notes

- Clip content you have the rights to, or join a streamer's clipping program. Reposted footage gets copyright strikes.
- Face detection uses OpenCV's YuNet model (`models/`). If a facecam isn't found automatically, pass `--facecam`.
