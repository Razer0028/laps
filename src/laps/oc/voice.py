"""Voice — 文を声にする・声を写す（声の複製）道具（2026-10-08 オーナー「Blackwall に音声クローンツールを搭載して下さい」）。

★仕組み: Chatterbox Multilingual（Resemble AI・MIT・23 言語・日本語あり・500M）を**利用者の機械で**動かす。
  参照の声（数秒〜30 秒の音声）を渡すと、その声で文を読む（学習は要らない＝zero-shot）。渡さなければ既定の声。
  ★作った音声には必ず Perth の透かし（耳では聞こえない・MP3 にしても残る）が入る。外す口は作らない。
  PyTorch は同梱の Python（純 Python の wheel だけ）に入れられないので、初めて使うときに uv（Astral・単体の実行ファイル・
  sha256 照合）を取り寄せ、専用の Python 3.11 と環境を作って入れる。置き場 ~/.blackwall/voice
  （LAPS_VOICE_DIR・アプリは <データ根>\\voice を渡す）。NVIDIA があれば CUDA 12.4 版の PyTorch。
  生成は別の Python で走る台本（oc/voice_worker.py）。依頼と結果は JSON のファイルで受け渡す。

★2026-10-08 開発機（Linux・CPU 4 スレッド）で通して決めたこと:
  ・uv の環境には setuptools が入らず、透かし（resemble-perth）が pkg_resources を読めずに None になって落ちた
    → setuptools<81 を足す。
  ・読み込み 7.7 秒・1.9 秒の声に 13 秒・写した声 2.6 秒に 7.3 秒（CPU）。透かしの検出 1.0。モデルは約 3GB。
  ・Listen で聞き直すと「同じ声で」→「同時声で」。漢字の読みを取り違えることがある
    → check=true で作った声を Listen で文字に戻して並べる。読みが怪しい語は かな で書く（skill voice-clone）。
"""
from __future__ import annotations

from .. import paths as _lp
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

from ..tools import ToolResult

# ── 取りに行く物（版を固定する）──────────────────────────────────────────────

_UV_REL = "0.12.23"
_UV_GH = f"https://github.com/astral-sh/uv/releases/download/{_UV_REL}/"
_UV = {   # 名前: (URL, sha256, 大きさ)
    "win": (_UV_GH + "uv-x86_64-pc-windows-msvc.zip",
            "75d05de6762778c31ee183398de7dd15093fad0ed90b1f236d8205ea5ec00c90", 18043715),
    "linux": (_UV_GH + "uv-x86_64-unknown-linux-gnu.tar.gz",
              "9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6", 19906150),
}
_PY = "3.11"
_PKGS = ["chatterbox-tts==0.1.7", "torch==2.6.0", "torchaudio==2.6.0", "setuptools<81"]
_TORCH_INDEX = {"cuda": "https://download.pytorch.org/whl/cu124", "cpu": "https://download.pytorch.org/whl/cpu"}

LANGS = {"ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja", "ko", "ms", "nl", "no", "pl",
         "pt", "ru", "sv", "sw", "tr", "zh"}
OUT_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".opus"}
REF_MIN_S, REF_MAX_S = 3.0, 30.0
MAX_LINES = 500
_CREATE_NO_WINDOW = 0x08000000
_lock = threading.Lock()


class VoiceError(Exception):
    pass


def _machine() -> str:
    return (platform.machine() or "").lower()


def available() -> bool:
    if os.environ.get("LAPS_VOICE", "1").strip() == "0":
        return False
    if _machine() not in ("amd64", "x86_64"):
        return False
    return sys.platform == "win32" or sys.platform.startswith("linux")


def _root() -> Path:
    d = os.environ.get("LAPS_VOICE_DIR")
    return Path(d).expanduser() if d else _lp.dot_dir() / "voice"


def _has_nvidia() -> bool:
    if os.environ.get("LAPS_VOICE_GPU", "1").strip() == "0":
        return False
    if sys.platform == "win32":
        win = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        return (Path(win) / "System32" / "nvcuda.dll").exists()
    return Path("/proc/driver/nvidia/version").exists()


def _flavor() -> str:
    return "cuda" if _has_nvidia() else "cpu"


def _venv_python() -> Path:
    v = _root() / "venv"
    return v / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _marker() -> Path:
    return _root() / "venv" / ".bw-ok.json"


def _want() -> dict:
    return {"pkgs": _PKGS, "py": _PY, "flavor": _flavor(), "uv": _UV_REL}


def installed() -> dict | None:
    """入っていればその中身（版・cuda/cpu）、無ければ None。"""
    try:
        got = json.loads(_marker().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not _venv_python().exists():
        return None
    return got


def _uv_env() -> dict:
    env = dict(os.environ)
    r = _root()
    env.update({"UV_PYTHON_INSTALL_DIR": str(r / "py"), "UV_CACHE_DIR": str(r / "uv-cache"),
                "UV_PYTHON_PREFERENCE": "only-managed", "PYTHONIOENCODING": "utf-8"})
    for k in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(k, None)
    return env


def _run(argv: list[str], cwd: Path, timeout: float, env: dict, log: Path) -> int:
    """出力は log へ（パイプを使わない）・窓を出さない・止めるに従う。"""
    from . import listen as _ls
    with open(log, "ab") as fe:
        kw: dict = {"cwd": str(cwd), "stdin": subprocess.DEVNULL, "stdout": fe, "stderr": fe, "env": env}
        if sys.platform == "win32":
            kw["creationflags"] = _CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            cmd: str | list[str] = " ".join(_ls._win_quote(a) for a in argv)
        else:
            kw["start_new_session"] = True
            cmd = argv
        proc = subprocess.Popen(cmd, **kw)
        t0 = time.monotonic()
        while proc.poll() is None:
            if _ls._cancelled():
                _ls._kill(proc)
                raise VoiceError("cancelled")
            if time.monotonic() - t0 > timeout:
                _ls._kill(proc)
                raise VoiceError(f"{Path(argv[0]).name} did not finish within {timeout:.0f} s")
            time.sleep(0.25)
    return proc.returncode


def _log_tail(log: Path, n: int = 12) -> str:
    try:
        lines = [ln for ln in log.read_bytes().decode("utf-8", "replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return "\n".join(x[-300:] for x in lines[-n:])


def _ensure_uv() -> Path:
    from . import listen as _ls
    key = "win" if sys.platform == "win32" else "linux"
    url, sha, size = _UV[key]
    d = _root() / "uv" / _UV_REL
    exe = d / ("uv.exe" if sys.platform == "win32" else "uv")
    if exe.exists() and (d / ".ok").exists():
        return exe
    archive = _root() / "downloads" / url.rsplit("/", 1)[1]
    try:
        if not (archive.exists() and archive.stat().st_size == size and _ls._sha256(archive) == sha):
            _ls._download(url, archive, sha, size)
        tmp = d.with_name(f".{d.name}.{os.getpid()}.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        _ls._safe_extract(archive, tmp)
    except Exception as e:  # noqa: BLE001
        raise VoiceError(str(e)) from e
    hits = sorted(tmp.rglob(exe.name), key=lambda p: len(p.parts))
    if not hits:
        raise VoiceError(f"{exe.name} was not found inside {archive.name}")
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    for p in hits[0].parent.iterdir():
        if p.is_file():
            shutil.move(str(p), str(d / p.name))
    shutil.rmtree(tmp, ignore_errors=True)
    if sys.platform != "win32":
        exe.chmod(exe.stat().st_mode | 0o111)
    (d / ".ok").write_text(sha, encoding="ascii")
    try:
        archive.unlink()
    except OSError:
        pass
    return exe


def prepare(load_model: bool = True) -> str:
    """専用の Python と PyTorch・Chatterbox を入れ、モデル（約 3GB）を取り寄せて一度読み込む。そろっていれば何もしない。"""
    if not available():
        return "voice: この機械では使わない"
    with _lock:
        r = _root()
        r.mkdir(parents=True, exist_ok=True)
        log = r / "install.log"
        want = _want()
        got = installed()
        new = False
        if got != want:
            free = shutil.disk_usage(r).free
            need = (7 if want["flavor"] == "cuda" else 5) * 1024 ** 3
            if free < need:
                raise VoiceError(f"not enough disk space in {r} for the voice engine "
                                 f"({need / 1024 ** 3:.0f} GB needed, {free / 1024 ** 3:.1f} GB free)")
            uv = _ensure_uv()
            env = _uv_env()
            log.write_text(f"== {time.strftime('%Y-%m-%d %H:%M:%S')} install {want}\n", encoding="utf-8")
            shutil.rmtree(r / "venv", ignore_errors=True)
            if _run([str(uv), "venv", "--python", _PY, str(r / "venv")], r, 900, env, log) != 0:
                raise VoiceError("could not create the Python environment:\n" + _log_tail(log))
            args = [str(uv), "pip", "install", "--python", str(_venv_python()), *_PKGS,
                    "--index-url", _TORCH_INDEX[want["flavor"]], "--extra-index-url", "https://pypi.org/simple",
                    "--index-strategy", "unsafe-best-match"]
            if _run(args, r, 3600, env, log) != 0:
                raise VoiceError("could not install the voice engine (PyTorch / Chatterbox):\n" + _log_tail(log))
            _marker().write_text(json.dumps(want), encoding="utf-8")
            shutil.rmtree(r / "uv-cache", ignore_errors=True)     # 入れた後の取り寄せの控え（数 GB）は要らない
            new = True
        info = ""
        if load_model:
            res = _work({"load_only": True}, timeout=3600)
            info = f"・{res.get('device')}" + (f"（{res['gpu']}）" if res.get("gpu") else "") + f"・読み込み {res.get('load_s')} 秒"
        return f"voice: {'用意した' if new else 'そろっている'}（{want['flavor']} 版{info}）"


def _work(req: dict, timeout: float) -> dict:
    """生成係（voice_worker.py）を専用の Python で走らせ、結果の JSON を返す。"""
    r = _root()
    run = r / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run.mkdir(parents=True, exist_ok=True)
    req = dict(req)
    req["result"] = str(run / "result.json")
    # ★auto: GPU が使えれば GPU、だめなら CPU（ドライバはあるのに CUDA が使えない機械で落とさない・開発機 で実際にそうだった）。
    req.setdefault("device", "auto")
    req.setdefault("threads", max(1, min(8, (os.cpu_count() or 2) - 1)))     # CPU で走るときの上限
    (run / "request.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    env = _uv_env()
    env.update({"HF_HOME": str(r / "hf"), "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false",
                "PYTHONUTF8": "1", "PYTHONWARNINGS": "ignore"})
    worker = Path(__file__).with_name("voice_worker.py")
    log = run / "worker.log"
    rc = _run([str(_venv_python()), "-u", str(worker), str(run / "request.json")], run, timeout, env, log)
    try:
        res = json.loads((run / "result.json").read_text(encoding="utf-8"))
    except Exception:
        raise VoiceError(f"the voice engine stopped without a result (exit {rc}):\n" + _log_tail(log))
    if res.get("error"):
        raise VoiceError(f"{res['error']}\n{res.get('trace', '')[-600:]}")
    _prune_runs()
    return res


def _prune_runs(keep: int = 20) -> None:
    runs = sorted((_root() / "runs").glob("*"), key=lambda p: p.name)
    for p in runs[:-keep]:
        shutil.rmtree(p, ignore_errors=True)


# ── 参照の声 ─────────────────────────────────────────────────────────────────

def _voices_dir() -> Path:
    return _root() / "voices"


_NAME_RE = re.compile(r"^[\w\-ぁ-んァ-ヶー一-龯]{1,40}$")


def _ffmpeg() -> str | None:
    from . import listen as _ls
    return _ls._ffmpeg()


def _seconds(p: Path) -> float | None:
    try:
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return None


def _prepare_ref(src: Path, dest: Path) -> tuple[Path, float | None, list[str]]:
    """参照の声を 24kHz・モノラルの wav（最長 30 秒）にして dest に置く。"""
    notes: list[str] = []
    ff = _ffmpeg()
    if ff:
        dest.parent.mkdir(parents=True, exist_ok=True)
        argv = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-t", str(int(REF_MAX_S)),
                "-ac", "1", "-ar", "24000", str(dest)]
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=300,
                           creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if p.returncode != 0 or not dest.exists():
            raise VoiceError(f"ffmpeg could not read the reference audio {src}: "
                             f"{p.stderr.decode('utf-8', 'replace')[-300:]}")
    elif src.suffix.lower() == ".wav":
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        notes.append("ffmpeg is not available, so the reference was used as is (not resampled or trimmed).")
    else:
        raise VoiceError(f"{src.suffix} reference audio needs ffmpeg (not found). Convert it to .wav first.")
    secs = _seconds(dest)
    if secs is not None and secs < REF_MIN_S:
        notes.append(f"The reference is only {secs:.1f} s. 8-20 s of clean speech gives a much closer voice.")
    return dest, secs, notes


def _resolve_voice(v: str, notes: list[str]) -> tuple[Path | None, dict | None]:
    """voice の指定（保存した名前・音声ファイルの道）を、生成係に渡す wav に変える。"""
    from . import tools as _oc
    if not v:
        return None, None
    saved = _voices_dir() / f"{v}.wav"
    if _NAME_RE.match(v) and saved.exists():
        meta = None
        try:
            meta = json.loads(saved.with_suffix(".json").read_text(encoding="utf-8"))
        except Exception:
            pass
        return saved, meta
    path, _n = _oc._fix_path(_oc._abs(v), "existing")
    p = Path(path)
    if not p.is_file():
        names = ", ".join(sorted(x.stem for x in _voices_dir().glob("*.wav"))) or "none"
        raise VoiceError(f"voice {v!r} is neither a saved voice (saved: {names}) nor an audio file.")
    tmp = _root() / "tmp" / f"ref-{os.getpid()}-{int(time.time())}.wav"
    ref, _secs, n = _prepare_ref(p, tmp)
    notes.extend(n)
    return ref, None


def _save_voice(name: str, src: str, consent: str) -> str:
    from . import tools as _oc
    if not _NAME_RE.match(name):
        raise VoiceError("save_voice: use 1-40 letters, digits, _ or - (Japanese is fine).")
    if len(" ".join(consent.split())) < 4:
        raise VoiceError("save_voice needs `consent`: whose voice this is and the permission to use it "
                         "(e.g. \"my own voice\", \"voice actor X, contract for game Y\", \"synthetic voice\").")
    path, _n = _oc._fix_path(_oc._abs(src), "existing")
    if not Path(path).is_file():
        raise VoiceError(f"save_voice: the reference audio {path} does not exist.")
    dest = _voices_dir() / f"{name}.wav"
    _ref, secs, notes = _prepare_ref(Path(path), dest)
    meta = {"name": name, "source": str(path), "consent": " ".join(consent.split()),
            "seconds": round(secs or 0, 1), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    dest.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"Saved voice {name!r} ({meta['seconds']} s, consent: {meta['consent']})." + (
        "\n" + "\n".join(notes) if notes else "")


def _list_voices() -> str:
    rows = []
    for p in sorted(_voices_dir().glob("*.wav")):
        try:
            m = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        except Exception:
            m = {}
        rows.append(f"- {p.stem}: {m.get('seconds', '?')} s · consent: {m.get('consent', '?')} · from {m.get('source', '?')}")
    inst = installed()
    head = (f"Voice engine: installed ({inst['flavor']})" if inst else
            "Voice engine: not installed yet (installed on first use, about 5-7 GB)")
    return head + "\n" + ("Saved voices:\n" + "\n".join(rows) if rows else "No saved voices yet.")


# ── 道具 ────────────────────────────────────────────────────────────────────

def _out_path(raw: str | None, i: int | None) -> Path:
    from . import tools as _oc
    if raw:
        p = Path(_oc._abs(str(raw)))
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        p = Path(_oc._cwd()) / "voice" / (f"voice-{stamp}.wav" if i is None else f"voice-{stamp}-{i + 1:03d}.wav")
    if p.suffix.lower() not in OUT_EXT:
        p = p.with_suffix(".wav")
    return p


def _convert(wav: Path, dest: Path) -> str | None:
    ff = _ffmpeg()
    if not ff:
        return f"{dest.suffix} needs ffmpeg (not found); kept the .wav at {wav}"
    p = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav), str(dest)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=600,
                       creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if p.returncode != 0 or not dest.exists():
        return f"ffmpeg could not write {dest.name}; kept the .wav at {wav}"
    try:
        wav.unlink()
    except OSError:
        pass
    return None


def voice_tool(inp: dict) -> ToolResult:
    inp = inp if isinstance(inp, dict) else {}
    try:
        if inp.get("list_voices"):
            return ToolResult(_list_voices(), ok=True, kind="ok")
        if inp.get("save_voice"):
            if not inp.get("voice"):
                raise VoiceError("save_voice needs `voice`: the path of the reference audio to save.")
            msg = _save_voice(str(inp["save_voice"]).strip(), str(inp["voice"]), str(inp.get("consent") or ""))
            if not (inp.get("text") or inp.get("lines")):
                return ToolResult(msg, ok=True, kind="ok")
            inp = {**inp, "voice": str(inp["save_voice"]).strip()}
        else:
            msg = ""
        if inp.get("prepare") and not (inp.get("text") or inp.get("lines")):
            t0 = time.time()
            return ToolResult(prepare() + f"（{time.time() - t0:.0f} 秒）", ok=True, kind="ok")
        items: list[dict] = []
        if inp.get("lines"):
            lines = inp["lines"]
            if not isinstance(lines, list):
                raise VoiceError("lines must be a list of {text, out}.")
            if len(lines) > MAX_LINES:
                raise VoiceError(f"at most {MAX_LINES} lines per call (got {len(lines)}); split the job.")
            for i, ln in enumerate(lines):
                if isinstance(ln, str):
                    ln = {"text": ln}
                items.append({"text": str(ln.get("text") or ""), "out": _out_path(ln.get("out"), i)})
        elif inp.get("text"):
            items.append({"text": str(inp["text"]), "out": _out_path(inp.get("out"), None)})
        else:
            raise VoiceError("give `text` (with optional `out`) or `lines`; or list_voices / save_voice.")
        if any(not it["text"].strip() for it in items):
            raise VoiceError("one of the lines has empty text.")
        lang = str(inp.get("language") or "ja").strip().lower()
        if lang not in LANGS:
            raise VoiceError(f"language {lang!r} is not supported. Supported: {', '.join(sorted(LANGS))}.")
        notes: list[str] = []
        ref, meta = _resolve_voice(str(inp.get("voice") or "").strip(), notes)
        t0 = time.time()
        setup = ""
        if installed() != _want():
            setup = prepare(load_model=False)
        from .voice_worker import split_text
        n_chunks = sum(len(split_text(it["text"])) for it in items)
        per = 25 if _flavor() == "cuda" else 90
        req: dict = {"language": lang, "voice": str(ref) if ref else None,
                     "items": [{"text": it["text"], "out": str(it["out"].with_suffix(".wav"))} for it in items]}
        for k in ("exaggeration", "cfg_weight", "temperature", "seed"):
            if inp.get(k) is not None:
                req[k] = inp[k]
        # 初めては、生成の前にモデル（約 3GB）を取り寄せる＝その分を待つ。
        first = not any((_root() / "hf").glob("hub/models--ResembleAI--chatterbox*"))
        res = _work(req, timeout=(3600 if first else 600) + per * n_chunks)
        out_rows, failed, outs = [], 0, []
        for it, row in zip(items, res.get("items") or []):
            if row.get("error"):
                failed += 1
                out_rows.append(f"✗ {it['text'][:60]} → {row['error']}")
                continue
            dest = it["out"]
            wav = Path(row["out"])
            if dest.suffix.lower() != ".wav":
                warn = _convert(wav, dest)
                if warn:
                    notes.append(warn)
                    dest = wav
            outs.append(str(dest))
            out_rows.append(f"✓ {dest}  ({row.get('seconds')} s · {row.get('chunks')} part(s) · {row.get('gen_s')} s to make)")
        head = (f"Made {len(outs)} of {len(items)} audio file(s) in {time.time() - t0:.0f} s on {res.get('device')}"
                + (f" ({res['gpu']})" if res.get("gpu") else "") + f" · language {lang} · voice: "
                + (f"saved {inp.get('voice')!r} (consent: {meta.get('consent')})" if meta else
                   (f"cloned from {inp.get('voice')}" if ref else "built-in default")) + ".")
        body = [msg] if msg else []
        if setup:
            body.append(f"First-time setup: {setup}")
        body += [head, *out_rows]
        body.append("Every file carries an inaudible Perth watermark (marks it as AI-generated).")
        body += notes
        if inp.get("check") and outs:
            try:
                from . import listen as _ls
                chk = _ls.listen_tool({"paths": outs, "language": lang})
                body.append("Check — what Listen hears in the result (compare with the text you asked for; a wrong "
                            "word usually means a misread kanji: rewrite it in kana and make that line again):\n"
                            + str(chk))
            except Exception as e:  # noqa: BLE001
                body.append(f"(Could not run the Listen check: {e})")
        elif outs:
            body.append("To confirm the words, run Voice again with check=true, or Listen on the files.")
        ok = failed == 0
        return ToolResult("\n".join(body), ok=ok, kind="ok" if ok else "error")
    except VoiceError as e:
        return ToolResult(f"Error: {e}", ok=False, kind="error")


DESCRIPTION = """Turn text into speech, optionally in a cloned voice (local Chatterbox Multilingual — on the GPU when there is one; nothing is sent over the network). 23 languages including Japanese (language="ja", the default).

- text + out: one audio file (.wav, or .mp3/.ogg/.flac/.m4a via ffmpeg). Long text is split at sentence ends and joined.
- lines: many files in one call (the model loads once): [{"text": "...", "out": "D:/game/voice/001.wav"}, ...].
- voice: whose voice to use — a saved voice name, or the path of a reference recording (8-20 s of one person speaking clearly, no music or noise; any audio format with ffmpeg). Omit for the built-in voice.
- save_voice + consent: keep the reference under a name for later (voice=<path>, save_voice="narrator", consent="whose voice and the permission").
- list_voices: show saved voices and whether the engine is installed.
- check: true transcribes the results with Listen so you can compare the words (misread kanji show up here).
- exaggeration (0.25-2, default 0.5: more = more emotional), cfg_weight (0-1, default 0.5: lower = slower, calmer pacing; use ~0.3 when the reference speaks fast), temperature, seed (same seed = same take).
The first call installs the engine (Python + PyTorch + model, about 5-7 GB) and can take several minutes.

Consent: only clone a voice you have the right to use — your own, someone who agreed, or a licensed or synthetic voice. Never imitate a real person to deceive anyone or to put words in their mouth. Every output is watermarked as AI-generated; there is no way to turn that off."""

SCHEMA_PROPS = {
    "text": {"type": "string", "description": "Text to speak (one file)"},
    "out": {"type": "string", "description": "Output file for text (.wav default; .mp3/.ogg/.flac/.m4a need ffmpeg)"},
    "lines": {"type": "array", "description": "Many files at once: [{text, out}]",
              "items": {"type": "object", "properties": {"text": {"type": "string"}, "out": {"type": "string"}}}},
    "voice": {"type": "string", "description": "Saved voice name or reference audio path (omit = built-in voice)"},
    "language": {"type": "string", "description": "Language code (ja, en, zh, ko, fr, de, ...). Default ja"},
    "save_voice": {"type": "string", "description": "Save the reference given in voice under this name"},
    "consent": {"type": "string", "description": "With save_voice: whose voice it is and the permission to use it"},
    "list_voices": {"type": "boolean", "description": "List saved voices and engine status"},
    "check": {"type": "boolean", "description": "Transcribe the results with Listen to verify the words"},
    "exaggeration": {"type": "number", "description": "Emotion intensity 0.25-2 (default 0.5)"},
    "cfg_weight": {"type": "number", "description": "Pacing/adherence 0-1 (default 0.5; ~0.3 for fast speakers)"},
    "temperature": {"type": "number", "description": "Sampling temperature (default 0.8)"},
    "seed": {"type": "integer", "description": "Random seed for a repeatable take"},
    "prepare": {"type": "boolean", "description": "Only install the engine and model now"},
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--prepare" in args:
        try:
            print(prepare(), flush=True)
            return 0
        except VoiceError as e:
            print(f"voice: そろえられなかった: {e}", flush=True)
            return 1
    print(_list_voices())
    return 0


if __name__ == "__main__":
    sys.exit(main())
