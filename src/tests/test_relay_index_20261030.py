"""★2026-10-30 全端末で 1 つの会話の一覧（convindex → remotehost → relay → /conversations・/events・peers.json）。

オーナー「Claude のアプリのような使いかた」→「一覧は中継・中身は元の機械」。
中継と名乗り役を手元のポートで本当に立て、合成の会話のフォルダで通しを確かめる（推論機も網の外も使わない）。
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path

from laps import convindex, relay, remotehost


def _write(p: Path, rows) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def make_session(home: Path, sid: str, prompt: str, *, outcome: str | None = "accepted", answer: str = "できました",
                 live: list | None = None, source: str = "") -> Path:
    d = home / "sessions" / sid
    _write(d / "messages.jsonl", [{"role": "user", "content": f"[アプリからの指示]\nx\n\n[利用者]\n{prompt}"}])
    ev = [{"ts": 100.0, "seq": 1, "type": "run_spec", "payload": {"prompt": prompt, "source": source}}]
    if outcome:
        ev.append({"ts": 112.5, "seq": 2, "type": "result", "payload": {"outcome": outcome, "text": answer}})
    _write(d / "events.jsonl", ev)
    if live is not None:
        _write(d / "live.ndjson", live)
    return d


def test_index_statuses_and_titles(tmp_path):
    home = tmp_path / "agent"
    make_session(home, "s_done", "テストを直して")
    make_session(home, "s_fail", "壊れた物", outcome="error")
    make_session(home, "s_stop", "止めた物", outcome="cancelled")
    make_session(home, "s_run", "走っている物", outcome=None, source="android",
                 live=[{"v": 1, "seq": 1, "type": "notice", "kind": "run_start", "source": "android", "prompt": "走っている物"},
                       {"v": 1, "seq": 2, "type": "token", "text": "途中の答え"}])
    make_session(home, "s_ask", "聞いている物", outcome=None,
                 live=[{"v": 1, "seq": 1, "type": "notice", "kind": "run_start", "source": "app"},
                       {"v": 1, "seq": 2, "type": "ask", "ask": "a1", "question": "どちらにしますか？"}])
    # アプリの題（app_root/conversations.json）が優先
    (tmp_path / "conversations.json").write_text(json.dumps([{"id": 1, "title": "題を付けた会話", "session": "s_done"}],
                                                            ensure_ascii=False), encoding="utf-8")
    idx = convindex.Index(home)
    changed, removed = idx.scan()
    by = {x["sid"]: x for x in changed}
    assert removed == [] and set(by) == {"s_done", "s_fail", "s_stop", "s_run", "s_ask"}
    assert by["s_done"]["status"] == "done" and by["s_done"]["title"] == "題を付けた会話" and by["s_done"]["last"] == "できました"
    assert by["s_done"]["first"] == "テストを直して" and by["s_done"]["secs"] == 12.5
    assert by["s_fail"]["status"] == "failed" and by["s_stop"]["status"] == "stopped"
    assert by["s_run"]["status"] == "running" and by["s_run"]["source"] == "android" and by["s_run"]["last"] == "途中の答え"
    assert by["s_ask"]["status"] == "asking" and by["s_ask"]["ask"] == "どちらにしますか？"
    # 変わっていなければ 2 回目は空（走っている物は毎回作り直すが、中身が同じなら出さない）
    assert idx.scan() == ([], [])
    # 答えた → 聞いていない状態へ
    with open(home / "sessions" / "s_ask" / "live.ndjson", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "notice", "kind": "ask_closed", "ask": "a1"}) + "\n")
    changed, _ = idx.scan()
    assert [x["status"] for x in changed if x["sid"] == "s_ask"] == ["running"]
    # 古い写し（5 分より前）は走っているとは見ない
    later = time.time() + 400
    changed, _ = idx.scan(now=later)
    st = {x["sid"]: x["status"] for x in changed}
    assert st.get("s_run") == "idle" and st.get("s_ask") == "idle"


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_relay_merges_devices_and_streams_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(remotehost, "INDEX_EVERY", 0.2)
    home = tmp_path / "agent"
    make_session(home, "20261030_000001_aaaa", "一つ目")
    port = _free_port()

    async def main():
        from websockets.asyncio.client import connect
        r = relay.Relay(token="tok", device_key=None, state_dir=tmp_path / "relay")
        stop_relay = asyncio.Event()

        async def run_relay():
            from websockets.asyncio.server import serve
            async with serve(r.handler, "127.0.0.1", port, process_request=r.process_request):
                await stop_relay.wait()
        rt = asyncio.create_task(run_relay())
        await asyncio.sleep(0.3)
        host = remotehost.Host(f"ws://127.0.0.1:{port}", "dev1", "http://127.0.0.1:9", "windows", None, home)
        stop = asyncio.Event()
        ht = asyncio.create_task(host.run_forever(stop))
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as resp:
                return json.loads(resp.read())
        for _ in range(50):
            rows = await asyncio.to_thread(get, "/conversations?token=tok")
            if rows:
                break
            await asyncio.sleep(0.1)
        assert [x["sid"] for x in rows] == ["20261030_000001_aaaa"]
        assert rows[0]["device"] == "dev1" and rows[0]["online"] is True and rows[0]["status"] == "done"
        # 合言葉が無ければ読めない
        try:
            await asyncio.to_thread(get, "/conversations")
            raise AssertionError("token なしで読めた")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        # 知らせの線: hello の後、答え待ちになった会話が 1 通届く
        async with connect(f"ws://127.0.0.1:{port}/events?token=tok") as ev:
            hello = json.loads(await asyncio.wait_for(ev.recv(), 5))
            assert hello["type"] == "hello" and hello["active"] == []
            make_session(home, "20261030_000002_bbbb", "二つ目", outcome=None,
                         live=[{"type": "notice", "kind": "run_start", "source": "android"},
                               {"type": "ask", "ask": "a1", "question": "続けますか？"}])
            got = None
            for _ in range(10):
                m = json.loads(await asyncio.wait_for(ev.recv(), 5))
                if m.get("type") == "conv" and m["item"]["sid"] == "20261030_000002_bbbb":
                    got = m["item"]
                    break
            assert got and got["status"] == "asking" and got["ask"] == "続けますか？" and got["device_name"]
        active = await asyncio.to_thread(get, "/conversations?token=tok&active=1")
        assert [x["sid"] for x in active] == ["20261030_000002_bbbb"]
        # 機械の側には全機械の一覧が書かれる（アプリの知らせと一覧）
        peers = json.loads((home / "remote" / "peers.json").read_text(encoding="utf-8"))
        assert {x["sid"] for x in peers} >= {"20261030_000002_bbbb"}
        assert (home / "remote" / "peer-events.ndjson").read_text(encoding="utf-8").strip()
        # 機械が切れたら、その機械の走っている会話は idle にして知らせる
        stop.set()
        await asyncio.wait_for(ht, 10)
        await asyncio.sleep(0.3)
        rows = await asyncio.to_thread(get, "/conversations?token=tok")
        st = {x["sid"]: (x["status"], x["online"]) for x in rows}
        assert st["20261030_000002_bbbb"] == ("idle", False)
        stop_relay.set()
        await rt
        # 目録は覚えてある（中継を起こし直しても一覧が出る）
        r._save_index()                        # 2 秒後の書き出しを待たずに書く（ループを止めたので）
        r2 = relay.Relay(token="tok", device_key=None, state_dir=tmp_path / "relay")
        assert {x["sid"] for x in r2.conversations()} == {"20261030_000001_aaaa", "20261030_000002_bbbb"}

    asyncio.run(main())
