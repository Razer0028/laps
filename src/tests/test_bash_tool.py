

def test_無言のサーバに入力待ちと言わない():
    """★2026-09-01: 打ち切りの助言が正反対になる場面。

    nginx / php-fpm / start.sh は**起動時に何も出力しない**ので、
    「何も出ない＝入力待ち」と読むと `</dev/null` を勧めることになる。
    それはサーバ起動には無意味で、この現場では**間違った助言は無い方がまし**。
    出力では区別できないので、コマンドの形で分ける。
    """
    from laps.tools.bash import _timeout_hint

    server = _timeout_hint("bash start.sh", "")
    assert "入力待ち" not in server, "サーバ起動に「入力待ち」と言っている"
    # ★2026-09-05: 助言は `setsid nohup … &` から「run_in_background=true か 末尾の &」へ
    #   変えた。`_wait_or_release` が**シェルの終了**で制御を返すようになり、
    #   出力を逃がさない裸の & でも待たされなくなったため（`&` を勧めても嘘にならない）。
    assert "背景" in server and "run_in_background" in server, "背景へ回す道を示していない"

    for cmd in ("nginx -g 'daemon off;'", "php-fpm -F", "php -S 0.0.0.0:18300"):
        h = _timeout_hint(cmd, "")
        assert "入力待ち" not in h, f"{cmd} に「入力待ち」と言っている"

    # 逆側: 本当に入力待ちらしい形では、従来どおり入力待ちを疑う
    stdin_wait = _timeout_hint("apt install foo", "")
    assert "入力待ち" in stdin_wait


def test_前面サーバは出力からも拾える():
    """PHP 組み込みサーバは "Development Server ... started" と言う。従来の経路。"""
    from laps.tools.bash import _timeout_hint

    h = _timeout_hint("bash start.sh",
                      "PHP 8.4.0 Development Server (http://0.0.0.0:18300) started")
    assert "前面で起動して戻ってきていません" in h
    # ★2026-09-05 訂正: 旧文は「失敗ではありません」と言っていたが、`_kill_group()` は
    #   グループごと SIGKILL している＝**サーバは死んでいる**。事実を言うこと。
    assert "強制終了" in h


def test_命令文をargvに置かない(monkeypatch):
    """★2026-09-01・実機で 9/9 = 100% だった欠陥を構造で消す。

    `bash -c "<命令文>"` はその文**全体**をシェルの argv に持つ。だから
    `pkill -f php` は走らせている当のシェルに必ず一致して**自分を撃つ**。
    実機の記録では、居残りサーバを止めようとした 9 回**すべて**が exit -15
    （自分のシェルを殺した）で、1 回もサーバを止められていない。

    ★見るのは「argv に命令文が無いこと」。実際に pkill を走らせて確かめるのは
      この機械では危険（試験が自分の走行を撃つ）。
    """
    from laps.tools.bash import _spawn_args

    args, kw = _spawn_args("pkill -f php-fpm-marker")
    flat = " ".join(str(a) for a in (args[0] if isinstance(args[0], list) else args))
    assert "php-fpm-marker" not in flat, f"命令文が argv に残っている: {flat}"
    assert kw.get("shell") is False, "shell=True だと文が argv に載る"
    script = kw.get("_bw_script")
    assert script, "台本の受け渡しが無い（後片付けできない）"
    import pathlib
    assert "pkill -f php-fpm-marker" in pathlib.Path(script).read_text(encoding="utf-8")
    pathlib.Path(script).unlink(missing_ok=True)


def test_台本は実行後に残らない():
    """★一時ファイルを残すと作業場が汚れ、次の走行の測定を壊す（この現場の常習）。"""
    import glob
    import tempfile

    from laps.tools import bash as B

    before = set(glob.glob(tempfile.gettempdir() + "/bwcmd-*.sh"))
    B.SPEC.handler({"command": "echo ok", "timeout": 20})
    after = set(glob.glob(tempfile.gettempdir() + "/bwcmd-*.sh"))
    assert after <= before, f"台本が残っている: {sorted(after - before)}"


def test_ファイル渡しでも普通の命令は動く(monkeypatch):
    """★構造を変えたので、当たり前のことが当たり前に動くかを見る。

    ★policy を明示する: 試験の既定は workspace で `(` が弾かれる。出荷既定は
      trusted（オーナーの意図的判断）なので、そちらの姿で確かめる。
    """
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    from laps.tools import bash as B

    assert "hello" in str(B.SPEC.handler({"command": "echo hello", "timeout": 20}))
    assert "1\n2" in str(B.SPEC.handler({"command": "printf '1\\n2\\n'", "timeout": 20}))
    # bash の機能（配列・[[ ]]）が効くこと＝dash に落ちていない
    out = str(B.SPEC.handler({"command": "a=(x y z); [[ ${#a[@]} == 3 ]] && echo bash-ok",
                              "timeout": 20}))
    assert "bash-ok" in out, out[:200]


def test_畳んでも失敗の理由は消さない():
    """★2026-09-01・run44 の解剖で見つけた実害。

    工程4 は 3 試行 3 時間すべてが `json.tool` の 1 本で落ちたが、その原因である
    PHP の `Undefined array key` を**モデルは 4 回しか見ていない**。同じ出力を
    畳んだ 39 件は 0 件が警告を含まず、**末尾（＝正しい JSON）だけ**を見せていた。
    ＝同じコマンドを 197 回打ったのは、直す手がかりを道具が隠したから。
    文脈を節約する仕組みが、節約してはいけないもの（失敗の理由）まで削っていた。
    """
    from laps.tools import bash as B

    B._reset_repeat_memory()
    body = ("Warning: Undefined array key \"artist\" in /site/api/works.php on line 22\n"
            + "x" * 400 + "\n"
            + '[{"id":1,"title":"work"}]')
    cmd, cwd = "curl -s http://127.0.0.1:18300/api/works", "/tmp"
    first = B._fold_repeat(cmd, cwd, body, body)
    assert "Undefined array key" in first, "初回は全文（畳まない）"

    second = B._fold_repeat(cmd, cwd, body, body)
    assert "前回と同じ出力" in second, "2 回目は畳む（節約は残す）"
    assert "Undefined array key" in second, \
        "★失敗の理由が消えている（モデルは直せない・197 回ループの原因）"
    assert '[{"id":1' in second, "末尾も残す（何が返ったかは分かるべき）"


def test_失敗の理由が無いときは従来どおり短く():
    """★理由が無い出力まで長くしない（削るつもりで増やさない）。"""
    from laps.tools import bash as B

    B._reset_repeat_memory()
    body = "\n".join(f"row {i}" for i in range(80))
    cmd, cwd = "ls -la /var/log", "/tmp"
    B._fold_repeat(cmd, cwd, body, body)
    second = B._fold_repeat(cmd, cwd, body, body)
    assert "前回と同じ出力" in second
    assert "★" not in second, "理由が無いのに理由の欄を出している"
    assert len(second) < len(body), "畳んだのに元より長い"
