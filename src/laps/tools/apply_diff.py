"""ApplyDiff tool — aider-style SEARCH/REPLACE diff format.

Accepts a single `diff` string containing one or more multi-file edit blocks:

    path/to/file.py
    <<<<<<< SEARCH
    old code
    =======
    new code
    >>>>>>> REPLACE

    path/to/other.py
    <<<<<<< SEARCH
    other old
    =======
    other new
    >>>>>>> REPLACE

Each block is applied atomically per-file. Failure on any block reverts that file.
Cross-file: partial success is reported; user can re-emit failed blocks.

Rationale (Phase Ω-M, 2026-05-22): aider's diff format empirically reduces
edit-tool round-trips 3-5x on multi-file refactors. Pairs with architect/editor
split (think-70b plans, omega-j emits blocks).
"""
from __future__ import annotations
from .edit import make_patch, _loose_match, _syntax_error
from .read import decode_bytes
from .atomic import write_text_atomic, write_bytes_atomic
from .textcodec import NotEditable, decode_for_edit, encode_back
from ..policy import authorize_path, PolicyDenied
import os
import re
from pathlib import Path

from . import ToolSpec, ToolResult


_FENCE = re.compile(r"^\s*```+[a-zA-Z0-9_+-]*\s*$")
#: SEARCH / 区切り / REPLACE の目印（**その行がまるごとその形**のときだけ）。
_MARK_SEARCH = re.compile(r"^\s*<{5,}\s*SEARCH\b.*$")
_MARK_SEP = re.compile(r"^={5,}\s*$")
_MARK_REPLACE = re.compile(r"^\s*>{5,}\s*REPLACE\b.*$")


def _clean_path_line(line: str) -> list[str]:
    """パスらしき行から**候補**を並べる（左が生のまま・右へ行くほど削った形）。

    ★なぜ要るか（2026-09-01/02・run44/46/47/48 の実測）: ApplyDiff は
      **14 回中 14 回失敗**していた。そのうち 5 回は、パスの行が

          # /root/vdirwork/run48/bin/build_rx.py

      という**コメント形式**で、道具はこれをファイル名そのものとして受け取り
      `file not found` と答えていた。ファイルは存在する。モデルは同じ diff を
      出し直し、3 回とも同じ答えが返る。

    ★安全側の決め方: 候補は並べるだけで、**選ぶのは「実在するほう」**
      （`_pick_path`）。生のパスが実在するなら決して削った形へ倒さない＝
      この正規化が既存の編集先を変えることは起こりえない。
    """
    s = line.strip()
    out = [s]
    # `**path**` / `` `path` `` / `"path"`
    for a, b in (("**", "**"), ("`", "`"), ('"', '"'), ("'", "'")):
        if len(s) > len(a) + len(b) and s.startswith(a) and s.endswith(b):
            s = s[len(a):-len(b)].strip()
            out.append(s)
    # 末尾の `:`（「site/api.php:」と書く形）
    if s.endswith(":"):
        s = s[:-1].strip()
        out.append(s)
    # 行頭の `#`（コメントとして書く形＝実測でいちばん多い）
    m = re.match(r"^#+\s+(\S.*)$", s)
    if m:
        s = m.group(1).strip()
        out.append(s)
    # 行頭の `--- a/` `+++ b/`（unified diff の見出しを貼り付けた形）
    m = re.match(r"^(?:---|\+\+\+)\s+[ab]/(\S.*)$", s)
    if m:
        out.append(m.group(1).strip())
    # `diff --git a/x b/x`
    m = re.match(r"^diff --git a/(\S+) b/(\S+)$", s)
    if m:
        out.append(m.group(2))
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _pick_path(cands: list[str]) -> str:
    """候補のうち**実在するもの**を優先して選ぶ。無ければ最後（いちばん削った形）。

    ★生のまま実在するなら必ずそれ。正規化が編集先をすり替えることはない。
    """
    for c in cands:
        try:
            if Path(c).exists():
                return c
        except (OSError, ValueError):
            # ValueError = 埋め込み NUL 等。候補の一つで落ちても他を見る。
            continue
    return cands[-1] if cands else ""


def _parse_blocks(diff: str) -> list[dict]:
    """SEARCH/REPLACE の塊を取り出す。**行単位の走査**（旧実装は 1 本の正規表現）。

    ★行単位にした理由（実測 14/14 失敗の内訳）:
      ① パスの行に `# ` などの飾りが付く → `_clean_path_line` で候補にする
      ② **最後の `>>>>>>> REPLACE` が無い**まま出力が終わる（1/14）。
         旧実装は塊が 1 つも取れず「形式が違う」とだけ答えていた。
         終端が無いのは**末尾の塊に限って**入力の終わりを終端とみなす。
      ③ 塊の前後に ``` の囲みが入る
    """
    lines = diff.split("\n")
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        if not _MARK_SEARCH.match(lines[i]):
            i += 1
            continue
        # パスは「SEARCH の直前にある、空でも囲みでもない行」
        j = i - 1
        while j >= 0 and (not lines[j].strip() or _FENCE.match(lines[j])):
            j -= 1
        path_line = lines[j] if j >= 0 else ""
        i += 1
        search: list[str] = []
        while i < len(lines) and not _MARK_SEP.match(lines[i]):
            if _MARK_SEARCH.match(lines[i]):     # 区切りが来ないまま次の塊＝壊れている
                break
            search.append(lines[i])
            i += 1
        if i >= len(lines) or not _MARK_SEP.match(lines[i]):
            break                                 # 区切りが無い＝取れない
        i += 1
        replace: list[str] = []
        closed = False
        while i < len(lines):
            if _MARK_REPLACE.match(lines[i]):
                closed = True
                i += 1
                break
            if _MARK_SEARCH.match(lines[i]):      # 終端の無いまま次の塊が始まった
                break
            replace.append(lines[i])
            i += 1
        if not closed:
            # ★終端が無い。末尾の囲み（```）だけ落として、そのぶんを採る。
            while replace and (not replace[-1].strip() or _FENCE.match(replace[-1])):
                replace.pop()
        cands = _clean_path_line(path_line)
        blocks.append({
            "path": _pick_path(cands),
            "path_candidates": cands,
            "search": "\n".join(search),
            "replace": "\n".join(replace),
            "unterminated": not closed,
        })
    return blocks


#: unified diff（`diff --git` / `@@ -1,2 +3,4 @@`）の見分け
_UNIFIED_HINT = re.compile(r"^(?:diff --git |@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@)", re.M)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _blocks_from_unified(diff: str) -> list[dict]:
    """unified diff を SEARCH/REPLACE の塊に**翻訳**する（適用の実体は共通）。

    ★なぜ受けるか（実測）: run44/46/47 で 3/14 が `diff --git …` 形式だった。
      旧実装は「SEARCH/REPLACE が見つかりません」と書式を並べるだけで、
      モデルは同じ形をもう一度出した。翻訳できるものを断る理由が無い。

    ★新しい適用器は作らない。文脈行(' ')と削除行('-')を SEARCH、
      文脈行と追加行('+')を REPLACE にするだけ＝**照合と書き込みは既存の道と同じ**。
    """
    out: list[dict] = []
    path = ""
    cur: dict | None = None
    for ln in diff.split("\n"):
        m = re.match(r"^diff --git a/(\S+) b/(\S+)$", ln)
        if m:
            path, cur = m.group(2), None
            continue
        m = re.match(r"^\+\+\+ (?:b/)?(\S+)", ln)
        if m and m.group(1) != "/dev/null":
            path, cur = m.group(1), None
            continue
        if ln.startswith("--- "):
            cur = None
            continue
        if ln.startswith("index ") and cur is None:
            continue
        if _HUNK_RE.match(ln):
            cur = {"path": path, "path_candidates": [path], "search": [], "replace": [],
                   "unterminated": False}
            out.append(cur)
            continue
        if cur is None:
            continue
        if ln.startswith("\\"):        # "\ No newline at end of file"
            continue
        if ln.startswith("-"):
            cur["search"].append(ln[1:])
        elif ln.startswith("+"):
            cur["replace"].append(ln[1:])
        elif ln.startswith(" ") or ln == "":
            cur["search"].append(ln[1:] if ln else "")
            cur["replace"].append(ln[1:] if ln else "")
        else:
            cur = None                 # hunk の外へ出た
    blocks = []
    for b in out:
        if not b["path"] or (not b["search"] and not b["replace"]):
            continue
        blocks.append({"path": _pick_path(b["path_candidates"]),
                       "path_candidates": b["path_candidates"],
                       "search": "\n".join(b["search"]),
                       "replace": "\n".join(b["replace"]),
                       "unterminated": False})
    return blocks


def _apply_block(body: str, search: str, replace: str) -> tuple[str, str | None, str]:
    """戻り: (新しい本文, エラー, 緩い照合の説明)。エラーが None なら成功。"""
    if not search:
        # Empty SEARCH = new file or append (aider convention)
        return body + replace, None, ""
    if search not in body:
        # ★照合カスケードを **Edit と共有する**（2026-09-02）。
        #   Edit には 2026-08-27 から入っていて実失敗の 40% を救っているのに、
        #   同じ「書き写しの微差」で落ちる ApplyDiff には**入っていなかった**
        #   ＝この現場が何度も踏んだ「片方で直して片方に残した」。
        #   実測: 直近 4 走行の ApplyDiff 14 回のうち 6 回が
        #   `SEARCH block not found` で、うち 1 回は**字下げが 2 文字違うだけ**。
        loose = _loose_match(body, search)
        if loose is not None:
            start, end, how = loose
            return body[:start] + replace + body[end:], None, how
        return body, f"SEARCH block not found (first 60 chars: {search[:60]!r})", ""
    cnt = body.count(search)
    if cnt > 1:
        return body, f"SEARCH block ambiguous ({cnt} matches); add more context", ""
    return body.replace(search, replace, 1), None, ""


def _run(inp: dict) -> str:
    diff = inp.get("diff", "")
    if not diff:
        return "Error: missing 'diff' (string with SEARCH/REPLACE blocks)"

    blocks = _parse_blocks(diff)
    converted = False
    if not blocks and _UNIFIED_HINT.search(diff):
        # ★unified diff で来たら翻訳して受ける（実測 3/14）。断らない。
        blocks = _blocks_from_unified(diff)
        converted = bool(blocks)
    if not blocks:
        return (
            "Error: no SEARCH/REPLACE blocks found. Expected format:\n"
            "    path/to/file.py\n"
            "    <<<<<<< SEARCH\n"
            "    old code\n"
            "    =======\n"
            "    new code\n"
            "    >>>>>>> REPLACE\n"
            + ("  ★unified diff（diff --git / @@）のようですが、hunk を読み取れませんでした。"
               "上の形で出し直してください。\n" if _UNIFIED_HINT.search(diff) else "")
        )

    # Group blocks by file (so per-file atomic)
    by_file: dict[str, list[dict]] = {}
    for b in blocks:
        by_file.setdefault(b["path"], []).append(b)

    results = []
    notes: list[str] = []
    if converted:
        notes.append("  ★unified diff として読み取り、SEARCH/REPLACE に翻訳して適用しました。")
    success_files = 0
    patches = []          # 画面向け差分（ファイルごと）。Edit と同じ形（edit.make_patch）。
    for path, file_blocks in by_file.items():
        p = Path(path)
        if any(b.get("unterminated") for b in file_blocks):
            notes.append(f"  ★{path}: 最後の `>>>>>>> REPLACE` がありませんでした"
                         "（末尾までを新しい内容とみなしました）。")
        if not p.exists() and any(b["search"] for b in file_blocks):
            cands = [c for b in file_blocks for c in b.get("path_candidates", [])]
            extra = ""
            if len(set(cands)) > 1:
                extra = ("（パスの行から読み取った候補: "
                         + " / ".join(dict.fromkeys(cands)) + " — どれも実在しません）")
            results.append(f"  ✗ {path}: file not found (use empty SEARCH for new files){extra}")
            continue
        # ★符号化を決め打ちしない／改行の流儀を保つ（2026-09-02）。
        #   旧実装は `p.read_text()` で、cp932 のファイルは**例外で丸ごと失敗**し、
        #   CRLF のファイルは LF に正規化されるので、書き戻すと**全行が変わった**
        #   別物になった。どちらも Edit では 8/16 に直っているのに、
        #   こちらに写っていなかった（＝片方で直して片方に残した）。
        crlf = False
        try:
            if p.exists():
                # ★2026-09-08 監査（tools high）: Read は authorize_read で資格情報のパスを拒むのに、
                #   Edit/ApplyDiff は素で読み、不一致時に中身を返していた＝Read の関門の迂回路。同じ関門を通す。
                try:
                    from ..policy import authorize_read as _authorize_read, PolicyDenied as _PolicyDenied
                    _authorize_read(str(p), kind="edit")
                except _PolicyDenied as _e:
                    return ToolResult(f"Error: {_e}", ok=False, kind="error")
                raw = p.read_bytes()
                crlf = raw.count(b"\r\n") > 0 and raw.count(b"\r\n") * 2 >= raw.count(b"\n")
                # ★損なわずに読めたものだけを直し、**読んだのと同じ符号化で書く**（2026-09-20・敵対レビュー）。
                #   旧: `decode_bytes`（cp932 を読める）で読み、**UTF-8 で書き戻していた**＝文字コードが黙って変わる。
                #   しかも候補の最後が latin-1 なので、バイナリも「読めて」しまい、UTF-8 で書かれて壊れた。
                body, _enc, _bom = decode_for_edit(raw)
                crlf = body.count("\r\n") > 0 and body.count("\r\n") * 2 >= body.count("\n")   # ★復号後に数える（UTF-16）
                body = body.replace("\r\n", "\n")
            else:
                body, _enc, _bom = "", "utf-8", b""
        except NotEditable as e:
            results.append(f"  ✗ {path}: {e}")
            continue
        except Exception as e:
            results.append(f"  ✗ {path}: read error {e}")
            continue

        new_body = body
        block_errors = []
        for i, b in enumerate(file_blocks):
            new_body, err, how = _apply_block(new_body, b["search"], b["replace"])
            if err:
                block_errors.append(f"block {i}: {err}")
            elif how:
                notes.append(f"  ★{path} block {i}: SEARCH は完全一致しませんでした（{how}）。"
                             "意図と違えば元に戻してから出し直してください。")

        if block_errors:
            results.append(f"  ✗ {path}: {'; '.join(block_errors)}")
            continue

        # ★構文ゲート（Edit と同じ・2026-09-02）。ApplyDiff にだけ無かった。
        #   実測: 直近 4 走行で Edit の構文ゲートは 41 回発火している（＝この現場の
        #   モデルは壊れた .py/.php を日常的に書く）。同じ内容を ApplyDiff から
        #   書けば**素通り**していた。ゲートは「この編集が壊した」ときだけ働く。
        if os.environ.get("LAPS_EDIT_SYNTAX_GATE", "1") != "0":
            new_err = _syntax_error(str(p), new_body)
            if new_err and _syntax_error(str(p), body) is None:
                results.append(f"  ✗ {path}: この編集は構文を壊します（{new_err}）。"
                               "このファイルは変更していません。")
                continue

        # All blocks for this file applied — write atomically
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                authorize_path(str(p), write=True, kind="edit")
            except PolicyDenied as e:
                # ★2026-08-31（レビュー E5・再現済み）: ここは `return` していた。
                #   2 ファイルの diff で 1 本目を**書き終えた後**に 2 本目が拒否されると、
                #   結果は `Error: policy denied: …` の 1 行だけになり、
                #   **既に書き換えたファイルのことが結果に一言も出ない**（実測）。
                #   モデルは「何も起きなかった」と読んで同じ diff を出し直す。
                #   拒否は「そのファイルの失敗」として記録し、報告を最後まで組み立てる。
                results.append(f"  ✗ {path}: policy denied: {e}")
                continue
            # ★不可分に書く（E4 と同じ理由）。旧 `p.write_text` は書けなかったとき
            #   元のファイルを 0 バイト／途中までの別物にした。説明文の
            #   "Per-file atomic" は**当てはまっていなかった**。
            try:
                _data = encode_back(new_body.replace("\n", "\r\n") if crlf else new_body, _enc, _bom)
            except NotEditable as e:
                results.append(f"  ✗ {path}: {e}")
                continue
            write_bytes_atomic(p, _data)
            results.append(f"  ✓ {path}: {len(file_blocks)} block(s) applied")
            success_files += 1
            patches.append(make_patch(body, new_body, str(path)))
        except Exception as e:
            results.append(f"  ✗ {path}: write error {e}（このファイルは書き換えていません）")

    ok = success_files == len(by_file)
    summary = f"Applied {success_files}/{len(by_file)} file(s)"
    if not ok:
        # ★2026-08-31（レビュー E5・再現済み）: 全滅した diff（`Applied 0/1 file(s)`）は
        #   `Error:` で始まらず patches も空なので、旧実装は**素の str を返し**、
        #   `as_result` の語彙表が **ok=True / kind=ok** と判定していた（実測）。
        #   ＝1 ファイルも当たっていない編集が「成功」として記録され、モデルの読む
        #   文字列にも失敗の印が無い。失敗は失敗として名乗る。
        summary = f"Error: {summary} — 適用できなかったファイルがあります"
        if success_files:
            summary += ("\n  ⚠ 一部のファイルだけが書き換わっています"
                        "（下の ✓ は適用済み・✗ は元のまま）。"
                        "残りを直すときは ✗ の分だけ出し直してください。")
    text = summary + "\n" + "\n".join(results + notes)
    return ToolResult(text, ok=ok, kind="ok" if ok else "error",
                      data={"patches": patches} if patches else None)


SPEC = ToolSpec(
    name="ApplyDiff",
    description=(
        "Apply aider-style SEARCH/REPLACE diff blocks across multiple files in a "
        "single call. Each block uses format:\n"
        "  path/to/file.py\n"
        "  <<<<<<< SEARCH\n"
        "  old code\n"
        "  =======\n"
        "  new code\n"
        "  >>>>>>> REPLACE\n"
        "Empty SEARCH appends to (or creates) the file. SEARCH must match exactly "
        "once or block fails. Per-file atomic."
    ),
    schema={
        "type": "object",
        "properties": {
            "diff": {
                "type": "string",
                "description": "Multi-block SEARCH/REPLACE diff text",
            },
        },
        "required": ["diff"],
    },
    mutates=True,
    handler=_run,
)
