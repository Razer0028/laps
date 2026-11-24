"""★2026-09-09: 合否基準 S3 が出荷の門から切り離されていた（scripts に S3 の参照が 0 件）。
門が preflight に繋がっていること・猶予が切れたら落ちることを固定する。
"""
import pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "check_s3_wired.py"
PY_ = str(ROOT / ".venv" / "bin" / "python")


def _run(*args):
    return subprocess.run([PY_, str(GATE), *args], capture_output=True, text=True, timeout=60)


def test_門がpreflightから呼ばれている():
    s = (ROOT / "scripts" / "preflight.sh").read_text(encoding="utf-8")
    assert "check_s3_wired.py" in s, "S3 の門が preflight に繋がっていません"


def test_猶予の中なら通り_切れたら落ちる():
    r = _run("--today", "2026-09-10")
    assert r.returncode == 0, r.stdout + r.stderr
    r = _run("--today", "2099-01-01")
    assert r.returncode == 1, "猶予が切れても通ってしまう"
    assert "切れました" in r.stdout


def test_現況の文書が要る(tmp_path, monkeypatch):
    """文書が無ければ落ちる（門が『無いものを黙って通す』ようにならないこと）。"""
    s = (ROOT / "src" / "deploy" / "s3-status.md")
    assert s.exists(), "S3 の現況の文書がありません"
    assert "waiver-until:" in s.read_text(encoding="utf-8")
