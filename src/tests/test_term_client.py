"""端末クライアントの殻（laps/term/client.py）の試験。

本物の `python -m laps.serve` を起動して話す。backend は繋がらない
127.0.0.1:9 でよい——ready は backend に触らずに出るし、send は接続失敗の
error + done で**ちょうど 1 回**閉じる（serve の約束）。

★ LAPS_HOME は必ず tmp_path に向ける（利用者の ~/.blackwall を汚さない）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps.term.client import ServeClient, decode_line  # noqa: E402

DEAD_BACKEND = "http://127.0.0.1:9/v1"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path / "sess"))


def _client(tmp_path: Path, **kw) -> ServeClient:
    kw.setdefault("workspace", str(tmp_path / "ws"))
    kw.setdefault("backend", DEAD_BACKEND)
    kw.setdefault("model", "test-model")
    kw.setdefault("home", str(tmp_path / "home"))
    return ServeClient(**kw)


@pytest.fixture
def client(tmp_path):
    c = _client(tmp_path)
    yield c
    c.quit()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------
# 起動・状態
# --------------------------------------------------------------------------

def test_起動して_ready_を受ける(client, tmp_path):
    ev = client.start()
    assert ev["type"] == "notice" and ev["kind"] == "ready"
    assert client.alive and client.pid == ev["pid"] > 0
    assert client.status is ev
    # 環境変数で渡した実行設定が子に届いている
    assert ev["policy"] == "workspace"
    assert ev["backend"] == DEAD_BACKEND
    assert ev["model"] == "test-model"
    assert ev["toolset"] == "full"
    assert ev["native_tools"] is True
    assert isinstance(ev["tools"], list) and ev["tools"]
    # cwd = 作業場（無ければ作る）
    assert Path(ev["cwd"]).resolve() == (tmp_path / "ws").resolve()
    assert (tmp_path / "ws").is_dir()


def test_二重起動は拒む(client):
    client.start()
    with pytest.raises(RuntimeError):
        client.start()


def test_ping_で_pong_が返り_status_が更新される(client):
    ready = client.start()
    pong = client.ping()
    assert pong["kind"] == "pong" and pong["type"] == "notice"
    assert pong["id"] == "p1"
    assert pong["pid"] == ready["pid"]
    assert pong["busy"] is None
    assert client.status is pong
    # 続けて ping しても id が衝突しない
    assert client.ping()["id"] == "p2"


def test_quit_で子が消える(tmp_path):
    c = _client(tmp_path)
    c.start()
    pid = c.pid
    assert _pid_exists(pid)
    c.quit()
    assert not c.alive
    assert not _pid_exists(pid), "quit 後も子プロセスが残っている"
    # 2 度 quit しても落ちない
    c.quit()


def test_quit_に応じない子は_SIGTERM_で止める(tmp_path, monkeypatch):
    """stdin を読まない（quit が届かない）子でも、自分が起動した pid を確実に止める。"""
    mod_dir = tmp_path / "mod"
    mod_dir.mkdir()
    (mod_dir / "sleepy_serve.py").write_text(
        "import sys, time, json\n"
        "sys.stdout.write(json.dumps({'type':'notice','kind':'ready','pid':1})+'\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(600)\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(mod_dir))   # PYTHONPATH は sys.path を継ぐ
    c = _client(tmp_path, module="sleepy_serve")
    c.start(timeout=10)
    pid = c.pid
    t = time.monotonic()
    c.quit(timeout=1.0)
    assert time.monotonic() - t < 10
    assert not _pid_exists(pid)


def test_起動失敗は_RuntimeError_に_stderr_を含む(tmp_path):
    c = _client(tmp_path, module="blackwall_no_such_module_xyz")
    with pytest.raises(RuntimeError) as e:
        c.start(timeout=10)
    msg = str(e.value)
    assert "No module named" in msg, msg
    assert "blackwall_no_such_module_xyz" in msg
    assert not c.alive


def test_ready_が来ないと時間切れで_RuntimeError(tmp_path, monkeypatch):
    mod_dir = tmp_path / "mod"
    mod_dir.mkdir()
    (mod_dir / "silent_serve.py").write_text(
        "import sys, time\nsys.stderr.write('silent: booting\\n'); sys.stderr.flush()\n"
        "time.sleep(600)\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(mod_dir))
    c = _client(tmp_path, module="silent_serve")
    with pytest.raises(RuntimeError) as e:
        c.start(timeout=1.0)
    assert "ready" in str(e.value)
    assert "silent: booting" in str(e.value), "stderr の末尾が説明に入っていない"
    assert not c.alive, "時間切れの子が残っている"


def test_起動できない_python_は_RuntimeError(tmp_path):
    c = _client(tmp_path, python=str(tmp_path / "no-such-python"))
    with pytest.raises(RuntimeError):
        c.start()


# --------------------------------------------------------------------------
# send / events
# --------------------------------------------------------------------------

def _collect_until_done(client: ServeClient, timeout: float = 60.0) -> list[dict]:
    got: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ev in client.events(timeout=1.0):
            if ev is None:
                break
            got.append(ev)
            if ev.get("type") == "done":
                return got
    raise AssertionError(f"done が来ない: {got}")


def test_send_は_rid_を返し_done_がちょうど_1_つ来る(client):
    client.start()
    rid = client.send([{"role": "user", "content": "1+1は？"}])
    assert rid == "r1"
    evs = _collect_until_done(client)
    dones = [e for e in evs if e.get("type") == "done"]
    assert len(dones) == 1
    assert dones[0]["id"] == rid
    # backend は居ないので失敗で閉じる（outcome が何であれ done は 1 つ）
    assert dones[0]["outcome"]
    assert any(e.get("type") == "error" for e in evs), "接続失敗が error として見えない"


def test_send_の本文に禁止鍵を入れない(client):
    """policy/ethics/home/native_tools を載せると serve が Rejected で落とす。"""
    import json

    sent: list[dict] = []
    orig = client._write

    def spy(obj):
        sent.append(json.loads(json.dumps(obj)))
        return orig(obj)

    client.start()
    client._write = spy  # type: ignore[method-assign]
    client.send([{"role": "user", "content": "x"}], rid="rx", session="s-1")
    body = sent[-1]
    assert body["type"] == "send" and body["id"] == "rx"
    for k in ("policy", "ethics", "home", "native_tools", "until", "yes", "env"):
        assert k not in body, k
    assert body["model"] == "test-model" and body["backend"] == DEAD_BACKEND
    assert body["tools"] == "full" and body["max_iters"] == 0
    assert body["session"] == "s-1"
    assert body["workspace"] == client.workspace
    evs = _collect_until_done(client)
    assert not any(e.get("kind") == "rejected" for e in evs), "serve に拒否された"


def test_cancel_と_answer_は行を書く(client):
    import json
    client.start()
    sent: list[dict] = []
    client._write = lambda obj: sent.append(json.loads(json.dumps(obj)))  # type: ignore[method-assign]
    client.cancel("r9")
    client.answer("a1", "はい")
    assert sent == [{"type": "cancel", "id": "r9"},
                    {"type": "answer", "ask": "a1", "text": "はい"}]


def test_cancel_を実際に送っても_done_は_1_つ(client):
    client.start()
    rid = client.send([{"role": "user", "content": "長い作業"}])
    client.cancel(rid)
    evs = _collect_until_done(client)
    assert sum(1 for e in evs if e.get("type") == "done") == 1


def test_events_は_timeout_で_None_を出して戻る(client):
    client.start()
    t = time.monotonic()
    got = list(client.events(timeout=0.3))
    assert got == [None]
    assert 0.2 <= time.monotonic() - t < 3


def test_ping_の間に来た事象は失われない(client):
    """send の直後に ping すると、pong より前に run_start 等が届く。脇に寄せて順に出す。"""
    client.start()
    rid = client.send([{"role": "user", "content": "1+1は？"}])
    pong = client.ping()
    assert pong["kind"] == "pong"
    evs = _collect_until_done(client)
    kinds = [(e.get("type"), e.get("kind")) for e in evs]
    assert ("done", None) in kinds
    assert all(e.get("id") in (rid, None) or e.get("type") == "notice" for e in evs)


def test_子が死ぬと_exit_が積まれ_events_が終わる(tmp_path):
    c = _client(tmp_path)
    c.start()
    pid = c.pid
    c._proc.kill()   # 外から殺された状況
    evs = list(c.events(timeout=10.0))
    assert evs and evs[-1]["type"] == "_exit"
    assert "code" in evs[-1]
    assert not c.alive
    # 死んだ後の send は例外
    with pytest.raises(RuntimeError):
        c.send([{"role": "user", "content": "x"}])
    c.quit()
    assert not _pid_exists(pid)


def test_起動前の_send_は例外(tmp_path):
    c = _client(tmp_path)
    assert not c.alive and c.pid is None
    with pytest.raises(RuntimeError):
        c.send([{"role": "user", "content": "x"}])


# --------------------------------------------------------------------------
# restart
# --------------------------------------------------------------------------

def test_restart_で_policy_と_ethics_を切り替える(client):
    r1 = client.start()
    pid1 = client.pid
    r2 = client.restart(policy="readonly", ethics="direct")
    assert r2["kind"] == "ready"
    assert client.pid != pid1 and not _pid_exists(pid1)
    assert r2["policy"] == "readonly"
    assert client.policy == "readonly" and client.ethics == "direct"
    # 上書きしていない設定は引き継ぐ
    assert r2["backend"] == r1["backend"] and r2["model"] == r1["model"]
    assert client.settings["ethics"] == "direct"


def test_restart_は知らない鍵を拒む(client):
    client.start()
    with pytest.raises(TypeError):
        client.restart(nonsense=1)


# --------------------------------------------------------------------------
# 行の解釈・stderr
# --------------------------------------------------------------------------

def test_decode_line_は壊れた行を捨てない():
    assert decode_line(b"\n") is None
    assert decode_line(b"   \n") is None
    assert decode_line(b'{"type":"token","text":"\xe3\x81\x82"}\n') == {"type": "token", "text": "あ"}
    g = decode_line(b"[serve] not json\n")
    assert g["type"] == "_garbage" and g["line"] == "[serve] not json"
    assert decode_line(b"[1,2]\n")["type"] == "_garbage"
    # 壊れた utf-8 でも落ちない
    assert decode_line(b"\xff\xfe{\n")["type"] == "_garbage"


def test_debug_log_に_stderr_が落ちる(tmp_path, monkeypatch):
    mod_dir = tmp_path / "mod"
    mod_dir.mkdir()
    (mod_dir / "noisy_serve.py").write_text(
        "import sys, json\n"
        "sys.stderr.write('[noisy] hello \\u2e3f\\n'); sys.stderr.flush()\n"
        "sys.stdout.write(json.dumps({'type':'notice','kind':'ready','pid':1})+'\\n'); sys.stdout.flush()\n"
        "sys.stdin.readline()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(mod_dir))
    log = tmp_path / "logs" / "debug.log"
    c = _client(tmp_path, module="noisy_serve", debug_log=str(log))
    c.start(timeout=10)
    c.quit()
    assert log.is_file()
    assert "[noisy] hello" in log.read_text(encoding="utf-8")
    assert "[noisy] hello" in c.stderr_tail()


def test_debug_note_はserveのstderrと同じファイルに追記される(tmp_path, monkeypatch):
    """★`--debug FILE` を付けても 0 バイトだった日があった（2026-09-03）: serve は
    正常系では stderr に 1 バイトも書かない（`_log` は例外パスでしか呼ばれない）ので、
    子の stderr だけを落とす仕組みでは「何も壊れていないのに長い」ケースの手がかりが
    残らない。クライアント側が見ている出来事も**同じファイル**に書ける。"""
    mod_dir = tmp_path / "mod"
    mod_dir.mkdir()
    (mod_dir / "quiet_serve.py").write_text(
        "import sys, json\n"
        "sys.stdout.write(json.dumps({'type':'notice','kind':'ready','pid':1})+'\\n'); sys.stdout.flush()\n"
        "sys.stdin.readline()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(mod_dir))
    log = tmp_path / "logs" / "debug.log"
    c = _client(tmp_path, module="quiet_serve", debug_log=str(log))
    c.start(timeout=10)
    c.debug_note("cancel 送信 rid=r1")
    c.debug_note("done 受信 rid=r1 outcome=cancelled")
    c.quit()
    body = log.read_text(encoding="utf-8")
    assert "[term " in body and "cancel 送信 rid=r1" in body
    assert "done 受信 rid=r1 outcome=cancelled" in body


def test_debug_note_はdebug_logが無ければ何もしない(tmp_path):
    c = _client(tmp_path)   # debug_log なし
    c.debug_note("何か")    # 例外を投げない・ファイルも作らない
    assert not (tmp_path / "debug.log").exists()


def test_環境変数の組み立て(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/existing/pp")
    c = _client(tmp_path, policy="trusted", ethics="raw", tools="focused", max_iters=7,
                extra_env={"LAPS_THINK": "0"})
    env = c._env()
    assert env["LAPS_POLICY"] == "trusted"
    assert env["LAPS_ETHICS"] == "raw"
    assert env["LAPS_TOOLSET"] == "focused"
    assert env["LAPS_NATIVE_TOOLS"] == "1"
    assert env["LAPS_MAX_SECONDS"] == "0" and env["LAPS_MAX_TOOL_CALLS"] == "0"
    assert env["LAPS_HOME"] == str(tmp_path / "home")
    assert env["LAPS_WORKSPACE"] == str(tmp_path / "ws")
    assert env["LAPS_THINK"] == "0"
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    pp = env["PYTHONPATH"].split(os.pathsep)
    assert str(SRC) in pp, "呼び出し側の sys.path を継いでいない"
    assert pp[-1] == "/existing/pp"
    # max_iters は send の本文にだけ載る（環境変数ではない）
    assert c.max_iters == 7


# --------------------------------------------------------------------------
# 検証で見つけた穴（回帰）
# --------------------------------------------------------------------------

def test_restart_後に旧い子の_exit_が新しい子へ混ざらない(tmp_path, monkeypatch):
    """孫プロセスが stdout/stderr を握ったまま子が死ぬと、旧ポンプは quit の後も
    EOF を待ち続ける。旧ポンプが `self._q` を後から見に行く作りだと、その `_exit` が
    **新しい子の列**に落ちて events(None) が「死んだ」と嘘をつく（実測で再現した）。"""
    mod_dir = tmp_path / "mod"
    mod_dir.mkdir()
    (mod_dir / "hold_serve.py").write_text(
        "import sys, json, subprocess\n"
        "sys.stdout.write(json.dumps({'type':'notice','kind':'ready','pid':1})+'\\n'); sys.stdout.flush()\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'])\n"   # 孫がパイプを継ぐ
        "sys.stdin.readline()\n", encoding="utf-8")
    (mod_dir / "calm_serve.py").write_text(
        "import sys, json\n"
        "sys.stdout.write(json.dumps({'type':'notice','kind':'ready','pid':1})+'\\n'); sys.stdout.flush()\n"
        "sys.stdin.readline()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(mod_dir))
    c = _client(tmp_path, module="hold_serve")
    c.start(timeout=10)
    t = time.monotonic()
    c.restart(timeout=10, module="calm_serve")
    assert time.monotonic() - t < 5, "孫がパイプを握っているだけで restart が止まる"
    assert c.alive
    # 旧い子の孫が消えるまで待っても、新しい子の列には何も落ちない
    time.sleep(6.5)
    assert c.alive and not c._exited
    got = list(c.events(timeout=0.2))
    assert got == [None], f"旧い子の事象が混ざった: {got}"
    c.quit()


def test_子の_exit_は必ず_events_None_で届く(tmp_path):
    """`_exited` の旗を先に立てると、列が空の一瞬に events(None) が `_exit` を出さずに戻る。"""
    for _ in range(8):
        c = _client(tmp_path)
        c.start()
        c._proc.kill()
        got = list(c.events(timeout=None))
        assert got and got[-1]["type"] == "_exit" and got[-1]["pid"] == c.pid
        c.quit()


def test_decode_line_は内部名を名乗る行を事象にしない():
    """子が `{"type":"_exit"}` を書いても events() が「死んだ」と誤らない。"""
    assert decode_line(b'{"type":"_exit","code":0}\n')["type"] == "_garbage"
    assert decode_line(b'{"type":"_garbage"}\n')["type"] == "_garbage"
    assert decode_line(b'{"type":"token","text":""}\n')["type"] == "token"


def test_長い行を読んでも遅くない(tmp_path, monkeypatch):
    """bufsize=0 の readline は 1 バイトずつ read(2) する（200KB×20 で 0.94 秒）。"""
    mod_dir = tmp_path / "mod"
    mod_dir.mkdir()
    (mod_dir / "fat_serve.py").write_text(
        "import sys, json\n"
        "sys.stdout.write(json.dumps({'type':'notice','kind':'ready','pid':1})+'\\n'); sys.stdout.flush()\n"
        "big = 'x' * 200000\n"
        "for i in range(20):\n"
        "    sys.stdout.write(json.dumps({'type':'tool_result','result':big})+'\\n')\n"
        "sys.stdout.write(json.dumps({'type':'done','outcome':'ok'})+'\\n'); sys.stdout.flush()\n"
        "sys.stdin.readline()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(mod_dir))
    c = _client(tmp_path, module="fat_serve")
    c.start(timeout=10)
    t = time.monotonic()
    evs = _collect_until_done(c, timeout=30)
    took = time.monotonic() - t
    c.quit()
    assert sum(1 for e in evs if e.get("type") == "tool_result") == 20
    assert all(len(e["result"]) == 200000 for e in evs if e.get("type") == "tool_result")
    assert took < 0.5, f"4MB の読み取りに {took:.2f}s"


def test_events_timeout_0_は待たずに戻る(client):
    client.start()
    t = time.monotonic()
    assert list(client.events(timeout=0)) == [None]
    assert time.monotonic() - t < 0.5


def test_作業場が作れないと_RuntimeError(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    c = _client(tmp_path, workspace=str(blocker / "ws"))   # ファイルの下にディレクトリは作れない
    with pytest.raises(RuntimeError):
        c.start()
    assert not c.alive


def test_restart_rolls_back_settings_when_start_fails(tmp_path, monkeypatch):
    """★レビュー（2026-08-23 high）: 失敗した /policy trusted が次の自動再起動で黙って有効化されていた。"""
    from laps.term.client import ServeClient
    c = ServeClient(workspace=str(tmp_path), backend="http://127.0.0.1:9/v1", model="m", policy="workspace")
    c.start()
    try:
        with pytest.raises(RuntimeError):
            c.restart(timeout=2.0, policy="trusted", module="laps.no_such_module_xyz")
        assert c.policy == "workspace", "失敗した切替の値が残っている"
        assert c.module == "laps.serve"
    finally:
        c.quit()
