"""ウェブ検索だけの通常会話モード（2026-09-21・オーナー指示）。

★「作業場を触る道具を渡さない」を**構造で**確かめる。
  規律（気をつける）ではなく、渡していないことを試験で固定する。
"""
import pytest

from laps.tools import CHAT_TOOLS, by_name

#: ★1 本でも混ざったら落とす。ここに名前を足す形の「うっかり」を止める門。
FORBIDDEN = ("Bash", "Verify", "RemoteShell", "BashOutput",
             "Read", "Write", "Edit", "ApplyDiff", "NotebookEdit",
             "Glob", "Grep", "Umbra", "Task", "SystemState")


@pytest.fixture()
def chat(monkeypatch):
    monkeypatch.setenv("LAPS_TOOLSET", "chat")
    return by_name()


def test_渡すのはウェブの2本だけ(chat):
    assert sorted(chat) == ["WebFetch", "WebSearch"]


def test_作業場を触る道具は1本も無い(chat):
    混入 = [n for n in FORBIDDEN if n in chat]
    assert not 混入, f"★会話モードに作業場の道具が混ざっています: {混入}"


def test_定数にも混ぜていない():
    """★`by_name` を通さずに CHAT_TOOLS を直接使う経路が将来できても効く。"""
    混入 = [n for n in FORBIDDEN if n in CHAT_TOOLS]
    assert not 混入, f"★CHAT_TOOLS に作業場の道具が混ざっています: {混入}"


def test_他のモードは今までどおり(monkeypatch):
    monkeypatch.setenv("LAPS_TOOLSET", "full")
    full = by_name()
    assert "Bash" in full and "Read" in full
    monkeypatch.setenv("LAPS_TOOLSET", "focused")
    assert "Bash" in by_name()


def test_実行できる道具が無いこと(chat):
    """★`executes=True` の道具が 0 本＝外を動かす手段が無い。"""
    assert [n for n, s in chat.items() if getattr(s, "executes", False)] == []


def test_書き換える道具が無いこと(chat):
    assert [n for n, s in chat.items() if getattr(s, "mutates", False)] == []
