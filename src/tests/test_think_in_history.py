"""自分の思考を履歴へ戻す（2026-09-21・goose との通信を中継で突き合わせて決めた）。

★実測（同じ課題・同じ推論機・要求を記録して比較）:
    goose     : assistant **32/32 通**が `reasoning_content` を持つ（計 7,548 字）
    BLACKWALL : **0 通**
★goose のモデルは「もう Win64 は見た」を自分の言葉で覚えている。
  こちらは毎回ゼロから同じ結論を導き直していた＝反復の正体。
"""
from laps.oc.base import _THINK_KEEP_CHARS, strip_think, take_think


def test_思考だけを取り出す():
    r = "前<think>まず Win64 を見る</think>後"
    assert take_think(r) == "まず Win64 を見る"
    assert strip_think(r) == "前後"          # 本文側は今までどおり混ざらない


def test_複数の塊は繋ぐ():
    assert take_think("<think>a</think>x<think>b</think>") == "a\nb"


def test_思考が無ければ空():
    assert take_think("ただの本文") == ""


def test_長い思考は末尾を残して畳む():
    """★結論は後ろにある。頭ではなく尻を残す。"""
    body = "あ" * (_THINK_KEEP_CHARS + 500) + "結論はこれ"
    out = take_think(f"<think>{body}</think>")
    assert out.endswith("結論はこれ")
    assert out.startswith("…（前略）…")
    assert len(out) <= _THINK_KEEP_CHARS + 10

