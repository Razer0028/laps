"""背景処理の台帳（`~/.blackwall/bg/<pid>.meta.json`）を**通行証にする条件**
（第4回b・総括 §2-12・2026-09-05）。

★塞いだ穴（この機械で実測）:
  - 台帳には **23 件すべて死亡済み**の記録が残っていた（最古は 8/6）。
    「番号が載っていれば自分の物」とだけ見ると、PID を引き当てた
    **無関係のプロセスを撃つ通行証**になる。→ 開始時刻で照合する。
  - しかもその照合以前に、読み出しが `Path.stem.isdigit()` で書かれていた。
    `1828868.meta.json` の `stem` は `1828868.meta` なので **一度も真にならない**
    ＝台帳は発行もされていなかった（守る側の効き目も無かった）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from laps import procutil
from laps.tools import bash as bash_tool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="/proc を読む")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    d = procutil.bg_dir(create=True)
    return d


#: 試験で使う「作業場」。台帳の記録はこの作業場のものとして書く（第4回c）。
WS = "/nowhere/ws1"


def _write(d, pid: int, **extra):
    rec = {"pid": pid, "pgid": pid, "cmd": "sleep 300", "cwd": "/", "workspace": WS}
    rec.update(extra)
    (d / f"{pid}.meta.json").write_text(json.dumps(rec), encoding="utf-8")
    (d / f"{pid}.out").write_bytes(b"")
    (d / f"{pid}.err").write_bytes(b"")
    return d / f"{pid}.meta.json"


def test_台帳の読み出しがファイル名で壊れない(home):
    """`Path.stem` は**最後の拡張子しか外さない**。この 1 行のせいで
    台帳は一度も引けていなかった。"""
    _write(home, 4242)
    recs = procutil.bg_records()
    assert [r["pid"] for r in recs] == [4242], recs


def test_死んだ記録は通行証にならない(home):
    """★この機械の 23 件がまさにこれ（全部死亡済み）。"""
    _write(home, 4_000_001, start_ticks=12345)
    assert procutil.owned_bg_pids(WS) == set()


def test_生きている記録は通行証になる(home):
    """自分が起こした背景処理は、cwd がどこであっても『自分の物』
    （run50 の工程4＝自分のサーバを止められず建て直せない壁の再発防止）。"""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        _write(home, p.pid, start_ticks=procutil.proc_start_ticks(p.pid))
        assert p.pid in procutil.owned_bg_pids(WS)
        row = {"pid": p.pid, "comm": "sleep", "cmdline": "sleep 30",
               "cwd": "/somewhere/else", "argv": ("sleep", "30")}
        assert procutil.in_workspace(row, WS), "台帳の通行証が効いていない"
    finally:
        p.kill(); p.wait(timeout=5)


def test_PID再利用は開始時刻でふるい落とす(home):
    """同じ番号を引き当てた別人を撃たない。開始時刻（/proc/<pid>/stat の 22 番目）で照合。"""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        real = procutil.proc_start_ticks(p.pid)
        assert real is not None
        _write(home, p.pid, start_ticks=real + 100_000)   # 別人の記録
        assert p.pid not in procutil.owned_bg_pids(WS)
        row = {"pid": p.pid, "comm": "sleep", "cmdline": "sleep 30",
               "cwd": "/somewhere/else", "argv": ()}
        assert not procutil.in_workspace(row, WS)
    finally:
        p.kill(); p.wait(timeout=5)


def test_古い記録が無ければmtimeで代用する(home):
    """`start_ticks` を持たない旧い台帳（今日より前に書かれた物）も、
    起動時刻と台帳の mtime を突き合わせて判断する（分からなければ通さない）。"""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        _write(home, p.pid)                       # start_ticks 無し＝旧形式
        assert p.pid in procutil.owned_bg_pids(WS), "起動直後なら同一人物と見なせる"
        old = home / f"{p.pid}.meta.json"
        os.utime(old, (time.time() - 86_400, time.time() - 86_400))
        assert p.pid not in procutil.owned_bg_pids(WS), "1 日ずれた記録を通している"
    finally:
        p.kill(); p.wait(timeout=5)


def test_死んだ控えを掃除するが新しい物と生きている物は残す(home):
    """★消しすぎない: 走行中のモデルは**終わった処理の出力**を読み返す。
    既定は「死んでいて、かつ 72 時間より古い」物だけ。"""
    old = _write(home, 4_000_002)
    os.utime(old, (time.time() - 80 * 3600, time.time() - 80 * 3600))
    fresh = _write(home, 4_000_003)              # 死んでいるが新しい
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        alive = _write(home, p.pid, start_ticks=procutil.proc_start_ticks(p.pid))
        os.utime(alive, (time.time() - 80 * 3600, time.time() - 80 * 3600))
        n = procutil.prune_bg_records()
        assert n == 1, n
        assert not old.exists() and not (home / "4000002.out").exists()
        assert fresh.exists(), "終わったばかりの出力まで消している"
        assert alive.exists(), "生きている背景処理の控えを消している"
    finally:
        p.kill(); p.wait(timeout=5)


def test_掃除は止められる(home, monkeypatch):
    old = _write(home, 4_000_004)
    os.utime(old, (time.time() - 80 * 3600, time.time() - 80 * 3600))
    monkeypatch.setenv("LAPS_BG_KEEP_HOURS", "0")
    assert procutil.prune_bg_records() == 0
    assert old.exists()


def test_本番の背景起動が開始時刻を控える(home, tmp_path, monkeypatch):
    """★配線の確認: 台帳を書くのは `_spawn_background`（本番の関数）。
    ここで `start_ticks` を書かなければ、上の照合は永久に mtime 頼りになる。"""
    ws = tmp_path / "ws"; ws.mkdir()
    # ★union-2（must_fix B-1）: conftest の既定作業場は `/tmp`（深さ 1）＝
    #   `canonical_workspace()` は `NO_WORKSPACE` を返す。以前はここが
    #   `NO_WORKSPACE` どうしの**自己一致バグ**で偶然通っていた（床が無いのに
    #   「自分の物」になっていた）——修正後は素の `NO_WORKSPACE` では絶対に
    #   一致しないので、実在する深さのある作業場を明示する。
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    out = bash_tool._run({"command": "sleep 20", "cwd": str(ws),
                          "run_in_background": True})
    out = out if isinstance(out, str) else out.text
    pid = int([ln for ln in out.splitlines() if ln.strip().startswith("pid:")][0].split(":")[1])
    try:
        meta = json.loads((procutil.bg_dir() / f"{pid}.meta.json").read_text(encoding="utf-8"))
        assert isinstance(meta.get("start_ticks"), int), meta
        assert meta["start_ticks"] == procutil.proc_start_ticks(pid)
        # ★どの作業場で起こしたかも控える（第4回c）。無いと台帳は
        #   `LAPS_HOME` を共有する**全セッション共通の kill 許可証**になる。
        assert meta.get("workspace") == procutil.canonical_workspace(), meta
        assert pid in procutil.owned_bg_pids()
    finally:
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


# ── 第4回c: 台帳は「ホスト共通の kill 許可証」だった ─────────────────────

def test_別の作業場で起こした記録は通行証にならない(home):
    """★塞いだ穴（2026-09-05・第4回c）: 台帳は `LAPS_HOME`（既定
    `~/.blackwall`）に置かれ、**同居する全セッションで共有**される。
    作業場を見ないと、`ws=/srv/shared` で走っているセッションにも
    `ws=/root/vdirwork/run53` の背景処理の通行証が出る＝ホスト共通の許可証。"""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        _write(home, p.pid, start_ticks=procutil.proc_start_ticks(p.pid))
        assert p.pid in procutil.owned_bg_pids(WS), "前提が崩れている"
        assert p.pid not in procutil.owned_bg_pids("/srv/shared")
        row = {"pid": p.pid, "comm": "sleep", "cmdline": "sleep 30",
               "cwd": "/somewhere/else", "argv": ("sleep", "30")}
        assert procutil.in_workspace(row, "/srv/shared") is False
    finally:
        p.kill(); p.wait(timeout=5)


def test_workspaceを書いていない古い記録は通さない(home):
    """★fail-closed: 分からない物を通すのは『撃つ許可』の側。"""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        rec = {"pid": p.pid, "pgid": p.pid, "cmd": "sleep 300", "cwd": "/",
               "start_ticks": procutil.proc_start_ticks(p.pid)}
        (home / f"{p.pid}.meta.json").write_text(json.dumps(rec), encoding="utf-8")
        assert p.pid not in procutil.owned_bg_pids(WS)
        assert p.pid not in procutil.owned_bg_pids(procutil.canonical_workspace())
    finally:
        p.kill(); p.wait(timeout=5)


def test_狭めた作業場でも自分のサーバの通行証は残る(home):
    """★run50 工程4 の壁を戻さない: `BashOutput(cwd="<ws>/site")` のように
    範囲を**狭めた**とき、完全一致で照合すると自分のサーバの通行証が消える。
    同じ枝（どちらかがどちらかの中）なら通す。"""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        _write(home, p.pid, start_ticks=procutil.proc_start_ticks(p.pid))
        assert p.pid in procutil.owned_bg_pids(WS + "/site"), "狭めたら通行証が消えた"
        assert p.pid in procutil.owned_bg_pids("/nowhere"), "広い側でも同じ枝なら通す"
        assert p.pid not in procutil.owned_bg_pids("/nowhere2/ws1")
    finally:
        p.kill(); p.wait(timeout=5)
# ── 長が先に死ぬ、いちばん普通の形（2026-09-05・6 巡目 must_fix ④）─────────────
#
# ★指摘（merge-bash の反証役・実測つき）: `bg_record_matches` は先頭で
#   「`proc_start_ticks(pid)` が None なら False」だった。ところが `nohup … &`＋
#   `exit 0` はシェル（＝グループの長）が**即座に**終わり、`bash._release_record`
#   はそのあとで控えを書くので `start_ticks` は必ず None・長の `/proc` はもう無い。
#   ＝控えた欄は**一度も参照されず** `owned_bg_pids()` に載らない。自分で起こした
#   背景サーバが「自分の物ではない」ことになり、`_port_report` は止めてくれない。
# ★直し方は「長が死んでいる」と「番号が別人に回った」の**区別**（`bg_group_matches`）。


def _dead_leader_group():
    """長（sh）だけが死に、構成員（sleep）が生きているグループを作る。

    戻り: `(pgid, member_pid)`。`start_new_session=True` なので pgid = sh の pid。
    """
    p = subprocess.Popen(["sh", "-c", "sleep 30 & exit 0"], start_new_session=True)
    p.wait(timeout=5)                          # ★長を刈り取る（/proc から消える）
    pgid = p.pid
    for _ in range(50):
        members = procutil.group_members(pgid)
        if members:
            return pgid, members[0]["pid"], members[0]["cmdline"]
        time.sleep(0.02)
    raise AssertionError("前提が崩れている（構成員が居ない）")


def _reap_group(pgid: int):
    try:
        os.killpg(pgid, 9)
    except OSError:
        pass


def test_長が死んでも控えの構成員で自分の物と分かる(home):
    """★これが載らなかったせいで「自分のサーバを止められない」形になっていた。"""
    pgid, mpid, mcmd = _dead_leader_group()
    try:
        assert procutil.proc_start_ticks(pgid) is None, "前提が崩れている（長が生きている）"
        _write(home, pgid, released=True, start_ticks=None, cmd="sh -c 'sleep 30 & exit 0'",
               started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
               members=[[mpid, mcmd]])
        # ★union-2: `_write` は既定で `workspace=WS` を書く（このファイル冒頭）。
        #   `owned_bg_pids()`（無引数）は conftest の既定作業場（/tmp 等）に落ちるので、
        #   `WS` と一致しない——他の試験と同じく明示して渡す。
        assert procutil.owned_bg_pids(WS) == {pgid}, "長が死んだだけで通行証を出していない"
        row = {"pid": pgid, "comm": "sh", "cmdline": "sh", "cwd": "/somewhere/else",
               "argv": ()}
        assert procutil.in_workspace(row, "/nowhere"), "台帳の通行証が効いていない"
    finally:
        _reap_group(pgid)


#: 控えを「別人の物」に変える 4 形（`None` の欄は控えから**消す**）。
_NOT_MINE = [
    ("構成員の欄が無い",                  {"members": None}),
    ("命令が別人",                        {"members": "別の命令"}),
    ("番号が別人（居ない pid）",          {"members": "別の番号"}),
    ("控えより後に生まれた（PID 使い回し）", {"started_at": "2026-08-06T00:00:00"}),
]


@pytest.mark.parametrize("label,patch", _NOT_MINE, ids=[x[0] for x in _NOT_MINE])
def test_長が死んだ控えでも別人なら通行証を出さない(home, label, patch):
    """★緩めた側ではないことを 4 形で固定する（通行証は**撃つ許可**なので fail-closed）。"""
    pgid, mpid, mcmd = _dead_leader_group()
    try:
        body = {"released": True, "start_ticks": None,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "members": [[mpid, mcmd]]}
        for k, v in patch.items():
            if v is None:
                body.pop(k, None)
            elif v == "別の命令":
                body["members"] = [[mpid, "まったく別の命令"]]
            elif v == "別の番号":
                body["members"] = [[mpid + 4_000_000, mcmd]]
            else:
                body[k] = v
        _write(home, pgid, **body)
        assert procutil.owned_bg_pids() == set(), f"{label} を通している"
    finally:
        _reap_group(pgid)


def test_グループが空になった控えは通行証にならない(home):
    """構成員も居なくなったら（＝本当に全部終わった）通さない。"""
    pgid, mpid, mcmd = _dead_leader_group()
    _write(home, pgid, released=True, start_ticks=None,
           started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), members=[[mpid, mcmd]])
    # ★union-2: 明示して渡す（上のコメントと同じ理由）。
    assert procutil.owned_bg_pids(WS) == {pgid}
    _reap_group(pgid)
    for _ in range(50):
        if not procutil.group_members(pgid):
            break
        time.sleep(0.02)
    assert procutil.owned_bg_pids(WS) == set(), "死んだグループに通行証を出している"


# ── must_fix B-1（union-2・2026-09-06）: NO_WORKSPACE の自己一致 ──────────
#
# ★塞いだ穴（実測で確認・union2.md の指摘どおり）: `canonical_workspace()` は
#   床（`/`・`$HOME`・深さ 1）で `procutil.NO_WORKSPACE` を返す。
#   `bg_record_workspace_ok(rec, workspace)` は両者を `os.path.realpath` して
#   `_within` で比べるので、**`workspace` も `rec_ws` もどちらも `NO_WORKSPACE`
#   （同じ 1 本の文字列）なら `a == b` で自己一致してしまう**——`/` や `$HOME` を
#   名乗る**別セッション**の背景処理の記録が、こちらの通行証になる形。


def test_NO_WORKSPACEどうしは自己一致しない(home, monkeypatch):
    """★床に届かない者どうしを「同じ作業場」と見なさない。"""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        # 別セッション（床が無い＝NO_WORKSPACE）が書いた記録、という体で控える。
        rec = {"pid": p.pid, "pgid": p.pid, "cmd": "sleep 30",
               "cwd": "/", "workspace": procutil.NO_WORKSPACE,
               "start_ticks": procutil.proc_start_ticks(p.pid)}
        (home / f"{p.pid}.meta.json").write_text(json.dumps(rec), encoding="utf-8")
        # こちらも床が無い（LAPS_WORKSPACE を `/` に）。
        monkeypatch.setenv("LAPS_WORKSPACE", "/")
        assert procutil.canonical_workspace() == procutil.NO_WORKSPACE, \
            "前提が崩れている（床の判定が変わった）"
        assert not procutil.bg_record_workspace_ok(rec, procutil.NO_WORKSPACE), \
            "NO_WORKSPACE どうしが自己一致している（must_fix B-1 が未修正）"
        assert p.pid not in procutil.owned_bg_pids(), \
            "床の無いセッションの記録に通行証が出ている"
    finally:
        p.kill(); p.wait(timeout=5)


def test_workspace欄にNO_WORKSPACEを書かない(monkeypatch, tmp_path):
    """★`bg_workspace_field()`（台帳へ書く側）は床に届かないとき空文字にする
    ——`NO_WORKSPACE` という特定の文字列を台帳に残さない（書く側からも塞ぐ）。"""
    monkeypatch.setenv("LAPS_WORKSPACE", "/")
    assert procutil.canonical_workspace() == procutil.NO_WORKSPACE
    assert procutil.bg_workspace_field() == ""
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    assert procutil.bg_workspace_field() == str(tmp_path / "ws")
