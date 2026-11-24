"""終わる前に、自分が足した行を見直す（2026-10-17・開発の強化 第 1 段）。

オーナー 2026-10-17「開発機能を最大限まで強化したい」。
★なぜ要るか: 走行には「変更の全体をもう一度見る」工程が無い。人が最後に diff を眺めて気づく類い——
  衝突の印の消し忘れ・デバッグの出力の残り・秘密の値・試験を 1 本に絞ったままの印・build 成果物の混入——は、
  試験でも linter でも捕まらない（どれも「正しいコード」として通る）。
★見るのは **git が知っている作業木の、足した行だけ**（`git diff -U0`）。消した行は見ない。
★判定は形だけ。中身の良し悪しは言わない（モデルに決めさせる）。秘密は `redact.py` の判定を借りる。
"""
from __future__ import annotations

import os
import re
import subprocess

#: 置いていくと困る物（形だけで分かる物に限る）。
_CONFLICT = re.compile(r"^(?:<{7}|={7}|>{7})(?:\s|$)")
_DEBUG = re.compile(r"""(?x)
    \bconsole\.(?:log|debug|dir)\s*\(
  | \bdebugger\s*;
  | \bprint\s*\(\s*(?:f?["'](?:TEST|DEBUG|HERE|XXX|aaa|\?\?\?)|["']?\s*\d+\s*["']?\s*\))
  | \bdbg!\s*\(
  | \bfmt\.Print(?:ln|f)?\s*\(\s*"(?:TEST|DEBUG|HERE|XXX)
  | \bDebug\.Log\s*\(
  | \bvar_dump\s*\(
  | \bTODO:\s*remove
""")
_FOCUS = re.compile(r"""(?x)
    \b(?:describe|it|test)\.only\s*\(
  | \bfdescribe\s*\(|\bfit\s*\(
  | @pytest\.mark\.only
  | \bt\.Skip\s*\(\s*["']
""")
#: 版管理に入れない物（足された「ファイル」の場所で見る）。
_JUNK_PATH = re.compile(r"(?:^|/)(?:node_modules|dist|build|target|__pycache__|\.venv|venv|coverage)/|"
                        r"(?:^|/)\.env(?:\.|$)|\.(?:pyc|log|tmp)$")


def _git(args: list[str], cwd: str, timeout: int = 20) -> str:
    """git を読み取りだけ呼ぶ。失敗したら空（走行は止めない）。"""
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    if p.returncode != 0:
        return ""
    return (p.stdout or b"").decode("utf-8", "replace")


def repo_root(path: str) -> str | None:
    d = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    out = _git(["rev-parse", "--show-toplevel"], d).strip()
    return out or None


def _is_secret(line: str) -> bool:
    """足した行に秘密らしい値があるか（redact.py の判定を借りる・無ければ何も言わない）。"""
    try:
        from ..redact import redact
        r = redact(line, fingerprint=False)
    except Exception:
        return False
    return bool(getattr(r, "kinds", None))


def scan_lines(added: dict[str, list[tuple[int, str]]]) -> list[str]:
    """足した行から気づきを作る。added: ファイル → [(行番号, 本文)]。"""
    out: list[str] = []
    for path, rows in added.items():
        if _JUNK_PATH.search(path.replace("\\", "/")):
            out.append(f"{path} — a build/dependency/ignored kind of file was added; it usually does not belong in git")
            continue
        for ln, text in rows:
            s = text.rstrip("\n")
            if _CONFLICT.match(s):
                out.append(f"{path}:{ln} — a merge conflict marker is still in the file: {s.strip()[:40]}")
            elif _DEBUG.search(s):
                out.append(f"{path}:{ln} — debug output left in: {s.strip()[:80]}")
            elif _FOCUS.search(s):
                out.append(f"{path}:{ln} — this runs only one test / skips one: {s.strip()[:80]}")
            elif _is_secret(s):
                out.append(f"{path}:{ln} — this line looks like it contains a credential or key (value not shown)")
    return out


def added_lines(root: str, paths: list[str] | None = None) -> dict[str, list[tuple[int, str]]]:
    """作業木で足された行（追跡していないファイルも含む）。`git diff -U0`（＋ --no-index で未追跡）。"""
    args = ["diff", "-U0", "--no-color", "--diff-filter=ACMR", "HEAD", "--"]
    args += list(paths or [])
    text = _git(args, root, timeout=30)
    if not text:
        text = _git(["diff", "-U0", "--no-color", "--", *(paths or [])], root, timeout=30)
    out: dict[str, list[tuple[int, str]]] = {}
    cur, ln = "", 0
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            cur, ln = line[6:], 0
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            ln = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++") and cur:
            out.setdefault(cur, []).append((ln, line[1:]))
            ln += 1
        elif line.startswith((" ", "-")) and cur and not line.startswith("---"):
            if line.startswith(" "):
                ln += 1
    return out


#: 必ず差し戻す種類（置いていくと壊れる・漏れる）。ほかは知らせるだけ。
_HARD = ("conflict marker", "credential or key")


def review(paths: list[str], cwd: str = "") -> tuple[str, bool]:
    """(気づきの本文, 差し戻すべきか)。git の中でなければ空。"""
    if (os.environ.get("LAPS_DIFFREVIEW") or "1").strip() == "0":
        return "", False
    base = cwd or os.getcwd()
    root = repo_root(paths[0] if paths else base)
    if not root:
        return "", False
    rels = []
    for p in paths or []:
        try:
            r = os.path.relpath(os.path.abspath(p), root)
        except ValueError:
            continue
        if not r.startswith(".."):
            rels.append(r.replace(os.sep, "/"))
    notes = scan_lines(added_lines(root, rels or None))
    if not notes:
        return "", False
    hard = any(k in n for n in notes for k in _HARD)
    head = ("[Harness] Looking at the lines you added, before this is finished:" if hard else
            "[Harness] Note on the lines you added (fix if it was not intended):")
    return head + "\n" + "\n".join(f"- {n}" for n in notes[:12]) + \
        (f"\n- …and {len(notes) - 12} more" if len(notes) > 12 else ""), hard
