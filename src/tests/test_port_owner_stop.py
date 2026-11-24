"""ポート起点で「持ち主を見つけて安全に止める」（第4回 core-server-lifecycle・2026-09-05）。

★なぜ要るか（run50-52 の実測）: Bash 902 件のうち
  `ps aux|grep` / `lsof` / `ss` / `pgrep` によるポート持ち主探しが **30 件**、
  `kill`/`pkill`/`killall` が **29 件**。モデルは毎回 2〜4 手を組み立て直しており、
  しかも `pkill -f <語>` は自分の argv に当たって**自分を撃つ**（実機 9/9）。

★安全規律は `bash.py:_scope_name_kill` と同じ「cwd が作業場配下か」。これを省くと、
  名前ではなくポート経由で他人のサービスを撃つ事故が再現する
  （2026-09-02 に corridor-bridge を 6 回 SIGKILL している）。ここではその
  **撃たないこと**を実プロセスで確かめる（文言だけでなく生存で見る）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

from laps import procutil
from laps.tools import bash as bash_tool
from laps.tools import bash_output as bo_tool


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until(fn, timeout=5.0, interval=0.05) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(interval)
    return False


def _listening(port: int) -> bool:
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=0.3)
        c.close()
        return True
    except OSError:
        return False


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ws = tmp_path / "ws"
    ws.mkdir()
    # ★作業場は**正典（LAPS_WORKSPACE）から**取る（2026-09-05・第4回b）。
    #   conftest の既定は `/tmp` なので、これを張らないと tmp_path 配下は
    #   すべて「作業場の中」になり、「外を撃たない」試験が的を失う
    #   （最初に書いた版がそれで、外の的が正しく中と判定されて赤になった）。
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    return ws


pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="/proc から読むので POSIX 専用")


def test_ポートの持ち主を引き当てて作業場の中だけ止める(env):
    port = _free_port()
    out = bash_tool._run({"command": f"python3 -u -m http.server {port} --bind 127.0.0.1 &",
                          "cwd": str(env), "timeout": 20})
    out = out if isinstance(out, str) else out.text
    assert _wait_until(lambda: _listening(port), timeout=5), out

    # 見るだけ: 持ち主が PID つきで出て、作業場の中と分類される
    look = bo_tool._run({"port": port, "cwd": str(env)})
    assert "作業場の中" in look, look
    assert "http.server" in look, look
    assert "kill=true" in look, look

    # 止める: 実際にポートが空く
    res = bo_tool._run({"port": port, "cwd": str(env), "kill": True})
    assert "OK:" in res, res
    assert _wait_until(lambda: not procutil.port_in_use(port), timeout=5), res
    assert "空きました" in res, res


def test_作業場の外のサーバはポート経由でも撃たない(env, tmp_path):
    """★これを省くと corridor-bridge を殺した事故がポート経由で再現する。
    文言ではなく**相手が生きていること**で確かめる。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(outside), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        assert _wait_until(lambda: _listening(port), timeout=5)
        res = bo_tool._run({"port": port, "cwd": str(env), "kill": True})
        assert "作業場の外" in res, res
        assert "何も撃っていません" in res, res
        # ★must_fix B-4（union-2）: 作業場の中に持ち主が居ない＝この道具の
        #   「作業場に無い」拒否。`Error:` 前置きで機械にも失敗と読める。
        from laps.tools import tool_failed
        assert res.startswith("Error:") and tool_failed(res), res
        time.sleep(0.4)
        assert proc.poll() is None, "作業場の外のプロセスを殺している"
        assert _listening(port), "作業場の外のサーバが止まっている"
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except OSError:
            pass
        proc.wait(timeout=5)


def test_エージェント自身が掴むポートは絶対に撃たない(env):
    """試験プロセス＝この文脈でのエージェント本体。自分を撃ったら走行ごと死ぬ。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        res = bo_tool._run({"port": port, "cwd": str(env), "kill": True})
        assert "このエージェント自身" in res, res
        assert "何も撃っていません" in res, res
        assert _listening(port), "自分自身を撃った"
    finally:
        s.close()


def test_誰も待ち受けていないポートは正直にそう言う(env):
    port = _free_port()
    res = bo_tool._run({"port": port, "cwd": str(env)})
    assert "空いています" in res, res


def test_ポートが数でなければ道具ごと落ちない(env):
    assert bo_tool._run({"port": "eighty"}).startswith("Error:")
    assert bo_tool._run({"port": 70000}).startswith("Error:")


def test_pgid_の解析はcommに空白や括弧が入っても壊れない(monkeypatch):
    """`/proc/<pid>/stat` の 2 番目は括弧に囲まれ、中に空白も括弧も入りうる
    （`php -S` を exec -a で名乗った場合など）。素朴な split は必ずいつか壊れる。"""
    fake = "4242 (weird (name) here) S 1 909 909 0 -1 4194560 100 0 0 0"
    monkeypatch.setattr(procutil, "_read", lambda path, binary=False: fake)
    assert procutil.pgid_of(4242) == 909


def test_自分のプロセスグループは決して撃たない():
    """`_group_members` は自分のグループを空で返す（撃つ対象にしない）。"""
    my_pgid = os.getpgid(0)
    assert bo_tool._group_members(my_pgid) == []
    # 実在しないグループも空（例外にしない）
    assert bo_tool._group_members(0) == []
    assert bo_tool._group_members(None) == []


# ── 第4回b: 反証で見つかった穴（すべて実プロセスの生死で確かめる）────────────
def _spawn_outside(tmp_path, port: int):
    """作業場の外で待ち受けるサーバ。**撃たれていないこと**を生存で見るための的。"""
    outside = tmp_path / "outside_target"
    outside.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(outside), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    assert _wait_until(lambda: _listening(port), timeout=5)
    return proc


def _reap(proc):
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


@pytest.mark.parametrize("mode", ["readonly", "workspace"])
def test_kill_は政策ゲートの下にある(env, tmp_path, monkeypatch, mode):
    """★塞いだ穴（実測）: `LAPS_POLICY=readonly` で `Bash("kill <pid>")` は
    `policy denied` になるのに、**同じプロセスが BashOutput(port=…, kill=true) では
    止まった**。`bash.py` 自身が「政策ゲートはあらゆる dispatch path の前」と
    書いている、その dispatch path をこの道具が新設していた。

    文言ではなく**相手が生きていること**で確かめる。
    """
    port = _free_port()
    out = bash_tool._run({"command": f"python3 -u -m http.server {port} --bind 127.0.0.1 &",
                          "cwd": str(env), "timeout": 20})
    out = out if isinstance(out, str) else out.text
    assert _wait_until(lambda: _listening(port), timeout=5), out
    try:
        monkeypatch.setenv("LAPS_POLICY", mode)
        res = bo_tool._run({"port": port, "cwd": str(env), "kill": True})
        assert "policy denied" in res, res
        assert "何も撃っていません" in res, res
        # ★must_fix B-4（union-2）: 拒否は `Error:` 前置きで機械にも失敗と読める。
        from laps.tools import tool_failed
        assert res.startswith("Error:") and tool_failed(res), res
        time.sleep(0.4)
        assert _listening(port), f"政策が拒んだのにプロセスが死んでいる（{mode}）"
        # 同じ政策で Bash の kill も拒まれる＝道具ごとに答えが違わない
        shell = bash_tool._run({"command": "kill -TERM 999999", "cwd": str(env)})
        shell = shell if isinstance(shell, str) else shell.text
        assert "policy denied" in shell, shell
    finally:
        monkeypatch.setenv("LAPS_POLICY", "trusted")
        bo_tool._run({"port": port, "cwd": str(env), "kill": True})


def test_cwd_で撃つ範囲を広げられない(env, tmp_path):
    """★塞いだ穴（実測）: 同じ作業場外のリスナが、cwd 省略なら「何も撃っていません」で
    生存したのに、`cwd="/"` を渡すと `[作業場の中]` に分類されて SIGTERM で死んだ。
    ＝**撃つ範囲をモデル自身が決められる**状態だった（ラボの知見「実行可否はモデルの
    外側に置く」に真正面から反する）。cwd は**狭める**方向にしか効かないこと。
    """
    port = _free_port()
    proc = _spawn_outside(tmp_path, port)
    try:
        for widen in ("/", "/root", str(tmp_path)):
            res = bo_tool._run({"port": port, "cwd": widen, "kill": True})
            assert "作業場の外" in res, (widen, res)
            assert "何も撃っていません" in res, (widen, res)
            time.sleep(0.3)
            assert proc.poll() is None, f"cwd={widen} で作業場の外を撃った"
            assert _listening(port), f"cwd={widen} で作業場の外のサーバが止まった"
    finally:
        _reap(proc)


def test_保護ポートは作業場の中でも撃たない(env, monkeypatch):
    """22（sshd）・18301（閲覧の中継）・18400（Android ゲートウェイ）は
    「作業場の中に見えても基盤側の物」。2026-09-03 の dogfood で実際に
    gateway の unit を撃って本番を壊している。★18300 は**保護しない**
    （走行が自分で建てるサーバで、片付けられないと工程4 の壁が再来する）。"""
    assert 18300 not in procutil.protected_ports()
    assert {22, 18301, 18400} <= procutil.protected_ports()
    port = _free_port()
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", str(port))
    out = bash_tool._run({"command": f"python3 -u -m http.server {port} --bind 127.0.0.1 &",
                          "cwd": str(env), "timeout": 20})
    out = out if isinstance(out, str) else out.text
    assert _wait_until(lambda: _listening(port), timeout=5), out
    try:
        res = bo_tool._run({"port": port, "cwd": str(env), "kill": True})
        assert "保護" in res, res
        # ★must_fix B-4（union-2）: 拒否は `Error:` 前置きで機械にも失敗と読める。
        from laps.tools import tool_failed
        assert res.startswith("Error:") and tool_failed(res), res
        time.sleep(0.3)
        assert _listening(port), "保護ポートを撃っている"
    finally:
        monkeypatch.delenv("LAPS_PROTECTED_PORTS", raising=False)
        bo_tool._run({"port": port, "cwd": str(env), "kill": True})


def test_stop_group_の床(env):
    """pgid<=1（init）と、自分・祖先のグループは撃たない。★:22 の持ち主は
    この機械では pid 1 / pgid 1（socket 活性化の systemd）で、`cwd="/"` を
    渡すと `killpg(1, …)` の経路に入っていた（床が 1 つも無かった）。"""
    for bad in (0, 1, -1, "x"):
        r = bo_tool._stop_group(bad)
        assert r.startswith("Error:"), (bad, r)
    mine = os.getpgid(0)
    r = bo_tool._stop_group(mine)
    assert r.startswith("Error:") and "自身" in r, r
    assert os.getpgid(0) == mine, "自分のグループを撃った"


def test_構成員が見えないグループを止めたと言わない(env, tmp_path):
    """★塞いだ穴（実測）: グループの外へ出た起動（setsid）に対して
    `pid … already exited`（成功の顔）を返しながら、ポートは使われたままだった
    ——`_FALSE_GREEN_KINDS` の "bind" ループを作る本体。"""
    port = _free_port()
    proc = _spawn_outside(tmp_path, port)
    try:
        # 実在しないグループ番号＋そのポートが使用中、という状況を本番関数へ
        r = bo_tool._stop_group(2_000_000, port=port)
        assert r.startswith("Error:"), r
        assert "まだ使われています" in r, r
        assert proc.poll() is None
    finally:
        _reap(proc)


def test_作業場のパスを触るだけの外部プロセスは撃たない(env, tmp_path):
    """★塞いだ穴（実測 4/4 True）: `in_workspace` の前方一致に区切りが無く、
    `ws in cmdline` の部分一致まで「自分の物」にしていた
    （`/home/user/project` → `/home/user/project-restore`、`/tmp` の `tail -f <ws>/x.log`）。
    名前撃ちでは「絞る」だけだったこの緩さが、ポート経由では**撃つ許可**になっていた。"""
    ws = str(env)
    sibling = ws + "-decoy"
    row_sibling = {"pid": 424242, "comm": "php", "cmdline": f"php -S x -t {sibling}",
                   "cwd": sibling, "argv": ("php", "-S", "x", "-t", sibling)}
    assert not procutil.in_workspace(row_sibling, ws), "兄弟ディレクトリを中と誤判定"
    row_ref = {"pid": 424243, "comm": "tail", "cmdline": f"tail -f {ws}/x.log",
               "cwd": "/tmp", "argv": ("tail", "-f", f"{ws}/x.log")}
    assert not procutil.in_workspace(row_ref, ws), "cmdline の部分一致で撃つ許可が出ている"
    assert procutil.references_workspace(row_ref, ws), "列挙の材料としては拾うこと"
    row_in = {"pid": 424244, "comm": "php", "cmdline": "php -S x",
              "cwd": os.path.join(ws, "site"), "argv": ("php", "-S", "x")}
    assert procutil.in_workspace(row_in, ws), "作業場の中を外と誤判定（自分のサーバを撃てない）"


def test_名前撃ちとポート撃ちが同じ判定を使う(env, monkeypatch):
    """★第4回の 12 領域で唯一の衝突がこの表だった。片方だけ直る形を作らない:
    `bash._scope_name_kill` と `bash_output._port_report` は同じ
    `procutil.in_workspace` を呼ぶ（差し替えたら**両方**の答えが変わる）。"""
    ws = str(env)
    sibling = ws + "-decoy"
    table = [(4242, "php", f"php -S 0.0.0.0:1 -t {sibling}", sibling,
              ("php", "-S", "0.0.0.0:1", "-t", sibling))]
    monkeypatch.setattr(bash_tool, "_proc_table", lambda: list(table))
    r = bash_tool._scope_name_kill("pkill -f php", ws)
    assert r is not None and r[0] == "true", r
    calls = []
    real = procutil.in_workspace
    monkeypatch.setattr(procutil, "in_workspace",
                        lambda row, wsx=None, **kw: calls.append(row["pid"]) or real(row, wsx, **kw))
    bash_tool._scope_name_kill("pkill -f php", ws)
    assert calls == [4242], "名前撃ちが procutil.in_workspace を呼んでいない"


# ── 第4回c の 2 巡目（2026-09-05）: 原因を断言しない／自分を別ユーザと言わない ──
def test_無関係の第三者がポートに居るとき撃つ助言を出さない(env, tmp_path):
    """★must_fix ②（`scratchpad/r4c/expH.py` が実物で再現）。

    旧 `_escaped_note` は「待受が残っている」だけで
    **「グループの外へ出た起動（setsid/disown/nohup 付きサブシェル）です」と断言**し、
    そのうえ `BashOutput(port=P, kill=true)` を勧めていた。実測では、自分の背景処理
    （ポートを一度も掴んでいない sleeper・控えの `cmd` にだけ `php -S 0.0.0.0:P` と
    書いてある）を止めたら、**たまたま同じポートに居た無関係の第三者**について
    「逃げた起動です」と言い切り、勧めたとおりに実行して**その第三者を殺した**
    （`third.poll() = -15`）。再 bind の競合（`expB` B3）も同じ形。

    この道具が握っているのは **pgid とポート番号だけ**で、そのポートの持ち主が
    「いま止めた相手の残骸」だという証拠はどこにも無い。だから断定しない。
    ⚠ 撃つ道自体は塞いでいない: `BashOutput(port=P)` は持ち主と cwd を並べたうえで、
      作業場の中の物が居るときにだけ `kill=true` を案内する＝**見てから撃つ**順序。
    """
    port = _free_port()
    third = _spawn_outside(tmp_path, port)          # 止める相手とは無関係の第三者
    stray = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=str(env), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        res = bo_tool._stop_group(os.getpgid(stray.pid), port=port, grace=0.6)
        assert not res.startswith("OK:"), res
        assert "区別できません" in res, res                 # 原因を断言していない
        assert "kill=true" not in res, res                  # 撃つ助言を出していない
        assert f"BashOutput(port={port})" in res, res       # 見る手は出している
        time.sleep(0.3)
        assert third.poll() is None, "無関係の第三者を殺した"
        assert _listening(port)
    finally:
        _reap(third)
        _reap(stray)


def test_停止のあとに生まれたリスナは残骸と決めつけない(env, tmp_path):
    """★must_fix ② の言い切ってよい側。起動時刻（`procutil.proc_start_ticks`）が
    **この停止を始めた時点より後**なら、止めた相手の残骸ではありえない
    （その時点でまだ存在していない）。そこだけは言い切る——が、撃つ助言は出さない。

    比較は `time.time()` でなく boot からの tick で行う（`_uptime_ticks`）。
    壁時計に直すには `/proc/stat` の `btime`（**整数秒**）が要り、±1 秒の丸めが
    「分からない」を「別人」に変えてしまうため。
    """
    since = bo_tool._uptime_ticks()
    assert since is not None, "この機械で /proc/uptime が読めない"
    time.sleep(0.05)                                  # 5 tick 以上あける
    port = _free_port()
    later = _spawn_outside(tmp_path, port)            # since より後に生まれたリスナ
    try:
        assert bo_tool._started_after(later.pid, since) is True
        note = bo_tool._escaped_note(2_000_000, port, since_ticks=since)
        assert note, "待受が残っているのに何も言わなかった"
        assert "あとに起動した" in note, note
        assert "残骸ではありません" in note, note
        assert "kill=true" not in note, note
        # 逆側: 物差しが無ければ言い切らない（分からないものを別人にしない）
        vague = bo_tool._escaped_note(2_000_000, port, since_ticks=None)
        assert vague and "区別できません" in vague, vague
        assert later.poll() is None
    finally:
        _reap(later)


def test_自分が掴んでいるポートを別ユーザと言わない(env):
    """★must_fix ⑤（軽）: `_escaped_note` は自分と親を `owners` から除くのに
    `port_in_use` からは除いていなかったので、**エージェント自身が掴んでいる
    ポート**で「持ち主は /proc から見えません＝別ユーザの可能性」という嘘が出た。
    見えないのではなく、見えているのが自分。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    port = s.getsockname()[1]
    try:
        assert procutil.port_in_use(port), "前提: 自分が LISTEN している"
        note = bo_tool._escaped_note(2_000_000, port)
        assert note, "自分が掴んでいるのに待受なしと言った"
        assert "別ユーザ" not in note, note
        assert "エージェント自身" in note, note
        assert "kill=true" not in note, note
    finally:
        s.close()


# ── 第4回c の 3 巡目（2026-09-05）: 撃つ前の観測を、すべての出口が経由する ──────
def _spawn_inside(cwd, port: int):
    """**作業場の中**（この起動と同じディレクトリ）で待ち受けるサーバ＝逃げた候補の的。"""
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    assert _wait_until(lambda: _listening(port), timeout=5)
    return proc


def _sleeper_group(cwd, ignore_sigterm: bool = False):
    """`_stop_group` が実際に撃つ相手（ポートは一度も握らない番人）。

    ★**用意ができるまで待つ**（実測でここを踏んだ）: `python -c` の起動中は
      SIGTERM ハンドラがまだ入っていないので、待たずに撃つと `SIG_IGN` を
      指定した番人まで死ぬ＝試験が「SIGTERM で止まらない」前提を作れない。
    """
    ready = os.path.join(str(cwd), f"ready-{os.getpid()}-{ignore_sigterm}")
    code = ("import signal, time, pathlib\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_sigterm else "")
            + f"pathlib.Path({ready!r}).write_text('1')\n"
            + "time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", code], cwd=str(cwd),
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _wait_until(lambda: os.path.exists(ready), timeout=10), "番人が用意できない"
    return p


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の killpg 経路")
def test_killpgがProcessLookupErrorでも観測を経由する(env, tmp_path, monkeypatch):
    """★再反証 M3（`bash_output.py:441` の枝）。

    `os.killpg(pgid, SIGTERM)` が `ProcessLookupError`（＝撃つ直前にグループが空に
    なっていた）のとき、旧実装は **port を渡されていても見ずに**
    `pid <pgid> already exited` を返していた——グループが空であることは、
    待ち受けが空いたことの証拠ではない。ここは他の出口と同じ観測を経由すること。
    """
    port = _free_port()
    escaped = _spawn_inside(env, port)          # 同じ作業ディレクトリで生き残る待受
    stray = _sleeper_group(env)
    try:
        pgid = os.getpgid(stray.pid)
        monkeypatch.setattr(bo_tool.os, "killpg",
                            lambda pg, sig: (_ for _ in ()).throw(ProcessLookupError()))
        res = bo_tool._stop_group(pgid, scope=str(env), grace=0.4)
        assert not res.startswith("OK:"), res
        assert res.startswith("Error:"), res
        assert "already exited" not in res, res
        assert str(port) in res and str(escaped.pid) in res, res
        assert escaped.poll() is None
    finally:
        # ★差し替えたままだと `_reap` の `os.killpg` が届かず**的が残る**
        #   （試験が自分の残骸を置いていく形。第4回で実際に 7 本残している）。
        monkeypatch.undo()
        _reap(escaped)
        _reap(stray)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の killpg 経路")
def test_止められなかったkillはErrorを名乗る(env, monkeypatch):
    """★再反証 M4。SIGKILL に失敗した出口が **`OK:` で始まって**いた
    （"OK: … まだ残っています（SIGKILL も試しましたが失敗: …）"）。

    この現場で機械が失敗を読む根拠は `Error:` 前置き
    （`laps/tools/__init__.py` の `tool_failed`）で、`OK:` で始めるかどうかでは
    ない。＝**止められなかった停止が「成功」として記録されていた**。
    （2 巡目が根拠にした「OK: で始めない」は誤りで、`Error:` 前置きが正しい。）
    """
    from laps.tools import tool_failed
    stray = _sleeper_group(env, ignore_sigterm=True)     # SIGTERM では死なない
    try:
        pgid = os.getpgid(stray.pid)
        real = bo_tool.os.killpg

        def _killpg(pg, sig):
            if sig == bo_tool.signal.SIGKILL:
                raise PermissionError("試験: SIGKILL を通さない")
            return real(pg, sig)

        monkeypatch.setattr(bo_tool.os, "killpg", _killpg)
        res = bo_tool._stop_group(pgid, grace=0.4)
        assert res.startswith("Error:"), res
        assert tool_failed(res), res
        assert "SIGKILL" in res, res
        assert stray.poll() is None, "前提が崩れている（SIGTERM で死んだ）"
    finally:
        monkeypatch.undo()          # ★SIGKILL を止めたままだと的が片付かない
        _reap(stray)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_観測は推測ではなく現物を見る(env, tmp_path):
    """`_observe_listen` の単体。**グループが実際に握っている LISTEN** だけを
    `ports` に入れ、同じ作業ディレクトリの外の物や、この起動より前から居る物を
    身内に数えない（数えると、正しく止まった停止まで赤くなる）。"""
    port = _free_port()
    inside = _spawn_inside(env, port)
    outside_port = _free_port()
    outside = _spawn_outside(tmp_path, outside_port)
    try:
        pgid = os.getpgid(inside.pid)
        snap = bo_tool._observe_listen(pgid, scope=str(env))
        assert snap["scanned"] is True
        assert port in snap["ports"], snap          # 構成員の実 LISTEN は拾う
        assert outside_port not in snap["ports"], snap
        # 身内: 別グループのリスナは scope の中だけ、しかも起動時刻で絞れる
        other = bo_tool._observe_listen(2_000_000, scope=str(env))
        assert [r["pid"] for r in other["kin"]] == [inside.pid], other["kin"]
        # ★物差しは **tick**（5 巡目 must_fix ①）。旧: `since_epoch=time.time()+3600`。
        #   壁時計は「打ち切りで背景に回された控え」では**打ち切り時刻**を指すので、
        #   関門に使うと本物の逃げ子まで落とす（`exp_rel3` で false green を実測）。
        #   同じ物差し（boot からの tick）同士でだけ「前から居た」を証明する。
        future = (bo_tool._uptime_ticks() or 0) + 10_000
        old = bo_tool._observe_listen(2_000_000, scope=str(env), since_ticks=future)
        assert old["kin"] == [], "この起動より前から居る物を身内に数えている"
        assert old["no_ruler"] is False and old["kept_unproven"] == 0, old
        # 物差しが無いときは**落とさない**（fail-open）。緩めた事実は印で出す。
        vague = bo_tool._observe_listen(2_000_000, scope=str(env), since_ticks=None)
        assert [r["pid"] for r in vague["kin"]] == [inside.pid], vague["kin"]
        assert vague["no_ruler"] is True and vague["kept_unproven"] == 1, vague
        far = bo_tool._observe_listen(2_000_000, scope=str(tmp_path / "outside_target"))
        assert [r["pid"] for r in far["kin"]] == [outside.pid], far["kin"]
    finally:
        _reap(inside)
        _reap(outside)


# ── 時計が読めないときに**黙って素通りしない**（2026-09-05・5 巡目・調停係の指摘）──
#
# ★実際に起きたこと（lifecycle 6a37b3b × merge-bash 33fb3b2 の union）:
#   `procutil._boot_time` が `_boot_epoch` に改名され、こちら側は
#   `except Exception` で受けていたので **AttributeError が「時刻が読めない」に化け、
#   起動時刻の関門が黙って全部素通り**した。試験 1 本だけが偶然それを捕まえた。
#   `/proc` が読めない（OSError）は「分からない」だが、**名前の取り違えは欠陥**。
#   飲まずに上げる＝壊れていることが分かる形にしておく。


def test_時計の関数を取り違えたら黙って素通りしない(monkeypatch):
    """`AttributeError`（＝コードの取り違え）を握り潰さないことを 3 か所で固定する。

    ★union-2（2026-09-06）: 正典は `_boot_time` ではなく `_boot_epoch`
      （merge-bash 側の改名が union の正典）。lifecycle 単独枝は改名前の
      `_boot_time` を消していたが、それは union には存在しない名前——
      `delattr` 自体が `AttributeError` になり、この試験がまさに検出したい
      「取り違えを握り潰す」経路を一度も通らずに落ちていた。消す対象を
      現行名へ直す（同じ穴が union のたびに再発する型なので直しても消えない）。
    """
    monkeypatch.delattr(procutil, "_boot_epoch")
    with pytest.raises(AttributeError):
        bo_tool._launch_epoch({"start_ticks": 12345}, None)
    monkeypatch.delattr(procutil, "_clock_ticks")
    with pytest.raises(AttributeError):
        bo_tool._uptime_ticks()
    monkeypatch.delattr(procutil, "proc_start_ticks")
    with pytest.raises(AttributeError):
        bo_tool._born_after_ticks(os.getpid(), 1)
    with pytest.raises(AttributeError):
        bo_tool._started_after(os.getpid(), 1)


def test_procが読めないのは分からないとして扱う(monkeypatch):
    """逆側: **本当に読めない**（OSError）ときは落ちずに「分からない」を返す。
    ここを一緒に上げてしまうと、`/proc` が無い機械で道具ごと使えなくなる。"""
    def _boom(_pid):
        raise OSError("no /proc")
    monkeypatch.setattr(procutil, "proc_start_ticks", _boom)
    assert bo_tool._born_after_ticks(os.getpid(), 1) is None
    assert bo_tool._started_after(os.getpid(), 1) is None


# ── 観測そのものが出来なかったときに「残っていない」と言わない ─────────────
# （2026-09-05・7 巡目。6 巡目 must_fix ① と**同じ形が第 1 の門に残っていた**）
#
# ★本番の形で実測した 2 つの false green（`scratchpad/r4d/wf-lifecycle-fix1/
#   probe_blind.py`・`Bash` → `BashOutput(pid=…, kill=true)`）:
#     rows   `procutil.proc_rows` が AttributeError（union の改名事故と同型）
#            → `pid N already exited`（`Error:` で始まらない＝機械は**成功**と読む）。
#              構成員は生きたまま、**撃ってすらいない**。
#     listen `/proc/net/tcp{,6}` が読めない
#            → `OK: SIGTERM で止まりました`。逃げたサーバは生きて port を握ったまま。
#   どちらも「観測できなかった」を「何も無かった」に読み替えた形で、この係が
#   2〜6 巡目で潰してきた false green の最後の顔。


def _blind_listen_table(monkeypatch):
    """`/proc/net/tcp{,6}` が読めない機械を真似る（読めなかった事実は申告する）。"""
    def _blind(errors=None):
        if errors is not None:
            errors.append("LISTEN の表が 1 つも読めません（試験: PermissionError）")
        return {}
    monkeypatch.setattr(bo_tool, "_listen_inode_ports", _blind)


def test_LISTENの表を読む定数が引けないなら黙って0件にしない(monkeypatch):
    """union の `_boot_time`→`_boot_epoch` と同型。**飲まずに申告する**。"""
    monkeypatch.delattr(procutil, "_ST_LISTEN")
    errs: list = []
    assert bo_tool._listen_inode_ports(errs) == {}
    assert errs and "定数" in errs[0], errs


def test_観測が盲なら観測できたと言わない(env, monkeypatch):
    """`snap["scanned"]` は「試みた」ではなく「**通った**」の意味。

    ★6 巡目までこの欄は無条件 True で、しかも**本番のどの判断にも使われて
      いなかった**（書いたが繋いでいない欄）。いま `blind` として繋いである。
    """
    _blind_listen_table(monkeypatch)
    snap = bo_tool._observe_listen(2_000_000, scope=str(env))
    assert snap["scanned"] is False, snap
    assert snap["blind"], snap

    monkeypatch.undo()
    def _boom(**kw):
        raise AttributeError("module 'procutil' has no attribute 'proc_rows'")
    monkeypatch.setattr(procutil, "proc_rows", _boom)
    snap2 = bo_tool._observe_listen(2_000_000, scope=str(env))
    assert snap2["scanned"] is False and snap2["blind"], snap2
    assert "proc_rows" in snap2["blind"][0], snap2["blind"]


def test_観測が盲なら止まったと言わない(env, monkeypatch):
    """実プロセスを撃つ本番経路（`_stop_group`）。**撃つのはやめないが、
    止まったとは言わない**——観測が出来ていないなら、見つからないのは当たり前。"""
    from laps.tools import tool_failed
    guard = _sleeper_group(env)
    try:
        pgid = os.getpgid(guard.pid)
        _blind_listen_table(monkeypatch)
        res = bo_tool._stop_group(pgid, grace=1.0)
        assert not res.startswith("OK"), res
        assert tool_failed(res), res            # 機械が失敗と読む根拠は Error: 前置き
        assert "確かめられていません" in res, res
        assert "LISTEN の表" in res, res
        # 撃つのはやめていない（沈黙ではなく、正直な断り）
        assert _wait_until(lambda: not bo_tool._group_members(pgid), timeout=5), res
    finally:
        _reap(guard)


def test_構成員を数えられないならもう終わったと言わない(env, monkeypatch):
    """`_group_members` の `except → []` は「数えられない」を「居ない」に丸めていた。"""
    from laps.tools import tool_failed
    def _boom(_pgid):
        raise AttributeError("module 'procutil' has no attribute 'group_members'")
    monkeypatch.setattr(procutil, "group_members", _boom)
    errs: list = []
    assert bo_tool._group_members(2_000_000, errs) == []
    assert errs and "group_members" in errs[0], errs
    res = bo_tool._stop_group(2_000_000, grace=0.2)
    assert tool_failed(res), res
    assert "もう終わっているようです" not in res, res
    assert "確かめられていません" in res, res


def test_数えられないだけの背景処理をalready_exitedと言わない(env, monkeypatch):
    """本番の形（`Bash` が控えを書く → `BashOutput(pid=…, kill=true)`）。

    ★実測（`probe_blind.py` の `rows` 形・base 10147fd）: `procutil.proc_rows` を
      壊すと `alive` が False になり、**撃たずに** `pid N already exited` を返した
      （`tool_failed()` は False＝機械は成功と読む）。`sleep` は生きたまま。
    """
    from laps.tools import tool_failed
    (env / "start.sh").write_text("#!/bin/sh\nsleep 300 &\nexit 0\n")
    out = bash_tool._run({"command": "bash start.sh", "cwd": str(env), "timeout": 10})
    out = out if isinstance(out, str) else out.text
    import re
    m = re.search(r"BashOutput\(pid=(\d+), kill=true\)", out)
    assert m, out                                   # 前提: 控えが出ている
    pgid = int(m.group(1))
    try:
        def _boom(_pgid):
            raise AttributeError("module 'procutil' has no attribute 'group_members'")
        monkeypatch.setattr(procutil, "group_members", _boom)
        res = bo_tool._run({"pid": pgid, "kill": True})
        res = res if isinstance(res, str) else res.text
        assert "already exited" not in res, res
        assert tool_failed(res), res
        assert "撃っていません" in res, res
    finally:
        try:
            os.killpg(pgid, 9)
        except OSError:
            pass


# ── 政策の門そのものが壊れたら撃たない（2026-09-05・7 巡目）─────────────
#
# ★この門は「`BashOutput(port=…, kill=true)` が政策を通らずに撃っていた」
#   （第4回b の反証・実測）を塞ぐために新設された物なのに、その中身が
#   「政策層が読めない＝従来どおり通す」「政策の判定が例外＝通す」だった。
#   `bash.py:18` は `from ..policy import authorize_command` を module 直下で
#   やるので、同じ行為の 2 つの入口のうち **Bash 側は落ち、こちら側は黙って撃つ**。


def test_政策層を読めないなら撃たない(monkeypatch):
    """`from ..policy import authorize_command` が失敗する形（改名・欠損）。"""
    from laps import policy
    monkeypatch.delattr(policy, "authorize_command")
    res = bo_tool._authorize_kill("kill -TERM 999999", None)
    assert res and res.startswith("Error:"), res
    assert "撃っていません" in res, res


def test_政策の判定が例外なら撃たない(env, monkeypatch):
    """実プロセスで見る（文言ではなく**生きていること**で）。"""
    from laps import policy
    from laps.tools import tool_failed
    guard = _sleeper_group(env)
    try:
        pgid = os.getpgid(guard.pid)

        def _boom(command, cwd=None):
            raise RuntimeError("試験: 政策の判定が壊れた")
        monkeypatch.setattr(policy, "authorize_command", _boom)
        res = bo_tool._authorize_kill(f"kill -TERM {pgid}", str(env))
        assert res and tool_failed(res), res
        assert "政策の判定が失敗" in res, res
        assert guard.poll() is None, "政策が壊れているのに撃った"
    finally:
        _reap(guard)


# ── 走行ごと消える床を、procutil 1 本に乗せない（2026-09-05・7 巡目）─────────
#
# ★実測（`scratchpad/r4d/wf-lifecycle-fix1/probe_selffloor.py`・**別セッション**で走らせた）:
#     base 10147fd  self_pgid=3738832 → **exit 143（自分のグループを SIGTERM した）**
#     この版        result=Error: …**このエージェント自身**… / survived=True
#   `procutil.ancestor_pgids` が壊れると（union で実際に起きた改名事故と同型）、
#   `except Exception: pass` が床を**黙って**消していた。撃つと走行ごと消える唯一の
#   防波堤なので、procutil に依らない照合（`os.getpgid(0)`）も置き、
#   祖先の照合が壊れたら**撃たない**。


def test_自分のグループを撃つ床はprocutil一本に乗っていない():
    """⚠ **子プロセス（新しいセッション）で試す**——床が壊れていれば、
    この試験を走らせているプロセス自身が死ぬ形なので、親から切り離して測る。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(bo_tool.__file__)))
    code = ("import os,sys\n"
            f"sys.path.insert(0,{root!r})\n"
            "os.environ['LAPS_POLICY']='trusted'\n"
            "from laps import procutil\n"
            "from laps.tools import bash_output as bo\n"
            "def _boom():\n"
            "    raise AttributeError('no ancestor_pgids')\n"
            "procutil.ancestor_pgids=_boom\n"
            "mine=os.getpgid(0)\n"
            "print(bo._stop_group(mine, grace=0.2).splitlines()[0])\n"
            "print('survived', os.getpgid(0)==mine)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       start_new_session=True, timeout=60)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    assert r.returncode == 0, (r.returncode, out, err[:400])   # 143 = 自分を撃った
    assert "自身" in out, out
    assert "survived True" in out, out


def test_祖先かどうかを確かめられないなら撃たない(env, monkeypatch):
    """相手が他人（作業場の番人）でも、**自分でないと確かめられない**なら撃たない。"""
    from laps.tools import tool_failed
    guard = _sleeper_group(env)
    try:
        pgid = os.getpgid(guard.pid)

        def _boom():
            raise AttributeError("module 'procutil' has no attribute 'ancestor_pgids'")
        monkeypatch.setattr(procutil, "ancestor_pgids", _boom)
        res = bo_tool._stop_group(pgid, grace=0.2)
        assert tool_failed(res), res
        assert "確かめられません" in res, res
        assert guard.poll() is None, "自分か祖先か分からないのに撃った"
    finally:
        _reap(guard)


def test_他ユーザのプロセスを死んだことにしない(monkeypatch):
    """`os.kill(pid, 0)` の **EPERM は「居る」**（別ユーザ）。

    ★旧 `except (OSError, ProcessLookupError): return False` は EPERM も
      「居ない」に倒していた。`_survivors` は生き残りを 1 本落とし、`_run` は
      `already exited` と言う——この係が潰している「見えない＝無い」の同型。
    ⚠ この機械では root なので EPERM を実プロセスで作れない（root は誰にでも
      信号を送れる）。**その 1 行だけ**を差し替えて見る。
    """
    def _eperm(_pid, _sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(bo_tool.os, "kill", _eperm)
    assert bo_tool._is_alive(999_999) is True

    def _gone(_pid, _sig):
        raise ProcessLookupError(3, "No such process")
    monkeypatch.setattr(bo_tool.os, "kill", _gone)
    assert bo_tool._is_alive(999_999) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_他ユーザの逃げ子の待受を待受なしに丸めない(env, monkeypatch):
    """★8 巡目・再反証 M1（**本番経路で false green を実測**）。

    `_pid_listen_ports` の docstring は 7 巡目にこう書いていた——
    「読めないなら空集合。**呼ぶ側は空集合を『待受なし』ではなく『観測できなかった』
    として扱うこと**」。ところが呼ぶ側 2 か所（`_survivors` /
    `_observe_listen` の構成員側）はどちらも空集合を「待受なし」に丸めており、
    `blind` に何も積まれないので `_after_kill` の `if blind:` は発火せず
    `_ok_text` に落ちていた＝**散文だけ正しく見える形**。

    ここでは 2 つを同時に見る:
      ① `_is_alive` は EPERM を「居る」と読む（7 巡目の直し E が効いている証拠）。
      ② それでも `_stop_group` は `OK:` を出さない（`tool_failed()` が True）。
    ①だけ直っていた 7 巡目は、②が `OK: SIGTERM で止まりました` だった。

    ⚠ この機械は root なので EPERM を実プロセスで作れない（root は誰にでも
      信号を送れるし、どの `/proc/<pid>/fd` も読める）。**その 2 か所だけ**を
      差し替えて、他ユーザのプロセスに見えるようにする。
    """
    from laps.tools import tool_failed
    port = _free_port()
    escaped = _spawn_inside(env, port)          # 作業場の中で生き残る逃げ子
    stray = _sleeper_group(env)                 # 実際に撃たれるグループ
    try:
        pgid = os.getpgid(stray.pid)
        esc = escaped.pid
        real_listdir, real_kill = os.listdir, os.kill
        deny = f"/proc/{esc}/fd"

        def _listdir(path, *a, **k):
            if str(path) == deny:
                raise PermissionError(13, "Permission denied")
            return real_listdir(path, *a, **k)

        def _kill(pid, sig, *a, **k):
            if pid == esc:
                raise PermissionError(1, "Operation not permitted")
            return real_kill(pid, sig, *a, **k)

        monkeypatch.setattr(bo_tool.os, "listdir", _listdir)
        monkeypatch.setattr(bo_tool.os, "kill", _kill)
        assert bo_tool._is_alive(esc) is True, "E の経路（EPERM＝居る）を通っていない"
        res = bo_tool._stop_group(pgid, scope=str(env), grace=0.4)
        assert res.startswith("Error:") and tool_failed(res), res
        assert "確かめられていません" in res, res
        assert "読めません" in res, f"読めなかった事実が本文に出ていない: {res}"
        assert "止まりました" not in res, f"止まったと言っている: {res}"
    finally:
        monkeypatch.undo()          # ★差し替えたままだと片付けの kill も届かない
        _reap(escaped)
        _reap(stray)


def test_本当に死んだ相手は盲にしない():
    """逆側（**偽の赤を作らない**）。`ProcessLookupError` / `FileNotFoundError` は
    「読めなかった」ではなく「居ない」なので `errors` に積まない——積むと、
    正しく止まった停止まで「確かめられていません」になる。"""
    errs: list = []
    imap = {"999999999": 65000}
    assert bo_tool._pid_listen_ports(2_000_000_000, imap, errs) == set()
    assert errs == [], errs                  # 死んだ pid＝盲にしない
    assert bo_tool._pid_listen_ports(os.getpid(), {}, errs) == set()
    assert errs == [], errs                  # imap が空＝呼ぶ前に盲を積んである


def test_身内を広げる側の失敗は盲にしない(env, monkeypatch):
    """`ancestor_pgids` / `bg_records` が落ちても `blind` にはしない。

    ★区別の理由: この 2 つが落ちると身内の候補は**増える**（自分の祖先や他の
      背景処理を除けなくなる）＝出るのは**偽の赤**で、生き残りを隠す側ではない。
      すべてを `blind` に積むと、正しく止まった停止まで
      「確かめられていません」になり、断り文の意味が薄れる。
      **数（`bg_observe_degraded`）としては残す。**
    """
    seen: list = []
    orig = bo_tool._bump
    monkeypatch.setattr(bo_tool, "_bump", lambda n, k=1: (seen.append(n), orig(n, k))[1])

    def _boom(*a, **k):
        raise AttributeError("試験: 壊した")
    monkeypatch.setattr(procutil, "bg_records", _boom)
    monkeypatch.setattr(procutil, "ancestor_pgids", _boom)
    snap = bo_tool._observe_listen(2_000_000, scope=str(env))
    assert snap["blind"] == [], snap          # 盲にはしない
    assert snap["scanned"] is True, snap
    assert "bg_observe_degraded" in seen, seen


# ── 二次（union-2・2026-09-06）: 断り文でも保護ポートの事実を出す ──────────
def test_escaped_noteは保護ポートの事実を消さない(env, tmp_path, monkeypatch):
    """★`_escaped_note`（グループは片付いたがポートが残っている）は
    保護ポートかどうかを一度も言っていなかった——保護ポートの持ち主が
    たまたま生き残った形でも「まだ使われています」としか出ない。"""
    port = _free_port()
    third = _spawn_outside(tmp_path, port)
    try:
        monkeypatch.setenv("LAPS_PROTECTED_PORTS", str(port))
        note = bo_tool._escaped_note(2_000_000, port)
        assert note, "待受が残っているのに何も言わなかった"
        assert "保護" in note, f"保護ポートの事実が本文から消えている: {note}"
    finally:
        monkeypatch.delenv("LAPS_PROTECTED_PORTS", raising=False)
        _reap(third)


def test_survivor_noteは保護ポートの事実を消さない(env, tmp_path, monkeypatch):
    """★`_survivor_note`（生き残りの一覧）も同様。"""
    port = _free_port()
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", str(port))
    try:
        row = {"pid": 999999, "comm": "php", "cmdline": f"php -S 0.0.0.0:{port}",
              "cwd": str(env), "kinship": "scope", "ports": [port]}
        note = bo_tool._survivor_note(2_000_000, [row], how="SIGTERM で片付きました")
        assert note.startswith("Error:"), note
        assert "保護" in note, f"保護ポートの事実が本文から消えている: {note}"
    finally:
        monkeypatch.delenv("LAPS_PROTECTED_PORTS", raising=False)
