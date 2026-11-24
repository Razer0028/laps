"""★2026-10-18 Qwen3.6 の調整値を作業のモードで選ぶ（backend.qwen36_sampling）。推論機は使わない。

オーナー「長期タスクでの内部思考の繰り返しなども非常に多く散見されている」。作者のカードの「一般の思考」（温度 1.0・
presence 1.5＝終わらない繰り返しを減らす）と「精密なコーディング」（0.6・0）を、開発モードとそれ以外で使い分ける。
"""
from __future__ import annotations

import pytest

from laps import backend as B


@pytest.fixture
def qwen36(monkeypatch):
    monkeypatch.setattr(B, "model_family", lambda *a, **k: "qwen")
    monkeypatch.setattr(B, "model_name", lambda *a, **k: "qwen3.6-35b-a3b-q4_k_m.gguf")
    monkeypatch.setattr(B, "thinking_enabled", lambda: True)
    for k in ("LAPS_MODE", "LAPS_QWEN_SAMPLING", "LAPS_TEMPERATURE", "LAPS_PRESENCE_PENALTY"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_development_mode_uses_the_coding_values(qwen36):
    qwen36.setenv("LAPS_MODE", "development")
    assert B.qwen36_sampling() == "coding"
    assert B.temperature() == 0.6 and B.sampling_params()["presence_penalty"] == 0.0


@pytest.mark.parametrize("mode", [None, "general", "analysis", "modeling"])
def test_other_modes_use_the_general_values(qwen36, mode):
    if mode:
        qwen36.setenv("LAPS_MODE", mode)
    assert B.qwen36_sampling() == "general"
    assert B.temperature() == 1.0 and B.sampling_params()["presence_penalty"] == 1.5


def test_switch_and_explicit_values_win(qwen36):
    qwen36.setenv("LAPS_QWEN_SAMPLING", "coding")
    assert B.temperature() == 0.6 and B.sampling_params()["presence_penalty"] == 0.0
    qwen36.setenv("LAPS_PRESENCE_PENALTY", "0.7")
    assert B.sampling_params()["presence_penalty"] == 0.7


def test_other_models_are_unchanged(qwen36):
    qwen36.setattr(B, "model_name", lambda *a, **k: "qwen3.8-27b.gguf")
    assert B.sampling_params()["presence_penalty"] == 0.0 and B.temperature() == 1.0
