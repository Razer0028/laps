"""★2026-11-20 依頼の始めに強く当たる記憶を断片で添える（construct.start_block・engine/memory.begin）。推論機は使わない。"""
from __future__ import annotations

import time

import pytest

from laps.oc import construct as c


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LAPS_CONSTRUCT", raising=False)
    monkeypatch.delenv("LAPS_CONSTRUCT_AUTO", raising=False)
    return tmp_path


def test_strong_hit_is_shown_as_fragment_weak_is_not(tmp_path):
    here = tmp_path / "proj"
    now = time.time()
    c.add("episode", "loop.py の _done_gate を直した（差し戻しの順番）", folder=here, ts=now - 86400)
    c.add("episode", "README を書いた", folder=here, ts=now - 3600)
    text, ids = c.start_block("loop.py の _done_gate をもう一度直して", str(here))
    assert ids and text.startswith("[Memory]")
    assert "_done_gate" in text and "README" not in text
    assert f"#{ids[0]} [" in text and "Construct(open=" in text
    assert c.start_block("天気はどう？", str(here)) == ("", [])


def test_already_in_context_is_not_repeated(tmp_path):
    here = tmp_path / "proj"
    c.add("episode", "loop.py の _done_gate を直した（差し戻しの順番）", folder=here, ts=time.time() - 86400)
    text, ids = c.start_block("loop.py の _done_gate をもう一度直して", str(here),
                              context=["作業ログ: loop.py の _done_gate を直した（差し戻しの順番）"])
    assert (text, ids) == ("", [])


def test_switch_off(tmp_path, monkeypatch):
    here = tmp_path / "proj"
    c.add("episode", "loop.py の _done_gate を直した（差し戻しの順番）", folder=here, ts=time.time() - 86400)
    monkeypatch.setenv("LAPS_CONSTRUCT_AUTO", "0")
    assert c.start_block("loop.py の _done_gate をもう一度直して", str(here)) == ("", [])


def test_memory_begin_adds_block_and_counts_open(tmp_path):
    from laps.engine.memory import Memory
    here = tmp_path / "proj"
    here.mkdir()
    c.add("episode", "loop.py の _done_gate を直した（差し戻しの順番）", folder=here, ts=time.time() - 86400)
    counts: dict[str, int] = {}
    m = Memory(str(here), "s1", lambda k, n=1: counts.__setitem__(k, counts.get(k, 0) + n))
    block = m.begin("loop.py の _done_gate をもう一度直して", new_conversation=False)
    assert "[Memory]" in block and counts.get("construct_auto_shown") == 1
    iid = next(iter(m.auto_ids))
    m._watch_recall({"open": iid, "why": "same gate"}, True, f"Memory #{iid} ...")
    assert counts.get("construct_auto_opened") == 1
