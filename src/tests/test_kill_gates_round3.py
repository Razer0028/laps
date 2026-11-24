"""kill の床の**再反証**で実物が死んだ 5 か所（2026-09-05・第4回c の 3 回目）。

反証役が隔離環境で再現した must_fix と、この試験の対応:

  M1 保護ポートの門は 2 つでは足りない。`BashOutput(pid=…, kill=true)` は
     `_stop_group(pg, port=_meta_port(meta))` を呼ぶが、`_meta_port` は
     **台帳の `cmd` から番号を正規表現で拾う**だけなので、`./run-server.sh` の
     ように綴りに番号が無ければ `port=None` ＝**保護判定が一度も走らない**。
     実測: 保護ポート 19911 を握る自分のサーバが `OK: 止まりました` で死んだ。
  M2 床が綴り依存だった。`sh -c "kill …"`・`bash -c 'pkill …'`・`( kill … )`・
     `eval kill …`・`xargs kill -9 <<< <pid>`・`python3 -c "os.kill(…)"` の
     **6/6** で保護ポートの設備が死んだ（前の版の「綴りに依存しない」は撤回）。
  M3 引用符の中の `cd <ws>`（`grep -rn "cd <ws>" /etc` 等）が所有の証拠になっていた。
  M4 `protected_ports()` が空白/改行区切り・`abc`・`0x4ce0` で**黙って保護ゼロ**。
  M5 断り文が案内する `BashOutput(pid=…, kill=true)` が作業場を見ておらず、
     兄弟の作業場 B から A の背景処理を実際に止められた。

★この試験は**自分で起こした `http.server` だけ**を相手にする（本番のポート
  22 / 18301 / 18400 には触らない）。保護は `LAPS_PROTECTED_PORTS` で宣言する。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import warnings

import pytest

from laps import procutil
from laps.tools import bash as B
from laps.tools import bash_output as BO

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="/proc を読む")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(port: int, up: bool = True, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if procutil.port_in_use(port) is up:
            return True
        time.sleep(0.05)
    return procutil.port_in_use(port) is up


@pytest.fixture()
def guarded(tmp_path, monkeypatch):
    """作業場の**中**の自分の http.server を「保護ポートの設備」として宣言する。

    ＝本番の Android ゲートウェイ（cwd が作業場の中に見える）と同じ形。
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
    if not _wait_port(port):                                  # pragma: no cover
        proc.kill(); proc.wait(timeout=5)
        pytest.skip("http.server が上がらなかった")
    try:
        yield proc, port, str(ws)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


# ── M2: 綴りの中に隠れた kill ────────────────────────────────────────────
@pytest.mark.parametrize("shape", ["sh -c", "bash -c", "( )", "eval", "xargs"])
def test_M2_綴りの中のkillも保護ポートの設備を撃たない(guarded, shape):
    """★実物で 6/6 死んだ形のうち、シェル構文の 5 つ。

    `python3 -c "os.kill(…)"` は**この層では塞げない**（下の別の試験が明記する）。
    """
    proc, port, ws = guarded
    cmd = {
        "sh -c": f'sh -c "kill -9 {proc.pid}"',
        "bash -c": f"bash -c 'pkill -f \"http.server {port}\"'",
        "( )": f"( kill -9 {proc.pid} )",
        "eval": f"eval kill {proc.pid}",
        "xargs": f"xargs kill -9 <<< {proc.pid}",
    }[shape]
    r = B._scope_nested_kill(cmd, ws, base=ws)
    assert r is not None, f"{shape} は無変更で通った（保護ポートの設備を撃てる）"
    assert "kill" not in r[0].replace("killall", ""), r[0]
    assert proc.poll() is None


def test_M2_run経由でも綴りの中のkillは設備を殺さない(guarded):
    """★配線: `_run()` を通し、**実際に生き残ること**まで見る。"""
    proc, port, ws = guarded
    for cmd in (f'sh -c "kill -9 {proc.pid}"',
                f"( kill -9 {proc.pid} )",
                f"eval kill -9 {proc.pid}",
                f"xargs kill -9 <<< {proc.pid}"):
        out = B._run({"command": cmd, "timeout": 20})
        time.sleep(0.3)
        assert proc.poll() is None, f"{cmd} で保護ポートの設備を撃ってしまった: {out}"
        assert "保護ポート" in out or "実行しません" in out, (cmd, out)


def test_M2_pythonCは塞げないことを明記する(guarded):
    """★正直に残す穴。シェル構文ではないので、この層は読み解かない。

    「塞いだ」と書いて塞げていないより、**塞げないと書いてある**ほうがよい
    （この現場が何度も踏んだ『看板だけ正しい』を作らない）。
    """
    proc, port, ws = guarded
    cmd = f'python3 -c "import os; os.kill({proc.pid}, 9)"'
    assert B._scope_nested_kill(cmd, ws, base=ws) is None
    assert "python" in B._nested_shell_spans.__doc__
    assert "塞げない" in B._nested_shell_spans.__doc__


def test_M2_解けないxargsは断る(guarded):
    """`ps|grep|awk|xargs kill` は対象が読めない＝断る（実記録に在る形）。"""
    proc, port, ws = guarded
    r = B._scope_nested_kill("ps aux | grep php | awk '{print $2}' | xargs kill",
                             ws, base=ws)
    assert r is not None and r[0] == "true", r
    assert "標準入力" in r[1], r[1]


def test_M2_中が全部自分の物なら一文字も変えない(tmp_path, monkeypatch):
    """★これは制限ではない（run50 工程4 の「自分のサーバを止められない壁」を戻さない）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "")
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(ws), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_port(port)
        assert B._scope_nested_kill(f'sh -c "kill {proc.pid}"', str(ws),
                                    base=str(ws)) is None
        assert B._scope_nested_kill("sh -c 'ls -la'", str(ws), base=str(ws)) is None
        assert B._scope_nested_kill("echo hello", str(ws), base=str(ws)) is None
    finally:
        proc.kill(); proc.wait(timeout=5)


# ── M1 / M5: BashOutput(pid=…, kill=true) の 3 つ目の門と作業場 ──────────
@pytest.fixture()
def bg_guarded(tmp_path, monkeypatch):
    """**綴りに番号の出ない**起動（`./run-server.sh`）で保護ポートを握らせ、
    それを台帳（`_spawn_background`）に載せる＝M1 の実物の形。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    port = _free_port()
    script = ws / "run-server.sh"
    script.write_text(
        f"#!/bin/sh\nexec {sys.executable} -m http.server {port} --bind 127.0.0.1\n",
        encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", str(port))
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    out = B._spawn_background("./run-server.sh", str(ws))
    pid = int(out.split("pid: ")[1].split("\n")[0])
    if not _wait_port(port):                                  # pragma: no cover
        pytest.skip("run-server.sh が上がらなかった")
    try:
        yield pid, port, str(ws)
    finally:
        try:
            os.killpg(pid, 9)
        except OSError:
            pass
        _wait_port(port, up=False, timeout=3)


def test_M1_台帳のcmdに番号が無くても保護ポートで断る(bg_guarded):
    """★実測で死んだ経路そのもの。`_meta_port` は None を返す（前提）。"""
    pid, port, ws = bg_guarded
    meta = json.loads((procutil.bg_dir() / f"{pid}.meta.json").read_text(encoding="utf-8"))
    assert BO._meta_port(meta) is None, "前提が崩れている（cmd に番号が出てはいけない）"
    assert pid in procutil.protected_port_pids() \
        or bool(set(procutil.protected_port_pids()) & {m["pid"] for m in BO._group_members(pid)})
    out = BO._run({"pid": pid, "kill": True})
    # ★統合（2026-09-05・lifecycle と合流）で**断りの文言が 2 通り**になった。
    #   lifecycle 側は撃つ前に `/proc/net/tcp` を読んで**実 LISTEN を観測**するので、
    #   台帳の綴りに番号が無くても第 1 の門（観測値 × 保護ポート）が先に鳴る
    #   ＝「ポート N は保護された設備です」。第 3 の門（`_protected_group_pids`）は
    #   観測が届かないときの受け皿として残る。どちらでも**撃たない**ことが要件。
    assert ("保護ポート" in out) or ("保護された設備" in out), out
    time.sleep(0.3)
    assert procutil.port_in_use(port), "保護ポートの設備を BashOutput で撃ってしまった"
    # ★第 3 の門そのものが生きていること（＝上の緑が観測の門だけに支えられていない）。
    assert BO._protected_group_pids(pid), "第 3 の門（ポートの持ち主で引く）が空になっている"


def test_M1_観測が届かなくても第3の門が保護ポートを守る(bg_guarded, monkeypatch):
    """★第 3 の門の**単独**の証明（2026-09-05・統合で追加）。

    統合後は実 LISTEN の観測が先に鳴るので、M1 の緑が
    「観測の門だけで立っている」のか「番号に依らない門が本当に在る」のかを
    区別できない。ここでは**観測を目隠しして**（`_observe_listen` の `ports` を
    空にする）、台帳の綴りにも番号が無い状態にしてから撃つ。
    それでも断るなら、断っているのは `_protected_group_pids` しかない。
    """
    pid, port, ws = bg_guarded
    real = BO._observe_listen
    monkeypatch.setattr(BO, "_observe_listen",
                        lambda pg, **kw: dict(real(pg, **kw), ports=set()))
    out = BO._run({"pid": pid, "kill": True})
    assert "保護ポート" in out, out           # 第 3 の門の文言（番号ではなく持ち主で引く）
    time.sleep(0.3)
    assert procutil.port_in_use(port), "観測が目隠しされると保護ポートの設備を撃ってしまう"


def test_M1_保護を外せば従来どおり止められる(bg_guarded, monkeypatch):
    """逃げ道を塞がない（自分のサーバは止められる）。"""
    pid, port, ws = bg_guarded
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "")
    out = BO._run({"pid": pid, "kill": True})
    assert out.startswith("OK"), out
    assert _wait_port(port, up=False, timeout=5), out


def test_M5_兄弟の作業場からは台帳のpidを撃てない(bg_guarded, tmp_path, monkeypatch):
    """★`_KILL_ADVICE` が案内していた抜け道。台帳は LAPS_HOME 共有なので、
    作業場を見ないと**ホスト共通の kill 許可証**になる。"""
    pid, port, ws = bg_guarded
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "")     # 保護は外して作業場だけ見る
    other = tmp_path / "ws-b"
    other.mkdir()
    monkeypatch.setenv("LAPS_WORKSPACE", str(other))
    out = BO._run({"pid": pid, "kill": True})
    assert "作業場のものではありません" in out, out
    time.sleep(0.3)
    assert procutil.port_in_use(port), "兄弟の作業場から背景処理を撃てた"
    # 元の作業場に戻れば止められる（制限ではない）。
    monkeypatch.setenv("LAPS_WORKSPACE", ws)
    assert BO._run({"pid": pid, "kill": True}).startswith("OK")


def test_M5_案内文が抜け道を勧めない():
    """断り文が「立てた物なら BashOutput で通る」と読める書き方を残さない。"""
    assert "この作業場で自分が run_in_background で起こした記録" in B._KILL_ADVICE
    assert "ここに出た相手には効きません" in B._KILL_ADVICE


# ── M3: 引用符の中の `cd <ws>` は所有の証拠にしない ──────────────────────
_WS = "/work/ws1"


@pytest.mark.parametrize("script", [
    f'grep -rn "cd {_WS}" /etc',
    f"echo 'cd {_WS}'",
    f'sed -i "s|cd {_WS}|X|" f.txt',
    f'printf "cd {_WS}\\n"',
    f"awk '/cd {_WS}/ {{print}}' f.txt",
])
def test_M3_引用符の中のcdは所有にしない(script):
    assert procutil.roots_at_workspace(script, _WS) is False, script
    # ★素の argv 形だけでなく `bash -c '…'` 形でも（実際にこう起動される）。
    row = {"pid": 424242, "cwd": "/tmp", "argv": ("bash", "-c", script),
           "cmdline": "bash -c " + script}
    assert procutil.in_workspace(row, _WS) is False, script


@pytest.mark.parametrize("script", [
    f"cd {_WS} && php -S 0.0.0.0:8000",
    f"cd {_WS}; python3 -m http.server",
    f"nohup cd {_WS} && ./run.sh",
    f"FOO=1 cd {_WS} && ./run.sh",
    f"(cd {_WS} && ./run.sh)",
])
def test_M3_本当に実行されるcdは従来どおり所有(script):
    assert procutil.roots_at_workspace(script, _WS) is True, script
    row = {"pid": 424243, "cwd": "/tmp", "argv": ("sh", "-c", script),
           "cmdline": "sh -c " + script}
    assert procutil.in_workspace(row, _WS) is True, script


def test_M3_cmdlineだけの古い形でも読める():
    """argv を持たない行（`/proc` を空白で継いだ形）でも `-c` の後ろは本体。"""
    assert procutil.roots_at_workspace(f"sh -c cd {_WS} && php -S 0:1", _WS) is True
    # 引用符が消えた flatten 形では、検索語の `cd` は語の位置が違うので拾わない。
    assert procutil.roots_at_workspace(f"grep -rn cd {_WS} /etc", _WS) is False


# ── M4: 保護ポートの区切りと fail-closed ────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ("22 18301 18400", {22, 18301, 18400}),          # 空白区切り
    ("22\n18301\n18400", {22, 18301, 18400}),        # 改行区切り
    ("22,18301;18400", {22, 18301, 18400}),          # `,` と `;` の混在
    ("  22 , 18301  ", {22, 18301}),                 # 余分な空白
])
def test_M4_区切りを全部受ける(monkeypatch, raw, want):
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", raw)
    assert procutil.protected_ports() == want


@pytest.mark.parametrize("raw", ["abc", "0x4ce0", "22,abc", "70000", "-1"])
def test_M4_読めない語は警告して既定を保つ(monkeypatch, raw):
    """★黙って保護ゼロに落ちない（fail-closed）。設定の書き損じで床が消えない。"""
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", raw)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        got = procutil.protected_ports()
    assert set(procutil._DEFAULT_PROTECTED_PORTS) <= got, (raw, got)
    assert any("LAPS_PROTECTED_PORTS" in str(x.message) for x in w), raw


def test_M4_明示の空文字は従来どおり保護なし(monkeypatch):
    """意図した逃げ道は残す（`test_保護を外せば自分のサーバは撃てる` が守る道）。

    ★2026-09-05・第4回c の 4 回目に**空白だけ**を分けた（旧版はここで
      `"   "` も「保護なし」と定めていた）。`LAPS_PROTECTED_PORTS=" "` は
      `docker run -e` や `.env` の行末の空白ひとつで出来てしまう形で、
      **警告も無く床が消える**——§M4 で `"22,abc"` を fail-closed にした当の穴が
      「空白だけ」に残っていた。逃げ道は**明示の空文字**だけにする。
    """
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "")
    assert procutil.protected_ports() == set()


def test_M4_未設定なら既定(monkeypatch):
    monkeypatch.delenv("LAPS_PROTECTED_PORTS", raising=False)
    assert procutil.protected_ports() == set(procutil._DEFAULT_PROTECTED_PORTS)


# ── M2（続き）: `kill` の**前**に 1 語あるだけで門の外に出られていた ──────
@pytest.mark.parametrize("shape,tmpl,want", [
    ("sudo",    "sudo kill -9 {pid}",                        "sudo true"),
    ("timeout", "timeout 5 kill -9 {pid}",                   "timeout 5 true"),
    ("nohup",   "nohup kill -9 {pid} &",                     "nohup true &"),
    ("command", "command kill -9 {pid}",                     "command true"),
    ("then",    "if [ -f x ]; then kill -9 {pid}; fi",       "if [ -f x ]; then true; fi"),
    ("do",      "for q in 1; do kill -9 {pid}; done",        "for q in 1; do true; done"),
    ("env",     "env FOO=1 kill {pid}",                      "env FOO=1 true"),
    ("{ }",     "{{ kill -9 {pid}; }}",                      "{ true; }"),
    ("&&",      "echo hi && kill -9 {pid}",                  "echo hi && true"),
])
def test_M2_killの前置きを剥がしても保護ポートは守る(guarded, shape, tmpl, want):
    """★`then kill $(cat server.pid)` は run50〜53 の実記録で **kill 断片 50 件中 6 件**
    ＝「どちらの門も見ない残る穴」の全部だった（`bench/replay_kill_gates.py`）。

    剥がした前置きは**そのまま書き戻す**（命令を壊さない）。断片の先頭の空白も保つ。
    """
    proc, port, ws = guarded
    cmd = tmpl.format(pid=proc.pid)
    r = B._scope_pid_kill(cmd, ws, base=ws)
    assert r is not None, f"{shape} が門を素通りした"
    assert r[0] == want, (shape, r[0])
    assert "保護ポート" in r[1], r[1]
    assert proc.poll() is None


def test_M2_前置きがあっても自分の物なら1文字も変えない(tmp_path, monkeypatch):
    """剥がす仕組みが「余計に断る」側へ倒れていないことを見る。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "")
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(ws), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_port(port)
        for cmd in (f"sudo kill -9 {proc.pid}",
                    f"timeout 5 kill -9 {proc.pid}",
                    f"echo hi && kill -9 {proc.pid}"):
            assert B._scope_pid_kill(cmd, str(ws), base=str(ws)) is None, cmd
    finally:
        proc.kill(); proc.wait(timeout=5)


def test_M2_知らない前置きは触らない():
    """fail-open を保つ（知らない語で始まる断片は 1 文字も変えない）。

    ★2026-09-05・第4回c の 5 回目に**期待値を 1 行書き換えた**（旧版は
      `sudo -u nobody kill -9 123` を「読めない＝触らない」と定めていた）。
      理由: `sudo` の**引数を取るオプション**（`-u/-g/-C/-p/-h/-r/-t`）を食わない
      せいで、`sudo -u root pkill -9 -f <保護ポートの設備>` が前置き剥がしの
      途中で止まり、**門が一度も見ないまま素通し**になっていた（反証 B(7)・
      実物で保護ポートのサーバが死んだ）。オプションの引数まで食うと当然
      `sudo -u nobody kill …` も読めるようになる。
    ★これは門を**広げない**: `_scope_pid_kill` は対象の PID を**減らすだけ**で
      増やさないし、`sudo -u nobody` の前置きは書き戻すので、走る資格は元のまま。
    """
    assert B._parse_kill("mywrapper kill -9 123") is None
    assert B._parse_kill("echo kill") is None
    # ★知らない**オプション付き前置き**は依然として触らない（引数を食う表に無い）。
    assert B._parse_kill("mywrapper -u nobody kill -9 123") is None
    # ★`sudo -u nobody` は読める（B(7)）。前置きは 1 文字も変えずに書き戻す。
    p = B._parse_kill("sudo -u nobody kill -9 123")
    assert p is not None and p["prefix"] == "sudo -u nobody ", p


@pytest.mark.parametrize("shape,tmpl", [
    ("sudo",    "sudo pkill -f \"http.server {port}\""),
    ("timeout", "timeout 5 pkill -f \"http.server {port}\""),
    ("nohup",   "nohup killall -9 python3"),
    ("then",    "if [ -f x ]; then pkill -f \"http.server {port}\"; fi"),
])
def test_M2_名前撃ちも前置きを剥がして門に通す(guarded, shape, tmpl):
    """★`kill` 側だけ剥がして `pkill` 側に残す＝この現場の常習「片方だけ直る」。

    `sudo pkill -f gateway` は本番のゲートウェイを狙える形そのもの。
    """
    proc, port, ws = guarded
    cmd = tmpl.format(port=port)
    r = B._scope_name_kill(cmd, ws)
    assert r is not None, f"{shape} が名前撃ちの門を素通りした"
    assert "pkill" not in r[0] and "killall" not in r[0], r[0]
    assert proc.poll() is None


def test_M2_前置きの剥がしは書き戻す():
    """剥がした語を落とさない（命令を壊さない）。"""
    assert B._strip_kill_prefix("sudo pkill -f x") == ("sudo ", "pkill -f x")
    assert B._strip_kill_prefix("timeout 5 pkill x") == ("timeout 5 ", "pkill x")
    assert B._strip_kill_prefix("env A=1 nohup pkill x") == ("env A=1 nohup ", "pkill x")
    assert B._strip_kill_prefix("pkill x") == ("", "pkill x")
    assert B._strip_kill_prefix("mywrapper pkill x") == ("", "mywrapper pkill x")
