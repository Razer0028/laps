"""道具の結果が「型」を持つこと（成否・空・拒否を散文から推測しない）。

★敵対レビュー §1.3（2026-08-16）の中心の指摘:
この設計の最も深い問題は「何が起きたか」が一度も型になっていないこと。
成功か失敗か、空か未実行か、すべて `[n] 名前 → 生文字列` に潰され、
下流の3層（モデル・guard・画面）が**それぞれ散文から推測し直していた**。

実害の実例:
  ・`tool_failed` は Windows の日本語エラーと Traceback を**見逃す**（実測）
  ・`cd` の失敗（「構文が間違っています」）を成功と判定していた
  ・「0件」と「検索そのものの失敗」が同じ文字列で、モデルが正しく報告しても誤りになる
"""
from __future__ import annotations

import os

import pytest

from laps.tools import ToolResult, as_result


def test_文字列としてそのまま使える() -> None:
    """★既存の呼び出し側（道具22個・試験1001本）を1行も変えずに型を足すための性質。"""
    r = ToolResult("hello", ok=False, kind="error")
    assert r == "hello" and isinstance(r, str)
    assert r.upper() == "HELLO"
    assert len(r) == 5


def test_名乗った型はそのまま通る() -> None:
    r = as_result(ToolResult("(no matches)", ok=True, kind="empty"))
    assert r.ok is True and r.kind == "empty" and r.inferred is False


def test_名乗らない道具は推測して印を付ける() -> None:
    """推測した回が数えられる＝型への移行度が記録から見える。"""
    r = as_result("Error: policy denied: xyz")
    assert r.ok is False and r.inferred is True


def test_bashは終了コードを型で持つ() -> None:
    """★これまで returncode は空出力のときだけ本文に書かれ、他は捨てられていた。"""
    os.environ["LAPS_POLICY"] = "off"
    from laps.tools.bash import _run

    ok = as_result(_run({"command": "echo hi"}))
    assert ok.ok is True and ok.inferred is False

    ng = as_result(_run({"command": "exit 7"}))
    assert ng.ok is False and ng.kind == "error", "終了コードを捨てている"
    assert ng.inferred is False, "語彙表で推測している（型になっていない）"


def test_0件は失敗ではない() -> None:
    """grep/glob の「該当なし」を error にすると、正しい報告が失敗として扱われる。"""
    r = ToolResult("(no matches)", ok=True, kind="empty")
    assert r.ok is True and r.kind == "empty"
