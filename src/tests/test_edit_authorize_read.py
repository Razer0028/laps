"""★2026-09-08 監査（tools high）: Edit / ApplyDiff が authorize_read を通さず、資格情報のパスを読んで中身を返していた。"""
import pathlib
from laps.tools import edit as E
from laps.tools import apply_diff as D


def _secret(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / ".ssh"; d.mkdir()
    # ★鍵の見出し（BEGIN … PRIVATE KEY）は書かない。関門が見るのは**パス**であり、
    #   本文に鍵らしい文字列を置くと redact の誤爆検査（tests/test_redact.py）が実ソースとして拾う。
    f = d / "id_ed25519"; f.write_text("SECRETLINE\nSECRETLINE2\n", encoding="utf-8")
    return f


def test_edit_refuses_secret_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _secret(tmp_path)
    out = str(E._run({"file_path": str(f), "old_string": "nothing-here", "new_string": "x"}))
    assert out.startswith("Error:") and "SECRETLINE" not in out, out[:200]
    assert "SECRETLINE" in f.read_text(encoding="utf-8")


def test_apply_diff_refuses_secret_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _secret(tmp_path)
    diff = f"--- a/{f.name}\n+++ b/{f.name}\n@@ -1,2 +1,2 @@\n-nothing-here\n+x\n SECRETLINE\n"
    inp = {"path": str(f), "diff": diff, "file_path": str(f), "patch": diff}
    fn = getattr(D, "_run", None) or getattr(D, "run")
    out = str(fn(inp))
    assert "SECRETLINE" not in out
