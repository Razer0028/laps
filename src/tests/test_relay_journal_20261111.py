"""★2026-11-11 中継が会話の中身の写しを持つ（relay 1.8・Journal）。

オーナー「アカウントごとのセッション管理も全部サーバー側で情報を管理して、どのデバイスでも同じアカウントで
ログインすれば自分のアカウントの他のセッションで自由に作業できるように」「Claude Code の Remote Control と同じ仕組みでいい」
「（中身は）消さないで残しておいてアプリから消せるようにしておく」。
中継を手元のポートで本当に立て、偽の機械が /_device/control で繋いで events を答える（推論機も網の外も使わない）。
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from laps import relay

SID = "20261111_120000_abcd"


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _ev(seq: int, ty: str, **payload) -> str:
    return json.dumps({"ts": 100.0 + seq, "seq": seq, "type": ty, "payload": payload}, ensure_ascii=False)


class FakeMachine:
    """中継の /_device/control に繋ぎ、http の依頼に会話の事象（since_seq より後）で答える。"""

    def __init__(self, port: int, did: str = "dev1") -> None:
        self.url = f"ws://127.0.0.1:{port}/_device/control?id={did}"
        self.events = {SID: [_ev(1, "run_spec", prompt="テストを直して"), _ev(2, "result", outcome="accepted", text="直しました")]}
        self.asked: list[str] = []
        self.ws = None
        self.task = None

    def item(self, sid: str, updated: float) -> dict:
        return {"sid": sid, "title": "テストを直す", "first": "テストを直して", "status": "done", "updated": updated}

    async def start(self, items: list[dict]) -> None:
        from websockets.asyncio.client import connect
        self.ws = await connect(self.url)
        await self.ws.send(json.dumps({"op": "hello", "info": {"name": "偽の機械", "kind": "linux", "version": "t"}}))
        await self.ws.send(json.dumps({"op": "index", "full": True, "items": items}))
        self.task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("op") != "http":
                continue
            path = msg["path"]
            self.asked.append(path)
            sid = path.split("/")[2]
            since = int(path.rsplit("since_seq=", 1)[1]) if "since_seq=" in path else 0
            rows = [x for x in self.events.get(sid, []) if json.loads(x)["seq"] > since]
            st, body = (200, "".join(x + "\n" for x in rows)) if sid in self.events else (404, "no such session\n")
            await self.ws.send(json.dumps({"op": "http_reply", "rid": msg["rid"], "status": st, "body": body}))

    async def index(self, items: list[dict], full: bool = False) -> None:
        await self.ws.send(json.dumps({"op": "index", "full": full, "items": items}))

    async def stop(self) -> None:
        await self.ws.close()
        try:
            await asyncio.wait_for(self.task, 5)
        except Exception:
            pass


def test_journal_copies_reads_offline_keeps_and_deletes(tmp_path):
    port = _free_port()
    state = tmp_path / "relay"

    async def main():
        r = relay.Relay(token="tok", device_key=None, state_dir=state)
        stop_relay = asyncio.Event()

        async def run_relay():
            from websockets.asyncio.server import serve
            async with serve(r.handler, "127.0.0.1", port, process_request=r.process_request, max_size=64 * 1024 * 1024):
                await stop_relay.wait()
        rt = asyncio.create_task(run_relay())
        await asyncio.sleep(0.3)
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.headers.get("X-Blackwall-Copy")

        async def wait_seq(n):
            for _ in range(100):
                if int((r.journal.info("dev1", SID) or {}).get("seq") or 0) >= n:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(f"写しが seq {n} に届かない: {r.journal.info('dev1', SID)}")

        m = FakeMachine(port)
        await m.start([m.item(SID, 100.0)])
        # ① 一覧が来たら、中継が自分で中身を引いて写す（since_seq=0 から）
        await wait_seq(2)
        assert m.asked[0] == f"/sessions/{SID}/events?since_seq=0"
        st, body, copy = await asyncio.to_thread(get, f"/d/dev1/sessions/{SID}/events?token=tok")
        assert st == 200 and copy == "relay" and [json.loads(x)["seq"] for x in body.splitlines()] == [1, 2]
        # since_seq も効く（端末は差分だけ取れる）
        _st, body, _ = await asyncio.to_thread(get, f"/d/dev1/sessions/{SID}/events?token=tok&since_seq=1")
        assert [json.loads(x)["seq"] for x in body.splitlines()] == [2]
        # ② 続きが増えて updated が変わったら、持っている seq の次から引く（重ならない）
        m.events[SID].append(_ev(3, "assistant", text="続きの答え"))
        await m.index([m.item(SID, 200.0)])
        await wait_seq(3)
        assert any(p.endswith("since_seq=2") for p in m.asked)
        assert len(r.journal.read("dev1", SID).splitlines()) == 3
        # ③ 機械が切れても読める（★1.7 までは 503）。一覧にも写しの印
        await m.stop()
        await asyncio.sleep(0.3)
        st, body, copy = await asyncio.to_thread(get, f"/d/dev1/sessions/{SID}/events?token=tok")
        assert st == 200 and copy == "relay; offline" and len(body.splitlines()) == 3
        rows = json.loads((await asyncio.to_thread(get, "/conversations?token=tok"))[1])
        assert rows[0]["sid"] == SID and rows[0]["online"] is False and rows[0]["copy"] is True
        # 写しの無い会話は、機械が寝ていれば今までどおり 503
        try:
            await asyncio.to_thread(get, "/d/dev1/sessions/20261111_000000_none/events?token=tok")
            raise AssertionError("写しの無い会話が読めた")
        except urllib.error.HTTPError as e:
            assert e.code == 503
        # ④ 機械の一覧から外れても（古くなって落ちた）、中継は消さずに archived で残す
        m2 = FakeMachine(port)
        m2.events = {}
        await m2.start([])
        await asyncio.sleep(0.3)
        rows = json.loads((await asyncio.to_thread(get, "/conversations?token=tok"))[1])
        hit = [x for x in rows if x["sid"] == SID]
        assert hit and hit[0]["archived"] is True and hit[0]["copy"] is True
        # ⑤ アプリから消す → 写しも一覧も消え、機械から同じ会話が来ても戻さない
        res = await r._op_conv_delete({"device": "dev1", "sid": SID}, None)
        assert res == {"ok": True, "removed": True}
        await m2.index([m2.item(SID, 300.0)])
        await asyncio.sleep(0.3)
        rows = json.loads((await asyncio.to_thread(get, "/conversations?token=tok"))[1])
        assert all(x["sid"] != SID for x in rows)
        try:
            await asyncio.to_thread(get, f"/d/dev1/sessions/{SID}/events?token=tok")
            raise AssertionError("消した会話が読めた")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        assert not any(SID in p for p in m2.asked)       # 消した会話は引きにも行かない
        await m2.stop()
        stop_relay.set()
        await rt
        r.journal.save()
        # ⑥ 中継を起こし直しても、消した印は残る
        r2 = relay.Relay(token="tok", device_key=None, state_dir=state)
        assert r2.journal.is_gone("dev1", SID) and r2.journal.read("dev1", SID) is None

    asyncio.run(main())


def test_journal_names_stay_inside(tmp_path):
    j = relay.Journal(tmp_path / "journal")
    assert j.path("..", "x") is None and j.path("dev", "..") is None and j.path("dev", ".hidden") is None
    assert j.append("dev", "..", _ev(1, "run_spec")) == 0
    assert j.path("dev", "20261111_a") == tmp_path / "journal" / "dev" / "20261111_a.jsonl"
    # seq の無い行・古い seq は足さない。同じ物を 2 回送っても重ならない
    body = _ev(1, "run_spec") + "\nnot json\n" + _ev(2, "result")
    assert j.append("dev", "s1", body) == 2 and j.append("dev", "s1", body) == 0
    assert [json.loads(x)["seq"] for x in j.read("dev", "s1").splitlines()] == [1, 2]
