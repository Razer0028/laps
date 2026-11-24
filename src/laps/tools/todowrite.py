"""TodoWrite tool: structured progress tracking, exposed as a tool the model can call.

Persists to the active session's .progress.md (markdown checklist format).
Reads LAPS_SESSION_ID env var (set by Agent.run) to find the right file.

Accepted shapes:
- replace whole list: {"todos": [{"content": "...", "status": "pending|in_progress|completed"}, ...]}
  （status は completed/done/finished のどれでも「終わり」と読む）
- toggle one: {"complete": "task text"} or {"start": "task text"}
- toggle many: {"complete": ["task a", "task b"]}   ★まとめて閉じる（手番を節約）
- append: {"add": "new task text"} (or {"add": ["a", "b", "c"]})
"""
from __future__ import annotations
from .. import paths as _lp
import difflib
import os
import re
import unicodedata
from pathlib import Path

from . import ToolSpec, ToolResult
from ..patches import scratchpad as sp_mod


def _session_progress_path() -> Path | None:
    sid = os.environ.get("LAPS_SESSION_ID")
    home = str(_lp.home_dir())
    if not sid:
        return None
    return Path(home) / "sessions" / sid / ".progress.md"


_DONE_WORDS = {"completed", "complete", "done", "finished", "closed", "resolved", "✓", "✔", "[x]"}


def _is_done_status(status) -> bool:
    """★2026-08-23 実測（devbench api-1）: モデルは "done" と書き、旧実装は
    "completed" しか認めなかった → "OK: TODO updated (0/5 done)" → モデルは
    **何も終わっていないと信じて最初からやり直した**（同じ出力の繰り返しの実体の1つ）。
    帳簿が現実と食い違う返事をすると、モデルは帳簿のほうを信じる。"""
    return str(status or "").strip().lower() in _DONE_WORDS


def _maybe_json_list(v) -> list:
    """"[\"a\", \"b\"]" のような JSON 文字列なら list に戻す。そうでなければ [v]。"""
    if isinstance(v, str):
        t = v.strip()
        if t.startswith("[") and t.endswith("]"):
            try:
                import json
                got = json.loads(t)
                if isinstance(got, list):
                    return got
            except Exception:
                pass
        return [v]
    return [v]


def _clean_text(v) -> str:
    """項目の文言を**1行に均す**（2026-09-02・実測した帳簿の破壊）。

    `.progress.md` は `- [ ] <文言>` の1行1項目で、`scratchpad.parse` は行頭で
    区切る。文言に改行が混じると、**その項目だけでなく帳簿全体が壊れる**:

        render([TodoItem("a\nb"), TodoItem(""), TodoItem("ok")])
          → "- [ ] a\nb\n- [ ] \n- [ ] ok\n"
        parse(...)  → [("a"), ("- [ ] ok")]      ← 3 件が 2 件になり、
                                                   3 件目の文言が `- [ ] ok` に化ける

    化けた文言は `complete` で閉じられない（「一覧に無い項目」と返る）ので、
    モデルは終わった作業を永久に閉じられず、帳簿を信じてやり直す——
    この道具が既に 2 度直している「帳簿が現実と食い違う」形そのもの。
    ★空文字も同じ（`parse` の `(.+?)` に一致せず、書いた瞬間に消える）。
    """
    t = str(v if v is not None else "")
    # 改行・復帰・タブは 1 個の空白に潰す（文言は残す。黙って捨てない）
    for ch in ("\r\n", "\r", "\n", "\t"):
        t = t.replace(ch, " ")
    while "  " in t:
        t = t.replace("  ", " ")
    return t.strip()


_SYMBOLS_RE = re.compile(r"[\s_\-.,:;!?'\"()\[\]{}<>/\\|~`@#$%^&*+=、。・「」『』（）【】]+")


def _loose_key(s: str) -> str:
    """`complete` の緩い照合用の鍵（2026-09-04・W4 記帳実測）。

    前後空白・全角半角・記号・大小を吸収する。NFKC で全角/半角を揃え、
    casefold で大小文字を吸収し、空白と記号を落とす（日本語の文字は
    `_SYMBOLS_RE` に含めていないので残る）。
    """
    s = unicodedata.normalize("NFKC", str(s or "")).casefold()
    return _SYMBOLS_RE.sub("", s)


def _find_loose_match(target: str, items: list[sp_mod.TodoItem]) -> sp_mod.TodoItem | None:
    """完全一致で見つからない `target` を、緩い照合で一覧から探す。

    双方向の包含（どちらかがどちらかを含む）で候補を集め、**複数一致は
    最長**（最も具体的な文言）を選ぶ。1件も無ければ None（=呼び出し側が
    「無い」と言う。ここで新規追加しない＝既存方針を維持）。
    """
    key_t = _loose_key(target)
    if not key_t:
        return None
    candidates = []
    for it in items:
        key_i = _loose_key(it.text)
        if not key_i:
            continue
        if key_t == key_i or key_t in key_i or key_i in key_t:
            candidates.append(it)
    if not candidates:
        return None
    candidates.sort(key=lambda it: len(it.text), reverse=True)
    return candidates[0]


def _suggest_candidates(target: str, items: list[sp_mod.TodoItem], limit: int = 3) -> list[str]:
    """完全一致・緩い一致とも失敗したとき、一覧の中から近そうな文言を挙げる。

    ★これは**自動では適用しない**（黙って新規追加しない、の方針は維持）。
    メッセージに載せて、モデルが次の呼び出しで正しい文言を選べるようにする
    だけ——「候補: …」を返して手ぶらの失敗にしない。
    """
    if not items:
        return []
    scored = []
    for it in items:
        ratio = difflib.SequenceMatcher(None, _loose_key(target), _loose_key(it.text)).ratio()
        scored.append((ratio, it.text))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [text for ratio, text in scored[:limit] if ratio > 0]


def _load_items(p: Path) -> list[sp_mod.TodoItem]:
    if not p.exists():
        return []
    # ★**符号化を明示する**（2026-09-02）。`read_text()` / `write_text()` は
    #   その機械の既定で読み書きするので、UTF-8 が既定でない環境
    #   （日本語 Windows の cp932。この製品は Windows 版を出荷している）では
    #   ・日本語の項目を書いた .progress.md が読み直せない（UnicodeDecodeError）
    #   ・cp932 に無い文字（→ ✓ 絵文字など。モデルは実際に使う）を書くと
    #     UnicodeEncodeError で **TodoWrite が丸ごと失敗する**
    #   TodoWrite は実機で 723 回＝3 番目に多い道具で、帳簿が死ぬと
    #   「終わっていないと信じて最初からやり直す」既知の壊れ方に直結する。
    return sp_mod.parse(p.read_text(encoding="utf-8", errors="replace"))


def _save_items(p: Path, items: list[sp_mod.TodoItem]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sp_mod.render(items), encoding="utf-8")


def _run(inp: dict) -> str:
    p = _session_progress_path()
    if p is None:
        return "Error: no active session (LAPS_SESSION_ID unset)"
    items = _load_items(p)
    before = sp_mod.render(items)      # ★何も変わらなかったことを言えるように、先に控える

    todos = inp.get("todos")
    add = inp.get("add")
    complete = inp.get("complete")
    start = inp.get("start")  # marks status (we render only done/not-done; start = no-op marker)

    changed = False
    dropped = 0
    loose_hits: list[tuple[str, str]] = []  # [(渡された文言, 実際に閉じた文言)]
    if todos is not None:
        if not isinstance(todos, list):
            return "Error: 'todos' must be a list of {content, status}"
        new_items = []
        for t in todos:
            if isinstance(t, dict):
                txt = _clean_text(t.get("content") or t.get("text") or "")
                done = _is_done_status(t.get("status"))
            else:
                # ★str 以外（数値など）も文字にして受ける。黙って捨てない
                #   ——旧実装は str でも dict でもない要素を**無言で落として**
                #   「5 件送ったのに 3/4 done」と答えていた。
                txt, done = _clean_text(t), False
            if not txt:
                dropped += 1
                continue
            new_items.append(sp_mod.TodoItem(text=txt, done=done))
        items = new_items
        changed = True

    if add is not None:
        adds = add if isinstance(add, list) else [add]
        for a in adds:
            _txt = _clean_text(a)
            if _txt:
                items.append(sp_mod.TodoItem(text=_txt, done=False))
                changed = True

    if complete:
        # ★複数まとめて閉じられるようにする（2026-08-19 実測）。
        #   複雑な課題の実機3走行で、モデルは {"complete": "1件"} を**7回**呼び、
        #   反復の約40%が予定の記帳に消えていた（作業そのものは10手ほど）。
        #   道具が list を受けないうえ、説明文にも書いていなかった＝
        #   **できると教えていないことは行われない**（今日いちばんの教訓と同じ形）。
        # ★2026-08-23 実測（devbench api-1）: モデルは {"complete": "[\"a\", \"b\"]"} と
        #   **JSON 文字列**で渡すことがある。旧実装はそれを1件の新しい項目として追加し
        #   （"1/6 done"）、帳簿が現実と食い違った。形を読める範囲で読む。
        targets = complete if isinstance(complete, list) else _maybe_json_list(complete)
        missing = []
        for _one in targets:
            if isinstance(_one, dict):
                _one = str(_one.get("content") or _one.get("text") or _one)
            # ★**探すときも同じ均し方をする**（2026-09-02）。
            #   登録側だけ 1 行に均すと、モデルが登録したときと同じ複数行の文言で
            #   閉じにきても一致せず、「一覧に無い項目」と返る＝
            #   **自分が足したものを永久に閉じられない**（実機 08-19 に実際に起きた形）。
            #   直す側と探す側で規則が違うのは、片側だけ直したのと同じこと。
            _one = _clean_text(_one)
            if any(i.text.strip() == _one for i in items):
                items = sp_mod.update_done(items, _one, done=True)
                continue
            # ★2026-09-04（W4 実測）: 実機の close 失敗は全件が完全一致の
            #   すれ違いではなく**一覧が空**（todos/add を一度も呼ばずに complete
            #   だけ呼ぶ）だった。ただし完全一致で見つからず、かつ一覧が空でない
            #   ときのために、前後空白・全角半角・記号・大小・双方向の包含を
            #   吸収した緩い照合を用意しておく（複数一致は最長を選ぶ）。
            loose = _find_loose_match(_one, items)
            if loose is not None:
                items = sp_mod.update_done(items, loose.text, done=True)
                if loose.text != _one:
                    loose_hits.append((_one, loose.text))
                continue
            missing.append(_one)
        changed = True
        if missing:
            # ★無いものを閉じろと言われたら、黙って新規追加しない。無いと言う。
            done = sum(1 for i in items if i.done)
            _save_items(p, items)
            names = "、".join(repr(m[:40]) for m in missing[:5])
            if not items:
                # ★2026-09-04 実測: 記録された close 失敗は**全件この形**
                #   （一覧が 0 件のまま complete だけ呼んでいた）。次にどうすれば
                #   よいかを名指しで教える——「無い」とだけ言っても同じ手を
                #   繰り返す（実機で最大3連続）。
                hint = ("\n一覧が空です。complete の前に todos か add で登録してください"
                        "（例: todos=[{\"content\": \"" + names.split("、")[0].strip("'") +
                        "\", \"status\": \"done\"}]）。")
            else:
                cands = _suggest_candidates(missing[0], items)
                hint = ("\n候補: " + "、".join(repr(c) for c in cands)) if cands else ""
            return ToolResult(
                f"一覧に無い項目: {names}。閉じられるのは一覧にある文言だけです "
                f"（{done}/{len(items)} done）:" + hint + "\n" + sp_mod.render(items),
                ok=False, kind="error")

    if start:
        # We don't track in_progress as a distinct state; treat as no-op acknowledgment.
        # Still ensure the item exists.
        # ★list / JSON 文字列でも落ちない（2026-09-02）。`complete` は 08-23 に
        #   「モデルは list や JSON 文字列を渡してくる」と実測して直したのに、
        #   すぐ下の `start` は `start.strip()` のままで、同じ形が来ると
        #   AttributeError → 呼び出しごと失敗していた（**片方だけ直した**兄弟）。
        for _st in (start if isinstance(start, list) else _maybe_json_list(start)):
            if isinstance(_st, dict):
                _st = _st.get("content") or _st.get("text") or ""
            _st = _clean_text(_st)
            if _st and not any(i.text.strip() == _st for i in items):
                items.append(sp_mod.TodoItem(text=_st, done=False))
                changed = True

    if not changed:
        # Read-only: return current snapshot.
        done = sum(1 for i in items if i.done)
        return f"TODO ({done}/{len(items)} done):\n" + sp_mod.render(items)

    done = sum(1 for i in items if i.done)
    after = sp_mod.render(items)
    _note = ""
    if dropped:
        # 落としたことを言う（黙って減らすと「5 件送ったのに 3 件」になる）
        _note = f"（文言が空の項目 {dropped} 件は登録していません）"
    if loose_hits:
        # ★何を閉じたか正直に言う（黙って別の項目を閉じない）。
        pairs = "、".join(f"{repr(a[:30])}→{repr(b[:30])}" for a, b in loose_hits[:5])
        _note += f"（近い文言で閉じました: {pairs}）"
    if after == before:
        # ★**変化なしを、変化なしと言う。**
        #   2026-08-18 の敵対レビューで実測: 帳簿系の道具は「失敗できない・変化なしと
        #   言えない」ので、モデルが同じ内容を書き続けても毎回 "OK: TODO updated" が返り、
        #   **停滞の燃料そのもの**になっていた（実機で1走行 143 呼び出し中 135 が無仕事・
        #   TodoWrite 28連発で 204 秒）。旧実装は todos が渡されただけで changed=True に
        #   していたため、中身が同一でも「更新した」と答えていた。
        #   道具が正直に言えば、モデルは次の手に移れる（停滞検知に頼る前に止まる）。
        return ToolResult(
            f"変化なし: TODO は既にその内容です（{done}/{len(items)} done）。"
            f"同じ更新を繰り返さないこと。次の作業に進むか、答えを書くこと。",
            ok=True, kind="empty")
    _save_items(p, items)
    return ToolResult(f"OK: TODO updated ({done}/{len(items)} done){_note}",
                      ok=True, kind="ok")


SPEC = ToolSpec(
    name="TodoWrite",
    description=(
        "Manage a structured TODO list for the current session. Pass any of: "
        "todos=[{content,status}] to replace, add=str|list to append, "
        "complete=str|list to mark done — **★終わったものが複数あるなら"
        "1回の呼び出しでまとめて渡すこと**（complete=[\"a\",\"b\"]）。"
        "1件ずつ呼ぶと、その回数だけ手番を使う。 "
        "★complete は登録済みの文言しか閉じられない。まだ何も todos/add で"
        "登録していないなら、complete だけを呼ばず todos=[{content,status:\"done\"}] "
        "で一度に登録して終わらせること。 "
        "start=str to register a new task as active. "
        "Call with no args to read the current list."
    ),
    schema={
        "type": "object",
        "properties": {
            "todos": {"type": "array", "items": {"type": "object"}},
            "add": {"type": ["string", "array"]},
            "complete": {"type": ["string", "array"],
                         "description": "終わった項目。複数なら配列でまとめて渡す"},
            "start": {"type": "string"},
        },
    },
    # ★帳簿の道具（世界を変えず、情報も持ってこない）。
    #   連続で呼ばれたら「まとめられる」と結果の中で伝えるために宣言する。
    bookkeeping=True,
    handler=_run,
)
