"""2026-10-31 課題集の走行記録から直した物（実物の本文・関数を 1 回ずつ呼ぶ）。"""
from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from laps.engine import compact, finish
from laps.oc import evidence, hostfacts, hostload, reading

UMBRA_ANSWER = "Edit呼び出しは**816回**あり、そのうち**238回**が失敗しました（失敗率29.2%）。doc_id=994 に書いてあります。"
FINDFUNC_ANSWER = ("設定を保存する関数は app/io/storage.py の persist_prefs です。この関数は ConfigParser を使用して設定を書き込みます。")


def test_an_explanation_in_an_answer_is_not_an_announcement():
    assert reading.unfinished(FINDFUNC_ANSWER, asked=True) == ""
    assert reading.unfinished("この関数は設定を書き込みます。", asked=True) == ""
    assert reading.unfinished(FINDFUNC_ANSWER) == "announce"      # 作業の依頼では今までどおり


@pytest.mark.parametrize("body,why", [
    ("まず README を読みます。", "announce"),
    ("次に設定ファイルを確認します。", "announce"),
    ("README の中身を確認します。", "announce"),
    ("調べ終わっていません。続きを調べます。", "announce"),
    ("テストの結果を確認しました。失敗が 3 件あります。原因を調査します。", "announce"),
    ("ログを読みました。原因はまだ特定できていません。設定ファイルも読みます。", "announce"),
    ("ファイルを開こうとして失敗しました。別の方法で読みます。", "announce"),
    ("調査はまだ途中です。残りのモジュールも確認が残っています。", "remaining"),
    ("調査はまだ途中です。", "remaining"),
    ("I haven't finished yet; the remaining files still need to be read.", "remaining"),
    ("I read the config but found nothing yet. Still investigating.", "remaining"),
])
def test_a_stopped_investigation_is_still_pushed_on_a_question(body, why):
    assert reading.unfinished(body, asked=True) == why


def _failing(ok, request="ログを調べて、失敗の数を教えて"):
    f, a = _finish(request=request)
    f.aids = types.SimpleNamespace(last_ok=ok)
    return f, a


def test_a_reported_failure_count_is_not_pushed_after_a_good_tool():
    f, _a = _failing(True)
    assert f.unfinished(UMBRA_ANSWER) == ("", False)


def test_a_failure_with_a_failing_tool_is_still_pushed():
    f, _a = _failing(False)
    assert f.unfinished(UMBRA_ANSWER)[0]
    f, _a = _failing(True, request="X を直して")                  # 作業の依頼は今までどおり
    assert f.unfinished("テストを実行しましたが失敗しました。")[0]


@pytest.mark.parametrize("req", ["この機能を実装してもらえる？", "README を更新してもらえますか", "リファクタリングしてもらえますか？",
                                 "このバグ直せますか？", "デプロイしてくれる？", "Could you update the README?", "Can you rename foo to bar?"])
def test_a_request_for_work_is_not_a_question(req):
    from laps.engine import endgate
    assert not endgate.only_question(req)


def test_a_real_question_stays_a_question():
    from laps.engine import endgate
    for req in ("いつ更新されましたか？", "最新のビルドはどれ？", "なぜ落ちたか教えて"):
        assert endgate.only_question(req), req


def _finish(request="X を直して", tool_calls=9, kept=""):
    a = types.SimpleNamespace(request=request, tool_calls=tool_calls, tool_counts={}, effects=0, rid="r", last_fold=False, counts=[],
                              out=types.SimpleNamespace(notice=lambda *a, **k: None), _ja=lambda: True, lang=lambda: "Japanese")
    a._count = lambda k, n=1: a.counts.append(k)
    f = finish.Finish(a, None)
    f.kept = kept
    return f, a


def test_a_short_answer_to_a_question_is_not_sent_back():
    f, a = _finish(request="いちばん多いのはどれですか？")
    assert f.weak_report("cli-027 28") == ""
    assert "final_report" not in a.counts


@pytest.mark.parametrize("body", ["完了。", "調べました。", "以上です。", "Done."])
def test_an_empty_short_answer_to_a_question_is_sent_back(body):
    f, a = _finish(request="ログを調べてなぜ落ちたか教えて")
    assert f.weak_report(body)
    assert "final_report:too_short" in a.counts


def test_a_short_answer_to_a_task_is_still_sent_back():
    f, a = _finish(request="X を直して")
    assert f.weak_report("直しました。")
    assert "final_report:too_short" in a.counts


def test_a_review_list_is_added_to_the_report_not_replacing_it():
    kept = "shop/users.py の get_user を find_user に変えました。呼び出し元の 3 か所も直して、試験が通りました。" * 3
    review = "\n".join(["✓ shop/users.py を変えた", "✓ orders.py を直した", "✓ report.py を直した", "✓ 試験が通った"] * 8)
    f, _a = _finish(kept=kept)
    merged = f.merge(review)
    assert kept in merged and review in merged


def test_a_new_review_list_replaces_the_old_list():
    report = "shop/users.py の get_user を find_user に変えました。呼び出し元の 3 か所も直して、試験が通りました。"
    l1, l2 = "✗ README が古い\n✓ 試験が通った", "✓ README を直した\n✓ 試験が通った\n✓ 別のフォルダでも動いた"
    f, _a = _finish(kept=f"{report}\n\n{l1}")
    merged = f.merge(l2)
    assert report in merged and l2 in merged and "✗ README が古い" not in merged
    f, _a = _finish(kept=l1)                                       # 一覧そのものが答えなら置き換えるだけ
    assert f.merge(l2) == l2


def test_a_rewritten_report_still_replaces_the_old_one():
    kept = "shop/users.py の get_user を find_user に変えました。" * 3
    new = "書き直しました。" * 40
    f, _a = _finish(kept=kept)
    assert f.merge(new) == new


def test_an_abbreviated_path_is_not_checked():
    ans = "鍵は `/work/.../.keys/x.txt` に保存しました。ファイルは /srv/x/…/y.txt に保存しました。"
    assert evidence.claimed_paths(ans, "/tmp") == []


def test_a_real_path_is_still_checked():
    assert ("/srv/x/y.txt", False) in evidence.claimed_paths("結果は `/srv/x/y.txt` に保存しました。", "/tmp")


@pytest.fixture
def no_pytest(monkeypatch):
    monkeypatch.setattr(hostfacts, "_has_pytest", lambda py: False)


def test_unittest_tests_get_a_command_that_runs(tmp_path, no_pytest):
    (tmp_path / "tests").mkdir()
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(add(1, 2), 3)\n")
    out = hostfacts.project_commands(str(tmp_path))
    assert "-m unittest discover -s tests`" in out and "pytest" not in out
    cmd = out.split("`")[1].split()
    r = subprocess.run([sys.executable, *cmd[1:]], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "Ran 1 test" in r.stderr


def test_unittest_tests_in_a_package_run_too(tmp_path, no_pytest):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(add(1, 2), 3)\n")
    out = hostfacts.project_commands(str(tmp_path))
    cmd = out.split("`")[1].split()
    assert cmd[-2:] == ["-t", "."]
    r = subprocess.run([sys.executable, *cmd[1:]], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "Ran 1 test" in r.stderr


def test_mock_alone_is_not_unittest_style(tmp_path, no_pytest):
    (tmp_path / "test_x.py").write_text("from unittest.mock import patch\ndef test_a():\n    assert True\n")
    assert "pytest is not installed" in hostfacts.project_commands(str(tmp_path))


def test_underscore_test_names_get_a_pattern_that_finds_them(tmp_path, no_pytest):
    (tmp_path / "calc_test.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n")
    out = hostfacts.project_commands(str(tmp_path))
    assert "-p '*_test.py'" in out
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-p", "*_test.py"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "Ran 1 test" in r.stderr


def test_has_pytest_is_not_fooled_by_the_harness_environment(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/nonexistent")              # Bash の道具は外すので、試験は実際には動く
    assert hostfacts._has_pytest(sys.executable) is True


def test_an_interpreter_that_cannot_start_is_not_read_as_no_pytest(tmp_path):
    bad = tmp_path / "py"
    bad.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    bad.chmod(0o755)
    assert hostfacts._has_pytest(str(bad)) is True


def test_no_pytest_is_read_from_a_clean_exit(tmp_path):
    bad = tmp_path / "py"
    bad.write_text("#!/bin/sh\nexit 1\n")
    bad.chmod(0o755)
    assert hostfacts._has_pytest(str(bad)) is False


def test_pytest_style_tests_say_it_is_missing(tmp_path, no_pytest):
    (tmp_path / "test_x.py").write_text("def test_a():\n    assert True\n")
    assert "pytest is not installed" in hostfacts.project_commands(str(tmp_path))


def test_pytest_is_named_when_it_is_there(tmp_path, monkeypatch):
    monkeypatch.setattr(hostfacts, "_has_pytest", lambda py: True)
    (tmp_path / "test_x.py").write_text("import unittest\n")
    assert "-m pytest -q`" in hostfacts.project_commands(str(tmp_path))


def test_has_pytest_asks_the_interpreter():
    assert hostfacts._has_pytest(sys.executable) is True          # この試験は pytest で走っている
    assert hostfacts._has_pytest("/nonexistent/python") is True   # 確かめられないときは今までどおり


def test_file_cache_is_read_from_memory_stat():
    assert hostload._file_cache("anon 100\nfile 900\ninactive_file 500\nactive_file 300\nslab 7\n") == 800
    assert hostload._file_cache("") == 0


def test_summary_input_keeps_what_was_only_in_the_thinking():
    long = "最初の結論は worker-02 は 3 本。" + "あ" * 2000 + "最後に worker-10 のエラー数は 3。"
    msgs = [{"role": "assistant", "content": "", "reasoning_content": long,
             "tool_calls": [{"function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "assistant", "content": "答え", "reasoning_content": "道具を呼ばない手番の思考"}]
    s = compact._serialize(msgs)
    assert "[Assistant thinking, unverified]" in s
    assert "worker-02 は 3 本" in s and "worker-10 のエラー数は 3" in s
    assert "道具を呼ばない手番の思考" not in s
    assert len(s) < 1000


def test_a_short_thought_is_kept_whole():
    assert compact._thought("  worker-10 は 3。 ") == "worker-10 は 3。"


def test_first_heading_only_reads_the_first_line():
    from laps.oc import docmake
    assert docmake._first_heading("\n# 価格表\n\n本文") == "価格表"
    assert docmake._first_heading("## 価格表 ##") == "価格表"
    assert docmake._first_heading("# C#") == "C#"
    assert docmake._first_heading("本文\n# 価格表") == ""
    assert docmake._first_heading(None) == ""


def test_the_docx_does_not_repeat_its_title(tmp_path):
    pytest.importorskip("docx")
    from laps.oc import docmake
    import docx
    p = tmp_path / "a.docx"
    docmake.build_docx({"path": str(p), "title": "価格表", "content": "# 価格表\n\n| item | price |\n|---|---|\n| a | 1 |\n"})
    d = docx.Document(str(p))
    assert [x.text for x in d.paragraphs if x.style.name in ("Title", "Heading 1")] == ["価格表"]
    q = tmp_path / "b.docx"
    docmake.build_docx({"path": str(q), "title": "価格表", "content": "# 別の見出し\n\n本文\n"})
    d = docx.Document(str(q))
    assert [x.text for x in d.paragraphs if x.style.name in ("Title", "Heading 1")] == ["価格表", "別の見出し"]
    r = tmp_path / "c.docx"
    docmake.build_docx({"path": str(r), "title": "価格表", "toc": True, "content": "# 価格表\n\n本文\n"})
    d = docx.Document(str(r))
    assert d.paragraphs[0].style.name == "Title" and d.paragraphs[0].text == "価格表"   # 目次があるときは表紙に題名を残す


def _ctx(tmp_path):
    import threading
    from pathlib import Path

    from laps.engine.tools import Ctx
    from laps.engine.tools.shell import Jobs
    return Ctx(workspace=str(tmp_path), root=Path(tmp_path), cancel=threading.Event(), preempt=threading.Event(), jobs=Jobs())


def _job(ctx, command):
    from laps.engine.tools import shell
    shell.BASH.run({"command": command, "run_in_background": True}, ctx)
    return next(iter(ctx.jobs.items))


def test_a_second_check_of_a_job_says_it_was_held(tmp_path, monkeypatch):
    from laps.engine.tools import shell
    monkeypatch.setattr(shell, "POLL_WAITS", (2.0,))
    ctx = _ctx(tmp_path)
    jid = _job(ctx, "sleep 30")
    try:
        assert "held" not in shell.BASH_OUTPUT.run({"id": jid, "wait": 0.1}, ctx).output
        assert "held 2s" in shell.BASH_OUTPUT.run({"id": jid, "wait": 0.1}, ctx).output
    finally:
        shell.BASH_OUTPUT.run({"id": jid, "kill": True}, ctx)


def test_a_server_gets_only_a_short_floor(tmp_path, monkeypatch):
    import time

    from laps.engine.tools import shell
    monkeypatch.setattr(shell, "POLL_WAITS", (20.0,))
    monkeypatch.setattr(shell, "SERVER_POLL_WAIT", 1.0)
    ctx = _ctx(tmp_path)
    jid = _job(ctx, "echo 'Serving HTTP on 0.0.0.0 port 8000'; sleep 30")
    try:
        time.sleep(0.5)
        shell.BASH_OUTPUT.run({"id": jid, "wait": 0.1}, ctx)
        t0 = time.monotonic()
        shell.BASH_OUTPUT.run({"id": jid, "wait": 0.1}, ctx)
        assert 0.8 < time.monotonic() - t0 < 5                    # 待ちなしの繰り返しにはならず、20 秒も止まらない
    finally:
        shell.BASH_OUTPUT.run({"id": jid, "kill": True}, ctx)


def test_a_child_summary_reaches_the_record():
    from laps.engine.loop import ChildOut
    seen = []
    out = types.SimpleNamespace(notice=lambda kind, rid, **f: seen.append((kind, f)))
    c = ChildOut(out, "r", "explore")
    c.notice("context_compacted", "x", message="Context summarized (39,096 → 6,025 tokens)")
    c.notice("heartbeat", "x", message="…")
    assert len(seen) == 1 and seen[0][0] == "child_compacted" and "39,096" in seen[0][1]["message"]   # 答えの下に残る subagent にしない


def test_progress_comes_first_in_the_schema():
    from laps.engine.loop import PROGRESS_FIELD
    from laps.engine.tools import base_tools
    for t in base_tools("full").values():
        p = t.schema(PROGRESS_FIELD)["function"]["parameters"]
        assert list(p["properties"])[0] == "progress" and p["required"][0] == "progress", t.name
        q = t.schema(None)["function"]["parameters"]
        assert "progress" not in q["properties"] and "progress" not in q["required"], t.name


