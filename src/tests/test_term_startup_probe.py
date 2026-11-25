"""起動時の確認と、モデル名の食い違い（2026-09-20）。

★画面が言うモデル名は設定から来る。llama-server は知らない名前でも黙って応えるので、
  設定が古いと**見出しだけが嘘をつく**。送り先が実際に出している名前と突き合わせる。
"""
from __future__ import annotations

import io

from rich.console import Console

from laps.term import app, slash


def test_合っていれば何も言わない():
    assert slash.model_mismatch("blackwall-coder", ["blackwall-coder"]) is None


def test_判断できないときは何も言わない():
    # ★取れなかった（None）を「食い違い」と読まない。分からないことを断定しない。
    assert slash.model_mismatch("x", None) is None
    assert slash.model_mismatch("", ["blackwall-coder"]) is None


def test_食い違いは次の一手つきで言う():
    m = slash.model_mismatch("blackwall-think-omega-j", ["blackwall-coder"])
    assert m and "blackwall-think-omega-j" in m and "/model blackwall-coder" in m


def test_複数出している送り先では名前を並べる():
    m = slash.model_mismatch("x", ["a", "b", "c", "d", "e"])
    assert m and "a・b・c・d" in m and "ほか" in m and "/model <名前>" in m


class _Ctx:
    def __init__(self):
        self.buf = io.StringIO()
        self.console = Console(file=self.buf, width=80, force_terminal=False, color_system=None)


def _run(monkeypatch, *, probe, served, model="blackwall-coder"):
    ctx = _Ctx()
    monkeypatch.delenv("LAPS_NO_STARTUP_PROBE", raising=False)
    monkeypatch.setattr(slash, "_settings", lambda c: {"backend": "http://x/v1", "model": model})
    monkeypatch.setattr(slash, "probe_backend", lambda b, timeout=0: probe)
    monkeypatch.setattr(slash, "served_models", lambda b, timeout=0: served)
    app._startup_probe(ctx)
    return ctx.buf.getvalue()


def test_問題が無ければ一行も出さない(monkeypatch):
    out = _run(monkeypatch, probe={"ok": True, "health": "ok", "error": ""}, served=["blackwall-coder"])
    assert out == "", "毎回出る行は読まれなくなる。問題が無いときは黙ること"


def test_届かないことは送る前に言う(monkeypatch):
    out = _run(monkeypatch, probe={"ok": False, "health": "", "error": "ConnectError: refused"}, served=None)
    assert "送り先に接続できません" in out and "refused" in out and "/backend" in out


def test_モデル名の食い違いを起動時に言う(monkeypatch):
    out = _run(monkeypatch, probe={"ok": True, "health": "ok", "error": ""},
               served=["coder"], model="other-model")
    assert "送り先にありません" in out and "/model coder" in out


def test_確認が落ちても対話は始まる(monkeypatch):
    ctx = _Ctx()
    monkeypatch.setattr(slash, "_settings", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    app._startup_probe(ctx)            # 例外を外へ出さない
    assert ctx.buf.getvalue() == ""


def test_止める手段がある(monkeypatch):
    ctx = _Ctx()
    monkeypatch.setenv("LAPS_NO_STARTUP_PROBE", "1")
    monkeypatch.setattr(slash, "probe_backend", lambda *a, **k: (_ for _ in ()).throw(AssertionError("叩いた")))
    app._startup_probe(ctx)
    assert ctx.buf.getvalue() == ""


def test_案内文は幅60の端末に収まる():
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(ch) in "WFA" else 1 for ch in app.PLACEHOLDER)
    assert w + len(app.PROMPT) <= 60


def test_長い知らせは続き行にも段が付く(monkeypatch):
    # ★Rich に任せると折り返しの 2 行目が桁 0 から始まり、見出しの続きに見える（実機で見た）。
    out = _run(monkeypatch, probe={"ok": True, "health": "ok", "error": ""},
               served=["coder"], model="other-model")
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) >= 2, "80 桁で 1 行に収まるなら、この試験の前提が変わっている"
    assert all(l.startswith("  ") for l in lines), lines
    assert all(len(l) == len(l.rstrip()) for l in lines), "末尾に空白を吐かない"
