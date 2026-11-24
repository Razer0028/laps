"""この機械を中継（推論機 の laps/relay.py）に名乗り、スマホから扱えるようにする（2026-10-08）。

オーナー「全てのデバイスにインストールされているセッションを Android から管理できるようにしたい」
「コマンドを使わずともデフォルトで」。

- この機械の中に、スマホ向けのゲートウェイ（laps/gateway.py と同じもの）を 127.0.0.1 だけで立てる。
  走行の記録は、この機械の BLACKWALL と同じ置き場（LAPS_HOME）を使う＝画面で始めた会話も一覧に出る。
- 中継へは**こちらから**繋ぎに行く（受け口を開けない）。切れたら 2→4→…→60 秒の間隔で繋ぎ直す。
- `--attach <URL>` のときは、既にあるゲートウェイ（開発機 の :18400 など）をそのまま中継に名乗らせる。
- 会話の目録（laps/convindex.py）を 3 秒ごとに見て、変わった会話を中継へ送る（2026-10-30・全端末で 1 つの一覧）。
  中継が配る全機械の目録は `<置き場>/remote/peers.json`（全部）と `peer-events.ndjson`（変わるたびに 1 行）に書く
  ＝Windows のアプリが、ほかの機械の会話の一覧と知らせに使う（アプリに待ち受けの口を作らない約束のまま）。
- 繋がっているかは `<置き場>/remote/status.json`（30 秒ごとに書き直す・/remote-control の窓が読む）。
- 1 台に 1 本だけ（置き場の remotehost.lock）。`LAPS_PARENT_PID` があれば、その親が終わったら自分も終わる
  （Windows のアプリの子として動くとき）。

切る: アプリの設定 `remote_control = false`、または環境変数 `LAPS_REMOTE=0`。
終わりの番号: 0＝切られている（起こし直さない）／3＝この機械では別の係が動いている／それ以外＝落ちた。
"""
from __future__ import annotations

from . import paths as _lp
import argparse
import asyncio
import getpass
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

from . import relayauth

DEFAULT_RELAY = relayauth.DEFAULT_RELAY
MAX_FRAME = 32 * 1024 * 1024
INDEX_EVERY = 3.0                      # 目録を見直す間隔（秒）
AUTH_CHECK_EVERY = 5.0                 # 認証情報のファイルが変わったかを見る間隔（秒）
PEER_EVENTS_MAX = 1024 * 1024          # peer-events.ndjson の上限（超えたら新しい 300 行に縮める）


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _home() -> Path:
    h = os.environ.get("LAPS_HOME")
    return Path(h) if h else _lp.dot_dir()


def _version() -> str:
    v = (os.environ.get("LAPS_APP_VERSION") or "").strip()
    if v:
        return v
    try:
        from .term import __version__
        return str(__version__)
    except Exception:
        return "?"


def device_id(home: Path) -> str:
    """機械ごとに 1 つの、変わらない名札（置き場の device-id）。ホスト名は変わりうるので鍵にしない。"""
    f = home / "device-id"
    try:
        v = f.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    v = uuid.uuid4().hex[:16]
    try:
        home.mkdir(parents=True, exist_ok=True)
        f.write_text(v + "\n", encoding="utf-8")
    except OSError:
        pass
    return v


def current_workspace() -> str:
    """いまの作業フォルダ。Windows のアプリは workspace.json の current、ほかは環境変数かホーム。"""
    f = os.environ.get("LAPS_REMOTE_WSFILE")
    if f:
        try:
            cur = json.loads(Path(f).read_text(encoding="utf-8")).get("current")
            if isinstance(cur, str) and cur.strip() and os.path.isdir(cur):
                return cur
        except Exception:
            pass
    for k in ("LAPS_REMOTE_WORKSPACE", "LAPS_WORKSPACE"):
        v = os.environ.get(k)
        if v and os.path.isdir(v):
            return v
    return str(Path.home())


def info(kind: str) -> dict:
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    return {"name": socket.gethostname(), "os": f"{platform.system()} {platform.release()}".strip(),
            "kind": kind, "version": _version(), "workspace": current_workspace(), "user": user}


def _http_get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "blackwall-remotehost"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 502, f"local gateway error: {e}\n"


# ── 1 台に 1 本・親の見張り ─────────────────────────────────────────────
def _single_instance(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    fh = open(home / "remotehost.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259                       # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _watch_parent(stop: asyncio.Event) -> None:
    try:
        pid = int(os.environ.get("LAPS_PARENT_PID") or 0)
    except ValueError:
        pid = 0
    if pid <= 0:
        return
    while not stop.is_set():
        if not _alive(pid):
            _log(f"親（pid {pid}）が終わった → 終わります")
            stop.set()
            return
        await asyncio.sleep(5)


# ── この機械のゲートウェイ ─────────────────────────────────────────────
async def start_local_gateway(home: Path):
    from .gateway import DEFAULT_BACKEND, DEFAULT_MODEL, Gateway, _ws_serve

    class _DeviceGateway(Gateway):
        async def _spawn(self, sid: str):
            # ★作業フォルダは走行を始めるたびに読み直す（画面で切り替えたフォルダに付いて行く）。
            try:
                self.workspace = Path(current_workspace())
            except Exception:
                pass
            return await super()._spawn(sid)

    gw = _DeviceGateway(token=None, workspace=Path(current_workspace()), home=home,
                        max_conn=int(os.environ.get("LAPS_REMOTE_MAX", "4") or 4),
                        backend=os.environ.get("LAPS_BACKEND") or DEFAULT_BACKEND,
                        model=os.environ.get("LAPS_MODEL") or DEFAULT_MODEL,
                        policy=os.environ.get("LAPS_POLICY") or "trusted",
                        toolset=os.environ.get("LAPS_TOOLSET") or "full", grace=600.0)
    server = await _ws_serve(gw.handler, "127.0.0.1", 0, process_request=gw.process_request,
                             max_size=8 * 1024 * 1024, ping_interval=20, ping_timeout=20)
    port = server.sockets[0].getsockname()[1]
    return gw, server, f"http://127.0.0.1:{port}"


# ── 中継との線 ────────────────────────────────────────────────────────
class Host:
    def __init__(self, relay: str, did: str, local_http: str, kind: str, key: str | None, home: Path | None = None) -> None:
        from .convindex import Index
        self.home = Path(home) if home else _home()
        self.index = Index(self.home)
        self.peers: dict[tuple, dict] = {}
        self.relay = relay.rstrip("/")
        self.did = did
        self.local_http = local_http.rstrip("/")
        self.local_ws = "ws" + self.local_http[4:] if self.local_http.startswith("http") else self.local_http
        self.kind = kind
        self.keyq = f"&key={quote(key)}" if key else ""
        self.tasks: set[asyncio.Task] = set()
        self.since = 0

    def _status(self, connected: bool, error: str = "") -> None:
        """繋がっているかを置き場に書く（/remote-control の窓が読む・relayauth.rc_info）。"""
        try:
            if connected and not self.since:
                self.since = int(time.time())
            elif not connected:
                self.since = 0
            d = self.home / "remote"
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "status.json.tmp"
            tmp.write_text(json.dumps({"connected": connected, "since": self.since, "error": error[:300], "relay": self.relay,
                                       "device": self.did}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, d / "status.json")
        except OSError:
            pass

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def _http(self, ws, rid: int, path: str) -> None:
        status, body = await asyncio.to_thread(_http_get, self.local_http + path)
        try:
            await ws.send(json.dumps({"op": "http_reply", "rid": rid, "status": status, "body": body},
                                     ensure_ascii=False))
        except Exception:
            pass

    async def _open(self, ws, cid: str, path: str) -> None:
        from websockets.asyncio.client import connect
        from .relay import splice
        try:
            local = await connect(self.local_ws + path, max_size=MAX_FRAME, ping_interval=20, ping_timeout=30,
                                  open_timeout=15)
        except Exception as e:  # noqa: BLE001
            try:
                await ws.send(json.dumps({"op": "open_failed", "cid": cid, "error": f"local: {e}"[:200]}))
            except Exception:
                pass
            return
        try:
            base, kw = relayauth.ws_connect_args(self.relay)
            data = await connect(f"{base}/_device/data?id={self.did}&cid={quote(cid)}{self.keyq}",
                                 max_size=MAX_FRAME, ping_interval=20, ping_timeout=30, open_timeout=15, **kw)
        except Exception as e:  # noqa: BLE001
            _log(f"中継への中身の線を開けない: {e!r}")
            await local.close()
            return
        await splice(local, data)

    async def _info_loop(self, ws) -> None:
        last = None
        while True:
            await asyncio.sleep(30)
            self._status(True)
            cur = info(self.kind)
            if cur != last:
                last = cur
                try:
                    await ws.send(json.dumps({"op": "info", "info": cur}, ensure_ascii=False))
                except Exception:
                    return

    async def _index_loop(self, ws) -> None:
        """会話の目録の差分を送る。繋いだ直後は全部（中継はそれで消えた会話も知る）。"""
        try:
            await asyncio.to_thread(self.index.scan)
            await ws.send(json.dumps({"op": "index", "full": True, "items": self.index.all()}, ensure_ascii=False))
            while True:
                await asyncio.sleep(INDEX_EVERY)
                changed, removed = await asyncio.to_thread(self.index.scan)
                if changed or removed:
                    await ws.send(json.dumps({"op": "index", "items": changed, "removed": removed}, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 目録が送れなくても中継の線は保つ
            _log(f"目録を送れない: {type(e).__name__}: {e}")

    def _peers_write(self, events: list[dict]) -> None:
        d = self.home / "remote"
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "peers.json.tmp"
            rows = sorted(self.peers.values(), key=lambda r: r.get("updated", 0), reverse=True)
            tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, d / "peers.json")
            if events:
                f = d / "peer-events.ndjson"
                with open(f, "a", encoding="utf-8") as fh:
                    for e in events:
                        fh.write(json.dumps({**e, "ts": time.time()}, ensure_ascii=False) + "\n")
                if f.stat().st_size > PEER_EVENTS_MAX:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
                    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as e:
            _log(f"peers を書けない: {e!r}")

    def _on_peers(self, msg: dict) -> None:
        if msg.get("op") == "peers":
            self.peers = {(str(r.get("device")), str(r.get("sid"))): r for r in msg.get("items") or [] if isinstance(r, dict)}
            # ★繋いだときの一覧でも 1 行書く（アプリは行を見て一覧を読み直す）。行が無いと、初めて入れた端末では
            #   ほかの会話が動くまで「ほかの端末」の欄が空のままだった（2026-10-31）。sid の無い行は知らせにならない。
            self._peers_write([{"sync": True, "device": "", "sid": "", "status": ""}])
            return
        it = msg.get("item")
        if not isinstance(it, dict):
            return
        key = (str(it.get("device")), str(it.get("sid")))
        if it.get("removed"):
            self.peers.pop(key, None)
        else:
            self.peers[key] = it
        self._peers_write([it])

    async def _auth_loop(self, ws, seen: float) -> None:
        """サインイン・サインアウトで認証情報が変わったら繋ぎ直す（アプリを起動し直さなくても持ち主が変わる）。"""
        while True:
            await asyncio.sleep(AUTH_CHECK_EVERY)
            if relayauth.stamp() != seen:
                _log("認証情報が変わったので繋ぎ直します")
                try:
                    await ws.close(1000, "auth changed")
                except Exception:
                    pass
                return

    async def run_forever(self, stop: asyncio.Event) -> None:
        from websockets.asyncio.client import connect
        backoff = 2.0
        said_down = False
        while not stop.is_set():
            seen = relayauth.stamp()
            try:
                base, kw = relayauth.ws_connect_args(self.relay)
                url = f"{base}/_device/control?id={self.did}{self.keyq}"
                async with connect(url, max_size=MAX_FRAME, ping_interval=20, ping_timeout=30,
                                   open_timeout=15, **kw) as ws:
                    await ws.send(json.dumps({"op": "hello", "info": info(self.kind)}, ensure_ascii=False))
                    _log(f"中継に繋がった: {base}（名札 {self.did}）")
                    backoff, said_down = 2.0, False
                    self._status(True)
                    it = asyncio.create_task(self._info_loop(ws))
                    ix = asyncio.create_task(self._index_loop(ws))
                    au = asyncio.create_task(self._auth_loop(ws, seen))
                    # ★終わる合図が来たら線を閉じて、下の `async for` を抜ける（2026-10-08）。
                    #   旧: 合図を待つだけで何もせず、次の通信が来るまで抜けなかった＝親（アプリ）が更新で入れ替わっても
                    #   古い係が 2 分居残り、新しい係が「既に動いている」で終わって、どちらも居なくなった（スマホに 503）。
                    async def _close_on_stop() -> None:
                        await stop.wait()
                        try:
                            await ws.close(1001, "device host stopping")
                        except Exception:
                            pass
                    stopper = asyncio.create_task(_close_on_stop())
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            if msg.get("op") == "http":
                                self._spawn(self._http(ws, int(msg.get("rid") or 0), str(msg.get("path") or "/")))
                            elif msg.get("op") == "open":
                                self._spawn(self._open(ws, str(msg.get("cid") or ""), str(msg.get("path") or "/ws")))
                            elif msg.get("op") in ("peers", "peer_conv"):
                                self._on_peers(msg)
                            if stop.is_set():
                                break
                    finally:
                        it.cancel()
                        ix.cancel()
                        au.cancel()
                        stopper.cancel()
            except Exception as e:  # noqa: BLE001
                # ★取り消された認証情報は持っていても使えない。消して画面にサインインし直しを出させる（環境変数の分は消せない）。
                if (getattr(getattr(e, "response", None), "status_code", 0) == 401 and relayauth.load().get("token")
                        and relayauth.issued_by(self.relay)):
                    _log("認証情報が取り消されていました → 手元の分を消します")
                    relayauth.clear()
                if not said_down:
                    _log(f"中継に繋がらない（{type(e).__name__}: {e}）→ 繋ぎ直しを続けます")
                    said_down = True
                self._status(False, f"{type(e).__name__}: {e}")
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)


async def amain(a) -> int:
    home = _home()
    lock = _single_instance(home)
    # ★入れ替わりの直後は古い係がまだ終わりかけている。少し待ってから判断する（2026-10-08）。
    waited = 0
    while lock is None and waited < 60:
        await asyncio.sleep(3)
        waited += 3
        lock = _single_instance(home)
    if lock is None:
        _log("この機械では既に動いています（60 秒待っても空かない）→ 終わります")
        return 3
    did = device_id(home)
    stop = asyncio.Event()
    # ★サービスとして止められたとき（SIGTERM）も、走らせている本体を行儀よく閉じてから終わる。
    try:
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    except (NotImplementedError, AttributeError, RuntimeError):
        pass
    watcher = asyncio.create_task(_watch_parent(stop))
    gw = server = None
    if a.attach:
        local = a.attach
    else:
        gw, server, local = await start_local_gateway(home)
    _log(f"remotehost {_version()} 名札 {did} 種類 {a.kind} 手元 {local} 中継 {a.relay}")
    host = Host(a.relay, did, local, a.kind, (os.environ.get("LAPS_RELAY_DEVICE_KEY") or "").strip() or None, home)
    try:
        await host.run_forever(stop)
    finally:
        host._status(False, "この機械の名乗り役が止まりました")
        watcher.cancel()
        if gw is not None:
            for ses in list(gw.sessions_live.values()):
                try:
                    await gw._close_session(ses)
                except Exception:
                    pass
        if server is not None:
            server.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    if (os.environ.get("LAPS_REMOTE") or "").strip() == "0":
        return 0
    ap = argparse.ArgumentParser(description="この機械を L.A.P.S の中継に名乗らせる")
    ap.add_argument("--relay", default=os.environ.get("LAPS_RELAY_URL") or DEFAULT_RELAY)
    ap.add_argument("--attach", default=os.environ.get("LAPS_REMOTE_ATTACH") or "",
                    help="既にあるゲートウェイ（例 http://127.0.0.1:18400）をそのまま名乗らせる")
    ap.add_argument("--kind", default=os.environ.get("LAPS_REMOTE_KIND")
                    or ("windows" if os.name == "nt" else "linux"))
    a = ap.parse_args(argv)
    try:
        return asyncio.run(amain(a))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
