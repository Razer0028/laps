"""SystemState: 何も観測できなかった回を「成功」と言わないこと（2026-09-02）。

★見つけ方（原因の形で grep した）: `remote_read` は 2026-09-01 に
  「90 秒待って何も得ていない。これは成功ではない」と `ToolResult(ok=False)` を
  返すよう直してある。同じ形の `system_state` は素の文字列のままだった。
  `Error:` で始まらない文字列は `tool_failed` の語彙表に当たらないので、
  **タイムアウトも「実行できない」も ok=True（画面に緑）** で記録されていた。

  実測（直す前）:
      as_result("[SystemState disk] タイムアウト（30秒）") → ok=True kind=ok

★分母: SystemState は実機の記録で 197 回呼ばれている（Bash / Read / TodoWrite に
  次ぐ 6 番目・全 3,072 回中）。常用の道具の失敗が緑で記録されると、
  失敗率そのものが測れなくなる。
"""
from __future__ import annotations

import subprocess

from laps.tools import as_result
from laps.tools import system_state as S


def test_タイムアウトは失敗として名乗る(monkeypatch) -> None:
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="df", timeout=30)

    monkeypatch.setattr(S.subprocess, "run", _boom)
    out = S._run({"verb": "disk"})
    assert getattr(out, "ok", True) is False
    assert getattr(out, "kind", "") == "error"
    assert as_result(out).ok is False


def test_実行できないのも失敗として名乗る(monkeypatch) -> None:
    """例: Windows に `df` が無い。観測できていないのは同じこと。"""
    def _boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "df")

    monkeypatch.setattr(S.subprocess, "run", _boom)
    out = S._run({"verb": "disk"})
    assert getattr(out, "ok", True) is False
    assert "実行できません" in out


def test_ゼロ件は失敗ではない(monkeypatch) -> None:
    """★やり過ぎない。「failed ユニットが 0 件」は正しく得られた観測。"""
    class _P:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: _P())
    out = S._run({"verb": "failed_units"})
    assert as_result(out).ok is True
    assert "(出力なし＝該当ゼロ件)" in out
