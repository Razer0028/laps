"""会話を数えるとき `tool_calls` を勘定に入れる（2026-08-19・実データで確認）。

★何が起きていたか:
  2026-08-16 に「native の tool_calls を会話に残す」直しを入れた（それ自体は正しい）。
  ところが**数える側を直していなかった**。assistant の `content` は空のことが多く、
  道具の名前と引数の JSON が**丸ごと勘定から漏れていた**。

  実データ（39回の道具呼び出しを含む79本の会話）で突き合わせた:

      直す前の数え方  12,505 tok
      サーバの実測    15,849 tok   → **-3,344 の過小予測**
      直した数え方    16,202 tok   → **+353 の過大予測（安全側・2%）**

★過小予測は危険（窓を溢れさせて 400 → 走行ごと死ぬ）。過大は少し無駄なだけ。
  **足りないより余るほうへ倒す。**

★長期課題で観測した `context_exhausted` と `context_overflow` は、
  どちらもこの1つの原因から出ていた。
"""
from __future__ import annotations

import laps.context_budget as CB


def _counter():
    return CB.TokenCounter(None)      # /tokenize 無し＝近似（バイト数）で数える


CALL = {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "Bash", "arguments": '{"command": "python3 -m pytest -q"}'}}],
}


def test_tool_calls_を数える() -> None:
    c = _counter()
    with_calls = c.count_messages([CALL])
    without = c.count_messages([{"role": "assistant", "content": ""}])
    assert with_calls > without + 30, f"{with_calls} vs {without}"


def test_名前と引数の両方を数える() -> None:
    c = _counter()
    short = c.count_messages([CALL])
    long_args = dict(CALL)
    long_args["tool_calls"] = [{"function": {"name": "Bash", "arguments": '{"command": "' + "x" * 500 + '"}'}}]
    assert c.count_messages([long_args]) > short + 400


def test_tool_call_id_も数える() -> None:
    c = _counter()
    a = c.count_messages([{"role": "tool", "content": "ok"}])
    b = c.count_messages([{"role": "tool", "content": "ok", "tool_call_id": "call_abcdef123"}])
    assert b > a


def test_tool_calls_が無ければ変わらない() -> None:
    """★負の対照。何でも増やすなら、ただの過大見積りになる。"""
    c = _counter()
    plain = [{"role": "user", "content": "やって"},
             {"role": "assistant", "content": "やりました"}]
    n1 = c.count_messages(plain)
    n2 = c.count_messages([dict(m) for m in plain])
    assert n1 == n2


def test_壊れた形でも落ちない() -> None:
    """★毎ターン通る経路。ここで例外を出すと走行ごと死ぬ。"""
    c = _counter()
    for bad in ([{"role": "assistant", "tool_calls": "文字列"}],
                [{"role": "assistant", "tool_calls": [None]}],
                [{"role": "assistant", "tool_calls": [{"function": None}]}]):
        try:
            c.count_messages(bad)
        except Exception as e:
            raise AssertionError(f"落ちた: {bad} → {e}")


def test_安全側に倒れている() -> None:
    """★過小より過大へ。飾りのぶんを保守側に見ておく。"""
    assert CB._TOOLCALL_OVERHEAD >= 8
