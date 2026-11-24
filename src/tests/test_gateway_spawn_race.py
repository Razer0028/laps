"""★2026-09-08 監査（serve/gateway high）: 同じ session 名の接続が同時に 2 本来ると serve が 2 本立ち片方が孤児。"""
import asyncio
from laps import gateway as gwmod


class _Proc:
    returncode = None


class _Ses:
    def __init__(self, sid):
        self.sid = sid; self.proc = _Proc(); self.client = None; self.reaper = None


def test_concurrent_same_sid_spawns_once(tmp_path, monkeypatch):
    gw = gwmod.Gateway(token=None, workspace=tmp_path / "ws", home=tmp_path / "home", max_conn=4,
                       backend="http://127.0.0.1:1", model="m", policy="trusted", toolset="default", grace=1.0)
    calls = []

    async def fake_spawn(sid):
        calls.append(sid)
        await asyncio.sleep(0.2)             # 本物は subprocess の起動で await する
        ses = _Ses(sid); gw.sessions_live[sid] = ses
        return ses

    monkeypatch.setattr(gw, "_spawn", fake_spawn)

    async def go():
        r = await asyncio.gather(gw._get_or_spawn("s1"), gw._get_or_spawn("s1"), gw._get_or_spawn("s1"))
        return r

    res = asyncio.run(go())
    assert calls == ["s1"], calls
    assert sum(1 for _, spawned in res if spawned) == 1
    assert len({id(ses) for ses, _ in res}) == 1


def test_dead_session_is_respawned(tmp_path, monkeypatch):
    gw = gwmod.Gateway(token=None, workspace=tmp_path / "ws", home=tmp_path / "home", max_conn=4,
                       backend="http://127.0.0.1:1", model="m", policy="trusted", toolset="default", grace=1.0)
    dead = _Ses("s2"); dead.proc.returncode = 1
    gw.sessions_live["s2"] = dead
    closed = []

    async def fake_close(ses): closed.append(ses.sid); gw.sessions_live.pop(ses.sid, None)
    async def fake_spawn(sid):
        ses = _Ses(sid); gw.sessions_live[sid] = ses; return ses

    monkeypatch.setattr(gw, "_close_session", fake_close)
    monkeypatch.setattr(gw, "_spawn", fake_spawn)
    ses, spawned = asyncio.run(gw._get_or_spawn("s2"))
    assert closed == ["s2"] and spawned and ses is not dead


def test_shutdown_sends_cancel_with_run_id(tmp_path):
    """★2026-09-08 監査: 猶予切れの cancel に id が無く必ず断られていた。"""
    gw = gwmod.Gateway(token=None, workspace=tmp_path / "ws", home=tmp_path / "home", max_conn=4,
                       backend="http://127.0.0.1:1", model="m", policy="trusted", toolset="default", grace=1.0)

    class _Stdin:
        def __init__(self): self.buf = b""; self.closed = False
        def write(self, b): self.buf += b
        async def drain(self): return None
        def close(self): self.closed = True

    class _Proc:
        def __init__(self): self.returncode = None; self.stdin = _Stdin(); self.pid = 1
        async def wait(self): self.returncode = 0; return 0
        def kill(self): self.returncode = -9

    proc = _Proc()
    asyncio.run(gw._shutdown(proc, "r42"))
    lines = proc.stdin.buf.decode().splitlines()
    assert any('"cancel"' in l and '"r42"' in l for l in lines), lines
    assert any('"shutdown"' in l for l in lines)


def test_replay_catches_up_lines_emitted_during_resend(tmp_path):
    """★2026-09-08 監査: 再送中に serve が出した行が、控えにも線にも出ずに落ちていた。"""
    gw = gwmod.Gateway(token=None, workspace=tmp_path / "ws", home=tmp_path / "home", max_conn=4,
                       backend="http://127.0.0.1:1", model="m", policy="trusted", toolset="default", grace=1.0)
    ses = _Ses("s3")
    from collections import deque
    ses.buffer = deque([(1, "l1"), (2, "l2"), (3, "l3")])
    got = []

    class _Conn:
        async def send(self, line):
            got.append(line)
            if line == "l1":                      # 最初の送信中に serve が 2 行出した
                ses.buffer.append((4, "l4")); ses.buffer.append((5, "l5"))
            await asyncio.sleep(0)

    n = asyncio.run(gw._send_replay(ses, _Conn(), since=0))
    assert got == ["l1", "l2", "l3", "l4", "l5"], got
    assert n == 5
