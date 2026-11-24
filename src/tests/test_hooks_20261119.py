"""フック（engine/hooks.py・2026-11-19）の試験。偽の推論機を相手に本物の走行を回す（test_engine_20261030 の台を借りる）。"""
from __future__ import annotations

import json
import sys

from tests.test_engine_20261030 import env  # noqa: F401  （台）

PY = sys.executable


def _hooks(tmp_path, conf: dict) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "hooks.json").write_text(json.dumps({"hooks": conf}), encoding="utf-8")


def _script(tmp_path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return f'"{PY}" "{p}"'


def test_pre_tool_use_exit_2_blocks_the_call_and_tells_the_model(env, tmp_path):
    guard = _script(tmp_path, "guard.py", "import sys,json\nd=json.load(sys.stdin)\n"
                    "if 'secret' in d['tool_input'].get('file_path',''):\n  print('do not touch secret files', file=sys.stderr); sys.exit(2)\n")
    _hooks(tmp_path, {"PreToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": guard}]}]})
    a, fake, rec, ws = env([{"calls": [("Write", {"file_path": "secret.txt", "content": "x"})]}, {"text": "やめました。"}])
    assert a.run("secret.txt を作って") == "やめました。"
    assert not (ws / "secret.txt").exists()
    tool_msgs = [m for m in fake.bodies[1]["messages"] if m["role"] == "tool"]
    assert "do not touch secret files" in tool_msgs[-1]["content"] and a.counters.get("hook_blocked") == 1


def test_pre_tool_use_matcher_does_not_touch_other_tools(env, tmp_path):
    guard = _script(tmp_path, "guard.py", "import sys\nsys.exit(2)\n")
    _hooks(tmp_path, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": guard}]}]})
    a, fake, rec, ws = env([{"calls": [("Write", {"file_path": "ok.txt", "content": "x"})]}, {"text": "作りました。"}])
    a.run("ok.txt を作って")
    assert (ws / "ok.txt").read_text() == "x" and not a.counters.get("hook_blocked")


def test_post_tool_use_stdout_is_added_to_the_result(env, tmp_path):
    post = _script(tmp_path, "post.py", "import sys,json,os\nd=json.load(sys.stdin)\n"
                   "print('formatted ' + os.environ['LAPS_HOOK_FILE'] + ' ok=' + str(d['tool_response']['ok']))\n")
    _hooks(tmp_path, {"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": post}]}]})
    a, fake, rec, ws = env([{"calls": [("Write", {"file_path": "a.py", "content": "x=1"})]}, {"text": "書きました。"}])
    a.run("a.py を書いて")
    tool_msgs = [m for m in fake.bodies[1]["messages"] if m["role"] == "tool"]
    assert "[Hook] formatted a.py ok=True" in tool_msgs[-1]["content"]


def test_user_prompt_submit_can_add_context_or_stop(env, tmp_path):
    add = _script(tmp_path, "add.py", "import sys,json\nd=json.load(sys.stdin)\nprint(json.dumps({'additionalContext': 'TODAY IS FRIDAY'}))\n")
    _hooks(tmp_path, {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": add}]}]})
    a, fake, rec, ws = env([{"text": "金曜です。"}])
    a.run("今日は何曜日？")
    assert "TODAY IS FRIDAY" in json.dumps(fake.bodies[0]["messages"], ensure_ascii=False)

    stop = _script(tmp_path, "stop.py", "import sys\nprint('no deploys on friday', file=sys.stderr); sys.exit(2)\n")
    _hooks(tmp_path, {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": stop}]}]})
    import os
    os.utime(tmp_path / "home" / "hooks.json", (1, 1))      # 同じ秒に書き直しても読み直させる
    b, fake2, rec2, _ = env([{"text": "出しました。"}])
    out = b.run("本番に出して")
    assert "no deploys on friday" in out and b.outcome == "declined" and not fake2.bodies


def test_stop_hook_keeps_the_run_going_up_to_the_limit(env, tmp_path):
    flag = tmp_path / "tests_ok"
    stop = _script(tmp_path, "stop.py", f"import sys,os\nif not os.path.exists(r'{flag}'):\n  print('run the tests first', file=sys.stderr); sys.exit(2)\n")
    _hooks(tmp_path, {"Stop": [{"hooks": [{"type": "command", "command": stop}]}]})
    a, fake, rec, ws = env([{"text": "原因は X です。"},
                            {"calls": [("Bash", {"command": f"touch '{flag}'"})]},
                            {"text": "試験も通しました。原因は X です。"}])
    assert a.run("原因は何ですか？") == "試験も通しました。原因は X です。"
    assert "run the tests first" in json.dumps(fake.bodies[1]["messages"], ensure_ascii=False)
    assert a.counters.get("hook_stop_continue") == 1


def test_stop_hook_that_always_blocks_does_not_loop_forever(env, tmp_path):
    stop = _script(tmp_path, "stop.py", "import sys\nprint('never', file=sys.stderr); sys.exit(2)\n")
    _hooks(tmp_path, {"Stop": [{"hooks": [{"type": "command", "command": stop}]}]})
    a, fake, rec, ws = env([{"text": "答え 1"}, {"text": "答え 2"}, {"text": "答え 3"}, {"text": "答え 4"}, {"text": "答え 5"}])
    assert a.run("これは何ですか？") == "答え 4"
    assert a.counters.get("hook_stop_continue") == 3 and len(fake.bodies) == 4


def test_a_broken_hook_command_does_not_stop_the_work(env, tmp_path):
    _hooks(tmp_path, {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": f'"{PY}" -c "import sys; sys.exit(7)"'}]}]})
    a, fake, rec, ws = env([{"calls": [("Write", {"file_path": "b.txt", "content": "y"})]}, {"text": "作りました。"}])
    a.run("b.txt を作って")
    assert (ws / "b.txt").read_text() == "y"
    assert any("7 で終わった" in str(f.get("message")) for t, f in rec.events if t == "notice")


def test_laps_hooks_0_turns_everything_off(env, tmp_path, monkeypatch):
    _hooks(tmp_path, {"PreToolUse": [{"hooks": [{"type": "command", "command": f'"{PY}" -c "import sys; sys.exit(2)"'}]}]})
    monkeypatch.setenv("LAPS_HOOKS", "0")
    a, fake, rec, ws = env([{"calls": [("Write", {"file_path": "c.txt", "content": "z"})]}, {"text": "作りました。"}])
    a.run("c.txt を作って")
    assert (ws / "c.txt").read_text() == "z"
