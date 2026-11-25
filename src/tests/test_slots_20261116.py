"""推論機の席の控え（2026-11-16）。子のエージェントや別の会話が席を使った後、元の会話を頭から読み直さない。"""
from __future__ import annotations

from laps.engine import slots
from laps.engine.llm import Info
from tests.test_engine_20261030 import env  # noqa: F401


class FakeSlotLLM:
    """席 0 と、推論機の置き場のファイルを真似る。"""

    def __init__(self, held: int = 0):
        self.held = held
        self.files: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_restore = False

    def info(self, **k):
        return Info(strata=True, llama=True)

    def slot_tokens(self):
        return self.held

    def slot_file(self, action, name, timeout=180.0):
        self.calls.append((action, name))
        if action == "save":
            self.files[name] = self.held
            return True, "{}"
        if self.fail_restore or name not in self.files:
            return False, "HTTP 400"
        self.held = self.files[name]
        return True, "{}"


def _count():
    seen: dict[str, int] = {}

    def count(k, n=1):
        seen[k] = seen.get(k, 0) + n
    return seen, count


def test_switching_conversations_saves_the_old_one_and_restores_the_new_one(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    llm = FakeSlotLLM()
    seen, count = _count()
    slots.begin(llm, "A", count)                 # A の 1 回目: 何も無い
    llm.held = 200_000
    slots.end(llm, "A")
    slots.begin(llm, "B", count)                 # B へ: A を保存
    assert ("save", slots.file_name("A")) in llm.calls and seen.get("slot_switch_saved") == 1
    llm.held = 50_000                            # B が席を使った
    slots.end(llm, "B")
    slots.begin(llm, "A", count)                 # A へ戻る: B を保存して A を戻す
    assert llm.held == 200_000 and seen.get("slot_switch_restored") == 1
    assert ("save", slots.file_name("B")) in llm.calls
    n = len(llm.calls)
    slots.end(llm, "A")
    slots.begin(llm, "A", count)                 # 同じ会話の続き: 何もしない
    assert len(llm.calls) == n


def test_a_slot_used_by_someone_else_is_not_saved_under_our_name(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    llm = FakeSlotLLM(held=120_000)
    seen, count = _count()
    slots.end(llm, "A")
    slots._wait_saving(10)                       # ★2026-11-25〜 終わりの保存（A の本物の中身・使われる前）は正しい保存。それを待ってから数える
    for t in __import__("threading").enumerate():
        if t.name == "bw-slot-save":
            t.join(10)
    llm.calls.clear()
    llm.held = 90_000                            # 別の機械が席を使った
    slots.begin(llm, "B", count)
    assert not any(a == "save" for a, _ in llm.calls), "相手の中身を A の名前で保存しない"


def test_a_file_that_cannot_be_restored_is_forgotten(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    llm = FakeSlotLLM(held=150_000)
    seen, count = _count()
    slots.end(llm, "A")
    slots.begin(llm, "B", count)
    llm.held = 30_000
    slots.end(llm, "B")
    llm.fail_restore = True                      # 推論機の設定が変わった
    slots.begin(llm, "A", count)
    assert seen.get("slot_restore_failed") == 1
    assert "A" not in slots._load().get("files", {})   # 次からは戻そうとしない


def test_child_agent_parks_and_restores_the_parent_slot(env, monkeypatch):  # noqa: F811
    a, *_ = env([{"text": "子の答え"}])
    fake = FakeSlotLLM(held=180_000)
    monkeypatch.setattr(a.llm, "info", lambda **k: Info(strata=True, llama=True, n_ctx=262144))
    monkeypatch.setattr(a.llm, "slot_tokens", fake.slot_tokens)
    monkeypatch.setattr(a.llm, "slot_file", fake.slot_file)
    a.last_tokens = 180_000
    out = a.spawn("調べて", "explore", "下見")
    assert "子の答え" in out
    name = slots.file_name(a.t.id)
    assert fake.calls == [("save", name)] and a._slot_restore == name
    assert a.counters.get("slot_saved") == 1


def test_small_parent_is_not_parked(env, monkeypatch):  # noqa: F811
    a, *_ = env([{"text": "子の答え"}])
    fake = FakeSlotLLM(held=5_000)
    monkeypatch.setattr(a.llm, "info", lambda **k: Info(strata=True, llama=True, n_ctx=262144))
    monkeypatch.setattr(a.llm, "slot_file", fake.slot_file)
    monkeypatch.setattr(a.llm, "slot_tokens", fake.slot_tokens)
    a.last_tokens = 5_000
    a.spawn("調べて", "explore", "下見")
    assert fake.calls == [] and not a._slot_restore


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_SLOT_CACHE", "0")
    llm = FakeSlotLLM(held=200_000)
    seen, count = _count()
    slots.end(llm, "A")
    slots.begin(llm, "B", count)
    assert llm.calls == []


def test_second_child_in_the_same_turn_keeps_the_parent_file(env, monkeypatch):  # noqa: F811
    """2026-11-18: 同じ手番の 2 本目の子の前に保存し直すと、1 本目の子の席で親の控えを上書きしていた。"""
    a, *_ = env([{"text": "子 1 の答え"}, {"text": "子 2 の答え"}])
    fake = FakeSlotLLM(held=150_000)
    monkeypatch.setattr(a.llm, "info", lambda **k: Info(strata=True, llama=True, n_ctx=262144))
    monkeypatch.setattr(a.llm, "slot_tokens", fake.slot_tokens)
    monkeypatch.setattr(a.llm, "slot_file", fake.slot_file)
    a.last_tokens = 150_000
    a.spawn("調べて 1", "explore", "下見 1")
    fake.held = 140_000                    # 1 本目の子が席を使い終えた（中身は子の会話）
    a.spawn("調べて 2", "explore", "下見 2")
    name = slots.file_name(a.t.id)
    assert fake.calls == [("save", name)]          # 保存は 1 回だけ（子の席で上書きしない）
    assert fake.files[name] == 150_000 and a._slot_restore == name
    assert a.counters.get("slot_park_kept") == 1


def test_snapshot_from_another_model_is_not_restored(tmp_path, monkeypatch):
    """2026-11-20: 控えに推論機の素性（モデル・版・窓）を結び付け、違えば戻さずに読み直す。"""
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    llm = FakeSlotLLM()
    seen, count = _count()
    model = {"p": "/m/a.gguf"}
    llm.info = lambda **k: Info(strata=True, llama=True, model_path=model["p"], build="Strata 0.1.55", n_ctx=262144)
    slots.begin(llm, "A", count)
    llm.held = 200_000
    slots.end(llm, "A")
    slots.begin(llm, "B", count)                 # A を保存（素性つき）
    llm.held = 30_000
    slots.end(llm, "B")
    model["p"] = "/m/abliterated.gguf"           # 推論機のモデルを替えた
    slots.begin(llm, "A", count)
    assert seen.get("slot_restore_skipped_mismatch") == 1
    assert ("restore", slots.file_name("A")) not in llm.calls
