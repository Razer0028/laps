"""TaskGet（task_mgmt._get）— この現場で一度も試験に呼ばれていなかった道具（2026-09-03）。

TaskCreate/TaskList/TaskUpdate/TaskStop は他の試験（test_task_status_canon.py・
test_no_change_tools.py 等）で通っているが、`TaskGet` だけは `grep -rn` で
1件も呼び出しが見つからなかった。壊れていても気づけない状態だったので、
存在する課題・存在しない課題・セッション無しの3本を足す。
"""
from __future__ import annotations

import json

import pytest

from laps.tools import task_mgmt as T


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_SESSION_ID", "s1")
    T._create({"subject": "工程1", "description": "説明文"})
    return tmp_path


def test_存在する課題をJSONで返す(session) -> None:
    out = T._get({"task_id": "1"})
    got = json.loads(out)
    assert got["subject"] == "工程1"
    assert got["description"] == "説明文"
    assert got["status"] == "pending"


def test_存在しない課題は名指しで断る(session) -> None:
    out = T._get({"task_id": "999"})
    assert out == "Error: task #999 not found"


def test_task_idを渡さないと空扱いで見つからない(session) -> None:
    out = T._get({})
    assert out.startswith("Error:") and "not found" in out


def test_セッションが無いと成功を装わない(monkeypatch) -> None:
    monkeypatch.delenv("LAPS_SESSION_ID", raising=False)
    out = T.SPECS[2].handler({"task_id": "1"})  # TaskGet
    assert T.SPECS[2].name == "TaskGet"
    assert getattr(out, "ok", True) is False
    assert out.startswith("Error:")
