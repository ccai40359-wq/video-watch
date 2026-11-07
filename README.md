<div align="center">
  <img src="assets/logo/banner_en.png" alt="video-watch — Make AI truly watch videos" width="800">
</div>

# video-watch

**English** | [中文](README.zh-CN.md)

Let your AI agent *really* get videos: drop a Bilibili/YouTube link, get back a structured digest with clickable timestamps.

**The honest pitch:** most of a video's meaning lives in its audio. So video-watch **listens first** — platform subtitles when they exist (instant, zero compute), local ASR when they don't — and only *looks* at frames when the picture actually carries the meaning (v1.1 roadmap). That's not a shortcut; it's the right order: **ears first, eyes when needed.**

**The repo ships no models** — just code and instructions (a few dozen KB). Engines and models are auto-downloaded and cached on first run.

## Architecture

```
URL → ① Metadata (free) → ② Subtitle line (90% of cases, instant) → ③ Audio line + local ASR (fallback)
     → ④ Scenario detection (agent) → ⑤ Templated output with clickable timestamps
▸ ②③ are the ears — shipped in v0 · the frame line (eyes) lands in v1.1
```

- **Subtitles first**: platform captions → digest in seconds, zero compute
- **Local ASR fallback**: auto hardware tiering, zero API keys end to end
- **Intelligence lives in SKILL.md**: the agent picks a template per scenario (tutorial / news / general)

## Quick start

```bash
pip install -r requirements.txt   # yt-dlp + faster-whisper
# ffmpeg is installed separately (see below); ASR is only needed when no subtitles exist
python scripts/fetch.py meta "https://www.youtube.com/watch?v=xxxx"
python scripts/fetch.py subtitle "URL" --out sub.json
python scripts/fetch.py audio "URL" --out audio.wav     # needs ffmpeg
python scripts/transcribe.py audio.wav --lang auto --out-dir .
```

For agent use: put this repo into your agent's skills directory — the agent reads SKILL.md and runs the whole pipeline autonomously.

## Hardware tiering (automatic)

| Hardware | Engine | Model |
|---|---|---|
| NVIDIA ≥8GB (any OS) | faster-whisper fp16 | large-v3 |
| NVIDIA 4–8GB | faster-whisper fp16 | large-v3-turbo |
| macOS Apple Silicon (RAM ≥16G) | whisper.cpp Metal | large-v3 |
| No NVIDIA GPU (Win/Linux) | whisper.cpp CPU | small |
| Fallback | faster-whisper CPU int8 | small |

First run auto-downloads (~0.5–3 GB, one-time) into `~/.cache/video-watch/` (override with `VIDEO_WATCH_CACHE`).

## BILI_SESSDATA (optional — unlocks Bilibili AI subtitles)

1. Log in to bilibili.com in your browser
2. F12 → Application → Cookies → `https://www.bilibili.com` → copy the `SESSDATA` value
3. Set env var `BILI_SESSDATA=<value>` (only ever sent as a request header; never logged or written to disk)

Without a cookie, videos without subtitles fall through to the audio line — you still get a digest, just slower.

## ffmpeg

- Windows: `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg`
- Ubuntu: `sudo apt install ffmpeg`

## Known limitations (v0, honest list)

- End-to-end verified: Windows + NVIDIA + Bilibili ✓; **the YouTube path requires your network to reach youtube.com** (works with `HTTPS_PROXY`; yt-dlp supports it natively)
- The whisper.cpp path (macOS / AMD) is implemented against official release assets but **not yet verified on real hardware** — issues welcome
- Single video per run; playlists processed one by one; Douyin etc. planned for v2
- Full Bilibili WBI signing not implemented (a 3-level fallback ladder avoids it for now)
- Benchmark (RTX 3080, 18-min audio): large-v3 33s / small 15s; CPU small ≈ 3–5 min

## License

MIT (code); ASR models are faster-whisper / GGUF conversions of OpenAI Whisper (MIT).

<div align="center">
  <img src="assets/logo/warm_light.png" alt="video-watch logo" width="180">
</div>
