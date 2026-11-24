"""実行ゲートの例外クラスが、木全体で**1つ**であること。

★実際に起きた形（2026-08-21）:
  ある試験が `importlib.reload(laps.policy)` を呼んでいた。
  リロードすると `PolicyDenied` は**新しいクラス**になる。
  先に import 済みの道具（write / edit / apply_diff / notebook_edit）は
  `from ..policy import PolicyDenied` で**古いクラス**を握ったままなので、
  `except PolicyDenied` が一致せず、**例外が道具の外へ抜ける**。

  症状が分かりにくい:
  - リロードした試験自体は通る
  - **あとから走る無関係な試験が落ちる**
  - 対で走らせると通る（全体で走らせたときだけ落ちる）
  私はこれを「順序依存」「別の試験の env 汚染」と2回誤診し、
  **クラスの id を実際に測るまで**分からなかった。

★この試験は「推測するな、測れ」を機械にしたもの。
  誰かがまた reload を足したら、ここが名指しで落ちる。
"""
from __future__ import annotations

import sys

import pytest


def test_policy_denied_is_one_class():
    from laps import policy
    from laps.tools import apply_diff, edit, notebook_edit, write

    holders = {
        "laps.policy": policy.PolicyDenied,
        "sys.modules": sys.modules["laps.policy"].PolicyDenied,
        "tools.write": write.PolicyDenied,
        "tools.edit": edit.PolicyDenied,
        "tools.notebook_edit": notebook_edit.PolicyDenied,
        "tools.apply_diff": apply_diff.PolicyDenied,
    }
    ids = {k: id(v) for k, v in holders.items()}
    assert len(set(ids.values())) == 1, (
        "PolicyDenied が分裂しています。どこかで importlib.reload(laps.policy) を"
        f"呼んでいませんか。道具の except が一致せず、例外が外へ抜けます: {ids}")


def test_no_test_reloads_the_policy_module():
    """★根を止める。reload さえしなければ分裂は起きない。

    policy.py の `_mode()` は毎回 `os.environ` を読むので、
    モードを変えるのに reload は**元々要らない**（monkeypatch.setenv で足りる）。
    """
    import ast
    from pathlib import Path
    # ★**文章ではなく実際の呼び出し**を見る（構文木で判定）。
    #   最初この門は行の文字列一致で書いたので、
    #   「reload をしない」と説明した**自分のコメントを咎めた**。
    #   門が粗いと、直す必要のない場所を直しにいくことになる（本日2度目）。
    here = Path(__file__).resolve().parent
    offenders = []
    for f in sorted(here.glob("test_*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute) else
                    fn.id if isinstance(fn, ast.Name) else "")
            if name != "reload" or not node.args:
                continue
            arg = ast.unparse(node.args[0])
            if "policy" in arg:
                offenders.append(f"{f.name}:{node.lineno} reload({arg})")
    assert not offenders, (
        "policy を reload している試験があります（PolicyDenied が分裂します）。"
        f"monkeypatch.setenv で足ります: {offenders}")
