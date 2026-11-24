"""見た目が同じで文字だけ違うパスを、実在する名前に直す（2026-10-05）。

★実機（オーナー機・Windows・0.1.264）で起きたこと:
  作業場のフォルダ名の空白は **全角空白（U+3000）** だった。モデルはそれを
  **半角空白（U+0020）** で書き写し、絶対パスを使った Glob と Bash が 3 回とも
  「パスが見つかりません」で失敗した（直前の依頼は相対パスだったので通っていた）。
  トークナイザは全角空白を半角に寄せるので、**モデルには区別できない**。
  ⇒ 「正しく書き写せ」と言っても直らない。道具の側で吸収する。

★やること: 存在しないパスを、部分ごとに親ディレクトリの実際の名前と照らし、
  **NFKC で正規化して空白を畳んだら一致する名前がちょうど 1 つ**なら、その名前に置き換える。
  NFKC は全角空白→半角空白、全角英数→半角、合成済み文字の揺れをまとめる。
★候補が 0 個・2 個以上なら直さない（意図と違う所へ行くより、失敗のほうがよい）。
★直したら呼び手が必ずそう言う（黙って直さない＝Edit の緩い照合と同じ作法）。
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

#: 1 つのディレクトリで見る名前の上限（巨大なディレクトリで固まらない）。
_SCAN_MAX = 5000


def _key(name: str) -> str:
    s = unicodedata.normalize("NFKC", name)
    s = re.sub(r"\s+", " ", s)
    return s.casefold() if os.name == "nt" else s


def fix_confusable(path: str, *, base: str | None = None) -> str | None:
    """`path` が存在せず、見た目が同じ実在のパスがちょうど 1 つあれば、それを返す。

    戻り値は**直した後のパスの文字列**（相対で渡されたら相対のまま返す）。直せなければ None。
    """
    if not path or os.environ.get("LAPS_PATHFIX", "1") == "0":
        return None
    try:
        p = Path(path)
        root = Path(base) if base else Path(os.getcwd())
        full = p if p.is_absolute() else root / p
        if full.exists():
            return None
        # ★シェル流の逃がし字（空白の前の `\`）を外した形も見る（2026-10-05・test2 で実測）。
        #   モデルは Glob（シェルを通さない）にも `"/root/デスク\　トップ/…"` と書いた。
        #   シェルなら `\` が消えるが、道具の引数では名前の一部として残り「存在しません」になる。
        #   ★Windows では `\` が区切りなので行わない。
        if os.name != "nt" and re.search(r"\\(?=\s)", path):
            unesc = re.sub(r"\\(?=\s)", "", path)
            q = Path(unesc)
            if (q if q.is_absolute() else root / q).exists():
                return unesc
            fixed = fix_confusable(unesc, base=base)
            if fixed:
                return fixed
            path, p = unesc, q
            full = p if p.is_absolute() else root / p
        parts = full.parts
        if not parts:
            return None
        cur = Path(parts[0])
        changed = False
        for part in parts[1:]:
            nxt = cur / part
            if nxt.exists():
                cur = nxt
                continue
            if not cur.is_dir():
                return None
            want = _key(part)
            hits = []
            with os.scandir(cur) as it:
                for i, e in enumerate(it):
                    if i >= _SCAN_MAX:
                        break
                    if _key(e.name) == want:
                        hits.append(e.name)
                        if len(hits) > 1:
                            return None
            if len(hits) != 1:
                return None
            cur = cur / hits[0]
            changed = True
        if not changed or not cur.exists():
            return None
        if p.is_absolute():
            return str(cur)
        try:
            return str(cur.relative_to(root))
        except ValueError:
            return str(cur)
    except (OSError, ValueError):
        return None


def describe_traps(s: str) -> list[str]:
    """`s` に含まれる「見た目では区別できない文字」の説明（重複なし）。"""
    out: list[str] = []
    seen: set[str] = set()
    for ch in s:
        if ch in seen or ch.isascii():
            continue
        if ch.isspace() or unicodedata.normalize("NFKC", ch) != ch:
            seen.add(ch)
            name = "全角空白" if ch == "　" else unicodedata.name(ch, "?")
            out.append(f"{name}（U+{ord(ch):04X}）")
    return out


def note(original: str, fixed: str) -> str:
    """直したことを結果に書く 1 行。"""
    return (f"★パス `{original}` は実在しないため、見た目が同じ実在のパス `{fixed}` を使いました"
            "（空白や英数字の全角/半角が違っていました。以後は相対パスで書くと間違えません）。")


def attach(res, line: str):
    """結果に一言添える。★ToolResult は型（ok/kind/data）を保ったまま前に、
    素の文字列は**後ろ**に足す（素の文字列は先頭の `Error:` で成否を推測されるので、前に足すと隠れる）。"""
    from . import ToolResult
    if isinstance(res, ToolResult):
        return ToolResult(line + "\n" + str(res), ok=res.ok, kind=res.kind,
                          inferred=res.inferred, data=res.data, truncated=res.truncated)
    return str(res) + "\n" + line


def fix_for(path: str, *, mode: str) -> str | None:
    """道具の引数のパスを直す。

    mode="existing": 在るはずのもの（Read / Edit / Grep）＝パス全体を直す。
    mode="parent"  : これから作るもの（Write）＝**親フォルダだけ**直す。
      ★最後の部分を直すと、似た名前の**既存ファイルを上書き**しうる（新しい名前のつもりが既存の
        `a b.txt` に当たる）。作る名前はモデルの書いたとおりにし、置き場所だけ実在に合わせる。
    """
    if not path or Path(path).exists():
        return None
    if mode == "existing":
        f = fix_confusable(path)
        if f:
            return f
    parent = Path(path).parent
    if str(parent) in ("", ".") or parent.exists():
        return None
    fp = fix_confusable(str(parent))
    return str(Path(fp) / Path(path).name) if fp else None


def wrap(fn, *keys: str, mode: str):
    """道具の handler を包み、`inp` のパス（keys の最初に在るもの）を入る前に直す。直したら一言添える。"""
    def run(inp: dict):
        if isinstance(inp, dict):
            for key in keys:
                v = inp.get(key)
                if not (isinstance(v, str) and v):
                    continue
                f = fix_for(v, mode=mode)
                if f:
                    inp = dict(inp)
                    inp[key] = f
                    return attach(fn(inp), note(v, f))
                break
        return fn(inp)
    run.__wrapped__ = fn
    return run
