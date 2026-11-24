"""失敗を、モデルが読む文字列に出す（2026-08-28・エージェント監査 S1/S2）。

★実測: 失敗した Bash が 3 走行で 118/24/49 件あったのに、モデルが読む
  `[n] Name → 本文` には**印が 1 文字も無かった**。ok は構造化データにだけ入り、
  読む側には届いていない＝この木で繰り返している「書いたが繋いでいない」。
★実測: `os.environ["LAPS_SUBAGENT"]="1"` を立てたまま戻さないので、
  委譲は 1 走行に 1 回しか効かず、以後は所要 0 秒の偽拒否（7 回中 4 回）。
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


def test_failed_bash_carries_exit_code_in_body():
    from laps.tools import bash
    out = str(bash.SPEC.handler({"command": "bash -c 'echo boom >&2; exit 3'"}))
    assert "[exit=3]" in out and "boom" in out


def test_successful_bash_has_no_exit_tag():
    from laps.tools import bash
    out = str(bash.SPEC.handler({"command": "echo fine"}))
    assert "[exit=" not in out and "fine" in out


def test_exit_tag_not_doubled_on_empty_output():
    from laps.tools import bash
    out = str(bash.SPEC.handler({"command": "false"}))
    assert out.count("exit") == 1, out


def test_real_nesting_is_still_refused(monkeypatch):
    from laps.tools import task as task_tool
    monkeypatch.setenv("LAPS_SUBAGENT", "1")
    out = str(task_tool.SPEC.handler({"description": "d", "prompt": "p"}))
    assert "nested" in out
    assert os.environ.get("LAPS_SUBAGENT") == "1", "断りで環境を壊している"


def test_timeout_hint_recognises_a_foreground_server():
    """★実測: timeout 47 件のうち待受けバナー付きが 10/12。旧実装は場面を問わず
    「winget なら --silent」という Windows 専用の助言を返していた（汎用の助言は
    置換しないより悪い、の再演）。"""
    from laps.tools.bash import _timeout_hint
    h = _timeout_hint("bash start.sh",
                      "PHP 8.4.24 Development Server (http://0.0.0.0:18300) started")
    assert "前面で起動して戻ってきていません" in h and "背景" in h
    assert "winget" not in h


def test_timeout_hint_for_silence_is_about_stdin():
    from laps.tools.bash import _timeout_hint
    h = _timeout_hint("apt install x", "")
    assert "入力待ち" in h and "/dev/null" in h


def test_timeout_hint_for_progress_says_slow_not_stuck():
    from laps.tools.bash import _timeout_hint
    h = _timeout_hint("pip download x", "Downloading foo.whl (45%) ETA 0:02:10")
    assert "遅いだけ" in h


def test_timeout_hint_does_not_invent_when_unknown():
    from laps.tools.bash import _timeout_hint
    h = _timeout_hint("x", "なにか関係のない行")
    assert "特定できません" in h


def test_timeout_hints_say_the_group_was_killed():
    """★2026-09-05 に**前提ごと差し替えた**試験。

    旧: `test_timeout_hints_never_recommend_a_bare_ampersand`
        ——「裸の & は道具の実行が終わると一緒に片付けられる」という前提で、
        助言に `nohup` を強制していた。
    ★この前提は**誤り**だった。実測（最小再現）で分かった本当の姿は逆で、
      裸の & で背景に回した子が**シェルの stdout/stderr パイプを持ち続ける**ため
      `communicate()` が EOF を待ち、**120 秒まるまる待たされた末に**
      `_kill_group()` がプロセスグループごと SIGKILL していた
      ＝片付けられるどころか、**正常に起動したサーバを道具が殺していた**。
      run50/51 の記録では timeout 18 件のうち 11 件がこの形（1,320 秒）。
    → `_wait_or_release()` で待つ相手をパイプからシェルへ変えたので、
      いま裸の & は正しく即座に制御を返す（勧めても嘘にならない）。

    そこで守るべき不変条件を書き換える: **打ち切りの助言は「殺した」と言うこと。**
    「失敗ではありません」「止まってはいません」のような、
    もっともらしいが事実と逆の文言を二度と入れない。

    ★2026-09-05（第4回b）にもう一段直した: 旧版はここで `php -S 0.0.0.0:18300` を
      渡していた——**この機械で 18300 が使われているか**で真偽が変わる文言を
      不変条件にしていたことになる（走行中なら「ポートは空いています」は嘘）。
      いまは (a) 実際に空いているポートで測り、(b)「空いています」と**言ったときだけ**
      本当に空いていることまで確かめる＝言った内容の真偽を、試験が見る。
    """
    import socket

    from laps import procutil
    from laps.tools.bash import _timeout_hint

    _s = socket.socket(); _s.bind(("127.0.0.1", 0)); free = _s.getsockname()[1]; _s.close()
    for cmd, partial in (("x", ""),
                         ("bash start.sh", ""),
                         ("pip download x", "Downloading 40%"),
                         ("x", "なにか不明な行"),
                         (f"php -S 0.0.0.0:{free}",
                          f"PHP Development Server (http://0.0.0.0:{free}) started")):
        h = _timeout_hint(cmd, partial)
        assert "強制終了" in h, f"殺したことを言っていない: {h!r}"
        assert "失敗ではありません" not in h, f"事実と逆: {h!r}"
        assert "止まってはいません" not in h, f"事実と逆: {h!r}"
        if "ポートは空いています" in h:
            assert not procutil.port_in_use(free), (
                f"空いていると言ったが実際は使われている: {h!r}")


def test_bash_tool_actually_runs_bash():
    """★2026-08-29 実測: shell=True は /bin/sh（Debian では dash）を使うのに、
    モデルは bash 前提で書く。`mkdir -p x/{a,b}` が**その名前のディレクトリ**を作り、
    残骸として 7 走行ぶん残っていた。さらに道具は shell: /bin/bash と**嘘の報告**をしていた。"""
    import shutil

    from laps.tools import bash
    if not shutil.which("bash"):
        return
    out = str(bash.SPEC.handler({"command": "echo x{1,2}"}))
    assert "x1 x2" in out, f"ブレース展開が効いていない（dash のまま）: {out!r}"


def test_bash_reports_the_shell_it_used(monkeypatch):
    """空出力のときに名乗るシェルは、**実際に走らせたもの**であること。"""
    import shutil

    from laps.tools import bash
    monkeypatch.setenv("SHELL", "/usr/bin/fish")     # 利用者のログインシェル（無関係）
    out = str(bash.SPEC.handler({"command": "true"}))
    assert "fish" not in out, "ログインシェルを実行シェルとして報告している"
    if shutil.which("bash"):
        assert "bash" in out
