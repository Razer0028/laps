# -*- coding: utf-8 -*-
"""差し戻し／作り直しのあと、端末に**同じ答えが並ばない**こと。

★実機（2026-08-30・session 20260830_183829_f36fba）:
  差し戻し 2 回＋停滞 Reset 2 回で**同じ答えが 5 回**生成され、Linux 端末版は
  その全部を印字した。`_body_text` を吐き出すのは道具呼び出しか通知のときだけで、
  iter 8 以降は道具を呼んでいなかったため、2 本・3 本とまとめて出た。

  serve.py は生成の切れ目に `iter_start` を出しており、Windows 側
  （app/src-tauri/src/sidecar.rs）は `[fabrication N]` を作り直しの合図に
  していたが、**端末版だけこの配線が無かった**。

  さらに停滞 Reset は `_emit` を1つも出しておらず、どちらの画面にも
  「作り直した」合図が届いていなかった（agent.py 側で `[reset N]` を出すように直した）。

★本文の欠落を作らないこと: 捨ててよいのは「次の生成に置き換えられる試作」だけ。
"""
from __future__ import annotations

import io

from rich.console import Console

from laps.term.render import Renderer


def _console() -> Console:
    return Console(record=True, width=80, force_terminal=True, file=io.StringIO())


def _turn(r: Renderer, events: list[dict]) -> dict:
    r.start_turn("r1")
    for ev in events:
        r.on_event(ev)
    return r.end_turn()


ANSWER = "192.0.2.251 は落ちていません。TCP ポート 22 は開いています [7]"


def _notice(msg: str) -> dict:
    return {"type": "notice", "id": "r1", "kind": "harness", "message": msg}


def _done(text: str) -> dict:
    return {"type": "done", "id": "r1", "outcome": "accepted", "text": text,
            "secs": 1.0, "tok_s": 40.0, "tool_calls": {}}


def test_差し戻しで作り直した試作を画面に残さない():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "token", "id": "r1", "text": ANSWER},
        _notice("[fabrication 1] 裏付けのない数値を差し戻し"),
        {"type": "token", "id": "r1", "text": ANSWER},
        _done(ANSWER),
    ])
    text = c.export_text(clear=False)
    assert text.count("落ちていません") == 1, text
    assert "[fabrication 1]" in text


def test_停滞リセットでも試作を残さない():
    """`[reset N]` は 2026-08-30 に新設した合図。これが無いと同じ答えが並ぶ。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "token", "id": "r1", "text": ANSWER},
        _notice("[reset 1] 同じ返答が続いたので、視点を変えさせます"),
        {"type": "token", "id": "r1", "text": ANSWER},
        _done(ANSWER),
    ])
    assert c.export_text(clear=False).count("落ちていません") == 1


def test_実機の順序_5生成2差し戻し2リセットで本文は1回だけ():
    c = _console()
    r = Renderer(c)
    out = _turn(r, [
        {"type": "token", "id": "r1", "text": ANSWER},          # iter 8
        _notice("[fabrication 1] 裏付けのない数値を差し戻し"),
        {"type": "token", "id": "r1", "text": ANSWER},          # iter 9
        _notice("[reset 1] 同じ返答が続いたので、視点を変えさせます"),
        {"type": "token", "id": "r1", "text": ANSWER},          # iter 10
        _notice("[fabrication 2] 裏付けのない数値を差し戻し"),
        {"type": "token", "id": "r1", "text": ANSWER},          # iter 11
        _notice("[reset 2] 同じ返答が続いたので、視点を変えさせます"),
        {"type": "token", "id": "r1", "text": ANSWER + " 追記"},  # iter 12
        _done(ANSWER + " 追記"),
    ])
    text = c.export_text(clear=False)
    assert text.count("落ちていません") == 1, text
    assert "追記" in text
    assert out["text"].count("落ちていません") == 1


def test_ふつうの通知は本文を捨てない():
    """作り直しの印を持たない通知（文脈の整理など）で本文を消さない。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "token", "id": "r1", "text": "前半の報告。"},
        _notice("★文脈が足りなくなったので、ここで打ち切りました"),
        {"type": "token", "id": "r1", "text": "後半の報告。"},
        _done("前半の報告。後半の報告。"),
    ])
    text = c.export_text(clear=False)
    assert "前半の報告。" in text
    assert "後半の報告。" in text


def test_道具の前に確定した本文は差し戻しでも消えない():
    """★本文の欠落を作らない。道具呼び出しで確定印字した分は既に画面にあり、
    あとから来た差し戻しで消えてはいけない。"""
    c = _console()
    r = Renderer(c)
    _turn(r, [
        {"type": "token", "id": "r1", "text": "調べます。"},
        {"type": "tool_call", "id": "r1", "name": "Bash", "input": {"command": "true"}, "call": 1},
        {"type": "tool_result", "id": "r1", "name": "Bash", "call": 1, "result": "open",
         "ok": True, "kind": "ok", "chars": 4, "truncated": False, "summary": None},
        {"type": "token", "id": "r1", "text": ANSWER},
        _notice("[fabrication 1] 裏付けのない数値を差し戻し"),
        {"type": "token", "id": "r1", "text": ANSWER},
        _done(ANSWER),
    ])
    text = c.export_text(clear=False)
    assert "調べます。" in text, text
    assert text.count("落ちていません") == 1, text
