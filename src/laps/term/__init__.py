"""BLACKWALL 端末クライアント（Linux/macOS）。

★これは `laps.serve`（JSON 行プロトコル）の**クライアント**であって、エンジンではない。
  本体・道具・policy・ethics・締め・差分はすべて serve から流れてくる。ここは描くだけ。
設計: /home/user/project/DESIGN-linux-term.md
"""
from __future__ import annotations

# ★版の決め方は `laps/_version.py` に一本化した（2026-09-09）。
#   ここにあった `_source_version()` が正しい実装で、`laps/__init__.py` 側が
#   "0.1.0" の直書きで腐っていた。写しを2つ置くと片方だけ古びて気づけないので、
#   実体を1本にして両方がそれを呼ぶ。振る舞いは変えていない。
from .._version import resolve as _resolve_version

__all__ = ["__version__"]

__version__ = _resolve_version()
