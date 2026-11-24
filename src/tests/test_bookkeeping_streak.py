"""記帳の連続は、**道具名ではなく「記帳かどうか」**で数える。

★実機で見えた形（v0.1.76・2026-08-22）:
  道具 36 回中 16 回が記帳（44%）だったのに、差し戻しは **2 回**しか鳴らなかった。
  条件が「同じ道具名の連続」だったため、モデルが
  TodoWrite → TaskUpdate → TaskList → TaskGet と**種類を回す**と素通りする。

  これは記録にある形そのもの——「TodoWrite 28連発を潰したら
  TaskUpdate→TaskStop→TaskList→TaskGet の4周期が8巡した」。
  ループ検知は周期を見るよう直したのに、こちらは同名のままだった。
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 2026-08-24 run22 試行3: 上の試験は**自作の _Spec** で書かれていたため、
# 本物の Task* ToolSpec に bookkeeping=True が無いことを見逃した
# （＝試験は緑・実機では Task 呼び出しのたびに streak がリセットされ素通り）。
# 「きれいな指紋で書いた試験は緑のまま本番だけ死ぬ」の再演。実物を見る。
# ---------------------------------------------------------------------------

def test_real_task_specs_carry_bookkeeping_flag():
    """★配線試験: 本物のレジストリの Task*/TodoWrite が bookkeeping=True であること。"""
    from laps.tools import task_mgmt, todowrite
    for spec in task_mgmt.SPECS:
        assert getattr(spec, "bookkeeping", False), \
            f"{spec.name} に bookkeeping=True が無い（streak が実機でリセットされる）"
    assert any(getattr(s, "bookkeeping", False)
               for s in getattr(todowrite, "SPECS", [todowrite.SPEC] if hasattr(todowrite, "SPEC") else []))


