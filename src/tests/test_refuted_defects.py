"""反証で実際に再現した欠陥を、直したうえで留める（2026-09-01・合流時）。

ここに並ぶのは全部「前の担当が入れた直しの**中に**あった穴」で、
反証の担当が実機で再現して見せたもの。数字は分母つきで各試験の説明に書く。

★この現場の作法どおり、**救ってはいけない例と対で**書く
  （締めすぎ＝正しい観測を「取れなかった」と読ませる、も同じくらい高くつく）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections import OrderedDict

import pytest

from laps.tools import as_result, fold
from laps.tools import grep as g


# ── Grep: 1 本のファイルを名指ししたとき ────────────────────────────────
# 実記録での出現率: 単一ファイル指定の Grep は 28/237（12%）。


def _engines(monkeypatch, mode: str):
    """mode: rg / grep / py"""
    real = shutil.which
    if mode == "rg":
        return
    drop = {"rg"} if mode == "grep" else {"rg", "grep"}
    monkeypatch.setattr(g.shutil, "which",
                        lambda n, _r=real: None if n in drop else _r(n))


@pytest.mark.parametrize("mode", ["rg", "grep", "py"])
def test_単一ファイル指定でもファイル名が出る(tmp_path, monkeypatch, mode):
    """★rg も grep も、引数が 1 本のファイルのときはファイル名を出さない。

    すると `_render` の束キー（`ln.split(":",1)[0]`）に**行番号**が入り、
    実測（修正前）: `Grep(pattern="def ", path="…/fold.py")` が
    「一致したファイル: 6 本。」と言った（1 本です）。
    """
    f = tmp_path / "one.txt"
    f.write_text("needle\nx\nneedle\n", encoding="utf-8")
    _engines(monkeypatch, mode)
    out = g.SPEC.handler({"pattern": "needle", "path": str(f)})
    assert "one.txt:1:needle" in out, out[:200]
    assert "一致したファイル" not in out, "1 本しかないのに複数と数えている"


@pytest.mark.parametrize("mode", ["rg", "grep", "py"])
def test_無視対象の中にいても名指しされたファイルは探す(tmp_path, monkeypatch, mode):
    """★これがいちばん高くつく嘘（「その語は無い」）。

    実測（修正前・rg の無い機械）: repo/.gitignore = "work/"、cwd = repo/work で
    `Grep(path="f.txt")` が **(no matches)**。rg は 8 件返す。
    原因は `_root_is_ignored` が `os.path.isdir` で門前払いしており、
    1 本のファイルを名指しした場合に働かなかったこと。
    """
    if not shutil.which("git"):
        pytest.skip("git が無い")
    repo = tmp_path / "repo"
    (repo / "work").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / ".gitignore").write_text("work/\n", encoding="utf-8")
    (repo / "work" / "f.txt").write_text("needle\n" * 8, encoding="utf-8")
    _engines(monkeypatch, mode)
    monkeypatch.chdir(repo / "work")
    out = g.SPEC.handler({"pattern": "needle", "path": "f.txt"})
    assert out.count("f.txt:") == 8, out[:300]


def test_無視対象そのものは名指ししない限り外す(tmp_path, monkeypatch):
    """★救ってはいけない側。上を「全部探す」にすると rg との食い違いが戻る。"""
    if not shutil.which("git"):
        pytest.skip("git が無い")
    repo = tmp_path / "repo"
    (repo / "work").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / ".gitignore").write_text("work/\n", encoding="utf-8")
    (repo / "work" / "f.txt").write_text("needle\n", encoding="utf-8")
    (repo / "kept.txt").write_text("needle\n", encoding="utf-8")
    _engines(monkeypatch, "grep")
    out = g.SPEC.handler({"pattern": "needle", "path": str(repo)})
    assert "kept.txt" in out
    assert "work/f.txt" not in out, "無視対象を根から探して rg と食い違っている"


def test_無視の前方一致が兄弟を巻き込まない():
    """`build` を無視しても `builds` は無視ではない（os.sep を落とすと壊れる）。"""
    ig = frozenset({os.path.abspath("/a/build")})
    assert g._is_ignored("/a/build/x", ig) is True
    assert g._is_ignored("/a/builds/x", ig) is False


def test_python走査の既定の制限時間は30秒():
    """★env を自分で置いてから測る試験しか無いと、**出荷時の既定**が一度も試されない。"""
    old = os.environ.pop("LAPS_GREP_TIMEOUT", None)
    try:
        assert g._walk_timeout() == 30.0
    finally:
        if old is not None:
            os.environ["LAPS_GREP_TIMEOUT"] = old


# ── fold.round_robin: 公平さを保ったまま止まるか ──────────────────────


def test_大きな束でも即座に返る():
    """★反証で再現した暴走。`return`→`continue` にしたら止まらなくなっていた。

    実測（修正前）: 50,000+2,000 → 2.88 秒 ／ 100,000+5,000 → 15.6 秒 ／
    200,000+10,000 → 65 秒（表示 509 行のために CPU 65 秒。Grep の 30 秒上限の外側）。
    同じ長さの警告が並ぶログ（`site/logs/server.log`）が現実の引き金。
    修正後は同じ入力・同じ出力で 0.01 秒未満。
    """
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    groups["big.log"] = [f"big.log:{i}:WARN same warning line padded out here"
                         for i in range(100_000)]
    for k in range(5_000):
        groups[f"s{k}.py"] = [f"s{k}.py:1:match here"]
    calls = [0]
    orig = fold.nbytes
    try:
        fold.nbytes = lambda t, _o=orig: (calls.__setitem__(0, calls[0] + 1) or _o(t))
        t0 = time.monotonic()
        shown, _ = fold.round_robin(groups, 30_000, reserve=520)
        dt = time.monotonic() - t0
    finally:
        fold.nbytes = orig
    assert dt < 2.0, f"{dt:.2f} 秒かかった（修正前は 15.6 秒）"
    assert len(shown) > 100, "止めるために表示まで削ってはいけない"
    # ★時間だけを見ると「尽きた束を毎周落とす」だけの半端な直しが素通りする
    #   （実測: 時間は同じでも走査量は 21 倍）。仕事量そのものを留める。
    #   現行 5,002 回 ／ その周に 1 本も置けなくても続ける版 105,000 回。
    assert calls[0] < 20_000, f"予算が埋まったあとも舐め続けている（{calls[0]:,} 回）"


def test_入らない1本が他の束を巻き添えにしない():
    """★救ってはいけない側。旧実装は最初の非適合で配分ごと打ち切っていた。"""
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    groups["huge.txt"] = ["huge.txt:1:" + "x" * 5_000]
    groups["a.py"] = ["a.py:1:short"]
    groups["b.py"] = ["b.py:1:short"]
    shown, _ = fold.round_robin(groups, 1_000, reserve=0)
    assert any("a.py" in s for s in shown) and any("b.py" in s for s in shown), shown
