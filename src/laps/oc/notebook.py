"""解析の帳面 — 作業フォルダの .blackwall/analysis/（2026-10-11）。

オーナー「いまの Blackwall は解析だけして結果はどこにも保存してない？」——その通りだった。道具が読んだ構造は会話の中にしか
無く、要約（文脈の圧縮）で短い要約に置き換わり、新しい会話は何も受け取らない。走行記録（1 つのゲームに 41 走行・22 時間）では、
同じ path_id・同じクラス名・同じファイルの中身を何度も調べ直していた。

道具が読んだ構造（Read のバイナリの構造・find・path_id・モデルの要約・DLL の型）を、対象と読み方ごとに 1 枚の .md に保存する
（同じ対象と読み方は上書き）。INDEX.md に一覧（新しい順）。推論機は呼ばない＝走行の時間を足さない。結果の末尾に保存先を 1 行添える。
オーナーが選んだのはこの形（2026-10-11「この案で」）。分かったことを積む帳面（FINDINGS.md）と、新しい会話・要約の後に帳面を渡す
仕組みは入れていない。

書くのは作業フォルダの .blackwall/analysis/ の下だけ。LAPS_NOTEBOOK=0 で止める。
"""
from __future__ import annotations

from .. import paths as _lp
import hashlib
import os
import re
import time
from pathlib import Path

DIR_NAME = "analysis"


def rel_dir(cwd) -> str:
    """帳面のフォルダの、作業フォルダからの相対の道（`.laps/analysis`・旧い置き場が在れば `.blackwall/analysis`）。"""
    return f"{_lp.workspace_name(cwd)}/{DIR_NAME}"
MAX_FILES = 400                     # analysis/ の上限（古い順に消す）
MAX_FILE_CHARS = 200_000            # 1 枚の上限


def enabled() -> bool:
    if os.environ.get("LAPS_NOTEBOOK", "1").strip() == "0":
        return False
    return os.environ.get("LAPS_TOOLSET", "").strip().lower() != "chat"


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE).strip("._")
    return s[:n] or "x"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def save(cwd: str, target: str, kind: str, args: str, body: str) -> str:
    """道具が読んだ構造を 1 枚に保存して、作業フォルダからの相対パスを返す（保存しなければ ""）。"""
    if not enabled() or not body.strip():
        return ""
    try:
        root = Path(cwd).resolve()
        tgt = Path(target).resolve()
        own = _lp.workspace_dir(root)
        if own in tgt.parents or tgt == own:
            return ""                                  # 帳面そのものは保存しない
        key = hashlib.sha1(f"{tgt}|{kind}|{args}".encode("utf-8", "replace")).hexdigest()[:8]
        name = f"{_slug(tgt.name)}-{_slug(kind, 12)}" + (f"-{_slug(args, 30)}" if args else "") + f"-{key}.md"
        d = own / DIR_NAME
        p = d / name
        try:
            size = os.path.getsize(tgt)
        except OSError:
            size = -1
        b = body if len(body) <= MAX_FILE_CHARS else body[:MAX_FILE_CHARS] + "\n…（長いので先頭だけ）"
        text = (f"# {tgt.name}\n\n- 対象: `{tgt}`\n- 読み方: {kind}" + (f"（{args}）" if args else "")
                + f"\n- 時刻: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" + (f"- 大きさ: {size:,} バイト\n" if size >= 0 else "")
                + "\n```text\n" + b.replace("```", "ˋˋˋ") + "\n```\n")
        _write(p, text)
        _reindex(d)
        return str(p.relative_to(root)).replace("\\", "/")
    except OSError:
        return ""


def _first_line(p: Path) -> str:
    try:
        with open(p, encoding="utf-8") as fh:
            txt = fh.read(4000)
    except OSError:
        return ""
    m = re.search(r"```text\n(.*)", txt, re.S)
    if not m:
        return ""
    got = []
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(("<", "大きさ:", "```")):
            got.append(ln)
        if len(got) == 2:
            break
    return " ".join(got)[:160]


def _reindex(d: Path) -> None:
    files = sorted((f for f in d.glob("*.md") if f.name != "INDEX.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[MAX_FILES:]:
        try:
            old.unlink()
        except OSError:
            pass
    files = files[:MAX_FILES]
    lines = ["# 解析の帳面（Blackwall が自動で保存・新しい順）", "",
             "道具が読んだファイルの構造です。同じファイルを調べ直す前にここを読んでください。", ""]
    for f in files:
        t = time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime))
        lines.append(f"- {t} [{f.name}]({f.name}) — {_first_line(f)}")
    _write(d / "INDEX.md", "\n".join(lines) + "\n")
