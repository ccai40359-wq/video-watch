# B站通道防御手册

> 来源：2026-11-07 三 lane GitHub 调研（agent-reach 文档、wzn99/bilibili-summary-android 防御实现、lxfater/BilibiliSummary 端点考古）+ 2026-11-07 本机实测。

## 字幕获取三级降级（fetch.py 已实现）

| 级 | 端点 | 签名 | 登录态 |
|---|---|---|---|
| 1 | `GET /x/player/wbi/v2?aid=&cid=` | 官方要求 wbi 签名，但带 Cookie+Referer 实测多数可用 | AI 字幕需 SESSDATA |
| 2 | `GET /x/player/v2?aid=&cid=` | 免签名 | 同上 |
| 3 | `GET /x/v2/subtitle/web/view?aid=&cid=` | 免签名 | 需 Cookie+Referer+Origin |

请求头必须带 `UA + Referer: https://www.bilibili.com/`（+`Origin`），否则 412。

## 风控码表（代码与本文档一一对应，改一处必须改另一处）

| 码 | 含义 | fetch.py 动作 |
|---|---|---|
| `-352` | 风控拦截 | 元数据路径: 30s 退避重试一次；字幕降级路径: 立即试下一级 |
| `-412` | 请求被拒 | 同上 |
| `-403` | 拒绝访问 | 同上 |
| `-400` | 参数错误 | 直接报错：视频可能已删除/私有 |
| `-404` | 不存在 | 直接报错 |
| `need_login_subtitle: true` | AI 字幕需登录 | 提示设 BILI_SESSDATA，走音频线 |

## 关键事实

- **AI 字幕（lan=ai-zh）必须登录态（SESSDATA）**；未登录时字幕列表可能为空或只有 CC 轨
- 优先选轨顺序：`zh-CN` > `ai-zh` > `zh-Hans` > `zh` > `zh-TW` > `en`；同语言人工轨优先于 AI 轨
- 字幕文件地址形如 `//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/...json`，协议头需补 `https:`
- 字幕 JSON 结构：`{"body":[{"from":秒,"to":秒,"content":"..."}]}`
- 短链 `b23.tv` 302 → 必须校验落点 host 仍在 B站白名单内再解析 BV 号
- 多P视频 `view` 接口 `videos>1`，v0 只处理 P1 并如实提示

## 音频线

- yt-dlp 拉音轨 **2026-11-07 实测可用**（format 30280, 64k AAC，免登录）
- agent-reach 文档声称"B站 412 全面拦截 yt-dlp"——与本机实测矛盾；结论：**分端点**。字幕/播放接口风控严（走原生 API），音轨 CDN 可过。若某天音轨也被拦，备选 `agent-reach` 的 `bili audio`（bili-cli）
- 高码率 1080P 视频音轨需要 cookies（`--cookies-from-browser`），音轨线不需要

## WBI 签名（v0 未实现，已留后路）

完整签名 = 从 `nav` 接口 `wbi_img` 取 key → mixinKey 重排 → `w_rid/wts` + `dm_img_*` 参数。v0 的三级降级绕开了它；若某天三级全部要求签名，实现指南见 bilibili-API-collect 仓库。
