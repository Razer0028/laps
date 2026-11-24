"""会話のグループをほかの機械とスマホにも見せる（2026-11-19）: 目録に group が載り、中継が受け取る。"""
from __future__ import annotations

import json
import os

from laps import convindex, relay


def _conv(home, sid: str, prompt: str) -> None:
    d = home / "sessions" / sid
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(json.dumps({"type": "run_spec", "prompt": prompt, "ts": 1}) + "\n", encoding="utf-8")


def test_group_from_the_app_reaches_the_index_and_changes_are_noticed(tmp_path):
    app = tmp_path / "app"
    home = app / "agent"
    (home / "sessions").mkdir(parents=True)
    _conv(home, "s1", "first task")
    _conv(home, "s2", "second task")
    convs = app / "conversations.json"
    convs.write_text(json.dumps([{"session": "s1", "title": "One", "group": "仕事"}, {"session": "s2", "title": "Two"}]), encoding="utf-8")
    idx = convindex.Index(home)
    idx.scan()
    items = {it["sid"]: it for it in idx.items.values()}
    assert items["s1"].get("group") == "仕事" and "group" not in items["s2"]

    convs.write_text(json.dumps([{"session": "s1", "title": "One"}, {"session": "s2", "title": "Two", "group": "遊び"}]), encoding="utf-8")
    os.utime(convs, (10, 10))                        # 同じ秒に書き直しても読み直させる
    r = idx.scan()
    changed = r[0] if isinstance(r, tuple) else r
    sids = {it["sid"] for it in changed}
    assert sids == {"s1", "s2"}
    items = {it["sid"]: it for it in idx.items.values()}
    assert "group" not in items["s1"] and items["s2"]["group"] == "遊び"


def test_relay_keeps_the_group_and_clips_it():
    assert "group" in relay.CONV_KEYS
    r = relay.Relay.__new__(relay.Relay)
    out = r._clean_item({"sid": "s1", "title": "t", "group": "g" * 100, "evil": 1})
    assert out["group"] == "g" * 60 and "evil" not in out
