"""Blackwall — Standalone agent (Phase Ω-K).

Ω-J SFT model + Pi-6 framework patches inline.
"""
# ★いちばん最初に環境変数の名前を写す（2026-11-14・BLACKWALL_* ⇄ LAPS_*・LAPS が勝つ）。
#   ほかの import より前に置く（部品は import のときに環境変数を読む物がある）。包み `blackwall` 経由でも
#   `laps` が先に読まれるので、ここ 1 か所で足りる。
from . import _envcompat as _envcompat  # noqa: E402

_envcompat.apply()

# ★版は直書きしない（2026-09-09）。ここは 208 版ぶん古い "0.1.0" のままで、
#   `blackwall-legacy --version`（cli.py:163）がその値を名乗っていた。
#   実体は `_version.py` 1本（`laps/term/__init__.py` も同じものを使う）。
from ._version import resolve as _resolve_version

__version__ = _resolve_version()
