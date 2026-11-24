"""同じ問いが、ripgrep の有無で別の答えになっていた（第三者レビュー E1）。

2026-08-31 に**再現してから**直した。同じ木で 4 つのパターンを両エンジンに投げた実測:

    pattern        rg          旧 `grep -e`（BRE）
    ------------   ---------   ------------------
    cat|dog        2 件        **0 件**
    foo\\d+         1 件        **0 件**
    foo[0-9]\\+     0 件        **1 件**
    alpha          4 件        5 件（`.hidden/` も読む）

道具は 0 件を「(no matches)」としか言わないので、**rg の無い機械では
「その文字列は存在しない」という嘘**が返っていた。終了コードを見ていなかった頃の
不具合（2026-08-30 に修正済み）と同じ形が、エンジンの違いという別の入口から開いていた。

⚠ この試験を書くとき、比較に使った shell の `grep` が別実装への差し替え
  （ugrep への関数）だったせいで一度誤った結論を出した。**器を疑うこと。**
  ここでは道具が実際に呼ぶ `shutil.which("grep")` の実体だけを見る。
"""
from __future__ import annotations

import shutil

import pytest

from laps.tools import grep as gr


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setattr(gr, "_GREP_FLAG", None)     # エンジン判定の記憶を毎回消す


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "animals.txt").write_text("cat\ndog\n", encoding="utf-8")
    (tmp_path / "num.txt").write_text("foo123\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "c.txt").write_text("alpha\n", encoding="utf-8")
    return tmp_path


def _no_rg(monkeypatch):
    """rg だけ「無い」ことにする（grep は本物を使う）。"""
    real = shutil.which
    monkeypatch.setattr(gr.shutil, "which",
                        lambda n, *a, **k: None if n == "rg" else real(n, *a, **k))


def _no_rg_no_grep(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(gr.shutil, "which",
                        lambda n, *a, **k: None if n in ("rg", "grep") else real(n, *a, **k))


@pytest.mark.parametrize("pattern,needle", [("cat|dog", "cat"), (r"foo\d+", "foo123")])
def test_代替エンジンでも同じ綴りが当たる(tree, monkeypatch, pattern, needle):
    """★ここが本体。修正前は代替経路だけ `(no matches)` を返していた。"""
    _no_rg(monkeypatch)
    out = str(gr._run({"pattern": pattern, "path": str(tree)}))
    assert needle in out, f"rg の無い機械で当たらない: {out[:200]}"


@pytest.mark.parametrize("pattern,needle", [("cat|dog", "cat"), (r"foo\d+", "foo123")])
def test_rgもgrepも無い機械でも動く(tree, monkeypatch, pattern, needle):
    """旧実装はここで `Error: [Errno 2] ... 'grep'`＝道具が丸ごと使えなかった（Windows）。"""
    _no_rg_no_grep(monkeypatch)
    out = str(gr._run({"pattern": pattern, "path": str(tree)}))
    assert needle in out, out[:200]


def test_隠しディレクトリの扱いが両エンジンで揃う(tree, monkeypatch):
    if not shutil.which("rg"):
        pytest.skip("rg が無い機械では比べられない")
    with_rg = str(gr._run({"pattern": "alpha", "path": str(tree)}))
    _no_rg(monkeypatch)
    without_rg = str(gr._run({"pattern": "alpha", "path": str(tree)}))
    for out in (with_rg, without_rg):
        assert "sub/b.txt" in out.replace("\\", "/")
        assert ".hidden" not in out, f"隠しを歩いている: {out[:200]}"


def test_隠しディレクトリを名指ししたら検索する(tree, monkeypatch):
    """一律に除外すると、食い違いを逆向きに作り直すだけになる（rg は名指しなら読む）。"""
    _no_rg(monkeypatch)
    out = str(gr._run({"pattern": "alpha", "path": str(tree / ".hidden")}))
    assert "c.txt" in out, out[:200]


def test_起点そのものを除外してしまわない(tree, monkeypatch):
    """★実際に踏んだ罠: `--exclude-dir=.*` は**検索の起点 `.` 自身**に当たり、
    どんな語でも 0 件になる。綴りは `.?*` でなければならない。"""
    _no_rg(monkeypatch)
    assert "--exclude-dir=.*" not in gr._hidden_excludes(".")
    # 起点 "." そのものを試すのが目的。ただし cwd はプロセス共有なので、全体スイートでは
    # 直前の試験が残した巨大な cwd を歩いて `grep timeout` になった（2026-09-06 preflight 実測）。
    # 小さな木へ chdir してから "." を渡す＝意図は保ったまま、他の試験の cwd に依存しない。
    monkeypatch.chdir(tree)
    out = str(gr._run({"pattern": "alpha", "path": "."}))
    # 起点 "." で走らせても、少なくとも「壊れて 0 件」ではないこと
    assert "Error" not in out.splitlines()[0]
    assert "a.txt" in out or "alpha" in out, out[:200]


def test_表せない綴りは0件と言わずに断る(tree, monkeypatch):
    """-P を持たない grep しか無い機械。`foo\\d+` は -E では `food+` として
    **黙って別のものに一致する**ので、答えを返さずに断る。"""
    _no_rg(monkeypatch)
    monkeypatch.setattr(gr, "_GREP_FLAG", "-E")
    out = str(gr._run({"pattern": r"foo\d+", "path": str(tree)}))
    assert out.startswith("Error:")
    assert "0 件" in out and "[0-9]" in out


def test_0件のときどのエンジンで引いたかが分かる(tree, monkeypatch):
    _no_rg(monkeypatch)
    out = str(gr._run({"pattern": "存在しない語ZZZ", "path": str(tree)}))
    assert out.startswith("(no matches)")      # 下流の 0 件判定を壊さない
    assert "ripgrep" in out                    # どの綴りで引いた 0 件かは言う
    from laps.tools import as_result
    assert as_result(out).kind == "empty"
