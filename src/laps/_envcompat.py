"""環境変数の名前の読み替え（`BLACKWALL_*` → `LAPS_*`・2026-11-14）。

正式名称を L.A.P.S に変えたので、コードが読む・書く名前は `LAPS_*`。
旧い `BLACKWALL_*` を設定している環境（systemd の unit・利用者の機械・ほかの台本）も
今までどおり動くように、`laps/__init__.py` のいちばん最初で `apply()` を 1 回呼ぶ:

- `LAPS_X` があれば `BLACKWALL_X` に同じ値を入れる（**LAPS が勝つ**）。
- `LAPS_X` が無く `BLACKWALL_X` があれば、`LAPS_X` に同じ値を入れる。
- 空文字の値もそのまま写す（「空＝切る」の意味のスイッチがある）。

こうしておけば、子プロセス（Bash の道具・サブエージェント・旧い版の部品）はどちらの名前でも読める。

走行の途中でコードが環境変数を書く・消す所は `setenv`・`setdefault`・`unsetenv` を通す
（旧い名前にも同じ値を書く・両方消す）。片方だけ書くと、旧い名前で読む子に古い値が残り、
片方だけ消すと、子の `apply()` が旧い名前から値を戻してしまう。

★ここは標準ライブラリの os しか読まない（`laps` のどの部品よりも先に動くため）。
"""
from __future__ import annotations

import os
from typing import MutableMapping, Optional

NEW = "LAPS_"
OLD = "BLACKWALL_"


def suffix(name: str) -> str:
    """`LAPS_X`・`BLACKWALL_X`・`X` のどれで渡しても `X` を返す。"""
    if name.startswith(NEW):
        return name[len(NEW):]
    if name.startswith(OLD):
        return name[len(OLD):]
    return name


def names(name: str) -> tuple[str, str]:
    """（新しい名前, 旧い名前）。"""
    s = suffix(name)
    return NEW + s, OLD + s


def apply(env: Optional[MutableMapping[str, str]] = None) -> None:
    """新旧の名前を写し合う（LAPS が勝つ）。既定は `os.environ`。何度呼んでもよい。"""
    env = os.environ if env is None else env
    keys = list(env.keys())
    for k in keys:
        if k.startswith(NEW) and len(k) > len(NEW):
            v = env[k]
            o = OLD + k[len(NEW):]
            if env.get(o) != v:
                env[o] = v
    for k in keys:
        if k.startswith(OLD) and len(k) > len(OLD):
            n = NEW + k[len(OLD):]
            if n not in env:
                env[n] = env[k]


def setenv(name: str, value: str, env: Optional[MutableMapping[str, str]] = None) -> None:
    """新旧の両方の名前に同じ値を書く。"""
    env = os.environ if env is None else env
    new, old = names(name)
    env[new] = value
    env[old] = value


def setdefault(name: str, value: str, env: Optional[MutableMapping[str, str]] = None) -> str:
    """`LAPS_X` が無ければ両方に書く。在ればその値を旧い名前にも写す。効いている値を返す。"""
    env = os.environ if env is None else env
    new, old = names(name)
    if new in env:
        cur = env[new]
        if env.get(old) != cur:
            env[old] = cur
        return cur
    env[new] = value
    env[old] = value
    return value


def unsetenv(name: str, env: Optional[MutableMapping[str, str]] = None) -> Optional[str]:
    """新旧の両方の名前を消す。消す前の値（新しい名前が先）を返す。無ければ None。"""
    env = os.environ if env is None else env
    new, old = names(name)
    prev_old = env.pop(old, None)
    prev_new = env.pop(new, None)
    return prev_new if prev_new is not None else prev_old
