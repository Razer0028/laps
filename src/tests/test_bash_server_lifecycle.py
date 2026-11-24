"""常駐サーバの導線を1本で通す（第3回 W2・2026-09-04・Bash/BashOutput 担当）。

★背景（VDIR 走行が実際に壁にした型・`REPORTS/vdir-full-fidelity-20260824.md`・
  `REPORTS/devbench-20260823.md` §1 より）:
  ・前のサーバを殺していない → `Address already in use`（次の走行の起動が失敗する形で効く）。
  ・常駐サーバを**前面**で起動して走行が固まる（timeout に当たるだけで終われば良いが、
    孫プロセスが生き残るとポートが空かず、次の起動がまた `Address already in use` になる）。

  ここでは実物のプロセス（`python3 -m http.server` / 自作の無言サーバ）を使って、
    背景起動 → 待受確認（ソケット接続）→ BashOutput で出力を読む → kill →
    **ポートが実際に空く**（bind できる）
  を1本の試験で通す。「打ち切った」「殺した」という文言だけでなく、OS レベルで
  ポートが実際に解放されたことまで確かめる——文言は正しくても孫が残っていれば
  次の起動はやはり `Address already in use` になるため。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

import pytest

from laps import procutil
from laps import tools as tools_mod
from laps.tools import tool_failed
from laps.tools import bash as bash_tool
from laps.tools import bash_output as bo_tool


def _text(out) -> str:
    return out if isinstance(out, str) else out.text


def _pid(out: str) -> int:
    for ln in out.splitlines():
        if ln.strip().startswith("pid:"):
            return int(ln.split(":", 1)[1])
    raise AssertionError(f"pid が結果に無い: {out}")


#: この試験が確保したポート（片付けの取りこぼしを数えるため・2026-09-05・5 巡目）。
_PORTS_IN_TEST: list[int] = []


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    _PORTS_IN_TEST.append(port)
    return port


def _wait_until(fn, timeout=5.0, interval=0.05) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(interval)
    return False


def _port_listening(port: int) -> bool:
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=0.3)
        c.close()
        return True
    except OSError:
        return False


def _port_free(port: int) -> bool:
    """実際に bind できるかで確かめる。★これが「文言は殺したと言うが孫が
    残っている」を捕まえる——`ss` の一覧ではなく OS の bind 可否そのものを見る。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        s.close()
        return False


def _killpg_quiet(pid: int) -> None:
    try:
        os.killpg(pid, 9)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _no_leaked_listeners(tmp_path):
    """★共有ホストに待ち受けを残さない（2026-09-05・5 巡目・union の反証役の指摘②）。

    この試験群は `setsid` でわざとグループの外へ逃がしたサーバを立てる。各試験の
    `finally` は `_reap_listeners(port)` を呼ぶが、**そのとき相手がまだ bind して
    いない**（`sleep …; exec <server>` の形）と取りこぼす——実測: 走行後も
    `ppid=1` の `http.server` が `/tmp/pytest-of-root/…/ws` に残っていた
    （この係自身が今日 9 本残した。うち 1 本は**この片付けの最初の版**が
    「いまポートが空いているなら何もしない」と早く抜けたせいで、
    1.5 秒後に上がってきた相手を逃した）。前提が崩れて `assert` で落ちた走行は
    `finally` に入る前に抜けるので、そこでも漏れる。

    だから**ポートではなくプロセスで**回収する: この試験の `tmp_path` 配下に
    cwd を持つものは、まだ何も握っていなくてもグループごと片付ける。
    それでも取りこぼしたら（＝あとから bind してきたら）**試験を赤にする**
    ——黙って残すと次の走行の `Address already in use` になって別の試験を落とす
    ＝この係が潰している当の形をこちらが作ることになる。

    ★実測（6 巡目・`sleep 1.5; exec http.server` を片付けずに走らせる探針）:
      この fixture を `return` で潰すと `ppid=1` の `http.server` が **1 本**残り、
      有効なら **0 本**。＝これは書いてあるだけの片付けではない。
    ★撃つ相手と待つ相手は分ける（6 巡目）: 撃つのは①この試験の `tmp_path` 配下に
      cwd がある か ②この試験が確保した番号を**実際に握っている**相手だけ。
      argv にその番号が出てくるだけの相手は**待つ理由**にしかしない
      （共有ホストなので、番号がたまたま一致した他人を撃たない）。
    """
    _PORTS_IN_TEST.clear()
    yield
    ports = list(_PORTS_IN_TEST)
    _PORTS_IN_TEST.clear()
    me, ppid, my_pg = os.getpid(), os.getppid(), os.getpgid(0)
    root = str(tmp_path)

    def _reap(pid: int) -> None:
        try:
            pg = os.getpgid(pid)
        except OSError:
            pg = None
        if pg is not None and pg != my_pg:
            _killpg_quiet(pg)              # ★グループごと（子を残さない）
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    try:
        imap = bo_tool._listen_inode_ports()
    except Exception:
        imap = {}
    maybe_late = 0
    try:
        rows = procutil.proc_rows(skip_self=False)
    except Exception:
        rows = []
    want = {str(p) for p in ports}
    for r in rows:                         # ① この試験の tmp 配下に居るもの
        pid = r.get("pid")
        if not isinstance(pid, int) or pid in (me, ppid):
            continue
        mine_here = str(r.get("cwd") or "").startswith(root)
        # ★argv に**この試験が確保した番号**が出てくる相手は、cwd が外へ出ていても
        #   「これから bind しうる」として**待つ側**に数える（6 巡目）。
        #   ⚠ 推測（argv の文字列）は**待つかどうかにしか使わない**——撃つのは
        #   ①cwd がこの試験の tmp 配下 か ②実際にこの番号を握っている相手だけ。
        #   共有ホストなので、番号がたまたま argv に出た他人を撃ってはいけない。
        may_bind = bool(want & set(r.get("argv") or ()))
        if not (mine_here or may_bind):
            continue
        try:
            listening_now = bool(bo_tool._pid_listen_ports(pid, imap))
        except Exception:
            listening_now = False
        if not listening_now:
            maybe_late += 1                # まだ握っていない＝あとから bind しうる
        if mine_here:
            _reap(pid)
    for port in ports:                     # ② cwd が外に出た逃げ子はポートで
        for r in procutil.port_listeners(port):
            pid = r.get("pid")
            if isinstance(pid, int) and pid not in (me, ppid):
                _reap(pid)
    if not maybe_late:
        return                             # 遅れて上がる相手が居ない＝待たない
    time.sleep(0.6)
    late = 0
    for port in ports:
        for r in procutil.port_listeners(port):
            pid = r.get("pid")
            if isinstance(pid, int) and pid not in (me, ppid):
                _reap(pid)
                late += 1
    still = [p for p in ports if procutil.port_in_use(p)]
    assert not late and not still, (
        f"試験がホストに待ち受けを残した（片付けたあとに上がってきた数 {late} / "
        f"いまも埋まっているポート {still}）。"
        "`_reap_listeners` は bind 前の相手を取りこぼすので、"
        "逃げ子は**プロセスとして**片付けること。")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("hello-w2\n", encoding="utf-8")
    # ★作業場は**この試験の ws** に閉じる（2026-09-05・第4回c 4 巡目）。
    #   `tests/conftest.py` の autouse は作業場を `TMPDIR`（既定 `/tmp`）に置く。
    #   身内の観測範囲を作業場へ広げた 4 巡目では、それは
    #   **「/tmp に居る全プロセスが身内候補」**という意味になり、
    #   *別の試験*が残した `http.server` を「この起動の残骸」と赤くした（実測で
    #   `test_裸のアンパサンド…` が落ちた）。本番の作業場は
    #   プロジェクトのディレクトリなので、試験もその形に合わせる。
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    return ws


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_常駐サーバの一生を1本で通す(env):
    """背景起動 → 待受確認 → 本物の HTTP 応答 → BashOutput で出力を読む → kill →
    ポートが実際に空く、を実物のプロセスで1本通す。"""
    port = _free_port()
    # -u: stdout を行バッファにする（既定は非 tty で完全バッファ＝BashOutput が
    #     しばらく何も読めない）。バナーを確かめたいのでここだけ明示する。
    cmd = f"python3 -u -m http.server {port} --bind 127.0.0.1"
    out = _text(bash_tool._run({"command": cmd, "cwd": str(env), "run_in_background": True}))
    assert out.startswith("OK:"), out
    pid = _pid(out)

    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5), \
            "背景起動したサーバに繋がらない（待受導線が壊れている）"

        # 本当に本物のサーバが答えている（道具を介さず直接確かめる）
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/index.html")
        r = conn.getresponse()
        body = r.read().decode()
        conn.close()
        assert r.status == 200 and "hello-w2" in body, (r.status, body)

        def _has_banner() -> bool:
            bo = bo_tool._run({"pid": pid, "tail": 4000})
            return "Serving HTTP" in bo

        assert _wait_until(_has_banner, timeout=5), "BashOutput でサーバの出力が読めない"
        bo = bo_tool._run({"pid": pid, "tail": 4000})
        assert f"pid: {pid}" in bo
        assert "alive: True" in bo

        kres = bo_tool._run({"pid": pid, "kill": True})
        assert kres.startswith("OK:"), kres
    finally:
        _killpg_quiet(pid)

    assert _wait_until(lambda: not _port_listening(port), timeout=5), \
        "kill 後もポートが応答している（孫が残っている）"
    assert _wait_until(lambda: _port_free(port), timeout=5), \
        "kill 後もポートに bind できない（Address already in use の再現条件）"

    # 死んだ後は BashOutput が alive: False を正しく言う
    bo_after = bo_tool._run({"pid": pid})
    assert "alive: False" in bo_after, bo_after


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_二つの背景サーバが互いの出力とポートを侵さない(env):
    """★実測で踏んだ型（`_spawn_background` の docstring①）: 一時ファイル名が
    固定だと、同時起動の 2 本目が 1 本目の出力を切り詰める。実物の 2 プロセスで
    確かめる（既存 test_bash_background.py は echo だけなので、ここでは常駐サーバで）。"""
    port_a, port_b = _free_port(), _free_port()
    out_a = _text(bash_tool._run({
        "command": f"python3 -u -m http.server {port_a} --bind 127.0.0.1",
        "cwd": str(env), "run_in_background": True}))
    out_b = _text(bash_tool._run({
        "command": f"python3 -u -m http.server {port_b} --bind 127.0.0.1",
        "cwd": str(env), "run_in_background": True}))
    pid_a, pid_b = _pid(out_a), _pid(out_b)
    assert pid_a != pid_b
    try:
        assert _wait_until(lambda: _port_listening(port_a), timeout=5)
        assert _wait_until(lambda: _port_listening(port_b), timeout=5)
        bo_a = bo_tool._run({"pid": pid_a, "tail": 4000})
        bo_b = bo_tool._run({"pid": pid_b, "tail": 4000})
        assert str(port_a) in bo_a and str(port_b) not in bo_a
        assert str(port_b) in bo_b and str(port_a) not in bo_b
    finally:
        _killpg_quiet(pid_a)
        _killpg_quiet(pid_b)
        _wait_until(lambda: _port_free(port_a), timeout=5)
        _wait_until(lambda: _port_free(port_b), timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_前面で起動したサーバはtimeoutで打ち切られポートが実際に空く(env):
    """裸の前面起動は timeout に当たる。孫（実際の待受プロセス）まで止めて
    ポートを実際に空けること——でなければ次の起動が Address already in use になる。"""
    port = _free_port()
    cmd = f"python3 -u -m http.server {port} --bind 127.0.0.1"
    t0 = time.time()
    out = _text(bash_tool._run({"command": cmd, "cwd": str(env), "timeout": 2}))
    elapsed = time.time() - t0
    assert elapsed < 6, f"timeout の指定を大きく超えて止まった: {elapsed}s"
    assert "timeout after 2s" in out, out
    # 出力にバナーがあるので「前面で動いたまま」の助言（背景へ回す道）が出る
    assert "前面で動いたまま" in out or "背景" in out, out

    assert _wait_until(lambda: _port_free(port), timeout=5), \
        "timeout で打ち切った後もポートに bind できない（孫が残っている）"

    # ポートが本当に空いていることの追加証拠: 同じポートで即座に新しいサーバが立つ
    port_check = socket.socket()
    port_check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        port_check.bind(("127.0.0.1", port))
        port_check.listen(1)
    finally:
        port_check.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_無言のサーバもtimeoutで孫まで止まりポートが空く(env):
    """php -S / nginx 相当（起動時に何も出力しない）。出力の有無で判定が変わる
    経路（`_timeout_hint` の `_SERVERISH` 分岐）を、実物の無言サーバで確かめる。"""
    script = env / "silent_serve.py"
    script.write_text(
        "import socket, sys, time\n"
        "port = int(sys.argv[1])\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', port))\n"
        "s.listen(5)\n"
        "time.sleep(120)\n",
        encoding="utf-8")
    port = _free_port()
    cmd = f"python3 silent_serve.py {port}"
    t0 = time.time()
    out = _text(bash_tool._run({"command": cmd, "cwd": str(env), "timeout": 2}))
    elapsed = time.time() - t0
    assert elapsed < 6, f"timeout の指定を大きく超えて止まった: {elapsed}s"
    assert "timeout after 2s" in out, out
    # 出力が無いので「入力待ち」ではなく「無言のサーバ」の助言に分岐すること
    assert "入力待ち" not in out, out
    assert "サーバを前面で起動して戻ってこない" in out, out

    assert _wait_until(lambda: _port_free(port), timeout=5), \
        "無言のサーバを timeout で打ち切った後もポートに bind できない（孫が残っている）"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_BashOutputのkillが実際にポートを空ける(env):
    """`BashOutput(kill=true)` 単体の効果を、`_spawn_background` の停止案内どおりに
    確かめる（案内文だけでなく実効果を見る）。"""
    port = _free_port()
    out = _text(bash_tool._run({
        "command": f"python3 -u -m http.server {port} --bind 127.0.0.1",
        "cwd": str(env), "run_in_background": True}))
    pid = _pid(out)
    assert "kill=true" in out, "停止案内が無い"
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        kres = bo_tool._run({"pid": pid, "kill": True})
        assert kres.startswith("OK:"), kres
        assert _wait_until(lambda: _port_free(port), timeout=5), \
            "BashOutput(kill=true) の後もポートに bind できない"
        # 二度目の kill は「もう死んでいる」と正直に言う（プロセスが無くてエラーにならない）
        kres2 = bo_tool._run({"pid": pid, "kill": True})
        assert "already exited" in kres2, kres2
    finally:
        _killpg_quiet(pid)


# ── 第4回 core-server-lifecycle（2026-09-05）: 背景に回した起動を殺さない ──────
def _release_pgid(out: str) -> int:
    import re
    m = re.search(r"BashOutput\(pid=(\d+)\)", out)
    assert m, f"背景の控え（グループ番号）が結果に無い: {out}"
    return int(m.group(1))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_裸のアンパサンドで起動したサーバは殺されず即座に制御が返る(env):
    """★この現場でいちばん高くついた欠陥の再現試験（実測で確認済み）。

    旧実装は `communicate()` で**パイプの EOF** を待っていた。`cmd &` の孫は
    シェルの stdout/stderr パイプを持ったまま走るので EOF が来ず、
      ① 既定 120 秒まるまる待たされ、
      ② そのあと `_kill_group()` がプロセスグループごと SIGKILL する
    ＝**自分が正しく起動したサーバを、道具が 120 秒後に殺していた。**
    run50/51 の記録では timeout 18 件のうち 11 件がこの形（1,320 秒）。

    ここでは実物のサーバで「速く返る」「生き残る」「追える」「止められる」を通す。
    """
    port = _free_port()
    cmd = f"python3 -u -m http.server {port} --bind 127.0.0.1 &"
    t0 = time.time()
    out = _text(bash_tool._run({"command": cmd, "cwd": str(env), "timeout": 20}))
    elapsed = time.time() - t0

    assert elapsed < 5, f"背景に回した起動で {elapsed:.1f}s 待たされた（旧欠陥の再発）"
    pgid = _release_pgid(out)
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5), \
            "背景に回したサーバが死んでいる（道具が殺した）"
        # 追える: BashOutput がグループで生死を見る（長＝シェルは既に死んでいる）
        bo = bo_tool._run({"pid": pgid, "tail": 4000})
        assert "alive: True" in bo, bo
        assert "Serving HTTP" in bo, bo
        # 止められる
        kres = bo_tool._run({"pid": pgid, "kill": True})
        assert kres.startswith("OK:"), kres
        assert _wait_until(lambda: _port_free(port), timeout=5), \
            "BashOutput(kill=true) の後もポートが空かない"
    finally:
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_ANDで繋いだ背景起動も即座に返る(env):
    """★文字列で `&` を見て直す手では**届かない**形（run51 に実在）。

    `A && B &` は**リスト全体**が背景に回り、その部分シェルが B の終了を待つ。
    だから `… > server.log 2>&1 &` と**出力をきちんと逃がしていても**、
    部分シェルがパイプを持って同じ 120 秒を食う。待ち方そのものを直したことを、
    その形で確かめる。
    """
    port = _free_port()
    cmd = (f"cd {env} && nohup python3 -u -m http.server {port} --bind 127.0.0.1 "
           f"> server.log 2>&1 & echo $! > server.pid")
    t0 = time.time()
    out = _text(bash_tool._run({"command": cmd, "cwd": str(env), "timeout": 20}))
    elapsed = time.time() - t0
    assert elapsed < 5, f"出力を逃がした背景起動でも {elapsed:.1f}s 待たされた"
    pgid = _release_pgid(out)
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        assert (env / "server.pid").exists(), "背景の後ろに続く前面コマンドが走っていない"
    finally:
        _killpg_quiet(pgid)
        _wait_until(lambda: _port_free(port), timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_背景に回した後も出力を読み続けてサーバを詰まらせない(env):
    """★読むのをやめるとパイプが 64KB で詰まり、背景のサーバが `write()` で止まる
    （＝生きているのに応答しない、を道具が作ってしまう）。制御を返したあとも
    汲み続けてファイルへ流していることを、64KB を超える出力で確かめる。"""
    script = env / "chatty.py"
    script.write_text(
        "import sys, time\n"
        "for i in range(4000):\n"
        "    sys.stdout.write('x' * 100 + '\\n'); sys.stdout.flush()\n"
        "sys.stdout.write('DONE-MARKER\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8")
    out = _text(bash_tool._run({"command": "python3 -u chatty.py &",
                                "cwd": str(env), "timeout": 20}))
    pgid = _release_pgid(out)
    try:
        def _done() -> bool:
            return "DONE-MARKER" in bo_tool._run({"pid": pgid, "tail": 4000})
        assert _wait_until(_done, timeout=15), \
            "64KB を超えたところで詰まった（背景に回した後に汲むのをやめている）"
    finally:
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_前面のサーバを打ち切ったときは殺したと言う(env):
    """★もっともらしいが事実と逆の文言を残さない。旧文は
    「打ち切られたのはサーバが終わらないからで、失敗ではありません」だったが、
    実際は `_kill_group()` がグループごと SIGKILL していた。"""
    port = _free_port()
    out = _text(bash_tool._run({
        "command": f"python3 -u -m http.server {port} --bind 127.0.0.1",
        "cwd": str(env), "timeout": 2}))
    assert "timeout after 2s" in out, out
    assert "強制終了" in out, out
    assert "失敗ではありません" not in out, out
    assert _wait_until(lambda: _port_free(port), timeout=5), out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_自分で出力を閉じて走り続ける命令は終わったと言わない(env):
    """★「シェルが終わった」だけで終了と判定すると取り逃がす形。

    `exec 1>&- 2>&-` で出力を自分で閉じてから走り続ける命令は、パイプが EOF に
    なるのにシェルは走ったまま。ここを「終わった」と報告すると、動いている物を
    放置したうえ成功と言うことになる（旧 `communicate` はここを timeout で
    捕まえていた）。新しい待ち方でも同じく timeout に落ちることを確かめる。
    """
    t0 = time.time()
    out = _text(bash_tool._run({
        "command": "exec 1>&- 2>&-; sleep 30", "cwd": str(env), "timeout": 2}))
    elapsed = time.time() - t0
    assert "timeout after 2s" in out, out
    assert 1.5 < elapsed < 6, f"打ち切りが働いていない: {elapsed}s"


def test_Windowsでは追えない残りにBashOutputを勧めない(monkeypatch, env):
    """★効かない助言を書かない（この現場が繰り返し踏んでいる形）。

    Windows の起動経路は新しいプロセスグループを作らず、`taskkill /T` は
    親が生きている前提で木を辿る。親が終わったあとに残った孫は BashOutput の
    pid では扱えないので、そこで BashOutput を勧めてはいけない。
    """
    monkeypatch.setattr(bash_tool.sys, "platform", "win32")

    class _P:
        pid = 424242

    note = bash_tool._release_record(_P(), "srv.exe &", str(env), None)
    assert "BashOutput" not in note, note
    assert "taskkill" in note, note
    assert "run_in_background" in note, note


# ── 第4回b: 「言ったことが本当か」を、言った側が確かめる ──────────────────
def _escaped_listener(tmp_path, port: int):
    """**プロセスグループの外**で待ち受けるプロセス（setsid 相当）を起こす。
    `killpg` では止まらない相手＝旧実装が「殺しました／ポートは空いています」と
    嘘をついた当の形。run51 のトレースには `setsid nohup php -S 0.0.0.0:18300 …` が
    11 件あり、モデルは実際にこれを打っている。"""
    p = subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    assert _wait_until(lambda: _port_listening(port), timeout=5)
    return p


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_打ち切りの助言はポートを確かめてから言う(env, tmp_path):
    """★反証で潰された嘘（実測）: 旧実装は打ち切りのたびに
    「サーバは動いていません／ポートは空いています。もう一度 kill する必要は
    ありません」と**言い切って**いた。`setsid` で逃げた起動では直後の
    `procutil.port_in_use` が True（生きている）。しかも旧試験
    `test_timeout_hints_say_the_group_was_killed` がこの文言を**不変条件として固定**
    したので、嘘を書くことが仕様になっていた。

    ここでは文言ではなく**言ったことが真か**を見る（検証手段は同じ変更で手元にある）。
    """
    port = _free_port()
    proc = _escaped_listener(tmp_path, port)
    try:
        cmd = f"setsid python3 -u -m http.server {port} --bind 127.0.0.1"
        # 構成員の居ないグループ番号＝killpg では何も止められなかった状況
        hint = bash_tool._timeout_hint(cmd, "", 2_000_001)
        assert "ポートは空いています" not in hint, hint
        assert "もう一度 kill する必要はありません" not in hint, hint
        assert "まだ使われています" in hint, hint
        assert f"BashOutput(port={port}" in hint, hint
        assert proc.poll() is None
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except OSError:
            pass
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_本当に消えたときだけ空いていると言う(env):
    """逆側。何も残っていないなら従来どおり「強制終了しました・もう一度 kill する
    必要はありません」と言い切ってよい（言わなすぎも助言として悪い）。"""
    port = _free_port()
    hint = bash_tool._timeout_hint(f"php -S 0.0.0.0:{port} -t site/", "", 2_000_002)
    assert "強制終了" in hint, hint
    assert "ポートは空いています" in hint, hint
    assert "まだ使われています" not in hint, hint


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_グループの外へ出た残りにグループ停止を勧めない(env):
    """★反証で潰した嘘 その2（実測）: `members` が空＝グループの外へ出たと
    **正しく検知した**うえで、同じ段落が `BashOutput(pid=<pgid>, kill=true)` を
    勧めていた。実行すると `already exited`（成功の顔）を返すのにポートは
    使われたまま——`_FALSE_GREEN_KINDS` の "bind" ループの製造元。"""
    class _P:
        pid = 2_000_003

    class _Pump:
        def spill_to(self, path):
            pass

    note = bash_tool._release_record(_P(), "php -S 0.0.0.0:19999 -t site/",
                                     str(env), (_Pump(), _Pump()))
    assert "外へ出た" in note, note
    assert f"BashOutput(pid={_P.pid}, kill=true)" not in note, note
    assert "BashOutput(port=19999)" in note, note


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_打ち切り後の汲み取りは合計2秒で終わる(env, tmp_path):
    """★実測で見つかった超過: 打ち切り後の汲み取りが `wait(2)` の**直列 2 本**で、
    グループの外へ逃げたプロセスが両方のパイプを握っていると予算を最大 4 秒超過した
    （timeout=5 の呼び出しが 9.05 秒）。合計 2 秒に直す。"""
    pidfile = tmp_path / "escaped.pid"
    cmd = (f"setsid --fork sh -c 'echo $$ > {pidfile}; sleep 20' ; sleep 20")
    t0 = time.time()
    out = _text(bash_tool._run({"command": cmd, "cwd": str(env), "timeout": 2}))
    elapsed = time.time() - t0
    try:
        assert "timeout after 2s" in out, out
        # 旧（直列 2 本）なら 2+4=6 秒以上。新（合計 2 秒）は 4 秒台に収まる。
        assert elapsed < 5.0, f"汲み取りの予算が直列のまま: {elapsed:.2f}s"
    finally:
        try:
            stray = int(pidfile.read_text().strip())
            os.killpg(os.getpgid(stray), 9)
        except Exception:
            pass


def test_Windowsでは末尾のアンパサンドを勧めない(monkeypatch, env):
    """★`cmd.exe` で末尾の `&` は**区切り**であって背景化ではない。
    `_release_record` にだけ win32 分岐を入れて助言側に入れていなかった＝片手落ち
    （master も `nohup` で同じ形だったので退行ではないが、直す機会はここ）。"""
    win = bash_tool._server_rule("win32")
    assert "末尾に &" not in win and "末尾の & を付け" not in win, win
    assert "区切り" in win and "背景化しません" in win, win
    assert "run_in_background" in win
    assert "末尾に &" in bash_tool._server_rule("linux")
    monkeypatch.setattr(bash_tool.sys, "platform", "win32")
    hint = bash_tool._timeout_hint("php -S 0.0.0.0:19998", "Now listening on", None)
    assert "区切り" in hint, hint
    assert "末尾に & を付ける" not in hint, hint


# ── 第4回c（2026-09-05）: 「グループが空になった」を「止まった」と読み替えない ──
def _stray_group(ignore_sigterm: bool = False):
    """**別のプロセスグループ**でただ待つプロセス（実機の `bash /tmp/bwcmd-*.sh`
    ラッパ相当）。`_stop_group` が撃つ相手はこれで、逃げたサーバではない。"""
    code = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)") if ignore_sigterm else "import time; time.sleep(60)"
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の killpg 経路")
@pytest.mark.parametrize("ignore_sigterm", [False, True])
def test_グループが空でもポートが埋まっていたらOKと言わない(env, tmp_path, ignore_sigterm):
    """★第4回b の反証①（3/3 再現）を本番関数で固定する。

    `cd <ws> && setsid python3 -m http.server P &` は**サーバをグループの外へ
    出す**。`_stop_group` が撃てるのはグループに残ったラッパだけで、それを撃つと
    グループは空になる——旧実装はそこで
      ・`killpg(SIGKILL)` が `ProcessLookupError`（ignore_sigterm=False）
      ・SIGKILL が通って構成員 0（ignore_sigterm=True）
    のどちらでも **`OK: SIGTERM で…止まりました`** を返した。**ポートは使われた
    まま**（`port_in_use` は True）＝`_FALSE_GREEN_KINDS` "bind" の製造元。
    直前の猶予ループが 30 回観測した証拠を捨てていたのが機構。

    ここでは作り物の pgid を使わず、**実際に逃がしたリスナ**と**実際に別グループで
    走るラッパ**を立てて、返り値が `OK:` で始まらないことと、ポートが本当に
    埋まったままであることの**両方**を見る。
    """
    port = _free_port()
    srv = _escaped_listener(tmp_path, port)
    stray = _stray_group(ignore_sigterm)
    try:
        pgid = os.getpgid(stray.pid)
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        res = bo_tool._stop_group(pgid, port=port, grace=0.6)
        assert not res.startswith("OK:"), res
        assert f"ポート {port}" in res and "まだ使われています" in res, res
        assert f"BashOutput(port={port})" in res, res
        # 言ったことが真か: ポートは本当に埋まったまま／サーバは生きている
        assert not _port_free(port), "前提が崩れている（逃げたサーバが死んだ）"
        assert srv.poll() is None
    finally:
        for p in (srv, stray):
            try:
                os.killpg(os.getpgid(p.pid), 9)
            except OSError:
                pass
        for p in (srv, stray):
            try:
                p.wait(timeout=5)
            except Exception:
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の killpg 経路")
def test_逃げていなければ従来どおりOKと言う(env, tmp_path):
    """逆側（言わなすぎも欠陥）。ポートを渡していて、それが実際に空いたなら
    `_stop_group` は今までどおり `OK:` を返す。"""
    port = _free_port()
    stray = _stray_group()
    try:
        pgid = os.getpgid(stray.pid)
        res = bo_tool._stop_group(pgid, port=port, grace=1.0)
        assert res.startswith("OK:"), res
        assert "まだ使われています" not in res, res
    finally:
        try:
            os.killpg(os.getpgid(stray.pid), 9)
        except OSError:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
@pytest.mark.xfail(strict=True, reason=(
    "第4回b 反証② の must_fix#3 は `bash.py:_kill_verdict` の直しを要求しているが、"
    "`laps/tools/bash.py` は tools-process との統合中（別係の担当）なので"
    "この係は触っていない。直したらこの試験は XPASS（＝赤）になる——"
    "そのときに `xfail` マーカーを外すこと。\n"
    "★union-2（2026-09-06・二次点検）: merge-bash 最終先端（afeac19・8巡目）を"
    "重ねたあとも `_kill_verdict`（laps/tools/bash.py:698）は未修正のままと"
    "実測で確認（`if not left and not busy:` は `_port_in_cmd(cmd) is None` の"
    "とき無条件に『もう一度 kill する必要はありません』へ進む＝this xfail は"
    "孤児ではなく引き続き有効。merge-bash の 8 巡は kill パターン照合の強化に"
    "専念しており `_kill_verdict` 自体には触れていない。union-2 では bash.py の"
    "実装を変えるスコープを持たないため、直さず記録のみ残す。次に bash.py を"
    "触る係が拾うこと。"))
def test_ポートが読めない打ち切りで止まったと断言しない(env, tmp_path):
    """★まだ**直っていない**穴を、機械で見えるようにしておく（2026-09-05・第4回c）。

    `_kill_verdict` は `_port_in_cmd(cmd)` が None（＝命令からポートが読めない）のとき
    `busy=False` に落ち、構成員が空なら「**強制終了しました／もう一度 kill する必要は
    ありません**」と**言い切る**。何も確かめていない。
    実測（この係が再実行して確認）: `_port_in_cmd("bash start.sh")` は None・
    `_timeout_hint("bash start.sh", "", <空のグループ>)` は上の断言をそのまま返す。
    反証②の独立集計では run50/51/52 の timeout 25 件のうち **17 件（68%）**が
    この枝に落ちる（`bash start.sh` / `bash stop.sh` / `kill %1` / `apt-get …`）。

    期待する形: 「グループは空になりましたが、この命令からはポートが読めないので
    止まったかは確かめていません。SystemState(listening) で待ち受けを見て…」。
    """
    port = _free_port()
    srv = _escaped_listener(tmp_path, port)
    try:
        hint = bash_tool._timeout_hint("bash start.sh", "", 2_000_009)
        assert "もう一度 kill する必要はありません" not in hint, hint
        assert "確かめていません" in hint or "確かめてください" in hint, hint
        assert srv.poll() is None
    finally:
        try:
            os.killpg(os.getpgid(srv.pid), 9)
        except OSError:
            pass
        srv.wait(timeout=5)


# ── 第4回c の 2 巡目（2026-09-05）: 確かめていないなら「止まりました」と言わない ──
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_控えからポートが読めないなら止まったと言わない(env, monkeypatch):
    """★must_fix ①（反証が実物で再現・`scratchpad/r4c/expF.py`）を**本番経路で**固定。

    `bash start.sh` の中で `setsid … http.server P &` を起こすと、グループに残るのは
    番人（`sleep 300`）だけ。`_release_record` は members が非空なので
    `BashOutput(pid=<pgid>, kill=true)` を**勧める**——それを実行すると番人が死んで
    グループが空になり、旧実装は猶予ループの中で `OK: SIGTERM で止まりました` を
    返した。**サーバは生きたまま**（`port_in_use` は True・listeners にも残る）。

    §1 で入れた `_escaped_note` がこの形を塞げなかった理由は 1 行で言える:
    **控えの `cmd` が `bash start.sh` なので `_port_in_cmd` が `None` を返し、
    `port is None` でポートの関門を素通りする**。記録上いちばん多いのがこの形
    （cmd からポートが読めない起動＝約 97 件）。
    ⚠ 根治は「起動時／解放時に**実際に LISTEN しているポート**を控えに書く」ことで、
      それは `bash.py` の仕事＝この係の担当外（REPORTS §4-7・merge-bash 側の後続）。
      ここで固定するのは「**確かめていないことを、確かめたように言わない**」だけ。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    port = _free_port()
    (env / "start.sh").write_text(
        "#!/bin/sh\n"
        f"setsid {sys.executable} -u -m http.server {port} --bind 127.0.0.1 &\n"
        "sleep 300 &\n"                       # グループに残る番人（撃たれる相手）
        "echo started\n", encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh", "cwd": str(env),
                                    "timeout": 8}))
    m = re.search(r"BashOutput\(pid=(\d+), kill=true\)", started)
    assert m, f"members 非空の枝を通っていない（この試験の前提が崩れている）: {started}"
    pgid = int(m.group(1))
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5), \
            "逃げたサーバが上がっていない（前提が崩れている）"
        meta = json.loads((procutil.bg_dir() / f"{pgid}.meta.json")
                          .read_text(encoding="utf-8"))
        assert bash_tool._port_in_cmd(str(meta.get("cmd") or "")) is None, \
            f"この試験は「cmd からポートが読めない」形を見る: {meta.get('cmd')!r}"

        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert not res.startswith("OK:"), res
        # ★3 巡目（2026-09-05）で**言い分が強くなった**ので期待も上げた。
        #   2 巡目: 「この控えからはポートが読めないので確かめていません」
        #           ＝正直だが、①`Error:` 前置きでないので**機械は成功として記録する**
        #             （`tools/__init__.py:tool_failed`）②生きているサーバを指せていない。
        #   3 巡目: 撃つ前に `/proc` を観測しているので、**生き残っている待受を名指しで
        #           出す**。ここでは「確かめていない」ではなく「これが残っている」を見る。
        assert res.startswith("Error:") and tool_failed(res), res
        assert str(port) in res, f"生きているサーバのポートを指していない: {res}"
        # 言ったことが真か: サーバは実際に生きている（＝旧実装の OK は嘘だった）
        assert procutil.port_in_use(port), "前提が崩れている（逃げたサーバが死んだ）"
        assert _port_listening(port)
    finally:
        for r in procutil.port_listeners(port):
            if r["pid"] not in (os.getpid(), os.getppid()):
                try:
                    os.kill(r["pid"], 9)
                except OSError:
                    pass
        _killpg_quiet(pgid)


# ── 第4回c の 3 巡目（2026-09-05）: 推測ではなく**観測**を証拠にする ────────────
#: 「命令に `:数字` が現れるだけで偽のポートを掴む」4 形（再反証 M1 が本番経路で再現）。
#: 真ん中の値は `bash._port_in_cmd` が返す**偽の**ポート。
_DECOY_FORMS = [
    ("ログの時刻", "bash start.sh >> run-13:45:07.log", 45),
    ("接続文字列", "env DATABASE_URL=postgres://u@db.local:5432/app bash start.sh", 5432),
    ("イメージ名", "bash start.sh --image registry:5000/app", 5000),
    ("トンネル",   "bash start.sh -L 19000:127.0.0.1:{port}", 127),
]

_ESCAPE_SH = ("#!/bin/sh\n"
              "setsid {py} -u -m http.server {port} --bind 127.0.0.1 &\n"
              "sleep 300 &\n"                      # グループに残る番人（撃たれる相手）
              "echo started\n")


def _reap_listeners(port: int) -> None:
    """その試験が起こした逃げたリスナを片付ける（自分と親には触らない）。"""
    for r in procutil.port_listeners(port):
        if r["pid"] not in (os.getpid(), os.getppid()):
            try:
                os.kill(r["pid"], 9)
            except OSError:
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
@pytest.mark.parametrize("name,tmpl,decoy", _DECOY_FORMS,
                         ids=[f[0] for f in _DECOY_FORMS])
def test_命令に紛れた偽のポートを証拠にしない(env, monkeypatch, name, tmpl, decoy):
    """★再反証 M1（本番経路で 4 形とも再現）を固定する。

    2 巡目の関門 `_unverified_stop_note` は `port is None` **だけ**で発火し、その
    `port` は `_meta_port`＝`bash._port_in_cmd(meta["cmd"])` の**正規表現**だった。
    命令に `:数字` や `-p 数字` が現れるだけで偽のポートを掴み、関門は外れる。
    偽のポートは `port_in_use()` が撃つ前も後も False なので、
    **偽の証拠で `OK: 止まりました`** が返っていた（サーバは生きたまま）。

    直しは「撃つ前に、そのグループが実際に LISTEN しているポートを `/proc` から
    観測して控える」。この形ではグループ側の観測は空になるが、**同じ作業ディレクトリで
    この起動より後に生まれた待受**（＝逃げた候補）を拾うので、嘘の成功は返らない。
    ⚠ ここでは monkeypatch を使わない（本番経路 `bash._run` → `bo._run` をそのまま通す）。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    port = _free_port()
    (env / "start.sh").write_text(_ESCAPE_SH.format(py=sys.executable, port=port),
                                  encoding="utf-8")
    cmd = tmpl.format(port=port)
    assert bash_tool._port_in_cmd(cmd) == decoy, \
        f"この試験の前提（偽ポートを掴む形）が崩れている: {bash_tool._port_in_cmd(cmd)!r}"
    assert decoy != port, "偶然ぶつかった（試験の前提が崩れている）"
    started = _text(bash_tool._run({"command": cmd, "cwd": str(env), "timeout": 8}))
    m = re.search(r"BashOutput\(pid=(\d+)", started)
    assert m, f"背景の控えが結果に無い: {started}"
    pgid = int(m.group(1))
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5), \
            "逃げたサーバが上がっていない（前提が崩れている）"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert not res.startswith("OK:"), res
        # ★機械が「失敗」を読む根拠は `Error:` 前置き（tools/__init__.py:tool_failed）。
        #   「OK: で始めない」だけでは**成功として記録される**（2 巡目の取り違え）。
        assert res.startswith("Error:"), res
        assert tool_failed(res), res
        assert str(port) in res, f"生きているサーバのポートを指していない: {res}"
        # 偽の数は**推測だと断って**しか出さない（判定には使っていないと明示する）
        assert f"`:{decoy}`" in res and "推測" in res, res
        # 言ったことが真か: サーバは実際に生きている（＝旧実装の OK は嘘だった）
        assert procutil.port_in_use(port) and _port_listening(port)
    finally:
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_控えからポートが読めなくても観測できたなら止まったと言う(env, monkeypatch):
    """★逆側（再反証 M6・言わなすぎ）。控えの `cmd` が `bash start.sh` で
    `_port_in_cmd` は None——2 巡目はそれだけで毎回
    「この控えからはポートが読めないので確かめていません」を返していた。
    **正しく止まった停止にまでその文言が出る**のが M6 の指摘。

    いまは控えではなく `/proc` を見るので、グループの中で待ち受けていたポートが
    実際に空いたことを**観測して**言い切れる。"""
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    port = _free_port()
    (env / "start.sh").write_text(
        f"#!/bin/sh\nexec {sys.executable} -u -m http.server {port} --bind 127.0.0.1\n",
        encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh", "cwd": str(env),
                                    "run_in_background": True}))
    pid = _pid(started)
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        meta = json.loads((procutil.bg_dir() / f"{pid}.meta.json").read_text(encoding="utf-8"))
        assert bash_tool._port_in_cmd(str(meta.get("cmd") or "")) is None, \
            "この試験は「控えからポートが読めない」形を見る"
        res = _text(bo_tool._run({"pid": pid, "kill": True}))
        assert res.startswith("OK:"), res
        assert str(port) in res, f"観測した根拠が本文に無い: {res}"
        assert "確かめていません" not in res, res
        assert _wait_until(lambda: _port_free(port), timeout=5), "実際には空いていない"
    finally:
        _killpg_quiet(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_ポートと無縁の背景処理に待受の文言を出さない(env, monkeypatch):
    """★再反証 M6 の本体。`bash build.sh` のような非サーバを止めたときにまで
    「待ち受けが残っていないかは確かめていません」「SystemState(listening) で
    確かめてください」を出していた＝毎回出る注意書きは読まれない。
    観測して何も無ければ、待受の話はしない。"""
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    (env / "build.sh").write_text(
        "#!/bin/sh\ni=0\nwhile [ $i -lt 200 ]; do echo building $i; sleep 1; i=$((i+1)); done\n",
        encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash build.sh", "cwd": str(env),
                                    "run_in_background": True}))
    pid = _pid(started)
    try:
        time.sleep(0.5)
        res = _text(bo_tool._run({"pid": pid, "kill": True}))
        assert res.startswith("OK:"), res
        for word in ("待ち受け", "待受", "SystemState(listening)", "確かめていません"):
            assert word not in res, (word, res)
    finally:
        _killpg_quiet(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_命令に22が現れるだけの背景処理は止められる(env, monkeypatch):
    """★再反証 M5（保護ポートの巻き込み＝**永久に止められない**退行）。

    保護ポートの床（8fe2245 で新設）は `_meta_port` の**推測値**を食っていたので、
    `bash start.sh -p 22 deploy` のように命令に 22 が現れるだけで
    「ポート 22 は保護された設備です。撃ちません」を返し続けた——sshd でも何でもない
    自分の背景処理が、**どうやっても止められなくなる**。床には観測値だけを食わせる。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    (env / "start.sh").write_text("#!/bin/sh\nexec sleep 300\n", encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh -p 22 deploy",
                                    "cwd": str(env), "run_in_background": True}))
    pid = _pid(started)
    try:
        assert bash_tool._port_in_cmd("bash start.sh -p 22 deploy") == 22, "前提が崩れている"
        res = _text(bo_tool._run({"pid": pid, "kill": True}))
        assert res.startswith("OK:"), res
        assert "保護" not in res, res
        # ★生死は道具と同じ見方で見る（`os.kill(pid, 0)` は**ゾンビにも True**
        #   を返す＝親がまだ回収していないだけの死体を「生きている」と読む）。
        assert _wait_until(lambda: not bo_tool._is_alive(pid)
                           and not bo_tool._group_members(pid), timeout=5), \
            "止めたと言ったのに生きている"
    finally:
        _killpg_quiet(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_命令に現れる第三者のポートを残骸と呼ばない(env, monkeypatch, tmp_path):
    """★推測を証拠にしないことの**もう一方の側**（偽の赤）。

    `_meta_port` は「読めても当てにならない」: 命令に `…:5432` と書いてあっても、
    その背景処理が 5432 を握っている保証はない。旧実装はその番号で `port_in_use`
    を見て、**たまたまそこに居た無関係の第三者**を「グループの外へ出た残骸です」と
    名指ししていた（`expH` で第三者を実際に殺すところまで再現している）。
    観測だけを証拠にすれば、この形は**そもそも起きない**。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    third_port = _free_port()
    outside = tmp_path / "third"
    outside.mkdir()
    third = subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(third_port), "--bind", "127.0.0.1"],
        cwd=str(outside), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _wait_until(lambda: _port_listening(third_port), timeout=5)
    cmd = f"env DATABASE_URL=postgres://u@db.local:{third_port}/app sleep 300"
    assert bash_tool._port_in_cmd(cmd) == third_port, "前提が崩れている"
    started = _text(bash_tool._run({"command": cmd, "cwd": str(env),
                                    "run_in_background": True}))
    pid = _pid(started)
    try:
        res = _text(bo_tool._run({"pid": pid, "kill": True}))
        assert res.startswith("OK:"), res            # 自分の背景処理は止まった
        assert str(third_port) not in res, f"第三者のポートを結論に混ぜている: {res}"
        time.sleep(0.3)
        assert third.poll() is None, "無関係の第三者を殺した"
        assert _port_listening(third_port)
    finally:
        _killpg_quiet(pid)
        try:
            os.killpg(os.getpgid(third.pid), 9)
        except OSError:
            pass
        third.wait(timeout=5)


# ── 第4回c の 4 巡目（2026-09-05）: **実機の形（cwd を渡さない）で測る** ──────────
#
# ★3 巡目の試験 8 本は**全部 `cwd=str(env)` を渡していた**。実機は渡さない
#   （trace 2,938/2,938・台帳 17/17 が `cwd=null`）。`_launch_scope` は
#   `meta["cwd"]` が空だと `None` を返していたので、**この現場でいちばん多い形では
#   身内の観測が一行も動かず**、逃げたサーバが生きたまま
#   `OK: SIGTERM で止まりました` が返っていた（`exp_nocwd` H1・★FALSE GREEN）。
#   試験が本番の形になっていなかったので、そこだけ緑だった。
#   ここから下は **`cwd` を渡さない**（`monkeypatch.chdir` でエージェントの cwd を
#   作業場に置く＝実機と同じ形）。

_ESCAPE_NOWAIT_SH = ("#!/bin/sh\n"
                     "setsid {py} -u -m http.server {port} --bind 127.0.0.1 &\n"
                     "sleep 300 &\n"                  # グループに残る番人
                     "echo started\n")


def _launch_nocwd(env, script: str, *, timeout: int = 8) -> int:
    """`cwd` を**渡さずに** `bash start.sh` を起こし、グループ番号を返す。"""
    (env / "start.sh").write_text(script, encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh", "timeout": timeout}))
    m = re.search(r"BashOutput\(pid=(\d+), kill=true\)", started)
    assert m, f"背景の控えが結果に無い（前提が崩れている）: {started}"
    pgid = int(m.group(1))
    meta = json.loads((procutil.bg_dir() / f"{pgid}.meta.json").read_text(encoding="utf-8"))
    assert meta.get("cwd") is None, \
        f"この試験は**cwd を渡さない**形を見る（台帳: {meta.get('cwd')!r}）"
    return pgid


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_cwdを渡さない実機の形でも逃げたサーバを見落とさない(env, monkeypatch):
    """★must_fix ①②（`exp_nocwd` H1 が本番経路で再現した false green）を固定。

    旧: `_launch_scope` が `None` → `_observe_listen` の身内が空 →
        `OK: SIGTERM で止まりました`（`port_in_use=True`・サーバは生きている）。
    新: `meta["cwd"]` が空なら `procutil.canonical_workspace()` に落とす。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)                             # ★エージェントの cwd＝作業場
    port = _free_port()
    pgid = _launch_nocwd(env, _ESCAPE_NOWAIT_SH.format(py=sys.executable, port=port))
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5), \
            "逃げたサーバが上がっていない（前提が崩れている）"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and tool_failed(res), res
        assert str(port) in res, f"生きているサーバのポートを指していない: {res}"
        # ★must_fix ⑤: 散文だけ正しく見える言い回しを消す。生き残りが居る本文で
        #   「SIGTERM で**止まりました**」と書かない（止まったのは構成員だけ）。
        assert "構成員は SIGTERM で居なくなりました" in res, res
        assert "（SIGTERM で止まりました）" not in res, res
        assert procutil.port_in_use(port), "前提が崩れている（逃げたサーバが死んだ）"
    finally:
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_cwdを渡さない走行でグループが空になっていても見落とさない(env, monkeypatch):
    """★must_fix ②が名指しした形（M2 枝＝`already exited`）を**cwd 省略**で。

    番人が自然に終わってグループが空になった状態で `kill=true` を押すと、
    `_run` は「撃つまでもない」側の枝に入る。そこでも `_observe_listen` を
    通っているが、`cwd` が無いと範囲が `None` で身内を 1 本も拾えなかった。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_nocwd(
        env,
        "#!/bin/sh\n"
        f"setsid {sys.executable} -u -m http.server {port} --bind 127.0.0.1 "
        ">/dev/null 2>&1 &\n"
        "sleep 2 &\n"                                 # ★自然に終わる番人
        "echo started\n", timeout=4)
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        assert _wait_until(lambda: not bo_tool._group_members(pgid)
                           and not bo_tool._is_alive(pgid), timeout=8), \
            "グループが空になる枝（already exited）に入っていない"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and tool_failed(res), res
        assert "already exited" not in res, res
        assert str(port) in res, res
        assert procutil.port_in_use(port)
    finally:
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_起動ディレクトリの外へ出た逃げ子も作業場の中なら見える(env, monkeypatch):
    """★`exp_cover` (c2)。起動 dir（`app/`）の外・作業場の中（`data/`）へ
    `cd` してから `setsid` したサーバ。旧 `_launch_scope` は**起動 dir に絞って**
    いたので取り逃していた（★FALSE GREEN）。範囲を作業場にして閉じる。"""
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    (env / "app").mkdir()
    (env / "data").mkdir()
    monkeypatch.chdir(env / "app")                     # 起動は app/ から（cwd は渡さない）
    port = _free_port()
    (env / "app" / "start.sh").write_text(
        "#!/bin/sh\n"
        f"cd ../data && setsid {sys.executable} -u -m http.server {port} "
        "--bind 127.0.0.1 &\n"
        "sleep 300 &\necho started\n", encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh", "timeout": 8}))
    m = re.search(r"BashOutput\(pid=(\d+), kill=true\)", started)
    assert m, started
    pgid = int(m.group(1))
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and tool_failed(res), res
        assert str(port) in res, res
        assert str(env / "data") in res, f"逃げ子の cwd を出していない: {res}"
    finally:
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
@pytest.mark.parametrize("delay", ["0.4", "0.8"])
def test_撃ったあとにbindする逃げ子を見落とさない(env, monkeypatch, delay):
    """★must_fix ③（`exp_window` の掃引。bind 遅延 0.2〜1.5 秒の**4 形すべて**が
    false green だった）。

    旧: 身内の候補を「**いま LISTEN しているもの**」で絞っていた
        （`if not ports: continue`）ので、`setsid sh -c 'sleep …; exec <server>'` は
        観測の時点で候補にすら入らず `OK:` が返り、**1 秒後にサーバが上がってきた**。
    新: 候補は「プロセス」で控え、撃ったあとに LISTEN を読み直す
        （`_survivors`）。まだ待ち受けていない逃げ子が生きているあいだは
        窓（既定 2.0 秒）を切って待つ（`_await_late_bind`）。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_nocwd(
        env,
        "#!/bin/sh\n"
        f"setsid sh -c 'sleep {delay}; exec {sys.executable} -u -m http.server {port} "
        "--bind 127.0.0.1' &\n"
        "sleep 300 &\necho started\n", timeout=1)
    try:
        late = not _port_listening(port)                # 撃つ時点でまだ上がっていないか
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and tool_failed(res), res
        assert str(port) in res, f"あとから上がったサーバを指していない: {res}"
        if late:
            assert "撃つ前は待ち受けていませんでした" in res, res
        assert _wait_until(lambda: _port_listening(port), timeout=3), \
            "前提が崩れている（あとから上がるはずのサーバが上がらない）"
    finally:
        # ★片付けも 2 度読む。1 度だけだと**片付けたあとに bind**されて逃げる
        #   （陰性対照で実際に 1 本残した——この試験が見ている当の欠陥そのもの）。
        _reap_listeners(port)
        time.sleep(0.5)
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_構成員が一本cdしただけで自分の背景処理を止められなくならない(env, monkeypatch):
    """★must_fix ④（`exp_d_detail` が実物で再現）。

    `_ownership_note` は cwd を **1 本だけ**選んでいた（長の現物 → `members[0]` の
    現物 → 台帳）。`bash start.sh` の中身が `cd /tmp && setsid …` だと、長が先に
    死んで `members[0]` が `cd /tmp` したサブシェルになる——**作業場の中に居る
    `sleep 300` を無視して**「pid N は作業場の外にいます。撃っていません」を
    返し続けた＝自分が起こした背景処理が永久に止められない。

    ⚠ 5 巡目（must_fix ③）で通り道が変わった: 「手掛かりのどれか 1 本が中なら通す」は
      **他人のグループを撃つ許可**になっていたので戻し、代わりに
      **控えが自分の物だと証明できたとき**（`_released_record_proves`）に通す。
      この形はそこを通る（＝ここが緑なら、狭めた門が仕事を殺していない）。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_nocwd(
        env,
        "#!/bin/sh\n"
        f"cd /tmp && setsid {sys.executable} -u -m http.server {port} "
        "--bind 127.0.0.1 &\n"
        "sleep 300 &\necho started\n")
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        members = bo_tool._group_members(pgid)
        assert any(not str(m.get("cwd") or "").startswith(str(env)) for m in members), \
            f"作業場の外に居る構成員が居ない（前提が崩れている）: {members}"
        assert any(str(m.get("cwd") or "").startswith(str(env)) for m in members), \
            f"作業場の中に居る構成員が居ない（前提が崩れている）: {members}"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert "作業場の外**にいます" not in res, f"所有権の門が誤って断った: {res}"
        # 実際に撃てている（構成員が居なくなる）
        assert _wait_until(lambda: not bo_tool._group_members(pgid), timeout=5), res
        # 逃げたサーバは生きているので、そこは赤で言う
        assert res.startswith("Error:") and str(port) in res, res
    finally:
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_親子を辿れない相手は第三者の可能性を先に書く(env, monkeypatch):
    """★must_fix ⑥。範囲を作業場へ広げた副作用で、**同じ作業場に居るだけの
    無関係なプロセス**も候補に入りうる（run50 では `php -S 18300/18301` が並走
    していた）。親を辿れなかった相手は、`Error:` の**本文の先**に
    「第三者かもしれない」と書き、撃つ導線は `BashOutput(port=…)`（持ち主と
    cwd を並べる側）に限る——ここから直接 `kill=true` は勧めない。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_nocwd(env, _ESCAPE_NOWAIT_SH.format(py=sys.executable, port=port))
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:"), res
        assert "第三者" in res and "区別できません" in res, res
        # 「第三者かもしれない」が、プロセスの一覧より**先**に出ている
        assert res.index("第三者") < res.index(f"pid "), res
        assert "kill=true" not in res, f"見る前に撃つ導線を出している: {res}"
        assert f"BashOutput(port={port})" in res, res
    finally:
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_起動より前から居る同じ作業場のサーバを残骸と呼ばない(env, monkeypatch):
    """★偽の赤の側（`exp_more` R1）。範囲を作業場へ広げたので、**先に居た**
    無関係のサーバを「この起動の残骸」に数えないことを固定する。

    控えに `start_ticks` があるとき（`run_in_background=true` の経路）は、
    `procutil.proc_start_ticks` と**同じ物差し（boot からの tick）**で比べられる
    ので、余裕なしで落とせる。
    ⚠ 打ち切りで背景に回された控え（`released`）には `start_ticks` が**無い**ので、
      この関門は使えない＝**落とさない**（5 巡目 must_fix ①）。その経路は対の試験
      `test_物差しが無い控えでは先に居たサーバも落とさず断り書きを付ける` が見る。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    third_port = _free_port()
    third = subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(third_port),
         "--bind", "127.0.0.1"],
        cwd=str(env), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_until(lambda: _port_listening(third_port), timeout=5)
        started = _text(bash_tool._run({"command": "sleep 300", "cwd": str(env),
                                        "run_in_background": True}))
        pid = _pid(started)
        meta = json.loads((procutil.bg_dir() / f"{pid}.meta.json")
                          .read_text(encoding="utf-8"))
        assert isinstance(meta.get("start_ticks"), int), \
            "この試験は tick が控えられている経路を見る"
        try:
            res = _text(bo_tool._run({"pid": pid, "kill": True}))
            assert res.startswith("OK:"), f"先に居た第三者で偽の赤が出た: {res}"
            assert str(third_port) not in res, res
            assert third.poll() is None, "無関係の第三者を殺した"
        finally:
            _killpg_quiet(pid)
    finally:
        try:
            os.killpg(os.getpgid(third.pid), 9)
        except OSError:
            pass
        third.wait(timeout=5)


# ── まだ塞げていない形（`exp_dvar` で 3 形とも実測・2026-09-05・5 巡目 must_fix ④）──
#
# ★4 巡目の記述は**間違っていた**。「不変条件＝子が自分で cd したか」と書き、
#   「親が cd する形（(d)）は閉じた」と申告していたが、実物で掃くと閉じているのは
#   **d1（`cd /tmp && setsid …&`）だけ**で、しかも理由が違う——`&&` が作った
#   サブシェルがグループに残り、そこを**親として辿れる**（`descendant`）から見える。
#   `;` や改行で `cd` を分けるとサブシェルは生まれず、**同じ「親が cd」でも消える**。
# 実際の不変条件は「**撃つ時点で、親を辿れず、cwd が作業場の外**」。誰が cd したかは
#   関係ない。下の 3 本（子が cd／親が cd／親が cd + nohup）で、そう固定する。
_D_HOLE = {
    "d2-子がcd": ("setsid sh -c 'cd /tmp && exec {py} -u -m http.server {port} "
                  "--bind 127.0.0.1' &\n"),
    "d5-親がcd": ("cd /tmp\n"
                  "setsid {py} -u -m http.server {port} --bind 127.0.0.1 &\n"),
    "d6-親がcdしてnohup": ("cd /tmp; nohup setsid {py} -u -m http.server {port} "
                           "--bind 127.0.0.1 >/dev/null 2>&1 &\n"),
}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
@pytest.mark.parametrize("shape", sorted(_D_HOLE))
@pytest.mark.xfail(strict=True, reason="★未解決（5 巡目に `exp_dvar` で 3 形とも実測）: "
                                       "撃つ時点で**親を辿れず（ppid=1）cwd が作業場の外**だと、"
                                       "`descendant` にも `scope` にも入らないので"
                                       "**逃げたサーバがどこにも現れない**。"
                                       "子が cd したか親が cd したかは関係ない。"
                                       "根治は控えに実 LISTEN／子の pid を書く bash.py 側の直し。")
def test_親を辿れず作業場の外に出た逃げ子は見えない(env, monkeypatch, shape):
    """**まだ塞げていない**形を、緑にせず試験として残す（効かなかった手を消さない）。

    3 形とも
      ・グループの構成員ではない（`setsid`）
      ・親を辿れない（起こしたシェルが先に死に、`ppid` が 1 になる）
      ・cwd が作業場の外（`scope` にも入らない）
    が同時に成り立つ。

    ★見るのは「**逃げたサーバが答えに現れるか**」（＝`str(port)` が本文にあるか）。
      「`OK:` で終わらないこと」を見ると、**別の理由で赤くなった走行を直ったと
      読み違える**——実測（`exp_dvar_nocwd`）: 実機の形（cwd を渡さない）だと
      d5/d6 は所有権の門が「作業場の外です。撃っていません」と断るので `OK:` に
      ならないが、逃げたサーバは相変わらずどこにも出てこない。
      直ったらこの試験が緑になり `strict=True` が知らせる。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_nocwd(
        env,
        "#!/bin/sh\n"
        + _D_HOLE[shape].format(py=sys.executable, port=port)
        + "sleep 300 &\necho started\n")
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert procutil.port_in_use(port), "前提が崩れている（逃げ子が死んだ）"
        assert str(port) in res, res               # ← いまはここで落ちる（穴）
    finally:
        _reap_listeners(port)
        _killpg_quiet(pgid)


# ── 第4回c の 5 巡目（2026-09-05）: **物差しが無い控え**で本物の逃げ子を捨てない ──
#
# ★4 巡目の穴（`exp_released` / `exp_rel3` が本番経路で再現・4/4）:
#   `bash._release_record`（打ち切りで背景に回す経路）は、シェルを刈り取った**あと**で
#   `proc_start_ticks(pgid)` を呼ぶので、控えの `start_ticks` は**必ず None**
#   （実機の台帳 17/17 が該当）。すると身内の関門が壁時計（`started_at`＝**打ち切り
#   時刻**）＋余裕 5 秒に落ち、**サーバが上がってから打ち切られるまでが 5 秒より
#   長い**普通の起動で、本物の逃げ子が「起動より前から居た第三者」として捨てられた
#   ＝道具自身が勧めた `BashOutput(pid=…, kill=true)` が
#   `OK: SIGTERM で止まりました` を返し、ポートは埋まったまま（★FALSE GREEN）。


def _escapee_pid(port: int) -> int | None:
    rows = procutil.port_listeners(port)
    return rows[0]["pid"] if rows else None


def _force_no_start_ticks(pgid: int) -> dict:
    """打ち切りで背景に回った控えから `start_ticks` を**強制的に**剥がし、
    「物差しが無い」形にして返す（union-2・2026-09-06）。

    ★union-2 で実測: merge-bash 側の直し（`bash.py` が `_release_record` に渡す
      `start_ticks` を**シェルが生きているうちに前倒しで控える**・4回目の直し）
      により、通常の起動では控えに `start_ticks` が**ほぼ必ず**残るようになった
      （＝以前あった false green の根は塞がった。良い変化）。一方この一群の試験は
      「控えに `start_ticks` が無いとき」の受け皿（`_born_after_ticks` の fail-open・
      `_launch_ticks(meta) is None` の分岐）を確かめるために書かれており、
      **その形をもう自然な打ち切りだけでは再現できない**（起動から死ぬまでが
      よほど一瞬でない限り前倒しの捕捉が間に合う）。試験の主眼は「控えにその欄が
      無いときにどう振る舞うか」であって「どうやって欄が消えるか」ではないので、
      控えを直接書き換えて欄を落とす——他の欄（`released`/`cwd`/`cmd`/`members`）は
      実際の起動が書いたものをそのまま使う。
    ★`started_at` も**呼び出し時点の壁時計**へ書き換える（union-2）: これも
      merge-bash 側の前倒し（`_launch_at` を**起動直後**に控える）で「打ち切り
      時刻」ではなく「起動時刻」を指すようになった。`_launch_epoch` の
      `started_at` フォールバックは（`start_ticks` が無い控えの旧仕様どおり）
      「打ち切られた時刻」を意味する前提の試験がある（起動から打ち切りまでの
      経過秒数を検算する）ので、ここで呼び出し時点（＝自然な遅延が経過した後）
      に更新して**その意味を保つ**。
    """
    p = procutil.bg_dir() / f"{pgid}.meta.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta.pop("start_ticks", None)
    meta["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def _launch_nocwd_empty(env, script: str, *, timeout: int = 8) -> int:
    """`cwd` を渡さずに起こし、**グループが空になった**控えの番号を返す。

    ★`_launch_nocwd` は `BashOutput(pid=…, kill=true)` の助言から番号を拾うが、
      グループに誰も残らない形では `bash._release_record` はその助言を**出さない**
      （出しても効かないと分かっているので。第4回b の反証で直した）。
      それでも控えは書かれていて、モデルは番号を見て `kill=true` を押せる——
      この試験が見ているのは**そこで返る `already exited`**（＝成功の顔）。
    """
    (env / "start.sh").write_text(script, encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh", "timeout": timeout}))
    m = re.search(r"/bg/(\d+)\.out", started)
    assert m, f"背景の控えが結果に無い（前提が崩れている）: {started}"
    return int(m.group(1))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_打ち切りで背景に回した控えでも先に上がったサーバを捨てない(env, monkeypatch):
    """★must_fix ①（`exp_rel3` hold=8s が本番経路で再現した false green）。

    控えに `start_ticks` が無い（＝物差しが無い）ときは「この起動より前から居た」を
    **証明できない**のだから、候補を落とさない（fail-open）。落としていたせいで、
    起動から 5 秒以上あとに打ち切られた走行では逃げ子が丸ごと消えていた。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    # 逃げたサーバを先に上げ、**7 秒たってから**打ち切りで背景に回す。
    pgid = _launch_nocwd(
        env,
        "#!/bin/sh\n"
        f"setsid {sys.executable} -u -m http.server {port} --bind 127.0.0.1 &\n"
        "sleep 300 &\n"                               # グループに残る番人
        "sleep 7\n", timeout=30)
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        meta_p = procutil.bg_dir() / f"{pgid}.meta.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        # ── 前提（この試験が見ている機構そのもの）──────────────────────
        assert meta.get("released") is True, meta
        # ★union-2: merge-bash 側の直しで通常の打ち切りでも start_ticks が
        #   残るようになった（_force_no_start_ticks の docstring参照）。
        #   この試験の主眼（物差しが無い形の受け皿）を保つため強制的に剥がす。
        meta = _force_no_start_ticks(pgid)
        assert bo_tool._launch_ticks(meta) is None, \
            "この試験は**物差しが無い**控えを見る（start_ticks があるなら前提が違う）"
        epoch = bo_tool._launch_epoch(meta, meta_p)
        esc = _escapee_pid(port)
        born = (procutil._boot_epoch()
                + procutil.proc_start_ticks(esc) / procutil._clock_ticks())
        assert epoch - born > 5.0, \
            f"前提が崩れている（起動と打ち切りの差が 5 秒以内: {epoch - born:.1f}s）"
        # ── 本題 ────────────────────────────────────────────────
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and tool_failed(res), res
        assert str(port) in res, f"本物の逃げ子を捨てている: {res}"
        assert "start_ticks" in res, f"物差しが無いことを言っていない: {res}"
        assert procutil.port_in_use(port), "前提が崩れている（逃げ子が死んだ）"
    finally:
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_物差しが無い控えでは先に居たサーバも落とさず断り書きを付ける(env, monkeypatch):
    """★must_fix ①の**払った代償**を、隠さずに固定する（tick の有無で両経路を見る）。

    対になる試験 `test_起動より前から居る同じ作業場のサーバを残骸と呼ばない` は
    **`start_ticks` がある**経路（`run_in_background=true`）で、先に居た第三者を
    落として `OK:` と言えることを見ている。こちらは**無い**経路——落とせないので
    候補に残る。そのぶん本文に「起動時刻が読めないので落としていない」と書き、
    第三者かもしれないと**先に**断る。黙って緩めない。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    third_port = _free_port()
    third = subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(third_port),
         "--bind", "127.0.0.1"],
        cwd=str(env), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_until(lambda: _port_listening(third_port), timeout=5)
        # 第三者が**先に**居る状態で、打ち切りで背景に回る走行を起こす
        pgid = _launch_nocwd(env, "#!/bin/sh\nsleep 300 &\necho started\n", timeout=1)
        try:
            # ★union-2: merge-bash の前倒し捕捉で通常は start_ticks が残る。
            #   この試験の主眼（物差しが無い形）を保つため強制的に剥がす。
            meta = _force_no_start_ticks(pgid)
            assert bo_tool._launch_ticks(meta) is None, meta
            res = _text(bo_tool._run({"pid": pgid, "kill": True}))
            assert res.startswith("Error:"), \
                f"落とさない側に倒したのだから、ここは赤になるはず: {res}"
            assert "start_ticks" in res and "証明できない" in res, res
            assert res.index("第三者") < res.index("pid "), res
            assert "kill=true" not in res, f"見る前に撃つ導線を出している: {res}"
            assert third.poll() is None, "無関係の第三者を殺した"
        finally:
            _killpg_quiet(pgid)
    finally:
        try:
            os.killpg(os.getpgid(third.pid), 9)
        except OSError:
            pass
        third.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
@pytest.mark.parametrize("delay", ["0.8", "1.5"])
def test_グループが空の枝でも撃ったあとにbindする逃げ子を待つ(env, monkeypatch, delay):
    """★must_fix ②（`exp_g2` が本番経路で再現・遅延 0.8/1.5 秒とも false green）。

    `_run` の `already exited` 枝（グループが**撃つ前から空**）は、読み直しを 1 回
    しかしていなかった。`setsid sh -c 'sleep …; exec <server>' &` を起こした走行では
    その 1 回でまだ何も待ち受けていないので `pid N already exited` を返し
    （`Error:` ではないので機械は**成功**と読む）、直後にサーバが上がってきた。
    `_after_kill` と**同じ 1 本**（`_survivors_now`）を通す。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_nocwd_empty(
        env,
        "#!/bin/sh\n"
        f"setsid sh -c 'sleep {delay}; exec {sys.executable} -u -m http.server {port} "
        "--bind 127.0.0.1' &\n"
        "echo started\n", timeout=20)                 # ★番人を置かない＝グループは空
    try:
        assert not bo_tool._group_members(pgid) and not bo_tool._is_alive(pgid), \
            "この試験は `already exited` の枝を見る（前提が崩れている）"
        assert not _port_listening(port), "前提が崩れている（撃つ前にもう上がっている）"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and tool_failed(res), res
        assert "already exited" not in res, res
        assert str(port) in res, f"あとから上がったサーバを指していない: {res}"
        assert "撃つ前は待ち受けていませんでした" in res, res
    finally:
        _reap_listeners(port)
        time.sleep(0.3)
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_サーバでない逃げ子のために毎回2秒待たない(env, monkeypatch):
    """★must_fix ⑤（`exp_wait` の実測: 151 ms → 2,195 ms＝**14 倍**）。

    4 巡目の `_await_late_bind` は「逃げ子が生きていて、まだ何も握っていない」なら
    **必ず**窓（2 秒）を払っていた。`setsid sleep 300 &` は永遠に bind しないので、
    その 2 秒は丸ごと無駄——`bash build.sh` の停止が毎回 14 倍になる。
    窓は「これから待ち受けを始めるかもしれない相手」にだけ払う（`_may_bind`）。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    pgid = _launch_nocwd(
        env,
        "#!/bin/sh\n"
        "setsid sleep 300 &\n"                        # ★逃げた**非サーバ**
        "sleep 300 &\necho building\n", timeout=1)
    escaped = [r for r in procutil.proc_rows(skip_self=False)
               if r.get("cmdline", "").startswith("sleep 300")]
    assert escaped, "前提が崩れている（逃げた sleep が居ない）"
    try:
        t0 = time.perf_counter()
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        dt = time.perf_counter() - t0
        assert res.startswith("OK:"), res
        assert dt < 1.5, f"窓（2 秒）を払っている: {dt*1000:.0f} ms / {res}"
    finally:
        _killpg_quiet(pgid)
        for r in escaped:
            try:
                os.kill(r["pid"], 9)
            except OSError:
                pass


def test_待つ相手はこれから待ち受けを始めうるものだけ():
    """`_may_bind` の単体。★推測（正規表現）は**待つかどうか**にしか使わない。"""
    assert bo_tool._may_bind({"kinship": "descendant", "cmdline": "sleep 300"}) is True
    assert bo_tool._may_bind({"kinship": "scope", "cmdline": "sleep 300"}) is False
    assert bo_tool._may_bind({"kinship": "scope", "cmdline": "sleep 300"},
                             18300) is True          # 控えにポートの手掛かり
    assert bo_tool._may_bind(
        {"kinship": "scope",
         "cmdline": "sh -c sleep 1; exec python3 -m http.server 8123"}) is True
    assert bo_tool._may_bind({"kinship": "scope",
                              "cmdline": "php -S 0.0.0.0:8000 -t site/"}) is True


# ── 10 巡目（2026-09-05・再反証 MF-A）: **推測が False の側**は安全側ではなかった ──
#
# ★何が起きていたか（反証役が本番経路 `bash_output._run({"pid":…,"kill":True})` で実測）:
#   `_await_late_bind` は `_may_bind` が False の候補を `pend` から**外して**いたので、
#   「命令にポート番号が出てこない遅延 bind の逃げ子」は一度も読み直されなかった。
#     強い形（`sh -c 'sleep 1.2; exec … http.server <port>'`）… 794 ms / `Error:` ＋番号を名指し
#     弱い形（`setsid python -u srv.py &`／ポートは `port.txt`）… 148 ms / **`OK:`** ★false green
#   `_may_bind` の散文には「推測を安全側にだけ効かせる」と書いてあったが、
#   **推測の False がそのまま `OK:`＝『止まった』を作っていた**。
#   ⚠ base 04a0bbf でも同じ（この巡の退行ではなく、9 巡目が直したと申告した当の関数に
#     残っていた嘘）。10 巡目は (1) 散文を実態に合わせ (2) 弱い候補にも**短い窓**を付けた。

_WEAK_SRV_PY = (
    "import http.server, socketserver, time, pathlib\n"
    "time.sleep({delay})\n"
    "p = int(pathlib.Path('port.txt').read_text().strip())\n"
    "socketserver.TCPServer.allow_reuse_address = True\n"
    "httpd = socketserver.TCPServer(('127.0.0.1', p),\n"
    "                               http.server.SimpleHTTPRequestHandler)\n"
    "httpd.serve_forever()\n")


def _launch_weak_late_binder(env, port: int, delay: str) -> int:
    """**命令にポート番号が出てこない**遅延 bind の逃げ子（`_may_bind` が False）。

    ポートは `port.txt` から読むので、`bash._port_in_cmd` は何も掴めない
    ——実機で普通にある形（設定ファイル／環境変数からポートを読むサーバ）。
    """
    (env / "srv.py").write_text(_WEAK_SRV_PY.format(delay=delay), encoding="utf-8")
    (env / "port.txt").write_text(str(port), encoding="utf-8")
    # ★前提: これは**弱い候補**（推測が False）。`_may_bind` を「常に True」に
    #   変えて試験を通す抜け道を塞ぐ。
    assert bo_tool._may_bind(
        {"kinship": "scope", "cmdline": f"{sys.executable} -u srv.py"}) is False, \
        "この試験は推測が False の形を見る（前提が崩れている）"
    return _launch_nocwd(
        env,
        "#!/bin/sh\n"
        f"setsid {sys.executable} -u srv.py >/dev/null 2>&1 &\n"
        "sleep 300 &\necho started\n", timeout=1)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_命令に番号が無い遅延bindの逃げ子も窓を切って待つ(env, monkeypatch):
    """★再反証 MF-A。推測（`_port_in_cmd`）が False でも、**待たない**にはしない。

    ★窓を**実定数の 4 倍**に広げて測る（`_LATE_BIND_WEAK_SETTLE * 4`）。
      理由は 2 つ:
        ・共有ホストの負荷で bind の着地が数百 ms ずれる（既定 0.8 秒の余裕は
          実測で ~300 ms しかない＝素の値で測ると**試験が揺れる**）。
        ・それでも**定数そのものは効かせる**——`_LATE_BIND_WEAK_SETTLE = 0`
          （＝9 巡目の状態）に戻すと 0×4 = 0 でこの試験は赤くなる。
      既定値でこの形が捕まることは探針で測ってある（`x_forms.py weak 1.2`:
      3/3 で `Error:` ＋ポートを名指し・792〜798 ms）。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    monkeypatch.setattr(bo_tool, "_LATE_BIND_WEAK_SETTLE",
                        bo_tool._LATE_BIND_WEAK_SETTLE * 4)
    port = _free_port()
    pgid = _launch_weak_late_binder(env, port, "1.2")
    try:
        assert not _port_listening(port), "前提が崩れている（撃つ前にもう上がっている）"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and tool_failed(res), res
        assert str(port) in res, f"あとから上がったサーバを指していない: {res}"
        assert "撃つ前は待ち受けていませんでした" in res, res
    finally:
        _reap_listeners(port)
        time.sleep(0.3)
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
@pytest.mark.xfail(strict=True, reason=(
    "★未解決（10 巡目・再反証 MF-A で実測）: 弱い候補の窓は 0.8 秒しかないので、"
    "**それより遅く** bind する非子孫（命令に番号が無い形）は取り逃して `OK:` を返す。"
    "実測: bind 2.5 秒の形は 470 ms で `OK: SIGTERM で止まりました`＋サーバは上がる。"
    "窓を伸ばせば `setsid sleep 300 &` が居るだけの停止の費用が線形に増えるので、"
    "額では買えない。根治は控えに実 LISTEN／子の pid を書く bash.py 側の直し。"))
def test_命令に番号が無い遅い遅延bindは今も取り逃す(env, monkeypatch):
    """**まだ塞げていない**形を、緑にせず試験として残す（効かなかった手を消さない）。

    直ったらこの試験が緑になり `strict=True` が知らせる。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_weak_late_binder(env, port, "2.5")
    try:
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert _wait_until(lambda: _port_listening(port), timeout=5), \
            "前提が崩れている（あとから上がるはずのサーバが上がらない）"
        assert str(port) in res, res               # ← いまはここで落ちる（穴）
    finally:
        _reap_listeners(port)
        time.sleep(0.3)
        _reap_listeners(port)
        _killpg_quiet(pgid)


# ── 10 巡目（2026-09-05・再反証 MF-B）: `OK:` の本文に**緩めた印**を出す ──────
#
# ★何が起きていたか: `no_ruler`（控えに `start_ticks` が無い）と `kept_unproven`
#   （「この起動より前から居た」を証明できないので落とさずに残した候補の数）は
#   `_survivor_note` の中にしか書かれていなかった＝**生き残りが見つかって赤くなった
#   ときだけ**印が出る。印が要るのは逆の経路で、実測（弱い形の走行）では
#   `no_ruler=True` / `kept_unproven=2`・counters も `bg_launch_ticks_missing=1` /
#   `bg_kin_kept_unproven=2` と正しく立っているのに、利用者とモデルが読む文字列は
#   `OK: SIGTERM で止まりました（グループ N）。` だけだった。
#   **counters に出ていて本文に出ていないものは、読む側には無いのと同じ。**


def test_物差しの断りは残した候補があるときだけ数を書く():
    """`_unproven_note` の単体（3 形）。**落とした候補が無いなら黙る**（偽の赤を作らない）。"""
    assert bo_tool._unproven_note(None) == ""
    assert bo_tool._unproven_note({}) == ""
    assert bo_tool._unproven_note({"no_ruler": False, "kept_unproven": 0}) == ""
    t1 = bo_tool._unproven_note({"no_ruler": True, "kept_unproven": 2})
    assert "start_ticks" in t1 and "2 本" in t1, t1
    t2 = bo_tool._unproven_note({"no_ruler": True, "kept_unproven": 0})
    assert "start_ticks" in t2 and "1 本もありません" in t2, t2
    t3 = bo_tool._unproven_note({"no_ruler": False, "kept_unproven": 1})
    assert "起動時刻を読めませんでした" in t3 and "1 本" in t3, t3


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_物差しが無いまま残した候補はOKの本文にも書く(env, monkeypatch):
    """★再反証 MF-B。`kept_unproven` を抱えたまま `OK:` と言う経路にこそ印が要る。"""
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    pgid = _launch_nocwd(
        env,
        "#!/bin/sh\n"
        "setsid sleep 300 &\n"                        # ★逃げた**非サーバ**（弱い候補）
        "sleep 300 &\necho building\n", timeout=1)
    # ★union-2: merge-bash の前倒し捕捉で通常は start_ticks が残る。
    #   この試験の主眼（物差しが無い形）を保つため強制的に剥がす。
    meta = _force_no_start_ticks(pgid)
    assert meta.get("start_ticks") is None, "この試験は**物差しが無い控え**の形を見る"
    escaped = [r for r in procutil.proc_rows(skip_self=False)
               if r.get("cmdline", "").startswith("sleep 300")]
    assert escaped, "前提が崩れている（逃げた sleep が居ない）"
    try:
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("OK:"), res             # ★成功そのものは変えない
        assert "起動時刻（start_ticks）がありません" in res, \
            f"緩めた事実が本文に出ていない（counters にしか無い）: {res}"
        assert "落とさずに残しました" in res, res
    finally:
        _killpg_quiet(pgid)
        for r in escaped:
            try:
                os.kill(r["pid"], 9)
            except OSError:
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_物差しがある普通の停止には断りを出さない(env, monkeypatch):
    """偽の赤（というより**毎回の言い訳**）を増やしていないことの対照。

    `run_in_background=true` の控えは `proc_start_ticks` を撃つ前に読めるので
    `start_ticks` を持つ＝物差しがある。落とさずに残した候補も無い。
    そのときは `OK:` に何も足さない。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    out = _text(bash_tool._run({
        "command": f"{sys.executable} -u -m http.server {port} --bind 127.0.0.1",
        "cwd": str(env), "run_in_background": True}))
    pid = _pid(out)
    meta = json.loads((procutil.bg_dir() / f"{pid}.meta.json").read_text(encoding="utf-8"))
    assert isinstance(meta.get("start_ticks"), int), \
        f"この試験は**物差しがある控え**の形を見る（台帳: {meta.get('start_ticks')!r}）"
    try:
        assert _wait_until(lambda: _port_listening(port), timeout=5)
        res = _text(bo_tool._run({"pid": pid, "kill": True}))
        assert res.startswith("OK:"), res
        assert "start_ticks" not in res, f"物差しがあるのに断っている: {res}"
        assert "落とさずに残しました" not in res, res
    finally:
        _reap_listeners(port)
        _killpg_quiet(pid)


# ── 候補の上限（`_KIN_CAP`）で**黙って捨てない**（2026-09-05・8 巡目・再反証 M2）──
#
# ★何が起きていたか（`probe_m2.py`・本番の形で実測）:
#   `_KIN_CAP = 64` の注記は「切った本数は `snap["kin_total"]` に残す（黙って捨てない）」
#   と書いていたが、切り捨ての注記は **`_survivor_note` の中にしか無かった**
#   ＝**生き残りが見つかったときにしか出ない**。`_after_kill` / `_ok_text` は
#   `kin_total` を一度も見ず、`blind` にも積まれない（`scanned` は True のまま）。
#   しかも並べ替えが `not x["ports"]`＝**まだ待ち受けていない候補を後ろへ送る**ので、
#   4 巡目 (g) で塞いだはずの**遅延 bind の的から先に**落としていた。
#   実測: 候補 70 本 → `OK: SIGTERM で止まりました` ＋ 1.2 秒後にサーバが上がる。
#          候補 62 本 → 従来どおり `Error:`（境界の下は今までどおり）。
#
# ⚠ 水増しは合成（`procutil.proc_rows` に行を足す）だが、**的は本物のプロセス**で、
#   検知の経路（`_survivors` / `_await_late_bind` / `port_in_use`）は本番のまま。
#   共有ホストで 70 個のプロセスを起こさないための割り切り。


def _pad_rows(monkeypatch, ws, n: int, *, strong: bool = False,
             pid_base: int = 2_100_000_000):
    """候補を n 本水増しする。旧 sort は pid 昇順で、上限を食い潰して本物の的を
    押し出す欠陥を再現する形として使われていた（いまの並べ替えは kinship/ports/
    `_may_bind` が先で pid は最後のタイブレーカーなので、その意味は薄い）。

    ★union-2（2026-09-06・実測で発見）: 既定を**低い pid**（旧: 2）にしていたら、
      この機械（14 日稼働・多数の kworker/cpuhp スレッド）では低 pid の大半が
      **実在の古いカーネルスレッド**と衝突し、`_born_after_ticks` が「この起動より
      前から居る」と正しく判定して**候補にすら残らない**（実測: 70 本中 59 本が
      `kin_total` に入る前に落ちた）——水増しの本数を当てにする試験が軒並み
      「前提が崩れている」で赤くなった。`pid_base` を pid_max よりはるかに大きい
      値にすれば実在プロセスと衝突せず「もう居ない候補」になる
      （`_born_after_ticks` は None＝物差し無しで残す＝水増しの意図どおり）。"""
    real = procutil.proc_rows
    cmd = "python -u -m http.server 9999" if strong else "sleep 60"
    argv = (("python", "-u", "-m", "http.server", "9999") if strong
            else ("sleep", "60"))

    def rows(skip_self: bool = True):
        out = list(real(skip_self=skip_self))
        for i in range(n):
            out.append({"pid": pid_base + i, "comm": argv[0], "cmdline": cmd,
                        "cwd": str(ws), "argv": argv, "pgid": 900_000 + i})
        return out

    monkeypatch.setattr(procutil, "proc_rows", rows)


def _launch_late_binder(env, port: int, delay: str = "1.2") -> int:
    """撃ったあとに bind する逃げ子（`_may_bind` が True の**強い候補**）。"""
    pgid = _launch_nocwd(
        env,
        "#!/bin/sh\n"
        f"setsid sh -c 'sleep {delay}; exec {sys.executable} -u -m http.server {port} "
        "--bind 127.0.0.1' >/dev/null 2>&1 &\n"
        "sleep 300 &\necho started\n", timeout=1)
    # ★union-2（2026-09-06）: merge-bash の前倒し捕捉で通常は start_ticks が残る。
    #   この試験群は「物差しが無い」形（born_before による水増しの取りこぼしを
    #   起こさない形）に依存しているので、強制的に剥がす
    #   （`_force_no_start_ticks` の docstring 参照）。
    _force_no_start_ticks(pgid)
    return pgid


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
@pytest.mark.parametrize("pad,cap_exceeded", [(70, True), (61, False)],
                         ids=["候補71本-上限超え", "候補62本-境界の下"])
def test_候補が上限を超えても遅れてbindする逃げ子を切り捨てない(env, monkeypatch,
                                                                pad, cap_exceeded):
    """★再反証 M2。上限を超えた側でも `OK:` を出さず、**ポートを名指しする**こと。
    境界の下（62 本）は従来どおり `Error:`＝この直しが偽の赤を増やしていないこと。"""
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_late_binder(env, port)
    try:
        _pad_rows(monkeypatch, env, pad)
        assert ((pad + 1) > bo_tool._KIN_CAP) is cap_exceeded, \
            f"前提が崩れている（候補 {pad + 1} 本 / 上限 {bo_tool._KIN_CAP}）"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and tool_failed(res), res
        assert str(port) in res, f"切り捨てた逃げ子のポートを指していない: {res}"
        assert "撃つ前は待ち受けていませんでした" in res, res
        assert _wait_until(lambda: _port_listening(port), timeout=3), \
            "前提が崩れている（あとから上がるはずのサーバが上がらない）"
    finally:
        monkeypatch.undo()
        _reap_listeners(port)
        time.sleep(0.5)
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_強い候補を切り捨てたなら止まったと言わない(env, monkeypatch):
    """上限を超えた候補が**全部**「これから bind しうる」相手なら、的そのものが
    切り落とされる。そのときは名指しできないので、せめて **`OK:` を言わない**
    （＝観測が最後まで通っていないと言う）。黙って捨てた側を `OK:` に流さない。

    ★2026-09-05・9 巡目（再反証 MF-1）で**境界が変わった**。8 巡目はここを
      `_KIN_CAP = 64` で測っていたが、強い候補にその床を掛けると
      **正しく止まった停止まで `Error:`** になっていた（下の
      `test_生きた強い第三者が上限を超えても正しい停止はOKと言う` が同じ形を
      緑で固定する）。いま強い候補が切られるのは `_KIN_STRONG_CAP` の上だけなので、
      **切り捨てを起こすにはそこを超えて水増しする**。
      本文が名乗る本数も `_KIN_CAP` ではなく**実際に読み直した数**であること。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_late_binder(env, port)
    pad = bo_tool._KIN_STRONG_CAP + 40
    seen: dict = {}
    real_obs = bo_tool._observe_listen

    def obs(*a, **kw):
        snap = real_obs(*a, **kw)
        seen.update(kin_total=snap.get("kin_total"),
                    kin=len(snap.get("kin") or []),
                    over=len(snap.get("kin_over") or []))
        return snap

    monkeypatch.setattr(bo_tool, "_observe_listen", obs)
    try:
        # ★union-2: この試験は「低い pid の水増しが**本物の的（実 pid）を
        #   押し出す**」形に依存する（`_pad_rows` の docstring 冒頭のとおり、
        #   いまの並べ替えでは pid が最後のタイブレーカー）。既定の高 pid_base
        #   では実 pid のほうが必ず低くなり escapee が常に生き残ってしまうので、
        #   ここだけ明示的に低い範囲を使う（`_launch_late_binder` が
        #   `_force_no_start_ticks` で物差しを外しているので、低 pid でも
        #   実在カーネルスレッド衝突による水増しの取りこぼしは起きない）。
        _pad_rows(monkeypatch, env, pad, strong=True, pid_base=2)
        t0 = time.perf_counter()
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        dt = time.perf_counter() - t0
        assert res.startswith("Error:") and tool_failed(res), res
        assert "確かめられていません" in res, res
        assert seen["kin"] == bo_tool._KIN_STRONG_CAP and seen["over"] > 0, \
            f"前提が崩れている（強い候補が切られていない）: {seen}"
        assert f"{bo_tool._KIN_STRONG_CAP} 本だけです" in res, \
            f"読み直した本数が実態と違う（_KIN_CAP を書いていないか）: {res}"
        assert f"候補が {seen['kin_total']} 本あり" in res, \
            f"候補の総数が本文に出ていない: {res}"
        # ★「まだ生きています」だけだと**撃ったのに死ななかった**と読める。
        #   `kin` は撃つ pgid の外の行なので、そう読まれると原因を取り違える。
        assert "撃った相手ではありません" in res, \
            f"生き残りの意味を取り違える書き方になっている: {res}"
        assert "止まりました" not in res, res
        # ★床を 64→128 に上げた分の**費用**をここで測る（限界の自己申告のため）。
        #   窓（`_LATE_BIND_SETTLE` 2.0 秒）＋読み直しで、実測 3 秒台に収まること。
        assert dt < 8.0, f"切り捨ての読み直しに時間が掛かりすぎ: {dt:.2f} 秒"
    finally:
        monkeypatch.undo()
        _reap_listeners(port)
        time.sleep(0.5)
        _reap_listeners(port)
        _killpg_quiet(pgid)


# ── 上限が**偽の赤**を作らない（2026-09-05・9 巡目・再反証 MF-1）───────────
#
# ★何が起きていたか（`p_mf1.py`・本番の形・的は逃げ子も待受も無い普通の停止）:
#   8 巡目は強い候補も `_KIN_CAP = 64` で切り、「**切って生きていたら盲**」に
#   していた。ところが `kin` は定義上**撃つ pgid の外**の行だけ（構成員は
#   `pg == pgid` で先に除かれる）なので、`os.killpg(pgid, …)` は `kin_over` の
#   相手を**一度も殺せない**＝「生きていた」が構造的にほぼ常に真。
#   実測（生きている第三者 70 本を水増し・的は `sleep 300 &` だけ）:
#     base 04a0bbf … `Error: …止まったかどうかは確かめられていません`（tool_failed=True）
#     いま         … `OK: SIGTERM で止まりました`
#   ＝**正しく止まった停止が偽の赤**になっていた。


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_生きた強い第三者が上限を超えても正しい停止はOKと言う(env, monkeypatch):
    """`_KIN_CAP` を超える**生きている**強い候補（作業場に居る第三者）が居ても、
    的が本当に止まったなら `OK:`。上の `…止まったと言わない` と対になる
    ——切り捨てを盲にするのは `_KIN_STRONG_CAP` を超えたときだけ。"""
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    pgid = _launch_nocwd(env, "#!/bin/sh\nsleep 300 &\necho started\n", timeout=1)
    seen: dict = {}
    real_obs = bo_tool._observe_listen

    def obs(*a, **kw):
        snap = real_obs(*a, **kw)
        seen.update(kin_total=snap.get("kin_total"),
                    kin=len(snap.get("kin") or []),
                    over=len(snap.get("kin_over") or []))
        return snap

    monkeypatch.setattr(bo_tool, "_observe_listen", obs)
    try:
        # ★pid_base=2＝**実在して生きている** pid（合成行だが `_is_alive` は True）。
        #   ここが `test_上限で切った候補が撃って死んだならOKと言う`（もう居ない pid）
        #   との違いで、MF-1 はこちらでしか出ない。
        _pad_rows(monkeypatch, env, 70, strong=True)
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert seen.get("kin_total", 0) > bo_tool._KIN_CAP, \
            f"前提が崩れている（候補 {seen} / 上限 {bo_tool._KIN_CAP}）"
        assert seen.get("over") == 0, \
            f"強い候補が切られている（_KIN_STRONG_CAP={bo_tool._KIN_STRONG_CAP}）: {seen}"
        assert res.startswith("OK:"), f"切り捨てだけで偽の赤が出た: {res}"
        assert "確かめられていません" not in res, res
    finally:
        monkeypatch.undo()
        _killpg_quiet(pgid)


# ── 並べ替えは 3 段（2026-09-05・9 巡目・再反証 MF-3）────────────────────
#
# ★何が起きていたか（`p_rank.py`・逃げ子は本物で**撃つ前から LISTEN 中**・
#   水増しは `python app.py --port 8080` を 70 本＝`_may_bind` だけ True）:
#     base 06215bf … `Error: …待ち受けが残っています` ポート番号を名指し **する**
#     8 巡目 tip  … `Error: …止まったかどうかは確かめられていません` 名指し **しない**
#   8 巡目は「現に LISTEN している」と「正規表現で bind しそう」を同じ段に潰した
#   ので、低 pid の**推測**候補が待受中の逃げ子を上限の外へ押し出した。
#   どちらも赤だが、**唯一の次の一手（`BashOutput(port=<番号>)`）に要る番号**が
#   本文から消える＝赤の質が落ちる。


def _launch_listening_escapee(env, port: int) -> int:
    """撃つ**前から**待ち受けている逃げ子（`setsid` でグループの外）。"""
    pgid = _launch_nocwd(env, _ESCAPE_NOWAIT_SH.format(py=sys.executable, port=port),
                         timeout=1)
    assert _wait_until(lambda: _port_listening(port), timeout=5), \
        "逃げたサーバが上がっていない（前提が崩れている）"
    return pgid


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_現に待ち受けている逃げ子は推測だけの候補に押し出されない(env, monkeypatch):
    """★再反証 MF-3。観測した LISTEN（証拠）は、正規表現の推測より**先に**残す。

    ⚠ **切り捨てが起きる本数まで水増しする**。`_KIN_STRONG_CAP` の下では全員
      残るので、並べ替えを 8 巡目の 2 段に戻しても結果が変わらない
      ——それでは「並べ替え」ではなく「床の高さ」を測っていることになる
      （最初に書いた版がまさにそれで、変異 B を素通しした）。
    ★見るのは**置き場所**（`kin` か `kin_over` か）と本文の両方。番号が本文に出る
      だけなら `_survivors` の読み直し（別の機構）でも通ってしまう。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    port = _free_port()
    pgid = _launch_listening_escapee(env, port)
    owner = [r["pid"] for r in procutil.port_listeners(port)
             if r.get("pid") not in (os.getpid(), os.getppid())]
    assert owner, "逃げ子の pid が取れない（前提が崩れている）"
    seen: dict = {}
    real_obs = bo_tool._observe_listen

    def obs(*a, **kw):
        snap = real_obs(*a, **kw)
        seen.update(kin=[r["pid"] for r in (snap.get("kin") or [])],
                    over=[r["pid"] for r in (snap.get("kin_over") or [])],
                    kin_total=snap.get("kin_total"))
        return snap

    monkeypatch.setattr(bo_tool, "_observe_listen", obs)
    try:
        # 推測だけ（`_may_bind` は True・待受は無い）の低 pid を床より多く
        _pad_rows(monkeypatch, env, bo_tool._KIN_STRONG_CAP + 40, strong=True)
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert seen["over"], f"前提が崩れている（切り捨てが起きていない）: {seen['kin_total']}"
        assert set(owner) & set(seen["kin"]), (
            "待受中の逃げ子が推測だけの候補に押し出された"
            f"（逃げ子 {owner} / 切り捨て側 {seen['over'][:5]}…）")
        assert not (set(owner) & set(seen["over"])), owner
        assert res.startswith("Error:") and tool_failed(res), res
        assert str(port) in res, f"番号が本文から消えた: {res}"
    finally:
        monkeypatch.undo()
        _reap_listeners(port)
        time.sleep(0.5)
        _reap_listeners(port)
        _killpg_quiet(pgid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_弱い候補を捨てた事実を数と本文に残す(env, monkeypatch):
    """★再反証 MF-4 の副作用。上限は弱い候補にだけ効くので、**捨てた事実**が
    どこにも残らないと「痕跡ゼロで消える」形が戻る。

    ・数 … `bg_kin_dropped_weak`（本番で 0 なら配線ミスと分かる形）。
    ・本文 … `_survivor_note` の「候補は N 本あり、M 本を読み直しました」の
             **M は実際に読み直した数**（`_KIN_CAP` を書くと嘘になる）。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)

    class _Ag:                                  # `_bump` が数を書き込む先
        counters: dict = {}

    ag = _Ag()
    ag.counters = {}
    tok = tools_mod.CURRENT_AGENT.set(ag)
    port = _free_port()
    pgid = _launch_listening_escapee(env, port)
    seen: dict = {}
    real_obs = bo_tool._observe_listen

    def obs(*a, **kw):
        snap = real_obs(*a, **kw)
        seen.update(kin_total=snap.get("kin_total"),
                    kin=len(snap.get("kin") or []),
                    over=len(snap.get("kin_over") or []))
        return snap

    monkeypatch.setattr(bo_tool, "_observe_listen", obs)
    try:
        # ★強い候補を 64 本より多くしてから弱い候補を足す。そうしないと
        #   「読み直した本数」がたまたま `_KIN_CAP` と同じ 64 になり、本文が
        #   `_KIN_CAP` を書いていても試験が通ってしまう（変異 E を素通しした形）。
        # ★union-2（2026-09-06）: 強弱で pid 範囲を分ける（`_pad_rows` の既定と
        #   衝突しないよう弱い側は別範囲を明示・詳細は `_pad_rows` の docstring）。
        _pad_rows(monkeypatch, env, 70, strong=True)    # 推測あり＝強い（残る）
        _pad_rows(monkeypatch, env, 70, strong=False,   # 待受も手掛かりも無い＝弱い
                  pid_base=2_000_000_000)
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        dropped = seen["kin_total"] - seen["kin"] - seen["over"]
        assert seen["kin"] != bo_tool._KIN_CAP, \
            f"読み直した本数が _KIN_CAP と同じでは本文の嘘を捕まえられない: {seen}"
        assert dropped > 0, f"前提が崩れている（弱い候補が捨てられていない）: {seen}"
        assert ag.counters.get("bg_kin_dropped_weak") == dropped, \
            f"捨てた数が計器に出ていない: {ag.counters} / {seen}"
        assert str(port) in res, res
        assert f"候補は {seen['kin_total']} 本あり、{seen['kin']} 本を読み直しました" in res, \
            f"読み直した本数が実態と違う: {res}"
    finally:
        tools_mod.CURRENT_AGENT.reset(tok)
        monkeypatch.undo()
        _reap_listeners(port)
        time.sleep(0.5)
        _reap_listeners(port)
        _killpg_quiet(pgid)


def test_切り捨てのうち待ち受けていた行は盲にせず読み直す():
    """★9 巡目・MF-1 の第 2 段（`_survivors` の新しい枝）。

    `_KIN_STRONG_CAP` から溢れた行でも、**撃つ前に現に LISTEN していた**なら
    `kin` と同じ物差しで読み直す＝ポート番号を名指しできる。
    8 巡目はここを一律「確かめられていません」に落としていた（名指しできるのに
    しない＝赤の質が落ちる）。

    ⚠ 本番の形（プロセスを 129 本起こす）は共有ホストでは取れないので、
      `_survivors` を直に呼ぶ。**待ち受けは本物**（この試験プロセス自身が握る）で、
      読み直しの経路（`_listen_inode_ports` → `_pid_listen_ports`）は本番のまま。
    """
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        me, up = os.getpid(), os.getppid()
        snap = {
            "kin": [],
            # ①撃つ前から待ち受けていた（読み直す） ②待受も無い（盲の材料）
            "kin_over": [
                {"pid": me, "ports": [port], "kinship": "scope",
                 "cmdline": "pytest", "argv": ("pytest",), "cwd": os.getcwd()},
                {"pid": up, "ports": [], "kinship": "scope",
                 "cmdline": "python -m http.server 9999",
                 "argv": ("python", "-m", "http.server", "9999"), "cwd": os.getcwd()},
            ],
            "kin_total": 200, "blind": [],
        }
        out = bo_tool._survivors(snap)
        assert [r["pid"] for r in out] == [me], \
            f"待ち受けていた切り捨てを読み直していない: {out}"
        assert port in out[0]["ports"], out
        assert snap["kin_read"] == 1, snap.get("kin_read")
        assert any("200 本あり" in m and "1 本だけです" in m for m in snap["blind"]), \
            f"読み直していない側の盲が積まれていない: {snap['blind']}"
    finally:
        sock.close()


# ── 道具の外へ例外を出さない（2026-09-05・8 巡目・再反証 M3）─────────────
def test_道具が途中で落ちたら機械が読める失敗を返す(env, monkeypatch):
    """★実測（`probe_m3.py`・`del procutil._boot_time`）:
        RESULT: (例外が道具の外まで出た) NameError: name '_boot_time' is not defined
        tool_failed = False
      利用者とモデルが読むのは `Error in BashOutput: …` になるが、
      `tools/__init__.py:tool_failed` が失敗と読む根拠は **`Error:` 前置き**なので
      **False**＝この 1 経路だけ、3 巡目に自分で立てた規律に乗っていなかった。

    ★union-2（2026-09-06）: 正典名は `_boot_epoch`（merge-bash 側の改名）。
      `_launch_epoch` はいまもこれを呼ぶ（`since_epoch=_launch_epoch(...)`）ので、
      同じ「取り違えが例外の外まで出る」形は現行名を消すことで再現する。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    started = _text(bash_tool._run({"command": "sleep 300", "cwd": str(env),
                                    "run_in_background": True}))
    pid = _pid(started)
    try:
        monkeypatch.delattr(procutil, "_boot_epoch")
        res = _text(bo_tool._run({"pid": pid, "kill": True}))   # 例外を外へ出さない
        assert res.startswith("Error:") and tool_failed(res), res
        assert "確かめられていません" in res, res
        assert "道具の欠陥" in res, res
    finally:
        monkeypatch.undo()
        _killpg_quiet(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_上限で切った候補が撃って死んだならOKと言う(env, monkeypatch):
    """★偽の赤を作らない側（8 巡目の refine）。`make -j64` のように**身内が上限を
    超える**作業場は普通にある。切り捨てを「観測できなかった」と機械的に読むと、
    正しく止まった停止まで全部「確かめられていません」になる。

    切った候補を盲にするのは、**撃ったあとも生きていた**ときだけ
    （`_is_alive` は `os.kill(pid,0)` 1 発なので、上限を置いた理由＝
    `/proc/<pid>/fd` の読み直しとは費用が桁違い）。
    ここでは切った候補を全部「もう居ない pid」にして、`OK:` が出ることを見る。
    """
    monkeypatch.setenv("LAPS_WORKSPACE", str(env))
    monkeypatch.chdir(env)
    pgid = _launch_nocwd(env, "#!/bin/sh\nsleep 300 &\necho started\n", timeout=1)
    try:
        # pid_max より大きい番号＝**もう居ない**（強い候補だが撃つまでもなく死んでいる）
        _pad_rows(monkeypatch, env, 70, strong=True, pid_base=2_000_000_000)
        t0 = time.perf_counter()
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        dt = time.perf_counter() - t0
        assert res.startswith("OK:"), f"切り捨てだけで偽の赤が出た: {res}"
        assert "確かめられていません" not in res, res
        assert dt < 1.5, f"死んだ候補のために窓を払っている: {dt*1000:.0f} ms"
    finally:
        monkeypatch.undo()
        _killpg_quiet(pgid)
