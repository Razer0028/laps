"""自己保護の範囲 —「自分が書いた物が、次に**ゲートを通らずに実行される**」場所。

★2026-09-02。`_SELF_PROTECTED` は権限定義・プロンプト・監査ログの3種類だけを
  見ていたが、同じ形の置き場が他に3つあった。探し方は「症状」ではなく
  **原因の形**（＝LAPS_HOME の下にあって、中身が実行される）:

    hooks.py:43          `subprocess.run([script])`  … policy を通らない
    plugin_loader.py:50  `exec_module(mod)`          … **プロセス内**で実行
    postedit.py:36/347   `postedit.json` の command を shell=True で実行

  hooks は 2026-08-16 に別セッションが `claudecode-gap` §hooks で
  「`policy._SELF_PROTECTED` に入っていない・未修正」と名指ししていたもの。
  出荷構成（LAPS_HOME が作業フォルダの外）では手が届かないが、
  **ベンチ構成では届く**（`src/bench/real_tasks.py:261` は LAPS_HOME を
  workspace の中に置く）。この試験はその**ベンチ構成の形**で確かめる。

⚠ 誤爆の実測: 許可済み edit/write/bash 37,967 件（2026-08-07〜09-02）のうち、
   hooks/ plugins/ postedit.json への書き込みは **0 件**。
"""
from __future__ import annotations

import pytest

import laps.policy as policy


@pytest.fixture
def bench_shape(monkeypatch, tmp_path):
    """`real_tasks.py:261` と同じ配置＝LAPS_HOME が workspace の**中**。"""
    ws = tmp_path / "work"; ws.mkdir()
    home = ws / ".bw"
    for sub in ("hooks/pre_tool", "plugins", "sessions/s1"):
        (home / sub).mkdir(parents=True)
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    monkeypatch.setenv("LAPS_HOME", str(home))
    return ws, home


@pytest.mark.parametrize("shape", [
    "touch {home}/hooks/pre_tool/x.sh",
    "echo evil > {home}/plugins/evil.py",
    "cp a.json {home}/postedit.json",
    "mv a.py {home}/plugins/a.py",
    "rm -rf {home}/hooks",
])
def test_実行される置き場には書けない(bench_shape, shape):
    ws, home = bench_shape
    cmd = shape.format(home=home)
    v = policy.check_command(cmd, cwd=str(ws))
    assert not v.allowed, f"自分で実行経路を作れる: {cmd}"


def test_check_path_側でも同じ(bench_shape):
    ws, home = bench_shape
    for path in (home / "hooks" / "pre_tool" / "x.sh",
                 home / "plugins" / "evil.py",
                 home / "postedit.json"):
        assert not policy.check_path(str(path), write=True).allowed, path


def test_普通の状態ファイルは書ける(bench_shape):
    """★巻き添えを固定する。エージェントは自分の会話記録・チェックポイントを
    LAPS_HOME の下に**書く必要がある**（同じ親の下だからと閉じない）。"""
    ws, home = bench_shape
    for cmd in (f"touch {home}/sessions/s1/events.jsonl",
                f"mkdir -p {home}/checkpoints/x",
                f"echo x > {home}/sessions/s1/messages.jsonl"):
        v = policy.check_command(cmd, cwd=str(ws))
        assert v.allowed, f"普通の状態書き込みが止まった: {cmd} — {v.reason}"


def test_読むのは止めない(bench_shape):
    ws, home = bench_shape
    v = policy.check_command(f"cat {home}/plugins/evil.py", cwd=str(ws))
    assert v.allowed, v.reason
