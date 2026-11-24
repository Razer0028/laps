"""名前撃ち（`pkill python` 等）を作業場の中だけに絞る（2026-09-02）。

★実害: devbench の api 課題「最後にサーバを止めること」で、エージェント（trusted）が
  名前で撃ち、同じホストの **corridor-bridge（python3）が 7 分で 6 回 SIGKILL**、
  telemetry の受け口も 2 回死んだ。記録にある名前撃ち 41 件は**全部自分の php が狙い**。
★制限ではない: 狙い（作業場の中）は撃つ。**狙いの外に当たる弾だけ落とし、落とした相手を
  結果に列挙して、本当に狙うなら PID で撃てる道を示す。**
"""
import pytest

from laps.tools import bash as B

WS = "/work/ws1"
TABLE = [
    (100, "python3", "python3 server.py --port 18100", "/work/ws1",
     ("python3", "server.py", "--port", "18100")),                             # 自分の
    (101, "python3", "/usr/bin/python3 /srv/shared/scripts/corridor_bridge.py", "/",
     ("/usr/bin/python3", "/srv/shared/scripts/corridor_bridge.py")),          # 他人の
    (102, "python3", "/usr/bin/python3 /usr/local/lib/blackwall-telemetry/ingest.py --serve", "/",
     ("/usr/bin/python3", "/usr/local/lib/blackwall-telemetry/ingest.py", "--serve")),
    (103, "php", "php -S 0.0.0.0:18300 router.php", "/work/ws1/site",
     ("php", "-S", "0.0.0.0:18300", "router.php")),                            # 自分の
    (104, "php", "php-fpm: pool www", "/", ("php-fpm:", "pool", "www")),        # 他人の
]


@pytest.fixture(autouse=True)
def _fake_proc(monkeypatch):
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE))
    monkeypatch.setattr(B, "_registered_bg_pids", lambda *a, **k: set())
    monkeypatch.setattr(B.sys, "platform", "linux")


def test_pkill_f_python_は作業場の中だけを撃つ():
    r = B._scope_name_kill("pkill -f python", WS)
    assert r is not None
    new, note = r
    assert new == "kill -TERM 100", new
    assert "corridor_bridge" in note and "ingest.py" in note, "落とした相手を列挙していない"
    assert "PID" in note, "本当に狙うときの道を示していない"


def test_pkill_9_は信号を保つ():
    """★2026-09-05: 信号は**逐語**で保つ（`-9` を `-KILL` に、`-HUP` を `-TERM` に
    化けさせない）。第4回の not_done に「混在時に `-HUP` が `-TERM` に化ける」と
    書いてあった穴をここで閉じる。"""
    new, _ = B._scope_name_kill("pkill -9 -f python", WS)
    assert new == "kill -9 100"
    new2, _ = B._scope_name_kill("pkill -HUP -f python", WS)
    assert new2 == "kill -HUP 100", new2
    new3, _ = B._scope_name_kill("pkill -f python", WS)
    assert new3 == "kill -TERM 100", new3


def test_作業場の外にしか当たらなければ撃たない():
    new, note = B._scope_name_kill("pkill -f corridor_bridge", WS)
    assert new == "true", "他人のサービスを撃っている"
    assert "撃っていません" in note and "corridor_bridge" in note


def test_killall_は_comm_で見る():
    new, note = B._scope_name_kill("killall php", WS)
    assert new == "kill -TERM 103", new
    assert "php-fpm" in note


def test_明示PIDのkillは触らない():
    assert B._scope_name_kill("kill 101", WS) is None
    assert B._scope_name_kill("kill -9 101 102", WS) is None


def test_巻き添えが無ければ書き換えない():
    """★狙いの外に当たらないなら、何もしない（黙って広げない・黙って狭めない）。"""
    assert B._scope_name_kill("pkill -f 'server.py --port 18100'", WS) is None


def test_作業場が分からなければ触らない():
    assert B._scope_name_kill("pkill -f python", None) is None


def test_pgrep_を起こさない(monkeypatch):
    """★`pgrep -f` は自分の argv に当たって自分を殺す（この現場で 1 日 5 回）。/proc を直接読む。"""
    import subprocess
    def boom(*a, **k): raise AssertionError("subprocess を起こしている（pgrep 自己一致の再来）")
    monkeypatch.setattr(subprocess, "run", boom); monkeypatch.setattr(subprocess, "Popen", boom)
    B._scope_name_kill("pkill -f python", WS)


def test_引用符つきの空白入りパターンを拾う():
    """★記録の名前撃ち 41 件中 20 件がこの形（`pkill -f "php -S 0.0.0.0:18300"`）。"""
    # ★この形は作業場の外に当たらない（php-fpm の cmdline に一致しない）ので
    #   **書き換えない（None）が正しい**。最初に書いた期待値のほうが間違っていた。
    assert B._scope_name_kill('pkill -f "php -S 0.0.0.0:18300"', WS) is None
    # 外に当たる形なら絞る（`2>/dev/null` が付いていても拾う＝記録に実在した形）
    new, note = B._scope_name_kill('pkill -f "php.*" 2>/dev/null', WS)
    assert new.startswith("kill -TERM 103"), new
    assert "php-fpm" in note


def test_連結命令の中の名前撃ちも絞る():
    r = B._scope_name_kill('pkill -f python; sleep 1; echo done', WS)
    assert r is not None
    new, note = r
    assert new.startswith("kill -TERM 100;") and "echo done" in new, new
    assert "corridor_bridge" in note


# ── 第4回b: 「作業場」の基準を道具の引数から取り上げる ──────────────────
def test_cwd引数は名前撃ちの範囲を広げも狭めもしない(monkeypatch, tmp_path):
    """★塞いだ穴（第4回b・両方の反証が同じ根を指した）:
    範囲の基準が `cwd or os.getcwd()` ＝**モデルが渡す引数**だった。
      (a) `cwd="/"` を渡せば全ホストが「作業場」になり、**作業場の外に居る他人の
          プロセスが『自分の物』に化ける**（＝名前撃ちが絞られずに当たる）。
      (b) `cwd="<ws>/site"` を渡すと**自分のサーバが『外』に化けて撃てなくなる**
          （run50 の工程4 で実際に起きた壁）。
    Bash の `cwd` は「ここで走らせる」であって「これが作業場だ」ではない。
    基準を正典（`policy._workspace()`）へ移すと両方同時に消える。
    ★2026-09-05（第4回c）の訂正: 旧 (a) は「`kill -9 1` まで自分の物」と書いて
      いたが**事実ではない**。pid 1・自分・祖先・保護ポートの設備は
      `_kill_protected_pids()` が cwd に関わらず常に落とす。cwd で消えるのは
      「作業場の外の**普通の**プロセスを守る側」で、そこが本当の実害。
    """
    ws = tmp_path / "ws"
    (ws / "site").mkdir(parents=True)
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    table = [
        (100, "php", "php -S 0.0.0.0:18300", str(ws), ("php", "-S", "0.0.0.0:18300")),
        (101, "php", "php-fpm: pool www", "/", ("php-fpm:", "pool", "www")),
    ]
    monkeypatch.setattr(B, "_proc_table", lambda: list(table))
    seen = {}

    def _spy(cmd, workspace):
        seen["ws"] = workspace
        # ★**必ず無害な命令へ書き換えて返す**（2026-09-05・この試験で実際に事故った）。
        #   最初の版は None を返しており、`_run` が `pkill -f php` を**そのまま実行**した。
        #   走らせているシェルの argv に "php" の 3 文字が入っていたため自己一致し、
        #   さらに同じホストの docker コンテナ（vdir の本番 videodir）の
        #   `docker-php-entrypoint` まで落として再起動させた（restartCount 2・30 秒で復帰）。
        #   ＝この領域が直している当の失敗（`pkill -f` の自己一致・他人のサービスの巻き添え）を
        #   **試験の側でやった**。判定に実行は要らないので、実行させない。
        return "true", ""
    monkeypatch.setattr(B, "_scope_name_kill", _spy)
    for widen in ("/", "/root", str(ws / "site")):
        out = B._run({"command": "pkill -f __never_matches__", "cwd": widen, "timeout": 5})
        assert seen["ws"] == str(ws), f"cwd={widen} が作業場の基準を書き換えた"
        assert "Error" not in (out if isinstance(out, str) else out.text)


# ── 境界バグ2件の再現（2026-09-05・r4 tools-process 監査で発見） ──────────────
# 旧実装: `mine = (cwd and os.path.realpath(cwd).startswith(ws)) or (ws in cmdline)`
# ①は cwd の前方一致に区切り文字が無い、②は cmdline 全体への部分文字列一致。
# どちらも「文字列としては前方一致/部分一致するが、パスとしては別物」を『中』に化けさせる。

def test_兄弟ディレクトリを作業場の中と誤判定しない(monkeypatch):
    """`/work/ws1-decoy/app` は文字列としては `/work/ws1` で始まるが、
    別ディレクトリ（兄弟）であって作業場の中ではない。"""
    decoy = (777001, "python3", "python3 /work/ws1-decoy/app/run.py",
             "/work/ws1-decoy/app", ("python3", "/work/ws1-decoy/app/run.py"))
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE) + [decoy])
    new, note = B._scope_name_kill("pkill -f python", WS)
    assert new == "kill -TERM 100", f"兄弟ディレクトリのプロセスを撃つ側に含めた: {new}"
    assert "777001" in note, "兄弟ディレクトリのプロセスが『撃たなかった』一覧に出ていない"


def test_wsを接頭辞に持つ別パスをcmdlineから誤判定しない(monkeypatch):
    """`ws in cmdline` は `/work/ws1` が `/work/ws10-other-project/...` の**部分文字列**
    としてたまたま一致するだけでも『中』に化けさせていた（`r4` vs `r42` と同じ形の穴）。
    cwd は作業場と無関係（＝バックアップ/インデクサ等の別プロセスを模す）。"""
    other = (777002, "python3", "python3 /work/ws10-other-project/build.py",
             "/var/lib/backupd", ("python3", "/work/ws10-other-project/build.py"))
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE) + [other])
    new, note = B._scope_name_kill("pkill -f python", WS)
    assert new == "kill -TERM 100", f"無関係プロセスを撃つ側に含めた: {new}"
    assert "777002" in note


def test_台帳に載っているpidは相対起動でも中と判定する(monkeypatch):
    """★cwd が空・argv が相対パスの自分のサーバ（run50 のような起動）でも、
    `run_in_background` の台帳（`_bg_dir` の `<pid>.meta.json`）に載っていれば
    『中』——cwd/argv の判定だけに頼らない一次シグナル。"""
    ghost = (999001, "php", "php -S 0.0.0.0:18300 router.php", "",
             ("php", "-S", "0.0.0.0:18300", "router.php"))
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE) + [ghost])
    monkeypatch.setattr(B, "_registered_bg_pids", lambda *a, **k: {999001})
    new, note = B._scope_name_kill("pkill -f php", WS)
    assert new is not None
    kept = set(new.removeprefix("kill -TERM ").split())
    assert kept == {"103", "999001"}, kept
    assert "104" not in note.split("撃ちました")[0]  # 104(php-fpm) は依然 outside


def test_断り文はPIDで撃ち直せと案内しない():
    """★第4回b 反証②-2: 名前撃ちの断り文は「起動時に控えた PID を `kill <pid>` するか」と
    逃げ道を案内していた。明示 PID の kill も同じ基準で断るようになった以上、その案内は
    **両ゲートが同時に断つ経路**を作る（run53 の実記録で断りが 2 回・どちらも
    断って正しい相手＝docker の php と別ディレクトリの検収サーバ 5 本）。
    案内は「持ち主を確かめる／自分の物は作業場の中に居るはず」に書き換えた。"""
    _, note = B._scope_name_kill("pkill -f corridor_bridge", WS)
    assert "PID を名指ししても同じ基準で断ります" in note, note
    assert "ss -ltnp" in note
    assert "起動時に控えた PID を `kill <pid>` するか" not in note
    # PID ゲートの断り文と**同じ案内**であること（片方だけ直る形を作らない）
    _, note2 = B._scope_pid_kill("kill 101", WS)
    assert B._KILL_ADVICE in note and B._KILL_ADVICE in note2


def test_名前撃ちでも自分と祖先は撃たない():
    """`pkill -f python` が自分（python プロセス）に当たる形。/proc を実際に読む。"""
    import os
    from laps import procutil
    real_rows = [(os.getpid(), "python3", "python3 -m pytest", os.getcwd(),
                  ("python3", "-m", "pytest"))]
    import pytest as _p
    B_table = B._proc_table
    try:
        B._proc_table = lambda: list(real_rows)
        r = B._scope_name_kill("pkill -f python3", os.getcwd())
        assert r is not None and r[0] == "true", r
        assert str(os.getpid()) in r[1]
    finally:
        B._proc_table = B_table
