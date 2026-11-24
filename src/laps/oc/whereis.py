"""道具が事実を名乗る（2026-10-13 オーナー「改善を続けて」）。

1. 見つからなかった名前を、作業場の別の場所から探して添える。
   PC の走行は、参照先の `resources.resource` を展開済みのフォルダの中で 3 回探して見つけられず、「展開ツールが統合したのだろう」と
   推測して 40 分空回りした。同じ名前のファイルは元のゲームのフォルダ（`7\\RoundAndRoundTrain_Data\\`）にあった。
2. 読んだ・作ったファイルの拡張子と中身が合わないことを添える（oc/mediacheck）。
   名前が音声の名前の `.fsb`（58 本）は、画像のデータから誤った位置で切り出した物で、FSB の印が無いのに、エージェントはその
   バイトの並び（画像の圧縮ブロック）を音声の構造として読み解こうとした。
どちらも推論機は呼ばない（作業場の一覧と頭のバイトだけ）。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

#: 作業場の名前の索引を使い回す秒数。
INDEX_SECS = 60.0
NAMES_MAX = 3
PATHS_MAX = 3

_CACHE: dict[str, Any] = {"cwd": None, "t": 0.0, "index": None}
_TOKEN = re.compile(r"\"([^\"]+)\"|'([^']+)'|(\S+)")


def _index(cwd: str) -> dict[str, list[str]] | None:
    """作業場のファイルとフォルダの名前（小文字）→ 作業場からの場所。worklog.snapshot を使う（多すぎる作業場は None）。"""
    now = time.monotonic()
    if _CACHE["cwd"] == cwd and _CACHE["index"] is not None and now - _CACHE["t"] < INDEX_SECS:
        return _CACHE["index"]
    try:
        from . import worklog
        snap = worklog.snapshot(cwd)
    except Exception:
        snap = None
    if not snap:
        return None
    idx: dict[str, list[str]] = {}
    dirs: set[str] = set()
    for rel in snap:
        idx.setdefault(os.path.basename(rel).lower(), []).append(rel)
        d = os.path.dirname(rel)
        while d and d not in dirs:
            dirs.add(d)
            d = os.path.dirname(d)
    for d in dirs:
        idx.setdefault(os.path.basename(d).lower(), []).append(d + os.sep)
    _CACHE.update(cwd=cwd, t=now, index=idx)
    return idx


def _token_paths(inp: Any, cwd: str) -> set[str]:
    """命令の中に書かれた場所（作業場からの相対・絶対）を正規化した物。指した場所そのものを「別の場所」に数えない。"""
    out: set[str] = set()
    cmd = inp.get("command") if isinstance(inp, dict) else None
    if isinstance(cmd, str):
        for m in _TOKEN.finditer(cmd):
            tok = next((g for g in m.groups() if g), "").strip("\"'")
            if tok and ("/" in tok or "\\" in tok):
                try:
                    out.add(os.path.normcase(os.path.abspath(os.path.join(cwd, tok.replace("\\", os.sep).replace("/", os.sep)))))
                except (OSError, ValueError):
                    pass
    return out


def _candidates(name: str, inp: Any) -> list[str]:
    """呼び出しが指した名前（場所の最後の部分）。"""
    if not isinstance(inp, dict):
        return []
    raw: list[str] = []
    for k in ("file_path", "path", "notebook_path"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            raw.append(v)
    pat = inp.get("pattern")
    if name == "Glob" and isinstance(pat, str) and pat:
        raw.append(pat)
    cmd = inp.get("command")
    if isinstance(cmd, str) and cmd:
        for m in _TOKEN.finditer(cmd):
            tok = next((g for g in m.groups() if g), "")
            if tok and not tok.startswith("-") and ("." in tok or "/" in tok or "\\" in tok):
                raw.append(tok)
            if len(raw) > 30:
                break
    out: list[str] = []
    for r in raw:
        b = re.split(r"[\\/]", r.strip("\"'").rstrip("/\\"))[-1]      # Windows の \\ も区切る（どの OS でも）
        if len(b) < 4 or "*" in b or "?" in b or b.startswith(("$", "%")) or b.lower() in ("dir", "ls", "cd"):
            continue
        if b.lower() not in (x.lower() for x in out):
            out.append(b)
    return out


def elsewhere_note(name: str, inp: Any, cwd: str, *, remote: bool = False) -> str:
    """失敗した（または何も見つからなかった）呼び出しが指した名前が、作業場の別の場所にあれば、その場所を添える一文。"""
    names = _candidates(name, inp)
    if not names:
        return ""
    idx = _index(cwd)
    if not idx:
        return ""
    rows: list[str] = []
    for n in names:
        hits = idx.get(n.lower()) or []
        if not hits:
            continue
        if not remote:
            # 指した場所そのものにあるなら「別の場所」ではない（失敗の理由は他にある・2026-10-13: 命令の中の道も見る）
            pointed = {os.path.normcase(os.path.abspath(os.path.join(cwd, r))) for r in _raw_paths(inp)} | _token_paths(inp, cwd)
            hits = [h for h in hits if os.path.normcase(os.path.abspath(os.path.join(cwd, h.rstrip("/\\")))) not in pointed]
        if hits:
            more = f" (and {len(hits) - PATHS_MAX} more)" if len(hits) > PATHS_MAX else ""
            rows.append(f"`{n}` → " + ", ".join(hits[:PATHS_MAX]) + more)
        if len(rows) >= NAMES_MAX:
            break
    if not rows:
        return ""
    where = "on this machine, in the workspace" if remote else "elsewhere in the workspace"
    return ("\n\nNote: a file or folder with the same name exists " + where + " (paths relative to "
            + cwd + "):\n" + "\n".join(f"- {r}" for r in rows))


def _raw_paths(inp: Any) -> list[str]:
    if not isinstance(inp, dict):
        return []
    return [str(inp[k]) for k in ("file_path", "path", "notebook_path") if isinstance(inp.get(k), str)]


def content_note(name: str, inp: Any, cwd: str) -> str:
    """Read で読んだファイルの拡張子と中身が合わなければ、その一文（画像は Read 自身が言うので除く）。"""
    if name != "Read" or not isinstance(inp, dict):
        return ""
    p = inp.get("file_path") or inp.get("path")
    if not isinstance(p, str) or not p:
        return ""
    try:
        from . import mediacheck
        full = p if os.path.isabs(p) else os.path.join(cwd, p)
        ext = os.path.splitext(full)[1].lower()
        if ext not in mediacheck.CHECK_EXTS or ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
            return ""
        if not os.path.isfile(full):
            return ""
        ok, why = mediacheck.check_file(full)
    except Exception:
        return ""
    if ok is False:
        return (f"\n\nNote: this file's content does not match its extension ({ext}): {why}. "
                "Files made from it, or next to it by the same step, are probably not that format either.")
    return ""


#: 同じフォルダの別名の写しを見分けるときに比べる頭と尻の量。
DUP_SAMPLE = 1 << 20


def _sample(path: str, size: int) -> bytes | None:
    try:
        with open(path, "rb") as f:
            head = f.read(min(DUP_SAMPLE, size))
            if size > 2 * DUP_SAMPLE:
                f.seek(size - DUP_SAMPLE)
                return head + f.read(DUP_SAMPLE)
            return head
    except OSError:
        return None


def duplicate_note(name: str, inp: Any, cwd: str) -> str:
    """読んだ（調べた）ファイルが、同じフォルダの別の名前のファイルと同じ大きさ・同じ頭と尻なら、その写しだと添える。

    ★2026-10-13: 展開フォルダの `sharedassets0.resource` は、エージェント自身が朝に `sharedassets0.assets.resS`（画像の
      データ・426,110,640 バイト）を別名で写した物だった。午後の走行はそれを音声の置き場と思い込み、バイトの並びを 30 分以上
      読み解こうとした。名前の違う同じ中身は、道具が言えば一目で分かる。"""
    if name not in ("Read", "Grep") or not isinstance(inp, dict):
        return ""
    p = inp.get("file_path") or inp.get("path")
    if not isinstance(p, str) or not p:
        return ""
    full = p if os.path.isabs(p) else os.path.join(cwd, p)
    try:
        if not os.path.isfile(full):
            return ""
        size = os.path.getsize(full)
        if size < 4096:
            return ""
        full = os.path.abspath(full)
        me = os.path.normcase(full)
        d = os.path.dirname(full)
        twins = []
        for n in os.listdir(d):
            q = os.path.join(d, n)
            # ★2026-10-13 PC の走行: `D:/TEST/...` と書いた道は join すると `/` と `\` が混ざり、自分自身を写しとして数えていた。
            if os.path.normcase(os.path.abspath(q)) == me or not os.path.isfile(q):
                continue
            try:
                if os.path.getsize(q) == size:
                    twins.append(q)
            except OSError:
                continue
        if not twins:
            return ""
        mine = _sample(full, size)
        same = [os.path.basename(q) for q in twins[:4] if mine is not None and _sample(q, size) == mine]
    except OSError:
        return ""
    if not same:
        return ""
    return (f"Note: {os.path.basename(full)} is a byte-for-byte copy of {', '.join(same)} in the same folder (same size, "
            f"{size:,} bytes, and the same first and last bytes). It is not a separate file: whatever you find in it is the "
            f"content of {same[0]}, so judge it by what {same[0]} is.")


def glob_duplicates_note(name: str, result: str, cwd: str, limit: int = 3) -> str:
    """Glob の結果の中で、同じフォルダの名前違いのファイルが同じ大きさ・同じ頭と尻なら、写しの組として名指しする（2026-10-13）。
    どの作業でも起きる（`app.js` と `app.js.bak`・`data (1).bin`・前の走行が別名で写した物）。小さいファイル（64KB 未満）は見ない。"""
    if name != "Glob" or not result or result.startswith("No files found"):
        return ""
    groups: dict[tuple[str, int], list[str]] = {}
    for ln in result.splitlines()[:400]:
        p = ln.strip()
        if not p or p.startswith(("(", "[", "Note:")):
            continue
        full = p if os.path.isabs(p) else os.path.join(cwd, p)
        try:
            if not os.path.isfile(full):
                continue
            size = os.path.getsize(full)
        except OSError:
            continue
        if size >= 65536:
            groups.setdefault((os.path.normcase(os.path.dirname(os.path.abspath(full))), size), []).append(full)
    rows: list[str] = []
    for (_d, size), files in groups.items():
        if len(files) < 2:
            continue
        samples: dict[bytes, list[str]] = {}
        for f in files[:6]:
            s = _sample(f, size)
            if s is not None:
                samples.setdefault(s, []).append(os.path.basename(f))
        for names in samples.values():
            if len(names) >= 2:
                rows.append(f"{' = '.join(names)} ({size:,} bytes, same first and last bytes)")
        if len(rows) >= limit:
            break
    if not rows:
        return ""
    return ("Note: these files in the list are byte-for-byte copies of each other under different names, so they hold "
            "the same thing:\n" + "\n".join(f"- {r}" for r in rows[:limit]))
