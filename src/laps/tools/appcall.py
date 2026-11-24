"""アプリ（Windows の Blackwall）に頼みごとを渡す窓口（2026-10-18）。

ask_user の set_asker と同じ形: serve.py が走行の間だけ `fn(op, args, timeout) -> dict` を差し込む。
None のまま＝アプリの外（端末・作業用のコンテナ・中継の走行）＝アプリに頼む道具は出さない。
★アプリは頼めることを走行の依頼の `app_caps`（例 ["browser"]）で名乗る。名乗っていない物は頼まない
  （古いアプリは知らない頼みに答えないので、道具が時間切れまで待ってしまう）。
"""
from __future__ import annotations

_CALLER = None
_CAPS: frozenset = frozenset()


def set_caller(fn, caps=()) -> None:
    global _CALLER, _CAPS
    _CALLER = fn
    _CAPS = frozenset(str(c) for c in (caps or ()))


def can(cap: str) -> bool:
    return _CALLER is not None and cap in _CAPS


def call(op: str, args: dict, timeout: float = 60.0) -> dict:
    """アプリに頼んで答えを待つ。答えは dict（ok・error・中身）。頼めないときは RuntimeError。"""
    fn = _CALLER
    if fn is None:
        raise RuntimeError("not running inside the Blackwall app")
    return fn(op, args, timeout)
