"""保護ポートの設備を **kill の 2 つの門**でも守る（2026-09-05・第4回c）。

★塞いだ穴（反証が実物で再現した）: 保護ポート（既定 22 / 18301 / 18400）は
  `bash_output._stop_group`——**ポート経由の停止**にしか効いていなかった。
  作業場が `/home/user/project` のとき、本番の Android ゲートウェイ
  （`python -m laps.gateway`・cwd `/home/user/project/src`）は `in_workspace` の
  ②（cwd が作業場配下）で堂々と「中」に入るので、

    * `pkill -f gateway` → 名前撃ちゲートは inside=3 / outside=0 ＝**無変更で通す**
    * `kill <gateway の pid>`（`ss -ltnp` で引ける）→ PID ゲートも「中」＝通す

  となり、2026-09-03 の dogfood で実際に壊した相手をもう一度撃てた。
  pid は再起動のたびに変わるので、番号ではなく**ポートの持ち主**で引く。

この試験は**自分で起こした `http.server` だけ**を相手にする（本番のポートには
触らない）。保護ポートは `LAPS_PROTECTED_PORTS` で試験用に宣言する。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

from laps import procutil
from laps.tools import bash as B

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="/proc を読む")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def guarded_server(tmp_path, monkeypatch):
    """作業場の**中**で立てた自分の http.server を、保護ポートとして宣言する。

    ＝「作業場の中に居るのに撃たせない」＝本番のゲートウェイと同じ形。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    port = _free_port()
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", str(port))
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(ws), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        if procutil.port_in_use(port):
            break
        time.sleep(0.05)
    else:                                   # pragma: no cover
        proc.kill(); proc.wait(timeout=5)
        pytest.skip("http.server が上がらなかった")
    try:
        yield proc, port, str(ws)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_保護ポートの持ち主を引ける(guarded_server):
    proc, port, _ws = guarded_server
    assert proc.pid in procutil.protected_port_pids()
    # 保護の宣言を外せば、ただのプロセスに戻る（逃げ道を塞がない）。
    os.environ["LAPS_PROTECTED_PORTS"] = ""
    try:
        assert proc.pid not in procutil.protected_port_pids()
    finally:
        os.environ["LAPS_PROTECTED_PORTS"] = str(port)


def test_名前撃ちは保護ポートの設備を撃たない(guarded_server):
    """★`pkill -f` は本番で実際に使われる形。作業場の中に見えても落とす。"""
    proc, port, ws = guarded_server
    assert procutil.in_workspace(
        {"pid": proc.pid, "cwd": ws, "argv": (), "cmdline": ""}, ws), \
        "前提が崩れている（この設備は作業場の中に居るはず）"
    r = B._scope_name_kill(f'pkill -f "http.server {port}"', ws)
    assert r is not None, "保護ポートの設備を無変更で通した"
    assert r[0] == "true", r
    assert "保護ポート" in r[1], r[1]
    assert str(proc.pid) in r[1], r[1]


def test_PID撃ちも保護ポートの設備を撃たない(guarded_server):
    """★`ss -ltnp` で pid を引いて `kill <pid>` する形（案内文が勧める手順）。"""
    proc, port, ws = guarded_server
    r = B._scope_pid_kill(f"kill {proc.pid}", ws)
    assert r is not None, "保護ポートの設備を PID 名指しで撃てた"
    assert r[0] == "true", r
    assert "保護ポート" in r[1], r[1]


def test_run経由でも保護ポートの設備は生き残る(guarded_server):
    """★配線: `_run()` を通し、**実際に生き残ること**まで見る。"""
    proc, port, ws = guarded_server
    out = B._run({"command": f"kill -9 {proc.pid}"})
    assert "保護ポート" in out, out
    time.sleep(0.4)
    assert proc.poll() is None, "保護ポートの設備を撃ってしまった"
    out = B._run({"command": f'pkill -9 -f "http.server {port}"'})
    assert "保護ポート" in out, out
    time.sleep(0.4)
    assert proc.poll() is None, "名前撃ちで保護ポートの設備を撃ってしまった"


def test_保護を外せば自分のサーバは撃てる(guarded_server, monkeypatch):
    """★これは制限ではない: 宣言を外せば従来どおり自分の物として撃てる
    （逃げ道を塞ぐと run50 工程4 の「自分のサーバを止められない壁」が戻る）。"""
    proc, port, ws = guarded_server
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "")
    assert B._scope_pid_kill(f"kill {proc.pid}", ws) is None
