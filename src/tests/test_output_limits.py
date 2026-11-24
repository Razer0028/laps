"""道具が返す量は、推論器の窓に対して相対に決まる。

★実機で踏んだ形（2026-08-19）: WebFetch が固定 50,000 文字の上限を持っていたが、
  窓 16,384 トークンの推論器では **50,000 文字は窓そのものより大きい**。
  1回の道具呼び出しで会話が入らなくなり、走行が context_overflow で終わった。
  調べると上限の考え方が道具ごとにばらばらで、WebSearch には上限が無かった。
"""
from __future__ import annotations

import pytest

from laps.tools import limits


def test_no_window_keeps_the_old_behaviour(monkeypatch):
    """★窓が分からないときは従来値。振る舞いを勝手に変えない。"""
    monkeypatch.delenv("LAPS_N_CTX", raising=False)
    for ceiling in (20000, 30000, 50000):
        assert limits.out_cap(ceiling) == ceiling


@pytest.mark.parametrize("n_ctx,want", [
    # ★2026-09-13: 期待値を**既定の割合から導く**（0.25→0.10 に下げたとき、ここが
    #   literal だったせいで 6 本落ちた。定数を二重に持つと必ず片方だけ古くなる）。
    (16384, None),      # 実機の窓。従来の 50,000 より遥かに小さい
    (32768, None),
    (131072, None),     # 天井（各道具の従来値）は超えない
])
def test_cap_follows_the_window(monkeypatch, n_ctx, want):
    monkeypatch.setenv("LAPS_N_CTX", str(n_ctx))
    expect = max(4000, min(50000, int(n_ctx * limits.DEFAULT_OUT_CAP_FRACTION * 2)))
    assert limits.out_cap(50000) == expect, (n_ctx, expect)


def test_floor_is_not_crossed(monkeypatch):
    """窓が極端に小さくても、道具が何も返せない状態にはしない。"""
    monkeypatch.setenv("LAPS_N_CTX", "1024")
    assert limits.out_cap(50000) == 4000


def test_garbage_window_is_ignored(monkeypatch):
    for bad in ("", "0", "-5", "ごみ", "16k"):
        monkeypatch.setenv("LAPS_N_CTX", bad)
        assert limits.out_cap(30000) == 30000


def test_note_says_it_was_cut():
    """★黙って切らない。切ると「全部読めた」と思い込む。"""
    n = limits.note(120_000, 8192, "n を小さくしてください。")
    assert "切りました" in n and "120,000" in n and "8,192" in n


def test_every_content_tool_has_a_cap():
    """★次に道具を足す人が同じ穴を開けないための門。

    外から来る文字列を返す道具は、必ず limits を通すこと。
    """
    import inspect
    from laps.tools import bash, grep, webfetch, websearch
    for mod in (bash, grep, webfetch, websearch):
        src = inspect.getsource(mod)
        assert "limits.out_cap" in src, f"{mod.__name__} に上限がありません"
