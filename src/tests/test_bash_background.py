"""背景起動（run_in_background）の穴を固定する（2026-09-02）。

★実測: 直近 8 走行の記録で背景起動は **44 回**（うち直近 4 走行で 16 回）。
  全部が `php -S …` の常駐サーバで、この現場で最も高くついた残骸の元
  （run44 の server.log に `Failed to listen on 0.0.0.0:18300` が残っている）。

直した 3 つ:
  ① 一時ファイルの名前が固定（`tmp.out`）で、**同時に 2 つ起動すると潰し合う**。
     後の起動は rename に失敗して「Error launching background」を返すが、
     プロセスは走っている＝**嘘の失敗**。
  ② 前面は bash・背景は `/bin/sh`（Debian では dash）だった＝
     bash 前提の綴りが背景でだけ黙って壊れる（片方で直して片方に残した）。
  ③ Windows では `preexec_fn=os.setsid` で**必ず失敗**していた
     （`bash_output.py` の注記も同じことを書いている）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.tools import bash as bash_tool  # noqa: E402


def _text(out) -> str:
    return out if isinstance(out, str) else out.text


def _pid(out: str) -> int:
    for ln in out.splitlines():
        if ln.strip().startswith("pid:"):
            return int(ln.split(":", 1)[1])
    raise AssertionError(f"pid が結果に無い: {out}")


@pytest.fixture()
def bg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    return tmp_path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_two_concurrent_launches_keep_separate_logs(bg_home):
    """同時に 2 つ起動しても、出力が混ざらない・片方が失敗しない。"""
    a = _text(bash_tool._run({"command": "echo AAA", "run_in_background": True}))
    b = _text(bash_tool._run({"command": "echo BBB", "run_in_background": True}))
    assert a.startswith("OK:") and b.startswith("OK:"), (a, b)
    pa, pb = _pid(a), _pid(b)
    assert pa != pb
    for _ in range(50):
        time.sleep(0.05)
        if (bg_home / "bg" / f"{pa}.out").read_bytes() and \
           (bg_home / "bg" / f"{pb}.out").read_bytes():
            break
    assert (bg_home / "bg" / f"{pa}.out").read_text().strip() == "AAA"
    assert (bg_home / "bg" / f"{pb}.out").read_text().strip() == "BBB"
    # 台帳も両方そろっている（片方が消えていない）
    for pid in (pa, pb):
        meta = json.loads((bg_home / "bg" / f"{pid}.meta.json").read_text(encoding="utf-8"))
        assert meta["pid"] == pid


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="bash が無い環境")
def test_background_uses_bash_like_the_foreground(bg_home):
    """背景だけ dash で走って、bash 前提の綴りが黙って壊れることがない。"""
    out = _text(bash_tool._run(
        {"command": "echo x{1,2,3} > /dev/stderr; echo done", "run_in_background": True}))
    pid = _pid(out)
    err = bg_home / "bg" / f"{pid}.err"
    for _ in range(60):
        time.sleep(0.05)
        if err.read_bytes():
            break
    # dash はブレース展開をしないので "x{1,2,3}" のまま出る
    assert err.read_text().strip() == "x1 x2 x3", err.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の起動経路")
def test_background_is_its_own_session_so_the_group_can_be_stopped(bg_home):
    """グループごと止められる（pid はシェルのもの＝kill だけでは子が残る）。"""
    out = _text(bash_tool._run({"command": "sleep 30", "run_in_background": True}))
    pid = _pid(out)
    try:
        assert os.getpgid(pid) == pid, "新しいセッションになっていない"
        assert "kill=true" in out or "taskkill" in out, out
    finally:
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


def test_background_stdin_is_closed(bg_home):
    """入力待ちで永久に止まらない（前面と同じ約束）。"""
    if sys.platform == "win32":
        pytest.skip("POSIX の起動経路")
    out = _text(bash_tool._run({"command": "cat; echo EOF", "run_in_background": True}))
    pid = _pid(out)
    p = bg_home / "bg" / f"{pid}.out"
    for _ in range(60):
        time.sleep(0.05)
        if b"EOF" in p.read_bytes():
            break
    assert "EOF" in p.read_text()
