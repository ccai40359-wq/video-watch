# video-watch

让 AI 智能体"真的看视频"：丢一个 B站/YouTube 链接，拿回带可点击时间戳的摘要。

**仓库里没有模型**——只有代码与说明书（几十 KB）。首次运行自动下载引擎与模型并缓存。

## 架构

```
URL → ①元数据(零成本) → ②字幕线(90%到此为止, 秒出) → ③音频线+本地ASR(兜底)
     → ④场景识别(agent) → ⑤模板化输出(时间戳可点跳转)
```

- 字幕优先：平台现成字幕 3 秒出稿，不算力
- 本地 ASR 兜底：显卡自动选档，全程零 API key
- 智能在 SKILL.md 决策树：agent 按场景选模板（教学/新闻/通用）

## 快速开始

```bash
pip install -r requirements.txt   # yt-dlp + faster-whisper
# ffmpeg 需自行安装(见下), ASR 仅在无字幕时才需要
python scripts/fetch.py meta "https://www.bilibili.com/video/BVxxxx"
python scripts/fetch.py subtitle "URL" --out sub.json
python scripts/fetch.py audio "URL" --out audio.wav     # 需 ffmpeg
python scripts/transcribe.py audio.wav --lang auto --out-dir .
```

给 agent 用：把本仓库放进 agent 的 skills 目录，agent 读 SKILL.md 后全自动执行。

## BILI_SESSDATA（可选，解锁 B站 AI 字幕）

1. 浏览器登录 bilibili.com
2. F12 → Application → Cookies → `https://www.bilibili.com` → 复制 `SESSDATA` 的值
3. 设置环境变量 `BILI_SESSDATA=<值>`（只进请求头，脚本不落盘不打印）

无 cookie 时视频无 AI 字幕 → 自动走音频线，照样出稿（慢一点）。

## ffmpeg 安装

- Windows: `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg`
- Ubuntu: `sudo apt install ffmpeg`

## 硬件选档（自动）

| 硬件 | 引擎 | 模型 |
|---|---|---|
| N卡≥8GB(不限OS) | faster-whisper fp16 | large-v3 |
| N卡4-8GB | faster-whisper fp16 | large-v3-turbo |
| macOS M系列(内存≥16G) | whisper.cpp Metal | large-v3 |
| 无N卡 Win/Linux | whisper.cpp CPU | small |
| 兜底 | faster-whisper CPU int8 | small |

首次运行自动下载（模型 ~0.5–3GB 一次性），缓存于 `~/.cache/video-watch/`（可用 `VIDEO_WATCH_CACHE` 环境变量迁移）。

## 已知问题 / 未尽事项（v0 如实交底）

- 端到端实测：Windows + N卡 + B站全链路 ✓；**YT 全链路依赖用户网络可达 youtube.com**；whisper.cpp 路径按官方发布资产实现，**未在真机实测**（本机有 N卡，走不到该分支）——macOS/A卡用户首跑遇错请提 issue
- 单视频；播放列表逐条；抖音等平台 v2
- B站 wbi 完整签名未实现（三级降级绕开；若 B站全面强制签名需按 references 指南补）
- 大陆网络访问 YouTube 需用户自备网络条件，skill 不代理
- speed 基准（RTX 3080, 18min 音频）：large-v3 33s / small 15s；CPU small 约 3–5 分钟

## License

MIT（代码）；转录模型为 OpenAI Whisper（MIT）的 faster-whisper/GGUF 转换版。
