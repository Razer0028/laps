"""★2026-09-08 監査（clients high）: Web の「受理」の集合が term/Windows とずれ、accepted_after_pushback を ✗ にしていた。
3 クライアントの ACCEPTED 集合を突き合わせる。"""
import pathlib, re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _web():
    src = (ROOT / "laps" / "webui" / "app.js").read_text(encoding="utf-8")
    m = re.search(r'const ACCEPTED = \[([^\]]+)\]', src)
    assert m, "app.js に ACCEPTED が無い"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


def test_web_accepted_matches_term():
    from laps.term import render as R
    assert _web() == set(R._ACCEPTED), (_web(), R._ACCEPTED)


def test_web_accepted_matches_windows():
    rs = ROOT.parent / "app" / "src-tauri" / "blackwall-core" / "src" / "wire.rs"
    if not rs.exists():
        return
    m = re.search(r'pub const ACCEPTED: &\[&str\] = &\[([^\]]+)\]', rs.read_text(encoding="utf-8"))
    assert m
    assert _web() == set(re.findall(r'"([a-z_]+)"', m.group(1)))
