"""長くなった会話は、新しい依頼の頭で 1 回だけ要約する（2026-11-25 オーナー「会話が続くとどんどん蓄積されていってしまうので
Claude アプリみたいな感じの仕組みにしたい」）。作業の途中ではやらない。手元の推論機だけ。LAPS_COMPACT_EARLY=0 で止まる。
★覚え書きは設定の置き場（~/.config）に書かれるので、試験では XDG_CONFIG_HOME を一時フォルダーへ向ける。
"""
from __future__ import annotations

import json

import pytest

from tests.test_engine_20261030 import Fake, _serve  # noqa: F401

from laps.engine import compact

ASK = "The conversation has grown long, so this turn is a summary"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("LAPS_COMPACT_EARLY", raising=False)
    monkeypatch.delenv("LAPS_COMPACT_AT", raising=False)
    return tmp_path


def _history(pairs: int, size: int) -> list[dict]:
    h = []
    for i in range(pairs):
        h.append({"role": "user", "content": f"q{i} " + "a" * size})
        h.append({"role": "assistant", "content": f"r{i} " + "b" * size})
    return h


def _chars(body: dict) -> int:
    return len(json.dumps(body["messages"], ensure_ascii=False))


def _last(body: dict) -> str:
    return json.dumps(body["messages"][-1], ensure_ascii=False)


def test_a_long_conversation_is_summarized_once_at_the_start_of_the_next_request(cfg, monkeypatch):
    monkeypatch.setenv("LAPS_COMPACT_EARLY", "100000")   # 2026-11-27 から既定は切ってある
    fake = Fake([{"text": "## Objective\n- keep going\n"}, {"text": "答え。"}], n_ctx=262144)
    home, ws = cfg / "h", cfg / "w"
    ws.mkdir()
    hist = _history(6, 40_000)                    # 約 12 万トークン（既定のしきい値 10 万を越える）
    ev = _serve(fake, home, ws, [{"type": "run", "id": "r1", "messages": hist + [{"role": "user", "content": "次の依頼"}]}], 1)
    assert ev[-1]["type"] == "done" and ev[-1]["text"] == "答え。"
    first, work = fake.bodies[0], fake.bodies[1]
    assert ASK in _last(first), "最初の要求は、会話の続きとして頼む要約"
    assert "次の依頼" in json.dumps(first["messages"], ensure_ascii=False), "要約は新しい依頼まで含めて頼む"
    assert "## Objective" in json.dumps(work["messages"], ensure_ascii=False), "作業は要約から始める"
    assert _chars(work) < _chars(first) // 3, "モデルに見せる分が大きく減る"
    assert ev[-1]["counters"].get("compaction_boundary") == 1
    assert any(e.get("kind") == "context_compacted" for e in ev if e.get("type") == "notice")
    starts = [e for e in ev if e.get("type") == "iter_start"]
    assert starts and starts[0].get("compact_at") == 100_000, "輪の印は境目の要約の量"


def test_short_conversations_and_the_switch_are_left_alone(cfg, monkeypatch):
    fake = Fake([{"text": "答え。"}], n_ctx=262144)
    home, ws = cfg / "h", cfg / "w"
    ws.mkdir()
    _serve(fake, home, ws, [{"type": "run", "id": "r1", "messages": _history(2, 20_000) + [{"role": "user", "content": "次"}]}], 1)
    assert ASK not in _last(fake.bodies[0]), "短い会話は要約しない"
    monkeypatch.delenv("LAPS_COMPACT_EARLY", raising=False)   # 既定（2026-11-27 から切ってある）
    fake2 = Fake([{"text": "答え。"}], n_ctx=262144)
    home2, ws2 = cfg / "h2", cfg / "w2"
    ws2.mkdir()
    _serve(fake2, home2, ws2, [{"type": "run", "id": "r1", "messages": _history(6, 40_000) + [{"role": "user", "content": "次"}]}], 1)
    assert ASK not in _last(fake2.bodies[0]), "既定では長い会話でも依頼の頭で要約しない（オーナー 11-27）"
    monkeypatch.setenv("LAPS_COMPACT_EARLY", "0")
    assert compact.early_at(262144) == 0


def test_the_threshold_gives_way_to_a_small_window(monkeypatch):
    monkeypatch.delenv("LAPS_COMPACT_AT", raising=False)
    monkeypatch.delenv("LAPS_COMPACT_EARLY", raising=False)
    assert compact.early_at(262144) == 0, "既定は切ってある（2026-11-27）"
    monkeypatch.setenv("LAPS_COMPACT_EARLY", "100000")
    assert compact.early_at(262144) == 100_000
    assert compact.early_at(65536) == 0, "窓が小さい推論機では、窓の手前の要約に任せる"
    monkeypatch.setenv("LAPS_COMPACT_EARLY", "5000")
    assert compact.early_at(262144) == 16_000, "小さすぎる値は 16000 に"


def test_the_length_comes_from_the_inference_server_when_the_slot_holds_this_conversation(tmp_path, monkeypatch):
    """しきい値は、席にこの会話が入っていれば推論機が数えた本物の長さで見る（文字からの見積もりは日本語や道具の出力でずれる）。"""
    import types

    from laps.engine import slots
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "h"))
    llm = types.SimpleNamespace(info=lambda fresh=False: types.SimpleNamespace(strata=True, llama=True))
    slots._save({"owner": "conv-a", "owner_tokens": 150_000})
    assert slots.held_for(llm, "conv-a") == 150_000
    assert slots.held_for(llm, "conv-b") == 0, "席が別の会話なら分からない＝見積もる"
    plain = types.SimpleNamespace(info=lambda fresh=False: types.SimpleNamespace(strata=False, llama=True))
    assert slots.held_for(plain, "conv-a") == 0, "席の控えを使わない推論機では見積もる"


def test_a_small_window_keeps_most_of_itself_before_summarizing(monkeypatch):
    """窓 3.2 万（子の推論機）で 2 万を空けると 1.3 万で要約が始まる → 空ける量は窓の 4 分の 1 まで。大きい窓は今までどおり。"""
    monkeypatch.delenv("LAPS_COMPACT_AT", raising=False)
    assert compact.usable(32768) == 24576
    assert compact.usable(262144) == 262144 - 20_000
    assert compact.usable(131072) == 131072 - 20_000
