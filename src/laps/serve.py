"""`python -m laps.serve` の入口（アプリ・端末版・スマホの中継・ゲートウェイが起こす）。

★2026-11-19 旧い中核（oc/loop.py の OcAgent と、ここにあった 3,200 行の旧い窓口）を外した
（オーナー「Blackwall の強化案… 進めて」）。2026-10-30 から走行は新しい中核（laps/engine）だけで、
旧い方は LAPS_ENGINE=legacy のときしか動いていなかった。窓口の実体は laps/engine/server.py。
"""
from __future__ import annotations

from .engine.server import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
