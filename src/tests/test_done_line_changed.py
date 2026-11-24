"""締めの行に「調べただけ」と「直した」の区別を出す（2026-09-11・vdir #v94）。

★出どころ: vdir が同じ故障・同じ依頼文で**現場の大きさだけ**変えた対照実験。
  小さい現場: 直った（0→2件）・1本書いた・圧縮 0 回
  大きい現場: **直っていない・1本も書いていない**・圧縮 8 回 —— なのに outcome は
  両方とも `accepted`。記録上は成功と同じに見え、利用者は直ったと読む。

★方針: outcome は書き換えない（過去の記録と比べられなくなる・意図を当てにいくと
  質問に答えただけの回を失敗と貼る）。**事実を足して見えるようにする**だけ。
"""
from __future__ import annotations

from laps.term import render as R


def _line(done: dict, secs: float = 30.0, n_tools: int = 5) -> str:
    r = R.Renderer.__new__(R.Renderer)
    r._bundle = []
    r._width = lambda: 200
    t = R.Renderer._done_line(r, done, secs, n_tools)
    return "" if t is None else str(t)


def test_変更なしなら締めに出る():
    s = _line({"outcome": "accepted", "changed": False})
    assert "変更なし" in s, s


def test_変更があれば出さない():
    s = _line({"outcome": "accepted", "changed": True})
    assert "変更なし" not in s, s


def test_判定できないときは断定しない():
    """★None は「分からない」。控えが無いのに『変更なし』と貼らない。"""
    s = _line({"outcome": "accepted", "changed": None})
    assert "変更なし" not in s, s
    s2 = _line({"outcome": "accepted"})          # キー自体が無い
    assert "変更なし" not in s2, s2


def test_圧縮回数が出る():
    s = _line({"outcome": "accepted", "changed": True, "compactions": 8})
    assert "圧縮" in s and "8" in s, s


def test_圧縮0なら出さない():
    s = _line({"outcome": "accepted", "changed": True, "compactions": 0})
    assert "圧縮" not in s, s


def test_outcome_は書き換えていない():
    """★accepted のままであること（過去の記録と比較可能に保つ）。"""
    s = _line({"outcome": "accepted", "changed": False})
    assert "同じ手の繰り返し" not in s and "不合格" not in s


def test_vdir_の大きい現場の形():
    """実測の再現: accepted・0本・圧縮8回 → 締めに『変更なし』と『圧縮 8』が出る。"""
    s = _line({"outcome": "accepted", "changed": False, "compactions": 8}, secs=168, n_tools=17)
    assert "変更なし" in s and "圧縮" in s, s


# ── 本体側: 作業場が実際に変わったかの判定 ──────────────────────────
import laps.oc.base as agent_mod


def _agent(files=(), ck=None):
    a = agent_mod.Agent.__new__(agent_mod.Agent)
    a.state = type("S", (), {"files_written": list(files), "tools_called": []})()
    a.checkpoint = ck
    return a


class _Ck:
    def __init__(self, touched=(), untracked=(), bash=None):
        self._t, self._u, self._b = list(touched), list(untracked), bash or {}
    def touched(self): return self._t
    def untracked_since_watermark(self): return self._u
    def bash_effects_since_watermark(self): return self._b


def test_書いたファイルがあれば変更あり():
    assert _agent(files=["a.py"])._workspace_changed() is True


def test_控えが無ければ判定しない():
    """★None を返す。控えが無いのに『変更なし』と断定すると、直っている回を誤って貼る。"""
    assert _agent()._workspace_changed() is None


def test_checkpoint_が触れたものを見る():
    assert _agent(ck=_Ck(touched=["x.php"]))._workspace_changed() is True
    assert _agent(ck=_Ck(untracked=["new.txt"]))._workspace_changed() is True


def test_Bash_由来の変更も捕まえる():
    """★files_written だけでは sed -i / > file を見落とす（これが vdir の事例の形）。"""
    assert _agent(ck=_Ck(bash={"written": ["conf.ini"]}))._workspace_changed() is True


def test_何も変わっていなければ_False():
    assert _agent(ck=_Ck())._workspace_changed() is False


def test_checkpoint_が壊れても走行を止めない():
    class Boom:
        def touched(self): raise RuntimeError("boom")
    assert _agent(ck=Boom())._workspace_changed() is None
