"""記録は伏せて残すが、次の依頼で送る並びは前の依頼と同じにする（2026-11-14）。

実機: 道具の出力や引数に伏せる対象（パスワード・トークン）があると、走行中のモデルは伏せる前を読み、次の依頼では記録から読んだ伏字を
読む。そこから先のプロンプトが変わり、推論機が会話を読み直していた（7 万トークンで 175 秒・Ask と Agent の切り替えに見えていた）。
"""
from __future__ import annotations

import io

from tests.test_engine_20261030 import Fake

from laps.engine import session
from laps.engine.server import Server, Spec
from laps.engine.session import Transcript
from laps.engine.wire import Out

SECRET = "hunter2abc"
CMD = f"echo 'login --password={SECRET}'"


def _two_runs(tmp_path, monkeypatch, between=None):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setenv("LAPS_LIVE", "0")
    monkeypatch.setenv("LAPS_EXPLORE_FIRST", "0")
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = Fake([{"calls": [("Bash", {"command": CMD})]}, {"text": "終わりました。"}, {"text": "はい。"}])
    srv = Server(Out(io.StringIO()))
    try:
        sid = Transcript().id
        common = dict(session=sid, backend=fake.url, model="north-test", workspace=str(ws))
        srv._run(Spec(id="r1", prompt="ログインの確認をしてください。お願いします", toolset="full", **common))
        n1 = len(fake.bodies)
        if between:
            between()
        srv._run(Spec(id="r2", prompt="いまの結果は何でしたか", toolset="chat", **common))
    finally:
        fake.close()
    return fake.bodies, n1, session.sessions_dir() / sid / "messages.jsonl"


def test_next_request_sends_what_the_model_already_read(tmp_path, monkeypatch):
    b, n1, path = _two_runs(tmp_path, monkeypatch)
    last, nxt = b[n1 - 1]["messages"], b[n1]["messages"]
    assert SECRET in str(last)                                  # 走行中のモデルは伏せる前を読んでいる
    assert nxt[:len(last)] == last                              # 次の依頼でも同じ並び（推論機の控えがそのまま使える）
    assert SECRET not in path.read_text(encoding="utf-8")       # 記録は伏せたまま


def test_after_a_restart_the_record_is_what_is_sent(tmp_path, monkeypatch):
    """本体を起動し直した後は、伏せる前はどこにも残っていない（ディスクに書かない）。"""
    b, n1, path = _two_runs(tmp_path, monkeypatch, between=session._LIVE.clear)
    assert SECRET not in str(b[n1]["messages"])
    assert SECRET not in path.read_text(encoding="utf-8")
