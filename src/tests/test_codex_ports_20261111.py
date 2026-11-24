"""★2026-11-11 Codex から持ち込んだ物（オーナー「12以外導入していいです」）。

入力を開けた命令へ答えを送る（Bash の stdin・BashOutput の send）／命令へ渡す環境から鍵らしい変数を外す／
Edit の字形をそろえる段／要約で利用者の発言を原文で残す／止められた依頼の印。
"""
from __future__ import annotations

import re
import sys
import threading

import pytest

from laps.engine import compact
from laps.engine.tools import Ctx, edit_match, shell


def _ctx(tmp_path):
    return Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のシェルで試す")
def test_stdin_job_takes_answers(tmp_path):
    ctx = _ctx(tmp_path)
    prog = tmp_path / "ask.py"
    prog.write_text("name = input('your name? ')\nprint('hello ' + name, flush=True)\n"
                    "again = input('again [y/n] ')\nprint('bye' if again == 'n' else 'more', flush=True)\n")
    r = shell.bash({"command": f'"{sys.executable}" -u "{prog}"', "stdin": True}, ctx)
    jid = re.search(r"\[(bg\d+) ", r.output).group(1)
    assert "your name?" in r.output and "running with stdin open" in r.output
    r = shell.bash_output({"id": jid, "send": "Blackwall"}, ctx)
    assert "hello Blackwall" in r.output
    r = shell.bash_output({"id": jid, "send": "n", "wait": 5}, ctx)
    assert "bye" in r.output and "finished with exit code 0" in r.output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のシェルで試す")
def test_send_needs_stdin_open(tmp_path):
    ctx = _ctx(tmp_path)
    r = shell.bash({"command": "sleep 5", "run_in_background": True}, ctx)
    jid = re.search(r"as (bg\d+)", r.output).group(1)
    r = shell.bash_output({"id": jid, "send": "y"}, ctx)
    assert not r.ok and "stdin=true" in r.output
    shell.bash_output({"id": jid, "kill": True}, ctx)


def test_child_env_drops_secret_names(monkeypatch):
    monkeypatch.setenv("LAPS_GATEWAY_TOKEN", "x")
    monkeypatch.setenv("SOME_API_KEY", "x")
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("LAPS_KEEP_ENV", "gh_token")
    env = shell.child_env()
    assert "LAPS_GATEWAY_TOKEN" not in env and "SOME_API_KEY" not in env
    assert env["GH_TOKEN"] == "x" and env["NO_COLOR"] == "1" and env["TERM"] == "dumb"
    monkeypatch.setenv("LAPS_ENV_SCRUB", "0")
    assert "SOME_API_KEY" in shell.child_env()


def test_edit_matches_typographic_drift():
    text = "say(“hello”) — done\n"
    assert edit_match.replace(text, 'say("hello") - done', "x") == "x\n"


def test_summary_keeps_earlier_user_words():
    raw = [{"role": "user", "content": "最初の依頼"},
           {"role": "assistant", "content": "ok"},
           {"role": "user", "content": "[The user sent this while you were working]\nポートは 8080 にして\n\n(The request you were working on: 「…」. rest)"},
           {"role": "user", "content": "memory block", "bw_note": True},
           {"role": "assistant", "content": "ok"}]
    words = compact._user_words(raw, len(raw), "最初の依頼")
    assert "ポートは 8080 にして" in words and "最初の依頼" not in words and "memory block" not in words


def test_undo_restores_edits_and_skips_later_changes(tmp_path):
    from laps.engine import undo
    from laps.engine.tools import files
    ws = tmp_path / "ws"
    ws.mkdir()
    conv = tmp_path / "conv"
    (ws / "a.py").write_text("x = 1\ny = 2\n")
    (ws / "b.py").write_text("keep\n")
    ctx = Ctx(workspace=str(ws), root=conv, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    ctx.changes = undo.begin(conv, "r1")
    files.read({"file_path": "a.py"}, ctx)
    files.read({"file_path": "b.py"}, ctx)
    assert files.edit({"file_path": "a.py", "old_string": "y = 2", "new_string": "y = 3\nz = 4"}, ctx).ok
    assert files.edit({"file_path": "b.py", "old_string": "keep", "new_string": "changed"}, ctx).ok
    assert files.write({"file_path": "new.txt", "content": "hello\n"}, ctx).ok
    changed = {c["path"].rsplit("/", 1)[-1]: c for c in ctx.changes.finish()}
    assert changed["a.py"]["status"] == "modified" and (changed["a.py"]["added"], changed["a.py"]["removed"]) == (2, 1)
    assert changed["new.txt"]["status"] == "added"
    assert "/undo" in undo.summary(list(changed.values()), False)
    (ws / "b.py").write_text("the user edited it later\n")     # 依頼の後に変わった＝戻さない
    rc = undo.last_undoable(conv)
    assert rc is not None and rc.rid == "r1"
    res = rc.undo()
    assert (ws / "a.py").read_text() == "x = 1\ny = 2\n"
    assert not (ws / "new.txt").exists()
    assert (ws / "b.py").read_text() == "the user edited it later\n"
    assert any(p.endswith("b.py") for p, _ in res["skipped"])
    assert undo.last_undoable(conv) is None                      # 2 回目は戻す物が無い
    assert "restored" in undo.model_note(res)


def test_undo_command_words():
    from laps.engine import undo
    assert undo.is_command(" /undo ") and undo.is_command("/取り消し") and not undo.is_command("undo the last change")


def test_serve_undo_command_runs_without_the_model(tmp_path, monkeypatch):
    """`/undo` を送ると、推論機を呼ばずに直前の依頼を戻し、done を出す。記録には戻した事の一言だけが残る。"""
    import io
    import json as _json
    from laps.engine import server, undo
    from laps.engine.session import Transcript
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("old\n")
    t = Transcript("undotest1")
    t.append({"role": "user", "content": "a.txt を直して"})
    rc = undo.begin(t.root, "r1")
    rc.record(str(ws / "a.txt"))
    (ws / "a.txt").write_text("new\n")
    assert rc.finish()
    t.close()
    buf = io.StringIO()
    srv = server.Server(server.Out(buf))
    spec = server.parse_run({"prompt": "/undo", "session": "undotest1", "workspace": str(ws), "id": "u1"}, 1)
    srv._run(spec)
    events = [_json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
    done = [e for e in events if e.get("type") == "done"][-1]
    assert done["outcome"] == "accepted" and "元に戻しました" in done["text"]
    assert (ws / "a.txt").read_text() == "old\n"
    raw = Transcript("undotest1").raw
    assert raw[-1].get("bw_note") and "undid" in raw[-1]["content"] and not any(m.get("content") == "/undo" for m in raw)


def test_shell_sandbox_in_development_mode(tmp_path, monkeypatch):
    """開発モードの Bash は作業場だけ書ける。.git は読むだけ。外への書き込みは理由つきで sandbox=false。"""
    from laps.engine.tools import shellbox
    if not shellbox.available():
        pytest.skip("bwrap が使えない")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"     # /tmp の中は書ける（一時置き場）ので、/tmp の外に作る
    import os as _os
    base = "/srv/tmp/blackwall" if _os.path.isdir("/srv/tmp/blackwall") else None
    if base is None:
        pytest.skip("/tmp の外に試しの置き場が無い")
    import tempfile
    ws = tempfile.mkdtemp(dir=base, prefix="boxws-")
    outside = tempfile.mkdtemp(dir=base, prefix="boxout-")
    _os.makedirs(_os.path.join(ws, ".git"))
    monkeypatch.delenv("LAPS_SHELL_SANDBOX", raising=False)
    monkeypatch.setenv("LAPS_POLICY", "trusted")              # 関所ではなく囲いを試す（試験の既定は workspace）
    ctx = Ctx(workspace=ws, root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs(),
              mode="development")
    assert shell.bash({"command": "echo ok > made.txt && cat made.txt"}, ctx).ok
    r = shell.bash({"command": f"echo x > {outside}/nope.txt"}, ctx)
    assert not r.ok and "sandbox=false" in r.output and not _os.path.exists(f"{outside}/nope.txt")
    r = shell.bash({"command": "echo x > .git/HEAD"}, ctx)
    assert not r.ok
    r = shell.bash({"command": f"echo x > {outside}/yes.txt", "sandbox": False, "reason": "the user asked"}, ctx)
    assert r.ok and "outside the sandbox" in r.output and _os.path.exists(f"{outside}/yes.txt")
    ctx.mode = "general"                                       # 開発モード以外は囲わない
    assert shell.bash({"command": f"echo x > {outside}/general.txt"}, ctx).ok
    import shutil as _sh
    _sh.rmtree(ws, ignore_errors=True)
    _sh.rmtree(outside, ignore_errors=True)


def test_apply_patch_multi_file_all_or_nothing(tmp_path, monkeypatch):
    from laps.engine.tools import patch
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ctx = _ctx(tmp_path)
    (tmp_path / "app.py").write_text("def main():\n    print(\"hi\")\n    return 0\n")
    (tmp_path / "old.txt").write_text("bye\n")
    p = ("*** Begin Patch\n*** Update File: app.py\n@@ def main():\n-    print(\"hi\")\n+    print(\"hello\")\n"
         "*** Add File: notes/a.txt\n+first\n+second\n*** Delete File: old.txt\n*** End Patch\n")
    r = patch.run({"patch": p}, ctx)
    assert r.ok, r.output
    assert (tmp_path / "app.py").read_text() == "def main():\n    print(\"hello\")\n    return 0\n"
    assert (tmp_path / "notes/a.txt").read_text() == "first\nsecond\n" and not (tmp_path / "old.txt").exists()
    # 2 つ目のファイルが外れたら、1 つ目も書かない
    (tmp_path / "b.py").write_text("x = 1\n")
    bad = ("*** Begin Patch\n*** Update File: b.py\n-x = 1\n+x = 2\n*** Update File: app.py\n-    nope()\n+    yes()\n"
           "*** End Patch")
    r = patch.run({"patch": bad}, ctx)
    assert not r.ok and "nothing was changed" in r.output and (tmp_path / "b.py").read_text() == "x = 1\n"


def test_apply_patch_lenient_forms(tmp_path, monkeypatch):
    from laps.engine.tools import patch
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ctx = _ctx(tmp_path)
    (tmp_path / "t.txt").write_text("a\nb  \nc — d\nlast\n")
    # 行末の空白・字形・最初の @@ 無し・*** End of File・<<'EOF' の包み
    p = ("<<'EOF'\n*** Begin Patch\n*** Update File: t.txt\n b\n-c - d\n+c and d\n@@\n-last\n+LAST\n*** End of File\n"
         "*** End Patch\nEOF")
    r = patch.run({"patch": p}, ctx)
    assert r.ok, r.output
    assert (tmp_path / "t.txt").read_text() == "a\nb  \nc and d\nLAST\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のシェルで試す")
def test_bash_apply_patch_heredoc_is_routed(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ctx = _ctx(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("one\n")
    cmd = "cd sub && apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: x.txt\n-one\n+two\n*** End Patch\nEOF"
    r = shell.bash({"command": cmd}, ctx)
    assert r.ok and "ApplyPatch" in r.output and (tmp_path / "sub" / "x.txt").read_text() == "two\n"
