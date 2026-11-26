"""中継（relay.py）のアカウントの客（2026-10-31・標準ライブラリだけ）。

オーナー「自分のアカウントでログインしているデバイスならどこからでも見れるように」「利用にはアカウント登録が必要に」
「メールアドレスで登録の方が楽かも」「仕組みを似たような感じで使うってだけね、システムは自作する」。

- 認証情報の置き場は `~/.config/blackwall/auth.json`（0600）。中継へ繋ぐ部品（remotehost・relaybridge・ctlclient・labtool）は
  ここを読む。★環境変数には入れない（子のプロセス＝エージェントの Bash へ伝わって記録に残る）。`LAPS_RELAY_TOKEN` は
  明示して渡すときの上書きとして残してある。
- 認証情報とパスワードは TLS でしか送らない（手元 127.0.0.1 の試験の線を除く）。TLS の相手は、同梱の証明書
  `relay_cert.pem` そのもので確かめる（住所が変わっても通る・この証明書の鍵を持つ相手としか話さない）。
- 本文のあるやり取りは中継の WebSocket `/auth`。MCP サーバの環境には websockets が無いので、ctlclient の最小の受け手で話す。

人が使う口: `python -m laps.relayauth login|register|logout|whoami|approve`（`blackwall login` なども同じ）。
アプリが使う口: `python -m laps.relayauth --json`（標準入力に 1 行の JSON・パスワードを引数に出さない）。
"""
from __future__ import annotations

from . import paths as _lp
import argparse
import asyncio
import concurrent.futures
import getpass
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_RELAY = "ws://127.0.0.1:18400"   # 自分で立てた中継（laps.relay）の住所を LAPS_RELAY_URL で渡す
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
PAIR_POLL_SECS = 3.0
HISTORY_TEXT_MAX = 4000
_NO_TOKEN_OPS = ("register", "login", "pair_start", "pair_poll")
_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}")


class AuthError(Exception):
    """中継が断った・届かなかった。code は unreachable か、中継の error の語。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ── 置き場 ─────────────────────────────────────────────────────────
def auth_file() -> Path:
    f = (os.environ.get("LAPS_AUTH_FILE") or "").strip()
    return Path(f) if f else _lp.config_dir(xdg=False) / "auth.json"


def load() -> dict:
    try:
        d = json.loads(auth_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d: dict) -> None:
    f = auth_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.replace(tmp, f)


def clear() -> None:
    try:
        auth_file().unlink()
    except OSError:
        pass


def stamp() -> float:
    """認証情報のファイルの時刻（変わったら繋ぎ直す）。無ければ 0。"""
    try:
        return auth_file().stat().st_mtime
    except OSError:
        return 0.0


def token() -> str:
    return (os.environ.get("LAPS_RELAY_TOKEN") or "").strip() or str(load().get("token") or "")


def headers() -> dict[str, str]:
    t = token()
    return {"X-Blackwall-Token": t} if t else {}


# ── 線 ─────────────────────────────────────────────────────────────
def relay_url() -> str:
    return (os.environ.get("LAPS_RELAY_URL") or DEFAULT_RELAY).strip().rstrip("/")


def secure_url(url: str, *, has_secret: bool) -> str:
    """ws/wss の形にそろえる。秘密を載せる線は TLS にする（手元の試験の線を除く）。"""
    u = urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(u.scheme, u.scheme)
    if scheme == "ws" and has_secret and (u.hostname or "") not in _LOOPBACK:
        scheme = "wss"
    return urlunsplit((scheme, u.netloc, u.path.rstrip("/"), "", ""))


def _pinned_pem() -> str:
    f = (os.environ.get("LAPS_RELAY_CA") or "").strip()
    return Path(f if f else Path(__file__).with_name("relay_cert.pem")).read_text(encoding="ascii")


def ssl_context() -> ssl.SSLContext:
    """中継の証明書そのものを信頼の根にする（これ以外の相手とは話さない）。★住所では確かめない: 網の引っ越しで
    証明書を作り直さずに済むようにする（この証明書の鍵を持つ相手なら、どの住所でも本物）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=_pinned_pem())
    return ctx


def ws_connect_args(url: str | None = None) -> tuple[str, dict]:
    """websockets の connect に渡す (住所, 引数)。認証情報があれば TLS で見出しに載せる（住所に書かない）。"""
    h = headers()
    target = secure_url(url or relay_url(), has_secret=bool(h))
    kw: dict = {}
    if target.startswith("wss://"):
        kw["ssl"] = ssl_context()
    if h:
        kw["additional_headers"] = h
    return target, kw


class SyncLine:
    """同期の形（send・recv(timeout)・close・for）で使う中継の線。Lab 道具と labconsole が使う。

    ★websockets の同期の客を TLS で使わない: 受け取りの糸と送る糸が同じ TLS の物を同時に触るので、Python 3.12＋OpenSSL 3.0 では
      応答が来ないまま止まり、ときどきプロセスごと落ちる（2026-10-31 実測: 200 本中 20 本が止まり、1 回コアダンプ。
      同じ条件で asyncio の客は 300 本中 0 本）。ここは専用の糸で asyncio の客を動かす（TLS を触る糸は 1 本だけ）。"""

    def __init__(self, url: str, **kw) -> None:
        from websockets.asyncio.client import connect
        self._closed = False
        self._closing = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="bw-relay-line", daemon=True)
        self._thread.start()
        try:
            self._ws = self._call(self._open(connect, url, kw))
        except BaseException:
            self._stop()
            raise

    @staticmethod
    async def _open(connect, url: str, kw: dict):
        return await connect(url, **kw)

    def _call(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        while True:
            try:
                return fut.result(1.0)
            except concurrent.futures.TimeoutError:
                if fut.done():                 # 中の処理そのものの時間切れ
                    raise
                if not self._thread.is_alive():  # ★閉じた後の呼び出しで待ち続けない（割り込みが別の糸から閉じる）
                    fut.cancel()
                    raise ConnectionError("the line was closed") from None

    def _stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        if not self._thread.is_alive():
            self._loop.close()

    def send(self, message) -> None:
        self._call(self._ws.send(message))

    def recv(self, timeout: float | None = None):
        try:
            return self._call(asyncio.wait_for(self._ws.recv(), timeout))
        except asyncio.TimeoutError:
            raise TimeoutError("timed out while waiting for a message") from None

    def close(self, code: int = 1000, reason: str = "") -> None:
        """閉じる。★2 回目（割り込みと終わりの両方から呼ばれる）は何もしない。"""
        with self._closing:
            if self._closed:
                return
            self._closed = True
        try:
            self._call(self._ws.close(code, reason))
        finally:
            self._stop()

    @property
    def close_code(self):
        return self._ws.close_code

    def __iter__(self):
        from websockets.exceptions import ConnectionClosedOK
        try:
            while True:
                yield self.recv()
        except ConnectionClosedOK:
            return

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def http_get_text(path: str, *, relay: str | None = None, timeout: float = 6.0) -> str:
    h = headers()
    ws = secure_url(relay or relay_url(), has_secret=bool(h))
    tls = ws.startswith("wss://")
    url = ("https" + ws[3:] if tls else "http" + ws[2:]) + path
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context() if tls else None) as r:
        return r.read().decode("utf-8", "replace")


def http_get_json(path: str, *, relay: str | None = None, timeout: float = 6.0):
    return json.loads(http_get_text(path, relay=relay, timeout=timeout))


def _proxy_for(host: str) -> str:
    """OS の設定のプロキシ（urllib と同じ判定）。CONNECT で通せる http:// のときだけ使う。"""
    try:
        if urllib.request.proxy_bypass(host):
            return ""
        p = urllib.request.getproxies()
        got = p.get("https") or p.get("http") or p.get("all") or ""
    except Exception:  # noqa: BLE001 — 読めなければ直接
        return ""
    return got if got.startswith("http://") else ""


def _tcp(host: str, port: int, timeout: float) -> socket.socket:
    proxy = _proxy_for(host)
    if not proxy:
        return socket.create_connection((host, port), timeout=timeout)
    try:
        return _through_proxy(proxy, host, port, timeout)
    except OSError:                        # ★プロキシが通さなくても LAN の中なら直接届くことがある
        return socket.create_connection((host, port), timeout=timeout)


def _through_proxy(proxy: str, host: str, port: int, timeout: float) -> socket.socket:
    p = urlsplit(proxy)
    s = socket.create_connection((p.hostname or "", p.port or 80), timeout=timeout)
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk or len(head) > 65536:
            s.close()
            raise OSError("the proxy closed the connection")
        head += chunk
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 200" not in status:
        s.close()
        raise OSError(f"the proxy refused: {status}")
    return s


def open_socket(url: str, timeout: float) -> socket.socket:
    """ws:// か wss:// の相手へ TCP を開く（TLS ならその上に TLS を張る）。"""
    u = urlsplit(url)
    host = u.hostname or ""
    sock = _tcp(host, u.port or (443 if u.scheme == "wss" else 80), timeout)
    if u.scheme == "wss":
        try:
            return ssl_context().wrap_socket(sock, server_hostname=host)
        except Exception:
            sock.close()
            raise
    return sock


# ── /auth ─────────────────────────────────────────────────────────
def rpc(op: str, *, relay: str | None = None, timeout: float = 20.0, **fields) -> dict:
    """中継の /auth に 1 通送って答えを返す。★パスワードを運ぶ線なので、必ず TLS（手元の試験の線を除く）。"""
    from . import ctlclient
    url = secure_url(relay or relay_url(), has_secret=True)
    u = urlsplit(url)
    host, port = u.hostname or "", u.port or (443 if u.scheme == "wss" else 80)
    try:
        sock = open_socket(url, timeout)
    except (OSError, ssl.SSLError, ValueError) as e:
        raise AuthError("unreachable", f"中継 {host}:{port} に届きません（{type(e).__name__}: {e}）") from None
    try:
        try:
            # ★入り直す操作に古い認証情報を付けない（取り消された物を持っていると 401 で門前払いになる）
            ctlclient.ws_handshake(sock, host, port, "/auth", {} if op in _NO_TOKEN_OPS else headers())
        except ctlclient.CtlClientError as e:
            text = str(e)
            if " 401" in text:
                raise AuthError("unauthorized", "サインインし直してください") from None
            if " 404" in text:
                raise AuthError("unsupported", "この中継はアカウントに対応していません") from None
            raise AuthError("unreachable", text) from None
        sock.settimeout(timeout)
        try:
            ctlclient._send_frame(sock, 0x1, json.dumps({"op": op, **fields}, ensure_ascii=False).encode("utf-8"))
            reply = json.loads(ctlclient._recv_message(sock))
        except (OSError, ValueError, ctlclient.CtlClientError) as e:
            raise AuthError("unreachable", f"中継との線が切れました（{type(e).__name__}: {e}）") from None
    finally:
        try:
            ctlclient._send_frame(sock, 0x8, (1000).to_bytes(2, "big"))
        except OSError:
            pass
        sock.close()
    if not isinstance(reply, dict):
        raise AuthError("unreachable", "中継の答えの形が読めません")
    if not reply.get("ok"):
        raise AuthError(str(reply.get("error") or "failed"), str(reply.get("message") or ""))
    return reply


def _device() -> dict:
    kind = os.environ.get("LAPS_REMOTE_KIND") or ("windows" if os.name == "nt" else "linux")
    return {"name": socket.gethostname(), "kind": kind}


def issued_by(relay: str) -> bool:
    """手元の認証情報はこの中継が出した物か。別の中継（試験用など）の 401 で本物のサインインを消さないため。
    2026-10-31 試験が 127.0.0.1 の中継へ繋いで 401 を受け、開発機 の本物の auth.json を消した。開発機 は 2 時間、中継に繋がらなくなった。"""
    mine = str(load().get("relay") or "")
    if not mine:
        return True
    a, b = urlsplit(mine), urlsplit(relay)
    return (a.hostname, a.port) == (b.hostname, b.port)


def _forget_if_ours(relay: str | None) -> None:
    """取り消された認証情報を消す。★別の中継の 401 や、環境変数で渡した値の 401 では、手元の本物を消さない。"""
    if not (os.environ.get("LAPS_RELAY_TOKEN") or "").strip() and issued_by(relay or relay_url()):
        clear()


def _store(reply: dict, relay: str | None) -> dict:
    acc = reply.get("account") or {}
    save({"token": reply["token"], "email": acc.get("email") or "", "account": acc.get("id") or "",
          "relay": relay or relay_url(), "at": int(time.time())})
    return {"email": acc.get("email") or ""}


def register(email: str, password: str, relay: str | None = None) -> dict:
    return _store(rpc("register", relay=relay, email=email, password=password, device=_device()), relay)


def login(email: str, password: str, relay: str | None = None) -> dict:
    return _store(rpc("login", relay=relay, email=email, password=password, device=_device()), relay)


def logout(relay: str | None = None) -> None:
    """中継に取り消しを頼み、手元の認証情報を消す。★中継に届かなくても手元は消す（サインアウトできないと困る）。"""
    try:
        if token():
            rpc("logout", relay=relay)
    except AuthError:
        pass
    clear()


def status(relay: str | None = None) -> dict:
    """{signed_in, email, reachable, mode, signup, error}。★届かないときは手元の認証情報を信じる（手元の作業を止めない）。"""
    d = load()
    out = {"signed_in": bool(token()), "email": d.get("email") or "", "reachable": False,
           "mode": "", "signup": "", "error": ""}
    try:
        r = rpc("status", relay=relay, timeout=8.0)
    except AuthError as e:
        if e.code == "unauthorized":           # 取り消された認証情報。持っていても使えない
            _forget_if_ours(relay)
            out.update(signed_in=False, email="", reachable=True, error="signed_out")
        elif e.code == "unsupported":
            out.update(reachable=True, mode="off", error="unsupported")
        else:
            out["error"] = e.message
        return out
    out.update(reachable=True, mode=r.get("mode") or "", signup=r.get("signup") or "")
    if r.get("account"):
        out["email"] = r["account"].get("email") or out["email"]
    return out


def _clip(value, limit: int = HISTORY_TEXT_MAX):
    """道具の入力と結果の長い文字列を切る（中身は元の機械に残っている・画面に要るのは頭だけ）。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value[:200]]
    return value


def history(device: str, sid: str, relay: str | None = None, since_seq: int = 0, out_seq: list | None = None) -> list[dict]:
    """ほかの機械の会話の中身（走行ごと）。形は Rust の session_recover_run と同じ＝画面の組み立てをそのまま使える。
    ★since_seq（2026-11-27 オーナー「Claude に寄せて」）: その seq より後だけを読む。続きが前の走行の途中から始まるときは、
      最初の走行に `cont: True` を付けて返す（画面が控えの最後の走行に足す）。out_seq を渡すと、読んだ最大の seq を入れる。"""
    if not _ID_RE.fullmatch(device) or not _ID_RE.fullmatch(sid):
        raise AuthError("bad_request", "機械か会話の名札の形が違います")
    q = f"?since_seq={int(since_seq)}" if since_seq and int(since_seq) > 0 else ""
    try:
        body = http_get_text(f"/d/{quote(device)}/sessions/{quote(sid)}/events{q}", relay=relay, timeout=30.0)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AuthError("unauthorized", "サインインし直してください") from None
        if e.code == 503:
            raise AuthError("offline", "その機械はいま繋がっていません") from None
        raise AuthError("not_found", f"会話を読めません（HTTP {e.code}）") from None
    except (OSError, ssl.SSLError, ValueError) as e:
        raise AuthError("unreachable", f"中継に届きません（{type(e).__name__}: {e}）") from None
    runs: list[dict] = []
    cur: dict | None = None
    top = int(since_seq or 0)
    for line in body.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        ty, ts, p = o.get("type"), o.get("ts"), o.get("payload") if isinstance(o.get("payload"), dict) else {}
        try:
            top = max(top, int(o.get("seq") or 0))
        except (TypeError, ValueError):
            pass
        if ty == "run_spec":
            cur = {"prompt": str(p.get("prompt") or ""), "started": ts, "ended": ts, "finished": False, "events": []}
            runs.append(cur)
            continue
        if cur is None:
            if not since_seq:
                continue
            cur = {"prompt": "", "started": ts, "ended": ts, "finished": False, "events": [], "cont": True}
            runs.append(cur)
        if ty == "tool_start":
            ev = {"type": ty, "ts": ts, "name": p.get("name"), "input": _clip(p.get("input")), "call": p.get("call")}
        elif ty == "tool_end":
            ev = {"type": ty, "ts": ts, "name": p.get("name"), "call": p.get("call"), "ok": p.get("ok"),
                  "kind": p.get("kind"), "result": _clip(str(p.get("result") or ""))}
        elif ty == "assistant":
            ev = {"type": ty, "ts": ts, "text": p.get("text")}
        elif ty == "result":
            cur["finished"] = True
            ev = {"type": ty, "ts": ts, "text": p.get("text"), "outcome": p.get("outcome")}
        else:
            continue
        cur["ended"] = ts
        cur["events"].append(ev)
    if out_seq is not None:
        out_seq.append(top)
    return [r for r in runs if not r.get("cont") or r["events"]]


def pair_wait(relay: str | None = None, say=print) -> dict:
    """画面のない機械のサインイン。コードを出し、サインイン済みの端末で承認されるのを待つ。パスワードをこの機械で打たない。"""
    r = rpc("pair_start", relay=relay, device=_device())
    say(f"サインイン済みの端末の L.A.P.S で、次のコードを承認してください（{int(r.get('expires_in') or 600) // 60} 分間有効）:")
    say(f"    {r.get('user_code')}")
    say("  Windows のアプリ: Account → 画面のない機械を追加 ／ ほかの機械: blackwall approve <コード>")
    deadline = time.monotonic() + float(r.get("expires_in") or 600)
    while time.monotonic() < deadline:
        time.sleep(PAIR_POLL_SECS)
        got = rpc("pair_poll", relay=relay, code=r["code"])
        if not got.get("pending"):
            return _store(got, relay)
    raise AuthError("expired", "コードの期限が切れました")


# ── ほかの機械の操作の線 ───────────────────
#: アプリから中継越しに頼んでよい操作（laps/remotectl.py）。ファイルや機械の管理はここでは通さない。
CTL_OPS = ("machine.info", "chat.send", "chat.stop", "sessions.delete")


def ctl_call(device: str, op: str, args: dict | None = None, relay: str | None = None, timeout: float = 30.0) -> dict:
    """向こうの機械の操作の線へ 1 回だけ頼み、{"ok": …, "result": …} を返す。"""
    if not _ID_RE.fullmatch(device) or op not in CTL_OPS:
        raise AuthError("bad_request", f"その操作は頼めません: {op}")
    base, kw = ws_connect_args(relay)
    try:
        line = SyncLine(f"{base}/d/{quote(device)}/ws?ctl=1", max_size=8 * 1024 * 1024, open_timeout=15, **kw)
    except Exception as e:  # noqa: BLE001 — 繋がらない理由をそのまま渡す
        raise AuthError("unreachable", f"{device} に繋がりません: {e}") from None
    try:
        line.send(json.dumps({"type": "ctl", "id": "c-1", "op": op, "args": args or {}}, ensure_ascii=False))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                o = json.loads(line.recv(max(0.1, deadline - time.monotonic())))
            except (TimeoutError, ValueError):
                continue
            if isinstance(o, dict) and o.get("type") == "ctl_result" and o.get("id") == "c-1":
                return {"ok": bool(o.get("ok")), "result": o.get("result"), "error": o.get("error"), "message": o.get("message")}
        raise AuthError("timeout", f"{device} が答えません")
    except AuthError:
        raise
    except Exception as e:  # noqa: BLE001
        raise AuthError("unreachable", f"{device} との線が切れました: {e}") from None
    finally:
        line.close()


# ── /remote-control の窓 ───────────────────
#: 名乗り役が状態を書き直す間隔は 30 秒（remotehost._info_loop）。これより古ければ名乗り役は止まっている。
RC_STATUS_STALE = 90.0


def rc_info(sid: str = "", relay: str | None = None, home: str | os.PathLike | None = None, device: str = "") -> dict:
    """この機械が中継に繋がっているか、この会話をほかの端末で開く住所と QR（Claude の /remote-control の窓と同じ物）。網には出ない。"""
    from urllib.parse import urlsplit

    from . import qr
    h = Path(home or _lp.home_dir())
    if sid and not _ID_RE.fullmatch(sid):
        sid = ""
    out: dict = {"connected": False, "since": 0, "error": "", "device": "", "name": socket.gethostname(),
                 "email": str(load().get("email") or ""), "signed_in": bool(token())}
    try:
        out["device"] = (h / "device-id").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    f = h / "remote" / "status.json"
    try:
        st = json.loads(f.read_text(encoding="utf-8"))
        fresh = time.time() - f.stat().st_mtime < RC_STATUS_STALE
        out["connected"] = bool(st.get("connected")) and fresh
        out["since"] = st.get("since") or 0
        out["error"] = str(st.get("error") or "")[:300] if fresh else "この機械の名乗り役が動いていません"
    except (OSError, ValueError):
        out["error"] = "この機械の名乗り役が動いていません"
    if not out["device"]:
        return out
    base, _kw = ws_connect_args(relay)
    target = device if device and _ID_RE.fullmatch(device) else out["device"]    # ほかの機械の会話を開いているならその機械へ
    query = f"d={quote(target)}" + (f"&s={quote(sid)}" if sid else "")
    out["app"] = f"blackwall://open?{query}"
    out["web"] = f"https://{urlsplit(base).netloc}/#dev={quote(target)}" + (f"&sid={quote(sid)}" if sid else "")
    out["qr"] = qr.encode(out["app"])
    return out


# ── アプリの口（--json）────────────────────────────────────────────
def json_call(req: dict, relay: str | None = None) -> dict:
    """1 通の頼みを処理して答えを返す（例外にしない）。"""
    op = str(req.get("op") or "")
    try:
        if op == "local":                      # 手元だけ（網に出ない・見回り用）
            d = load()
            return {"ok": True, "signed_in": bool(token()), "email": d.get("email") or ""}
        if op == "status":
            return {"ok": True, **status(relay)}
        if op in ("register", "login"):
            fn = register if op == "register" else login
            return {"ok": True, **fn(str(req.get("email") or ""), str(req.get("password") or ""), relay)}
        if op == "logout":
            logout(relay)
            return {"ok": True}
        if op in ("sessions", "machines"):
            return {"ok": True, "items": rpc(op, relay=relay).get("items") or []}
        if op == "revoke":
            rpc("revoke", relay=relay, id=str(req.get("id") or ""))
            return {"ok": True}
        if op == "forget_machine":
            rpc("forget_machine", relay=relay, id=str(req.get("id") or ""))
            return {"ok": True}
        if op == "password":
            rpc("password", relay=relay, old=str(req.get("old") or ""), new=str(req.get("new") or ""))
            return {"ok": True}
        if op == "pair_approve":
            r = rpc("pair_approve", relay=relay, user_code=str(req.get("user_code") or ""),
                    scope=str(req.get("scope") or ""))
            return {"ok": True, "device": r.get("device") or {}}
        if op == "history":
            seq: list = []
            try:
                since = int(req.get("since_seq") or 0)
            except (TypeError, ValueError):
                since = 0
            runs = history(str(req.get("device") or ""), str(req.get("sid") or ""), relay, since_seq=since, out_seq=seq)
            return {"ok": True, "runs": runs, "seq": seq[0] if seq else since}
        if op == "conv_delete":                # 中継の写しと一覧から消す（relay 1.8・オーナー 11-11「アプリから消せるように」）
            r = rpc("conv_delete", relay=relay, device=str(req.get("device") or ""), sid=str(req.get("sid") or ""))
            return {"ok": True, "removed": bool(r.get("removed"))}
        if op == "ctl":
            a = req.get("args")
            return ctl_call(str(req.get("device") or ""), str(req.get("ctl") or ""), a if isinstance(a, dict) else {}, relay)
        if op == "rc":
            return {"ok": True, **rc_info(str(req.get("sid") or ""), relay, device=str(req.get("device") or ""))}
        return {"ok": False, "error": "bad_request", "message": f"知らない操作: {op}"}
    except AuthError as e:
        if e.code == "unauthorized":
            _forget_if_ours(relay)
        return {"ok": False, "error": e.code, "message": e.message}


# ── 人の口 ─────────────────────────────────────────────────────────
def _ask_password(twice: bool) -> str:
    pw = getpass.getpass("パスワード: ")
    if twice and pw != getpass.getpass("もう一度: "):
        raise AuthError("mismatch", "2 回の入力が違います")
    return pw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blackwall", description="L.A.P.S のアカウント")
    ap.add_argument("--json", action="store_true", help="標準入力の 1 行の JSON を処理する（アプリの口）")
    ap.add_argument("--relay", default=None, help="中継の住所（既定 LAPS_RELAY_URL か ws://127.0.0.1:18400）")
    ap.add_argument("what", nargs="?", choices=("login", "register", "logout", "whoami", "approve"))
    ap.add_argument("code", nargs="?", help="approve のコード")
    ap.add_argument("--password", action="store_true", help="login: コードの承認ではなくメールアドレスとパスワードで入る")
    ap.add_argument("--machine-only", action="store_true",
                    help="approve: 接続だけの認証情報にする（その機械からほかの機械を見ない・操作しない）")
    a = ap.parse_args(argv)
    if a.json:
        try:
            req = json.loads(sys.stdin.readline() or "{}")
        except ValueError:
            req = {}
        print(json.dumps(json_call(req if isinstance(req, dict) else {}, a.relay), ensure_ascii=False), flush=True)
        return 0
    try:
        if a.what == "register":
            email = input("メールアドレス: ").strip()
            print(f"登録しました: {register(email, _ask_password(True), a.relay)['email']}")
        elif a.what == "login":
            if a.password:
                email = input("メールアドレス: ").strip()
                print(f"サインインしました: {login(email, _ask_password(False), a.relay)['email']}")
            else:
                print(f"サインインしました: {pair_wait(a.relay)['email']}")
        elif a.what == "logout":
            logout(a.relay)
            print("サインアウトしました")
        elif a.what == "approve":
            if not a.code:
                print("コードを指定してください: blackwall approve ABCD-2345", file=sys.stderr)
                return 2
            r = rpc("pair_approve", relay=a.relay, user_code=a.code, scope="machine" if a.machine_only else "")
            print(f"承認しました: {(r.get('device') or {}).get('name') or 'その機械'}" + ("（接続だけ）" if a.machine_only else ""))
        else:
            s = status(a.relay)
            print(f"サインイン中: {s['email']}" if s["signed_in"] else "サインインしていません（blackwall login）")
            if not s["reachable"]:
                print(f"中継に届きません: {s['error']}")
    except AuthError as e:
        print(e.message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
