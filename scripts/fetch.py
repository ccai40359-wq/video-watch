#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""video-watch 通道层: URL → 元数据 / 字幕 / 音频 (Bilibili + YouTube)

用法:
  python fetch.py meta "<URL>"
  python fetch.py subtitle "<URL>" [--out sub.json]
  python fetch.py audio "<URL>" [--out audio.wav]

设计要点:
  - 所有自建 HTTP 请求: scheme 必须 https 且 host 在白名单内
  - B站字幕三级降级: x/player/wbi/v2 → x/player/v2 → x/v2/subtitle/web/view
  - YT 字幕: 人工轨 > 自动轨原声(-orig), 语言 pin 死不取翻译轨; vtt 去重
  - BILI_SESSDATA 只从环境变量读, 不落盘/不进日志/不进 argv
  - 子进程全部 list 参数 shell=False; 传给 yt-dlp 的 URL 先过白名单
"""
import argparse
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_DIR = Path(os.environ.get("VIDEO_WATCH_CACHE", Path.home() / ".cache" / "video-watch"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ---- 白名单 (P0-10): host 精确匹配, 不允许子串判断 ----
BILI_HOSTS = {"api.bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv",
              "aisubtitle.hdslb.com"}
YT_HOSTS = {"www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com",
            "music.youtube.com"}
DOWNLOAD_HOSTS = {"huggingface.co", "hf-mirror.com", "github.com",
                  "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
YT_SUB_LANGS = ["zh-Hans", "zh", "zh-CN", "en"]

RISK_CODES = {-352: "触发B站风控(-352)", -412: "触发B站风控(-412)", -403: "拒绝访问(-403)"}


def fail(msg: str, hint: str = "") -> None:
    print(f"[错误] {msg}", file=sys.stderr)
    if hint:
        print(f"[怎么办] {hint}", file=sys.stderr)
    sys.exit(3)


def check_url(url: str, hosts: set) -> str:
    """scheme 必须 https, host 精确匹配白名单; 返回规范化后的 https URL"""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        fail(f"URL 协议不支持: {p.scheme}", "请提供 http/https 链接")
    host = (p.hostname or "").lower()
    if host not in hosts:
        fail(f"URL 域名不在支持范围: {host}", "v0 仅支持 Bilibili 与 YouTube 链接")
    return urllib.parse.urlunparse(p._replace(scheme="https"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """带 Cookie 的请求禁用重定向, 防凭据随 302 外泄; b23.tv 短链也走它手动校验落点"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def assert_public_host(hostname: str):
    """SSRF 防护: 域名解析结果必须全部是公网地址(防 DNS rebinding 指向内网/环回)"""
    try:
        infos = socket.getaddrinfo(hostname, 443)
    except OSError as e:
        fail(f"域名解析失败: {hostname}", str(e))
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            fail(f"域名解析到内网/保留地址, 已拒绝: {hostname} -> {ip}")


def http_get_json(url: str, headers: dict = None, timeout: int = 30):
    """白名单 + 公网IP校验后 GET, 返回解析后的 JSON"""
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in (BILI_HOSTS | DOWNLOAD_HOSTS):
        raise ValueError(f"非白名单地址被拒绝: {p.hostname}")
    assert_public_host(p.hostname)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    if "Cookie" in (headers or {}):
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def sanitize_title(title: str) -> str:
    """标题 → 安全文件名 (P0-14): 非法字符/穿越/超长"""
    stem = re.sub(r'[\\/:*?"<>|]', "_", title)
    stem = stem.replace("..", "_").replace("/", "_").replace("\\", "_")
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:80] or "video"


def detect_platform(url: str):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in BILI_HOSTS:  # b23.tv 等B站域优先判定(含子串歧义防护)
        return "bilibili"
    return "youtube"


# ---------------- Bilibili ----------------

def bili_headers(extra: dict = None) -> dict:
    h = {"User-Agent": UA, "Referer": "https://www.bilibili.com/",
         "Origin": "https://www.bilibili.com"}
    sess = os.environ.get("BILI_SESSDATA", "").strip()
    if sess:
        h["Cookie"] = f"SESSDATA={sess}"  # 只进请求头, 不落日志
    if extra:
        h.update(extra)
    return h


def bili_api(url: str, retries: int = 1):
    """GET B站 API, 风控码退避重试一次"""
    for attempt in range(retries + 1):
        try:
            j = http_get_json(url, headers=bili_headers())
        except Exception as e:
            if attempt < retries:
                time.sleep(30)
                continue
            fail(f"B站请求失败: {url.split('?')[0]}", f"网络异常: {e}")
        code = j.get("code", -1)
        if code == 0:
            return j["data"]
        if code in RISK_CODES and attempt < retries:
            print(f"[warn] {RISK_CODES[code]}, 30s 后重试一次", file=sys.stderr)
            time.sleep(30)
            continue
        if code == -400:
            fail("B站返回 -400(参数错误)", "视频可能已删除/私有, 或链接不完整")
        if code == -404:
            fail("B站返回 -404", "视频不存在或已删除")
        fail(f"B站 API 返回码 {code}: {j.get('message', '')}",
             "若为风控(-352/-412), 稍后重试; AI 字幕需设置 BILI_SESSDATA(见 README)")
    return None


def extract_bvid(url: str) -> str:
    p = urllib.parse.urlparse(url)
    m = re.search(r"(BV[0-9A-Za-z]{10})", p.path + "?" + p.query)
    if m:
        return m.group(1)
    m = re.search(r"av(\d+)", p.path + "?" + p.query)
    if m:
        return "av" + m.group(1)
    if p.hostname == "b23.tv" or "/s/" in p.path:  # 短链: 手动跟随重定向(禁自动302, 防SSRF)
        assert_public_host(p.hostname)
        final_url = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as resp:
                final_url = resp.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                final_url = e.headers.get("Location", "")
            else:
                fail(f"b23.tv 短链请求失败: HTTP {e.code}", "短链可能已失效, 请直接粘贴 BV 链接")
        except Exception as e:
            fail("b23.tv 短链解析失败", f"网络异常: {e}")
        fq = urllib.parse.urlparse(final_url)
        if fq.scheme != "https" or (fq.hostname or "") not in BILI_HOSTS:
            fail("b23.tv 短链重定向到了非 B站域名, 已拒绝",
                 f"落点: {fq.hostname}")
        m = re.search(r"(BV[0-9A-Za-z]{10})", fq.path)
        if m:
            return m.group(1)
        fail("短链未能解析出 BV 号", "请直接粘贴含 BV 号的链接")
    fail("未能从 URL 中解析出 BV 号", "请粘贴形如 bilibili.com/video/BVxxxx 的链接")


def bili_meta(url: str) -> dict:
    vid = extract_bvid(url)
    key = "bvid" if vid.startswith("BV") else "aid"
    data = bili_api(f"https://api.bilibili.com/x/web-interface/view?{key}={vid[2:] if key=='aid' else vid}")
    return {
        "platform": "bilibili", "video_id": data.get("bvid"),
        "title": data.get("title"), "uploader": (data.get("owner") or {}).get("name"),
        "duration": data.get("duration"), "desc": (data.get("desc") or "")[:500],
        "pages": data.get("videos", 1),
        "subtitle_tracks": len(((data.get("subtitle") or {}).get("list")) or []),
        "url": f"https://www.bilibili.com/video/{data.get('bvid')}/",
        "aid": data.get("aid"), "cid": data.get("cid"),
    }


def bili_fetch_subtitle_list(aid: str, cid: str) -> tuple:
    """三级降级拿字幕轨列表, 返回 (subtitles_list, used_endpoint, need_login)"""
    endpoints = [
        (f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}", "wbi/v2"),
        (f"https://api.bilibili.com/x/player/v2?aid={aid}&cid={cid}", "player/v2"),
        (f"https://api.bilibili.com/x/v2/subtitle/web/view?aid={aid}&cid={cid}", "web/view"),
    ]
    need_login = False
    for url, name in endpoints:
        try:
            data = http_get_json(url, headers=bili_headers())
        except SystemExit:
            raise
        except Exception as e:
            print(f"[warn] {name} 请求失败: {e}", file=sys.stderr)
            continue
        if data.get("code") in RISK_CODES:
            print(f"[warn] {name} {RISK_CODES[data['code']]}, 试下一级", file=sys.stderr)
            continue
        if data.get("code") != 0:
            print(f"[warn] {name} 返回 {data.get('code')}, 试下一级", file=sys.stderr)
            continue
        d = data.get("data") or {}
        sub = d.get("subtitle") or {}
        need_login = bool(sub.get("need_login_subtitle"))
        subs = sub.get("subtitles") or d.get("subtitles") or []
        if subs:
            return subs, name, need_login
    return [], "none", need_login


LANG_PRIORITY = ["zh-CN", "ai-zh", "zh-Hans", "zh", "zh-TW", "en"]


def pick_bili_track(subs: list) -> dict:
    def rank(s):
        lan = (s.get("lan") or s.get("lan_doc") or "").lower()
        for i, want in enumerate(LANG_PRIORITY):
            if want in lan:
                return (0, i, 0 if "ai" not in lan else 1)
        return (1, 9, 1)
    return sorted(subs, key=rank)[0]


def bili_subtitle(url: str) -> dict:
    meta = bili_meta(url)
    aid, cid = str(meta["aid"]), str(meta["cid"])
    if not cid:
        fail("未拿到 cid", "视频可能尚未转码完成或已失效")
    subs, endpoint, need_login = bili_fetch_subtitle_list(aid, cid)
    if need_login and not os.environ.get("BILI_SESSDATA"):
        print("[hint] 该视频 AI 字幕需要登录态: 设置 BILI_SESSDATA 后可获取(见 README)", file=sys.stderr)
    if not subs:
        if not os.environ.get("BILI_SESSDATA"):
            print("[hint] 未检测到 BILI_SESSDATA: 设置后可解锁 B站 AI 字幕(见 README), 本次将走音频线", file=sys.stderr)
        return {"platform": "bilibili", "video_id": meta["video_id"], "source": "none",
                "segments": [], "note": "无可取字幕(常见原因: AI字幕需SESSDATA登录态)"}
    track = pick_bili_track(subs)
    surl = track.get("subtitle_url") or ""
    if surl.startswith("//"):
        surl = "https:" + surl
    p = urllib.parse.urlparse(surl)
    if p.scheme != "https" or (p.hostname or "") not in BILI_HOSTS:
        fail(f"字幕文件地址异常: {p.hostname}", "已按白名单拦截")
    body = http_get_json(surl, headers=bili_headers())
    items = body.get("body") if isinstance(body, dict) else body
    segs = [{"start": float(x["from"]), "end": float(x["to"]), "text": (x["content"] or "").strip()}
            for x in items if x.get("content")]
    return {"platform": "bilibili", "video_id": meta["video_id"], "title": meta["title"],
            "source": "ai" if "ai" in (track.get("lan") or "") else "manual",
            "language": track.get("lan"), "endpoint": endpoint, "segments": segs}


# ---------------- VTT 去重 ----------------

def parse_vtt(text: str):
    """解析 vtt → [(start_sec, text), ...]"""
    cues = []
    start = None
    buf = []
    for raw in text.splitlines():
        line = raw.strip()
        if "-->" in line:
            if start is not None and buf:
                cues.append((start, " ".join(buf)))
            start = None
            buf = []
            m = re.match(r"(\d+):(\d+):(\d+)\.(\d+)\s*-->", line)
            if not m:
                m = re.match(r"(\d+):(\d+)\.(\d+)\s*-->", line)
                if m:
                    start = int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 1000
                continue
            start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
        elif line and start is not None and not line.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:")):
            clean = re.sub(r"<[^>]+>", "", line)  # 去 <c> 等标签
            clean = html.unescape(clean).strip()
            if clean and not re.fullmatch(r"[\d:.,]+", clean):
                buf.append(clean)
        elif not line:
            if start is not None and buf:
                cues.append((start, " ".join(buf)))
            start, buf = None, []
    if start is not None and buf:
        cues.append((start, " ".join(buf)))
    return cues


def dedup_rolling(cues):
    """YT 自动字幕滚动去重: 新行 = 旧行尾部 + 新内容, 只保留新内容"""
    prev = ""
    segs = []
    for start, text in cues:
        k = 0
        cap = min(len(prev), len(text), 60)
        for cand in range(cap, 0, -1):
            if prev.endswith(text[:cand]):
                k = cand
                break
        new = text[k:].strip()
        if not new:
            continue
        if segs and new == segs[-1]["text"]:
            segs[-1]["end"] = start
            continue
        segs.append({"start": start, "end": start, "text": new})
        prev = text
    return segs


# ---------------- YouTube ----------------

def run_ytdlp(args: list, timeout: int = 300) -> str:
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        fail("未找到 yt-dlp", "pip install yt-dlp (或 pip install -r requirements.txt)")
    try:
        r = subprocess.run([ytdlp, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        fail("yt-dlp 超时", "网络问题或视频过大; 稍后重试")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-3:]
        fail("yt-dlp 执行失败", " / ".join(tail) or "未知错误")
    return r.stdout


def yt_check(url: str) -> str:
    return check_url(url, YT_HOSTS)


def yt_meta(url: str) -> dict:
    raw = run_ytdlp(["--dump-json", "--no-playlist", "--skip-download", yt_check(url)])
    d = json.loads(raw)
    chapters = [{"start": c.get("start_time"), "title": c.get("title")}
                for c in (d.get("chapters") or [])]
    return {"platform": "youtube", "video_id": d.get("id"), "title": d.get("title"),
            "uploader": d.get("uploader"), "duration": d.get("duration"),
            "desc": (d.get("description") or "")[:500], "chapters": chapters,
            "manual_subs": sorted((d.get("subtitles") or {}).keys()),
            "auto_subs": sorted((d.get("automatic_captions") or {}).keys()),
            "url": f"https://www.youtube.com/watch?v={d.get('id')}"}


def yt_pick_lang(meta: dict):
    """人工 zh > 人工 en > 自动原声 zh(-orig) > 自动原声 en(-orig)。

    automatic_captions 会列出全部"可翻译目标语言"键——那些不是真实存在的轨,
    只有 "-orig" 后缀键才是原声轨; 绝不取翻译轨, 交集为空宁可放弃走音频线。
    """
    manual, auto = meta["manual_subs"], meta["auto_subs"]
    for lang in YT_SUB_LANGS:
        if lang in manual:
            return lang, False
    orig = {a[:-5] for a in auto if a.endswith("-orig")}
    for lang in YT_SUB_LANGS:
        if lang in orig:
            return lang, True
    return None, False


def yt_subtitle(url: str) -> dict:
    meta = yt_meta(url)
    lang, is_auto = yt_pick_lang(meta)
    if not lang:
        return {"platform": "youtube", "video_id": meta["video_id"], "title": meta["title"],
                "source": "none", "segments": [], "note": "该视频无可用字幕"}
    tmp = CACHE_DIR / "media"
    tmp.mkdir(parents=True, exist_ok=True)
    base = str(tmp / meta["video_id"])
    args = ["--skip-download", "--no-playlist", "-o", base,
            "--sub-langs", f"{lang}-orig" if is_auto else lang,
            "--sub-format", "vtt"]
    args += ["--write-auto-sub"] if is_auto else ["--write-sub"]
    run_ytdlp(args, timeout=180)
    vtt = Path(base + f".{lang}-orig.vtt" if is_auto else base + f".{lang}.vtt")
    if not vtt.exists():
        hits = sorted(tmp.glob(f'{meta["video_id"]}*.vtt'))
        if not hits:
            fail("字幕文件未生成", "重试一次; 若仍失败走音频线")
        vtt = hits[0]
    cues = parse_vtt(vtt.read_text(encoding="utf-8", errors="replace"))
    segs = dedup_rolling(cues)
    for i, s in enumerate(segs[:-1]):
        s["end"] = segs[i + 1]["start"]
    if segs:
        segs[-1]["end"] = cues[-1][0] + 3
    return {"platform": "youtube", "video_id": meta["video_id"], "title": meta["title"],
            "source": "auto" if is_auto else "manual", "language": lang, "segments": segs}


# ---------------- 音频线 ----------------

def ensure_ffmpeg():
    if not shutil.which("ffmpeg"):
        fail("未安装 ffmpeg", "Windows: winget install Gyan.FFmpeg | macOS: brew install ffmpeg | Ubuntu: sudo apt install ffmpeg")


def fetch_audio(url: str, out: Path):
    ensure_ffmpeg()
    platform = detect_platform(url)
    if platform == "bilibili":
        meta = bili_meta(url)
        vid, page_url = meta["video_id"], meta["url"]
    else:
        meta = yt_meta(url)
        vid, page_url = meta["video_id"], yt_check(url)
    media = CACHE_DIR / "media"
    media.mkdir(parents=True, exist_ok=True)
    wav = out if out.is_absolute() else Path(out).resolve()
    if wav.exists() and wav.stat().st_size > 10000:
        print(f"[info] 命中缓存: {wav}", file=sys.stderr)
        return
    raw = media / f"{sanitize_title(vid)}_{vid}.m4a"
    run_ytdlp(["-f", "ba/b", "--no-playlist", "-o", str(raw), page_url], timeout=900)
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not wav.exists():
        fail("ffmpeg 转码失败", (r.stderr or "")[-300:])
    print(f"[info] 音频就绪: {wav} ({wav.stat().st_size // 1024} KB)", file=sys.stderr)


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description="video-watch fetch")
    ap.add_argument("action", choices=["meta", "subtitle", "audio"])
    ap.add_argument("url")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.action == "meta":
        url = check_url(args.url, BILI_HOSTS | YT_HOSTS)
        result = bili_meta(url) if detect_platform(url) == "bilibili" else yt_meta(url)
        if result.get("platform") == "bilibili" and result.get("pages", 1) > 1:
            result["note"] = f"多P视频({result['pages']}P), v0 只处理 P1"
    elif args.action == "subtitle":
        url = check_url(args.url, BILI_HOSTS | YT_HOSTS)
        result = bili_subtitle(url) if detect_platform(url) == "bilibili" else yt_subtitle(url)
    else:
        out = Path(args.out) if args.out else Path.cwd() / "audio.wav"
        fetch_audio(check_url(args.url, BILI_HOSTS | YT_HOSTS), out)
        result = {"ok": True, "wav": str(out)}

    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out and args.action in ("meta", "subtitle"):
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text, encoding="utf-8")
        print(f"[info] 已写入 {args.out}", file=sys.stderr)
    print(text)


if __name__ == "__main__":
    main()
