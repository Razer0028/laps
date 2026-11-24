"""中継のアカウント（relay 1.6）と客（relayauth）の試験。

本物の中継を手元の口で立て、本物の線で話す（同じ口で平文と TLS・見せる範囲・取り消し・乗っ取りの拒否）。
「LAN から平文で来た」場合は手元では作れないので、process_request を偽の線で直接呼ぶ。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from laps import ctlclient, relay as R, relayauth

websockets = pytest.importorskip("websockets")
from websockets.sync.client import connect  # noqa: E402

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl が無い")


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl が無い")
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", "2", "-subj", "/CN=relay-test", "-keyout", str(d / "k"), "-out", str(d / "c")],
                   check=True, capture_output=True)
    return d / "c", d / "k"


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(R, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(R, "SCRYPT_P", 1)
    monkeypatch.delenv("LAPS_RELAY_TOKEN", raising=False)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)


class Running:
    """本物の中継を別の糸で動かす。"""

    def __init__(self, state, mode, cert=None, signup="open"):
        self.relay = R.Relay(token=None, device_key=None, state_dir=state, auth_mode=mode, signup=signup)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        tls = R._tls_context(*cert) if cert else None
        ready = threading.Event()

        def go():
            async def main():
                started = asyncio.Event()
                task = asyncio.create_task(R.run(self.relay, "127.0.0.1", self.port, tls, started))
                await started.wait()
                self.loop = asyncio.get_running_loop()
                ready.set()
                await task
            asyncio.run(main())

        self.thread = threading.Thread(target=go, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    @property
    def wss(self):
        return f"wss://127.0.0.1:{self.port}"

    @property
    def ws(self):
        """手元の平文の線（中継は手元の線なら認証情報を受ける）。★同期の客を TLS で使わない（relayauth.SyncLine の説明）。"""
        return f"ws://127.0.0.1:{self.port}"

    def stop(self):
        self.loop.call_soon_threadsafe(self.relay.stop.set)
        self.thread.join(10)


@pytest.fixture
def running(tmp_path, cert, monkeypatch):
    monkeypatch.setenv("LAPS_RELAY_CA", str(cert[0]))
    made = []

    def start(mode="required", **kw):
        r = Running(tmp_path / f"state{len(made)}", mode, cert, **kw)
        made.append(r)
        monkeypatch.setenv("LAPS_RELAY_URL", r.wss)
        return r
    yield start
    for r in made:
        r.stop()


def as_user(monkeypatch, tmp_path, name):
    monkeypatch.setenv("LAPS_AUTH_FILE", str(tmp_path / f"{name}.json"))


def signup(monkeypatch, tmp_path, name):
    as_user(monkeypatch, tmp_path, name)
    relayauth.register(f"{name}@example.com", "password-" + name)
    return relayauth.token()


def tls_get(r, path, token=""):
    req = urllib.request.Request(f"https://127.0.0.1:{r.port}{path}",
                                 headers={"X-Blackwall-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=5, context=relayauth.ssl_context()) as resp:
        return resp.status, json.loads(resp.read().decode())


def tls_status(r, path, token=""):
    try:
        return tls_get(r, path, token)[0]
    except urllib.error.HTTPError as e:
        return e.code


class Machine:
    """中継に名乗る偽の機械（本物の線）。"""

    def __init__(self, r, did, token="", name=None):
        h = {"X-Blackwall-Token": token} if token else {}
        self.ws = connect(f"{r.ws}/_device/control?id={did}", additional_headers=h, open_timeout=5)
        self.ws.send(json.dumps({"op": "hello", "info": {"name": name or did, "kind": "linux", "version": "t"}}))
        self.first = json.loads(self.ws.recv(timeout=5))     # 繋いだときの peers

    def index(self, *sids):
        self.ws.send(json.dumps({"op": "index", "full": True, "items": [{"sid": s, "title": s, "updated": 1} for s in sids]}))

    def close(self):
        self.ws.close()


def eventually(fn, secs=5.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


# ── 同じ口で平文と TLS ────────────────────────────────────────────
def test_one_port_speaks_plain_and_tls(running):
    r = running("optional")
    with urllib.request.urlopen(f"http://127.0.0.1:{r.port}/health", timeout=5) as resp:
        plain = json.loads(resp.read().decode())
    assert plain["tls"] is True and plain["auth_mode"] == "optional"
    assert tls_get(r, "/health")[1]["tls"] is True


def test_other_certificate_is_refused(running, tmp_path, monkeypatch):
    r = running("optional")
    other = tmp_path / "other.crt"
    k = tmp_path / "other.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", "2", "-subj", "/CN=other", "-keyout", str(k), "-out", str(other)],
                   check=True, capture_output=True)
    monkeypatch.setenv("LAPS_RELAY_CA", str(other))
    as_user(monkeypatch, tmp_path, "a")
    with pytest.raises(relayauth.AuthError) as e:
        relayauth.register("a@example.com", "password-a")
    assert e.value.code == "unreachable"
    assert not relayauth.auth_file().exists()


# ── 登録とサインイン ──────────────────────────────────────────────
def test_register_stores_only_hashes(running, tmp_path, monkeypatch):
    r = running()
    tok = signup(monkeypatch, tmp_path, "alice")
    assert tok.startswith(R.TOKEN_PREFIX)
    assert stat.S_IMODE(os.stat(relayauth.auth_file()).st_mode) == 0o600
    server = r.relay.accounts.path
    assert stat.S_IMODE(os.stat(server).st_mode) == 0o600
    text = server.read_text(encoding="utf-8")
    assert tok not in text and "password-alice" not in text and "alice@example.com" in text


def test_login_and_mistakes(running, tmp_path, monkeypatch):
    running()
    signup(monkeypatch, tmp_path, "alice")
    relayauth.clear()
    assert relayauth.login("ALICE@example.com ", "password-alice")["email"] == "alice@example.com"   # 大文字と空白はそろえる
    for fn, args, code in ((relayauth.login, ("alice@example.com", "wrong-password"), "bad_login"),
                           (relayauth.login, ("nobody@example.com", "password-x"), "bad_login"),
                           (relayauth.register, ("alice@example.com", "password-2"), "email_taken"),
                           (relayauth.register, ("not-an-email", "password-2"), "bad_email"),
                           (relayauth.register, ("b@example.com", "short"), "weak_password")):
        with pytest.raises(relayauth.AuthError) as e:
            fn(*args)
        assert e.value.code == code


def test_too_many_failures_stop(running, tmp_path, monkeypatch):
    running()
    signup(monkeypatch, tmp_path, "alice")
    for _ in range(R.FAIL_MAX):
        with pytest.raises(relayauth.AuthError):
            relayauth.login("alice@example.com", "wrong-password")
    with pytest.raises(relayauth.AuthError) as e:
        relayauth.login("alice@example.com", "password-alice")    # 正しくても 5 分は止まる
    assert e.value.code == "too_many"


def test_signup_can_be_closed(running, tmp_path, monkeypatch):
    running(signup="closed")
    as_user(monkeypatch, tmp_path, "a")
    with pytest.raises(relayauth.AuthError) as e:
        relayauth.register("a@example.com", "password-a")
    assert e.value.code == "signup_closed"


# ── 見せる範囲 ────────────────────────────────────────────────────
def test_required_mode_isolates_accounts(running, tmp_path, monkeypatch):
    r = running("required")
    a = signup(monkeypatch, tmp_path, "alice")
    b = signup(monkeypatch, tmp_path, "bob")
    assert tls_status(r, "/devices") == 401
    assert tls_status(r, "/conversations") == 401
    assert tls_status(r, "/devices", "bwd_not-a-real-token") == 401
    ma, mb = Machine(r, "machine-a", a), Machine(r, "machine-b", b)
    try:
        ma.index("sid-a")
        mb.index("sid-b")
        assert eventually(lambda: "sid-a" in (r.relay.index.get("machine-a") or {})
                          and "sid-b" in (r.relay.index.get("machine-b") or {}))
        assert [d["id"] for d in tls_get(r, "/devices", a)[1]] == ["machine-a"]
        assert [d["id"] for d in tls_get(r, "/devices", b)[1]] == ["machine-b"]
        assert [c["sid"] for c in tls_get(r, "/conversations", a)[1]] == ["sid-a"]
        assert tls_status(r, "/d/machine-b/health", a) == 404           # 別のアカウントの機械は「無い」
        # 知らせも自分の機械の分だけ
        with connect(f"{r.ws}/events", additional_headers={"X-Blackwall-Token": a}) as ev:
            hello = json.loads(ev.recv(timeout=5))
            assert [d["id"] for d in hello["devices"]] == ["machine-a"]
            mb.index("sid-b", "sid-b2")
            ma.index("sid-a", "sid-a2")
            got = json.loads(ev.recv(timeout=5))
            assert got["item"]["device"] == "machine-a"
        # ★機械どうしの知らせも、同じアカウントの分だけ（別のアカウントの会話の題を配らない）
        ma.ws.send(json.dumps({"op": "info", "info": {"name": "machine-a"}}))
        mb.index("sid-b", "sid-b3")
        with pytest.raises(TimeoutError):
            while True:
                msg = json.loads(ma.ws.recv(timeout=0.5))
                assert msg.get("item", {}).get("device") != "machine-b"
    finally:
        ma.close()
        mb.close()


def test_machine_cannot_be_taken_over(running, tmp_path, monkeypatch):
    r = running("required")
    a = signup(monkeypatch, tmp_path, "alice")
    b = signup(monkeypatch, tmp_path, "bob")
    m = Machine(r, "machine-a", a)
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as e:
            Machine(r, "machine-a", b)                                  # 名札を知っていても別のアカウントでは名乗れない
        assert e.value.response.status_code == 403
        assert "machine-a" in r.relay.devices
    finally:
        m.close()


def test_optional_mode_keeps_old_clients_working(running, tmp_path, monkeypatch):
    r = running("optional")
    old = Machine(r, "old-machine")                                     # 認証情報のない古い機械
    a = signup(monkeypatch, tmp_path, "alice")
    b = signup(monkeypatch, tmp_path, "bob")
    mine = Machine(r, "alice-machine", a)
    theirs = Machine(r, "bob-machine", b)
    try:
        everyone = {d["id"] for d in tls_get(r, "/devices")[1]}
        assert everyone == {"old-machine", "alice-machine", "bob-machine"}   # 切り替えの間は全部見える
        seen = {d["id"]: d for d in tls_get(r, "/devices", a)[1]}
        assert set(seen) == {"old-machine", "alice-machine"}            # サインインすると自分の物と、誰の物でもない物
        assert seen["alice-machine"]["mine"] is True and seen["old-machine"]["mine"] is False
        with pytest.raises(websockets.exceptions.InvalidStatus) as e:
            Machine(r, "alice-machine")                                 # 持ち主のいる機械に名札だけで名乗れない
        assert e.value.response.status_code == 403
    finally:
        old.close()
        mine.close()
        theirs.close()


def test_owner_survives_reconnect_and_restart(running, tmp_path, monkeypatch):
    r = running("required")
    a = signup(monkeypatch, tmp_path, "alice")
    m = Machine(r, "machine-a", a)
    m.close()
    assert eventually(lambda: "machine-a" not in r.relay.devices)
    assert r.relay.seen["machine-a"]["owner"]
    state = r.relay.state_file.parent
    again = R.Relay(token=None, device_key=None, state_dir=state, auth_mode="required")
    assert again.seen["machine-a"]["owner"] == r.relay.seen["machine-a"]["owner"]
    assert again.accounts.check(a) is not None


# ── 取り消し ──────────────────────────────────────────────────────
def test_revoke_closes_the_line_and_invalidates(running, tmp_path, monkeypatch):
    r = running("required")
    a = signup(monkeypatch, tmp_path, "alice")
    m = Machine(r, "machine-a", a)
    as_user(monkeypatch, tmp_path, "alice")
    rows = relayauth.rpc("sessions")["items"]
    assert len(rows) == 1 and rows[0]["current"] and rows[0]["device"] == "machine-a" and rows[0]["online"]
    second = tmp_path / "second.json"
    monkeypatch.setenv("LAPS_AUTH_FILE", str(second))
    relayauth.login("alice@example.com", "password-alice")
    relayauth.rpc("revoke", id=rows[0]["id"])                           # もう 1 台の端末から取り消す
    assert eventually(lambda: "machine-a" not in r.relay.devices)
    assert tls_status(r, "/devices", a) == 401


def test_status_clears_a_revoked_token(running, tmp_path, monkeypatch):
    running("required")
    signup(monkeypatch, tmp_path, "alice")
    tok = relayauth.token()
    relayauth.rpc("logout")                                             # 中継の側だけ取り消す
    relayauth.save({"token": tok, "email": "alice@example.com"})
    s = relayauth.status()
    assert s["signed_in"] is False and s["error"] == "signed_out"
    assert not relayauth.auth_file().exists()


def test_status_keeps_login_when_unreachable(tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    as_user(monkeypatch, tmp_path, "alice")
    relayauth.save({"token": "bwd_x", "email": "alice@example.com"})
    got = relayauth.status(f"ws://127.0.0.1:{port}")
    assert got["signed_in"] is True and got["reachable"] is False       # ★中継が止まっても手元の作業を止めない


def test_password_change_signs_out_other_devices(running, tmp_path, monkeypatch):
    r = running("required")
    a = signup(monkeypatch, tmp_path, "alice")
    second = tmp_path / "second.json"
    monkeypatch.setenv("LAPS_AUTH_FILE", str(second))
    relayauth.login("alice@example.com", "password-alice")
    keep = relayauth.token()
    with pytest.raises(relayauth.AuthError):
        relayauth.rpc("password", old="wrong-password", new="new-password-1")
    relayauth.rpc("password", old="password-alice", new="new-password-1")
    assert tls_status(r, "/devices", a) == 401
    assert tls_status(r, "/devices", keep) == 200
    relayauth.clear()
    assert relayauth.login("alice@example.com", "new-password-1")["email"] == "alice@example.com"


def test_forget_machine(running, tmp_path, monkeypatch):
    r = running("optional")
    old = Machine(r, "old-row")
    a = signup(monkeypatch, tmp_path, "alice")
    with pytest.raises(relayauth.AuthError) as e:
        relayauth.rpc("forget_machine", id="old-row")                   # 繋がっている誰の物でもない機械は外せない
    assert e.value.code == "online"
    old.close()
    assert eventually(lambda: "old-row" not in r.relay.devices)
    relayauth.rpc("forget_machine", id="old-row")
    assert "old-row" not in {d["id"] for d in tls_get(r, "/devices", a)[1]}
    m = Machine(r, "alice-machine", a)
    relayauth.rpc("forget_machine", id="alice-machine")                 # 自分の機械は繋がっていても外せる（認証情報も取り消す）
    assert eventually(lambda: "alice-machine" not in r.relay.devices)
    assert "alice-machine" not in r.relay.seen
    m.close()
    assert tls_status(r, "/devices", a) == 401


# ── 画面のない機械 ────────────────────────────────────────────────
def test_pairing_flow(running, tmp_path, monkeypatch):
    running("required")
    signup(monkeypatch, tmp_path, "alice")
    as_user(monkeypatch, tmp_path, "headless")
    start = relayauth.rpc("pair_start", device={"name": "workbox", "kind": "linux"})
    assert relayauth.rpc("pair_poll", code=start["code"])["pending"] is True
    as_user(monkeypatch, tmp_path, "alice")
    with pytest.raises(relayauth.AuthError):
        relayauth.rpc("pair_approve", user_code="WRONG-CODE")
    done = relayauth.rpc("pair_approve", user_code=start["user_code"].lower())
    assert done["device"]["name"] == "workbox"
    as_user(monkeypatch, tmp_path, "headless")
    got = relayauth.rpc("pair_poll", code=start["code"])
    assert got["token"].startswith(R.TOKEN_PREFIX) and got["account"]["email"] == "alice@example.com"
    with pytest.raises(relayauth.AuthError) as e:
        relayauth.rpc("pair_poll", code=start["code"])                  # 1 回きり
    assert e.value.code == "expired"


def test_pairing_needs_a_signed_in_approver(running, tmp_path, monkeypatch):
    running("required")
    as_user(monkeypatch, tmp_path, "nobody")
    start = relayauth.rpc("pair_start", device={"name": "x"})
    with pytest.raises(relayauth.AuthError) as e:
        relayauth.rpc("pair_approve", user_code=start["user_code"])
    assert e.value.code == "unauthorized"


def test_admin_reset_from_inside(running, tmp_path, monkeypatch):
    r = running("required")
    a = signup(monkeypatch, tmp_path, "alice")
    with connect(f"ws://127.0.0.1:{r.port}/auth") as ws:
        ws.send(json.dumps({"op": "admin_reset", "email": "alice@example.com", "password": "reset-password"}))
        assert json.loads(ws.recv(timeout=5))["ok"] is True
    assert tls_status(r, "/devices", a) == 401
    relayauth.clear()
    assert relayauth.login("alice@example.com", "reset-password")["email"] == "alice@example.com"


# ── 平文の線（LAN から来た場合は偽の線で）──────────────────────────
class FakeTransport:
    def __init__(self, tls):
        self.tls = tls

    def get_extra_info(self, name):
        return object() if name == "ssl_object" and self.tls else None


class FakeConn:
    def __init__(self, ip="192.0.2.50", tls=False):
        # 手前の口（_front）を通った線の形: 中継から見える相手は 127.0.0.1 で、本当の住所は手前の口が覚えている
        self.peer = ip
        self.remote_address = ("127.0.0.1", 40000)
        self.local_address = ("127.0.0.1", 50000)
        self.transport = FakeTransport(tls)

    def respond(self, status, body):
        class Resp:
            pass
        r = Resp()
        r.status, r.body, r.headers = status, body, {}
        return r


class FakeReq:
    def __init__(self, path, headers=None):
        self.path = path
        self.headers = headers or {}


def call(relay, path, *, headers=None, front=True, **conn):
    c = FakeConn(**conn)
    if front:
        relay.front_peers[(40000, 50000)] = c.peer
    else:
        relay.front_peers.pop((40000, 50000), None)
    return asyncio.run(relay.process_request(c, FakeReq(path, headers)))


def fresh(tmp_path, mode="required"):
    rel = R.Relay(token=None, device_key=None, state_dir=tmp_path, auth_mode=mode)
    rel.tls_on = True
    return rel


def test_a_line_the_front_does_not_know_is_not_local(tmp_path):
    """★2026-11-13 手前の口の覚えが無い線を「この機械の中」と読まない（覚えの鍵が口の番号だけで、平文と TLS の線が同じ番号を
    取ると上書きされ、先に閉じた方が消していた。残った線は 127.0.0.1 に見え、平文でパスワードを受け、管理の操作も通った）。"""
    rel = fresh(tmp_path)
    assert call(rel, "/auth", ip="127.0.0.1", front=False).status == 403
    rel.front_peers[(40000, 50001)] = "127.0.0.1"                       # 別の内側の口への線の覚えは使わない
    assert call(rel, "/auth", ip="127.0.0.1", front=False).status == 403
    rel.tls_on = False                                                  # 手前の口を使わないときは、線の相手がそのまま相手
    c = FakeConn()
    c.remote_address = ("127.0.0.1", 40000)
    assert rel._peer_ip(c) == "127.0.0.1"


def test_passwords_are_refused_on_a_plain_line(tmp_path):
    rel = fresh(tmp_path)
    assert call(rel, "/auth").status == 403
    assert call(rel, "/auth", tls=True) is None                         # TLS なら受ける
    assert call(rel, "/auth", ip="127.0.0.1") is None                   # 手元の線も受ける


def test_tokens_are_refused_on_a_plain_line(tmp_path):
    rel = fresh(tmp_path)
    aid = rel.accounts.create("a@example.com", "password-a")
    raw, _ = rel.accounts.issue(aid, {})
    assert call(rel, "/devices", headers={"X-Blackwall-Token": raw}).status == 403
    assert call(rel, "/devices?token=" + raw).status == 403
    assert call(rel, "/devices", headers={"X-Blackwall-Token": raw}, tls=True).status == 200


def test_plain_page_redirects_to_tls(tmp_path):
    rel = fresh(tmp_path, "optional")
    r = call(rel, "/", headers={"Host": "192.0.2.160:18400"})
    assert r.status == 302 and r.headers["Location"] == "https://192.0.2.160:18400/"


def test_off_mode_behaves_like_before(tmp_path):
    rel = R.Relay(token=None, device_key=None, state_dir=tmp_path)
    assert rel.accounts is None
    assert call(rel, "/devices").status == 200
    assert call(rel, "/auth").status == 404
    assert not (tmp_path / "accounts.json").exists()


def test_broken_accounts_file_stops_startup(tmp_path):
    (tmp_path / "accounts.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(SystemExit):                                     # ★空で始めて次の保存で全部消さない
        R.Relay(token=None, device_key=None, state_dir=tmp_path, auth_mode="required")


# ── 客 ────────────────────────────────────────────────────────────
def test_secret_never_goes_plain_to_another_host():
    assert relayauth.secure_url("ws://192.0.2.160:18400", has_secret=True) == "wss://192.0.2.160:18400"
    assert relayauth.secure_url("ws://192.0.2.160:18400", has_secret=False) == "ws://192.0.2.160:18400"
    assert relayauth.secure_url("http://192.0.2.160:18400/", has_secret=True) == "wss://192.0.2.160:18400"
    assert relayauth.secure_url("ws://127.0.0.1:18400", has_secret=True) == "ws://127.0.0.1:18400"


def test_token_is_not_put_in_the_address(tmp_path, monkeypatch):
    as_user(monkeypatch, tmp_path, "a")
    relayauth.save({"token": "bwd_secret"})
    url, kw = relayauth.ws_connect_args("ws://127.0.0.1:1")
    assert "bwd_secret" not in url and kw["additional_headers"] == {"X-Blackwall-Token": "bwd_secret"}
    from laps import relaybridge
    url2, _ = relaybridge._url("ws://127.0.0.1:1", "dev", "line")
    assert "bwd_secret" not in url2


def test_works_through_an_http_proxy(running, tmp_path, monkeypatch):
    """オーナー機は OS のプロキシ越しのことがある。CONNECT で通す。"""
    r = running("required")
    seen = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def proxy():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head:
                head += c.recv(4096)
            seen.append(head.split(b"\r\n")[0].decode())
            up = socket.create_connection(("127.0.0.1", r.port))
            c.sendall(b"HTTP/1.1 200 OK\r\n\r\n")

            def pump(a, b):
                try:
                    while True:
                        d = a.recv(65536)
                        if not d:
                            break
                        b.sendall(d)
                except OSError:
                    pass
                for x in (a, b):
                    try:
                        x.close()
                    except OSError:
                        pass
            threading.Thread(target=pump, args=(c, up), daemon=True).start()
            threading.Thread(target=pump, args=(up, c), daemon=True).start()

    threading.Thread(target=proxy, daemon=True).start()
    monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{srv.getsockname()[1]}")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    as_user(monkeypatch, tmp_path, "alice")
    try:
        assert relayauth.register("alice@example.com", "password-alice")["email"] == "alice@example.com"
    finally:
        srv.close()
    assert seen and seen[0].startswith(f"CONNECT 127.0.0.1:{r.port}")


def test_ctlclient_goes_over_tls_with_the_token(running, tmp_path, monkeypatch):
    r = running("required")
    a = signup(monkeypatch, tmp_path, "alice")
    m = Machine(r, "machine-a", a)
    try:
        assert [d["id"] for d in ctlclient.devices()] == ["machine-a"]
        assert ctlclient.resolve("machine-a")["id"] == "machine-a"
    finally:
        m.close()
    relayauth.clear()
    with pytest.raises(ctlclient.CtlClientError):
        ctlclient.devices()


def test_remotehost_end_to_end_over_tls(running, tmp_path, monkeypatch):
    """本物の remotehost が認証情報で繋ぎ、スマホ役の線が TLS と中継を通って機械の中まで届く。サインアウトで切れる。"""
    from websockets.asyncio.server import serve as aserve

    from laps import remotehost
    r = running("required")
    tok = signup(monkeypatch, tmp_path, "alice")
    home = tmp_path / "home"
    home.mkdir()
    stop_box = {}

    def machine():
        async def main():
            async def echo(ws):
                async for m in ws:
                    await ws.send("echo:" + m)
            stop = asyncio.Event()
            stop_box.update(loop=asyncio.get_running_loop(), stop=stop)
            async with serve_local(echo) as local:
                port = local.sockets[0].getsockname()[1]
                host = remotehost.Host(r.wss, "real-machine", f"http://127.0.0.1:{port}", "linux", None, home)
                await host.run_forever(stop)

        def serve_local(h):
            return aserve(h, "127.0.0.1", 0)
        asyncio.run(main())

    t = threading.Thread(target=machine, daemon=True)
    t.start()
    try:
        assert eventually(lambda: "real-machine" in r.relay.devices, 10)
        assert r.relay.devices["real-machine"].who["id"]
        assert r.relay.seen["real-machine"]["owner"]
        with relayauth.SyncLine(f"{r.wss}/d/real-machine/ws?x=1", ssl=relayauth.ssl_context(),
                                additional_headers={"X-Blackwall-Token": tok}, open_timeout=10) as phone:
            phone.send("hello")
            assert phone.recv(timeout=10) == "echo:hello"
            with pytest.raises(TimeoutError):
                phone.recv(timeout=0.2)                                # 同期の形の時間切れは TimeoutError
        relayauth.rpc("logout")
        assert eventually(lambda: "real-machine" not in r.relay.devices, 10)
    finally:
        stop_box["loop"].call_soon_threadsafe(stop_box["stop"].set)
        t.join(10)


def test_history_rebuilds_runs(monkeypatch):
    lines = [{"type": "init", "payload": {}},
             {"type": "run_spec", "ts": 1.0, "payload": {"prompt": "ログを見て"}},
             {"type": "tool_start", "ts": 2.0, "payload": {"name": "Bash", "input": {"command": "x" * 9000}, "call": "c1"}},
             {"type": "token", "ts": 2.1, "payload": {"text": "捨てる"}},
             {"type": "tool_end", "ts": 3.0, "payload": {"name": "Bash", "call": "c1", "ok": True, "result": "y" * 9000}},
             {"type": "assistant", "ts": 4.0, "payload": {"text": "見ました"}},
             {"type": "result", "ts": 5.0, "payload": {"text": "見ました", "outcome": "ok"}},
             {"type": "run_spec", "ts": 6.0, "payload": {"prompt": "続けて"}}]
    monkeypatch.setattr(relayauth, "http_get_text",
                        lambda path, **k: "\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    runs = relayauth.history("dev1", "20261031_010101_abcd")
    assert [r["prompt"] for r in runs] == ["ログを見て", "続けて"]
    assert [e["type"] for e in runs[0]["events"]] == ["tool_start", "tool_end", "assistant", "result"]
    assert runs[0]["finished"] is True and runs[1]["finished"] is False
    assert len(runs[0]["events"][0]["input"]["command"]) <= relayauth.HISTORY_TEXT_MAX + 1
    assert len(runs[0]["events"][1]["result"]) <= relayauth.HISTORY_TEXT_MAX + 1
    with pytest.raises(relayauth.AuthError):
        relayauth.history("../etc", "x")


def test_token_is_masked_in_records():
    from laps import redact
    raw = R.TOKEN_PREFIX + "Abcdefghijklmnopqrstuvwxyz0123456789_-Abcd"
    assert raw not in redact.redact(f"見つけた: {raw} です").text


def test_agent_cannot_read_or_overwrite_the_token_file():
    from laps import policy
    assert policy.check_read_path("~/.config/blackwall/auth.json").allowed is False
    assert policy.check_path("~/.config/blackwall/auth.json", write=True).allowed is False


def test_login_works_with_a_stale_token(running, tmp_path, monkeypatch):
    running("required")
    signup(monkeypatch, tmp_path, "alice")
    tok = relayauth.token()
    relayauth.rpc("logout")
    relayauth.save({"token": tok, "email": "alice@example.com"})         # 取り消された認証情報が手元に残っている
    assert relayauth.login("alice@example.com", "password-alice")["email"] == "alice@example.com"
    assert relayauth.token() != tok


def test_signing_out_releases_the_machine(running, tmp_path, monkeypatch):
    r = running("optional")
    a = signup(monkeypatch, tmp_path, "alice")
    m = Machine(r, "shared-pc", a)
    assert r.relay.seen["shared-pc"]["owner"]
    as_user(monkeypatch, tmp_path, "alice")
    relayauth.rpc("logout")
    assert eventually(lambda: "shared-pc" not in r.relay.devices)
    m.close()
    assert "owner" not in r.relay.seen["shared-pc"]
    b = signup(monkeypatch, tmp_path, "bob")                            # 同じ機械に別のアカウントで入り直せる
    m2 = Machine(r, "shared-pc", b)
    try:
        assert r.relay.seen["shared-pc"]["owner"]
    finally:
        m2.close()


def test_pairing_guesses_are_limited(running, tmp_path, monkeypatch):
    running("required")
    signup(monkeypatch, tmp_path, "alice")
    for _ in range(R.FAIL_MAX):
        with pytest.raises(relayauth.AuthError) as e:
            relayauth.rpc("pair_approve", user_code="ZZZZ-ZZZZ")
        assert e.value.code == "not_found"
    with pytest.raises(relayauth.AuthError) as e:
        relayauth.rpc("pair_approve", user_code="ZZZZ-ZZZZ")
    assert e.value.code == "too_many"


def test_blocked_proxy_falls_back_to_direct(running, tmp_path, monkeypatch):
    running("required")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def refuse():
        try:
            c, _ = srv.accept()
            c.recv(4096)
            c.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            c.close()
        except OSError:
            pass
    threading.Thread(target=refuse, daemon=True).start()
    monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    as_user(monkeypatch, tmp_path, "alice")
    try:
        assert relayauth.register("alice@example.com", "password-alice")["email"] == "alice@example.com"
    finally:
        srv.close()


def test_android_carries_the_same_certificate():
    root = __import__("pathlib").Path(R.__file__).resolve().parents[2]
    android = root / "android/app/src/main/res/raw/relay_cert.pem"
    if android.exists():
        assert android.read_bytes() == (root / "src/laps/relay_cert.pem").read_bytes()


def test_machine_only_credential_can_only_connect(running, tmp_path, monkeypatch):
    """作業場（workbox）のような機械に置く認証情報。漏れても、ほかの機械の一覧も操作の線も開けない。"""
    r = running("required")
    a = signup(monkeypatch, tmp_path, "alice")
    mine = Machine(r, "alice-pc", a)
    mine.index("sid-a")
    assert eventually(lambda: "sid-a" in (r.relay.index.get("alice-pc") or {}))
    as_user(monkeypatch, tmp_path, "sandbox")
    start = relayauth.rpc("pair_start", device={"name": "workbox", "kind": "linux"})
    as_user(monkeypatch, tmp_path, "alice")
    relayauth.rpc("pair_approve", user_code=start["user_code"], scope="machine")
    as_user(monkeypatch, tmp_path, "sandbox")
    tok = relayauth.rpc("pair_poll", code=start["code"])["token"]
    relayauth.save({"token": tok, "email": "alice@example.com"})
    box = Machine(r, "workbox", tok)
    try:
        assert box.first["items"] == []                                  # 繋いだときの目録は空
        assert r.relay.seen["workbox"]["owner"] == r.relay.seen["alice-pc"]["owner"]  # 持ち主はアカウント
        for path in ("/devices", "/conversations", "/server/metrics", "/d/alice-pc/health", "/d/workbox/health"):
            assert tls_status(r, path, tok) == 403, path
        for op, fields in (("sessions", {}), ("machines", {}), ("revoke", {"id": "x"}), ("forget_machine", {"id": "alice-pc"}),
                           ("password", {"old": "x", "new": "y" * 8}), ("pair_approve", {"user_code": "ABCD-2345"})):
            with pytest.raises(relayauth.AuthError) as e:
                relayauth.rpc(op, **fields)
            assert e.value.code == "forbidden", op
        # 同じアカウントの本物の端末からは、その機械が見えて操作の線も開ける。知らせは接続だけの機械に配らない
        assert {d["id"] for d in tls_get(r, "/devices", a)[1]} == {"alice-pc", "workbox"}
        mine.index("sid-a", "sid-a2")
        with pytest.raises(TimeoutError):
            while True:
                assert json.loads(box.ws.recv(timeout=0.5)).get("op") != "peer_conv"
        relayauth.rpc("logout")                                          # 自分のサインアウトはできる
        assert eventually(lambda: "workbox" not in r.relay.devices)
    finally:
        mine.close()
        box.close()


def test_approval_without_scope_stays_full(running, tmp_path, monkeypatch):
    """0.1.618 の画面は scope を送らない。今までどおり全部の権限で出す。"""
    r = running("required")
    signup(monkeypatch, tmp_path, "alice")
    as_user(monkeypatch, tmp_path, "gateway")
    start = relayauth.rpc("pair_start", device={"name": "gw", "kind": "linux"})
    as_user(monkeypatch, tmp_path, "alice")
    relayauth.rpc("pair_approve", user_code=start["user_code"])
    as_user(monkeypatch, tmp_path, "gateway")
    tok = relayauth.rpc("pair_poll", code=start["code"])["token"]
    assert tls_status(r, "/devices", tok) == 200
