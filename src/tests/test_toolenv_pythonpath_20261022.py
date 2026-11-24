"""★2026-10-22 Terminal-Bench の fix-pandas-version: term の client が PYTHONPATH に自分の sys.path（/usr/lib/python3.11・同梱の venv）を
入れ、Bash の道具まで伝わって、課題の Python 3.8 が 3.11 の標準ライブラリを読んで起動できなかった。道具に渡す環境では元の値に戻す。"""
from laps.oc import toolenv


def test_child_env_restores_the_users_pythonpath(monkeypatch):
    monkeypatch.setattr(toolenv, "child_path", lambda: __import__("os").environ.get("PATH", ""))
    monkeypatch.setenv("PYTHONPATH", "/usr/lib/python3.11:/opt/blackwall/venv/lib/python3.11/site-packages")
    monkeypatch.setenv("LAPS_USER_PYTHONPATH", "")
    env = toolenv.child_env()
    assert env is not None and "PYTHONPATH" not in env                  # 元は無かった
    monkeypatch.setenv("LAPS_USER_PYTHONPATH", "/home/u/lib")
    assert toolenv.child_env()["PYTHONPATH"] == "/home/u/lib"           # 元の値へ
    monkeypatch.delenv("LAPS_USER_PYTHONPATH")
    assert toolenv.child_env() is None                                   # 起動元が書き換えていなければ触らない


def test_term_client_records_the_original(monkeypatch):
    from laps.term import client as c
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("LAPS_USER_PYTHONPATH", raising=False)
    cl = c.ServeClient.__new__(c.ServeClient) if hasattr(c, "ServeClient") else None
    if cl is None:
        return
    for k in ("extra_env", "backend", "model", "workspace", "policy", "ethics", "tools"):
        setattr(cl, k, {} if k == "extra_env" else "x")
    cl.home = None
    env = cl._env()
    assert env.get("LAPS_USER_PYTHONPATH") == "" and env.get("PYTHONPATH")
