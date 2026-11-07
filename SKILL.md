---
name: video-watch
description: >
  MUST USE when the user shares a Bilibili/YouTube video link and wants it
  summarized, transcribed, or its content extracted ("总结这个视频",
  "这个视频讲了什么", "帮我看看这个教程", "转录成文字").
  Covers youtube.com, youtu.be, bilibili.com, b23.tv and local audio/video files.
  Pipeline: metadata → subtitles if available (fast) → local ASR fallback
  (auto hardware tier) → scenario-based output with clickable timestamps.
  NOT for: livestreams, playlist batch download (v0 limit — say so and process
  videos one by one), or posting/comments (write operations).
  HONEST SCOPE: v0 understands videos through AUDIO (platform subtitles +
  local ASR) — it does not see visual frames yet; be upfront about this if
  the user asks whether it truly "watches" the visuals (frame line = v1.1).
---

# video-watch — 让 AI 真的"看"视频

本 skill 的智能在**决策树**，脚本只做机械动作。按顺序执行，每一步都有明确出口。

## 0. 管线总览

```
URL → ①元数据(必经) → ②字幕线(90%到此为止) → ③音频线+本地ASR(兜底)
     → ④场景识别 → ⑤按模板输出（时间戳可点跳转）
```

任何一层失败都降级到下一层，不中断。死端（连音频都拿不到）也有明确输出。

## 1. 环境自检（每会话一次）

```bash
python -c "import faster_whisper" 2>/dev/null && echo ASR_OK || echo "需要: pip install -r <skill>/requirements.txt"
ffmpeg -version >/dev/null 2>&1 && echo FFMPEG_OK || echo "需要安装 ffmpeg（见 README）"
```

- ASR 缺失 → `pip install -r requirements.txt`（yt-dlp / faster-whisper）
- ffmpeg 缺失 → 音频线不可用，但字幕线仍可服务；把 README 里的安装指引转给用户
- 网络注意：若 huggingface.co 不可达（大陆网络），transcribe.py 会自动降级 hf-mirror.com，无需干预

## 2. ①元数据层（必经，零成本）

```bash
python <skill>/scripts/fetch.py meta "<URL>"
```

返回 JSON：平台、标题、UP主/频道、时长、章节（如有）、字幕轨可用性、BV/视频ID。

**决策点**：
- 时长 > 3 小时 → 告知用户将耗时较长，确认后继续
- 无效/私有/已删视频 → 把脚本的错误信息转告用户（含"怎么办"），终止
- 播放列表/直播链接 → 提示"v0 仅支持单视频"，逐条处理用户指定的视频

## 3. ②字幕线（能走就走）

```bash
python <skill>/scripts/fetch.py subtitle "<URL>" --out /tmp/sub.json
```

脚本内部自动降级（B站：wbi/v2 → player/v2 → subtitle/web/view 三级；YT：人工轨 > 自动轨，语言已 pin 死不取翻译轨）。

**判定**：输出 JSON 有 segments 且非空 → 字幕线成功，**跳到第 5 步**。
空/失败（常见：B站 AI 字幕需要登录态）→ 提示一次"设置 BILI_SESSDATA 可用 AI 字幕（见 README）"，然后**继续第 4 步**，不等待用户。

## 4. ③音频线 + 本地 ASR（兜底）

```bash
# 4a. 拉音轨并转 16k 单声道 wav
python <skill>/scripts/fetch.py audio "<URL>" --out /tmp/audio.wav

# 4b. 本地转录（硬件自动选档，见下方决策表）
python <skill>/scripts/transcribe.py /tmp/audio.wav --lang auto --out-dir /tmp/
```

产出两个文件：`*.timestamped.txt`（[mm:ss] 分段）与 `*.txt`（纯文本）。脚本会打印所用引擎/模型档位——**把这个信息带进最终输出**（标注转录来源与"ASR 可能有个别同音字错误"）。

### 硬件选档决策表（脚本自动执行，此处供理解）

| 探测结果 | 引擎 | 模型 | 备注 |
|---|---|---|---|
| N卡 显存≥8GB | faster-whisper CUDA fp16 | large-v3 | batch=8, Linux 同样适用 |
| N卡 显存4–8GB | faster-whisper CUDA fp16 | large-v3-turbo | |
| N卡 显存<4GB | faster-whisper CPU int8 | small | GPU 太小不值得用 |
| 驱动异常/无N卡 (Win/Linux) | whisper.cpp CPU 预编译 | ggml-small | 兜底再回 faster-whisper CPU |
| macOS arm64 | whisper.cpp Metal（brew） | ggml-large-v3（内存≥16G）/ ggml-large-v3-turbo | 统一内存=显存 |
| 其余（Intel Mac 等） | faster-whisper CPU int8 | small | |

首次运行会自动下载模型/二进制到 `~/.cache/video-watch/`（可用 `VIDEO_WATCH_CACHE` 覆盖），下载源仅限白名单（见 manifest）。**重跑不重复下载。**

## 5. ④场景识别 → ⑤输出模板

根据元数据+内容选择模板；拿不准时用"通用"。

**判断信号**：标题/章节含 教程/指南/how-to/安装/配置/从零 → 教学；新闻/报道/事件 → 新闻；其他 → 通用。

### 模板A：教学视频

```
▸ 30秒 TL;DR：这个视频教了什么、值不值得完整看（给结论）
▸ 分步要点 + 可点击时间戳
   - 每步一行：要点 — [mm:ss](链接)
▸ "你只需要看" 段落：核心操作区间 + 常见坑区间（如果识别得到）
▸ 来源标注：字幕来源（人工/自动/ASR）+ 转录模型档位
```

### 模板B：多个新闻视频

```
▸ 每条 3 句话：谁 / 发生了什么 / 为什么相关
▸ 横向对比：同一事件多源口径差异、互相矛盾处
▸ 排序建议：先看哪条、哪条只读摘要即可
```

### 模板C：通用（观点/口播/杂谈）

```
▸ 一句话总结核心论点
▸ 分段要点（跟视频叙事顺序）+ 时间戳
▸ 原文金句 2–3 条（引用 ASR 稿时注明可能有同音字误差）
```

**时间戳链接格式**（必须可点）：
- YouTube: `https://www.youtube.com/watch?v=<ID>&t=<秒>s`
- B站: `https://www.bilibili.com/video/<BV>/?t=<秒>`

时间取自字幕 segment 起始或 ASR 时间戳。YT 无章节时长>30分钟时按 5 分钟切片给"段落级"时间戳，不要伪造精确章节名。

## 6. 失败处理速查

| 症状 | 动作 |
|---|---|
| fetch 报"URL 不受支持" | 检查是否 B站/YT 之外的平台；明确告知 v0 支持范围 |
| B站 -412/-352 | 不要立刻重试；隔 30s 重试一次，再失败→直接走音频线 |
| need_login_subtitle | 提示用户设 BILI_SESSDATA 后可拿 AI 字幕，本次走音频线 |
| ASR 全部失败 | 用元数据（标题+简介+置顶评论）给"极简摘要"，明确标注"未看过正文，仅元数据" |
| 下载卡住/太慢 | Ctrl+C 后重跑安全（临时文件自动清理，断点重下） |

细节与风控码表：[references/bilibili-api.md](references/bilibili-api.md)、[references/youtube-api.md](references/youtube-api.md)

## 7. Manifest（下载源白名单，脚本内置校验）

所有脚本直接发起的下载仅允许以下 https 源，其余一律拒绝：

```
huggingface.co            # faster-whisper 模型
hf-mirror.com             # 上者的镜像降级
github.com                # whisper.cpp 预编译二进制（release 资产）
objects.githubusercontent.com / release-assets.githubusercontent.com  # 允许的 release 跳转域
api.bilibili.com / www.bilibili.com / m.bilibili.com / b23.tv / aisubtitle.hdslb.com  # B站数据与字幕
```

模型/二进制**不进 git 仓库**——仓库只有代码与说明书，首次运行按需下载并缓存。

## 8. v0 边界（如实告知用户）

- **诚实定位：v0 用"耳朵"理解视频**（字幕/ASR），尚不看画面帧——若用户问"你真的看了画面吗"，如实回答"当前通过语音与字幕理解内容，画面线在 v1.1"；对画面承载信息的视频（无声演示/游戏实况）主动说明覆盖有限
- 单视频；播放列表逐条处理
- B站 AI 字幕需 BILI_SESSDATA（README 有获取步骤）；无 cookie 时自动走音频线
- 抖音/小红书等平台不在 v0 范围
- macOS/仅 CPU 机器速度较慢（small 档 15 分钟音频约 3–5 分钟），属预期
