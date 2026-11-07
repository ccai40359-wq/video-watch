#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""video-watch 引擎层: wav → 文本, 硬件自动选档

用法:
  python transcribe.py <audio.wav> [--lang auto|zh|en|...] [--out-dir DIR]
                       [--model large-v3|large-v3-turbo|small] [--engine auto]

选档决策表(SKILL.md 同步):
  N卡≥8GB → faster-whisper large-v3 fp16   | N卡4-8GB → turbo fp16
  N卡<4GB/驱动异常 → CPU int8 small        | macOS arm64 → whisper.cpp Metal
  Win/Linux 无N卡 → whisper.cpp CPU 预编译 | 引擎失败 → 逐级回退, 不硬崩

已固化并防倒退的三个坑:
  1. KMP_DUPLICATE_LIB_OK (Anaconda OpenMP 冲突)   — import 前设置
  2. huggingface.co 不可达 → 自动降级 hf-mirror.com — 加载失败重试一次
  3. Windows nvidia pip 包 DLL 需注入 PATH 并预加载  — import ctranslate2 前
"""
import argparse
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
import tarfile
from pathlib import Path

# 坑1: 必须在任何重型 import 之前
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

CACHE = Path(os.environ.get("VIDEO_WATCH_CACHE", Path.home() / ".cache" / "video-watch"))
MODEL_DIR = CACHE / "models"
ENGINE_DIR = CACHE / "engines"
MEDIA_TMP = CACHE / "media"

WHISPERCPP_BUILD = "b5454"  # 最近一个带预编译资产的构建号
WHISPERCPP_ASSET = {
    ("windows", "x64"): "whisper-bin-x64.zip",
    ("linux", "x64"): "whisper-bin-ubuntu-x64.tar.gz",
    ("linux", "arm64"): "whisper-bin-ubuntu-arm64.tar.gz",
}
GGUF_NAMES = {"large-v3": "ggml-large-v3.bin", "large-v3-turbo": "ggml-large-v3-turbo.bin",
              "small": "ggml-small.bin"}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def sanitize_stem(name: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|]', "_", name).replace("..", "_")
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:60] or "audio"


def assert_public_host(hostname: str):
    """SSRF 防护: 解析 DNS 并拒绝私网/环回/保留地址(防 rebinding)"""
    try:
        infos = socket.getaddrinfo(hostname, 443)
    except OSError:
        return
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            log(f"[错误] 域名解析到内网/保留地址, 已拒绝: {hostname} -> {ip}")
            sys.exit(3)


def join_plain(parts):
    """中文语种直接拼接; 非中文(分词语种)补空格防粘词"""
    if parts and not any("\u4e00" <= ch <= "\u9fff" for ch in (parts[0] or "")[:20]):
        return " ".join(parts)
    return "".join(parts)


# ---------- 下载(白名单) ----------

def safe_download(url: str, dest: Path, desc: str):
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in {
        "huggingface.co", "hf-mirror.com", "github.com",
        "objects.githubusercontent.com", "release-assets.githubusercontent.com"}:
        log(f"[错误] 非白名单下载地址被拒绝: {p.hostname}")
        sys.exit(3)
    assert_public_host(p.hostname)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"[下载] {desc} -> {dest.name}")
    req = urllib.request.Request(url, headers={"User-Agent": "video-watch/0.1"})

    def report(blocks, bs, total):
        if total > 0 and blocks % 400 == 0:
            log(f"\r[下载] {desc}: {blocks * bs // 1048576}/{total // 1048576} MB", flush=True)

    try:
        urllib.request.urlretrieve(url, tmp, reporthook=report)
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        log(f"[错误] 下载失败: {e}")
        raise
    tmp.rename(dest)


# ---------- 硬件探测 ----------

def nvidia_vram_mb():
    """返回首卡显存 MB; 无卡/命令异常返回 0"""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return 0
        line = r.stdout.strip().splitlines()[0].strip()
        m = re.match(r"\d+", line)
        return int(m.group()) if m else 0
    except Exception:
        return 0


def mac_mem_gb():
    try:
        return os.sysconf("HW_MEMSIZE") // (1024 ** 3)
    except Exception:
        return 8


def decide(os_name, machine, vram):
    """选档决策表 — 每个格子都有确定落点(不允许未定义路径)"""
    if vram >= 8000:                                   # N卡(不限OS, Linux+N卡同样吃满)
        return ("faster-whisper", "cuda", "large-v3")
    if 4000 <= vram < 8000:
        return ("faster-whisper", "cuda", "large-v3-turbo")
    if vram > 0:
        return ("faster-whisper", "cpu", "small")      # N卡<4GB: GPU不值得用
    if os_name == "darwin" and machine == "arm64":
        model = "large-v3" if mac_mem_gb() >= 16 else "large-v3-turbo"
        return ("whisper.cpp", "metal", model)
    if os_name in ("windows", "linux"):
        return ("whisper.cpp", "cpu", "small")
    return ("faster-whisper", "cpu", "small")          # macOS Intel 等兜底格


# ---------- faster-whisper 引擎 ----------

def preload_cuda_dlls():
    """坑3: Windows 下把 nvidia pip 包的 bin 注入 DLL 搜索路径"""
    if platform.system().lower() != "windows":
        return
    import ctypes
    import importlib
    bins = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            mod = importlib.import_module(pkg)
            bindir = Path(list(mod.__path__)[0]) / "bin"
            if bindir.exists():
                bins.append(bindir)
                os.add_dll_directory(str(bindir))
        except Exception:
            pass
    if bins:
        os.environ["PATH"] = os.pathsep.join(str(b) for b in bins) + os.pathsep + os.environ.get("PATH", "")
        for b in bins:
            for name in ("cudnn64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll",
                         "cudnn_adv64_9.dll", "cublas64_12.dll", "cublasLt64_12.dll"):
                f = b / name
                if f.exists():
                    try:
                        ctypes.WinDLL(str(f))
                    except OSError:
                        pass


def run_faster_whisper(wav: Path, model_name: str, device: str, lang: str, out_dir: Path, stem: str):
    try:
        from faster_whisper import WhisperModel, BatchedInferencePipeline
    except ImportError:
        log("[错误] 未安装 faster-whisper: pip install faster-whisper")
        raise SystemExit(3)

    def load(hf_endpoint: str):
        os.environ["HF_ENDPOINT"] = hf_endpoint
        compute = "float16" if device == "cuda" else "int8"
        return WhisperModel(model_name, device=device, compute_type=compute)

    preload_cuda_dlls()
    t0 = time.time()
    try:
        model = load(os.environ.get("HF_ENDPOINT", "https://huggingface.co"))
    except Exception as e:  # 坑2: 直连失败自动降级镜像
        log(f"[warn] huggingface.co 加载失败({type(e).__name__}), 降级 hf-mirror.com 重试")
        model = load("https://hf-mirror.com")
    log(f"[info] 模型 {model_name} 加载完成 ({time.time() - t0:.0f}s, device={device})")

    t1 = time.time()
    try:
        pipe = BatchedInferencePipeline(model=model)
        gen, info = pipe.transcribe(str(wav), language=None if lang == "auto" else lang,
                                    batch_size=8, vad_filter=True)
    except Exception as e:
        log(f"[warn] 批量推理失败({e}), 降级为逐段推理")
        gen, info = model.transcribe(str(wav), language=None if lang == "auto" else lang, vad_filter=True)

    lines, plain, last = [], [], 0.0
    def fmt(ts):
        if ts >= 3600:
            return f"{int(ts) // 3600}:{int(ts) % 3600 // 60:02d}:{int(ts) % 60:02d}"
        return f"{int(ts) // 60:02d}:{int(ts) % 60:02d}"
    for seg in gen:
        text = seg.text.strip()
        if not text:
            continue
        lines.append(f"[{fmt(seg.start)}-{fmt(seg.end)}] {text}")
        plain.append(text)
        if seg.end - last >= 120:
            last = seg.end
            log(f"[进度] {fmt(seg.end)}/{fmt(info.duration)} ({seg.end / info.duration * 100:.0f}%)")
    if not lines:
        log("[结果] 未检测到语音(纯音乐/静音?)")
        return 0
    (out_dir / f"{stem}.timestamped.txt").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / f"{stem}.txt").write_text(join_plain(plain), encoding="utf-8")
    log(f"[完成] {len(lines)} 段, 耗时 {time.time() - t1:.0f}s")
    return len(lines)


# ---------- whisper.cpp 引擎 ----------

def ensure_whispercpp_binary(os_name, machine) -> Path:
    for exe in ("whisper-cli", "whisper-cpp", "main"):
        found = shutil.which(exe)
        if found:
            return Path(found)
    if os_name == "darwin":
        log("[错误] macOS 未找到 whisper-cli", "请运行: brew install whisper-cpp 后重试")
        raise SystemExit(3)
    key = ("windows" if os_name == "windows" else "linux", "x64" if machine == "x64" else "arm64")
    asset = WHISPERCPP_ASSET.get(key)
    if not asset:
        log(f"[错误] 无预编译二进制: {key}", "请参考 README 从源码构建 whisper.cpp")
        raise SystemExit(3)
    base = ENGINE_DIR / f"whispercpp-{WHISPERCPP_BUILD}"
    if base.exists() and any(base.rglob("whisper-cli*")):
        return next(p for p in base.rglob("whisper-cli*") if p.suffix in ("", ".exe"))
    url = f"https://github.com/ggml-org/whisper.cpp/releases/download/{WHISPERCPP_BUILD}/{asset}"
    archive = MEDIA_TMP / asset
    MEDIA_TMP.mkdir(parents=True, exist_ok=True)
    safe_download(url, archive, "whisper.cpp 引擎")
    log("[info] 解压引擎...")
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(base)
    else:
        with tarfile.open(archive) as t:
            t.extractall(base)
    exe = next((p for p in base.rglob("whisper-cli*") if p.suffix in ("", ".exe")), None)
    if not exe:
        log("[错误] 解压后未找到 whisper-cli")
        raise SystemExit(3)
    return exe


def run_whispercpp(wav: Path, model_name: str, lang: str, out_dir: Path, stem: str, device_note: str):
    os_name, machine = platform.system().lower(), platform.machine().lower()
    exe = ensure_whispercpp_binary(os_name, machine)
    gguf = MODEL_DIR / GGUF_NAMES[model_name]
    if not gguf.exists() or gguf.stat().st_size < 1_000_000:  # P1-5: 大小下限防截断
        url = f"https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/{GGUF_NAMES[model_name]}"
        try:
            safe_download(url, gguf, f"GGUF {model_name}")
        except Exception:
            url = url.replace("hf-mirror.com", "huggingface.co")
            safe_download(url, gguf, f"GGUF {model_name}")
    outbase = out_dir / stem
    cmd = [str(exe), "-m", str(gguf), "-f", str(wav), "-oj", "-of", str(outbase)]
    if lang != "auto":
        cmd += ["-l", lang]
    log(f"[info] whisper.cpp ({device_note}) 推理中...")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=7200)
    if r.returncode != 0:
        log(f"[错误] whisper-cli 失败: {(r.stderr or '')[-300:]}")
        raise SystemExit(3)
    jpath = out_dir / f"{stem}.json"
    lines, plain = [], []
    if jpath.exists():
        data = json.loads(jpath.read_text(encoding="utf-8"))
        def fmt(v):
            sec = int(v) // 1000
            if sec >= 3600:
                return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
            return f"{sec // 60:02d}:{sec % 60:02d}"
        for item in data.get("transcription", []):
            text = (item.get("text") or "").strip()
            if not text:
                continue
            ms = (item.get("offsets") or {})
            lines.append(f"[{fmt(ms.get('from', 0))}-{fmt(ms.get('to', 0))}] {text}")
            plain.append(text)
        jpath.unlink()
    if not lines:
        log("[结果] 未检测到语音(纯音乐/静音?)")
        return 0
    (out_dir / f"{stem}.timestamped.txt").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / f"{stem}.txt").write_text(join_plain(plain), encoding="utf-8")
    log(f"[完成] {len(lines)} 段, 耗时 {time.time() - t0:.0f}s")
    return len(lines)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="video-watch transcribe")
    ap.add_argument("wav", type=Path)
    ap.add_argument("--lang", default="auto")
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--model", default=None, help="覆盖自动选档")
    ap.add_argument("--engine", default="auto", choices=["auto", "faster-whisper", "whisper.cpp"])
    args = ap.parse_args()

    if not args.wav.exists():
        log(f"[错误] 文件不存在: {args.wav}")
        sys.exit(2)
    out_dir = (args.out_dir or args.wav.parent).resolve()  # resolve 已归一化穿越分量
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = sanitize_stem(args.wav.stem)

    os_name, machine = platform.system().lower(), platform.machine().lower()
    vram = nvidia_vram_mb()
    engine, device, model = decide(os_name, machine, vram)
    log(f"[选档] os={os_name}/{machine} vram={vram}MB -> engine={engine} model={model}")
    preload_cuda_dlls()  # 必须先于 faster_whisper/ctranslate2 导入(坑3验证过的顺序)
    if args.model:
        if args.model not in {"large-v3", "large-v3-turbo", "small", "medium"}:
            log(f"[错误] 未知模型: {args.model}", "可选: large-v3 / large-v3-turbo / small / medium")
            sys.exit(2)
        model = args.model
    if engine == "whisper.cpp" and model not in GGUF_NAMES:
        log(f"[错误] whisper.cpp 引擎不支持模型 {model}", "可选: " + ", ".join(GGUF_NAMES))
        sys.exit(2)
    if args.engine != "auto":
        engine = args.engine
        if engine == "faster-whisper":
            device = "cuda" if vram > 0 else "cpu"  # metal 只属于 whisper.cpp

    if engine == "faster-whisper":
        code = run_faster_whisper(args.wav.resolve(), model, device, args.lang, out_dir, stem)
    else:
        note = "Metal" if device == "metal" else "CPU"
        try:
            code = run_whispercpp(args.wav.resolve(), model, args.lang, out_dir, stem, note)
        except (SystemExit, Exception) as e:  # 下载/解压/子进程异常统一接住进兜底
            log(f"[warn] whisper.cpp 路径失败({type(e).__name__}), 尝试 faster-whisper CPU 兜底")
            code = run_faster_whisper(args.wav.resolve(), "small", "cpu", args.lang, out_dir, stem)
    print(json.dumps({"ok": code > 0, "segments": code, "engine": engine,
                      "model": model, "device": str(device)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
