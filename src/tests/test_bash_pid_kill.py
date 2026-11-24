"""明示 PID の `kill` を作業場の中（＋自分が起動した背景処理）だけに絞る（2026-09-05）。

★実害（r4 tools-process 監査で発見）: `_scope_name_kill` は**名前撃ち**しか見ておらず、
  裸の `kill <PID>`（`kill -9 1` すら）はどの層にも一切スコープされていなかった。
  `_scope_name_kill` 自身のコメント通り明示 PID は意図的に無視、`workspace_guard`
  は 2026-09-04 に kill 系を対象から外し（run50 の自己サーバ再起動を通すため）、
  `policy.py` の `_ALWAYS_DENIED` にも kill/pkill は無く、既定の trusted モードは
  そこも素通りする。実測: `kill -9 1`（pid 1 = init）が両ゲートとも無処理で通った。

★これは制限ではない（`_scope_name_kill` と同じ設計）: 作業場の中の PID は撃つ。
  狙いの外に当たる PID だけ落とし、落とした相手を列挙する。0.1.191 が広げすぎた
  ヒューリスティック（goal の文言に依存する形）は再導入しない——この道具は
  goal の中身を見ず、常にプロセスの所属だけで判定する。
"""
import os
import time
import subprocess
import sys

import pytest

from laps.tools import bash as B

# ★下の real-proc テストが「実際の /proc を読む」ために、autouse フィクスチャで
#   差し替えられる前の本物を控えておく（モジュール import 時点＝どのテストの
#   フィクスチャもまだ走っていない）。
_REAL_PROC_TABLE = B._proc_table
_REAL_REGISTERED_BG_PIDS = B._registered_bg_pids

WS = "/work/ws1"
TABLE = [
    (100, "python3", "python3 server.py --port 18100", "/work/ws1",
     ("python3", "server.py", "--port", "18100")),          # 自分の
    (101, "python3", "/usr/bin/python3 /srv/shared/scripts/corridor_bridge.py", "/",
     ("/usr/bin/python3", "/srv/shared/scripts/corridor_bridge.py")),   # 他人の
    (1, "systemd", "/sbin/init", "/", ("/sbin/init",)),       # pid 1 = init。他人の最たる例。
]


@pytest.fixture(autouse=True)
def _fake_proc(monkeypatch):
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE))
    monkeypatch.setattr(B, "_registered_bg_pids", lambda *a, **k: set())
    monkeypatch.setattr(B.sys, "platform", "linux")


def test_作業場の中のpidはそのまま():
    assert B._scope_pid_kill("kill 100", WS) is None


def test_作業場の外のpidは_trueに落ちる():
    new, note = B._scope_pid_kill("kill 101", WS)
    assert new == "true"
    assert "101" in note and "作業場の外" in note


def test_init_pid1を無条件に撃たない():
    """★2026-09-05 実測で確認した実害そのもの: `kill -9 1` が両ゲートとも
    無処理で通っていた。init は作業場の中に居るわけがないので必ず落ちる。"""
    new, note = B._scope_pid_kill("kill -9 1", WS)
    assert new == "true", f"pid 1 を撃つ経路が残っている: {new}"
    assert "1" in note


def test_信号を保つ():
    new, _ = B._scope_pid_kill("kill -9 101", WS)
    assert new == "true"           # 全部 outside なので true（信号の保持は inside がある場合に見る）
    new2, _ = B._scope_pid_kill("kill -KILL 100 101", WS)
    assert new2 == "kill -KILL 100", new2
    new3, _ = B._scope_pid_kill("kill -s HUP 100 101", WS)
    assert new3 == "kill -s HUP 100", new3


def test_混在は中だけに絞る():
    new, note = B._scope_pid_kill("kill -9 100 101", WS)
    assert new == "kill -9 100", new     # ★信号は逐語（-9 のまま。-KILL に化けない）
    assert "101" in note


def test_不明なpidは判定不能として通す():
    """★fail-open: `/proc` に無い（既に終了・別 PID 名前空間）PID は
    分からないので撃つ側に残す（狭めすぎて run50 の自己再起動を再発させない）。"""
    r = B._scope_pid_kill("kill 100 555555", WS)
    assert r is None, "全部『分かる範囲では中、または不明』なら書き換えない"


def test_不明pidと外部pidが混ざれば外部だけ落とす():
    new, note = B._scope_pid_kill("kill 555555 101", WS)
    assert new is not None
    # ★信号指定が無かったのだから、付け足さない（元の形をそのまま保つ）。
    assert new == "kill 555555", new
    assert "101" in note


def test_連結命令の中も絞る():
    r = B._scope_pid_kill("kill 101; sleep 1; echo done", WS)
    assert r is not None
    new, note = r
    assert new.startswith("true;") and "echo done" in new, new


def test_pkillは対象にしない():
    """名前撃ちは `_scope_name_kill` の仕事——ここが二重に手を出さない。"""
    assert B._scope_pid_kill("pkill -f python", WS) is None
    assert B._scope_pid_kill("killall php", WS) is None


def test_作業場が分からなければ触らない():
    assert B._scope_pid_kill("kill 101", None) is None


def test_win32では触らない(monkeypatch):
    monkeypatch.setattr(B.sys, "platform", "win32")
    assert B._scope_pid_kill("kill 101", WS) is None


def test_台帳のpidはcwdが空でも中と判定する(monkeypatch):
    ghost = (999001, "php", "php -S 0.0.0.0:18300 router.php", "",
             ("php", "-S", "0.0.0.0:18300", "router.php"))
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE) + [ghost])
    monkeypatch.setattr(B, "_registered_bg_pids", lambda *a, **k: {999001})
    assert B._scope_pid_kill("kill 999001", WS) is None


# ── run() 経由の配線（fake ではなく本物の呼び出し口を通す） ─────────────────

def test_run経由でも外部pidはtrueに書き換わる(monkeypatch, tmp_path):
    """`_run()` が `_scope_pid_kill` を実際に呼び、書き換えた命令を実行することを
    確かめる——シェルへ渡る前に絞られていることの配線試験。★本番の既定
    （`policy._mode()`）は trusted（conftest の autouse は試験の安全側デフォルトで
    workspace に固定しているので、ここだけ明示的に本番既定へ戻す）。"""
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_WORKSPACE", WS)
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    out = B._run({"command": "kill -9 1", "cwd": None})
    assert "denied" not in out
    assert "作業場の外" in out, out


def test_実機のprocで作業場の中の子プロセスは撃たない(monkeypatch, tmp_path):
    """★フェイク _proc_table だけに頼らない検証（本物の /proc を読む）。
    自分で立てた使い捨ての子プロセス（`sleep`）だけを対象にする——
    本番の他セッションのプロセスには一切触れない。"""
    monkeypatch.setattr(B, "_proc_table", _REAL_PROC_TABLE)
    monkeypatch.setattr(B, "_registered_bg_pids", _REAL_REGISTERED_BG_PIDS)
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    outside = str(tmp_path / "outside")  # 存在しなくても realpath は例外にならない
    proc = subprocess.Popen(["sleep", "30"], cwd=ws)
    try:
        r_inside = B._scope_pid_kill(f"kill {proc.pid}", ws)
        assert r_inside is None, "作業場の中の実プロセスを書き換えてしまった"

        r_outside = B._scope_pid_kill(f"kill {proc.pid}", outside)
        assert r_outside is not None
        new, note = r_outside
        assert new == "true"
        assert str(proc.pid) in note
        # 実際にまだ生きている（"true" に化けたので触っていないはず）。
        assert proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# ── 第4回b: 反証 2 件の must_fix（すべて再現つきで指摘された穴） ─────────────

def test_プロセスグループ撃ちを素通しにしない(monkeypatch):
    """★反証①の中心: `_PID_KILL_RE` は**正の十進**しか見ていなかったので、
    `kill -9 -1`（＝自分が信号を送れる全プロセス。root では実質ホスト停止）・
    `kill -- -1`・`kill 0`（＝自分のプロセスグループ＝エージェント自身）が
    どの層にも触られず通っていた。`kill -9 1` と**同じ命令・同じ数字の形**で、
    一文字違うだけ。ここは fail-open にしない（通した先が全部だから）。

    ★`_proc_rows` を**空**にして測る（2026-09-05）: 最初この試験は
    「グループの中身検査」がたまたま断ってくれるおかげで緑になっており、
    `-1`/`0` の fail-closed を丸ごと消しても**赤くならなかった**（変異試験で発覚）。
    中身検査が何も見つけない状況でも断つ＝**この分岐が理由で断っている**ことを見る。
    """
    monkeypatch.setattr(B, "_proc_rows", lambda: [])
    for cmd, must in (("kill -9 -1", "全プロセス"), ("kill -- -1", "全プロセス"),
                      ("kill -TERM -1", "全プロセス"),
                      ("kill 0", "自分のプロセスグループ全部"),
                      ("kill -9 0", "自分のプロセスグループ全部")):
        r = B._scope_pid_kill(cmd, WS)
        assert r is not None, f"素通りしている: {cmd}"
        assert r[0].startswith("true"), (cmd, r[0])
        assert must in r[1], (cmd, r[1])


def test_自分のプロセスグループを名指ししても落とす(monkeypatch):
    """`kill -- -<自分の pgid>` は `kill 0` と同じ意味。数字が違うだけで通してしまわない。"""
    monkeypatch.setattr(B, "_proc_rows", lambda: [])
    pg = os.getpgrp()
    r = B._scope_pid_kill(f"kill -- -{pg}", WS)
    assert r is not None and r[0].startswith("true"), r
    assert f"pgid {pg}" in r[1], r[1]


def test_中身が空のグループは素通しのまま(monkeypatch):
    """★`-N` の既定は fail-open（誰も居ない＝分からない）。`-1`/`0`/自分の pgid
    だけが例外だという線引きを、ここで明示的に固定する。"""
    monkeypatch.setattr(B, "_proc_rows", lambda: [])
    assert B._scope_pid_kill("kill -- -424242", WS) is None


def test_他人のプロセスグループは中身を数えて落とす(monkeypatch):
    """`-N` は「プロセスグループ N」。中身を /proc で数え、外の物が居れば落とす。"""
    rows = [{"pid": 100, "comm": "python3", "cmdline": "python3 server.py",
             "cwd": "/work/ws1", "argv": ("python3", "server.py"), "pgid": 4242},
            {"pid": 101, "comm": "python3", "cmdline": "/usr/bin/python3 corridor_bridge.py",
             "cwd": "/", "argv": ("/usr/bin/python3", "corridor_bridge.py"), "pgid": 4242}]
    monkeypatch.setattr(B, "_proc_rows", lambda: list(rows))
    r = B._scope_pid_kill("kill -- -4242", WS)
    assert r is not None and r[0].startswith("true"), r
    assert "corridor_bridge" in r[1]
    # 中身が全部『中』なら触らない
    monkeypatch.setattr(B, "_proc_rows", lambda: [rows[0]])
    assert B._scope_pid_kill("kill -- -4242", WS) is None


def test_自分と親と祖先は撃たせない():
    """★反証②-3: `_proc_table()` は `getpid()`/`getppid()` を飛ばすので、
    エージェント自身と親は「表に無い＝不明＝素通し」に化けていた。
    **いちばん高くつく相手にだけ穴が開いていた。**"""
    from laps import procutil
    for pid in [os.getpid(), os.getppid()] + procutil.ancestors(os.getpid())[:2]:
        r = B._scope_pid_kill(f"kill -9 {pid}", WS)
        assert r is not None and r[0].startswith("true"), f"自分/祖先 {pid} を撃てる"


def test_bin_kill_も同じ扱い():
    """記録に `/bin/kill` の形が在る。綴りが違うだけで素通りさせない。"""
    for prog in ("/bin/kill", "/usr/bin/kill"):
        r = B._scope_pid_kill(f"{prog} -9 1", WS)
        assert r is not None and r[0].startswith("true"), prog


def test_末尾の抑止と背景化を落とさない():
    """`2>/dev/null` と末尾 `&` は記録に実在する形。付いていても拾い、書き換え後も残す。"""
    new, _ = B._scope_pid_kill("kill -9 101 2>/dev/null", WS)
    assert new == "true 2>/dev/null", new
    new2, _ = B._scope_pid_kill("kill 101 &", WS)
    assert new2 == "true &", new2
    new3, _ = B._scope_pid_kill("kill -9 100 101 2>/dev/null", WS)
    assert new3 == "kill -9 100 2>/dev/null", new3


def test_pidファイル経由の撃ちを解いて判定する(tmp_path):
    """★§2-8: 記録の kill 系のうち `kill $(cat server.pid)` が 8 件・
    `kill -9 $(lsof -t -i :18300)` が 4 件＝**どのゲートも見ていなかった 20 件の主力**。
    外部コマンドは起こさず、`cat` の中身は自分で読んで判定する。"""
    pidfile = tmp_path / "server.pid"
    pidfile.write_text("101\n", encoding="utf-8")     # 101 = 作業場の外
    r = B._scope_pid_kill(f"kill $(cat {pidfile})", WS)
    assert r is not None and r[0] == "true", r
    assert "corridor_bridge" in r[1]
    pidfile.write_text("100\n", encoding="utf-8")     # 100 = 作業場の中
    assert B._scope_pid_kill(f"kill $(cat {pidfile})", WS) is None


def test_pidファイルは道具のcwdからの相対でも解く(tmp_path):
    (tmp_path / "server.pid").write_text("101", encoding="utf-8")
    r = B._scope_pid_kill("kill $(cat server.pid)", WS, base=str(tmp_path))
    assert r is not None and r[0] == "true", r


def test_解けない置き換えは素通しのまま():
    """`$!`・`%1`・`$(jobs -p)`・`$(ps … | grep …)` は解かない＝従来どおり通す。
    前 3 つはシェル自身の子＝構造上いつも『自分の物』。**塞いだふりをしない。**"""
    for cmd in ("kill $!", "kill %1", "kill $(jobs -p) 2>/dev/null",
                'kill -TERM $(ps -ef | grep "php -S" | grep -v grep | awk "{print \\$2}")',
                "kill $PID"):
        assert B._scope_pid_kill(cmd, WS) is None, cmd


def test_lsofのポート持ち主を解いて判定する(monkeypatch):
    from laps import procutil
    monkeypatch.setattr(procutil, "port_listeners",
                        lambda port: [{"pid": 101, "comm": "python3",
                                       "cmdline": "corridor_bridge.py", "cwd": "/",
                                       "argv": ("python3",), "pgid": 1}])
    r = B._scope_pid_kill("kill -9 $(lsof -t -i :18300) 2>/dev/null", WS)
    assert r is not None and r[0] == "true 2>/dev/null", r
    monkeypatch.setattr(procutil, "port_listeners", lambda port: [])
    assert B._scope_pid_kill("kill -9 $(lsof -t -i :18300) 2>/dev/null", WS) is None


# ── 作業場の基準（反証②-1・②-2: 同じ原因で穴と誤検知が同時に出ていた） ────────

def test_cwd引数で看板の防御を消せない(monkeypatch, tmp_path):
    """★`cwd` は Bash 道具が**モデルに開いている引数**なので、基準がそこだと
    `Bash(command="kill <外の pid>", cwd="/")` で「作業場＝/」になり、
    **作業場の外の普通のプロセスが『自分の物』に化けて撃てて**しまう。
    基準を `policy._workspace()` に移したので、`cwd="/"` でも落ちる。

    ★2026-09-05（第4回c）に**試験を書き直した**。旧版は `kill -9 1` を使って
      いたが、pid 1 は `_kill_protected_pids()` が cwd に関わらず常に落とすので、
      **基準が cwd に戻っても必ず緑**＝配線を何も確かめていなかった
      （「試験が緑」は「その機構が働いた」証拠にならない、をここでも 1 回）。
      いまは**保護対象でない、作業場の外の実プロセス**で見る。基準が cwd に
      戻ると `cwd="/"` でこの pid が『中』になり、この試験は赤くなる。
    """
    monkeypatch.setattr(B, "_proc_table", _REAL_PROC_TABLE)
    monkeypatch.setattr(B, "_registered_bg_pids", _REAL_REGISTERED_BG_PIDS)
    ws = str(tmp_path / "ws"); os.makedirs(ws, exist_ok=True)
    outside = str(tmp_path / "outside"); os.makedirs(outside, exist_ok=True)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_WORKSPACE", ws)
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    proc = subprocess.Popen(["sleep", "30"], cwd=outside)
    try:
        out = B._run({"command": f"kill {proc.pid}", "cwd": "/"})
        assert "作業場の外" in out, out
        assert str(proc.pid) in out, out
        time.sleep(0.4)
        assert proc.poll() is None, "cwd=/ を渡して作業場の外のプロセスを撃てた"
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=5)


def test_cwdを下に渡しても自分のサーバを撃てる(monkeypatch, tmp_path):
    """★同じ原因の裏側: `cwd="<ws>/site"` を渡すと作業場が `<ws>/site` になり、
    `<ws>` で動く**自分のサーバが『外』に化けて撃てなくなる**（誤検知）。
    run50 工程4 の壁と同じ形なので、ここを踏むと本番が止まる。"""
    monkeypatch.setattr(B, "_proc_table", _REAL_PROC_TABLE)
    monkeypatch.setattr(B, "_registered_bg_pids", _REAL_REGISTERED_BG_PIDS)
    ws = str(tmp_path / "ws"); site = os.path.join(ws, "site")
    os.makedirs(site, exist_ok=True)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_WORKSPACE", ws)
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    proc = subprocess.Popen(["sleep", "30"], cwd=ws)
    try:
        out = B._run({"command": f"kill {proc.pid}", "cwd": site})
        assert "作業場の外" not in out and "撃たせない相手" not in out, out
        proc.wait(timeout=5)              # 実際に止まった＝絞られていない
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=5)


def test_sh_cで起動した自分のサーバを外と誤判定しない():
    """★反証②-7: `_proc_is_mine` を『絶対パスのトークンだけ』に狭めた結果、
    `_spawn_background` が実際に作る argv（`sh -c "cd <ws> && php -S …"`・cwd は外）
    が **旧＝中／新＝外** に変わっていた。取りこぼしたら run50 の壁が戻る。"""
    from laps import procutil
    row = {"pid": 900, "comm": "sh", "cwd": "/tmp",
           "argv": ("/bin/sh", "-c", f"cd {WS} && php -S 0.0.0.0:18300 -t site/"),
           "cmdline": f"/bin/sh -c cd {WS} && php -S 0.0.0.0:18300 -t site/"}
    assert procutil.in_workspace(row, WS) is True
    # オプションに貼り付いた形（`-t<ws>/site`）も拾う
    row2 = {"pid": 901, "comm": "php", "cwd": "/tmp",
            "argv": ("php", "-S", "127.0.0.1:8000", f"-t{WS}/site"), "cmdline": ""}
    assert procutil.in_workspace(row2, WS) is True


# ── 台帳（`~/.blackwall/bg/*.meta.json`）の通行証を締める ─────────────────

def test_台帳は起動時刻が合わないpidを通さない(monkeypatch, tmp_path):
    """★反証①の二次指摘: 台帳の PID は cwd/argv を無視した**無条件の通行証**で、
    掃除もされない（この機械に 23 件・全部死亡済み・最古は 2026-08-06）。
    PID が再利用されると無関係のプロセスに通行証が渡る。起動時刻で照合する。"""
    import json as _json, time as _time
    monkeypatch.setattr(B, "_registered_bg_pids", _REAL_REGISTERED_BG_PIDS)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    proc = subprocess.Popen(["sleep", "30"])
    try:
        bg = tmp_path / "bg"; bg.mkdir(parents=True, exist_ok=True)
        meta = bg / f"{proc.pid}.meta.json"
        ws = str(tmp_path / "ws")
        # ① 起動時刻が合っている＝本人（作業場も一致している）
        meta.write_text(_json.dumps({"pid": proc.pid, "cmd": "x", "cwd": None,
            "workspace": ws,
            "started_at": _time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        assert proc.pid in B._registered_bg_pids(ws)
        # ② 合っていない＝PID 再利用。通行証を渡さない。
        meta.write_text(_json.dumps({"pid": proc.pid, "cmd": "x", "cwd": None,
            "workspace": ws,
            "started_at": "2020-01-01T00:00:00"}), encoding="utf-8")
        assert proc.pid not in B._registered_bg_pids(ws)
    finally:
        proc.kill(); proc.wait(timeout=5)


def test_kill判定は台帳を掃除しない(monkeypatch, tmp_path):
    """★塞いだ穴（2026-09-05・第4回c）: `_registered_bg_pids()` は判定のたびに
    `procutil.prune_bg_records()` を呼んでいた。台帳は `LAPS_HOME` を共有する
    **同居セッション全部**の物なので、こちらが `kill` を 1 回判定するたびに
    **他セッションの背景処理の出力（.out/.err）を消していた**——
    「読むだけの判定が、他人の書いた物を消す」形。掃除は起こす側
    （`_spawn_background`）と明示の保守経路（`procutil.prune_bg_records`）だけ。"""
    from laps import procutil
    monkeypatch.setattr(B, "_registered_bg_pids", _REAL_REGISTERED_BG_PIDS)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    bg = tmp_path / "bg"; bg.mkdir(parents=True, exist_ok=True)
    dead = 4194300                                    # pid_max 近辺＝まず生きていない
    for suffix in (".meta.json", ".out", ".err"):
        (bg / f"{dead}{suffix}").write_text("{}", encoding="utf-8")
    old = time.time() - B._BG_META_TTL_S - 60
    for suffix in (".meta.json", ".out", ".err"):
        os.utime(bg / f"{dead}{suffix}", (old, old))
    B._registered_bg_pids()
    assert (bg / f"{dead}.meta.json").exists(), "判定が他人の控えを消している"
    assert (bg / f"{dead}.out").exists()
    # 明示の保守経路でだけ消える
    assert procutil.prune_bg_records() == 1
    assert not (bg / f"{dead}.meta.json").exists()
    assert not (bg / f"{dead}.out").exists()


def test_背景起動は掃除の経路として生きている(monkeypatch, tmp_path):
    """★掃除を判定から外したので、**起こす側に残っていること**を配線で見る
    （外した先で誰も掃除しないと台帳が無限に伸びる）。"""
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    bg = tmp_path / "bg"; bg.mkdir(parents=True, exist_ok=True)
    dead = 4194302
    for suffix in (".meta.json", ".out", ".err"):
        (bg / f"{dead}{suffix}").write_text("{}", encoding="utf-8")
    old = time.time() - B._BG_META_TTL_S - 60
    for suffix in (".meta.json", ".out", ".err"):
        os.utime(bg / f"{dead}{suffix}", (old, old))
    ws = tmp_path / "ws"; ws.mkdir(exist_ok=True)
    out = B._run({"command": "sleep 5", "cwd": str(ws), "run_in_background": True})
    pid = int([ln for ln in out.splitlines() if ln.strip().startswith("pid:")][0].split(":")[1])
    try:
        assert not (bg / f"{dead}.meta.json").exists(), "起動側の掃除まで消えている"
    finally:
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


def test_新しい台帳は死んでいても消さない(monkeypatch, tmp_path):
    """★終わった背景処理の出力は後から読む（BashOutput）。TTL 前に消さない。"""
    monkeypatch.setattr(B, "_registered_bg_pids", _REAL_REGISTERED_BG_PIDS)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    bg = tmp_path / "bg"; bg.mkdir(parents=True, exist_ok=True)
    (bg / "4194301.meta.json").write_text("{}", encoding="utf-8")
    (bg / "4194301.out").write_text("結果", encoding="utf-8")
    B._registered_bg_pids()
    assert (bg / "4194301.out").exists()


def test_知らない形の命令は触らない():
    """対象の**形**が白名簿に無いなら触らない（fail-open）。

    ★2026-09-05・第4回c の 4 回目に**期待値を書き換えた**（旧版はここで
      `kill 101 & echo x` と `kill 101 | tee log` を「触らない」と定めていた）。
      理由: 「`_split_segments` は単独の `&` や `|` では割らないので、書き換えると
      `kill & echo x` という壊れた命令が出来る」——これは**壊さない書き方が
      無かったから諦めていた**だけで、その fail-open は実物で保護ポートのサーバを
      殺した穴そのものだった（反証 B2: `kill -9 <pid> | cat` が素通し）。
      いまは末尾（`| …` / `& …` / `2>/dev/null`）を `_split_control_tail` で
      剥がして**そのまま書き戻す**ので、壊れた命令は出来ない。
      ここが守るのは「壊れた命令を作らない」という**不変条件のほう**で、
      素通しはその手段ではない。
    """
    for cmd in ("kill -l", "kill 101 > out.txt extra", "killer 101"):
        assert B._scope_pid_kill(cmd, WS) is None, cmd
    # 末尾つきは**断ったうえで末尾を保つ**（101 は作業場の外）
    for cmd, want in (("kill 101 & echo x", "true & echo x"),
                      ("kill 101 | tee log", "true | tee log"),
                      ("kill -9 101 | cat", "true | cat")):
        r = B._scope_pid_kill(cmd, WS)
        assert r is not None, f"{cmd} が素通しになっている（反証 B2 の形）"
        assert r[0] == want, (cmd, r[0])


def test_実機のprocでプロセスグループ撃ちを判定する(monkeypatch, tmp_path):
    """★作り物の表ではなく**本番の経路**（`_proc_table` → `_proc_rows` → pgid）で
    `kill -- -N` の中身検査が動くことを見る。`_proc_rows` に `pgid` を載せ忘れると
    members が常に空になり、本番では一度も発火できない（最初そうなっていた）。"""
    monkeypatch.setattr(B, "_proc_table", _REAL_PROC_TABLE)
    monkeypatch.setattr(B, "_registered_bg_pids", _REAL_REGISTERED_BG_PIDS)
    ws = str(tmp_path / "ws"); os.makedirs(ws, exist_ok=True)
    outside = str(tmp_path / "outside"); os.makedirs(outside, exist_ok=True)
    # 自分のセッションを持たせる＝pgid が pid と等しい使い捨ての子。
    proc = subprocess.Popen(["sleep", "30"], cwd=ws, start_new_session=True)
    try:
        from laps import procutil
        assert procutil.pgid_of(proc.pid) == proc.pid, "前提が崩れている"
        # 作業場の中のグループ → 触らない
        assert B._scope_pid_kill(f"kill -- -{proc.pid}", ws) is None
        # 作業場の外のグループ → 落とす（中身を数えて判定できている証拠）
        r = B._scope_pid_kill(f"kill -- -{proc.pid}", outside)
        assert r is not None and r[0] == "true", r
        assert str(proc.pid) in r[1]
        assert proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)


# ── 第4回c: 作業場の床（`/` を渡されてもホスト全部が自分の物にならない）────

def test_作業場が_なら何ひとつ自分の物にならない(monkeypatch, tmp_path):
    """★`serve._apply_env` は依頼の `spec.workspace` を**検証せず**に
    `LAPS_WORKSPACE` へ入れて chdir する。`/` を渡されると
    `in_workspace` はホストのほぼ全プロセスを「中」と判定し（再測 260/260）、
    kill 系ゲートは絞る側から**撃つ許可**を出す側に化けていた（実測）。
    床に届かない作業場では**何ひとつ『自分の物』にならない**（撃たない側へ倒す）。
    ★理由も出す: 黙って全部断ると、モデルは同じ命令を綴りを変えて撃ち続ける。"""
    monkeypatch.setattr(B, "_proc_table", _REAL_PROC_TABLE)
    monkeypatch.setattr(B, "_registered_bg_pids", _REAL_REGISTERED_BG_PIDS)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_WORKSPACE", "/")
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    victim = tmp_path / "victim"; victim.mkdir()
    proc = subprocess.Popen(["sleep", "30"], cwd=str(victim))
    try:
        out = B._run({"command": f"kill {proc.pid}"})
        assert "作業場の外" in out, out
        assert "作業場が設定されていません" in out, out
        time.sleep(0.4)
        assert proc.poll() is None, "ws=/ でホストのプロセスを撃てた"
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=5)
