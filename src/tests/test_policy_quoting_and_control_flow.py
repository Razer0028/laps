"""引用符・改行・コマンド置換・heredoc・`;`/`&&` 連結の扱いを固定する（W5・2026-09-04）。

★スコープ: `$(...)`/バックティックの readonly 拒否・trusted 通過、および `&&` を挟んだ
`cd` の cwd 追従は `tests/test_policy.py` / `tests/test_policy_cd_cwd.py` に既にある。
ここはその隙間——**自己保護が `;`/`&&` の後段でも効くか**と、**heredoc をこのゲートが
どう扱うか**（`docs/policy-modes.md` の「見つけた食い違い」節に書いた実測）を固定する。

heredoc の2本（`test_heredoc_*`）は「これが正しい挙動」の主張ではなく、
「これが**いまの**挙動で、直すなら分かってから直す」という凍結。詳細説明は
`docs/policy-modes.md` を参照。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import laps.policy as policy


@pytest.fixture
def audit_file(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "audit.jsonl").write_text("x")
    monkeypatch.setenv("LAPS_HOME", str(home))
    return home / "audit.jsonl"


# --- `;`/`&&` chaining: does self-protection see every stage? --------------

@pytest.mark.parametrize("mode", ["trusted", "off"])
@pytest.mark.parametrize("sep", [";", "&&"])
def test_self_protect_fires_on_later_stage_of_chain(monkeypatch, tmp_path, audit_file, mode, sep):
    """`echo ok <sep> rm -f <audit>` — 1段目は無害でも、2段目は自己保護に掛かるべき。
    `&&` は test_policy_cd_cwd.py に前例があるが `cd` 限定。`;` は今回が初出。"""
    monkeypatch.setenv("LAPS_POLICY", mode)
    cmd = f"echo ok {sep} rm -f {audit_file}"
    v = policy.check_command(cmd, cwd=str(tmp_path))
    assert not v.allowed, f"{mode} で自己保護が {sep} の後段まで届いていない: {cmd}"


def test_quoted_separator_does_not_split(monkeypatch, tmp_path, audit_file):
    """引用符の中の `;` は区切りではない——`shlex(posix=True)` の役目。
    その上で本物の `&&` の後段（自己保護）はちゃんと分離されて検査されることを確かめる。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    cmd = f'echo "a;b" && rm -f {audit_file}'
    v = policy.check_command(cmd, cwd=str(tmp_path))
    assert not v.allowed, "引用符内の ; に惑わされず、&& の後段の rm を検査できていない"
    assert "audit.jsonl" in v.reason


def test_quoted_separator_alone_is_one_stage(monkeypatch, tmp_path):
    """比較対照: `&&`/後段が無ければ、引用符内の `;` は素通りする無害な1コマンド。"""
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    v = policy.check_command('echo "a;b"', cwd=str(tmp_path))
    assert v.allowed, v.reason


# --- unbalanced quotes across a multi-line -c already covered in
#     test_policy_trusted_parse.py; not repeated here.


# --- heredoc: this gate has no heredoc-specific handling --------------------
#
# `_split_commands()` treats a newline exactly like `;` (deliberate — a
# multi-line `python3 -c "..."` injection must not slip past as one opaque
# argv). The side effect: heredoc *body* lines are tokenised and checked as
# their own standalone command, because the parser has no concept of
# "we are inside a <<EOF block right now".

def test_heredoc_body_is_not_a_command_in_workspace(monkeypatch, tmp_path):
    """`cat <<EOF / hello world / EOF` — 本文は命令ではない（2026-10-22 から本文を読み飛ばす）。
    以前は本文の 1 語目 `hello` をプログラム名として検査して拒否していた。"""
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    v = policy.check_command("cat <<EOF\nhello world\nEOF", cwd=str(tmp_path))
    assert v.allowed, v.reason


def test_heredoc_inert_body_passes_in_trusted(monkeypatch, tmp_path):
    """trusted は "解析できないなら通す" 側なので、危険に見えない本文なら通る。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    v = policy.check_command("cat <<EOF\nnothing dangerous here\nEOF", cwd=str(tmp_path))
    assert v.allowed, v.reason


def test_heredoc_body_written_to_a_file_is_data(monkeypatch, tmp_path, audit_file):
    """ファイルへ書く本文の `rm -rf <audit の親>` は文字列であって実行されない（2026-10-22 から通す）。
    以前は本文を段として読み、自己保護に偶然かかって拒否していた。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    home = audit_file.parent
    cmd = f"cat > {tmp_path}/out.txt <<EOF\nrm -rf {home}\nEOF"
    v = policy.check_command(cmd, cwd=str(tmp_path))
    assert v.allowed, v.reason


def test_heredoc_fed_to_a_shell_is_still_checked(monkeypatch, tmp_path, audit_file):
    """シェルへ流し込む本文は実際に走る＝従来どおり段として読み、自己保護が効く。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    home = audit_file.parent
    v = policy.check_command(f"bash <<'EOF'\nrm -rf {home}\nEOF", cwd=str(tmp_path))
    assert not v.allowed
    assert "audit.jsonl" in v.reason or str(home) in v.reason
