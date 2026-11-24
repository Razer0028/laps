"""中継の向こうの機械の操作（`/d/<機械>/ws?ctl=1`）を 1 つ呼ぶ（2026-10-14・依存なし）。

オーナー（2026-10-14）「B で進めて」＝Claude Code の MCP（blackwall-mcp）から、Blackwall のアプリが動いている
Windows の画面を操作する。画面の操作はその機械のゲートウェイ（アプリが起こす＝利用者がログインしている画面の
セッションで動く）が受ける（remotectl.desktop）。SSH の命令は画面の無い Session 0 で動くので窓に届かない。

★標準ライブラリだけで書く: blackwall-mcp（開発機 の /opt/blackwall-mcp）の環境には websockets が無く、MCP サーバは
  依存なしで作ってある。中継（websockets のサーバ）とだけ話すので、RFC 6455 の最小の受け手で足りる
  （テキストの枠・分割・ping・close）。
中継の宛先は Lab 道具（oc/labtool）と同じ LAPS_RELAY_URL。認証情報と TLS は laps/relayauth.py（2026-10-31）。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
import uuid
from urllib.parse import quote, urlsplit

from . import relayauth


class CtlClientError(Exception):
    pass


def relay_url() -> str:
    return relayauth.relay_url()


#: この機械の認証情報が「名乗るだけ」（中継の MACHINE_SCOPE）で、一覧と操作の線を断られた。以後は認証情報を付けずに頼む。
#: ★2026-11-14: 開発機 の root の認証情報は名乗るだけの物（だれでもコマンドを走らせられる機械のため）。それを付けて頼むと中継は 403 を
#:   返し、MCP の desktop が全部使えなかった。中継がアカウントを必須にしていない間は、付けなければ通る（付けない客と同じ扱い）。
#:   必須にした後は 401 になり、「blackwall login が要る」と分かる。
_machine_only = False


def _headers() -> dict[str, str]:
    return {} if _machine_only else relayauth.headers()


def _get_devices(timeout: float):
    import urllib.error
    import urllib.request
    h = _headers()
    ws = relayauth.secure_url(relay_url(), has_secret=bool(h))
    tls = ws.startswith("wss://")
    req = urllib.request.Request(("https" + ws[3:] if tls else "http" + ws[2:]) + "/devices", headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=relayauth.ssl_context() if tls else None) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def devices(timeout: float = 6.0) -> list[dict]:
    """中継に名乗っている機械の一覧（id・name・online・version など）。"""
    global _machine_only
    import urllib.error
    relay = relay_url()
    try:
        try:
            rows = _get_devices(timeout)
        except urllib.error.HTTPError as e:
            if e.code != 403 or _machine_only or not relayauth.headers():
                raise
            _machine_only = True               # 名乗るだけの認証情報だった。付けずにもう一度
            rows = _get_devices(timeout)
    except urllib.error.HTTPError as e:
        hint = " — sign in on this machine with `blackwall login`" if e.code in (401, 403) else ""
        raise CtlClientError(f"the relay {relay} refused the request (HTTP {e.code}){hint}") from None
    except Exception as e:  # noqa: BLE001
        raise CtlClientError(f"the relay {relay} did not answer ({type(e).__name__}: {e})") from None
    if isinstance(rows, dict):
        rows = rows.get("devices") or []
    return [r for r in rows if isinstance(r, dict)]


def resolve(machine: str) -> dict:
    """名札（id）か機械の名前（大文字小文字を問わない）から 1 台を選ぶ。繋がっている物を優先する。"""
    want = (machine or "").strip()
    rows = devices()
    hit = [r for r in rows if str(r.get("id") or "") == want] or \
          [r for r in rows if str(r.get("name") or "").lower() == want.lower()]
    if not hit:
        names = ", ".join(f"{r.get('name')} ({r.get('id')})" for r in rows) or "none"
        raise CtlClientError(f"no machine '{want}' on the relay. Machines: {names}")
    online = [r for r in hit if r.get("online", True)]
    if not online:
        raise CtlClientError(f"{want} is registered on the relay but offline now")
    return online[0]


# ── RFC 6455 の最小の受け手（平文の ws・こちらから送る枠はマスクする）─────────────────────────────

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise CtlClientError("the relay closed the connection")
        buf += chunk
    return buf


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", n)
    mask = os.urandom(4)
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _recv_message(sock: socket.socket) -> str:
    """テキストの 1 通（分割された枠はつなぐ）。ping には pong を返す。close なら誤りにする。"""
    parts: list[bytes] = []
    while True:
        b1, b2 = _recv_exact(sock, 2)
        fin, opcode = b1 & 0x80, b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", _recv_exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack("!Q", _recv_exact(sock, 8))[0]
        mask = _recv_exact(sock, 4) if b2 & 0x80 else b""
        data = _recv_exact(sock, n) if n else b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x9:                       # ping
            _send_frame(sock, 0xA, data)
            continue
        if opcode == 0xA:                       # pong
            continue
        if opcode == 0x8:                       # close
            code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 0
            raise CtlClientError(f"the line was closed ({code} {data[2:].decode('utf-8', 'replace')})".strip())
        parts.append(data)
        if fin:
            return b"".join(parts).decode("utf-8", "replace")


def _open(device_id: str, timeout: float) -> socket.socket:
    global _machine_only
    if not _machine_only and relayauth.headers():
        try:
            return _open_with(device_id, timeout, relayauth.headers())
        except CtlClientError as e:
            if " 403 " not in str(e) + " ":
                raise
            _machine_only = True               # 名乗るだけの認証情報だった。付けずにもう一度
    return _open_with(device_id, timeout, _headers())


def _open_with(device_id: str, timeout: float, h: dict[str, str]) -> socket.socket:
    url = relayauth.secure_url(relay_url(), has_secret=bool(h))
    u = urlsplit(url)
    if u.scheme not in ("ws", "wss"):
        raise CtlClientError(f"only ws:// and wss:// relays are supported here (got {u.scheme}://)")
    host, port = u.hostname or "", u.port or (443 if u.scheme == "wss" else 80)
    try:
        sock = relayauth.open_socket(url, timeout)
    except (OSError, ssl.SSLError, ValueError) as e:
        raise CtlClientError(f"could not reach the relay {host}:{port} ({e})") from None
    ws_handshake(sock, host, port, f"/d/{quote(device_id)}/ws?ctl=1", h)
    return sock


def ws_handshake(sock: socket.socket, host: str, port: int, path: str, headers: dict[str, str]) -> None:
    """WebSocket の握手。101 でなければ線を閉じて誤りにする（誤りの文に応答の 1 行目を入れる）。"""
    key = base64.b64encode(os.urandom(16)).decode()
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise CtlClientError("the relay closed the connection during the handshake")
        head += chunk
        if len(head) > 65536:
            sock.close()
            raise CtlClientError("the relay sent an oversized handshake")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 101 " not in status + " ":
        sock.close()
        raise CtlClientError(f"the relay refused the line: {status}")


def call(device_id: str, op: str, args: dict, timeout: float = 120.0) -> dict:
    """操作を 1 つ送り、答え（ok・result または error・message）を返す。"""
    deadline = time.monotonic() + timeout
    sock = _open(device_id, min(timeout, 25.0))
    rid = "mcp-" + uuid.uuid4().hex[:12]
    try:
        _send_frame(sock, 0x1, json.dumps({"type": "ctl", "id": rid, "op": op, "args": args},
                                          ensure_ascii=False).encode("utf-8"))
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CtlClientError(f"no answer from the machine within {timeout:.0f} s")
            sock.settimeout(left)
            try:
                msg = _recv_message(sock)
            except socket.timeout:
                raise CtlClientError(f"no answer from the machine within {timeout:.0f} s") from None
            try:
                obj = json.loads(msg)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "ctl_result" and obj.get("id") == rid:
                return obj
    finally:
        try:
            _send_frame(sock, 0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        sock.close()
