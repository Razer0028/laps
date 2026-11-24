"""帳簿の道具が「変化なし」を言えること。

★2026-08-18 敵対レビューの指摘（実機で確認済み）:
Todo/Task 系は「失敗できない・変化なしと言えない」ので、モデルが同じ内容を
書き続けても毎回「OK」が返り、**停滞の燃料そのもの**になっていた。
  v0.1.52 … TodoWrite 28連発で 204 秒
  v0.1.53 … TaskUpdate→TaskStop→TaskList→TaskGet の4周期×8巡（道具33回）
  1走行 143 呼び出し中 135 が無仕事

道具が正直に言えば、モデルは停滞検知に頼る前に次の手へ移れる。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from laps.tools import as_result


@pytest.fixture
def session(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_SESSION_ID", "t1")
    return tmp_path


def test_同じTODOを書き直しても変化なしと答える(session) -> None:
    from laps.tools.todowrite import _run
    todos = [{"content": "A", "status": "completed"}, {"content": "B", "status": "pending"}]
    first = as_result(_run({"todos": todos}))
    assert first.kind == "ok" and "OK" in str(first)
    for _ in range(3):
        again = as_result(_run({"todos": todos}))
        assert again.kind == "empty", "同じ内容の書き直しを『更新した』と答えている"
        assert "変化なし" in str(again)
        assert "繰り返さない" in str(again), "次の一手が書かれていない"


def test_本当に変えたときは更新と答える(session) -> None:
    from laps.tools.todowrite import _run
    _run({"todos": [{"content": "A", "status": "pending"}]})
    changed = as_result(_run({"todos": [{"content": "A", "status": "completed"}]}))
    assert changed.kind == "ok" and "OK" in str(changed)


def test_停止済みのtaskをもう一度止めても変化なし(session) -> None:
    from laps.tools import task_mgmt
    as_result(task_mgmt._create({"subject": "x"}))
    first = as_result(task_mgmt._stop({"task_id": "1"}))
    assert first.kind == "ok"
    again = as_result(task_mgmt._stop({"task_id": "1"}))
    assert again.kind == "empty" and "変化なし" in str(again)
