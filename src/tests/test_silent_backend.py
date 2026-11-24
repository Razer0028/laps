"""沈黙する推論機を 600 秒待たない。

★実機で起きた形（2026-08-23・v0.1.87）:
  「600 秒のあいだ Python から1バイトも届きませんでした」で走行が落ち、
  利用者は「まだ最初の応答が来ていません（455秒）」を見続けた。
  送り先は手元の推論器（model: None）で、P40 には要求が1件も届いていなかった。

  原因は3重だった:
    1. httpx.Timeout(600) ＝ 繋がったあと1バイトも来なくても 600 秒待つ
    2. ReadTimeout をリトライしていた（4秒が 22.5 秒に膨らむ・実測）
    3. 逐次配信が失敗すると一括経路へ落ち、**もう一度**待ってリトライ

★止まるより失敗するほうがよい。失敗は読めるが、沈黙は読めない。
"""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from laps.backend import Backend, BackendConfig


@pytest.fixture
def silent_server():
    """繋がるが1バイトも返さない。"""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(4)
    port = srv.getsockname()[1]
    keep = []
    def acc():
        try:
            while True: keep.append(srv.accept()[0])
        except Exception: pass
    threading.Thread(target=acc, daemon=True).start()
    yield port
    srv.close()


@pytest.fixture(autouse=True)
def _short(monkeypatch):
    monkeypatch.setenv("LAPS_READ_TIMEOUT", "2")


def _backend(port):
    return Backend(BackendConfig(base_url=f"http://127.0.0.1:{port}/v1", model="x"))


def test_chat_gives_up_fast_and_names_the_target(silent_server):
    b = _backend(silent_server)
    t = time.time()
    with pytest.raises(RuntimeError) as ei:
        b.chat([{"role": "user", "content": "hi"}], max_tokens=5)
    assert time.time() - t < 6, "沈黙する相手にリトライしている"
    assert str(silent_server) in str(ei.value), "どこへ送ったかを言っていない"
    assert "Connection" in str(ei.value), "次の一手（Connection を確かめる）を言っていない"


@pytest.mark.parametrize("streaming", [False, True])
def test_tools_path_gives_up_fast_on_both_routes(silent_server, streaming):
    """★逐次と一括の両方。片方だけ直すと、もう片方が二重に待つ。"""
    b = _backend(silent_server)
    tools = [{"type": "function", "function": {"name": "Bash",
              "parameters": {"type": "object", "properties": {}}}}]
    kw = {"on_token": (lambda s: None)} if streaming else {}
    t = time.time()
    with pytest.raises(RuntimeError):
        b.chat_with_tools([{"role": "user", "content": "hi"}], tools, max_tokens=5, **kw)
    assert time.time() - t < 6, f"{'逐次' if streaming else '一括'} が沈黙する相手を待ちすぎ"


def test_default_read_timeout_is_not_600(monkeypatch):
    """既定の応答待ちの門。★2026-10-18 更新: 2026-10-06 にオーナー「タイムアウトまでの時間を延長して」で既定を 600 秒に
    延ばした（backend.READ_TIMEOUT_DEFAULT の注記・密の 27B で窓を読み直すと約 300 秒）。この門はそれより前の「120 秒以下」の
    ままで赤だった。いまの決まり: 既定は READ_TIMEOUT_DEFAULT で、最初の 1 byte の番犬より長い（番犬の順を保つ）。"""
    from laps import backend as _b
    monkeypatch.delenv("LAPS_READ_TIMEOUT", raising=False)
    monkeypatch.delenv("LAPS_FIRST_BYTE_TIMEOUT", raising=False)
    b = _backend(1)
    c = b._new_client()
    assert c.timeout.read is not None and c.timeout.read == float(_b.READ_TIMEOUT_DEFAULT), \
        f"read timeout が既定と違う: {c.timeout.read}"
    assert _b.FIRST_BYTE_TIMEOUT_DEFAULT < c.timeout.read
