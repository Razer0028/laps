"""Read の上限が `limits` を**通る**こと（写しを作らない）。

★2026-09-13 に実際に踏んだ: `read.py` が `limits.out_cap` の中身（`int(env)//4*2`）を
  **直書きで写していた**。`limits.py` の docstring は「道具が返してよい量を 1 箇所で決める」と
  宣言していたのに、**いちばん量を返す Read だけがその 1 箇所を通っていなかった**。
  実害: 割合を可変にする `LAPS_OUT_CAP_FRACTION` が Read に効かず、
  圧縮回数を下げる A/B が**同じ挙動どうしの比較**になっていた（気づかずに 2 本測った）。
"""
from __future__ import annotations

import inspect

from laps.tools import limits, read


def test_read_は_limits_を通る(monkeypatch):
    """★字面ではなく**振る舞い**で見る（コメントに式が書いてあっても通るように）。

    どの組み合わせでも `limits.out_cap` と一致するなら、写しは残っていない。
    """
    src = inspect.getsource(read._text_cap)
    assert "limits.out_cap" in src, "写しが戻っている（1 箇所で決める約束が破れている）"
    for n_ctx in ("", "8192", "16384", "32768", "131072"):
        for frac in ("", "0.05", "0.10", "0.25", "0.50", "zzz"):
            if n_ctx:
                monkeypatch.setenv("LAPS_N_CTX", n_ctx)
            else:
                monkeypatch.delenv("LAPS_N_CTX", raising=False)
            if frac:
                monkeypatch.setenv("LAPS_OUT_CAP_FRACTION", frac)
            else:
                monkeypatch.delenv("LAPS_OUT_CAP_FRACTION", raising=False)
            assert read._text_cap() == limits.out_cap(read._MAX_TEXT_CHARS), (n_ctx, frac)


def test_割合の環境変数が_read_に効く(monkeypatch):
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    monkeypatch.delenv("LAPS_OUT_CAP_FRACTION", raising=False)
    base = read._text_cap()
    monkeypatch.setenv("LAPS_OUT_CAP_FRACTION", "0.05")
    small = read._text_cap()
    assert small < base, (base, small)
    assert small == limits.out_cap(read._MAX_TEXT_CHARS)


def test_窓が分からなければ従来値(monkeypatch):
    """★保守側に倒さない（振る舞いを変えない）という既存の約束を守る。"""
    monkeypatch.delenv("LAPS_N_CTX", raising=False)
    assert read._text_cap() == read._MAX_TEXT_CHARS


def test_読めない割合では既定に戻る(monkeypatch):
    """★既定値そのものを書かない（既定を変えたら試験も直す、になるのを避ける）。"""
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    monkeypatch.delenv("LAPS_OUT_CAP_FRACTION", raising=False)
    base = read._text_cap()
    for bad in ("", "zzz", "5", "-1", "0", "0.001", "0.95"):
        monkeypatch.setenv("LAPS_OUT_CAP_FRACTION", bad)
        assert read._text_cap() == base, bad


def test_既定は_窓の1割(monkeypatch):
    """★2026-09-13 に 0.25 → 0.10 へ下げた（圧縮が頻発して体感が悪かった）。
    戻したいときはここと `DEFAULT_OUT_CAP_FRACTION` の根拠コメントを一緒に読むこと。"""
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    monkeypatch.delenv("LAPS_OUT_CAP_FRACTION", raising=False)
    assert read._text_cap() == 6553
