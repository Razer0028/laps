"""Bash の道具が起動する子プロセスの PATH（2026-10-06 オーナー「開発用の道具や環境を同封したい」）。

2 つを足す:
  1. 道具の置き場 `~/.blackwall/tools`（LAPS_TOOLS_DIR で替えられる）。持ち運べる形の道具
     （zip を展開しただけの ffmpeg・node・go など）をここに置けば、管理者権限なしで次の命令から使える。
     置き場の直下・各ディレクトリ・その中の bin を、実行ファイルがある所だけ PATH の先頭に足す（深さ 3 まで）。
  2. ★Windows はレジストリの PATH（機械＋利用者）を毎回読み直して、今の PATH に無い所を末尾に足す。
     winget や scoop で入れた道具は利用者の PATH に書かれるが、**動いているプロセスの PATH は起動時のまま**
     なので、Blackwall を再起動するまで「認識されていません」になっていた。末尾に足すので、アプリが
     自分で先頭に置いた物（同梱の Python 等）の順は変えない。
"""
from __future__ import annotations

from .. import paths as _lp
import os
import sys
from pathlib import Path

_MAX_DEPTH = 3
_WIN_EXE = (".exe", ".cmd", ".bat", ".ps1", ".com")


def tools_dir() -> Path:
    d = os.environ.get("LAPS_TOOLS_DIR")
    return Path(d).expanduser() if d else _lp.dot_dir() / "tools"


# ffmpeg（音声・動画の変換。Listen が wav 以外を読むときと、声や動画の作業で使う）。版と中身を固定する。
# GyanD/codexffmpeg の公式リリース（gyan.dev の essentials 版・2026-09-20）。照合値は GitHub の API の digest。
_FFMPEG = ("ffmpeg-9.0.2-essentials_build.zip",
           "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip",
           "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba", 114768076)


# Linux（端末版）用。johnvansickle.com の静的版（版が上がると旧版は old-releases/ へ移るので 2 か所を順に試す）。
# 照合値は 2026-10-07 に取得して計算した。Linux 版はまず .deb の Recommends（apt の ffmpeg）で入る。これは入っていない機械の逃げ道。
_FFMPEG_LINUX = ("ffmpeg-7.0.2-amd64-static.tar.xz",
                 ("https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
                  "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-7.0.2-amd64-static.tar.xz"),
                 "abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67", 41888096)


def prepare_ffmpeg() -> str:
    """ffmpeg が使えなければ道具の置き場（tools_dir()\\ffmpeg）へ取り寄せる（oc/parts.py から・2026-10-07）。
    ★オーナー機で ~/.blackwall ごと消え、Blackwall が以前取り寄せた ffmpeg が無くなった。起動のたびにそろえる。
    PATH（レジストリ・道具の置き場を含む）に既にあれば何もしない。Windows は gyan.dev 版・Linux（x86_64）は静的版。"""
    import shutil
    found = shutil.which("ffmpeg", path=child_path())
    if found:
        return f"ffmpeg: そろっている（{found}）"
    import platform
    from .listen import _download, _safe_extract      # 取り寄せ・照合・安全な展開は Listen と同じ物を使う
    root = tools_dir()
    if sys.platform == "win32":
        name, url, sha, size = _FFMPEG
        urls, exe_name = (url,), "ffmpeg.exe"
    elif sys.platform.startswith("linux") and platform.machine().lower() in ("x86_64", "amd64"):
        name, urls, sha, size = _FFMPEG_LINUX
        exe_name = "ffmpeg"
    else:
        return "ffmpeg: この機械向けの取り寄せ先が無い（パッケージの ffmpeg を入れてください）"
    archive = root / ".downloads" / name
    last: Exception | None = None
    for u in urls:                                       # 照合値が合った物だけを使う（版が上がった先は次の候補へ）
        try:
            _download(u, archive, sha, size)
            last = None
            break
        except Exception as e:
            last = e
    if last is not None:
        raise last
    tmp = root / f".ffmpeg.{os.getpid()}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    _safe_extract(archive, tmp)
    exe = next(iter(sorted((p for p in tmp.rglob(exe_name) if p.is_file()), key=lambda p: len(p.parts))), None)
    if exe is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"{name} の中に {exe_name} がありません")
    # Windows は bin の 1 つ上（final\\bin\\ffmpeg.exe）、Linux の静的版は直下（final/ffmpeg）。どちらも PATH の深さ 3 に収まる。
    top = exe.parent.parent if exe.parent.name.lower() == "bin" else exe.parent
    final = root / "ffmpeg"
    shutil.rmtree(final, ignore_errors=True)
    os.replace(top, final)
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        archive.unlink()
    except OSError:
        pass
    got = next(iter(sorted(final.rglob(exe_name), key=lambda p: len(p.parts))), final)
    return f"ffmpeg: 用意した（{got}）"


def _has_exe(d: Path) -> bool:
    try:
        for p in d.iterdir():
            if not p.is_file():
                continue
            if sys.platform == "win32":
                if p.suffix.lower() in _WIN_EXE:
                    return True
            elif os.access(p, os.X_OK):
                return True
    except OSError:
        pass
    return False


def managed_dirs() -> list[str]:
    root = tools_dir()
    if not root.is_dir():
        return []
    out: list[str] = []

    def walk(d: Path, depth: int) -> None:
        if _has_exe(d):
            out.append(str(d))
        if depth >= _MAX_DEPTH:
            return
        try:
            subs = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return
        for s in subs:
            walk(s, depth + 1)

    walk(root, 0)
    return out


def _registry_path() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    out: list[str] = []
    keys = ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"))
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                val, typ = winreg.QueryValueEx(k, "Path")
        except OSError:
            continue
        if not isinstance(val, str):
            continue
        if typ == winreg.REG_EXPAND_SZ:
            try:
                val = winreg.ExpandEnvironmentStrings(val)
            except OSError:
                val = os.path.expandvars(val)
        out += [p.strip() for p in val.split(";") if p.strip()]
    return out


def _norm(p: str) -> str:
    p = p.strip().rstrip("\\/")
    return p.lower() if sys.platform == "win32" else p


def child_path(current: str | None = None) -> str:
    cur = [p for p in (current if current is not None else os.environ.get("PATH", "")).split(os.pathsep) if p]
    seen = {_norm(p) for p in cur}
    head = [d for d in managed_dirs() if _norm(d) not in seen]
    seen.update(_norm(d) for d in head)
    tail = []
    for p in _registry_path():
        n = _norm(p)
        if n not in seen:
            tail.append(p)
            seen.add(n)
    return os.pathsep.join(head + cur + tail)


#: 起動元（term の client・アプリの gateway）が本体を見つけるために PYTHONPATH を書き換える前の値（空＝元は無かった）。
USER_PYTHONPATH = "LAPS_USER_PYTHONPATH"


def child_env() -> dict[str, str] | None:
    """子プロセスに渡す環境。足す物も戻す物も無ければ None（＝今までどおり親の環境を受け継ぐ）。
    ★PYTHONPATH は起動元が書き換える前の値に戻す（2026-10-22 Terminal-Bench の fix-pandas-version: term の client が自分の sys.path
    ＝/usr/lib/python3.11 と同梱の venv を PYTHONPATH に入れ、Bash の道具まで伝わって、課題の Python 3.8 が 3.11 の標準ライブラリを
    読んで起動できなかった。モデルは課題の python3 を Blackwall の venv につなぎ替えて落ちた）。"""
    env = None
    try:
        new = child_path()
    except Exception:
        new = os.environ.get("PATH", "")
    if new != os.environ.get("PATH", ""):
        env = dict(os.environ)
        env["PATH"] = new
    if USER_PYTHONPATH in os.environ:
        orig = os.environ.get(USER_PYTHONPATH, "")
        if os.environ.get("PYTHONPATH", "") != orig:
            env = env if env is not None else dict(os.environ)
            if orig:
                env["PYTHONPATH"] = orig
            else:
                env.pop("PYTHONPATH", None)
    return env


# ── 定番の道具をそろえる（知識サーバー の道具の棚から・2026-10-15）────────────────────────────────────────
# オーナー 2026-10-15「既成のツールをもっと使うような仕組みに」「READ とか EDIT だけだと時間がかかるし精度も不安定」
# 「せっかく 知識サーバー にツールを色々配置しているのに全然使ってくれない」。棚の道具はモデルが自分で探して入れない限り
# この機械に無く、<env> にも出なかった。起動のたびに（oc/parts）定番の物を入れておき、<env> に名前を出す（installed_tools）。
#: そろえる物（棚の名前）。PATH に既にある物・入れてある物は入れない。LAPS_TOOLBOX=0 で止める・名前をカンマで並べると替わる。
TOOLBOX = ("rg", "fd", "jq", "yq", "sd", "7zr", "7zip", "exiftool", "MediaInfo", "DetectItEasy")
#: 棚の名前 → PATH で探す命令（どれかがあれば入れない）
_TOOLBOX_CMD = {"7zip": ("7z", "7za", "7zz"), "MediaInfo": ("mediainfo",), "DetectItEasy": ("diec",)}
STAMP = ".bw-installed.json"


def toolbox_names() -> list[str]:
    v = (os.environ.get("LAPS_TOOLBOX") or "1").strip()
    if v == "0":
        return []
    if v in ("1", ""):
        return list(TOOLBOX)
    return [x.strip() for x in v.split(",") if x.strip()]


def prepare_toolbox() -> str:
    """（公開版には道具の棚が無いので何もしない）"""
    return "toolbox: off"

def installed_tools(limit: int = 30) -> list[str]:
    """道具の置き場にある道具（棚から入れた物は名前と主な命令）。<env> に出す。"""
    root = tools_dir()
    if not root.is_dir():
        return []
    out = []
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")), key=lambda d: d.name.lower())
    except OSError:
        return []
    for d in dirs:
        exes = []
        for dirpath, dirnames, filenames in os.walk(d):
            if len(Path(dirpath).relative_to(d).parts) >= _MAX_DEPTH:
                dirnames[:] = []
            for fn in filenames:
                low = fn.lower()
                if (sys.platform == "win32" and low.endswith((".exe", ".cmd", ".bat"))) or \
                        (sys.platform != "win32" and os.access(os.path.join(dirpath, fn), os.X_OK)
                         and "." not in fn):
                    exes.append(Path(fn).stem)
            if len(exes) > 40:
                break
        if not exes:
            continue
        cmds = sorted({e for e in exes}, key=lambda e: (e.lower() != d.name.lower(), len(e), e.lower()))[:3]
        out.append(d.name if cmds == [d.name] else f"{d.name} ({', '.join(cmds)})")
        if len(out) >= limit:
            break
    return out
