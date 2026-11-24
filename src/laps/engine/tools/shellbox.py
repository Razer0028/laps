"""Bash の囲い（bubblewrap）。開発モードの命令を「作業場と一時置き場と道具の置き場だけ書ける」形で走らせる。
手本: Codex の linux-sandbox（bwrap: 根は読むだけ・書ける根だけを bind・.git は読むだけ）と sandboxing/src/denial.rs
（拒まれた形の出力の見分け）。2026-11-11 オーナー「開発の作業だけ囲う」で了承。

- LAPS_SHELL_SANDBOX: auto（既定・開発モードだけ）／on（どのモードでも）／off。
- Linux で bwrap が動くときだけ（Windows・bwrap が無い・名前空間を作れない は囲わずに走らせ、そう言う）。
- モデルは Bash(sandbox=false, reason=…) で囲いの外を選べる（コミットや作業場の外の設定など）。関所（policy.py）はそのまま効く。
"""
from __future__ import annotations

from ... import paths as _lp
import os
import shutil
import subprocess
import sys
from pathlib import Path

#: 家の中で、道具が書く置き場（パッケージの取り寄せ・組み立ての控え）。在る物だけ書けるようにする。
HOME_CACHES = (".cache", ".npm", ".local", ".cargo", ".rustup", "go", ".gradle", ".m2", ".bun", ".deno", ".yarn",
               ".pnpm-store", ".nuget", ".dotnet", ".conda", ".pyenv", ".ipython", ".jupyter")
#: 作業場の中でも読むだけにする物（履歴を壊さない・Codex は .git と自分の設定を守る）。
PROTECTED = (".git",)
#: 囲いに拒まれたときの出力の形（Codex の denial.rs と同じ語・小文字で探す）。
DENIAL_WORDS = ("read-only file system", "operation not permitted", "permission denied",
                # ★ラボの機械は LANG=ja_JP（2026-11-11 試験で「読み込み専用ファイルシステムです」が見分けられなかった）
                "読み込み専用ファイルシステム", "許可されていない操作", "操作は許可されていません", "許可がありません")

_AVAILABLE: bool | None = None


def available() -> bool:
    """bwrap で読むだけの根を作れるか（1 回だけ試して覚える）。"""
    global _AVAILABLE
    if _AVAILABLE is None:
        exe = shutil.which("bwrap")
        ok = False
        if exe and sys.platform.startswith("linux"):
            try:
                r = subprocess.run([exe, "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "true"],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
                ok = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
        _AVAILABLE = ok
    return _AVAILABLE


def wanted(mode: str) -> bool:
    """この走行の命令を囲うか（設定とモードだけで決める。bwrap が使えるかは available）。"""
    v = (os.environ.get("LAPS_SHELL_SANDBOX") or "auto").strip().lower()
    if v in ("0", "off", "false", "no"):
        return False
    if v in ("1", "on", "true", "yes"):
        return True
    return mode == "development"


def wrap(argv: list[str], workspace: str) -> list[str] | None:
    """囲った argv。作業場が根や家そのものなど、囲う意味が無いときは None（囲わない）。"""
    exe = shutil.which("bwrap")
    ws = os.path.realpath(workspace)
    home = os.path.realpath(os.path.expanduser("~"))
    if not exe or ws in ("/", home) or not os.path.isdir(ws):
        return None
    state = os.path.realpath(str(_lp.home_dir()))
    out = [exe, "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc"]
    # ★順番が意味を持つ（後の bind が上に重なる）。一時置き場と道具の置き場 → 作業場 → 守る物、の順。
    #   作業場が /tmp の中にあるとき、/tmp を後から重ねると .git の守りが消える。
    for p in ("/tmp", "/var/tmp", state, *(os.path.join(home, c) for c in HOME_CACHES)):
        out += ["--bind-try", p, p]
    out += ["--bind", ws, ws]
    for p in PROTECTED:
        out += ["--ro-bind-try", os.path.join(ws, p), os.path.join(ws, p)]
    return out + ["--die-with-parent", "--chdir", ws, "--", *argv]


def denied_hint(code: int | None, text: str) -> str:
    """囲いに拒まれた形の失敗なら、モデルへの一言。違えば空。"""
    if code in (0, None):
        return ""
    low = text[-4000:].lower()
    if not any(w in low for w in DENIAL_WORDS):
        return ""
    return ("This may have been blocked by the sandbox: in development mode, commands can write only inside the "
            "workspace, /tmp and tool caches, and .git is read-only. If it really must write elsewhere (for example a "
            "commit the user asked for, or a system setting), run it again with sandbox=false and a short reason.")


def label(ws: str) -> str:
    return f"sandbox: write only in {Path(ws).name or ws}, /tmp and caches; .git read-only"
