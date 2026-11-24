"""★`--debug` は既定 on（2026-09-03・W8 監査: 診断は実装済みなのに未指定で「無かったこと」になっていた）。"""
from __future__ import annotations

from pathlib import Path

from laps.term.app import default_debug_path


def test_unspecified_debug_defaults_to_home_log(monkeypatch):
    monkeypatch.delenv("LAPS_TERM_DEBUG", raising=False)
    assert default_debug_path(None) == str(Path.home() / ".blackwall" / "term-debug.log")


def test_explicit_path_is_kept_and_empty_means_off(monkeypatch):
    monkeypatch.delenv("LAPS_TERM_DEBUG", raising=False)
    assert default_debug_path("/tmp/x.log") == "/tmp/x.log"
    assert default_debug_path("") is None


def test_env_off_disables_the_default(monkeypatch):
    monkeypatch.setenv("LAPS_TERM_DEBUG", "off")
    assert default_debug_path(None) is None
    monkeypatch.setenv("LAPS_TERM_DEBUG", "0")
    assert default_debug_path(None) is None
    assert default_debug_path("/tmp/y.log") == "/tmp/y.log"     # 明示は env より強い
