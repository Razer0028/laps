"""`BashOutput(pid=…, kill=true)` の**所有権の関門**（第4回c・2026-09-05）。

★塞いだ穴（第4回b の反証④「書いたが繋いでいない」・実測で再現済み）:
  PID 使い回しの関門 `procutil.bg_record_matches` / `owned_bg_pids` は
  `_port_report` と `bash._scope_name_kill` からしか呼ばれておらず、
  **いちばん撃つ経路である `BashOutput(pid=…, kill=true)` には配線されていなかった**。
  作業場の外（cwd=/tmp）の無関係なプロセスに、同じ番号の古い控え
  （`start_ticks` 不一致）を置いて呼ぶと、`owned_bg_pids()` は `set()`・
  `bg_record_matches` は False と**正しく弾いているのに**、この経路は
  `OK: SIGTERM で止まりました` を返して**その無関係のプロセスを殺した**
  （この機械の台帳には現に 23 件の死んだ控えが残っている）。

ここでは作り物ではなく**実際のプロセス**を立て、返り値の文言と**生存**の両方を見る。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from laps import procutil
from laps.tools import bash_output as bo_tool


def _sleeper(cwd: str):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            cwd=cwd, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _free_port() -> int:
    """**この機械のいまの状態に依存しない**ポートを取る（2026-09-05・2 巡目 must_fix ③）。

    ★直した欠陥: `_plant_record` の cmd に `0.0.0.0:18300` が**焼き込まれていた**。
      `_stop_group` は控えの cmd から読んだポートで `port_in_use` を見るので、
      **この機械で 18300 に誰かが居るかどうかで試験の合否が変わる**
      （18300 は run5x の作業場が自分で建てるサーバのポートで、実際によく埋まる。
      しかも `_escaped_note` の直しで「埋まっていたら OK と言わない」を入れた以上、
      埋まっている日はこの 2 本が理由もなく落ちる形になっていた）。
      試験が自分で確保したポートを使う。
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _plant_record(pid: int, *, cwd: str, start_ticks, mtime: float | None = None,
                  port: int | None = None, workspace: str | None = None):
    """★`workspace` を必ず書く（2026-09-05・統合）。

    台帳は `LAPS_HOME` を共有する**全セッションの物**なので、
    `procutil.bg_record_workspace_ok` は `workspace` の無い記録を**通さない**
    （fail-closed）。本番の `bash._spawn_background` は
    `procutil.canonical_workspace()` を書き込むので、控えを作り物で置くここも
    同じ形にする。書かないと `owned_bg_pids()` が常に空になり、
    「自分が起こした物すら止められない」形を試験しているだけになる。
    """
    d = procutil.bg_dir(create=True)
    meta = d / f"{pid}.meta.json"
    body = {"pid": pid, "pgid": pid,
            "cmd": f"php -S 0.0.0.0:{port if port is not None else _free_port()} -t site/",
            "cwd": cwd, "started_at": "2026-08-06T00:00:00",
            "workspace": workspace or procutil.canonical_workspace()}
    if start_ticks is not None:
        body["start_ticks"] = start_ticks
    meta.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    (d / f"{pid}.out").write_bytes(b"")
    (d / f"{pid}.err").write_bytes(b"")
    if mtime is not None:
        os.utime(meta, (mtime, mtime))
    return meta


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    return ws


def _text(out) -> str:
    return out if isinstance(out, str) else out.text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_番号を使い回した別人は撃たない(env, tmp_path):
    """古い控えと同じ番号を引き当てた**無関係のプロセス**は殺さない。
    通行証（`bg_record_matches`）が塞がっているだけでなく、**扉も閉じている**こと。"""
    victim = _sleeper("/tmp")
    try:
        _plant_record(victim.pid, cwd="/root/vdirwork/run50",
                      start_ticks=12345, mtime=1754400000.0)
        # 前提: procutil 側は正しく「別人」と言っている
        assert procutil.owned_bg_pids() == set()
        res = _text(bo_tool._run({"pid": victim.pid, "kill": True}))
        assert res.startswith("Error:"), res
        assert "別人" in res and "撃っていません" in res, res
        time.sleep(0.4)
        assert victim.poll() is None, "無関係のプロセスを殺した"
    finally:
        try:
            os.killpg(os.getpgid(victim.pid), 9)
        except OSError:
            pass
        victim.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_古い形式の控えで作業場の外のプロセスは撃たない(env, tmp_path):
    """`start_ticks` を持たない**古い形式の控え**（この機械に現に 23 件ある形）は
    開始時刻で照合できない。通行証にならないので撃たない。

    ★`bg_record_matches` は「番号は生きているが控えの mtime とかけ離れている」を
      False にするので、この控えは通行証にならない。関門が 2 段ある意味はここ。

    ★★**この試験は 2026-09-05 の統合まで「偽の緑」だった**（統合で露見）。
      旧版は `"作業場の外" in res` を見ていたが、返っていたのは実際には
      **「別人」**の断り文で、そこに `作業場の外` という語は無い。にもかかわらず
      緑だったのは、**pytest の `tmp_path` に試験名がそのまま入る**ため
      （`/tmp/pytest-of-root/pytest-N/test_古い形式の控えで作業場の外のプロセスは
      撃たない0/outside`）——断り文が現物の cwd を載せるので、
      **試験が自分の名前を読んで合格していた**。
      いま見るのは (a) 断っていること (b) 断りの**理由**（照合できない＝別人／
      PID の使い回し） (c) **相手が生きていること**の 3 つ。
      パスの綴りに依存しないよう、理由の語は明示して確かめる。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _sleeper(str(outside))
    try:
        _plant_record(victim.pid, cwd=str(outside),
                      start_ticks=None, mtime=1754400000.0)   # 8/6 の古い控え
        assert procutil.owned_bg_pids() == set()
        res = _text(bo_tool._run({"pid": victim.pid, "kill": True}))
        assert res.startswith("Error:"), res
        assert "撃っていません" in res, res
        assert "別人" in res or "作業場の外" in res.split("見た cwd")[0], res
        time.sleep(0.4)
        assert victim.poll() is None, "作業場の外のプロセスを殺した"
    finally:
        try:
            os.killpg(os.getpgid(victim.pid), 9)
        except OSError:
            pass
        victim.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_作業場の外でも自分が起こしたと証明できるものは止められる(env, tmp_path):
    """★ここは反証の must_fix から**意図して外した**（第4回c・理由を残す）。

    反証は「meta の cwd が作業場の外なら撃たない」を求めたが、控えの
    `start_ticks` が現物と一致するということは、**その番号は使い回しではなく、
    自分が起こしたその処理そのもの**だという強い証拠になる（PID 再利用では
    絶対に一致しない）。それを撃てなくすると、`trusted` で作業場の外に起こした
    自分の背景処理を止める手段が消える＝同じ反証の別の指摘
    「起動できるが停止できない」を作り直すことになる。
    判定は `procutil.in_workspace`（① 控えが一致 ② cwd が作業場配下）の 1 本に
    委ね、`_port_report` と同じ規律にそろえた。ここはその**意図した通り道**を固定する。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _sleeper(str(outside))
    port = _free_port()          # ★機械の状態に依存しない（旧: 18300 焼き込み）
    try:
        assert not procutil.port_in_use(port), "前提: この試験が使うポートは空いている"
        _plant_record(victim.pid, cwd=str(outside), port=port,
                      start_ticks=procutil.proc_start_ticks(victim.pid))
        assert procutil.owned_bg_pids() == {victim.pid}
        res = _text(bo_tool._run({"pid": victim.pid, "kill": True}))
        assert res.startswith("OK:"), res
        victim.wait(timeout=5)
    finally:
        try:
            os.killpg(os.getpgid(victim.pid), 9)
        except OSError:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_自分が起こした作業場の中の処理は今までどおり止まる(env):
    """★逆側（門が仕事を殺していないこと）。控えの開始時刻が一致し、cwd が
    作業場の中なら、関門は素通りして今までどおり止まる。"""
    victim = _sleeper(str(env))
    port = _free_port()          # ★機械の状態に依存しない（旧: 18300 焼き込み）
    try:
        assert not procutil.port_in_use(port), "前提: この試験が使うポートは空いている"
        _plant_record(victim.pid, cwd=str(env), port=port,
                      start_ticks=procutil.proc_start_ticks(victim.pid))
        assert procutil.owned_bg_pids() == {victim.pid}
        res = _text(bo_tool._run({"pid": victim.pid, "kill": True}))
        assert res.startswith("OK:"), res
        victim.wait(timeout=5)
        assert victim.poll() is not None
    finally:
        try:
            os.killpg(os.getpgid(victim.pid), 9)
        except OSError:
            pass


# ── 第4回c の 5 巡目（2026-09-05・must_fix ③）: **構成員 1 本を通行証にしない** ──
#
# ★4 巡目の穴（`exp_own` が実物で再現・3 形とも）: 手掛かり（台帳の申告・長の現物・
#   構成員の現物）を全部並べて「**どれか 1 つでも作業場の中なら通す**」にしたので、
#   **他人のプロセスグループに作業場の中の構成員が 1 本混じっているだけで通った**。
#   長が死んだ他人のグループ（構成員は `/tmp` と作業場に 1 本ずつ）に古い控えを置くと、
#   台帳の cwd が「作業場／`/tmp`／無し」のどれでも `OK: SIGTERM で止まりました` を
#   返し、**他人のグループを実際に殺した**（陰性対照 cb1a54e は 3 形とも断っていた）。
# いま通すのは ①控えがこのグループの物だと証明できた ②長の現物が中
#   ③構成員が**全員**中 のどれか。


def _foreign_group(ws: str):
    """他人のグループ: 長は即死・構成員 2 本（1 本は作業場の中／1 本は `/tmp`）。"""
    p = subprocess.Popen(
        ["setsid", "sh", "-c", f"cd /tmp; sleep 60 & cd {ws}; sleep 60 &"],
        cwd="/tmp", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait(timeout=5)
    time.sleep(0.4)
    return p.pid, procutil.group_members(p.pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
@pytest.mark.parametrize("ledger_cwd", ["SELF_WS", "/tmp", None],
                         ids=["台帳が作業場", "台帳が外", "台帳に cwd 無し"])
def test_構成員が一本中に居るだけの他人のグループは撃たない(env, ledger_cwd):
    """★must_fix ③。3 形とも `exp_own` が実物で再現した（HEAD は 3/3 で殺した）。"""
    pgid, members = _foreign_group(str(env))
    assert len(members) == 2, f"前提が崩れている: {members}"
    assert any(str(m.get("cwd") or "").startswith(str(env)) for m in members), members
    assert any(not str(m.get("cwd") or "").startswith(str(env)) for m in members), members
    try:
        _plant_record(pgid, cwd=(str(env) if ledger_cwd == "SELF_WS" else ledger_cwd),
                      start_ticks=None, mtime=1754400000.0)   # 8/6 の古い控え
        assert procutil.owned_bg_pids() == set(), "前提: 通行証は出ていない"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:"), res
        assert "撃っていません" in res, res
        time.sleep(0.4)
        left = procutil.group_members(pgid)
        assert len(left) == 2, f"他人のグループを撃った（{len(left)} 本に減った）: {res}"
    finally:
        for m in members:
            try:
                os.kill(m["pid"], 9)
            except OSError:
                pass


def _foreign_group_all_outside():
    """他人のグループ: 長は即死・構成員 2 本とも `/tmp`（＝作業場の外）。

    ★`_foreign_group` は「1 本だけ中」の形。ここは**全部外**の形＝
      `_ownership_note` の ③（構成員が全員中）も ②（長の現物）も使えず、
      ①（控えがこのグループの物だという証明）だけが残る窓
      （6 巡目 must_fix ②で名指しされた「唯一の生きた窓」）。
    """
    p = subprocess.Popen(
        ["setsid", "sh", "-c", "cd /tmp; sleep 60 & sleep 60 &"],
        cwd="/tmp", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait(timeout=5)
    time.sleep(0.4)
    return p.pid, procutil.group_members(p.pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
@pytest.mark.parametrize("shape", ["構成員の欄が無い", "控えより後に生まれた構成員",
                                   "別のグループの構成員"])
def test_長が死に構成員が全員外なら所有権の門だけが止める(env, shape):
    """★6 巡目 must_fix ②（a）: `_ownership_note` が**唯一の門**になる**陽性**試験。

    ここは先行の門をどれも通ってしまう形である:
      ・政策は `trusted`（`_authorize_kill` は通す）
      ・長はもう居ないので `_run` の `alive` は**構成員**で True になる
      ・`_stop_group` はグループ番号を渡されたら撃つだけ（持ち主を見ない）
    つまりこの 3 形で撃たれないなら、止めているのは `_ownership_note` しかない
    （実測: この関数を `return None` に潰すと 3 形とも他人のグループが死ぬ）。

    5 巡目まではここに**誤って断らないことしか見ていない**試験（`lifecycle.py:1099`）
    しか無く、断る側が固定されていなかった。
    ★併せて 6 巡目の緩め（`procutil.bg_group_matches`＝長が死んでも構成員で
      通行証を出す）が、この扉を開けていないことも見る——控えの `members` が
      「控えより後に生まれた」「別のグループ」なら証明にならない。
    """
    pgid, members = _foreign_group_all_outside()
    assert len(members) == 2, f"前提が崩れている: {members}"
    assert all(not str(m.get("cwd") or "").startswith(str(env)) for m in members), \
        f"前提が崩れている（作業場の中に居る構成員がある）: {members}"
    other = _sleeper("/tmp")                  # 「別のグループ」用の生きた pid
    try:
        meta_p = _plant_record(pgid, cwd=None, start_ticks=None, mtime=1754400000.0)
        body = json.loads(meta_p.read_text(encoding="utf-8"))   # started_at は 8/6
        if shape == "控えより後に生まれた構成員":
            body["members"] = [[m["pid"], m["cmdline"]] for m in members]
        elif shape == "別のグループの構成員":
            body["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            body["members"] = [[other.pid, "python"]]
        meta_p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        assert procutil.owned_bg_pids() == set(), "前提: 通行証は出ていない"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert res.startswith("Error:") and "撃っていません" in res, res
        assert "作業場の外" in res, res
        time.sleep(0.4)
        left = procutil.group_members(pgid)
        assert len(left) == 2, f"他人のグループを撃った（{len(left)} 本に減った）: {res}"
        assert other.poll() is None, "控えに名前を借りただけの第三者を殺した"
    finally:
        for m in members:
            try:
                os.kill(m["pid"], 9)
            except OSError:
                pass
        try:
            os.killpg(os.getpgid(other.pid), 9)
        except OSError:
            pass
        other.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_構成員が一本外へcdした自分の背景処理は止められる(env, tmp_path, monkeypatch):
    """★逆側（狭めた門が仕事を殺していないこと・4 巡目 must_fix ④の退行防止）。

    `bash start.sh` の中身が `cd /tmp && setsid …` だと、長（シェル）が先に死んで
    構成員の 1 本が `/tmp` に居るサブシェルになる。それでも**控えが自分の物だと
    証明できる**（`bash._release_record` は控えにそのときの構成員を書いており、
    pid・命令・生まれた時刻が一致する）ので通す——ここが ① の通り道。
    """
    monkeypatch.chdir(env)
    from laps.tools import bash as bash_tool
    # ★逃げ子に**目印**を付ける（6 巡目）。`setsid` はグループを出るので
    #   `finally` の `killpg(pgid)` では回収できず、この試験は走るたびに
    #   `sleep 300`（ppid=1・cwd=/tmp）を共有ホストに 1 本ずつ残していた
    #   （実測で拾って回収した）。素の `sleep 300` を pid で探すと**他人の
    #   `sleep 300` まで撃つ**ので、argv に一意の目印を入れて自分の物だけ撃つ。
    tag = f"bwtag-{os.getpid()}-{int(time.time()*1000)}"
    (env / "start.sh").write_text(
        "#!/bin/sh\n"
        f"cd /tmp && setsid {sys.executable} -c 'import time; time.sleep(300)' {tag} &\n"
        "sleep 300 &\necho started\n", encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh", "timeout": 1}))
    import re
    m = re.search(r"BashOutput\(pid=(\d+), kill=true\)", started)
    assert m, started
    pgid = int(m.group(1))
    try:
        members = procutil.group_members(pgid)
        assert any(not str(x.get("cwd") or "").startswith(str(env)) for x in members), \
            f"前提が崩れている（外へ出た構成員が居ない）: {members}"
        meta_p = procutil.bg_dir() / f"{pgid}.meta.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        assert bo_tool._released_record_proves(meta, meta_p, members) is True, meta
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert "作業場の外**にいます" not in res, f"所有権の門が誤って断った: {res}"
        assert not procutil.group_members(pgid), res
    finally:
        try:
            os.killpg(pgid, 9)
        except OSError:
            pass
        _reap_tagged(tag)                 # ★グループを出た逃げ子（目印で自分の物だけ）


def _reap_tagged(tag: str) -> int:
    """argv に `tag` を持つプロセスだけを回収する（共有ホストで他人を撃たないため）。"""
    n = 0
    for r in procutil.proc_rows(skip_self=False):
        if tag in (r.get("argv") or ()):
            try:
                os.kill(r["pid"], 9)
                n += 1
            except OSError:
                pass
    return n


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_構成員が全員外へ出た自分の背景処理も証明できれば止められる(env, monkeypatch):
    """★6 巡目 must_fix ④ の**本番側の効き目**（実測で挙動が変わった 1 形）。

    `#!/bin/sh` の中身が `cd /tmp` で始まると、長（シェル）が先に死んだあと
    構成員が**全員**作業場の外に居る。5 巡目はここで
    `Error: pid N は**作業場の外**にいます`＝**自分が起こした背景処理を
    自分で止められない**（実測: 構成員 2 本 → 2 本のまま生存）。
    `bg_group_matches` を `owned_bg_pids` に繋いだので、控えが自分の物だと
    証明できるこの形は通る（実測: 2 本 → 0 本）。

    ⚠ 緩めた側なので、**他人**の同じ形は
      `test_長が死に構成員が全員外なら所有権の門だけが止める`（3 形）が押さえる。
    """
    monkeypatch.chdir(env)
    from laps.tools import bash as bash_tool
    import re
    (env / "start.sh").write_text(
        "#!/bin/sh\ncd /tmp\nsleep 300 &\nsleep 300 &\nexit 0\n", encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh", "timeout": 2}))
    m = re.search(r"BashOutput\(pid=(\d+), kill=true\)", started)
    assert m, started
    pgid = int(m.group(1))
    try:
        members = procutil.group_members(pgid)
        assert len(members) == 2, f"前提が崩れている: {members}"
        assert all(not str(x.get("cwd") or "").startswith(str(env)) for x in members), \
            f"前提が崩れている（作業場の中に居る構成員がある）: {members}"
        assert pgid in procutil.owned_bg_pids(), "長が死んだ控えに通行証が出ていない"
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert "作業場の外**にいます" not in res, f"自分の背景処理を止められない: {res}"
        time.sleep(0.5)
        assert not procutil.group_members(pgid), res
    finally:
        try:
            os.killpg(pgid, 9)
        except OSError:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_控えの構成員と別人なら証明にしない(env):
    """`_released_record_proves` の単体。**控えに載っているだけ**では通さない。

    ① 控えに `members` が無い（作り物の古い控え）→ False
    ② 番号は同じでも命令が違う → False
    ③ 控えを書いた時刻より**あとに**生まれている（PID の使い回し）→ False
    """
    victim = _sleeper(str(env))
    try:
        mem = [{"pid": victim.pid, "cmdline": "python -c import time; time.sleep(60)",
                "cwd": str(env)}]
        meta_p = _plant_record(victim.pid, cwd=str(env), start_ticks=None,
                               mtime=time.time())
        base = json.loads(meta_p.read_text(encoding="utf-8"))
        # `_plant_record` は 8/6 を焼き込むので、いまの控えとして書き直す
        base["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        assert bo_tool._released_record_proves(base, meta_p, mem) is False  # ①
        base["members"] = [[victim.pid, "まったく別の命令"]]
        assert bo_tool._released_record_proves(base, meta_p, mem) is False  # ②
        base["members"] = [[victim.pid, mem[0]["cmdline"]]]
        assert bo_tool._released_record_proves(base, meta_p, mem) is True
        old = dict(base, started_at="2026-08-06T00:00:00")                  # ③
        assert bo_tool._released_record_proves(old, meta_p, mem) is False
    finally:
        try:
            os.killpg(os.getpgid(victim.pid), 9)
        except OSError:
            pass
        victim.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_控えのファイルが無くても所有権の判定で落ちない(env):
    """★nit ⑥: `meta_p` は `None` で渡されうる（`_launch_epoch` は既にそう書いて
    ある）のに、ここだけ `except OSError` だったので**単体で呼ぶと落ちた**。"""
    victim = _sleeper(str(env))
    try:
        meta = {"pid": victim.pid, "cmd": "sleep 60", "cwd": str(env),
                "start_ticks": procutil.proc_start_ticks(victim.pid)}
        assert bo_tool._ownership_note(victim.pid, meta, None, []) is None
        meta_bad = dict(meta, start_ticks=1)          # 別人（時刻が合わない）
        note = bo_tool._ownership_note(victim.pid, meta_bad, None, [])
        assert note and note.startswith("Error:") and "別人" in note, note
    finally:
        try:
            os.killpg(os.getpgid(victim.pid), 9)
        except OSError:
            pass
        victim.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_長が先に死ぬ普通の形でも自分の背景処理を止められる(env, monkeypatch):
    """★merge-bash-4 の反証役の指摘（2026-09-05・実測つき）を、この枝の側で受ける。

    指摘: `procutil.bg_record_matches` は先頭で「`proc_start_ticks(pid)` が None なら
    False」なので、**長が先に死ぬいちばん普通の形**（`nohup … &` ＋ `exit 0`）では
    控えの `start_ticks` が一度も参照されず `owned_bg_pids` に載らない。

    ★6 巡目でここを直した（`procutil.bg_group_matches`）。「長が死んでいる」と
      「番号が別人に回った」を**区別**する: 控えの `members` に載っている pid が
      いまも**同じ pgid の**構成員で、命令が一致し、控えより後に生まれていなければ
      自分の物。だから 5 巡目まで False だった `owned_bg_pids()` に**載る**ように
      なった——`_port_report` も `bash._scope_name_kill` も、この形の背景サーバを
      「自分の物」として扱えるようになる（載らない間は止めてくれなかった）。
    ここでは①台帳の照合が通ること ②**構成員の欄が無い縮約の控えは通らない**こと
    （＝関門は緩んでいない）③実際に止まること、を測って残す。
    """
    monkeypatch.chdir(env)
    from laps.tools import bash as bash_tool
    import re
    (env / "start.sh").write_text(
        # ★出力を逃がさない（`>/dev/null` を付けると子がパイプを離すので、
        #   `bash.py` は "done" で終わり**控えを書かない**＝この形にならない）。
        "#!/bin/sh\nnohup sleep 40 &\nexit 0\n", encoding="utf-8")
    started = _text(bash_tool._run({"command": "bash start.sh", "timeout": 8}))
    m = re.search(r"BashOutput\(pid=(\d+), kill=true\)", started)
    assert m, started
    pgid = int(m.group(1))
    try:
        members = procutil.group_members(pgid)
        assert members, "前提が崩れている（背景の仲間が居ない）"
        assert not bo_tool._is_alive(pgid), "前提が崩れている（長がまだ生きている）"
        meta_p = procutil.bg_dir() / f"{pgid}.meta.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        assert meta.get("released") is True, meta
        # ★union-2（2026-09-06）: merge-bash 側の直しで `start_ticks` は
        #   **起動直後に**前倒しで控えられるようになり、この控えにも残る
        #   （良い変化）。ただしこの試験の主眼は「**長がいま死んでいる**ので
        #   `bg_record_matches` が構成員照合へ落ちる」ことであって、控えに
        #   `start_ticks` が有るか無いかでは**ない**（`bg_record_matches` の
        #   分岐は `start_ticks(pid)`＝**現物**の生死で決まり、控えの欄は見ない）。
        #   控えの形を元の主眼（欄が無い）に合わせて剥がす。
        meta.pop("start_ticks", None)
        meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        assert meta.get("start_ticks") is None, meta
        # ① 台帳の照合が、長が死んでいても**構成員で**通る（6 巡目 must_fix ④）
        rec = bo_tool._as_bg_record(pgid, meta, meta_p)
        assert procutil.bg_group_matches(rec) is True, rec
        assert procutil.bg_record_matches(rec) is True, rec
        assert pgid in procutil.owned_bg_pids()
        # ② 5 巡目までの**縮約**の控え（`members` 欄が無い）は通らない
        #    ＝関門そのものは緩んでいない（分からない物は撃たない側に倒す）
        assert procutil.bg_record_matches(
            {"pid": pgid, "start_ticks": meta.get("start_ticks"),
             "mtime": meta_p.stat().st_mtime}) is False
        # ③ 別人に回った番号は通らない（控えの命令と一致しない）
        fake = dict(rec, members=[[m["pid"], "まったく別の命令"] for m in members])
        assert procutil.bg_group_matches(fake) is False, fake
        # ④ 撃つ経路の所有判定も、同じ 1 本を通って同じ事実に辿り着く
        assert bo_tool._released_record_proves(meta, meta_p, members) is True
        # ⑤ 実際に止まる（門が仕事を殺していない）
        res = _text(bo_tool._run({"pid": pgid, "kill": True}))
        assert "撃っていません" not in res, res
        assert not procutil.group_members(pgid), res
    finally:
        try:
            os.killpg(pgid, 9)
        except OSError:
            pass


# ── 判定が**壊れた**ときに黙って通さない（2026-09-05・6 巡目・must_fix ① の同型）──
#
# ★union で実際に起きた形: `procutil._boot_time` が改名され、こちら側の
#   `except Exception` が `AttributeError` を飲んで「時刻が読めない」に化け、
#   起動時刻の関門が**黙って全部素通り**した。同じ形が所有権の門にも 4 か所あった:
#     ・`bg_record_matches` が落ちたら `ok = True`（＝一致したことにする）
#     ・`in_workspace` が落ちたら `None`（＝呼ぶ側が「撃ってよい」に読み替える）
#     ・`owned_bg_pids` が落ちたら `set()`（＝黙って狭める）
#   撃つ許可の門なので、確かめられないなら**撃たない**（fail-closed）。


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
@pytest.mark.parametrize("target,exc,outside", [
    ("bg_record_matches",
     AttributeError("module 'procutil' has no attribute '_boot_time'"), False),
    ("in_workspace",
     AttributeError("module 'procutil' has no attribute 'canonical_workspace'"), False),
    ("owned_bg_pids", TypeError("bad ledger"), True),
], ids=["台帳の照合が壊れた", "作業場の判定が壊れた", "通行証の一覧が壊れた"])
def test_所有権の判定が壊れたら撃たない(env, tmp_path, monkeypatch, target, exc, outside):
    """★確かめられないものを「確かめた」と言わない。文言でも欠陥だと名指しする。

    `outside=True` の形（`owned_bg_pids` が壊れる）は、cwd が作業場の**外**で
    ないと意味が無い——中に居るなら cwd だけで通してよく、通行証は要らないから。
    そこは陰性対照 `test_作業場の外でも自分が起こしたと証明できるものは止められる`
    が「壊れていなければ止まる」側を押さえている。
    """
    where = tmp_path / "outside" if outside else env
    where.mkdir(exist_ok=True)
    victim = _sleeper(str(where))
    try:
        _plant_record(victim.pid, cwd=str(where),
                      start_ticks=procutil.proc_start_ticks(victim.pid))

        def _boom(*a, **k):
            raise exc
        monkeypatch.setattr(procutil, target, _boom)
        res = _text(bo_tool._run({"pid": victim.pid, "kill": True}))
        assert res.startswith("Error:") and "撃っていません" in res, res
        assert "確かめられません" in res and type(exc).__name__ in res, res
        assert victim.poll() is None, "確かめられないまま撃った"
    finally:
        try:
            os.killpg(os.getpgid(victim.pid), 9)
        except OSError:
            pass
        victim.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
@pytest.mark.parametrize("outside", [False, True], ids=["作業場の中", "作業場の外"])
def test_壊れていなければ同じ形は今までどおり止まる(env, tmp_path, outside):
    """★上の 3 本の陰性対照（門が普段の仕事を殺していないこと）。"""
    where = tmp_path / "outside" if outside else env
    where.mkdir(exist_ok=True)
    victim = _sleeper(str(where))
    try:
        _plant_record(victim.pid, cwd=str(where),
                      start_ticks=procutil.proc_start_ticks(victim.pid))
        res = _text(bo_tool._run({"pid": victim.pid, "kill": True}))
        assert res.startswith("OK:"), res
        victim.wait(timeout=5)
    finally:
        try:
            os.killpg(os.getpgid(victim.pid), 9)
        except OSError:
            pass


class _Counted:
    """`_bump` の宛先だけを持つ殻（`tools.CURRENT_AGENT` に立てる）。"""
    def __init__(self):
        self.counters: dict[str, int] = {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_所有権の門は本番で数えられる(env, tmp_path, monkeypatch):
    """★6 巡目: この門が「呼ばれ N 回・断り M 回」と**本番で**数えられること。

    union が「呼ばれ 30 回・断り 0 回」を出せたのは数えたからで、数が無ければ
    「配線してあるが一度も効いていない」に気づけない（この現場の常連の欠陥）。
    ここでは通す形・断る形・壊れた形の 3 通りで、印が実際に増えるのを見る。
    """
    from laps import tools as tools_mod
    ag = _Counted()
    tok = tools_mod.CURRENT_AGENT.set(ag)
    outside = tmp_path / "outside"
    outside.mkdir()
    mine = _sleeper(str(env))              # 作業場の中＝通る
    theirs = _sleeper(str(outside))        # 作業場の外・控えは別人＝断る
    try:
        _plant_record(mine.pid, cwd=str(env),
                      start_ticks=procutil.proc_start_ticks(mine.pid))
        _plant_record(theirs.pid, cwd=str(outside), start_ticks=None,
                      mtime=1754400000.0)  # 8/6 の古い控え＝通行証は出ない
        assert _text(bo_tool._run({"pid": mine.pid, "kill": True})).startswith("OK:")
        assert ag.counters.get("bg_own_gate_checked") == 1, ag.counters
        assert "bg_own_gate_denied" not in ag.counters, ag.counters
        # ★union-2（2026-09-06）: `mine` も **手前**の PID 使い回し検問
        #   （`_run_body` 内・merge-bash 側）を通ってから `_ownership_note` に届く
        #   ——通る形でも「チェックした」印は増える（否認の印は増えない）。
        assert ag.counters.get("bg_reuse_gate_checked") == 1, ag.counters
        assert "bg_reuse_gate_denied" not in ag.counters, ag.counters
        res = _text(bo_tool._run({"pid": theirs.pid, "kill": True}))
        assert res.startswith("Error:"), res
        # ★`theirs` は `_ownership_note` より**手前**の検問そのもので断られ、
        #   `_ownership_note`（＝`bg_own_gate_checked`）へは届かない——lifecycle
        #   単独枝にはこの手前の検問が無く、この試験はそれを知らずに書かれていた。
        assert ag.counters.get("bg_own_gate_checked") == 1, ag.counters
        assert "bg_own_gate_denied" not in ag.counters, ag.counters
        assert ag.counters.get("bg_reuse_gate_checked") == 2, ag.counters
        assert ag.counters.get("bg_reuse_gate_denied") == 1, ag.counters

        # ★壊れた形は**控えが一致する**相手で見る（一致しない相手は手前の
        #   「別人」で断られてしまい、作業場の判定まで届かない）。
        third = _sleeper(str(env))
        _plant_record(third.pid, cwd=str(env),
                      start_ticks=procutil.proc_start_ticks(third.pid))

        def _boom(*a, **k):
            raise AttributeError("module 'procutil' has no attribute 'in_workspace'")
        monkeypatch.setattr(procutil, "in_workspace", _boom)
        assert "確かめられません" in _text(bo_tool._run({"pid": third.pid, "kill": True}))
        assert ag.counters.get("bg_own_gate_broken") == 1, ag.counters
        assert third.poll() is None, "確かめられないまま撃った"
    finally:
        tools_mod.CURRENT_AGENT.reset(tok)
        for p in (mine, theirs, locals().get("third")):
            if p is None:
                continue
            try:
                os.killpg(os.getpgid(p.pid), 9)
            except OSError:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass


# ── must_fix B-5（union-2・2026-09-06）: 断り文に作業場が無い理由を出す ──────
#
# ★塞いだ穴（union2.md の指摘どおり・実測で確認）: `canonical_workspace()` が
#   `NO_WORKSPACE`（床に届かない・実在し得ない 1 本のパス）を返すとき、
#   `_ownership_note` / `_run_body` の断り文には**そのパスの見た目**
#   （`/nonexistent/blackwall-no-workspace`）だけが出て、「なぜ作業場が
#   無いのか」が本文から分からなかった。`procutil.workspace_note()` を足す。


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_作業場が無いとき所有権の断り文に理由が出る(tmp_path, monkeypatch):
    """`_ownership_note` の「作業場の外」denial（pid の cwd 経路）。

    ★`_run_body` の**手前**の台帳作業場チェック（`_ws_now` 不一致・下の別試験）が
    NO_WORKSPACE では必ず先に断ってしまい `_ownership_note` まで届かないので、
    ここは `_ownership_note` を直接呼ぶ。
    """
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    other = tmp_path / "somewhere"
    other.mkdir()
    victim = _sleeper(str(other))
    try:
        meta = {"pid": victim.pid, "pgid": victim.pid, "cmd": "sleep 60",
                "cwd": str(other), "started_at": "2026-08-06T00:00:00",
                "start_ticks": procutil.proc_start_ticks(victim.pid),
                "workspace": str(other)}
        # ★床に届かない作業場（深さ 1）＝ canonical_workspace() は NO_WORKSPACE。
        monkeypatch.setenv("LAPS_WORKSPACE", "/tmp")
        res = bo_tool._ownership_note(victim.pid, meta, None, [])
        assert res is not None and res.startswith("Error:") and "作業場の外" in res, res
        assert "作業場が設定されていません" in res, \
            f"NO_WORKSPACE の理由が本文に出ていない: {res}"
    finally:
        victim.kill(); victim.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の /proc を見る")
def test_作業場が無いとき台帳不一致の断り文にも理由が出る(env, tmp_path, monkeypatch):
    """`_run_body` の「この作業場のものではありません」denial（pid の台帳経路）。

    証明できる控え（開始時刻が一致）にして、`_ownership_note` より**手前**の
    台帳作業場チェックで断らせる。
    """
    victim = _sleeper(str(env))
    try:
        _plant_record(victim.pid, cwd=str(env),
                      start_ticks=procutil.proc_start_ticks(victim.pid),
                      workspace=str(tmp_path / "elsewhere"))
        # いまの作業場を床未満（NO_WORKSPACE）に変える。
        monkeypatch.setenv("LAPS_WORKSPACE", "/tmp")
        res = _text(bo_tool._run({"pid": victim.pid, "kill": True}))
        assert res.startswith("Error:") and "この作業場のものではありません" in res, res
        assert "作業場が設定されていません" in res, \
            f"NO_WORKSPACE の理由が本文に出ていない: {res}"
        assert victim.poll() is None, "作業場が無いのに撃っている"
    finally:
        victim.kill(); victim.wait(timeout=5)


# ── 二次（union-2・2026-09-06）: 古い台帳（workspace 欄なし）の逃げ道案内 ──
def test_古い台帳は他の経路を案内する(env, tmp_path):
    """`workspace` 欄が無い**古い台帳**は pid 経路で永久に通行証にならない
    （作業場を直しても効かない）。案内が要る。"""
    victim = _sleeper(str(env))
    try:
        d = procutil.bg_dir(create=True)
        meta_p = d / f"{victim.pid}.meta.json"
        # ★`workspace` 欄を意図的に書かない（古い台帳の形）。
        meta_p.write_text(json.dumps({
            "pid": victim.pid, "pgid": victim.pid, "cmd": "sleep 60",
            "cwd": str(env), "started_at": "2026-08-06T00:00:00",
            "start_ticks": procutil.proc_start_ticks(victim.pid),
        }, ensure_ascii=False), encoding="utf-8")
        (d / f"{victim.pid}.out").write_bytes(b"")
        (d / f"{victim.pid}.err").write_bytes(b"")
        res = _text(bo_tool._run({"pid": victim.pid, "kill": True}))
        assert res.startswith("Error:") and "この作業場のものではありません" in res, res
        assert "古い台帳" in res, res
        assert 'Bash("kill <pid>")' in res and "BashOutput(port=" in res, \
            f"他の経路を案内していない: {res}"
        assert victim.poll() is None, "古い台帳のプロセスを撃った"
    finally:
        victim.kill(); victim.wait(timeout=5)
