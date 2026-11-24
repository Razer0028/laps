"""道具は自分の成否を自分で名乗る（中身の言葉で当てない）。

★2026-09-21 に実測で見つけた欠陥の回帰試験。

`as_result()` は型を名乗らない道具に対し `_FAILED_BODY` の語彙表で成否を推測する。
ところが Read と Glob は**成功経路が素の str** だったため、

    ・ごく普通のログ（本文に "Permission denied" の行がある）を読むと ok=False
    ・"No such file or directory" という名前のファイルが一覧に入ると Glob が失敗

になっていた。★**読んだ内容は観測であって、道具の成否ではない。**
ログ・障害記録・エラー集を読むほど失敗に見えるので、開発用途ほど当たる。

友人の実機（KOSEI）の 9 走行では道具 39 回中 Read が 25 回・Glob が 9 回で、
`tool_status_inferred` は 33（typed は 6）＝**85% が推測**だった。
"""
from __future__ import annotations

import os
import time

import pytest

from laps.tools import as_result
from laps.tools import glob as gl
from laps.tools import read as rd


@pytest.fixture()
def work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# 語彙表に載っている語を「中身」に持つ、まったく正常なファイル。
_TRAPS = (
    "Permission denied",
    "No such file or directory",
    "command not found",
    "アクセスが拒否されました",
    "指定されたパスが見つかりません",
)


@pytest.mark.parametrize("phrase", _TRAPS)
def test_reading_a_log_that_mentions_failure_is_not_a_failure(work, phrase):
    f = work / "app.log"
    f.write_text(f"2026-09-21 backup\n{phrase}\nretrying...\n", encoding="utf-8")
    r = as_result(rd.SPEC.handler({"file_path": "app.log"}))
    assert r.ok is True, f"{phrase!r} を含むログを読んだだけで失敗扱いになった"
    assert r.kind == "ok"
    assert r.inferred is False, "成功を型で名乗っていない（推測に委ねている）"


def test_reading_a_missing_file_is_still_a_failure(work):
    """★守りを緩めていないこと。本物の失敗は失敗のまま。"""
    r = as_result(rd.SPEC.handler({"file_path": "nope.txt"}))
    assert r.ok is False and r.kind == "error"


def test_glob_is_not_judged_by_the_names_it_lists(work):
    (work / "sub").mkdir()
    (work / "sub" / "No such file or directory.log").write_text("x", encoding="utf-8")
    r = as_result(gl.SPEC.handler({"pattern": "**/*.log"}))
    assert r.ok is True and r.inferred is False


def test_glob_zero_matches_is_empty_not_error(work):
    r = as_result(gl.SPEC.handler({"pattern": "*.nothing-here"}))
    assert r.ok is True and r.kind == "empty"


def test_bash_is_judged_by_its_exit_code_not_its_output(work):
    """★対照。bash は元から終了コードで名乗っている（直す必要が無かった）。

    出力に失敗の語が出ても、終了コードが 0 なら成功。
    """
    from laps.tools import bash as bs
    r = as_result(bs.SPEC.handler({"command": "echo Permission denied"}))
    assert r.ok is True, "終了コード 0 なのに出力の語で失敗扱いになった"
    assert r.inferred is False


# --------------------------------------------------------------------------
# 総当たりで見つけた残り 3 本（2026-09-21・★静的な数え方では見つからなかった）
# --------------------------------------------------------------------------
#
# ★`grep -c "ToolResult(" ` で数えると 16/20 が「使っている」ように見えた。
#   実際に叩いたら **失敗経路だけ型で、成功経路は素の str** という形だった。
#   ⇒ 「使っている」と「全経路で名乗っている」は別。★数えずに呼ぶこと。


@pytest.mark.parametrize("pattern", ["denied", "Permission denied"])
def test_grep_for_an_error_word_is_not_a_failed_grep(work, pattern):
    """★いちばん確実に当たる型。**探した語が必ず出力に入る。**

    `Grep(pattern="denied")` ＝ 権限の問題をログから探す、という開発者が
    まさにやる操作が「道具が失敗した」ことになっていた。
    """
    (work / "srv.log").write_text("2026-09-21 backup\nPermission denied\n", encoding="utf-8")
    from laps.tools import grep as gp
    r = as_result(gp.SPEC.handler({"pattern": pattern, "path": "."}))
    assert r.ok is True, f"{pattern!r} を探したら Grep が失敗扱いになった"
    assert r.inferred is False


def test_system_state_names_its_own_exit_code(work):
    """★この道具は `exit={returncode}` を**本文に書いている**のに型では黙っていた。"""
    from laps.tools import system_state as ss
    r = as_result(ss.SPEC.handler({"verb": "disk"}))
    assert r.ok is True and r.inferred is False


def test_write_is_not_judged_by_the_path_it_wrote(work):
    """成功した Write が ok=False になると、agent の `files_written` に載らない
    ＝停滞判定と「宣言して止まった」の入力が欠ける。"""
    from laps.tools import write as wr
    r = as_result(wr.SPEC.handler(
        {"file_path": "No such file or directory.txt", "content": "x"}))
    assert r.ok is True and r.inferred is False


def test_every_read_only_tool_names_its_success(work):
    """★総当たりの杭。新しい道具を足した人がここで気づけるように。

    破壊的・対外的なもの（RemoteShell / Task / AskUser / WebFetch / WebSearch /
    Umbra / RemoteRead）は呼ばない。
    """
    import importlib
    (work / "a.txt").write_text("alpha\n", encoding="utf-8")
    os.environ.setdefault("LAPS_SESSION_ID", "test-status-audit")
    cases = [
        ("read", {"file_path": "a.txt"}),
        ("glob", {"pattern": "*.txt"}),
        ("grep", {"pattern": "alpha", "path": "."}),
        ("bash", {"command": "echo hi"}),
        ("write", {"file_path": "n.txt", "content": "x\n"}),
        ("system_state", {"verb": "disk"}),
        ("todowrite", {"todos": [{"content": "t", "status": "pending",
                                  "activeForm": "doing t"}]}),
    ]
    guessed = []
    for mod_name, inp in cases:
        mod = importlib.import_module("laps.tools." + mod_name)
        r = as_result(mod.SPEC.handler(inp))
        if r.inferred:
            guessed.append(f"{mod.SPEC.name}(ok={r.ok},kind={r.kind})")
    assert not guessed, ("成功経路で型を名乗っていない道具: " + ", ".join(guessed)
                         + "\n→ 素の str を返すと `as_result` が**出力の中身**で"
                           " 成否を当てにいく。道具は自分の成否を知っている。")


# --------------------------------------------------------------------------
# 対外的・特殊な 3 本（2026-09-21・総当たりから外していた分）
# --------------------------------------------------------------------------

def test_bash_output_is_not_judged_by_the_background_processs_stderr(work, monkeypatch):
    """★本文に**背景プロセスの stderr がそのまま入る**ので特に当たる。

    ビルドや試験が `command not found` を吐いた瞬間に
    「BashOutput が失敗した」ことになっていた。
    ★この道具の仕事は出力を読んで見せることで、読めたなら成功。
    """
    from laps.tools import bash as bs, bash_output as bo
    started = str(bs.SPEC.handler(
        {"command": "echo 'bash: nope: command not found' 1>&2; echo done",
         "run_in_background": True}))
    import re
    m = re.search(r"\b(\d{2,})\b", started)
    if not m:                      # 背景実行に対応していない構成なら、そこは測らない
        pytest.skip(f"背景実行の pid を読めない: {started[:80]!r}")
    for _ in range(50):
        out = bo.SPEC.handler({"bash_id": m.group(1)})
        if "done" in str(out) or "command not found" in str(out):
            break
        time.sleep(0.1)
    r = as_result(out)
    assert r.ok is True, "背景プロセスの stderr で BashOutput が失敗扱いになった"
    assert r.inferred is False


def test_notebook_edit_names_its_success(work):
    import json
    nb = {"cells": [{"cell_type": "code", "source": ["print(1)\n"], "metadata": {},
                     "outputs": [], "execution_count": None}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    (work / "n.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    from laps.tools import notebook_edit as ne
    r = as_result(ne.SPEC.handler({"notebook_path": "n.ipynb", "mode": "replace",
                                   "cell_index": 0,
                                   "new_source": "print('Permission denied')"}))
    assert r.ok is True and r.inferred is False


def test_webfetch_is_not_judged_by_the_page_it_fetched(work, monkeypatch):
    """★開発者が読むのは「`command not found` の直し方」のようなページなので、
    **役に立つ取得ほど失敗に見える**。

    ★外へは出ない: 自分のループバックに立てた試験ページだけを取る。
    非公開アドレスの遮断（SSRF 対策）は道具が用意している正規の口で開ける。
    """
    import functools, http.server, threading
    page = work / "page.html"
    page.write_text("<html><body><h1>bash: command not found を直す</h1>"
                    "<p>Permission denied が出たら PATH を確認します。</p>"
                    "</body></html>", encoding="utf-8")
    monkeypatch.setenv("LAPS_ALLOW_PRIVATE_FETCH", "1")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(work))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from laps.tools import webfetch as wf
        out = wf.SPEC.handler({"url": f"http://127.0.0.1:{srv.server_address[1]}/page.html"})
    finally:
        srv.shutdown()
    r = as_result(out)
    assert "command not found" in str(out), "罠の語がページに入っていない＝試験が無意味"
    assert r.ok is True and r.inferred is False
