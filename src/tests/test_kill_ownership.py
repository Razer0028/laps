"""所有権判定（`procutil`）の境界——kill 系ゲートが共有する**唯一の**判定。

★2026-09-05（第4回b）: 同じ判定が `tools/bash.py`（名前撃ち・PID 撃ち）と
  `tools/bash_output.py`（ポートの持ち主）に別々に在ると必ず片方だけ直る。
  実体を `laps/procutil.py` に 1 本化したので、境界の試験もここ 1 か所に置く。
"""
import os
import subprocess

from laps import procutil as P

WS = "/work/ws1"


def test_兄弟ディレクトリは中ではない():
    """旧 `realpath(cwd).startswith(ws)` は `/home/user/project` に対して
    `/home/user/project-decoy` を『中』と誤判定していた。"""
    assert P.path_within("/work/ws1", WS) is True
    assert P.path_within("/work/ws1/site", WS) is True
    assert P.path_within("/work/ws1-decoy/app", WS) is False
    assert P.path_within("", WS) is False


def test_接頭辞がたまたま一致する別パスは中ではない():
    """旧 `ws in cmdline` は `/work/ws1` が `/work/ws10-other/...` の**部分文字列**
    として一致するだけで『中』に化けた（`r4` と `r42` と同じ形の穴）。"""
    assert P.mentions_workspace("python3 /work/ws10-other/build.py", WS) is False
    assert P.mentions_workspace("/other/work/ws1/x", WS) is False


def test_貼り付いた書き方も中と読む():
    """★逆に「絶対パスのトークンだけ」に寄せると、実際に使われている形を落とす。
    `_spawn_background` が作る argv は `sh -c "cd <ws> && …"`（1 トークン）。"""
    assert P.mentions_workspace(f"cd {WS} && php -S 0.0.0.0:18300", WS) is True
    assert P.mentions_workspace(f"-t{WS}/site", WS) is True
    assert P.mentions_workspace(f"--docroot={WS}", WS) is True
    assert P.mentions_workspace(f"php -S 0.0.0.0:18300 -t {WS}/site", WS) is True


def test_in_workspaceはcwdと土台にした起動だけを所有と見る():
    """★2026-09-05 の統合で **1 か所だけ期待値を書き換えた**（理由をここに残す）。

    元の tools-process 版はこの試験で「cwd が外でも argv に作業場の絶対パスが
    あれば『中』」（`php /work/ws1/x.php` → True）を要求していた。しかし
    core-server-lifecycle 側の反証の must_fix 3 は
    「`ws in cmdline` の部分一致は**列挙にだけ使い、kill の根拠にしない**
    （cwd が外なら撃たない）」を明示的に求めており、その根拠は実測——
    `/tmp` に居る `tail -f <ws>/x.log` が『自分の物』に化け、名前撃ちでは
    「絞る」だけだったこの緩さが、ポート経由の停止では**撃つ許可**になっていた。
    `tail -f <ws>/x.log` と `php <ws>/x.php` は**構造として同じ形**なので、
    片方だけ許すことはできない。

    一方 tools-process の must_fix 5 が要求する
    「**非絶対トークン中**の作業場パス（`-t/path`・`--docroot=/path`・
    `sh -c "cd /path && …"`）も所有シグナルとして拾う」は残す（落とすと
    run50 工程4 の「自分のサーバを止められない」壁が戻る）。
    ＝所有の線は「**作業場を土台にして動いている**形」で引き、
    裸の絶対パス参照は `references_workspace` で列挙にだけ使う。
    """
    assert P.in_workspace({"cwd": "/work/ws1/site", "argv": (), "cmdline": ""}, WS) is True
    # 土台にしている形（`cd <ws>` / 貼り付いたオプション）は cwd が外でも『中』
    assert P.in_workspace({"cwd": "/", "cmdline": "",
                           "argv": ("/bin/sh", "-c", f"cd {WS} && php -S :1")}, WS) is True
    assert P.in_workspace({"cwd": "/", "cmdline": "",
                           "argv": ("php", "-S", ":1", f"-t{WS}/site")}, WS) is True
    # 裸の絶対パスを触っているだけ＝『外』。ただし列挙の材料にはなる。
    ref = {"cwd": "/", "argv": ("php", "/work/ws1/x.php"), "cmdline": ""}
    assert P.in_workspace(ref, WS) is False
    assert P.references_workspace(ref, WS) is True
    assert P.in_workspace({"cwd": "/", "argv": ("php", "/etc/x.php"),
                           "cmdline": ""}, WS) is False
    assert P.in_workspace({"cwd": "/", "argv": (), "cmdline": ""}, None) is False


def test_祖先を数えられる():
    me = os.getpid()
    anc = P.ancestors(me)
    assert anc, "祖先が 1 つも取れない"
    assert me not in anc
    assert P.ppid_of(me) == anc[0]


def test_起動時刻が壁時計として読める():
    """台帳の PID 再利用照合が依存する値。実プロセスで確かめる。"""
    import time
    t0 = time.time()
    p = subprocess.Popen(["sleep", "5"])
    try:
        st = P.start_epoch(p.pid)
        assert st is not None
        assert abs(st - t0) < 10, (st, t0)
    finally:
        p.kill(); p.wait(timeout=5)
    assert P.start_epoch(4194300) is None       # 居ない PID


def test_proc_tableはargvを持つ():
    rows = P.proc_table()
    assert rows and len(rows[0]) == 5
    assert isinstance(rows[0][4], tuple)


def test_基準の作業場は環境変数で決まる(monkeypatch, tmp_path):
    """★`cwd` 引数（モデルの手の中）ではなく `policy._workspace()` を基準にする。"""
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    assert P.owner_workspace() == os.path.realpath(str(tmp_path))


# ── 第4回c: 作業場の床（`serve._apply_env` は spec.workspace を無検証で渡す）──

def test_浅すぎる場所は作業場として受けない(monkeypatch, tmp_path):
    """★塞いだ穴（2026-09-05・第4回c・実測）: `serve._apply_env` は依頼の
    `spec.workspace` を**検証せずに** `LAPS_WORKSPACE` に入れて chdir する。
    `/` を渡されると `in_workspace` はホストのほぼ全プロセスを「中」と判定し
    （反証の実測 288 本中 278 本／再測 **260 本中 260 本＝全部**）、
    kill 系ゲートは絞る側から**撃つ許可**を出す側へ化ける
    （ゲートが在ることが、かえって「確認済み」の顔をする）。

    ★戻り値を空文字にしてはいけない: `bash._scope_*_kill` は `not workspace` で
      **何も見ずに素通し**するので、空にすると床が fail-open になる。
      実在し得ないパス（`NO_WORKSPACE`）を返して「何ひとつ中に無い」状態にする。
    """
    assert P.workspace_floor_ok("/") is False
    assert P.workspace_floor_ok("/root") is False
    assert P.workspace_floor_ok(os.path.expanduser("~")) is False
    assert P.workspace_floor_ok("/home/user/project") is True
    assert P.workspace_floor_ok(str(tmp_path)) is True

    monkeypatch.setenv("LAPS_WORKSPACE", "/")
    ws = P.canonical_workspace()
    assert ws == P.NO_WORKSPACE
    assert P.in_workspace({"pid": 1, "cwd": "/etc", "argv": (), "cmdline": ""}, ws) is False
    assert P.in_workspace({"pid": 2, "cwd": "/home/user/project", "argv": (),
                           "cmdline": ""}, ws) is False
    assert "作業場が設定されていません" in P.workspace_note()

    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    assert P.canonical_workspace() == os.path.realpath(str(tmp_path))
    assert P.workspace_note() == ""


def test_cdは実行される語のときだけ所有と読む():
    """★塞いだ穴（2026-09-05・第4回c）: `cd <ws>` の綴りを **argv のどの語でも**
    「作業場を土台にしている」と読むと、`grep -rn "cd <ws>" /etc` や
    `echo 'cd <ws>'` の**ただの引数**まで所有の証拠になる
    ——`pkill -f` が自分の argv に当たるのと同じ、字面で判定する族。
    `cd` が実際に走るのは「シェルが `-c` で受け取った語」の中だけ。"""
    sh = {"cwd": "/", "cmdline": "",
          "argv": ("/bin/sh", "-c", f"cd {WS} && php -S 0.0.0.0:18300 -t site/")}
    assert P.in_workspace(sh, WS) is True, "自分のサーバを取りこぼした（run50 の壁）"
    for argv in (("grep", "-rn", f"cd {WS}", "/etc"),
                 ("echo", f"cd {WS}"),
                 ("python3", "-c", f"print('cd {WS}')"),
                 ("rg", "--", f"cd {WS}")):
        row = {"cwd": "/", "cmdline": " ".join(argv), "argv": argv}
        assert P.in_workspace(row, WS) is False, argv
        # 列挙の材料にはなる（黙って落とさない）
        assert P.references_workspace(row, WS) is True, argv
    # ラッパ越しでも取りこぼさない
    assert P.in_workspace({"cwd": "/", "cmdline": "", "argv": (
        "timeout", "30", "bash", "-lc", f"cd {WS} && php -S :1")}, WS) is True
    assert P.shell_script_args(("php", "-S", ":1")) == []
    assert P.shell_script_args(("/bin/sh", "-c", "x")) == ["x"]
