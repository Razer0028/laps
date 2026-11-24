"""★2026-11-12 Ask モードで Web 検索以外の道具も使う（オーナー「askモードのときにWeb検索以外もagentモードと同じように
使えるようにして。ただし、agentモードの時のようにタスクを実行するのではなく質問に答えるためにツールを実行させる」）。

新しい中核（blackwall/engine）の toolset="chat"。旧い中核の CHAT_TOOLS（tests/test_chat_mode.py）は変えていない。
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

from laps import policy
from laps.engine import prompt
from laps.engine.tools import Ctx, base_tools, shell


def test_ask_tools_read_but_do_not_write():
    tools = set(base_tools("chat"))
    assert {"Read", "Grep", "Glob", "Bash", "BashOutput", "WebSearch", "WebFetch", "AskUserQuestion"} <= tools
    assert not tools & {"Write", "Edit", "ApplyPatch", "TodoWrite", "Remember", "Task", "Tools", "Skill", "Construct"}


def test_ask_prompt_is_for_answering():
    s = prompt.system(os.getcwd(), "m", "bash", toolset="chat")
    assert s.startswith(prompt.ASK) and "Scratch folder" not in s and "<env>" in s
    assert prompt.system(os.getcwd(), "m", "bash", toolset="full").startswith(prompt.BASE.replace("{PARALLEL}", prompt._parallel()).replace("{BG}", prompt._bg()))


def _ctx(tmp_path):
    return Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(),
               jobs=shell.Jobs(), answer_only=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のシェルで試す")
def test_ask_bash_runs_only_looking_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    ctx = _ctx(tmp_path)
    r = shell.bash({"command": "cat a.txt && ls"}, ctx)
    assert r.ok and "hello" in r.output
    for cmd in ("echo x > b.txt", "rm a.txt", "touch c.txt",
                "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: d.txt\n+x\n*** End Patch\nEOF"):
        r = shell.bash({"command": cmd}, ctx)
        assert not r.ok and "Not run in Ask mode" in r.output, cmd
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "bwhome") == ["a.txt"]   # bwhome は試験の器が作る
    ctx.answer_only = False                       # Agent モードは今までどおり
    assert shell.bash({"command": "echo x > b.txt"}, ctx).ok


def test_answer_check_does_not_widen_or_leak(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    assert policy.check_answer_command("ssh p40 nvidia-smi", cwd=str(tmp_path)).allowed
    assert not policy.check_answer_command("ssh p40 'systemctl restart x'", cwd=str(tmp_path)).allowed
    assert policy._mode() == "trusted" and policy.check_command("rm -rf build", cwd=str(tmp_path)).allowed
    monkeypatch.setenv("LAPS_POLICY", "workspace")         # もとの設定より緩めない
    assert not policy.check_answer_command("ssh p40 nvidia-smi", cwd=str(tmp_path)).allowed
    assert not policy.check_answer_command("rm -rf build", cwd=str(tmp_path)).allowed


def test_parallel_hint_switch(monkeypatch):
    """★2026-11-20 道具を同じ手番でまとめて呼ぶ一言（強めた形が既定・LAPS_PARALLEL_HINT=0 で旧の 1 行）。"""
    import os
    from laps.engine import prompt
    monkeypatch.delenv("LAPS_PARALLEL_HINT", raising=False)
    s = prompt.system(os.getcwd(), "m", "bash", toolset="full")
    assert "Batch independent calls" in s and "{PARALLEL}" not in s
    monkeypatch.setenv("LAPS_PARALLEL_HINT", "0")
    s = prompt.system(os.getcwd(), "m", "bash", toolset="full")
    assert "Make independent calls in the same turn" in s and "Batch independent calls" not in s
