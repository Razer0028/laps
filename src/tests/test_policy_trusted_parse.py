"""trusted では解析できない構文も通す（2026-09-03 dogfood: 複数行の python3 -c "…" が引用符の解析で拒否＝43 件）。
readonly / workspace は従来どおり fail-closed。"""
from laps import policy


CMD = 'cd /w && python3 -c "\nimport re\npatterns = [r\'(password|token)\\s*=\\s*[\\"\\\'][^\\"\\\']*[\\"\\\']\']\nprint(patterns)\n'   # 閉じ引用符なし


def test_trusted_passes_unparseable_quotes(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    v = policy.check_command(CMD, cwd=str(tmp_path)) if hasattr(policy, "check_command") else policy.verdict_for(CMD, cwd=str(tmp_path))
    assert v.allowed, v


def test_workspace_still_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    v = policy.check_command(CMD, cwd=str(tmp_path)) if hasattr(policy, "check_command") else policy.verdict_for(CMD, cwd=str(tmp_path))
    assert not v.allowed and "解析できない" in v.reason


# ★2026-10-22 Terminal-Bench の診断: 代入だけの行と、here document の本文を trusted でも拒否していた（「プログラム名を特定できない」）。
def _ok(cmd, tmp_path):
    return policy.check_command(cmd, cwd=str(tmp_path))


def test_trusted_assignment_only_line_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    assert _ok("N=5\necho $N", tmp_path).allowed
    assert _ok("x=1; echo hi", tmp_path).allowed


def test_trusted_heredoc_bodies_are_not_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    c_file = "cat > /tmp/t.c << 'EOF'\n#if 0\nimport sys\ndef fib(n):\n    a,b=0,1\n#endif\nEOF\ngcc /tmp/t.c -o /tmp/t && /tmp/t 10"
    py = "python3 << 'PYEOF'\nimport struct\nheader_size = 16\nprint(header_size)\nPYEOF"
    assert _ok(c_file, tmp_path).allowed
    assert _ok(py, tmp_path).allowed


def test_workspace_assignment_only_still_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    assert not _ok("N=5", tmp_path).allowed
