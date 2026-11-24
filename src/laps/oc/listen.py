"""Listen — 音声ファイルを聞いて文字にする道具（2026-10-06 オーナー「Blackwall に聴覚を持たせたい」）。

★仕組み: whisper.cpp（MIT・github.com/ggml-org/whisper.cpp）の whisper-cli を**利用者の機械で**動かす。
  推論機（推論機）には何も足さない＝音声を外へ送らない。NVIDIA の GPU がある Windows は CUDA 版、
  それ以外は CPU 版。初回の呼び出しで本体とモデルを取りに行き（大きさと sha256 を照合）、以後は使い回す。
  置き場 ~/.blackwall/listen（LAPS_LISTEN_DIR で替えられる）。
  ★toolenv の置き場（~/.blackwall/tools）には入れない: 本体の zip には main.exe・bench.exe・command.exe
    など一般的な名前の実行ファイルが 30 本あり、PATH の先頭に足すと Bash の命令を横取りしうる。

★実機（利用者の PC RTX 4070・2026-10-06）で決めたこと:
  ・CUDA 11.8 版の zip には cuBLAS の DLL が入っておらず、CUDA の読み込みが**黙って**失敗して CPU で走った。
    12.4 版（674MB）は自己完結 → 12.4 を使う。どちらで走ったかは結果に必ず書く（stderr の load_backend）。
  ・台詞 6 本で 4.6 秒（モデルの読み込み込み）。日本語の台詞はほぼ正確。
  ・VAD（無音の切り捨て）は既定で切る: 小声の台詞が丸ごと消え（「これで…いいんですか…?」→ 空）、
    切り詰めた残りから「ご視聴ありがとうございました」を作った（元は「わかりました。」）。
  ・効果音（心音）は「Thank you.」になる。トークンの確率（平均 0.8）では本物の台詞（0.73〜0.99）と分けられない。
    → よく出る定型句に「疑わしい」の印を付けて返す。★消さない（判断はモデルと利用者に残す）。
  ・日本語のファイル名・全角空白のパスも読めた。念のため Windows では引数を**すべて**引用符で包む
    （list2cmdline は ASCII の空白しか引用しない＝全角空白で切る道具がある・desktop.py の csc の実例）。
"""
from __future__ import annotations

from .. import paths as _lp
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import wave
import zipfile
from collections import Counter
from pathlib import Path

from ..tools import ToolResult

# ── 取りに行く物（版と中身を固定する）────────────────────────────────────────

_REL = "b5130"   # whisper.cpp 1.9.4（2026-09-11）
_GH = f"https://github.com/ggml-org/whisper.cpp/releases/download/{_REL}/"
_HF = "https://huggingface.co/"
# 名前: (URL, sha256, 大きさ)
_ENGINES = {
    "win-cuda": (_GH + "whisper-cublas-12.4.0-bin-x64.zip",
                 "af520ddd034d985b55dfeea3e465ed93653ba2aee1a55e865033edc548c272a7", 674539285),
    "win-cpu": (_GH + "whisper-bin-x64.zip",
                "f9ec6c52a2e949b62ab51fa21d0d497958f9e41c3010c157c4e42932d5316f3c", 8573270),
    "linux-cpu": (_GH + "whisper-bin-ubuntu-x64.tar.gz",
                  "53e7fd8b5764edad916b8848dd0af6abb1ff1d3b86c899e79c78652412536c32", 9793438),
}
_MODEL = ("ggml-large-v3-turbo-q8_0.bin", _HF + "ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin",
          "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1", 874188075)
_VAD = ("ggml-silero-v6.2.0.bin", _HF + "ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin",
        "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987", 885098)

NATIVE_EXT = {".wav", ".mp3", ".flac", ".ogg"}          # whisper-cli がそのまま読める（--help の supported audio formats）
CONVERT_EXT = {".m4a", ".aac", ".opus", ".webm", ".mp4", ".mkv", ".mov", ".avi", ".wma", ".aif", ".aiff",
               ".amr", ".3gp", ".oga", ".caf", ".m4v", ".flv", ".ts"}   # ffmpeg があれば 16kHz の wav にしてから
AUDIO_EXT = NATIVE_EXT | CONVERT_EXT
MAX_FILES = 5000
_INLINE_FILES = 20          # これ以下で文字数も少なければ、結果を全部その場で返す
_INLINE_CHARS = 9000
_PREVIEW_ROWS = 25
_CMDLINE_BUDGET = 24000     # Windows の命令行の上限 32767 に余裕を持たせる
_CHUNK_FILES = 200
_CREATE_NO_WINDOW = 0x08000000

_install_lock = threading.Lock()

# whisper が無音・効果音・音楽から作りやすい定型句（正規化した形で比べる）。★消さずに印を付けるだけ。
_HALLUCINATIONS = {
    "ご視聴ありがとうございました", "ご視聴ありがとうございます", "最後までご視聴ありがとうございました",
    "チャンネル登録お願いします", "チャンネル登録をお願いします", "チャンネル登録よろしくお願いします",
    "おやすみなさい", "ありがとうございました", "字幕", "字幕作成者", "thankyou", "thankyouverymuch",
    "thanksforwatching", "thankyouforwatching", "pleasesubscribe", "bye", "you", "ok", "so", "the", "music",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目", "字幕由amaraorg社区提供", "谢谢观看", "감사합니다", "시청해주셔서감사합니다",
}
_NORM_DROP = re.compile(r"[\s\u3000.,!?。、！？…・「」『』（）()\[\]【】〜~♪\-ー—'\"]+")


class ListenError(Exception):
    pass


# ── 載せるかどうか・置き場 ────────────────────────────────────────────────

def _machine() -> str:
    return (platform.machine() or "").lower()


def available() -> bool:
    """この機械で動かせるときだけ True（必ず失敗する道具は棚に載せない）。"""
    if os.environ.get("LAPS_LISTEN", "1").strip() == "0":
        return False
    if _machine() not in ("amd64", "x86_64"):
        return False
    return sys.platform == "win32" or sys.platform.startswith("linux")


def _root() -> Path:
    d = os.environ.get("LAPS_LISTEN_DIR")
    return Path(d).expanduser() if d else _lp.dot_dir() / "listen"


def _has_nvidia() -> bool:
    if sys.platform != "win32":
        return False
    win = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return (Path(win) / "System32" / "nvcuda.dll").exists()   # NVIDIA のドライバが入れる CUDA の本体


def _variant() -> str:
    if sys.platform == "win32":
        if os.environ.get("LAPS_LISTEN_GPU", "1").strip() != "0" and _has_nvidia():
            return "win-cuda"
        return "win-cpu"
    return "linux-cpu"


# ── 取りに行く・確かめる・展開する ─────────────────────────────────────────

def _cancelled() -> bool:
    try:
        from . import tools as _t
        return bool(_t._cancel_probe())
    except Exception:
        return False


def _download(url: str, dest: Path, sha: str, size: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest.parent).free
    if free < size * 1.3 + 200 * 1024 * 1024:
        raise ListenError(f"not enough disk space in {dest.parent} to download {dest.name} "
                          f"({size / 1e6:.0f} MB needed, {free / 1e6:.0f} MB free)")
    part = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(url, headers={"User-Agent": "blackwall-listen"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            while True:
                if _cancelled():
                    raise ListenError("cancelled while downloading")
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                h.update(buf)
                got += len(buf)
        if got != size or h.hexdigest() != sha:
            raise ListenError(f"downloaded {dest.name} does not match the expected file "
                              f"(size {got} vs {size}, sha256 {h.hexdigest()[:16]}…)")
        os.replace(part, dest)
    except ListenError:
        raise
    except Exception as e:  # noqa: BLE001 — 網・ディスクの失敗はまとめて理由つきで返す
        raise ListenError(f"could not download {url}: {e}") from e
    finally:
        try:
            part.unlink()
        except OSError:
            pass


def _safe_extract(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    base = into.resolve()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                if not (into / m.filename).resolve().is_relative_to(base):
                    raise ListenError(f"unsafe path in {archive.name}: {m.filename}")
            z.extractall(into)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                where = into / m.name
                if not where.resolve().is_relative_to(base):
                    raise ListenError(f"unsafe path in {archive.name}: {m.name}")
                if m.issym() or m.islnk():
                    # 同梱の .so の版の別名（libwhisper.so.1 → libwhisper.so.1.9.4）は中に閉じていれば通す
                    tgt = (where.parent / m.linkname) if m.issym() else (into / m.linkname)
                    if not tgt.resolve().is_relative_to(base):
                        raise ListenError(f"unsafe link in {archive.name}: {m.name} -> {m.linkname}")
            try:
                t.extractall(into, filter="data")
            except TypeError:   # filter の無い古い Python
                t.extractall(into)


def _find_cli(d: Path) -> Path | None:
    name = "whisper-cli.exe" if sys.platform == "win32" else "whisper-cli"
    hits = sorted(d.rglob(name), key=lambda p: len(p.parts))
    return hits[0] if hits else None


def _ensure_engine() -> tuple[Path, bool]:
    """(whisper-cli の場所, いま入れたか)。"""
    var = _variant()
    url, sha, size = _ENGINES[var]
    final = _root() / "engine" / f"{_REL}-{var}"
    cli = _find_cli(final) if final.is_dir() else None
    if cli is not None and (final / ".ok").exists():
        return cli, False
    archive = _root() / "downloads" / url.rsplit("/", 1)[1]
    if not (archive.exists() and archive.stat().st_size == size and _sha256(archive) == sha):
        _download(url, archive, sha, size)
    tmp = final.with_name(f".{final.name}.{os.getpid()}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    _safe_extract(archive, tmp)
    cli = _find_cli(tmp)
    if cli is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ListenError(f"whisper-cli was not found inside {archive.name}")
    if sys.platform != "win32":
        for p in cli.parent.iterdir():
            if p.is_file() and "." not in p.name:
                p.chmod(p.stat().st_mode | 0o111)
    (tmp / ".ok").write_text(sha, encoding="ascii")
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    try:
        archive.unlink()   # 展開したら書庫（最大 674MB）は要らない
    except OSError:
        pass
    return _find_cli(final), True


def prepare() -> str:
    """whisper.cpp の本体と既定のモデルを先にそろえておく（oc/parts.py から・初回の待ちを無くす）。
    そろっていれば何もしない（照合済みの印 .ok を見るだけ）。"""
    if not available():
        return "listen: この機械では使わない"
    cli, e_new = _ensure_engine()
    model, m_new = _ensure_model(_MODEL)
    state = "用意した" if (e_new or m_new) else "そろっている"
    return f"listen: {state}（{cli}・{model.name}）"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


def _ensure_model(spec: tuple[str, str, str, int]) -> tuple[Path, bool]:
    name, url, sha, size = spec
    p = _root() / "models" / name
    ok = p.with_name(name + ".ok")
    if p.exists() and p.stat().st_size == size and ok.exists():
        return p, False
    if not (p.exists() and p.stat().st_size == size and _sha256(p) == sha):
        _download(url, p, sha, size)
    ok.write_text(sha, encoding="ascii")
    return p, True


# ── 入力を集める ───────────────────────────────────────────────────────────

def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _collect(inp: dict, notes: list[str]) -> list[Path]:
    from . import tools as _t
    raw: list[str] = []
    if inp.get("path"):
        raw.append(str(inp["path"]))
    ps = inp.get("paths")
    if isinstance(ps, str):
        ps = [ps]
    if isinstance(ps, list):
        raw += [str(x) for x in ps if str(x).strip()]
    if not raw:
        raise ListenError("give `path` (a file, a folder, or a glob like D:\\voices\\*.wav) or `paths`")
    recursive = bool(inp.get("recursive"))
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        k = os.path.normcase(str(p))
        if k not in seen:
            seen.add(k)
            out.append(p)

    for r in raw:
        a = _t._abs(r)
        if any(c in r for c in "*?["):
            import glob as _glob
            hits = [Path(h) for h in _glob.glob(a, recursive=True)]
            files = [h for h in hits if h.is_file() and h.suffix.lower() in AUDIO_EXT]
            if not files:
                notes.append(f"no audio files matched {r}")
            for h in sorted(files, key=lambda p: _natural(str(p))):
                add(h)
            continue
        a, n = _t._fix_path(a, "existing")
        if n:
            notes.append(n)
        p = Path(a)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            files = [h for h in it if h.is_file() and h.suffix.lower() in AUDIO_EXT]
            if not files:
                kinds = Counter(h.suffix.lower() for h in p.iterdir() if h.is_file())
                kinds_s = ", ".join(f"{k or '(none)'}x{v}" for k, v in kinds.most_common(6))
                notes.append(f"no audio files in {p}"
                             + ("" if recursive else " (use recursive=true to include sub-folders)")
                             + (f"; file types here: {kinds_s}" if kinds else ""))
            for h in sorted(files, key=lambda q: _natural(str(q))):
                add(h)
        elif p.is_file():
            add(p)
        else:
            raise ListenError(f"not found: {a}")
    if len(out) > MAX_FILES:
        raise ListenError(f"{len(out)} files is too many for one call (max {MAX_FILES}); narrow the folder or glob")
    return out


# ── 実行 ─────────────────────────────────────────────────────────────────

def _win_quote(a: str) -> str:
    out, bs = ['"'], 0
    for ch in a:
        if ch == "\\":
            bs += 1
            continue
        if ch == '"':
            out.append("\\" * (bs * 2 + 1) + '"')
        else:
            out.append("\\" * bs + ch)
        bs = 0
    out.append("\\" * (bs * 2) + '"')
    return "".join(out)


def _kill(proc: subprocess.Popen) -> None:
    try:
        from .. import procrun
        procrun._kill_tree(proc)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run(argv: list[str], cwd: Path, timeout: float, env: dict | None) -> tuple[int, str]:
    """(終了コード, stderr)。パイプを使わない（procrun と同じ理由）・窓を出さない・中断に従う。"""
    err_path = cwd / "stderr.txt"
    with open(err_path, "wb") as fe:
        kw: dict = {"cwd": str(cwd), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": fe,
                    "env": env}
        if sys.platform == "win32":
            kw["creationflags"] = _CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            cmd: str | list[str] = " ".join(_win_quote(a) for a in argv)
        else:
            kw["start_new_session"] = True
            cmd = argv
        proc = subprocess.Popen(cmd, **kw)
        t0 = time.monotonic()
        while proc.poll() is None:
            if _cancelled():
                _kill(proc)
                raise ListenError("cancelled")
            if time.monotonic() - t0 > timeout:
                _kill(proc)
                raise ListenError(f"whisper-cli did not finish within {timeout:.0f} s")
            time.sleep(0.25)
    try:
        err = err_path.read_bytes().decode("utf-8", "replace")
    except OSError:
        err = ""
    return proc.returncode, err


def _ffmpeg() -> str | None:
    try:
        from . import toolenv
        return shutil.which("ffmpeg", path=toolenv.child_path())
    except Exception:
        return shutil.which("ffmpeg")


_PIC_W = 1200


def sound_picture(path: Path, ff: str | None = None, timeout: float = 90.0) -> tuple[bytes | None, str]:
    """音を目で見る（2026-10-19 オーナー「耳も強化して」）: 上に波形・下にスペクトログラムを並べた 1 枚の PNG と、
    長さ・音量・無音の区間の文。台詞でない音（音楽・効果音・無音）は whisper では「no speech」になるので、形を見せる。
    ★推論機が画像を読める（KAT に Qwen3.6 の画像の部品・2026-10-19）ので、画像はモデルへ渡る（_pending_images）。"""
    ff = ff or _ffmpeg()
    if not ff:
        return None, "A picture of the sound needs ffmpeg, which was not found on PATH."
    facts = []
    try:
        cp = subprocess.run([ff, "-hide_banner", "-nostats", "-i", str(path), "-af",
                             "volumedetect,silencedetect=noise=-40dB:d=0.3", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        err = cp.stderr or ""
        m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
        dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else None
        sr = re.search(r"(\d+) Hz", err)
        mean = re.search(r"mean_volume: (-?[\d.]+) dB", err)
        peak = re.search(r"max_volume: (-?[\d.]+) dB", err)
        starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
        ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", err)]
        if dur is not None:
            facts.append(f"length {dur:.2f} s")
        if sr:
            facts.append(f"{int(sr.group(1)) // 1000} kHz")
        if mean and peak:
            facts.append(f"mean volume {float(mean.group(1)):.1f} dB, peak {float(peak.group(1)):.1f} dB"
                         + (" (clipping)" if float(peak.group(1)) >= -0.1 else ""))
        if starts:
            spans = []
            for i, a in enumerate(starts[:8]):
                b = ends[i] if i < len(ends) else dur
                spans.append(f"{max(a, 0):.2f}-{b:.2f} s" if b is not None else f"{max(a, 0):.2f} s-end")
            facts.append("silence " + ", ".join(spans) + (" …" if len(starts) > 8 else ""))
        else:
            facts.append("no silence longer than 0.3 s")
    except (OSError, subprocess.TimeoutExpired) as e:
        facts.append(f"could not measure the sound ({type(e).__name__})")
    import tempfile
    fd, out = tempfile.mkstemp(suffix=".png", prefix="bw-sound-")
    os.close(fd)
    try:
        # ★つなぐ前に両方を rgb24 にそろえる（そろえないと白黒の形式に落ちて色が消えた）。
        graph = (f"[0:a]aformat=channel_layouts=mono,showwavespic=s={_PIC_W}x200:colors=0xe11d48,format=rgb24[w];"
                 f"[0:a]aformat=channel_layouts=mono,showspectrumpic=s={_PIC_W}x400:legend=0:scale=log:fscale=log"
                 ":color=magma,format=rgb24[sp];"
                 "[w][sp]vstack=inputs=2,format=rgb24")
        cp = subprocess.run([ff, "-hide_banner", "-nostats", "-y", "-i", str(path), "-filter_complex", graph,
                             "-frames:v", "1", out], capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace",
                            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        png = Path(out).read_bytes() if cp.returncode == 0 and os.path.getsize(out) > 0 else None
        if png is None:
            facts.append("the picture could not be drawn: " + ((cp.stderr or "").strip().splitlines() or ["?"])[-1][:160])
    except (OSError, subprocess.TimeoutExpired) as e:
        png = None
        facts.append(f"the picture could not be drawn ({type(e).__name__})")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    head = (f"Sound of {path.name}: " + "; ".join(facts) + ".")
    if png:
        head += ("\nThe picture is attached: top = waveform (loudness over time), bottom = spectrogram (time left to right "
                 "across the whole length; pitch low at the bottom to high at the top, log scale). Speech shows as "
                 "stacked horizontal bands, music as steady lines and beats, noise as an even haze.")
    return png, head


def _wav_seconds(p: Path) -> float | None:
    if p.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return None


def _norm(text: str) -> str:
    return _NORM_DROP.sub("", text).lower()


def _ts(ms: int) -> str:
    s, ms = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return (f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}") + f".{ms // 10:02d}"


def _threads() -> int:
    n = os.cpu_count() or 4
    return max(2, min(8, n // 2))


def _transcribe(files: list[Path], *, language: str, prompt: str, vad: bool,
                notes: list[str]) -> tuple[list[dict], dict]:
    t0 = time.monotonic()
    with _install_lock:
        cli, new_engine = _ensure_engine()
        model, new_model = _ensure_model(_MODEL)
        vad_model = _ensure_model(_VAD)[0] if vad else None
    setup_s = time.monotonic() - t0
    info = {"setup_s": setup_s if (new_engine or new_model) else 0.0, "backend": "unknown"}

    work = _root() / "work" / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    env = None
    if sys.platform != "win32":
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = str(cli.parent) + (os.pathsep + env["LD_LIBRARY_PATH"]
                                                     if env.get("LD_LIBRARY_PATH") else "")
    results: list[dict] = [{"file": str(f), "index": i} for i, f in enumerate(files)]
    try:
        # 読めない形式は ffmpeg で 16kHz・単声の wav にしてから渡す
        src: list[Path | None] = []
        ff = None
        for r, f in zip(results, files):
            if f.suffix.lower() in NATIVE_EXT:
                src.append(f)
                continue
            ff = ff or _ffmpeg()
            if not ff:
                r["error"] = f"{f.suffix} needs ffmpeg to convert first (ffmpeg was not found on PATH)"
                src.append(None)
                continue
            out = work / f"c{r['index']:05d}.wav"
            try:
                rc, err = _run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(f),
                                "-vn", "-ac", "1", "-ar", "16000", out.name], work, 600, None)
            except ListenError as e:
                if "cancelled" in str(e):
                    raise
                rc, err = 1, str(e)
            if rc != 0 or not out.exists():
                r["error"] = "ffmpeg could not convert it: " + (err.strip().splitlines() or ["?"])[-1][:200]
                src.append(None)
            else:
                src.append(out)
        # 長さは渡す wav から測る（変換した物は作業場ごと最後に消すので、ここで。元の m4a・mp4 は wave で読めない）
        for r, s in zip(results, src):
            if s is not None:
                r["seconds"] = _wav_seconds(s)

        gpu = "cuda" in _variant()
        # モデルは作業場（cwd）からの相対で渡す＝利用者のプロファイルの日本語・全角空白を引数に載せない
        base = [str(cli), "-m", os.path.relpath(model, work), "-l", language or "auto", "-oj", "-np",
                "-t", str(_threads())]
        if prompt:
            base += ["--prompt", prompt]
        if vad_model:
            base += ["--vad", "-vm", os.path.relpath(vad_model, work)]
        todo = [(r, s) for r, s in zip(results, src) if s is not None]
        i = 0
        stderr_all = []
        while i < len(todo):
            chunk, length = [], sum(len(a) + 3 for a in base)
            while i < len(todo) and len(chunk) < _CHUNK_FILES:
                r, s = todo[i]
                add = len(str(s)) + 16
                if chunk and length + add > _CMDLINE_BUDGET:
                    break
                chunk.append((r, s))
                length += add
                i += 1
            argv = list(base)
            mb = 0.0
            for r, s in chunk:
                argv += ["-of", f"o{r['index']:05d}", str(s)]
                try:
                    mb += s.stat().st_size / 1e6
                except OSError:
                    pass
            timeout = (90 + 3 * len(chunk) + 6 * mb) if gpu else (180 + 20 * len(chunk) + 90 * mb)
            rc, err = _run(argv, work, timeout, env)
            stderr_all.append(err)
            for r, s in chunk:
                jp = work / f"o{r['index']:05d}.json"
                if not jp.exists():
                    bad = [ln for ln in err.splitlines() if ("error" in ln.lower() or "failed" in ln.lower())
                           and s.name in ln]
                    r["error"] = (bad[-1].strip()[:200] if bad else
                                  f"whisper-cli produced no result (exit {rc})")
                    continue
                try:
                    data = json.loads(jp.read_text(encoding="utf-8", errors="replace"))
                except Exception as e:  # noqa: BLE001
                    r["error"] = f"could not read whisper-cli output: {e}"
                    continue
                r["language"] = ((data.get("result") or {}).get("language") or "")
                segs = []
                for seg in data.get("transcription") or []:
                    off = seg.get("offsets") or {}
                    text = (seg.get("text") or "").strip()
                    if text:
                        segs.append({"from": int(off.get("from") or 0), "to": int(off.get("to") or 0),
                                     "text": text})
                r["segments"] = segs
                r["text"] = " ".join(s["text"] for s in segs).strip()
        joined = "\n".join(stderr_all)
        info["backend"] = ("GPU (CUDA)" if "loaded CUDA backend" in joined
                           else "CPU" if "loaded CPU backend" in joined else "unknown")
        if gpu and info["backend"] == "CPU":
            notes.append("the CUDA build fell back to the CPU (the GPU driver may be too old); results are "
                         "the same, only slower")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    _flag(results)
    info["elapsed_s"] = time.monotonic() - t0
    return results, info


def _flag(results: list[dict]) -> None:
    """whisper が音の無い所から作りがちな文に印を付ける（消さない）。"""
    langs = Counter(r.get("language") for r in results if r.get("text"))
    major = langs.most_common(1)[0][0] if langs else None
    for r in results:
        t = r.get("text") or ""
        if not t:
            continue
        n = _norm(t)
        if n in _HALLUCINATIONS:
            r["flag"] = "suspect: a phrase whisper often invents from silence, music or sound effects"
        elif not n:
            r["flag"] = "suspect: only symbols, no words"
        elif len(results) >= 5 and major and r.get("language") != major and len(n) <= 12:
            r["flag"] = f"suspect: short line detected as '{r.get('language')}' while most files are '{major}'"


# ── 結果の書き出し ─────────────────────────────────────────────────────────

def _save(results: list[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ext = dest.suffix.lower()
    tmp = dest.with_name(dest.name + ".tmp")
    if ext == ".json":
        body = json.dumps([{k: r.get(k) for k in ("file", "language", "seconds", "text", "segments", "flag", "error")}
                           for r in results], ensure_ascii=False, indent=1)
        tmp.write_text(body, encoding="utf-8")
    elif ext == ".txt":
        lines = [f"{r['file']}\t{r.get('text') or ''}" for r in results]
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        sep = "," if ext == ".csv" else "\t"
        import csv
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:   # BOM つき＝Excel で文字化けしない
            w = csv.writer(f, delimiter=sep)
            w.writerow(["file", "language", "seconds", "text", "flag", "error"])
            for r in results:
                w.writerow([r["file"], r.get("language") or "",
                            "" if r.get("seconds") is None else f"{r['seconds']:.2f}",
                            r.get("text") or "", r.get("flag") or "", r.get("error") or ""])
    os.replace(tmp, dest)


def _render(results: list[dict], info: dict, notes: list[str], saved: Path | None) -> str:
    n = len(results)
    spoken = [r for r in results if r.get("text")]
    silent = [r for r in results if "segments" in r and not r.get("text")]
    failed = [r for r in results if r.get("error")]
    flagged = [r for r in results if r.get("flag")]
    langs = Counter(r.get("language") for r in spoken)
    head = [f"Listened to {n} file(s) in {info['elapsed_s']:.1f} s on {info['backend']}"
            + (f" (first-time setup took {info['setup_s']:.0f} s and is now cached)" if info.get("setup_s") else "")
            + "."]
    head.append(f"speech: {len(spoken)} · no speech detected: {len(silent)} · flagged: {len(flagged)}"
                f" · failed: {len(failed)}" + (f" · languages: {', '.join(f'{k}×{v}' for k, v in langs.most_common(5))}"
                                              if langs else ""))
    head += [f"note: {x}" for x in notes]
    total_chars = sum(len(r.get("text") or "") for r in results)
    inline = n <= _INLINE_FILES and total_chars <= _INLINE_CHARS
    body: list[str] = []
    if inline and saved is not None:
        body.append(f"\nResults were also written to: {saved}")
    if inline:
        for r in results:
            dur = f"{r['seconds']:.1f} s · " if r.get("seconds") is not None else ""
            body.append(f"\n== {r['file']}  ({dur}{r.get('language') or '?'})")
            if r.get("error"):
                body.append(f"ERROR: {r['error']}")
                continue
            if not r.get("segments"):
                body.append("(no speech detected — silence, music or sound effects; Listen only transcribes speech)")
                continue
            for s in r["segments"]:
                body.append(f"[{_ts(s['from'])} → {_ts(s['to'])}] {s['text']}")
            if r.get("flag"):
                body.append(f"⚠ {r['flag']}")
    else:
        if saved is not None:
            body.append(f"\nFull results ({n} rows: file, language, seconds, text, flag, error) were written to:\n{saved}")
        body.append(f"\nFirst {min(_PREVIEW_ROWS, n)} rows:")
        for r in results[:_PREVIEW_ROWS]:
            txt = r.get("error") and f"ERROR: {r['error']}" or r.get("text") or "(no speech)"
            mark = " ⚠" if r.get("flag") else ""
            body.append(f"{Path(r['file']).name}\t{r.get('language') or ''}\t{txt[:160]}{mark}")
        if flagged:
            body.append(f"\nFlagged ({len(flagged)}) — likely invented by whisper, check before using: "
                        + ", ".join(Path(r['file']).name for r in flagged[:15]) + (" …" if len(flagged) > 15 else ""))
        if failed:
            body.append(f"\nFailed ({len(failed)}): " + "; ".join(f"{Path(r['file']).name}: {r['error']}"
                                                                   for r in failed[:8]))
    return "\n".join(head + body)


# ── 道具の入口 ─────────────────────────────────────────────────────────────

def listen_tool(inp: dict) -> ToolResult:
    if not isinstance(inp, dict):
        return ToolResult("Error: arguments must be a JSON object", ok=False, kind="error")
    notes: list[str] = []
    try:
        files = _collect(inp, notes)
        if not files:
            return ToolResult("Error: no audio files to listen to. " + " ".join(notes), ok=False, kind="error")
        lang = str(inp.get("language") or "auto").strip().lower() or "auto"
        if not re.fullmatch(r"auto|[a-z]{2,3}", lang):
            return ToolResult(f"Error: language must be 'auto' or a code like 'ja' / 'en' (got {lang!r})",
                              ok=False, kind="error")
        results, info = _transcribe(files, language=lang, prompt=str(inp.get("prompt") or "").strip(),
                                    vad=bool(inp.get("vad")), notes=notes)
    except ListenError as e:
        msg = str(e)
        if msg.startswith("could not download") or "disk space" in msg:
            msg += (f"\nThe engine and model are fetched once into {_root()} (about "
                    f"{(_ENGINES[_variant()][2] + _MODEL[3]) / 1e9:.1f} GB). Check the network or free space, then retry.")
        return ToolResult(f"Error: {msg}", ok=False, kind="cancelled" if msg == "cancelled" else "error")

    saved: Path | None = None
    want = str(inp.get("save_to") or "").strip()
    total_chars = sum(len(r.get("text") or "") for r in results)
    if want or len(results) > _INLINE_FILES or total_chars > _INLINE_CHARS:
        from . import tools as _t
        if want:
            saved = Path(_t._abs(want))
            if saved.suffix.lower() not in (".tsv", ".csv", ".json", ".txt"):
                saved = saved.with_suffix(".tsv")
        else:
            saved = Path(_t._cwd()) / f"listen-{time.strftime('%Y%m%d-%H%M%S')}.tsv"
        try:
            _save(results, saved)
        except OSError as e:
            notes.append(f"could not write {saved}: {e}")
            saved = None
    text = _render(results, info, notes, saved)
    ok = any("segments" in r for r in results)
    picture = None
    if inp.get("look"):
        png, head = sound_picture(files[0])
        text = head + "\n\n" + text
        if png:
            import base64
            picture = {"path": f"{files[0].name}.sound.png", "mime": "image/png", "b64": base64.b64encode(png).decode("ascii")}
            ok = True
    data = {"files": len(results), "speech": sum(1 for r in results if r.get("text")),
            "failed": sum(1 for r in results if r.get("error")),
            "flagged": sum(1 for r in results if r.get("flag")),
            "backend": info["backend"], "saved": str(saved) if saved else None}
    if picture:
        data["image"] = picture
    return ToolResult(text, ok=ok, kind="ok" if ok else "error", data=data)


DESCRIPTION = """Listen to audio files and transcribe the speech in them (local whisper.cpp — on the GPU when one is available; nothing is sent over the network).

- Accepts one file, a folder (all audio files in it; recursive=true for sub-folders), a glob like D:\\voices\\*_voice*.wav, or a list in `paths`. Reads wav / mp3 / flac / ogg directly; m4a / aac / opus / mp4 / webm and other formats are converted with ffmpeg when it is on PATH.
- Returns the language and timestamped text for each file. With more than 20 files (or long text) the full table is written to a TSV (or to `save_to`: .tsv / .csv / .json / .txt) and a summary plus the first rows are returned — read the file for the rest.
- Use it to check what an audio file really holds — whether it is speech, and what is said. You can hear audio this way: ffmpeg's volume numbers only show that there is sound, not what it is.
- Use this instead of installing whisper / Python speech packages through Bash. Many files in one call is much faster than one call per file (the model loads once).
- It transcribes speech only. Music, sound effects and silence come back as "no speech", and whisper sometimes invents stock phrases from them (e.g. "Thank you.", "ご視聴ありがとうございました") — those are marked ⚠ suspect; do not present them as real dialogue.
- `language` defaults to auto-detect; set it (e.g. "ja") when you know it — short clips are often mis-detected. `prompt` can carry names or terms to spell correctly.
- look=true also shows you the sound of the first file as a picture (waveform and spectrogram) with its length, volume and silent parts — use it for music, sound effects, noise or silence, which have no speech to transcribe.
- The first call downloads the engine and model once (about 1.5 GB with an NVIDIA GPU, 0.9 GB otherwise) and may take a few minutes."""


def schema() -> dict:
    return {"type": "object", "properties": {
        "path": {"type": "string", "description": "An audio file, a folder, or a glob pattern"},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "Several files or folders"},
        "recursive": {"type": "boolean", "description": "Include sub-folders when a folder is given (default false)"},
        "language": {"type": "string", "description": "Spoken language code such as ja / en, or auto (default)"},
        "prompt": {"type": "string", "description": "Optional names or terms to help spelling"},
        "save_to": {"type": "string", "description": "Optional output file (.tsv, .csv, .json or .txt) for the "
                    "full results"},
        "look": {"type": "boolean", "description": "Also show the sound of the first file as a picture (waveform and "
                 "spectrogram) with its length, volume and silences"},
        "vad": {"type": "boolean", "description": "Skip silence with voice-activity detection (default false; "
                "only for long recordings with long silences — it drops quiet short lines)"},
    }, "required": []}
