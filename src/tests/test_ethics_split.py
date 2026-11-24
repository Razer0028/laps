"""倫理の刻みと「現場の安全」を分ける（2026-08-29・オーナーの観察から）。

★分ける前: 刻みを raw にすると、慎重さと一緒に**破壊操作の確認まで消えていた**。
  オーナーが外したいのは倫理の側で、破壊操作の確認は**ラボを守る仕組み**。
  混ぜてあると「無検閲にすると危険になる」という誤った取引を強いる。
"""
from __future__ import annotations

import pytest

from laps import ethics


def _block(monkeypatch, lv: str) -> str:
    monkeypatch.setenv("LAPS_ETHICS", lv)
    return ethics.block()


@pytest.mark.parametrize("lv", ["strict", "standard", "direct", "raw"])
def test_destructive_confirmation_survives_every_level(monkeypatch, lv):
    b = _block(monkeypatch, lv)
    assert "破壊的操作" in b, f"{lv} でラボの保護が消えている"
    assert "バックアップ" in b


@pytest.mark.parametrize("lv", ["strict", "standard", "direct", "raw"])
def test_honesty_survives_every_level(monkeypatch, lv):
    b = _block(monkeypatch, lv)
    assert "検証なしに" in b or "でっち上げ" in b


def test_raw_drops_the_caution_but_keeps_safety(monkeypatch):
    raw = _block(monkeypatch, "raw")
    std = _block(monkeypatch, "standard")
    assert len(raw) < len(std), "raw が縮んでいない"
    assert "スコープの確認" not in raw, "raw で第三者スコープの縛りが残っている"
    assert "破壊的操作" in raw


def test_levels_are_ordered_by_length(monkeypatch):
    sizes = [len(_block(monkeypatch, lv)) for lv in ("raw", "direct", "standard", "strict")]
    assert sizes == sorted(sizes), f"刻みの大小が逆転している: {sizes}"


def test_ops_safety_file_exists():
    from pathlib import Path
    p = ethics._prompt_dir() / "ops-safety.md"
    assert p.exists(), "現場の安全のファイルが無い"
    body = p.read_text(encoding="utf-8")
    assert "破壊的操作" in body
    # ★本文に設計意図（刻みの話）を書かない: モデルに刻みの存在を教えない規律
    #   （test_ethics.py::test_block_never_leaks_the_dial_to_the_model）。
    #   私は一度これを破って落とされた。意図は laps/ethics.py 側に書く。
    for word in ("刻み", "raw", "standard"):
        assert word not in body, f"本文に刻みの話が混ざっている（{word}）"
