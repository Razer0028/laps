"""検収の不合格に「実際は何が返ったか」を添える（2026-08-27・run29 の解剖）。

★run29 の実測: 3 試行 4.2 時間の失敗証拠 1,343 字に、実体である `500` も
  `SQLite3 extension not available` も**一度も含まれていなかった**。
  検収は `curl -s -o /dev/null -w "%{http_code}" … | grep -q 200` で本文も状態も捨て、
  しかも別の検収 `curl … | python3 -m json.tool` は**500 のエラー JSON も JSON なので合格**。
  モデルは「何が起きているか」を教えられないまま同じ課題を 3 回渡された。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    # 本番の工程走行は --policy trusted。検収は bash を通すので同じ条件で測る。
    monkeypatch.setenv("LAPS_POLICY", "trusted")


def test_interactive_bash_keeps_the_context_lines():
    """対話中の道具呼び出しでは従来どおり（空振り再試行を止めた実測がある）。"""
    import os

    from laps.tools import bash
    os.environ.pop("LAPS_BASH_TERSE", None)
    out = str(bash.SPEC.handler({"command": "true"}))
    assert "cwd:" in out and "shell:" in out


def test_repeated_empty_output_drops_the_boilerplate_and_counts():
    """★実測（run34）: 定型文つき結果 381,097 字のうち **95%（362,308 字）が2回目以降**。
    1つのコマンドは同じ空結果を **139 回**返していた。初回は full、以降は1行＋回数。"""
    import os

    from laps.tools import bash
    os.environ.pop("LAPS_BASH_TERSE", None)
    bash._EMPTY_SEEN.clear()
    first = str(bash.SPEC.handler({"command": "true"}))
    assert "cwd:" in first, "初回は文脈を出す（空振り再試行を止めた実測がある）"
    second = str(bash.SPEC.handler({"command": "true"}))
    assert "cwd:" not in second, "2回目も定型文を出している"
    assert "2 回目" in second, "何回目かを言っていない（回数そのものが合図）"
    third = str(bash.SPEC.handler({"command": "true"}))
    assert "3 回目" in third


def test_different_commands_each_get_their_first_full_result():
    import os

    from laps.tools import bash
    os.environ.pop("LAPS_BASH_TERSE", None)
    bash._EMPTY_SEEN.clear()
    a = str(bash.SPEC.handler({"command": "true"}))
    b = str(bash.SPEC.handler({"command": "true "}))     # 空白違いは同じ扱い
    c = str(bash.SPEC.handler({"command": "/bin/true"}))  # 別コマンド
    assert "cwd:" in a and "cwd:" not in b and "cwd:" in c
