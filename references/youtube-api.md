# YouTube 通道手册

## 字幕轨选择规则（防翻译轨，fetch.py 已实现）

优先级：人工 zh > 人工 en > 自动 zh（原声 `-orig`）> 自动 en（`-orig`）。

- **语言 pin 陷阱**：对自动字幕请求 `zh-Hans` 而视频原声是英文时，YouTube 会给**翻译轨**，整篇语言错乱。对策：自动轨必须请求 `<lang>-orig` 后缀（yt-dlp 语义 = 原声轨）
- 人工轨文件名 `<id>.<lang>.vtt`；自动原声轨 `<id>.<lang>-orig.vtt`；翻译轨与人工轨文件名同形——所以必须靠 meta 的 `subtitles`/`automatic_captions` 键位先判轨再下载，不能事后猜文件名
- 若视频只有小语种字幕无 zh/en：v0 放弃字幕走音频线（宁可 ASR，不用看不懂的翻译轨）

## VTT 滚动去重（fetch.py `dedup_rolling` 已实现）

YT 自动字幕每行 = 上一行尾部 + 新内容。算法：对每个 cue，在字符级找新行前缀与已积累文本后缀的最大重叠（上限 60 字符），只保留新增部分；空增量跳过。人工字幕无重叠，算法天然幂等。`<c>`/`<00:00:00.000>` 等标签在解析时剥除。

## 章节三级降级（agent 在 SKILL.md 第 5 步执行）

1. 官方章节：meta 的 `chapters` 非空 → 直接当目录（"只有官方章节反映作者意图"）
2. 无章节且时长>30min → 5 分钟切片给段落级时间戳，段落名用内容概括，不伪造章节名
3. 短视频 → 单一段落

## 时间戳跳转

- `https://www.youtube.com/watch?v=<ID>&t=<秒>s`
- B站：`https://www.bilibili.com/video/<BV>/?t=<秒>`

## 网络事实（2026-11 调研）

- YouTube 对 timedtext/player 端点的封锁针对**云端机房 IP**；本地住宅 IP 三条路线（yt-dlp / youtube-transcript-api / innertube 直连）都稳。本 skill 本地跑 = 不受影响
- yt-dlp 是客户端对抗更新最勤的路线，社区默认最耐用；版本过旧会失效 → requirements 已设最低版本，报错时先升级 yt-dlp
- YT 需要用户网络可达 youtube.com；不可达时 yt-dlp 报错会如实上抛

## 播放列表/直播（v0 边界）

- `watch?v=..&list=..` → `--no-playlist` 只取单视频
- 纯播放列表页/直播链接 → v0 不支持，如实提示
