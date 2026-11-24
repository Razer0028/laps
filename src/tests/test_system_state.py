"""A verb, not a command — so there is nothing to pipe the evidence into.

Eleven of fourteen measured fabrications had the same shape: the agent ran
`systemctl --failed --no-legend | awk '{print $1}'`, got four bullets with the
names cut off, and reported that there were no failed units. It was not lying
about the units, it could no longer see them.

No guard reaches that. A citation check compares a claim against the output it
cites, and the cited output really was four bullets — the loss happened before
there was anything to check. So the fix is earlier: an interface with no pipe in
it. These tests hold that property, because a tool that quietly grew a `command`
field would be the old failure with a new name.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest

from laps.tools import system_state as S


def test_there_is_no_way_to_pass_a_command():
    """The schema is the guarantee. If this loosens, the tool is pointless."""
    props = S.SPEC.schema["properties"]
    assert set(props) == {"verb"}, f"verb 以外の入口が増えている: {sorted(props)}"
    assert props["verb"]["enum"] == sorted(S._VERBS)


def test_every_verb_is_a_fixed_argv_with_no_shell():
    for verb, (argv, _desc) in S._VERBS.items():
        assert isinstance(argv, list), f"{verb} が文字列コマンドになっている"
        joined = " ".join(argv)
        for shell_char in ("|", ">", "<", ";", "&&", "$("):
            assert shell_char not in joined, f"{verb} にシェル記号 {shell_char}"


def test_every_verb_only_reads():
    """No verb may mutate. Adding one would be a code change, not a runtime one."""
    writers = {"rm", "mv", "cp", "systemctl-start", "kill", "tee", "dd", "chmod"}
    for verb, (argv, _d) in S._VERBS.items():
        assert argv[0] not in writers
        if argv[0] == "systemctl":
            assert ("--failed" in argv) or ("list-unit-files" in argv) or ("list-units" in argv), \
                f"{verb} が systemctl の書き込み動詞になりうる"


def test_an_unknown_verb_is_refused_and_lists_what_exists():
    out = S.SPEC.handler({"verb": "rm -rf /"})
    assert out.startswith("Error:")
    assert "failed_units" in out, "使える項目が示されていない"


def test_failed_units_returns_names_not_bullets():
    """The output the measurements needed and never got.

    `--no-legend --plain` keeps the unit names; the agent's own pipeline was
    throwing them away. If this ever returns bare bullets the tool has stopped
    solving the problem it exists for.
    """
    out = S.SPEC.handler({"verb": "failed_units"})
    assert out.startswith("[SystemState failed_units]")
    body = out.split("\n", 1)[1]
    if body.strip() != "(出力なし＝該当ゼロ件)":
        for line in body.splitlines():
            if line.strip():
                assert line.strip() not in ("●", "*"), "名前が落ちて記号だけになっている"


def test_an_empty_result_says_so_rather_than_returning_nothing():
    """"No failed units" is exactly the claim that was being fabricated.

    An empty string invites the model to fill the gap; a sentence does not.
    """
    import subprocess
    from unittest import mock
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with mock.patch("subprocess.run", return_value=fake):
        out = S.SPEC.handler({"verb": "failed_units"})
    assert "該当ゼロ件" in out


def test_disk_and_memory_answer_whole():
    for verb in ("disk", "memory"):
        out = S.SPEC.handler({"verb": verb})
        assert out.startswith(f"[SystemState {verb}]")
        assert len(out.splitlines()) >= 2, f"{verb} の出力が1行しかない"


def test_the_description_tells_the_model_to_prefer_this_over_bash():
    """The tool only helps if it gets chosen; the description is the only lever."""
    assert "Bash" in S.SPEC.description
    assert "パイプ" in S.SPEC.description


def test_failed_units_states_the_count():
    """The harness counts; the model reports.

    Handing back four raw lines moved the failure instead of removing it. Without
    this tool the model pipes systemctl through `wc -l` and the count is right;
    with it, 44 of 100 runs reported three units against an output of four. Every
    part of this project that works follows the same rule, and asking a model to
    count lines is asking it to compute.
    """
    out = SPEC_out = S.SPEC.handler({"verb": "failed_units"})
    body = SPEC_out.split("\n", 1)[1]
    if body.strip() != "(出力なし＝該当ゼロ件)":
        assert body.startswith("合計 "), "件数が示されていない"
        n = int(body.split()[1])
        listed = len([l for l in body.splitlines()[1:] if l.strip()])
        assert n == listed, f"示した件数 {n} と行数 {listed} が食い違う"


def test_the_count_matches_reality():
    """Cross-checked against systemctl itself, not against our own arithmetic."""
    import subprocess
    real = subprocess.run(["systemctl", "--failed", "--no-pager", "--no-legend", "--plain"],
                          capture_output=True, text=True)
    expected = len([l for l in real.stdout.splitlines() if l.strip()])
    body = S.SPEC.handler({"verb": "failed_units"}).split("\n", 1)[1]
    if expected == 0:
        assert "ゼロ件" in body
    else:
        assert body.startswith(f"合計 {expected} 件"), f"実際は {expected} 件"


def test_cp932_の応答で落ちない(monkeypatch):
    """★2026-09-02・`remote_read` と同じ欠陥がここにもあった（原因の形で grep して発見）。

    旧: `text=True`＝既定の UTF-8 で解釈。日本語 Windows（cp932）の応答で
    **UnicodeDecodeError** になり、しかも `except OSError` でも
    `TimeoutExpired` でも捕まらない＝道具ごと死ぬ。
    実機では `RemoteRead` が同じ形で 5 動詞すべて落ちている。
    """
    from laps.tools import system_state as S

    def fake_run(argv, **kw):
        assert kw.get("text") is not True, "text=True に戻っている（符号化の決め打ち）"
        class R:
            returncode = 0
            stdout = "ディスクの空き".encode("cp932")
            stderr = b""
        return R()

    monkeypatch.setattr(S.subprocess, "run", fake_run)
    out = str(S.SPEC.handler({"verb": "disk"}))
    assert "codec can't decode" not in out, "符号化で落ちている"
    assert "ディスクの空き" in out, f"cp932 を読めていない: {out[:120]}"


def test_長い出力でも_stderr_を切り落とさない(monkeypatch):
    """★`(stdout + stderr)[:上限]` は stderr が**後ろ**なので失敗の理由だけ消える。"""
    from laps.tools import system_state as S

    def fake_run(argv, **kw):
        class R:
            returncode = 1
            stdout = b"x" * 40000
            stderr = "FATAL: 読めません".encode("utf-8")
        return R()

    monkeypatch.setattr(S.subprocess, "run", fake_run)
    out = str(S.SPEC.handler({"verb": "disk"}))
    assert "FATAL: 読めません" in out, "stderr が切り落とされている"
    assert "略" in out, "省いたことを言っていない（黙って切らない）"
