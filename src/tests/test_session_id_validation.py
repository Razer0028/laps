"""セッション id は裸の 1 要素だけ（2026-08-23 レビュー high: `/resume ../x` で任意ディレクトリに書けた）。"""
from __future__ import annotations

import pytest

from laps import session as S


# ★末尾の改行（2026-09-05・r4c・第4回c 反証 #2）: Python の `$` は「末尾の改行 1 個の
#   手前」にも一致するので、旧 `^...$` は `"abc\n"` を通していた（実測で
#   `sessions/abc\n/events.jsonl` が作られた）。`Gateway.session_events` の旧実装は
#   `re.fullmatch` で弾いていたので、書けるのに読めない退行になっていた。
@pytest.mark.parametrize("bad", ["../x", "/etc", "a/b", "..", "", ".hidden", "a\\b", "x" * 200,
                                 "abc\n", "abc\r", "abc\n\n", "abc\r\n", "abc\n/x", "\n"])
def test_bad_ids_are_rejected(bad, tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    assert not S.is_valid_session_id(bad)
    with pytest.raises(ValueError):
        S.Session.load(bad)


def test_normal_ids_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    sid = "20260823_105210_638021"
    assert S.is_valid_session_id(sid)
    (S.sessions_dir() / sid).mkdir(parents=True)
    assert S.Session.load(sid).id == sid


def test_resume_command_refuses_bad_id(tmp_path, monkeypatch):
    from laps.term import slash
    from rich.console import Console
    import io
    class Ctx: pass
    ctx = Ctx(); ctx.console = Console(file=io.StringIO(), record=True, width=80); ctx.session = None; ctx.client = None
    slash.dispatch(ctx, "/resume ../../etc")
    assert ctx.session is None and "不正" in ctx.console.export_text()


# ---------------------------------------------------------------------------
# serve 経路（線の向こうから来る session）— 2026-09-05・r4c・第4回b 反証 must_fix A
#
# `Server._owner_sink` は「依頼の持ち主 session の events.jsonl へ書く」ために
# `events.session_log_path(session_id)` を呼ぶ新経路で、`Session.load` を通らない
# ＝上の `is_valid_session_id` の門を迂回していた。とくに `_accept_send` の Rejected 枝は
# `parse_send` を一度も通っていない生の `obj.get("session")` を使うので、**弾いた要求**で
# sessions_dir の外にディレクトリと events.jsonl を作れた（反証の PoC）。
# ここでは本番の `Server._accept_send` / `Server._accept_cancel` を直接呼んで固定する。
# ---------------------------------------------------------------------------

_BAD_SIDS = ["../x", "../../victimdir", "/etc", "a/b", "..", ".hidden", "a\\b", "x" * 200,
             # ★改行つき（第4回c 反証 #2）。旧 `_SID_RE` はここを通し、線の向こうから
             #   来た `"s1\n"` で `sessions/s1\n/` という別 dir が生まれていた。
             "abc\n", "abc\r\n"]


def _spy_session_log_path(monkeypatch, tmp_path):
    """`events.session_log_path` を「呼ばれたら記録して落ちる」スパイに差し替える。

    `_owner_sink` は関数の中で `from .events import session_log_path` するので、
    モジュール属性を差し替えれば本番の呼び出しがそのまま捕まる。"""
    from laps import events as E
    calls: list[str] = []

    def spy(sid: str):
        calls.append(sid)
        return tmp_path / "sessions" / sid / "events.jsonl"

    monkeypatch.setattr(E, "session_log_path", spy)
    return calls








