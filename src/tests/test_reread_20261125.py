"""続けてタスクを頼んでも、会話を頭から読み直さない（2026-11-25 オーナー「タスクを連続して頼んでもコンテキストの読み直しが発生する」）。

推論機 の推論機の記録（6 日）で、頭から読み直した依頼が多数あった。偽の推論機で再現できた原因は 2 つ:
- 覚え書き（Remember）が変わると前置きを作り直していた（覚え書きは全部の会話で共通なので、ほかの会話も読み直していた）。
- Agent で始めた会話をチャット（talk）・画像に切り替えると、前置きと道具の一覧が 1 文字目から変わっていた。
もう 1 つ、別の機械の L.A.P.S が同じ推論機を使うと、相手はこちらの会話の席を保存しない → 依頼の終わりに大きい会話の席を保存しておく。
★覚え書きは設定の置き場（~/.config）に書かれるので、試験では XDG_CONFIG_HOME を一時フォルダーへ向ける（向けないと本物に書く）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_engine_20261030 import Fake, _serve  # noqa: F401  (同じ偽の推論機と serve の回し方)

from laps.engine import slots


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def _sys(body: dict) -> str:
    return next((m["content"] for m in body["messages"] if m["role"] == "system"), "")


def _tools(body: dict) -> str:
    return json.dumps(body.get("tools"), ensure_ascii=False, sort_keys=True)


def _text(body: dict) -> str:
    return json.dumps(body["messages"], ensure_ascii=False)


def _run(fake, home, ws, session, prompt, history, tools=None):
    req = {"type": "run", "id": f"r{len(fake.bodies)}", "messages": history + [{"role": "user", "content": prompt}]}
    if session:
        req["session"] = session
    if tools:
        req["tools"] = tools
    ev = _serve(fake, home, ws, [req], 1)
    return ev[-1]


def test_a_new_remembered_note_does_not_rebuild_the_prefix(cfg):
    fake = Fake([{"calls": [("Remember", {"action": "add", "text": "返事は短く"})]}, {"text": "覚えました。"},
                 {"text": "二つ目。"}, {"text": "三つ目。"}])
    home, ws = cfg / "h", cfg / "w"
    ws.mkdir()
    d1 = _run(fake, home, ws, None, "返事は短くと覚えて", [])
    assert (cfg / "cfg").exists(), "覚え書きは一時フォルダーの設定の置き場に書かれる"
    n1 = len(fake.bodies)
    d2 = _run(fake, home, ws, d1["session"], "次のタスク", [{"role": "user", "content": "返事は短くと覚えて"},
                                                       {"role": "assistant", "content": d1.get("text", "")}])
    last1, first2 = fake.bodies[n1 - 1], fake.bodies[n1]
    assert _sys(first2) == _sys(last1), "覚え書きが増えても前置きは作り直さない（会話を頭から読み直さない）"
    assert _tools(first2) == _tools(last1)
    assert "remembered notes changed" in _text(first2) and "返事は短く" in _text(first2), "変わったことは依頼の後ろの一言で渡す"
    n2 = len(fake.bodies)
    _run(fake, home, ws, d2["session"], "三つ目のタスク", [])
    third = fake.bodies[n2]
    assert _text(third).count("remembered notes changed") == 1, "同じ知らせを依頼のたびに足さない"


def test_switching_an_agent_conversation_to_chat_keeps_the_prefix(cfg):
    fake = Fake([{"calls": [("Bash", {"command": "echo hi"})]}, {"text": "一つ目。"},
                 {"calls": [("Write", {"file_path": "x.txt", "content": "x"})]}, {"text": "チャットの答え。"}])
    home, ws = cfg / "h", cfg / "w"
    ws.mkdir()
    d1 = _run(fake, home, ws, None, "一つ目", [])
    n1 = len(fake.bodies)
    _run(fake, home, ws, d1["session"], "ちょっと話そう", [], tools="talk")
    talk = fake.bodies[n1]
    assert _sys(talk) == _sys(fake.bodies[n1 - 1]) and _tools(talk) == _tools(fake.bodies[n1 - 1]), \
        "Agent の会話をチャットに替えても前置きと道具は同じ（読み直さない）"
    assert "Chat mode, for the message above only" in _text(talk)
    refused = _text(fake.bodies[n1 + 1])
    assert "Not run in Chat mode" in refused and not (ws / "x.txt").exists(), "チャットでは書く道具を断る"


def test_a_new_chat_conversation_keeps_its_own_light_prefix(cfg):
    fake = Fake([{"text": "やあ。"}])
    home, ws = cfg / "h", cfg / "w"
    ws.mkdir()
    _run(fake, home, ws, None, "こんにちは", [], tools="talk")
    assert "This is a chat with no workspace" in _sys(fake.bodies[0]), "新しいチャットは軽い前置きのまま"


class _LLM:
    """推論機の席だけを真似る（Strata・席 0 の長さ・保存と戻しの呼び出しを記録する）。"""

    def __init__(self, held: int) -> None:
        self.held = held
        self.calls: list[tuple[str, str]] = []

    def info(self, fresh: bool = False):
        return SimpleNamespace(strata=True, model_path="/m/gyro.gguf", build="b1", n_ctx=262144, template="t")

    def slot_tokens(self):
        return self.held

    def slot_file(self, action: str, name: str, timeout: float = 0):
        self.calls.append((action, name))
        return True, "{}"


def _done_saving():
    slots._wait_saving(10)
    for t in threading.enumerate():
        if t.name == "bw-slot-save":
            t.join(10)


def test_large_conversation_is_saved_at_the_end_and_restored_after_another_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "h"))
    llm = _LLM(120000)
    slots.end(llm, "conv-a")
    _done_saving()
    assert ("save", slots.file_name("conv-a")) in llm.calls, "大きい会話は依頼の終わりに裏で保存する"
    reg = json.loads((tmp_path / "h" / "slots.json").read_text(encoding="utf-8"))
    assert reg["files"]["conv-a"]["tokens"] == 120000
    # 少ししか伸びていなければ保存し直さない
    llm.calls.clear()
    llm.held = 123000
    slots.end(llm, "conv-a")
    _done_saving()
    assert not any(a == "save" for a, _ in llm.calls)
    # 別の機械の L.A.P.S が席を使った（長さが変わった）→ 次の依頼でこの会話の控えを戻す
    llm.calls.clear()
    llm.held = 31000
    slots.begin(llm, "conv-a", lambda *a: None)
    assert ("restore", slots.file_name("conv-a")) in llm.calls


def test_small_conversations_and_the_switch_are_not_saved_at_the_end(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "h"))
    llm = _LLM(12000)
    slots.end(llm, "conv-b")
    _done_saving()
    assert llm.calls == [], "小さい会話は保存しない（読み直しても速い）"
    monkeypatch.setenv("LAPS_SLOT_SAVE_AT_END", "0")
    llm.held = 150000
    slots.end(llm, "conv-b")
    _done_saving()
    assert llm.calls == [], "LAPS_SLOT_SAVE_AT_END=0 で止まる"


def test_the_save_is_skipped_when_the_slot_is_already_in_use(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "h"))
    llm = _LLM(90000)
    real = llm.slot_tokens
    seen = {"n": 0}

    def changing():
        seen["n"] += 1
        return 90000 if seen["n"] == 1 else 95000          # 終わりの記録の後に、もう次の依頼が席を使った
    llm.slot_tokens = changing
    slots.end(llm, "conv-c")
    _done_saving()
    llm.slot_tokens = real
    assert not any(a == "save" for a, _ in llm.calls), "席がもう別の依頼に使われていたら保存しない"
