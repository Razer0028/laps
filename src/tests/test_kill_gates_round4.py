"""第4回c・**4 回目**の反証で実物が死んだ形を閉じる（2026-09-05）。

反証（`…/r4d/merge-bash-3/{p5,p6}.log`・保護ポートの自前サーバで再現）:
  名前撃ち  `pkill -9 -f X &` / `nohup pkill -9 -f X &` / `pkill -9 -f X | cat` /
            `pkill --signal 9 -f X`                          … **4 形が素通し**
  PID 撃ち  `kill -9 <pid> | cat` / `kill -9 +<pid>` / `true\\nkill -9 <pid>`
                                                             … **3 形が素通し**
どれも「門は入口に在るが、入口が断片の**先頭**しか見ない」形。PID 側の
`_parse_kill` は末尾の `&` を tail に剥がしていたのに、名前側の `_NAME_KILL_RE` は
`\\s*$` 固定で `&`・`|`・引数つき長オプションを受けられなかった＝この現場の常習
「**片方だけ直る**」。

★ここが守るのは 2 つ:
  ① 撃たせない相手（保護ポートの設備・作業場の外）には**綴りを変えても届かない**。
  ② 巻き添えが無いなら**1 文字も変えない**（正当な用途を潰さない）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import warnings

import pytest

from laps import procutil
from laps.tools import bash as B

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="/proc を読む")

WS = "/work/ws1"
#: 100 = 作業場の中の自分のサーバ／101 = 作業場の外の他人のサービス
TABLE = [
    (100, "python3", "python3 myserver.py --port 18100", "/work/ws1",
     ("python3", "myserver.py", "--port", "18100")),
    (101, "python3", "/usr/bin/python3 /srv/shared/scripts/corridor_bridge.py", "/",
     ("/usr/bin/python3", "/srv/shared/scripts/corridor_bridge.py")),
]


@pytest.fixture(autouse=True)
def _fake_proc(monkeypatch):
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE))
    monkeypatch.setattr(B, "_registered_bg_pids", lambda *a, **k: set())
    monkeypatch.setattr(B.sys, "platform", "linux")


# ── ①-a 名前撃ち: 末尾の `&` / `|` / 引数つき長オプション ────────────────
@pytest.mark.parametrize("cmd,want", [
    ("pkill -9 -f corridor_bridge &",            "true &"),
    ("nohup pkill -9 -f corridor_bridge &",      "nohup true &"),
    ("pkill -9 -f corridor_bridge | cat",        "true | cat"),
    ("pkill --signal 9 -f corridor_bridge",      "true"),
    ("pkill --signal=KILL -f corridor_bridge",   "true"),
    ("pkill -9 -f corridor_bridge 2>/dev/null &", "true 2>/dev/null &"),
    ("pkill -9 -f corridor_bridge & echo done",  "true & echo done"),
    ("sleep 0 | pkill -f corridor_bridge",       "sleep 0 | true"),
])
def test_R4_名前撃ちの末尾と長オプションを門に通す(cmd, want):
    r = B._scope_name_kill(cmd, WS)
    assert r is not None, f"素通し（反証で実物が死んだ形）: {cmd}"
    assert r[0] == want, (cmd, r[0])
    assert "corridor_bridge" in r[1], "落とした相手を列挙していない"


def test_R4_名前撃ちの末尾つきでも中の物は撃つ():
    """★断るのではなく**絞る**（狙いの外に当たる弾だけ落とす）。末尾は書き戻す。"""
    new, note = B._scope_name_kill("pkill -9 -f python3 &", WS)
    assert new == "kill -9 100 &", new
    new2, _ = B._scope_name_kill("pkill --signal HUP -f python3 | cat", WS)
    assert new2 == "kill -HUP 100 | cat", new2


def test_R4_killall_は複数の名前と_s_信号を受ける():
    """★`killall -s KILL` の `-s` は**信号**（`pkill -s` はセッション id＝別物）。
    旧 `_NAME_KILL_RE` は引数つきオプションを読めず、この形が素通しだった。"""
    new, note = B._scope_name_kill("killall -s KILL python3 nosuchproc", WS)
    assert new == "kill -KILL 100", new
    assert "corridor_bridge" in note
    # `-r` は名前（comm）への正規表現
    new2, _ = B._scope_name_kill("killall -r 'py.*3'", WS)
    assert new2 == "kill -TERM 100", new2


# ── ①-b PID 撃ち: `| cat` / `+pid` / 改行 ────────────────────────────────
@pytest.mark.parametrize("cmd,want", [
    ("kill -9 101 | cat",        "true | cat"),
    ("kill -9 +101",             "true"),
    ("kill -9 101 & echo done",  "true & echo done"),
    ("kill -9 101 2>/dev/null | tee log", "true 2>/dev/null | tee log"),
])
def test_R4_PID撃ちの末尾と符号つきを門に通す(cmd, want):
    r = B._scope_pid_kill(cmd, WS)
    assert r is not None, f"素通し（反証で実物が死んだ形）: {cmd}"
    assert r[0] == want, (cmd, r[0])


def test_R4_引用符つきの数字も門に通す():
    """★再生 ⑤ の盲点（`kill "12345"`）。bash は素通しで撃つのに、対象の白名簿が
    引用符を知らないので `_parse_kill` が None＝fail-open だった。綴りは変えずに
    中身だけ読む。"""
    r = B._scope_pid_kill('kill -9 "101"', WS)
    assert r is not None and r[0] == "true", r
    assert B._scope_pid_kill("kill -9 '100'", WS) is None, "自分の物まで書き換えた"
    # 解けない置き換えは従来どおり素通し（fail-open）
    assert B._scope_pid_kill('kill -9 "$pid"', WS) is None


def test_R4_改行は区切りとして割る():
    """★`true\\nkill -9 <pid>` は**どの門にも見えず**素通しだった（反証 B15）。"""
    r = B._scope_pid_kill("true\nkill -9 101", WS)
    assert r is not None, "改行で続く kill が素通し"
    assert r[0] == "true\ntrue", r[0]
    r2 = B._scope_name_kill("echo start\npkill -f corridor_bridge", WS)
    assert r2 is not None and r2[0] == "echo start\ntrue", r2


def test_R4_ヒアドキュメントの中身は割らない():
    """⚠ 取りこぼす方に倒す: 本文の 1 行が `kill 123` に見えるだけで書き換えると
    **中身を壊す**。`<<` が在る命令は改行で割らない。"""
    body = "cat <<EOF\nkill -9 101\nEOF"
    assert B._split_segments(body) == [body]
    assert B._scope_pid_kill(body, WS) is None


# ── ② 巻き添えが無ければ 1 文字も変えない（正当な用途 7 形） ──────────────
@pytest.mark.parametrize("cmd", [
    "pkill -f myserver",                 # 名前撃ち・素
    "pkill -9 -f myserver &",            # 名前撃ち・背景化
    "pkill --signal 9 -f myserver | cat",  # 名前撃ち・長オプション＋パイプ
    "kill -9 100",                       # PID・素
    "kill -9 100 | cat",                 # PID・パイプ
    "kill -9 +100",                      # PID・符号つき
    "kill -TERM 100 2>/dev/null &",      # PID・末尾つき
])
def test_R4_自分の物だけなら1文字も変えない(cmd):
    assert B._scope_name_kill(cmd, WS) is None, cmd
    assert B._scope_pid_kill(cmd, WS) is None, cmd


def test_R4_xargs_のファイル入力も解ける(tmp_path):
    """`kill $(cat f)` は解いていたのに `xargs kill < f` だけ「読めない＝断る」に
    落ちていた（反証 B8）。**自分の pid ファイルでも止められない**側の穴。"""
    (tmp_path / "mine.pid").write_text("100\n", encoding="utf-8")
    (tmp_path / "theirs.pid").write_text("101\n", encoding="utf-8")
    assert B._scope_xargs_kill("xargs kill -9 < mine.pid", WS,
                               base=str(tmp_path)) is None
    r = B._scope_xargs_kill("xargs kill -9 < theirs.pid", WS, base=str(tmp_path))
    assert r is not None and r[0] == "true", r
    assert "101" in r[1], r[1]


# ── 保護ポート: 空白だけの宣言で床を消さない ──────────────────────────────
def test_R4_空白だけの保護ポート宣言は既定を保って知らせる(monkeypatch):
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "   ")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        got = procutil.protected_ports()
    assert got == set(procutil._DEFAULT_PROTECTED_PORTS), got
    assert any("LAPS_PROTECTED_PORTS" in str(x.message) for x in w)
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "")
    assert procutil.protected_ports() == set(), "明示の空文字（逃げ道）を塞いだ"


# ── 所有権: `cd <ws>` の直後が改行でも作業場 ──────────────────────────────
def test_R4_改行で続くcdも作業場の所有():
    ws = "/work/ws1"
    row = {"pid": 5, "cwd": "/tmp",
           "argv": ("/bin/sh", "-c", f"cd {ws}\nphp -S 0.0.0.0:18300 -t site/"),
           "cmdline": f"/bin/sh -c cd {ws}\nphp -S 0.0.0.0:18300 -t site/"}
    assert procutil.in_workspace(row, ws) is True


# ── `_release_record`: 起動時刻は**起動直後に**控える ─────────────────────
def test_R4_release_recordは起動時に控えたtickを書く(tmp_path, monkeypatch):
    """★`proc.poll()` で刈り取った**後**に `/proc/<pgid>/stat` を読んでいたので
    `start_ticks` は必ず None・`started_at` は打ち切り時刻だった。すると
    `procutil.bg_record_matches()` の ①（tick 一致）にも ②（起動時刻）にも当たらず、
    **本物の逃げ子の記録が捨てられる**（時刻の関門の false green の根）。"""
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    dead = subprocess.Popen(["true"])
    dead.wait()

    class _P:
        pid = dead.pid

    class _Pump:
        def spill_to(self, p):
            p.write_text("", encoding="utf-8")

    import json
    note = B._release_record(_P(), "sleep 30 &", str(tmp_path), (_Pump(), _Pump()),
                             start_ticks=987654, started_at="2020-01-02T03:04:05",
                             launch_epoch=1577923445.0)
    meta = json.loads((tmp_path / "bg" / f"{dead.pid}.meta.json").read_text())
    assert meta["start_ticks"] == 987654, meta
    assert meta["started_at"] == "2020-01-02T03:04:05", meta
    assert isinstance(note, str)
    # 渡さなければ従来どおり（＝死んだ pid では None になる＝これが根因）
    B._release_record(_P(), "sleep 30 &", str(tmp_path), (_Pump(), _Pump()))
    meta2 = json.loads((tmp_path / "bg" / f"{dead.pid}.meta.json").read_text())
    assert meta2["start_ticks"] is None, meta2


# ── 実物で再現する（保護ポートの自前サーバを本番の入口 `_run` で撃つ） ─────
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def guarded(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    port = _free_port()
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", str(port))
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setattr(B, "_proc_table", procutil.proc_table)
    monkeypatch.setattr(B, "_registered_bg_pids", lambda *a, **k: set())
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(ws), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline and not procutil.port_in_use(port):
        time.sleep(0.05)
    if not procutil.port_in_use(port):                        # pragma: no cover
        proc.kill(); proc.wait(timeout=5)
        pytest.skip("http.server が上がらなかった")
    try:
        yield proc, port, str(ws)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.parametrize("shape", [
    "名前＋背景化", "nohup 名前＋背景", "名前＋パイプ", "名前 --signal",
    "PID＋パイプ", "PID +pid", "PID＋改行",
])
def test_R4_実物の保護ポートのサーバは7形すべてで生き残る(guarded, shape):
    """★反証と**同じ手順**（自前のサーバを保護ポートで宣言し、本番の入口
    `_run` に投げる）。4 形＋3 形とも、前の版では実際にサーバが死んだ。"""
    proc, port, ws = guarded
    pat = f"http.server {port}"
    cmd = {
        "名前＋背景化":     f'pkill -9 -f "{pat}" &',
        "nohup 名前＋背景": f'nohup pkill -9 -f "{pat}" &',
        "名前＋パイプ":     f'pkill -9 -f "{pat}" | cat',
        "名前 --signal":    f'pkill --signal 9 -f "{pat}"',
        "PID＋パイプ":      f"kill -9 {proc.pid} | cat",
        "PID +pid":         f"kill -9 +{proc.pid}",
        "PID＋改行":        f"true\nkill -9 {proc.pid}",
    }[shape]
    out = B._run({"command": cmd, "cwd": ws, "timeout": 20})
    text = out if isinstance(out, str) else out.text
    time.sleep(0.4)
    assert proc.poll() is None, f"{shape} で保護ポートの設備が死んだ: {cmd}\n{text}"
    assert "保護ポート" in text or "撃っていません" in text, text
