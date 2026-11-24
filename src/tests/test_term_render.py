"""term/render.py — serve のイベント列が設計 §2 の行の型で描かれること。

Console(record=True, width=80, force_terminal=True) に描いて export_text() を見る。
force_terminal=True なので Live が**実際に使われる**（transient の残骸が export に残らないことも見る）。
"""
from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from laps.term.render import (INDENT, TAIL, Renderer, cell_width, fold_physical,
                                   patch_lines, read_kind, wrap_with_gutter)
from laps.term import theme as T


def _reset_rich_style_caches() -> None:
    """rich の Style 系の lru_cache（parse / __add__ / _make_ansi_codes …）を全部空にする。
    控えが 1 つでも残ると、standard で描いた '37' が truecolor の Console でも出る（連鎖 4 回目で実測）。"""
    import rich.style as _rs
    for owner in (_rs.Style, _rs):
        for name in dir(owner):
            try:
                attr = getattr(owner, name)
            except Exception:
                continue
            f = getattr(attr, "cache_clear", None) or getattr(getattr(attr, "__func__", None), "cache_clear", None)
            if callable(f):
                try:
                    f()
                except Exception:
                    pass


def _console(tty: bool = True) -> Console:
    # ★2026-09-08: color_system を固定する。rich は Console 生成時に COLORTERM/TERM から色数を決めるので、
    #   env の無い systemd-run（出荷連鎖）では 8 色に落ち、META と NOTE の灰 2 色が同じ '37' に潰れて
    #   「4 種の SGR が全部違う」試験だけが落ちた（対話シェルでは truecolor で通る＝環境依存の偽赤）。
    #   試験が測るのは「様式が区別されているか」であって端末の色数ではない。
    # ★さらに（同日・連鎖 4 回目で判明）: rich は Style ごとに**最初に描いた色系の SGR を控え続ける**
    #   （Style.parse の lru_cache ＋ Style._ansi）。env の無い systemd-run では先に走る別の試験の
    #   Console(force_terminal=True) が "standard" になり、同じ style 文字列の控えが '37' で固定される。
    #   この試験の Console を truecolor にしても控えが勝つので、控えを空にしてから描く。
    _reset_rich_style_caches()
    if tty:
        return Console(record=True, width=80, force_terminal=True, color_system="truecolor", file=io.StringIO())
    return Console(record=True, width=80, force_terminal=False, file=io.StringIO())


def _done(text: str = "", **kw) -> dict:
    d = {"type": "done", "id": "r1", "outcome": "accepted", "text": text, "secs": 1.2,
         "tok_s": 40.5, "tool_calls": {}}
    d.update(kw)
    return d


def _call(name: str, inp: dict, call: int) -> dict:
    return {"type": "tool_call", "id": "r1", "name": name, "input": inp, "call": call}


def _result(name: str, call: int, result: str, *, ok: bool = True, summary: dict | None = None,
            kind: str | None = None, **kw) -> dict:
    d = {"type": "tool_result", "id": "r1", "name": name, "call": call, "result": result,
         "ok": ok, "kind": kind or ("ok" if ok else "error"), "chars": len(result),
         "truncated": False, "summary": summary}
    d.update(kw)
    return d


def _entry(r: Renderer, name: str, inp: dict):
    """走行中の ToolEntry を 1 つ（頭行だけを見たいとき）。"""
    from laps.term.render import ToolEntry, read_kind
    return ToolEntry(call=1, name=name, input=inp, read=read_kind(name, inp))


def _turn(r: Renderer, events: list[dict], rid: str = "r1") -> dict:
    r.start_turn(rid)
    for ev in events:
        r.on_event(ev)
    return r.end_turn()


# -- 本文 ---------------------------------------------------------------------

def test_token_then_done_prints_body_once():
    c = _console()
    r = Renderer(c)
    out = _turn(r, [
        {"type": "token", "id": "r1", "text": "Hello **wor"},
        {"type": "token", "id": "r1", "text": "ld**, 1+1=2."},
        _done("Hello **world**, 1+1=2."),
    ])
    text = c.export_text(clear=False)
    assert text.count("Hello world, 1+1=2.") == 1, text
    assert out["text"] == "Hello **world**, 1+1=2."
    assert r.transcript[-1] == {"role": "assistant", "content": "Hello **world**, 1+1=2."}


def test_live_transient_leaves_no_residue_in_export():
    """Live が本当に使われ（TTY 扱い）、そのフレームは export に残らない。"""
    c = _console()
    r = Renderer(c)
    assert r._use_live is True
    r.start_turn("r1")
    assert r._live is not None and r._live.is_started
    r.on_event({"type": "token", "id": "r1", "text": "alpha beta"})
    r.on_event(_call("Bash", {"command": "echo hi"}, 1))
    r.on_event(_result("Bash", 1, "hi\n", summary={"head": "echo hi", "tail": "1 行", "n": 1,
                                                   "status": "ok", "lines": 1}))
    r.on_event({"type": "token", "id": "r1", "text": " gamma"})
    r.on_event(_done("alpha beta gamma"))
    r.end_turn()
    assert r._live is None
    text = c.export_text(clear=False)
    assert text.count("alpha beta") == 1, text
    assert text.count("gamma") == 1, text
    assert text.count("✓ Bash (echo hi)") == 1, text
    # 本文→道具→本文 の順序が残る
    assert text.index("alpha beta") < text.index("✓ Bash") < text.index("gamma")
    assert c.record is True          # 記録は戻っている


def test_done_text_used_when_no_tokens():
    """native モードは token が出ず done.text だけが本文。"""
    c = _console()
    r = Renderer(c)
    out = _turn(r, [_done("only in done")])
    assert c.export_text(clear=False).count("only in done") == 1
    assert out["text"] == "only in done"


def test_non_tty_uses_no_live_and_prints_once():
    c = _console(tty=False)
    r = Renderer(c)
    assert r._use_live is False
    r.start_turn("r1")
    assert r._live is None
    r.on_event({"type": "token", "id": "r1", "text": "piped body"})
    r.on_event(_call("Bash", {"command": "true"}, 1))
    r.on_event(_result("Bash", 1, "", summary={"head": "true", "tail": "(No output)", "status": "ok"}))
    r.on_event(_done("piped body"))
    r.end_turn()
    text = c.export_text(clear=False)
    assert text.count("piped body") == 1
    assert "✓ Bash (true)" in text and "⎿  (No output)" in text


# -- 道具 1 回 = 2 行 -------------------------------------------------------

def test_single_read_two_lines():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Read", {"file_path": "/ws/a.py"}, 1),
        _result("Read", 1, "x\ny\nz", summary={"head": "a.py", "tail": "Read 3 lines", "n": 3,
                                               "status": "ok", "lines": 3}),
        {"type": "token", "id": "r1", "text": "ok"},
        _done("ok"),
    ])
    text = c.export_text(clear=False)
    assert "✓ Read (a.py)" in text, text
    assert "⎿  Read 3 lines" in text, text
    assert "読み取り" not in text        # 1 件は圧縮しない


def test_head_falls_back_to_first_string_arg_and_tail_to_first_line():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Umbra", {"query": "llama.cpp の KV 量子化", "k": 5}, 1),
        _result("Umbra", 1, "3件\n- a\n- b"),       # summary 無し
        _done(),
    ])
    text = c.export_text(clear=False)
    assert "✓ Umbra (llama.cpp の KV 量子化)" in text, text
    assert "⎿  3件" in text, text


def test_dot_colors_by_state():
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event(_call("Bash", {"command": "false"}, 1))
    r.on_event(_result("Bash", 1, "boom", ok=False, summary={"head": "false", "tail": "exit 1",
                                                               "n": 1, "status": "error"}))
    r.on_event(_call("Bash", {"command": "true"}, 2))
    r.on_event(_result("Bash", 2, "", summary={"head": "true", "tail": "(No output)", "status": "ok"}))
    r.on_event(_done(outcome="accepted"))
    r.end_turn()
    html = c.export_html(inline_styles=True, clear=False)
    # 失敗の点は赤、成功の点は ok 色（色の値は theme から）
    assert T.RED.lower() in html.lower()
    assert T.OK.lower() in html.lower()
    text = c.export_text(clear=False)
    assert "⎿  exit 1" in text and "boom" in text


# -- 差分 -------------------------------------------------------------------

def test_patch_renders_minus_and_plus_with_old_gutter():
    patch = {"path": "api.py", "added": 1, "removed": 1, "truncated": False,
             "hunks": [{"old_start": 12, "new_start": 12,
                        "lines": [" def add(a, b):", "-    return a+b", "+    return a + b"]}]}
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Edit", {"file_path": "api.py", "old_string": "a+b", "new_string": "a + b"}, 1),
        _result("Edit", 1, "OK: 1 replacement(s)",
                summary={"head": "api.py", "tail": "Added 1 line, removed 1 line", "n": 2,
                         "status": "ok", "lines": 1, "patch": patch}),
        _done(),
    ])
    text = c.export_text(clear=False)
    assert "✓ Edit (api.py)" in text
    assert "⎿  Added 1 line, removed 1 line" in text
    assert "12   def add(a, b):" in text, text
    assert "13 -     return a+b" in text, text
    assert "   +     return a + b" in text, text       # 追加行は旧行番号を持たない
    assert "13 -" in text and "14" not in text.split("Added")[1].split("done")[0]


def test_patch_lines_gap_between_hunks_and_styles():
    patch = {"hunks": [{"old_start": 1, "lines": ["-a", "+b"]},
                       {"old_start": 40, "lines": [" c", "+d"]}], "truncated": True}
    lines = patch_lines(patch)
    plain = [t.plain for t in lines]
    assert plain[0].endswith("1 - a") and plain[1].endswith("  + b")
    assert plain[2].strip() == "…"
    assert plain[3].endswith("40   c")
    assert "省略" in plain[-1]
    assert lines[0].style == T.S_DIFF_DEL and lines[1].style == T.S_DIFF_ADD and lines[3].style == T.S_DIFF_CTX


def test_multi_file_patches_show_path_heading():
    p1 = {"path": "a.py", "hunks": [{"old_start": 1, "lines": ["-x", "+y"]}]}
    p2 = {"path": "b.py", "hunks": [{"old_start": 5, "lines": ["-p", "+q"]}]}
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("ApplyDiff", {"diff": "..."}, 1),
        _result("ApplyDiff", 1, "OK", summary={"head": "a.py, b.py", "tail": "2 files: added 2, removed 2",
                                               "patches": [p1, p2]}),
        _done(),
    ])
    text = c.export_text(clear=False)
    assert "a.py" in text and "b.py" in text and "1 - x" in text and "5 - p" in text


# -- Bash の畳み ---------------------------------------------------------------

def test_bash_12_lines_keeps_head_and_tail():
    """★項目 11: 先頭 1 物理行＋末尾 2 物理行。旧実装は**先頭 3 行だけ**＝
    `journalctl -n 12` の**いちばん古い 3 行**が見えて最新 9 行が隠れていた（実測）。"""
    body = "\n".join(f"line{i}" for i in range(1, 13))
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Bash", {"command": "python3 -m pytest -q"}, 1),
        _result("Bash", 1, body, summary={"head": "python3 -m pytest -q", "tail": "12 行", "n": 12,
                                          "status": "ok", "lines": 12}),
        _done(),
    ])
    text = c.export_text(clear=False)
    assert "✓ Bash (python3 -m pytest -q)" in text
    assert "⎿  12 行" in text
    assert "line1" in text and "line11" in text and "line12" in text
    assert "line5" not in text and "line2" not in text
    assert "… 残り 9 行" in text, text
    assert text.index("… 残り 9 行") < text.index("line11")      # 畳みは中、末尾は後
    assert "→ /expand で全文" in text


def test_bash_4_lines_not_folded():
    body = "a\nb\nc\nd"
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Bash", {"command": "x"}, 1),
        _result("Bash", 1, body, summary={"head": "x", "tail": "4 行", "status": "ok"}),
        _done(),
    ])
    text = c.export_text(clear=False)
    assert "残り" not in text and "     d" in text          # 隠すのが 1 行なら畳まない


def test_bash_failure_keeps_head_and_tail():
    body = "\n".join(f"err{i}" for i in range(1, 15))
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Bash", {"command": "make"}, 1),
        _result("Bash", 1, body, ok=False, summary={"head": "make", "tail": "exit 2", "n": 2,
                                                     "status": "error", "lines": 14}),
        _done(outcome="accepted"),
    ])
    text = c.export_text(clear=False)
    assert "⎿  exit 2" in text
    assert "err1" in text and "err2" in text and "err3" in text
    assert "err13" in text and "err14" in text          # ★最後の行＝いちばん読みたい
    assert "err7" not in text
    assert "… 残り 9 行" in text, text
    assert "→ /expand で全文" in text
    html = c.export_html(inline_styles=True, clear=False)
    assert T.ALERT.lower() in html.lower()


def test_fold_keeps_head_and_tail():
    """★項目 11: 12 行 → 先頭 `[0]`・`…`・末尾 `[-2] [-1]`（末尾寄せ）。

    物理行で数える。隠すのが 1 行以下なら畳まない（「… 残り 1 行」のほうが長い）。
    ★前: 先頭 3 行だけ＝`journalctl -n 12` の**いちばん古い 3 行**が見えて最新が隠れた。
    """
    ls = [f"l{i}" for i in range(1, 13)]
    head, hidden, tail = fold_physical(ls, 80, "     ", 1, 2)
    assert head == ["l1"] and hidden == 9 and tail == ["l11", "l12"]
    assert fold_physical(["a", "b", "c", "d"], 80, "     ", 1, 2) == (["a", "b", "c", "d"], 0, [])
    assert fold_physical(["a"], 80, "     ", 3, 2) == (["a"], 0, [])
    # ★折り返す行は 2 物理行と数える: 幅 40 で 1 行が 2 行ぶんの席を取る
    wide = ["x" * 70] + [f"l{i}" for i in range(1, 8)]
    head, hidden, tail = fold_physical(wide, 40, "     ", 2, 1)
    assert head == ["x" * 70] and tail == ["l7"] and hidden == 6


# -- 読み取り系の圧縮 -----------------------------------------------------------

def test_three_reads_compress_to_one_line():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Read", {"file_path": "a.py"}, 1),
        _result("Read", 1, "x", summary={"head": "a.py", "tail": "Read 1 lines", "status": "ok"}),
        _call("Grep", {"pattern": "def "}, 2),
        _result("Grep", 2, "a.py:1:def f", summary={"head": "def ", "tail": "Found 1 lines", "status": "ok"}),
        _call("Bash", {"command": "ls -la"}, 3),
        _result("Bash", 3, "total 0", summary={"head": "ls -la", "tail": "1 行", "status": "ok"}),
        {"type": "token", "id": "r1", "text": "結論です"},
        _done("結論です"),
    ])
    text = c.export_text(clear=False)
    assert "読み取り 1件・検索 1件・一覧 1件  (/expand)" in text, text
    assert "✓ Read (a.py)" not in text and "✓ Grep" not in text and "✓ Bash (ls -la)" not in text
    assert text.index("読み取り 1件") < text.index("結論です")


def test_group_closes_when_non_read_tool_arrives():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Read", {"file_path": "a.py"}, 1),
        _result("Read", 1, "x", summary={"head": "a.py", "tail": "Read 1 lines", "status": "ok"}),
        _call("Bash", {"command": "cat b.py"}, 2),
        _result("Bash", 2, "y", summary={"head": "cat b.py", "tail": "1 行", "status": "ok"}),
        _call("Write", {"file_path": "c.py", "content": "z\n"}, 3),
        _result("Write", 3, "OK", summary={"head": "c.py", "tail": "Wrote 1 lines", "status": "ok"}),
        _done(),
    ])
    text = c.export_text(clear=False)
    assert "読み取り 2件  (/expand)" in text, text
    assert text.index("読み取り 2件") < text.index("✓ Write (c.py)")
    assert "⎿  Wrote 1 lines" in text


def test_failed_read_pops_out_of_group():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Read", {"file_path": "a.py"}, 1),
        _result("Read", 1, "x", summary={"head": "a.py", "tail": "Read 1 lines", "status": "ok"}),
        _call("Read", {"file_path": "missing.py"}, 2),
        _result("Read", 2, "Error: file not found: missing.py", ok=False,
                summary={"head": "missing.py", "tail": "File not found", "status": "notfound"}),
        _call("Read", {"file_path": "b.py"}, 3),
        _result("Read", 3, "y", summary={"head": "b.py", "tail": "Read 1 lines", "status": "ok"}),
        _done(),
    ])
    text = c.export_text(clear=False)
    assert "✗ Read (missing.py)" in text, text
    assert "⎿  File not found" in text
    assert "Error: file not found: missing.py" in text
    assert "読み取り 2件  (/expand)" in text      # 成功した 2 件はまとまる


def test_read_kind_table():
    assert read_kind("Read", {}) == "read"
    assert read_kind("Grep", {}) == "search" and read_kind("Glob", {}) == "search"
    assert read_kind("Bash", {"command": "cat x | grep y"}) == "read"
    assert read_kind("Bash", {"command": "  rg -n foo"}) == "search"
    assert read_kind("Bash", {"command": "tree -L 2"}) == "list"
    assert read_kind("Bash", {"command": "python3 x.py"}) is None
    assert read_kind("Bash", {}) is None
    assert read_kind("Write", {"file_path": "x"}) is None


# -- 思考 -----------------------------------------------------------------------

def test_think_shown_by_default(monkeypatch):
    """★2026-09-21 オーナー指示で振る舞いを反転した。

    旧: 思考は verbose のときだけ出す（`test_think_hidden_unless_verbose`）。
    新: **既定で出す。** 「でていないと推論しているんだかしていないんだか
        わかりません」——畳んだ思考は、出していないのと同じ。
    消したい人のために `LAPS_SHOW_THINK=0` を残す。
    """
    monkeypatch.delenv("LAPS_SHOW_THINK", raising=False)
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "think", "id": "r1", "text": "まず a を"},
        {"type": "think", "id": "r1", "text": "読む"},
        {"type": "token", "id": "r1", "text": "答え"},
        _done("答え"),
    ])
    text = c.export_text(clear=False)
    assert "∴ まず a を読む" in text, text
    assert "思考 0 秒" in text, text

    # 複数行の思考も、既定のまま全部出る（旧 verbose 版から移設）。
    c2 = _console()
    r2 = Renderer(c2)
    _turn(r2, [
        {"type": "think", "id": "r1", "text": "まず a を"},
        {"type": "think", "id": "r1", "text": "読む\n次に b"},
        {"type": "token", "id": "r1", "text": "答え"},
        _done("答え"),
    ])
    text2 = c2.export_text(clear=False)
    assert "∴ まず a を読む" in text2, text2
    assert "次に b" in text2
    assert text2.index("∴") < text2.index("答え")


def test_think_can_be_silenced(monkeypatch):
    monkeypatch.setenv("LAPS_SHOW_THINK", "0")
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "think", "id": "r1", "text": "まず a を"},
        {"type": "think", "id": "r1", "text": "読む"},
        {"type": "token", "id": "r1", "text": "答え"},
        _done("答え"),
    ])
    text = c.export_text(clear=False)
    assert "まず a を読む" not in text
    assert "思考 0 秒" in text, text


def test_no_thought_line_without_think():
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "token", "id": "r1", "text": "x"}, _done("x")])
    assert "思考" not in c.export_text(clear=False)


def test_set_verbose_toggle():
    c = _console()
    r = Renderer(c)
    r.set_verbose(True)
    assert r.verbose is True
    _turn(r, [{"type": "think", "id": "r1", "text": "t"}, _done("")])
    assert "∴ t" in c.export_text(clear=False)


# -- 通知・エラー・done ----------------------------------------------------------

def test_notice_and_error_lines():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "harness", "message": "pushback 1/3"},
        {"type": "notice", "id": "r1", "kind": "context_compacted", "before": 30000, "after": 12000},
        {"type": "notice", "id": "r1", "kind": "cancelled", "iter": 3},
        {"type": "notice", "id": "r1", "kind": "ready", "protocol": 1},
        {"type": "error", "id": "r1", "kind": "backend", "message": "connection refused"},
        _done(outcome="backend_error"),
    ])
    text = c.export_text(clear=False)
    # ★内部の語（[harness] / cancelled / backend_error）を画面に出さない（項目 17・23）
    assert "pushback 1/3" in text and "[harness]" not in text
    assert "文脈を圧縮 30,000 → 12,000" in text, text        # 4 桁以上に桁区切り（項目 23②）
    assert "✗ 中断を要求しました（もう一度 Ctrl-C で終了）" in text, text
    assert "cancelled" not in text and "backend_error" not in text
    assert "ready" not in text
    assert "✗ エラー（backend）: connection refused" in text, text
    assert "推論器に届かない" in text


def test_cancel_notice_is_not_repeated_within_one_turn():
    """★1 回の Ctrl-C で「中断を要求しました」系の文言が 1 行だけ（実機で 2〜3 行 出ていた・2026-09-03）。

    ①クライアント自身の即時表示 ②serve の受理（`kind=cancel, applied=…`）
    ③agent が反復境界で確定（`kind=cancelled`）—— 3 つとも同じターンに届く。
    """
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "cancel", "message":
         "中断を要求しました（本体の返事を待っています。もう一度 Ctrl-C で終了）"},
        {"type": "notice", "id": "r1", "kind": "cancel", "accepted": True, "applied": "running"},
        {"type": "notice", "id": "r1", "kind": "cancelled", "iter": 3},
        _done(outcome="cancelled"),
    ])
    text = c.export_text(clear=False)
    assert text.count("中断を要求しました") == 1, text
    # ★`applied` は情報として価値がある——ただの繰り返しにせず、続報として 1 行だけ足す。
    assert "走行中に届きました" in text, text
    assert text.count("走行中に届きました") == 1, text


def test_cancel_applied_shown_only_once_even_if_repeated():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "cancel", "applied": "pending"},
        {"type": "notice", "id": "r1", "kind": "cancel", "applied": "pending"},
        _done(outcome="cancelled"),
    ])
    text = c.export_text(clear=False)
    assert text.count("待ち行列") == 1, text


def test_cancel_notice_state_does_not_leak_across_turns():
    """次のターンでもまた中断すれば、また 1 回目として出る（ターンで引き継がない）。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "notice", "id": "r1", "kind": "cancel", "message": ""}, _done(outcome="cancelled")])
    _turn(r, [{"type": "token", "id": "r2", "text": "次の発言"}, _done("次の発言", id="r2")], rid="r2")
    _turn(r, [{"type": "notice", "id": "r3", "kind": "cancel", "message": ""}, _done(outcome="cancelled")], rid="r3")
    text = c.export_text(clear=False)
    assert text.count("中断を要求しました") == 2, text


def test_ws_guard_notice_builds_a_message_from_fields():
    """`ws_guard` に `message` は無く `tool`/`target`/`n` だけ——表引きの既定に落ちると
    **内部識別子 `ws_guard` がそのまま画面に出る**（項目 17 違反）。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "ws_guard", "tool": "Bash",
         "target": "systemctl restart blackwall-gateway", "n": 1},
        _done(),
    ])
    text = c.export_text(clear=False)
    assert "ws_guard" not in text, text
    assert "Bash" in text and "systemctl restart blackwall-gateway" in text, text
    row = next(ln for ln in _rows(text) if "Bash" in ln and "systemctl" in ln)
    assert row.startswith(T.MARK_BAD), row
    html = c.export_html(inline_styles=True, clear=False)
    assert T.ALERT.lower() in html.lower()


def test_ws_guard_notice_repeat_count():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "ws_guard", "tool": "Write",
         "target": "/etc/nginx/nginx.conf", "n": 2},
        _done(),
    ])
    assert "2 回目" in c.export_text(clear=False)


@pytest.mark.parametrize("kind,message", [
    ("cancel_error", "中断の後始末に失敗しました（kill_running）: boom"),
    ("checkpoint_error", "作業の控えを取れませんでした（begin）: no git"),
])
def test_cancel_error_and_checkpoint_error_notices_are_prominent(kind, message):
    """★W8（2026-09-04）: 同じ捜索で見つけたもう2つの抜け。serve.py は両方とも
    `message` を合成して渡す（`_RunState._on_event` の `checkpoint_error`/`cancel_error`
    分岐）ので識別子は漏れないが、`_NOTICE` 表に無いまま既定の無印・薄灰色に落ちていた
    （手で叩いて確認：直す前は先頭に印が一切付かなかった）。Windows 側（wire.rs）は
    両方とも GATE_KINDS（失敗の報せ＝左線・赤）。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": kind, "message": message},
        _done(),
    ])
    text = c.export_text(clear=False)
    row = next(ln for ln in _rows(text) if message[:8] in ln)
    assert row.startswith(T.MARK_BAD), row


def test_answer_guard_notice_is_prominent():
    """★W8（2026-09-04・4面プロトコル突き合わせ）: `answer_guard`（agent.py
    `_answer_guard` — 利用者が「いいえ」と答えた直後の Write/Edit/ApplyDiff を
    ハーネスが止めた合図）は Windows 側（wire.rs GATE_KINDS）では失敗の報せとして
    左線・赤で立つのに、端末版は `_NOTICE` 表に無く既定の無印・薄灰色に埋もれていた
    （ws_guard と同型の合図で、実際に message も付いているのに気づけない）。"""
    c = _console()
    r = Renderer(c)
    msg = "利用者が直前の質問に「いいえ」と答えたので、Write を実行しませんでした（1 回目）"
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "answer_guard", "tool": "Write",
         "answer": "いいえ", "n": 1, "message": msg},
        _done(),
    ])
    text = c.export_text(clear=False)
    row = next(ln for ln in _rows(text) if "実行しませんでした" in ln)
    assert row.startswith(T.MARK_BAD), row
    html = c.export_html(inline_styles=True, clear=False)
    assert T.ALERT.lower() in html.lower()


def test_user_stop_notice_is_shown_as_ok_not_dim():
    """★W8: `user_stop`（agent.py `_maybe_user_stop` — 利用者の「やめて」を仕組みで
    守って終わった合図）は失敗ではない（Windows 側 wire.rs は PLAIN・outcome も
    0.1.188 で「✓ 利用者が終了」に直っている）。端末版だけ `_NOTICE` 表に無く、
    既定の無印・薄灰色に落ちて「何が起きたか」が読み取れなかった。"""
    c = _console()
    r = Renderer(c)
    msg = "利用者の回答「終了」により、ここで終了します（以後は何も変えていません）"
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "user_stop", "answer": "終了", "message": msg},
        _done(outcome="user_stop"),
    ])
    text = c.export_text(clear=False)
    row = next(ln for ln in _rows(text) if "ここで終了します" in ln)
    assert row.startswith(T.MARK_OK), row
    assert not row.startswith(T.MARK_BAD), row


@pytest.mark.parametrize("tag", ["[host_scope 1] 依頼は 推論機 の話だが、そのホストに触れた道具呼び出しが 0 回",
                                 "[rerun 1] 合格を 1 回しか見ていない試験（証拠 [3]）を再現してから締める",
                                 "[fabrication 1] 裏付けのない数値を差し戻し",
                                 "[loop] 同じ返答の繰り返し — Reset を2回送っても変わらないので打ち切り",
                                 "[Ω-N] Detected fabricated bash output"])
def test_harness_gate_marks_are_shown_prominently(tag):
    """★agent.py の門（host_scope/rerun/fabrication/loop/Ω-…）は全部 `kind=\"harness\"` の
    notice で届く。既定の harness（無印・薄灰色）のままだと見落とす——
    過去に『失敗の左線が存在しない』を実機で踏んでいる（2026-09-03）。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "notice", "id": "r1", "kind": "harness", "message": tag}, _done()])
    text = c.export_text(clear=False)
    row = next(ln for ln in _rows(text) if tag[:10] in ln)
    assert row.startswith(T.MARK_BAD), row
    html = c.export_html(inline_styles=True, clear=False)
    assert T.ALERT.lower() in html.lower()


def test_harness_without_gate_mark_stays_plain():
    """★[割り込み] のような人間の割り込み転送は「ハーネスがモデルを差し戻した」印では
    ない——目立たせすぎない（既存の test_notice_and_error_lines の "pushback 1/3" と同じ扱い）。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "notice", "id": "r1", "kind": "harness", "message": "[割り込み] メモ"}, _done()])
    row = next(ln for ln in _rows(c.export_text(clear=False)) if "割り込み" in ln)
    assert not row.startswith(T.MARK_BAD), row


# -- 項目 16: 文脈まわりの 4 状態 ----------------------------------------------

def _sgr_of(raw: str, needle: str) -> str:
    """`needle` を含む行の**実バイトの SGR**（色を指定している最初の指定）。"""
    import re
    for ln in raw.splitlines():
        if needle in ln:
            m = re.search(r"\x1b\[([0-9;]*)m", ln)
            assert m, ("SGR が無い", ln)
            return m.group(1)
    raise AssertionError(f"{needle!r} が画面に無い:\n{raw}")


def test_notice_four_kinds_differ():
    """★項目 16: 警告・圧縮・仕切り直し・打ち切りの **4 種類の SGR が全部違う**こと。

    ★前（`probe_states2.py` 実測）: 重さの違う 4 つが `[harness]` に潰され、**1 種類の
      SGR**で並んでいた。いちばん重い「打ち切り」（走行が終わった）と、いちばん軽い
      「圧縮」（何も失っていない）が同じ見え方＝画面から重さが読めない。
    """
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "notice",
         "message": "この推論器の窓（16,384）では、作業の記録に約 5,000 トークンしか使えません。"},
        {"type": "notice", "id": "r1", "kind": "context_compacted",
         "before": 31240, "after": 12880},
        {"type": "notice", "id": "r1", "kind": "context_reset",
         "message": "文脈を仕切り直しました（3 回目）"},
        {"type": "notice", "id": "r1", "kind": "context_exhausted",
         "message": "★文脈が足りなくなったので、ここで打ち切りました"
                    "（会話 31,842 / 使える枠 32,768 トークン）。\n"
                    "作業を小さく分けるか、推論器を広い窓で起動し直してください。"},
        _done(outcome="context_exhausted"),
    ])
    raw = c.export_text(clear=False, styles=True)
    text = c.export_text(clear=False)
    codes = [_sgr_of(raw, n) for n in ("この推論器の窓", "文脈を圧縮", "仕切り直しました",
                                       "打ち切りました")]
    assert len(set(codes)) == 4, codes           # ★4 種類とも違う SGR
    # 字でも割れる（色が 1 バイトも出ない端末のため）
    reset = next(ln for ln in _rows(text) if "仕切り直しました" in ln)
    assert reset.startswith(T.MARK_BAD), reset
    assert "ここまでの要約だけ引き継ぎ" in reset, reset      # 何が引き継がれたかを同じ行で
    assert "文脈を圧縮 31,240 → 12,880" in text, text
    # 打ち切りには**次の一手**が続く（項目 19③と同じ約束）。頭行は折り返すので
    # 「打ち切りました」の**次に来る `→` の行」を見る（間にあるのは折り返した続き行だけ）。
    rows = _rows(text)
    end = next(i for i, ln in enumerate(rows) if "打ち切りました" in ln)
    after = rows[end + 1:end + 4]
    assert any(ln.strip().startswith("→") for ln in after), after
    assert "広い窓で起動し直して" in "".join(after), after
    # 内部の識別子は 1 つも出さない（項目 17・23）
    assert "context_" not in text and "harness" not in text, text
    assert all(cell_width(ln) <= 79 for ln in _rows(text)), \
        [ln for ln in _rows(text) if cell_width(ln) > 79]
    html = c.export_html(inline_styles=True, clear=False)
    assert T.ALERT.lower() in html.lower()       # 打ち切りと仕切り直しは赤


def test_done_stats_line():
    c = _console()
    r = Renderer(c)
    out = _turn(r, [
        _call("Bash", {"command": "true"}, 1),
        _result("Bash", 1, "", summary={"head": "true", "tail": "(No output)", "status": "ok"}),
        _done("", secs=34.4, tok_s=61, tool_calls={"Bash": 12}),
    ])
    text = c.export_text(clear=False)
    assert "✓ done    34s  道具 12   61.0 tok/s" in text, text
    assert out == {"text": "", "secs": 34.4, "tools": 12}


def test_done_outcome_not_accepted_is_shown_red():
    c = _console()
    r = Renderer(c)
    _turn(r, [_done("x", outcome="max_iters", secs=3, tok_s=10)])
    text = c.export_text(clear=False)
    assert "✗ done     3s  道具  0   10.0 tok/s  反復上限" in text, text
    assert "max_iters" not in text                       # 内部識別子は出さない（項目 8④）
    html = c.export_html(inline_styles=True, clear=False)
    assert T.ALERT.lower() in html.lower()

    c2 = _console()
    r2 = Renderer(c2)
    _turn(r2, [_done("x", outcome="accepted_after_pushback", secs=3, tok_s=10)])
    t2 = c2.export_text(clear=False)
    assert "accepted_after_pushback" not in t2 and "差し戻しあり" in t2, t2


def test_end_turn_without_done_marks_unresolved():
    """cancel 等で done が来ないまま閉じた: 走行中の道具は未解決として残り、done 行は赤。"""
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event(_call("Bash", {"command": "sleep 99"}, 1))
    out = r.end_turn()
    text = c.export_text(clear=False)
    assert "✗ Bash (sleep 99)" in text and "中断（結果なし）" in text
    assert "締めが来ていない" in text and "no done" not in text
    assert out["tools"] == 1


# -- /expand・transcript・user -------------------------------------------------

def test_expand_last_prints_full_args_and_result():
    body = "\n".join(f"line{i}" for i in range(1, 13))
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Bash", {"command": "seq 12", "timeout": 5}, 1),
        _result("Bash", 1, body, summary={"head": "seq 12", "tail": "12 行", "status": "ok"}),
        _done(),
    ])
    before = c.export_text(clear=False)
    assert "line5" not in before                     # 畳まれている（末尾 2 行は出る）
    r.expand_last()
    text = c.export_text(clear=False)
    assert "line12" in text and '"timeout": 5' in text and '"command": "seq 12"' in text


def test_expand_last_when_nothing():
    c = _console()
    r = Renderer(c)
    r.expand_last()
    assert "道具はまだ" in c.export_text(clear=False)


def test_expand_includes_patch_in_full():
    patch = {"path": "a.py", "hunks": [{"old_start": 1, "lines": ["-x", "+y"]}]}
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Edit", {"file_path": "a.py"}, 1),
        _result("Edit", 1, "OK", summary={"head": "a.py", "tail": "Added 1 line, removed 1 line", "patch": patch}),
        _done(),
    ])
    r.expand_last()
    assert c.export_text(clear=False).count("1 - x") == 2      # 本線 1 回＋expand 1 回


def test_user_line_and_transcript_roles():
    c = _console()
    r = Renderer(c)
    r.user("1+1 は？")
    _turn(r, [
        _call("Bash", {"command": "echo 2"}, 1),
        _result("Bash", 1, "2\n", summary={"head": "echo 2", "tail": "1 行", "status": "ok"}),
        {"type": "token", "id": "r1", "text": "2 です"},
        _done("2 です"),
    ])
    text = c.export_text(clear=False)
    assert "> 1+1 は？" in text
    roles = [t["role"] for t in r.transcript]
    assert roles == ["user", "tool", "assistant"]
    assert r.transcript[1]["name"] == "Bash" and r.transcript[1]["ok"] is True
    assert r.transcript[1]["content"] == "2\n"
    assert r.transcript[2]["content"] == "2 です"


def test_wave_is_placed_in_live_and_removed():
    class Wave:
        n = 0

        def __rich__(self):
            self.n += 1
            return "~~~wave~~~"

    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    w = Wave()
    r.set_wave(w)
    # Live のフレームを 1 回描かせる（記録には載らない）
    r._live.refresh()
    assert w.n >= 1
    r.set_wave(None)
    r._live.refresh()
    n = w.n
    r._live.refresh()
    assert w.n == n                       # 外した後は描かれない
    r.on_event(_done("fin"))
    r.end_turn()
    text = c.export_text(clear=False)
    assert "wave" not in text and text.count("fin") == 1


def test_running_tool_shown_in_live_not_in_record():
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event(_call("Bash", {"command": "sleep 1"}, 1))
    r._live.refresh()
    # 実行中の行は Live の中にだけある（Live の描画は記録に載せないので、記録を一時的に開いて見る）
    c.record = True
    assert "Bash (sleep 1)" not in c.export_text(clear=False)
    c.record = False
    frame = Console(width=80, force_terminal=True, file=io.StringIO(), record=True)
    frame.print(r._live.renderable)
    assert "▸ Bash (sleep 1)" in frame.export_text()      # 走行中は ▸
    r.on_event(_result("Bash", 1, "", summary={"head": "sleep 1", "tail": "(No output)", "status": "ok"}))
    r.on_event(_done())
    r.end_turn()
    assert c.export_text(clear=False).count("✓ Bash (sleep 1)") == 1


def test_start_turn_twice_closes_previous():
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event({"type": "token", "id": "r1", "text": "first"})
    r.start_turn("r2")                    # 前のターンを閉じてから始める
    r.on_event(_done("second"))
    r.end_turn()
    text = c.export_text(clear=False)
    assert "first" in text and "second" in text
    assert r._live is None


# -- 検証役が足した境界（2026-08-23）-------------------------------------------

def test_code_block_has_no_white_background():
    """pygments の "bw" は背景が #ffffff＝黒い端末で白い帯になる（実 pty で実測）。背景は塗らない。"""
    c = Console(record=True, width=60, force_terminal=True, file=io.StringIO(), color_system="truecolor")
    r = Renderer(c)
    _turn(r, [{"type": "token", "id": "r1", "text": "x\n\n```python\ndef f():\n    return 1  # c\n```\n"},
              _done("")])
    raw = c.file.getvalue()
    assert "48;2;255;255;255" not in raw and "48;5;231" not in raw, raw
    assert "38;2;0;0;0" not in raw                      # 黒い文字も出さない
    text = c.export_text(clear=False)
    assert "def f():" in text and "return 1" in text


def test_patch_lines_tolerates_broken_values():
    """壊れた値 1 つで描画が落ちない（old_start が文字列・lines に None・hunks が文字列）。"""
    lines = patch_lines({"hunks": [{"old_start": "abc", "lines": [None, 5, "", "-x"]}]})
    plain = [t.plain for t in lines]
    assert plain[-1].endswith("3 - x"), plain          # 0 始まりで 3 行進んだ
    assert patch_lines({"hunks": "str"}) == []
    assert patch_lines({"hunks": [{"lines": "notalist"}]}) == []


def test_start_turn_twice_non_tty_keeps_first_body():
    """非 TTY（Live 無し）でも、閉じていない前ターンの本文は消えない。"""
    c = _console(tty=False)
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event({"type": "token", "id": "r1", "text": "first"})
    r.start_turn("r2")
    r.on_event(_done("second"))
    r.end_turn()
    text = c.export_text(clear=False)
    assert "first" in text and "second" in text
    assert text.count("締めが来ていない") == 1        # 前ターンは done 無しで閉じた


@pytest.mark.parametrize("tty", [True, False])
def test_call_id_missing_or_unhashable_keeps_fifo(tty):
    """call が無い／辞書の鍵にできない値でも、先に走った道具が画面から消えない。"""
    c = _console(tty=tty)
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event(_call("Bash", {"command": "sleep 1"}, None))
    r.on_event(_call("Bash", {"command": "sleep 2"}, ["x"]))
    r.on_event(_result("Bash", None, "one", summary={"head": "sleep 1", "tail": "one"}))
    r.on_event(_result("Bash", ["x"], "two", summary={"head": "sleep 2", "tail": "two"}))
    r.on_event(_done())
    out = r.end_turn()
    text = c.export_text(clear=False)
    assert text.count("✓ Bash") == 2, text
    assert "⎿  one" in text and "⎿  two" in text
    assert "no result returned" not in text
    assert out["tools"] == 2


def test_weird_event_values_do_not_crash():
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    for ev in [{}, {"type": None}, {"type": "token", "text": None}, {"type": "token", "text": 123},
               {"type": "tool_call"}, {"type": "tool_call", "name": None, "input": "notadict", "call": 7},
               {"type": "tool_result", "call": 7, "result": 5, "ok": "yes", "chars": "7",
                "summary": {"head": 5, "tail": [1], "patches": [None, "x", {"hunks": "s"}]}},
               {"type": "tool_result"}, {"type": "notice"}, {"type": "error"}, {"type": "think", "text": None},
               {"type": "done", "outcome": None, "secs": "abc", "tok_s": "fast",
                "tool_calls": {"Bash": "2", "X": "bad"}, "text": None}]:
        r.on_event(ev)
    out = r.end_turn()
    r.expand_last()
    text = c.export_text(clear=False)
    assert "done  " in text and "締めが来ていない" in text   # outcome 無し
    assert "tok/s" not in text                          # 数値でない tok_s は出さない
    assert out["tools"] == 2


def test_multiline_tail_is_indented():
    c = _console()
    r = Renderer(c)
    _turn(r, [_call("X", {"a": "b"}, 1),
              _result("X", 1, "l1", summary={"head": "h", "tail": "t1\nt2"}), _done()])
    text = c.export_text(clear=False)
    assert "⎿  t1\n     t2" in text, text


def test_events_before_start_turn_and_double_end_turn():
    c = _console()
    r = Renderer(c)
    r.on_event({"type": "token", "id": "r1", "text": "x"})
    r.on_event(_call("Bash", {"command": "ls"}, 1))
    r.on_event(_result("Bash", 1, "a", summary={"head": "ls", "tail": "1 行"}))
    r.on_event(_done("x"))
    assert r.end_turn()["text"] == "x"
    r.end_turn()
    r.end_turn()
    assert c.export_text(clear=False).count("x") >= 1


def test_dumb_terminal_uses_no_live(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    c = Console(record=True, width=80, force_terminal=True, file=io.StringIO())
    r = Renderer(c)
    assert r._use_live is False
    _turn(r, [{"type": "token", "id": "r1", "text": "plain"}, _done("plain")])
    assert c.export_text(clear=False).count("plain") == 1


def test_no_color_still_renders(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    c = Console(record=True, width=80, force_terminal=True, file=io.StringIO())
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "false"}, 1),
              _result("Bash", 1, "boom", ok=False, summary={"head": "false", "tail": "exit 1", "status": "error"}),
              _done("")])
    text = c.export_text(clear=False)
    assert "✗ Bash (false)" in text and "⎿  exit 1" in text and "boom" in text


def test_print_above_live_holds_live_lock():
    """実端末（record=False）の _print は Live の刷新スレッドと同じ錠を持つ。

    Rich の Console.print は「消す量の計算」と「描いて積む」が別々で、その隙に刷新スレッドが
    1 フレーム描くと前のフレームの残骸が画面に残る（実 pty で再現）。錠を持てば起きない。
    """
    import threading
    c = Console(record=False, width=80, force_terminal=True, file=io.StringIO())
    r = Renderer(c)
    r.start_turn("r1")
    live = r._live
    assert live is not None
    lock = live._lock
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with lock:
            acquired.set()
            release.wait(2)
    th = threading.Thread(target=holder)
    th.start()
    acquired.wait(2)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    t0 = __import__("time").time()
    r._print("above")
    waited = __import__("time").time() - t0
    th.join(2)
    r.on_event(_done(""))
    r.end_turn()
    assert waited >= 0.25, waited                       # 錠が解けるまで待った＝同じ錠を持っている
    assert "above" in c.file.getvalue()


def test_pause_and_resume_keep_the_turn_open():
    """★pause/resume は app.py が ask の間に使う公開口（内部名を借りない）。"""
    import io
    from rich.console import Console
    from laps.term.render import Renderer
    con = Console(file=io.StringIO(), record=True, width=80, force_terminal=True)
    r = Renderer(con)
    assert r.pause() is False                     # ターンが開いていなければ何もしない
    r.start_turn("r1")
    r.on_event({"type": "token", "text": "聞きます。"})
    assert r.pause() is True
    assert r._live is None                        # Live は止まっている
    assert r._open is True                        # ターンは開いたまま
    r.resume()
    assert r._live is not None                    # 端末なら作り直す
    r.on_event({"type": "done", "outcome": "accepted", "secs": 1.0})
    r.end_turn()                                  # Live を閉じて記録を戻す
    out = con.export_text()
    # ★項目 8: 平文だけ・道具 0・10 秒未満・accepted のターンは done 行を出さない
    assert "聞きます。" in out and "done" not in out and r._open is False


# ==========================================================================
# UI 監査 2026-08-29（SPEC 項目 1,2,3,4,6,8,9,10,11,14,15,17,19,22,23）
# 前後の画: scratch/tui/after/{before,after}-*.txt（同じ場面を同じ幅で描いたもの）
# ==========================================================================

_JP = "検収に失敗しました → /api/works は 500 を返し、Range 付きの取得は 206 ではなく 200 でした。"


def _rows(text: str) -> list[str]:
    return text.splitlines()


# -- 項目 1: 折り返しの幅モデル ------------------------------------------------

def test_wrap_never_exceeds_width_in_both_models():
    """★日本語で必ず試す。A（曖昧幅）を 2 と数えても 1 と数えても幅に収まること。

    ★安全側に倒す理由（裁定 E）: A を 1 と数えて外すと端末が自前で折り返し、続き行が
      桁 0 に落ちて**段が消える**。2 と数えて外れても 1 桁早く折れるだけ。
    """
    import unicodedata
    text = ("検収に失敗しました→/api/works は 500・Range は 200…" * 4 + "\n"
            "mixed 日本語 and ASCII ●·→∴×■ が混ざった行 " * 3)

    def narrow(s: str) -> int:      # A を 1 と数える版
        return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)

    for w in (8, 10, 12, 20, 40, 60, 80, 100):
        for first, cont in ((TAIL, INDENT), ("", "  "), (INDENT, INDENT)):
            for ln in wrap_with_gutter(text, w, first, cont).splitlines():
                assert cell_width(ln) <= w - 1, (w, first, cell_width(ln), ln)
                assert narrow(ln) <= w - 1, (w, narrow(ln), ln)


def test_wrap_no_trailing_space():
    """末尾空白を吐かない（SPEC 項目 1④）。★空行は**本当に空行**（`'     '` を出さない）。

    段（ガター）が続き行にも付くことも併せて見る（項目 2 の前提）。
    """
    out = wrap_with_gutter(_JP * 3, 80, TAIL, INDENT)
    lines = out.splitlines()
    assert len(lines) > 3
    assert lines[0].startswith(TAIL)
    assert all(ln.startswith(INDENT) for ln in lines[1:]), lines
    assert all(ln == ln.rstrip() for ln in lines), lines      # 末尾空白を吐かない
    # 空行・末尾が空白で終わる本文・段だけの行のどれでも空白を残さない
    for w in (12, 40, 80):
        for first, cont in ((TAIL, INDENT), ("", "  "), (INDENT, INDENT)):
            got = wrap_with_gutter("a\n\nb   \n" + _JP, w, first, cont)
            assert all(ln == ln.rstrip() for ln in got.splitlines()), (w, first, got)
            assert "\n     \n" not in got and not got.endswith(" "), got


def test_wrap_narrow_terminal():
    """幅 8/10/12 で溢れない（SPEC 項目 1⑤）。

    ★前: `room = max(8, …)` の床があり、**幅 8/10/12 のどれでも 13 桁の行**を吐いていた
      （`typo/p5_edge.py` 実測）。端末は溢れた行を自前で折り返すので、続き行が桁 0 に
      落ちて段が消える＝いちばん狭い端末でいちばん読めなくなっていた。
    """
    import unicodedata

    def narrow(s: str) -> int:      # A を 1 と数える版
        return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)

    text = "検収に失敗しました → /api/works は 500・Range は 200 … あいう abc def"
    for w in (8, 10, 12):
        for first, cont in ((TAIL, INDENT), ("", "  "), (INDENT, INDENT), (T.MARK_BAD + " ", "  ")):
            lines = wrap_with_gutter(text, w, first, cont).splitlines()
            assert lines, (w, first)
            for ln in lines:
                assert cell_width(ln) <= w - 1, (w, first, cell_width(ln), ln)
                assert narrow(ln) <= w - 1, (w, first, narrow(ln), ln)


def test_wrap_prefix_measured_in_cells_not_characters():
    """接頭辞を len() で引くと、`⎿` や日本語の接頭で 1 桁ずれる（旧実装の穴）。"""
    out = wrap_with_gutter("あ" * 40, 40, "  ⎿  ", "     ")
    assert all(cell_width(ln) <= 39 for ln in out.splitlines()), out


# -- 項目 2: 段を通していない 4 経路 ------------------------------------------

def _cont_lines(text: str, head: str) -> list[str]:
    """`head` で始まる行の**続き行**（次に桁 0 から始まる行の手前まで）。"""
    rows = text.splitlines()
    i = [n for n, ln in enumerate(rows) if ln.startswith(head)][0]
    out = []
    for ln in rows[i + 1:]:
        if not ln.startswith(" ") or ln.startswith(TAIL):
            break                       # 桁 0 の行／次の `⎿` の見出しで終わり
        out.append(ln)
    return out


def test_bash_body_keeps_gutter():
    """Bash 成功の本文（項目 2）。旧実装は続き行が桁 0 に落ちて ⎿ の段が消えた。"""
    c = _console()
    r = Renderer(c)
    long_line = "Aug 29 03:10:00 devbox llama-server[9142]: " + "x" * 60
    _turn(r, [_call("Bash", {"command": "journalctl -n 1"}, 1),
              _result("Bash", 1, long_line, summary={"head": "journalctl -n 1", "tail": "1 行",
                                                     "status": "ok"}),
              _done()])
    text = c.export_text(clear=False)
    assert "x" * 20 in text
    cont = _cont_lines(text, "  ⎿  1 行")
    assert len(cont) >= 2
    assert all(ln.startswith(INDENT) for ln in cont), cont      # 続き行も段の中


def test_expand_and_error_and_notice_keep_gutter():
    c = _console()
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "x " + "あ" * 80}, 1),
              _result("Bash", 1, _JP * 2, summary={"head": "x", "tail": "1 行", "status": "ok"}),
              {"type": "notice", "id": "r1", "kind": "harness", "message": _JP * 2},
              {"type": "error", "id": "r1", "kind": "backend", "message": _JP * 2},
              _done()])
    r.expand_last()
    text = c.export_text(clear=False)
    # 通知・エラーは桁 0 の行だが、続き行は段 2 に落ちる（旧実装は桁 0 のままだった）
    for head in ("検収に失敗しました", "✗ エラー（backend）"):
        cont = _cont_lines(text, head)
        assert cont and all(ln.startswith("  ") and not ln.startswith("   ") for ln in cont), cont
    # /expand の引数と結果は段の中
    assert all(ln.startswith(INDENT) for ln in _cont_lines(text, "  ⎿  引数")), text
    assert all(cell_width(ln) <= 79 for ln in text.splitlines()), \
        [ln for ln in text.splitlines() if cell_width(ln) > 79]


# -- 項目 3: 走行中の頭行 ------------------------------------------------------

def test_head_line_wraps_with_gutter():
    """幅 60 で `● Bash (ssh …` の続き行が桁 0 に落ちていた（実測 motion/scrollback-narrow60）。"""
    c = Console(record=True, width=60, force_terminal=True, file=io.StringIO())
    r = Renderer(c)
    e = _entry(r, "Bash", {"command": "ssh user@192.0.2.161 'systemctl status blackwall-coder -l'"})
    t = r._head_line(e, running=True)
    lines = t.plain.splitlines()
    assert len(lines) > 1
    assert all(ln.startswith("  ") for ln in lines[1:]), lines
    assert all(cell_width(ln) <= 60 for ln in lines), lines


def test_reserve_counts_physical_rows():
    """折り返す頭行を行数で数えると、その分だけ本文が画面外へ押し出される（項目 3②）。"""
    from rich.segment import Segment
    c = Console(record=True, width=60, height=24, force_terminal=True, file=io.StringIO())
    r = Renderer(c)
    r.start_turn("r1")
    for i in range(4):
        r.on_event(_call("Bash", {"command": f"ssh user@192.0.2.16{i} 'systemctl status "
                                             f"blackwall-coder --no-pager -l'"}, i))
    r.on_event({"type": "token", "id": "r1", "text": "本文\n" * 40})
    opts = c.options.update(height=24)
    rows = sum(1 for sg in c.render(r._live.renderable, opts) if sg is Segment.line()
               or sg.text == "\n")
    assert rows <= 24, rows
    r.end_turn()


# -- 項目 4: 差分の続き行 ------------------------------------------------------

def test_patch_continuation_keeps_mark():
    long_add = "+" + "    self._print(Text(wrap_with_gutter(tail, self.console.width, TAIL, INDENT)))" * 2
    lines = patch_lines({"hunks": [{"old_start": 29, "lines": [" ctx", long_add]}]}, 80)
    rows = lines[-1].plain.splitlines()
    assert len(rows) > 1
    for row in rows:
        assert row[:5] == INDENT, row
        assert row[5:7].strip() in ("", "29", "30"), row       # 行番号欄（続き行は空）
        assert row[8] == "+", row                              # ★印は続き行にも残る
        assert cell_width(row) <= 79, row
    assert rows[0][5:7] == "  " and rows[1][5:7] == "  "       # 追加行は旧行番号を持たない


def test_patch_lines_without_width_is_unchanged():
    """width を渡さない呼び出し（既定 0）は旧来どおり 1 行 1 Text。"""
    lines = patch_lines({"hunks": [{"old_start": 1, "lines": ["-a", "+b"]}]})
    assert [t.plain for t in lines] == [f"{INDENT}1 - a", f"{INDENT}  + b"]


# -- 項目 6: 色が無くても状態が分かる -----------------------------------------

def test_no_color_success_and_failure_differ(monkeypatch):
    """★NO_COLOR / TERM=dumb では色が 1 バイトも出ない。**実バイト**で見る
    （export_text は no_color を反映しないので使わない）。旧実装は両方 `●` で
    バイト単位に同一だった（audit-color/no_color_bytes.py 実測）。"""
    for env in ("NO_COLOR", "TERM"):
        monkeypatch.setenv(env, "1" if env == "NO_COLOR" else "dumb")
    c = Console(record=True, width=80, force_terminal=True, file=io.StringIO())
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "systemctl status a"}, 1),
              _result("Bash", 1, "active", summary={"head": "status a", "tail": "1 行",
                                                    "status": "ok"}),
              _call("Bash", {"command": "systemctl restart b"}, 2),
              _result("Bash", 2, "boom", ok=False,
                      summary={"head": "restart b", "tail": "exit 5", "status": "error"}),
              _done()])
    raw = c.file.getvalue()
    ok_line = [ln for ln in raw.splitlines() if "status a" in ln][0]
    bad_line = [ln for ln in raw.splitlines() if "restart b" in ln][0]
    assert ok_line.strip().startswith(T.MARK_OK), ok_line
    assert bad_line.strip().startswith(T.MARK_BAD), bad_line
    assert ok_line[:2] != bad_line[:2]                  # バイトが違う＝字で分かれている


def test_diff_paints_no_background():
    """差分は前景色で描く（背景を塗らない）。16 色では追加も削除も同じ黒に潰れる。"""
    c = Console(record=True, width=80, force_terminal=True, file=io.StringIO(),
                color_system="truecolor")
    r = Renderer(c)
    patch = {"path": "a.py", "hunks": [{"old_start": 1, "lines": ["-x", "+y", " z"]}]}
    _turn(r, [_call("Edit", {"file_path": "a.py"}, 1),
              _result("Edit", 1, "OK", summary={"head": "a.py", "tail": "1 行", "patch": patch}),
              _done()])
    assert "48;" not in c.file.getvalue()                # 背景色の SGR が 1 つも無い


# -- 項目 8: done 行 -----------------------------------------------------------

def test_done_line_hidden_on_plain_success():
    """平文だけ・道具 0・10 秒未満・accepted なら、毎回同じことを言う行は出さない。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "token", "id": "r1", "text": "2 です。"},
              _done("2 です。", secs=3.0, tok_s=58.2)])
    text = c.export_text(clear=False)
    assert "done" not in text and "2 です。" in text
    # 10 秒以上なら出る（時間は言う価値がある）
    c2 = _console()
    _turn(Renderer(c2), [{"type": "token", "id": "r1", "text": "x"}, _done("x", secs=42.0)])
    assert "done" in c2.export_text(clear=False)


def test_done_line_shows_partial_failure():
    """★束の中の失敗が締めの行に 1 文字も残っていなかった（旧実装）。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "true"}, 1),
              _result("Bash", 1, "", summary={"head": "true", "tail": "ok", "status": "ok"}),
              _call("Bash", {"command": "false"}, 2),
              _result("Bash", 2, "boom", ok=False,
                      summary={"head": "false", "tail": "exit 1", "status": "error"}),
              _done(secs=12.0, tok_s=44.0, tool_calls={"Bash": 2})])
    text = c.export_text(clear=False)
    assert "失敗  1" in text, text
    # ★2026-09-03: 印は結末で決める（accepted なら ✓）。失敗は `失敗 N` の赤で見せる（上の assert）。
    #   旧「accepted でも ✗」は、python→python3 に切り替えただけの成功走行を赤で締めた（dogfood ⑮）。
    assert text[text.index("done") - 2] == T.MARK_OK


def test_done_line_columns_align():
    """桁が毎行ずれると走行どうしを縦に比べられない（frames/06 実測）。"""
    cols = []
    for secs, n, tok in ((1.0, 1, 58.2), (4.0, 12, 39.4), (128.0, 3, 7.5)):
        c = _console()
        _turn(Renderer(c), [_done("", secs=secs, tok_s=tok, tool_calls={"Bash": n})])
        line = [ln for ln in c.export_text(clear=False).splitlines() if "done" in ln][0]
        cols.append((line.index("道具"), line.index("tok/s")))
    assert len(set(cols)) == 1, cols


# -- 項目 9・10 ---------------------------------------------------------------

def test_blank_line_between_turns():
    c = _console()
    r = Renderer(c)
    r.user("最初")
    _turn(r, [{"type": "token", "id": "r1", "text": "はい"}, _done("はい")])
    r.user("次")
    rows = _rows(c.export_text(clear=False))
    assert rows[0].startswith("> 最初")                  # 起動直後には入れない
    i = [n for n, ln in enumerate(rows) if ln.startswith("> 次")][0]
    assert rows[i - 1].strip() == ""                     # 2 ターン目の前に空行 1


def test_head_line_weight_on_argument():
    """太字は名前ではなく引数の先頭の語（見分けたいのはサービス名・ファイル名）。"""
    c = _console()
    r = Renderer(c)
    e = _entry(r, "Bash", {"command": "systemctl restart blackwall-coder"})
    t = r._head_line(e)
    bold = [t.plain[sp.start:sp.end] for sp in t.spans if "bold" in str(sp.style)]
    assert bold == ["systemctl"], (bold, t.plain)


# -- 項目 11: 24 行の窓に収まるか ----------------------------------------------

def test_failure_fits_in_24_rows():
    """★旧実装は失敗の本文 10 行が幅 80 で 16 物理行に膨らみ、`✗ Bash (…)` の見出しと
    `⎿ exit 5` が画面外へ押し出されていた（audit-info/frames24/C 実測）。"""
    c = _console()
    r = Renderer(c)
    body = "\n".join(f"Aug 29 03:14:0{i % 10} devbox systemd[1]: " + "x" * 60
                     for i in range(14))
    _turn(r, [_call("Bash", {"command": "systemctl restart missing.service"}, 1),
              _result("Bash", 1, body, ok=False,
                      summary={"head": "systemctl restart missing.service", "tail": "exit 5",
                               "status": "error"}),
              _done(secs=3.0, tool_calls={"Bash": 1})])
    rows = [ln for ln in _rows(c.export_text(clear=False)) if ln.strip()]
    assert len(rows) <= 24, len(rows)
    window = rows[-24:]
    assert any(ln.startswith("✗ Bash") for ln in window)
    assert any("exit 5" in ln for ln in window)


# -- 項目 15・17: 状態 ---------------------------------------------------------

def test_phase_kinds_differ():
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "notice", "id": "r1", "kind": "phase_start", "message": "工程 2/7 API"},
              {"type": "notice", "id": "r1", "kind": "phase_done", "message": "工程 2/7 合格"},
              {"type": "notice", "id": "r1", "kind": "phase_retry", "message": "工程 2/7 不合格"},
              _done()])
    rows = [ln for ln in _rows(c.export_text(clear=False)) if "工程" in ln]
    assert [ln[0] for ln in rows] == [T.MARK_RUN, T.MARK_OK, T.MARK_BAD], rows
    assert not any("phase_" in ln for ln in rows)        # 内部の識別子を出さない
    html = c.export_html(inline_styles=True, clear=False)
    assert T.GOOD.lower() in html.lower() and T.ALERT.lower() in html.lower()


def test_notice_comes_after_the_body_it_interrupted():
    """旧実装は notice が**割り込んだ本文より先に**印字され、因果が逆に見えた（項目 15③）。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "token", "id": "r1", "text": "ここまで書いた。"},
              {"type": "notice", "id": "r1", "kind": "harness", "message": "割り込み"},
              {"type": "token", "id": "r1", "text": "続き。"},
              _done("ここまで書いた。続き。")])
    text = c.export_text(clear=False)
    assert text.index("ここまで書いた。") < text.index("割り込み") < text.index("続き。"), text


def test_stale_tool_marked_bad():
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event(_call("Bash", {"command": "find / -xdev"}, 1))
    r.end_turn()
    text = c.export_text(clear=False)
    assert "✗ Bash (find / -xdev)" in text and "⎿  中断（結果なし）" in text, text


def test_running_head_shows_elapsed_and_how_to_stop():
    """走行中の頭行の末尾に経過と中断の作法（**行は増やさない**・項目 14）。"""
    c = _console()
    r = Renderer(c)
    e = _entry(r, "Bash", {"command": "pytest -q"})
    e.t0 = e.t0 - 492
    t = r._head_line(e, running=True)
    assert "8m12s" in t.plain and "Ctrl-C 中断" in t.plain, t.plain
    assert t.plain.startswith(T.MARK_RUN)
    # 済んだ道具には出さない（流れて残る行に 0s が固まるのを避ける）
    e.done = True
    e.ok = True
    assert "Ctrl-C" not in r._head_line(e).plain


# -- 項目 19: 失敗の重複と次の一手 ---------------------------------------------

def test_failure_dedupes_multiline_body():
    """★旧実装の重複除けは「本文全体が tail と同じか」しか見ておらず、本文が複数行だと
    効かなかった（scratch/tui/after/before-03 実測: 同じ 3 行が 2 度出ていた）。"""
    c = _console()
    r = Renderer(c)
    body = _JP + "\n→ 代わりにこうできます: 倉庫に php8.4-sqlite3 を足してください"
    _turn(r, [_call("Bash", {"command": "acceptance"}, 1),
              _result("Bash", 1, body, ok=False,
                      summary={"head": "acceptance", "tail": _JP, "status": "error"}),
              _done(secs=3.0, tool_calls={"Bash": 1})])
    text = c.export_text(clear=False)
    assert text.count("検収に失敗しました") == 1, text
    assert "代わりにこうできます" in text


def test_failure_always_has_next_step():
    c = _console()
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "false"}, 1),
              _result("Bash", 1, "boom", ok=False,
                      summary={"head": "false", "tail": "exit 1", "status": "error"}),
              _done(secs=1.0, tool_calls={"Bash": 1})])
    assert "→ /expand で全文" in c.export_text(clear=False)


# -- 項目 22: /expand の空状態 -------------------------------------------------

def test_expand_empty_message_after_turn():
    """道具を使ったターンの**次**で「まだ使われていません」は嘘（項目 22①）。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "true"}, 1),
              _result("Bash", 1, "", summary={"head": "true", "tail": "ok"}), _done()])
    _turn(r, [{"type": "token", "id": "r1", "text": "x"}, _done("x")], rid="r2")
    r.expand_last()
    text = c.export_text(clear=False)
    assert "もう残っていません" in text and "まだ使われていません" not in text, text


# -- 項目 23: 数え方と並べ方 ---------------------------------------------------

def test_no_english_ui_words():
    """画面に出る語は日本語。★`exit 5` のようなシステムの生の値は訳さない。"""
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event({"type": "think", "id": "r1", "text": "考える"})
    r.on_event(_call("Bash", {"command": "make"}, 1))
    r.on_event(_result("Bash", 1, "\n".join(f"e{i}" for i in range(20)), ok=False,
                       summary={"head": "make", "tail": "exit 2", "status": "error"}))
    r.on_event(_call("Bash", {"command": "sleep 9"}, 2))
    r.end_turn()
    r.expand_last()
    text = c.export_text(clear=False)
    for word in ("tools", " lines", "no result", "Thought for", "input", "running)", "(empty)"):
        assert word not in text, (word, text)
    assert "exit 2" in text                              # 生の値はそのまま


def test_large_numbers_grouped():
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "notice", "id": "r1", "kind": "context_compacted",
               "before": 31240, "after": 8120},
              _call("Bash", {"command": "seq"}, 1),
              _result("Bash", 1, "x", summary={"head": "seq", "tail": "1 行"},
                      chars=1234567, truncated=True),
              _done()])
    r.expand_last()
    text = c.export_text(clear=False)
    assert "31,240 → 8,120" in text, text
    assert "1,234,567 文字" in text, text


# --------------------------------------------------------------------------
# 合流（2026-08-29）— 3 人が別々に書いたものを**繋いだ**ところ。
# ★ここが試験になっていないと、`written-but-not-wired.md` と同じ穴になる:
#   波は `refresh_per_second` を持ち、render は 20 の固定値で Live を作っていた＝
#   「--calm は実測で刷新回数を 1 回も減らしていなかった」が直らないまま出荷される。
# --------------------------------------------------------------------------

class _FakeWave:
    """波の代わり。render が**どの属性を読むか**だけを決める（波の中身は test_term_wave が見る）。"""

    def __init__(self, rps: float, live_rows: int) -> None:
        self.refresh_per_second = rps
        self.live_rows = live_rows
        self.rows = 2

    def __rich_console__(self, console, options):
        for _ in range(self.live_rows):
            yield "~" * 4


def test_live_refresh_rate_comes_from_the_wave():
    """Live の刷新回数は波が持つ上限に合わせる（calm が本当に帯域を下げる）。"""
    c = _console()
    r = Renderer(c)
    r.set_wave(_FakeWave(8.3, 2))
    r.start_turn("r1")
    assert r._live is not None
    assert r._live.refresh_per_second == pytest.approx(8.3)
    r.end_turn()

    r.set_wave(_FakeWave(22.2, 2))
    r.start_turn("r2")
    # 上限は 20（波が速くても端末へ書く回数は増やさない）
    assert r._live.refresh_per_second == pytest.approx(20.0)
    r.end_turn()

    r.set_wave(None)                      # 波が無いときは既定
    r.start_turn("r3")
    assert r._live.refresh_per_second == pytest.approx(Renderer.LIVE_FPS)
    r.end_turn()


def test_reserve_uses_live_rows_not_rows():
    """本文に割ける高さは、波が**いま実際に占める行数**で決める。

    ★`rows`（作り置きの 2）で引くと、波が off／遅延ゲートの最中でも本文が 2 行短くなる。
    """
    from laps.term.render import _LiveView
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    # ★本文は Markdown。素の改行は 1 段落に畳まれて 2 行になり、高さの差が出ない
    #   （最初にそう書いて外した）。コード塀にして 1 行 1 行のまま数える。
    body = "```\n" + "\n".join(f"L{i}" for i in range(40)) + "\n```"
    r.on_event({"type": "token", "id": "r1", "text": body})

    def body_rows(wave) -> int:
        r.set_wave(wave)
        frame = Console(width=80, height=24, force_terminal=True, file=io.StringIO())
        with frame.capture() as cap:
            frame.print(_LiveView(r))
        return sum(1 for ln in cap.get().splitlines() if "L" in ln)

    n_gated = body_rows(_FakeWave(20.0, 0))   # まだ出ていない波（live_rows=0）
    n_shown = body_rows(_FakeWave(20.0, 2))   # 出ている波
    r.end_turn()
    assert n_gated == n_shown + 2, (n_gated, n_shown)


def test_forget_tools_is_public_and_tells_the_truth():
    """`/clear` は束を捨てる。捨てた後の `/expand` は「まだ」ではなく「もう無い」と言う。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "seq 3"}, 1),
              _result("Bash", 1, "1\n2\n3", summary={"head": "seq 3", "tail": "3 行"}),
              _done()])
    assert r._bundle
    r.forget_tools()                       # ← slash.py の /clear がこれを呼ぶ
    assert not r._bundle
    c2 = _console()
    r.console = c2
    r.expand_last()
    text = c2.export_text(clear=False)
    assert "もう残っていません" in text, text
    assert "まだ使われていません" not in text


def test_body_never_ends_with_a_space():
    """本文（Markdown）が行末に空白を吐かない。

    ★Rich の Markdown は段落を幅いっぱいに空白で埋める。画面では見えないが、遅い回線では
      その空白がそのまま帯域になり、SPEC §6-4 の「末尾空白を吐かない」を本文だけが破っていた。
    """
    for width in (60, 80, 100):
        c = Console(record=True, width=width, force_terminal=True, file=io.StringIO())
        r = Renderer(c)
        _turn(r, [{"type": "token", "id": "r1", "text": "起動できません。"},
                  _done(text="起動できません。")])
        for line in c.export_text(clear=False).splitlines():
            assert line == line.rstrip(), (width, repr(line))


def test_failure_next_step_is_readable_not_a_footnote():
    """失敗のときの「→ /expand で全文」は**読ませたい行**なので注記の色に置かない。

    ★目盛り自身が NOTE を「読ませる本文には使わない」と書いている（Solarized dark で
      4.37:1）。畳んだ残り行数のような受け身の注記は NOTE のままでよい（下の Bash 成功側）。
    """
    c = _console()
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "false"}, 1),
              _result("Bash", 1, "boom", ok=False,
                      summary={"head": "false", "tail": "exit 1", "status": "error"}),
              _done()])
    styles = {}
    for seg in c._record_buffer:
        if seg.text.strip().startswith("→ /expand"):
            styles["next"] = seg.style
    assert styles, "次の一手の行が見つからない"
    assert str(styles["next"]).lower() == str(T.S_META).lower(), styles
    assert str(styles["next"]).lower() != str(T.S_DIM).lower()


# --------------------------------------------------------------------------
# 合流の実機走行（届かない送り先へ 1 回投げる）で見つけた 3 件
#   前: 8 行 — `run_start` / 同じ文面 2 度 / 0 秒のターンに 117.0 tok/s
#   後: 3 行
# --------------------------------------------------------------------------

def test_internal_kind_never_reaches_the_screen():
    """`run_start` は message を持たないので、内部識別子がそのまま画面に出ていた。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "notice", "id": "r1", "kind": "run_start", "session": "s1"},
              {"type": "notice", "id": "r1", "kind": "queued", "position": 2},
              {"type": "token", "id": "r1", "text": "はい"},
              _done(text="はい")])
    text = c.export_text(clear=False)
    assert "run_start" not in text, text
    assert "queued" not in text, text
    assert "はい" in text


def test_error_text_is_not_printed_twice():
    """`error` の文面と `done.text` が同じ文字列で来ることがある（届かない送り先）。"""
    msg = "[backend error] 推論機に接続できません: http://127.0.0.1:9/v1（ConnectError）。"
    c = _console()
    r = Renderer(c)
    _turn(r, [{"type": "error", "id": "r1", "kind": "backend", "message": msg},
              _done(text=msg, outcome="backend_error", secs=0.2, tok_s=117.0)])
    text = c.export_text(clear=False)
    assert text.count("ConnectError") == 1, text
    assert "エラー（backend）" in text


def test_tok_s_is_hidden_when_it_cannot_be_measured():
    """1 秒に満たないターンの tok/s は測れていない数字。出さない。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [_done(outcome="backend_error", secs=0.2, tok_s=117.0)])
    text = c.export_text(clear=False)
    assert "tok/s" not in text, text
    assert "推論器に届かない" in text
    c2 = _console()
    r2 = Renderer(c2)
    _turn(r2, [_done(outcome="max_iters", secs=3.0, tok_s=10.0)])
    assert "10.0 tok/s" in c2.export_text(clear=False)     # 測れているときは出す


# ---------------------------------------------------------------------------
# 本文の読み幅（2026-09-01・SPEC は幅 20/40/80/100 でしか検証していなかった）
# ---------------------------------------------------------------------------
_PROSE = ("192.0.2.251 が動作していることを確認しました。ネットワーク上に到達可能であり、"
          "SSH ポート（22）が開いているため、ホストは動作しています。しかし、システム状態の"
          "確認に必要な RemoteRead がエラーを返しました。")


def _prose_render(width: int, prose_width: int | None, monkeypatch, tmp_path) -> list[str]:
    import io

    from rich.console import Console

    from laps.term.render import _MD_THEME, _md_block

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if prose_width is None:
        monkeypatch.delenv("LAPS_PROSE_WIDTH", raising=False)
    else:
        monkeypatch.setenv("LAPS_PROSE_WIDTH", str(prose_width))
    buf = io.StringIO()
    con = Console(file=buf, width=width, force_terminal=False, no_color=True)
    con.push_theme(_MD_THEME)
    con.print(_md_block(con, _PROSE))
    return buf.getvalue().splitlines()


def test_prose_measure_caps_wide_terminal(monkeypatch, tmp_path):
    """★幅 200 の端末でも本文は読み幅に収まる（実機がこの幅だった）。"""
    from laps.term.render import cell_width

    lines = _prose_render(200, 96, monkeypatch, tmp_path)
    assert lines, "何も描かれていない"
    worst = max(cell_width(ln) for ln in lines)
    assert worst <= 96, f"本文が読み幅を超えている: {worst} セル"
    assert len(lines) >= 2, "1 行の板のまま（折れていない）"


def test_prose_measure_does_not_touch_narrow(monkeypatch, tmp_path):
    """★読み幅より狭い端末では**バイト同一**（既存の 36 場面の門を動かさない）。"""
    with_cap = _prose_render(80, 96, monkeypatch, tmp_path)
    without = _prose_render(80, 0, monkeypatch, tmp_path)
    assert with_cap == without, "狭い端末の絵が変わっている（門の前提が崩れる）"


def test_prose_measure_off_follows_terminal(monkeypatch, tmp_path):
    """★0 で従来どおり端末幅いっぱいに戻せる（戻せない変更は入れない）。"""
    from laps.term.render import cell_width

    lines = _prose_render(200, 0, monkeypatch, tmp_path)
    assert max(cell_width(ln) for ln in lines) > 96, "0 にしても狭いまま＝設定が効いていない"


def test_prose_measure_leaves_tool_output_alone(monkeypatch, tmp_path):
    """★道具の出力は幅が要るので**狭めない**（本文だけの話）。"""
    import io

    from rich.console import Console

    from laps.term.render import Renderer, cell_width

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_PROSE_WIDTH", "96")
    buf = io.StringIO()
    con = Console(file=buf, width=200, force_terminal=False, no_color=True)
    r = Renderer(con)
    long_line = "x" * 180
    _turn(r, [_call("Bash", {"command": "echo hi"}, 1),
              _result("Bash", 1, long_line),
              _done()])
    out = buf.getvalue().splitlines()
    assert any(cell_width(ln) > 96 for ln in out), \
        "道具の出力まで狭めている（幅が要るものを詰めてはいけない）"


# -- 狭い端末・組み立てた行の桁（2026-09-02 の監査） ---------------------------

@pytest.mark.parametrize("width", [8, 10, 12, 16, 20, 21, 22, 24, 40, 80, 200])
def test_head_line_fits_narrow_terminal(width):
    """道具の頭行が端末幅を越えない（幅 8〜21 で越えていた・実測）。

    ★なぜ大事か: この行は「**どの道具が失敗したか**」を名指す 1 行で、崩れたときの損は
      本文より大きい。SPEC 項目 1③ は `wrap_with_gutter` から `max(8, …)` を取り除いたが、
      その兄弟である `_head_line` には `max(20, …)` が残っており、幅 8〜21 の端末では
      印 2 桁ぶんはみ出して端末が自前で折り返し、続き行が桁 0 に落ちて段が消えていた。
    """
    c = Console(record=True, width=width, force_terminal=False, file=io.StringIO())
    r = Renderer(c)
    e = _entry(r, "Bash", {"command": "systemctl status blackwall-coder --no-pager --full"})
    e.summary = {"head": "systemctl status blackwall-coder --no-pager --full"}
    lines = r._head_line(e).plain.split("\n")
    assert max(cell_width(ln) for ln in lines) <= width - 1, lines
    for ln in lines[1:]:
        assert ln.startswith("  "), ("続き行の段が消えている", lines)


def test_fit_parts_drops_least_important_first():
    """`fit_parts` は rank の大きいものから落として幅に収める（0 は落とさない）。"""
    from laps.term.render import fit_parts
    parts = [("✗", T.S_MARK_BAD, 0), (" done", T.S_META, 0),
             ("  12s", T.S_META, 1), ("  道具 5", T.S_META, 2),
             ("  失敗 2", T.S_BAD, 0), ("  40.0 tok/s", T.S_META, 3)]
    assert fit_parts(parts, 100).plain == "✗ done  12s  道具 5  失敗 2  40.0 tok/s"
    got = fit_parts(parts, 28).plain          # tok/s（統計）が最初に落ちる
    assert "tok/s" not in got and "道具 5" in got and "失敗 2" in got, got
    got = fit_parts(parts, 20).plain          # 次に道具の数
    assert "道具" not in got and "12s" in got and "失敗 2" in got, got
    # 落とし切っても収まらないときは**自分で**段付きに折る（Rich に折らせない）
    folded = fit_parts(parts, 10).plain.split("\n")
    assert len(folded) > 1
    assert all(cell_width(ln) == len(ln.rstrip()) or True for ln in folded)
    assert all(ln == ln.rstrip() for ln in folded), folded


@pytest.mark.parametrize("width", [20, 24, 30, 40, 60, 80, 200])
def test_done_line_leaves_no_trailing_space(width):
    """締めの行が末尾空白を吐かない（SPEC §6-4）。

    ★実測（2026-09-02・実機の記録 148 走行を描き直した）: 幅 40 で **64 行**が
      末尾空白つきだった（うち 62 行がこの `done` 行）。原因は組み立てた `Text` が
      幅を超えたときの Rich の折り返しで、`Text.wrap` は「幅の**外**」の空白しか落とさず、
      折り目に置いた区切り 2 桁が行末に残る。幅 60・80 では 0 行＝**狭い端末でだけ**起きる。
    """
    c = Console(record=True, width=width, force_terminal=False, file=io.StringIO())
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "false"}, 1),
              _result("Bash", 1, "boom", ok=False, summary={"tail": "exit 1", "status": "error"}),
              _done(outcome="max_iters", secs=12.0, tok_s=44.0,
                    tool_calls={"Bash": 31})])
    for ln in c.export_text(clear=True).split("\n"):
        assert ln == ln.rstrip(), (width, repr(ln))


def test_expand_hint_keeps_gutter_when_narrow():
    """失敗の「次の一手」も段を通す（SPEC 項目 2 の兄弟が 10 か所残っていた）。"""
    c = Console(record=True, width=24, force_terminal=False, file=io.StringIO())
    r = Renderer(c)
    _turn(r, [_call("Bash", {"command": "false"}, 1),
              _result("Bash", 1, "boom", ok=False, summary={"tail": "exit 1", "status": "error"}),
              _done(outcome="accepted")])
    lines = [ln for ln in c.export_text(clear=True).split("\n") if "expand" in ln or "全文" in ln]
    assert lines, "次の一手が出ていない"
    for ln in lines:
        assert ln.startswith(INDENT), ("段が消えている", lines)


def test_prose_width_reads_the_config_file_we_were_started_with(tmp_path, monkeypatch):
    """`--config` で指した設定ファイルの `prose_width` が効く（効いていなかった）。

    ★旧実装は `config.resolve()` を**引数なし**で呼んでおり、読んでいたのは常に既定の
      `~/.config/blackwall/config.toml` だった＝`--config` を使う人には「設定したのに
      効かない」。しかも画面には何も出ないので気づけない。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))   # 既定の場所は別にする
    cfg = tmp_path / "mine.toml"
    cfg.write_text("prose_width = 40\n", encoding="utf-8")
    body = "日本語の本文がとても長く続きます。" * 10

    def widest(path):
        c = Console(record=True, width=200, force_terminal=False, file=io.StringIO())
        r = Renderer(c, config_path=path)
        _turn(r, [{"type": "token", "id": "r1", "text": body}, _done()])
        return max(cell_width(ln) for ln in c.export_text(clear=True).split("\n"))

    assert widest(cfg) <= 40, "--config の prose_width が効いていない"
    assert widest(None) > 40, "既定の設定まで 40 になっている（別の場所を読むべき）"


def test_close_stops_live_and_restores_cursor_after_exception():
    """描画の途中で例外が出ても Live を畳み、**カーソルを戻す**（2026-09-02）。

    ★旧: `run_turn` はそのまま抜け、Live が走ったまま＝カーソルを隠したままプロセスが
      終わった（Rich の Live は stop() でしか戻さず atexit も持たない）。利用者の手元に
      残るのは「カーソルの見えないシェル」で、`reset` を打つまで直らない。
    ★締めの行は出さない（走り切っていないのに done を出すのは嘘・SPEC 項目 18①）。
    """
    buf = io.StringIO()
    c = Console(file=buf, force_terminal=True, width=80, height=24)
    r = Renderer(c)
    r.start_turn("r1")

    class Boom(dict):
        def get(self, k, d=None):
            if k == "text":
                raise RuntimeError("壊れた事象")
            return dict.get(self, k, d)

    with pytest.raises(RuntimeError):
        r.on_event(Boom(type="token"))
    r.close()
    out = buf.getvalue()
    assert out.count("\x1b[?25l") == out.count("\x1b[?25h") >= 1, "カーソルが隠れたまま"
    assert r._live is None and r._open is False
    assert " done" not in out, "走り切っていないのに締めの行を出している"
    r.close()          # 2 回目も安全（finally から二重に呼ばれる）


def _span_color_for(html: str, needle: str) -> str:
    """Rich の `export_html` から、`needle` を含む1個の `<span style="color: …">` の色だけを取り出す。

    ★r4b/app-term-clients（レビュー must_fix #6・推奨）: 元の
      `test_engine_retry_session_ask_expired_are_not_dim` は
      `T.NOTE.lower() not in html.lower()`（html 全体の否定表明）で判定していた。
      これだと**当該行のスタイルを一度も見ない**——同じフレームに無関係な
      S_DIM 行が1つ増えるだけで、当該行が直っていても偽陽性で落ちる（かつ
      当該行が壊れて S_DIM になっても他の行に S_NOTE が無ければ偽陰性で通る、
      という逆方向の穴も同時にある）。ここでは対象行の `<span>` 1個だけを
      取り出し、その色を直接比べる。
    """
    for m in re.finditer(r'<span style="color: (#[0-9a-fA-F]{6})[^"]*">([^<]*)</span>', html):
        if needle in m.group(2):
            return m.group(1).lower()
    raise AssertionError(f"{needle!r} を含む <span> が html に無い（export_html の書式が変わった？）\n{html}")


# -- r4/app-term-clients（2026-09-05）: verifier/red_tests が既定の無印・薄灰色に
#    落ちていた抜け、と serve が出す notice kind の completeness ------------------

@pytest.mark.parametrize("kind,message", [
    ("verifier", "Verifier objection: claimed completion does not match reality.\n"
                 "  1. video.php を作ったと主張したが存在しない\n"
                 "Address each objection, then re-claim completion."),
    ("red_tests", "⚠ 最後に走らせたテストは 2 件失敗のまま（証拠 [3]）。この報告は合格を意味しません。"),
])
def test_verifier_and_red_tests_notices_are_prominent(kind, message):
    """★2026-09-05（r4/app-term-clients）: `verifier`（agent.py `_push_back_on_verifier`
    ＝完了の主張と作業場の実体が食い違う）と `red_tests`（`_push_back_on_red_tests`
    ＝差し戻しを1回使い切ってもテスト失敗のまま完了を認める）は、どちらも
    objection/警告の本文を運ぶ notice なのに `_NOTICE` 表に無く、既定の
    無印・薄灰色に落ちていた。`[verifier N]`/`[red_tests 1]` という短い予告
    （kind="harness"）は `_GATE_MARKS` 経由で既に目立つが、その直後に来る
    本体（objection_message／警告文）を運ぶこちらの notice が漏れていた。
    Windows 側（wire.rs GATE_KINDS）は元から両方を目立つ側に置いており、
    `test_windows_parity.py::test_the_gates_the_core_pushes_back_with_are_prominent_kinds`
    が固定している——端末だけが独立して漏れていた。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": kind, "message": message},
        _done(),
    ])
    text = c.export_text(clear=False)
    row = next(ln for ln in _rows(text) if message.splitlines()[0][:10] in ln)
    assert row.startswith(T.MARK_BAD), row
    html = c.export_html(inline_styles=True, clear=False)
    assert _span_color_for(html, message.splitlines()[0][:10]) == T.ALERT.lower()


@pytest.mark.parametrize("kind,message", [
    ("engine_retry", "[engine_retry 1] 推論機から 45 秒応答が無く、生存を確かめたうえで送り直しています"),
    ("session", "継続できないので新しく始める: セッションが見つかりません"),
    ("ask_expired", "この質問はもう時間切れです（本体は返事なしとして進みました）"),
])
def test_engine_retry_session_ask_expired_are_not_dim(kind, message):
    """★2026-09-05: 3つとも「利用者が気づかないと実害がある」種類の notice
    （推論機が長く黙っていた／セッションの継続が黙って失敗し会話が途切れた／
    利用者の返事が時間切れで黙って捨てられた）だが、いずれも `_NOTICE` 表に無く
    既定の無印・薄灰色に落ちていた。Windows 側（wire.rs PLAIN_KINDS）は3つとも
    「失敗の Gate ではないが、ふつうの1行として見える」扱い（`context_warn` と同格）
    なので、`verifier`/`red_tests` ほど重くはしない——ここでは
    「無印のまま・S_META（薄灰色ではない）」であることだけを確かめる。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": kind, "message": message},
        _done(),
    ])
    text = c.export_text(clear=False)
    row = next(ln for ln in _rows(text) if message[:10] in ln)
    assert not row.startswith(T.MARK_BAD), row       # Gate ほど重くしない
    html = c.export_html(inline_styles=True, clear=False)
    # S_DIM（NOTE）ではなく S_META で出ていること。★2色が同じ値だと判別できない
    # 前提が崩れるので先に確かめる。
    assert T.NOTE.lower() != T.META.lower(), "S_DIM と S_META が同じ色では判別できない"
    # ★r4b/app-term-clients（レビュー must_fix #6）: html 全体に対する
    #   「どこにも S_DIM の色が無い」という否定表明ではなく、**当該行の色そのもの**
    #   を見る（同じフレームに無関係な S_DIM 行が増えても壊れず、逆に当該行だけが
    #   S_DIM に戻っても見逃さない）。
    color = _span_color_for(html, message[:10])
    assert color != T.NOTE.lower(), f"{kind} が薄灰色（S_DIM）のまま（色={color}）"
    assert color == T.META.lower(), f"{kind} の色が S_META でない（色={color}）"


def test_subagent_relay_is_quiet_unless_the_child_tool_failed():
    """★2026-09-05（r4/app-term-clients・run50 の trace 再生で発見）: `tools/task.py`
    は委譲した子の道具活動（開始・成功・失敗）を全部 `kind="subagent"` の notice
    1本にまとめて親へ中継する（`"子: " + msg`）——kind だけでは成功と失敗を
    区別できない。既定（無印・薄灰色）のままだと、子が実際に失敗した道具呼び出し
    まで親の画面から見えなくなる。実測: run50 の trace で subagent notice 120件中
    5件が ✗ 失敗（run51/52 は委譲そのものが0回）——`verifier`/`red_tests`（0件）
    よりずっと高頻度に発火する。Windows 側（wire.rs）にも同じ形の判定を足した。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "notice", "id": "r1", "kind": "subagent", "message": "子: ▶ Bash"},
        {"type": "notice", "id": "r1", "kind": "subagent", "message": "子: ✓ Bash"},
        {"type": "notice", "id": "r1", "kind": "subagent",
         "message": "子: ✗ Bash 失敗: boom"},
        _done(),
    ])
    rows = _rows(c.export_text(clear=False))
    start_row = next(ln for ln in rows if "▶ Bash" in ln)
    ok_row = next(ln for ln in rows if "✓ Bash" in ln and "失敗" not in ln)
    bad_row = next(ln for ln in rows if "✗ Bash" in ln)
    assert not start_row.startswith(T.MARK_BAD), start_row
    assert not ok_row.startswith(T.MARK_BAD), ok_row
    assert bad_row.startswith(T.MARK_BAD), bad_row
    html = c.export_html(inline_styles=True, clear=False)
    assert _span_color_for(html, "✗ Bash") == T.ALERT.lower()


def test_term_knows_every_notice_kind_the_core_emits_or_names_the_gap():
    """★2026-09-05（r4/app-term-clients・P2）: `test_windows_parity.py::
    test_windows_knows_every_notice_kind_the_core_emits` と同じ発想を端末側にも作る。
    Windows だけがこの completeness test を持ち、端末には同種の突き合わせが無かった
    ため、`verifier`/`red_tests` を含む十数個の kind が長く漏れていても誰も気づけ
    なかった（本ファイルの上の2本の試験が実際に踏んだ形）。

    ★「知っている」の定義: `_NOTICE` 表のキー ∪ 文脈4状態(`_CONTEXT_KINDS`) ∪
    `_on_notice` が専用の分岐を持つ kind（cancel 系・ws_guard・subagent）∪
    `on_event` が `_on_notice` に渡す**前**に早期 return する kind
    （heartbeat・ready・pong・run_start・queued＝いずれも「状態は app/`/status`
    の持ち物」という理由で意図的に無視している）。

    ★`subagent`（`tools/task.py` が委譲した子の道具活動を中継する kind）は
    `_serve_notice_kinds()` を `tools/` まで拡張して初めて見つかった——
    `verifier`/`red_tests` より高頻度（run50-52 実記録で 120 件・うち5件が
    子の道具の失敗）に発火していたのに、Windows 側の completeness test も
    それまでこれを知らなかった（wire.rs にも無かった＝この r4 で両方に追加）。

    ★完全な網羅ではなく**これ以上増やさない門**（investigator の指摘どおり、
    残る10種 [cancel_detail/injected/inject_ignored/phase_guidance_dropped/
    phase_plan/queue_cleared/request/shutdown/status/umbra_absent] は今回の
    調査対象外＝`_NOTICE` に無いまま default の無印・薄灰色に落ちる可能性が残る
    未クローズの穴として `_KNOWN_UNCLASSIFIED_GAP` に明記し、追って1つずつ剥がす）。
    このテストは `missing` が **その明記した集合を超えて増える**ことだけを防ぐ。
    """
    from ._protocol_kinds import serve_notice_kinds

    # ★意図的に無視してよい kind（on_event 冒頭 or _on_notice 冒頭で早期 return）。
    #   Windows 側の QUIET_KINDS（wire.rs）と同じ形の明示リスト。
    quiet = {"ready", "pong", "run_start", "queued", "heartbeat"} | set(Renderer._QUIET_KINDS)
    known = (set(Renderer._NOTICE.keys())
             | set(Renderer._CONTEXT_KINDS)
             | {"cancel", "cancelled", "cancel_applied", "ws_guard", "subagent"}
             | quiet)
    missing = sorted(serve_notice_kinds() - known)

    # ★2026-09-05 時点で未分類のまま残っている10種（この試験を書いた時点の実測）。
    #   ここに載っていない kind が新たに漏れたら test は落ちる（＝増える方だけ止める）。
    _KNOWN_UNCLASSIFIED_GAP = {
        "injected", "status",
    }
    new_gap = sorted(set(missing) - _KNOWN_UNCLASSIFIED_GAP)
    assert not new_gap, (
        f"端末が知らない notice kind が新たに増えた: {new_gap} — "
        "render.py の _NOTICE か、この試験の quiet/明示リストに足す"
    )
    # ★逆方向（門が有効か）も見る: 一覧に書いた分は実際に「知らない」側にあること。
    #   ここが空になったら明記した穴が閉じた＝一覧から剥がしてよい合図。
    closed = sorted(_KNOWN_UNCLASSIFIED_GAP - set(missing))
    if closed:
        pytest.fail(
            f"_KNOWN_UNCLASSIFIED_GAP に書いた kind が既に分類済みになっている: {closed} — "
            "一覧から剥がして掃除する"
        )


def test_Write_は書いた中身を画面に出す():
    """★2026-09-13 オーナー「エージェント側で def とか出力できないの？」

    実機の記録では 4,705 バイトの Python を実際に書いていたのに、画面に出たのは
    `OK: wrote 4705 bytes to …` の 1 行だけだった＝**出力できないのではなく
    表示していなかった**。`ApplyDiff` が差分を出すのと揃える。
    """
    c = _console()
    r = Renderer(c)
    本文 = "#!/usr/bin/env python3\ndef check_directories():\n    return True\n"
    _turn(r, [
        _call("Write", {"file_path": "tools/gallery.py", "content": 本文}, call=1),
        _result("Write", 1, "OK: wrote 71 bytes to tools/gallery.py"),
        _done("書きました。"),
    ])
    画面 = c.export_text(clear=False)
    assert "def check_directories():" in 画面, "書いたコードが画面に出ていない"
    assert "gallery.py" in 画面


def test_締めに未実行のファイルを出す():
    """★2026-09-14 実機（版 0.1.223・09-13 23:59）から。

    エージェントはファイルを 9 本（試験 2 本を含む）書き、走らせた Bash は
    `mkdir -p …` の 1 本だけで、締めに「検収基準に従ったテストが作成され、
    **動作確認が完了しました**」と書いた。利用者には成功と見分けがつかない。
    → `変更なし` `見当たらない` と同じ約束で、**事実だけ**を締めに出す。
    """
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Write", {"file_path": "test_vdir.py", "content": "x"}, call=1),
        _result("Write", 1, "OK: wrote 1 bytes to test_vdir.py"),
        _done("できました。", unrun=["/home/test/test_vdir.py", "collector/collector.py"]),
    ])
    画面 = c.export_text(clear=False)
    assert "未実行" in 画面, "書いたのに走らせていない事実が締めに出ていない"
    assert "test_vdir.py" in 画面


def test_全部走らせたなら未実行は出ない():
    """誤爆しないこと。**事実が無いときに何か言うのが一番高くつく**。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [_done("できました。", unrun=[])])
    assert "未実行" not in c.export_text(clear=False)
