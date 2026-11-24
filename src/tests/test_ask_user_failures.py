"""★2026-09-08 監査（tools high）: AskUserQuestion の「聞けなかった／中断／空」が ok=True の文字列で返り、
モデルが利用者の返事として読んでいた。Error として名乗り、agent の答えの記帳にも印を付けない。"""
import sys
from laps.tools import ask_user as AU
from laps.tools import ToolResult


def _run(inp, monkeypatch, headless=True):
    monkeypatch.setenv("LAPS_HEADLESS", "1" if headless else "0")
    return AU._run(inp)


def test_no_tty_is_an_error_not_an_answer(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.delenv("LAPS_YES", raising=False)
    out = _run({"question": "続けますか？", "options": [{"label": "はい"}, {"label": "いいえ"}]}, monkeypatch)
    assert isinstance(out, ToolResult) and out.ok is False and out.kind == "error"
    assert str(out).startswith("Error:") and "no-tty" in str(out) and "This is not the user's answer" in str(out)


