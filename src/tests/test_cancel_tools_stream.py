"""中断は**道具付きの逐次配信**も切る（2026-09-03 dogfood: 最初の Ctrl-C が 73 秒止まらなかった）。

既存の test_cancel_aborts_inflight_stream.py は `_chat_stream`（道具なし）だけを見ていた。
実機のエージェントが使うのは `chat_with_tools`（native tool-calling）で、そこには
**逐次が失敗したら非逐次へ落ちる**フォールバックがある。中断で socket を落とすと
その失敗経路に入り、閉じた/黙った相手へ**もう一度 POST を送り直して**塞がりうる。

ここで確かめること（本物の `Backend.chat_with_tools` / `close` / `abort_inflight` を呼ぶ）:
  1. 実際の中断経路 `backend.close()`（＝request_cancel が呼ぶもの・`_hard_closed` を立てる）は、
     応答ヘッダの**前**（prefill＝slot 待ち）でも、ヘッダの**後**（生成中）でも、
     2 秒以内に呼び出しを戻す。
  2. `abort_inflight()` が何を切ったかを `last_abort`（item B）に残す。
  3. 中断（_hard_closed）後のフォールバックは一括経路へ落ちない＝黙った相手へ再送しない。

生の socket サーバを使うのは、応答ヘッダを**いつ返すか（あるいは一切返さないか）**を
こちらで完全に握るため（llama-server は起こさない・ただの HTTP）。
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from laps.backend import Backend, BackendConfig

_TOOLS = [{"type": "function",
           "function": {"name": "f", "parameters": {"type": "object"}}}]


class _SilentPeer:
    """要求は受け取るが、mode により応答を返さない偽推論機。

    never_headers      : 応答ヘッダを一切返さない（prefill / slot 待ちを模す）
    headers_then_silent: 200 + SSE ヘッダと 1 チャンクを返し、その後黙る（生成中）
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._ls.bind(("127.0.0.1", 0))
        self._ls.listen(5)
        self.port = self._ls.getsockname()[1]
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self._ls.accept()
            except OSError:
                return
            self._conns.append(c)
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c: socket.socket) -> None:
        try:
            c.recv(65536)                       # 要求を読み捨てる
            if self.mode == "never_headers":
                while True:
                    time.sleep(0.2)
            else:
                hdr = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                       b"Transfer-Encoding: chunked\r\n\r\n")
                body = b'data: {"choices":[{"delta":{"content":"hi "}}]}\n\n'
                chunk = ("%x\r\n" % len(body)).encode() + body + b"\r\n"
                c.sendall(hdr + chunk)
                while True:
                    time.sleep(0.2)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._ls.close()
        except OSError:
            pass
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass


@pytest.fixture(params=["never_headers", "headers_then_silent"])
def silent_peer(request):
    p = _SilentPeer(request.param)
    yield p
    p.close()


def _backend(peer: _SilentPeer) -> Backend:
    return Backend(BackendConfig(base_url=f"http://127.0.0.1:{peer.port}/v1",
                                 api_key="x", model="m", max_retries=0))


def test_close_aborts_tools_stream_on_silent_peer(silent_peer):
    """本命: 実際の中断経路 close() が、道具付き逐次を黙った相手からもぎ取る。"""
    b = _backend(silent_peer)
    ended = threading.Event()
    err: list[str] = []

    def run():
        try:
            b.chat_with_tools([{"role": "user", "content": "x"}], tools=_TOOLS,
                              on_token=lambda t: None, max_tokens=10)
        except BaseException as e:              # 中断は例外で戻る
            err.append(type(e).__name__)
        finally:
            ended.set()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(1.0)                             # 要求は送られ、応答待ちで blocked
    t0 = time.monotonic()
    b.close()                                   # ＝request_cancel が呼ぶもの
    assert ended.wait(timeout=2.0), (
        f"[{silent_peer.mode}] close() しても道具付き逐次が戻らない"
        "（フォールバックが黙った相手へ再送して塞がる旧挙動）")
    assert time.monotonic() - t0 < 2.0
    assert err, "中断が例外として伝わっていない"


def test_abort_inflight_records_what_it_severed(silent_peer):
    """item B: abort が掴んだ socket 数・所要を last_abort に残す。"""
    b = _backend(silent_peer)
    started = threading.Event()

    def run():
        try:
            started.set()
            b.chat_with_tools([{"role": "user", "content": "x"}], tools=_TOOLS,
                              on_token=lambda t: None, max_tokens=10)
        except BaseException:
            pass

    threading.Thread(target=run, daemon=True).start()
    started.wait(2.0)
    time.sleep(1.0)                             # 接続が pool に載るのを待つ
    n = b.abort_inflight()
    la = b.last_abort
    assert set(la) >= {"sockets", "inflight", "connections", "elapsed_ms"}
    assert la["sockets"] == n and isinstance(n, int)
    # 黙った相手でも接続は張れている＝socket を1本は掴めるはず（掴めない＝要求送信中で
    # pool を辿れなかった、という別状態。ここでは 1s 待ってから切るので掴める）。
    assert n >= 1, f"[{silent_peer.mode}] abort が socket を1本も掴めていない: {la}"
    b.close()


def test_cancelled_tools_stream_does_not_resend_to_silent_peer(silent_peer):
    """中断（_hard_closed）後は逐次の失敗を一括へ落とさない＝黙った相手へ再送しない。

    close() で `_hard_closed` を立ててから道具付き逐次を呼ぶと、`_ensure_open` は
    開き直さず、フォールバックは `if self._hard_closed: raise` で一括を試みない。
    黙った相手への 2 度目の POST（read timeout まで塞がる）が起きないことを、
    **戻るまでの時間**で見る。"""
    b = _backend(silent_peer)
    b.close()                                   # _hard_closed = True
    t0 = time.monotonic()
    with pytest.raises(Exception):
        b.chat_with_tools([{"role": "user", "content": "x"}], tools=_TOOLS,
                          on_token=lambda t: None, max_tokens=5)
    assert time.monotonic() - t0 < 2.0, (
        f"[{silent_peer.mode}] 中断後に黙った相手へ再送して塞がっている")
