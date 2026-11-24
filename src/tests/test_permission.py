"""破壊的コマンドの確認ゲート（permission.py）。

★これは**安全境界ではない**（境界は policy.py）。利用者に一言確認するための
  仕掛けで、聞ける相手が居ないときの既定は拒否。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps import permission  # noqa: E402


class _NoTTY:
    """パイプの向こうから来た標準入力（サイドカー・cron・`|`）。"""

    def isatty(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", _NoTTY())
    monkeypatch.delenv("LAPS_YES", raising=False)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))   # settings.json を持たない場所
    yield


def test_trusted_では聞けなくても止めない(monkeypatch):
    """★出荷構成（GUI + policy=trusted）で `rm -rf build` が**理由も代案も無く**
    止まっていた（2026-09-02）。

    policy はもう `rm -rf` も `sudo` も通しているのに、この 13 本の正規表現が
    二段目の門として残り、GUI には聞く相手（tty）が居ないので必ず拒否になる。
    アプリは意図的に `LAPS_YES` を渡さない（sidecar.rs:1246）ので、
    利用者にはこれを解く手段が無かった。モードの申告と実装を揃える。
    """
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    assert permission.is_destructive("rm -rf build")
    assert permission.confirm("rm -rf build") is True


@pytest.mark.parametrize("mode", ["workspace", "readonly", "off"])
def test_trusted_以外では従来どおり拒否(monkeypatch, mode: str):
    """★逆側を壊さない。trusted 以外は「聞けないなら拒否」のまま。"""
    monkeypatch.setenv("LAPS_POLICY", mode)
    assert permission.confirm("rm -rf /var") is False


def test_設定の拒否は_trusted_より強い(monkeypatch, tmp_path):
    """利用者が明示的に禁じた形は、モードに関わらず拒否される
    （settings は利用者の意思、trusted は既定）。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    (tmp_path / "settings.json").write_text(
        '{"deny_patterns": ["rm -rf /var"]}', encoding="utf-8")
    assert permission.confirm("rm -rf /var") is False
