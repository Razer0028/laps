"""帯7 —— 最深段（FLOOR_LEVEL）だけで効く「受領書」（2026-09-04・OSS 由来 A5）。

出典: goose/opencode の「古い道具結果は本体でなく計算結果（受領書）を返す」。
`REPORTS/oss-agents-study-20260902.md` §A5。

★測ってから入れた: run29/31/32/34（4 走行・5,709 手番）の再生で level=8（最深段）
  が全手番の 38%（2,167 件）を占めていた——すでにいちばん激しく削っている段が、
  いちばん多く選ばれている。その段でだけ、頭/尾の行を完全に落として1行受領書に
  差し替える。再生での前後比較（本文の該当節）を参照。

ここでは production の `laps.context_budget` を直接呼ぶ（写さない）。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from laps import context_budget as cb  # noqa: E402
from test_context_budget import counter, ev, tool_msg, lines, workload  # noqa: E402


def _plan_at(msgs, inputs, level, n_ctx=1_000_000, keep_last=0):
    """指定の段を直接その場で使わせる（`min_level` は開始段＝予算に余裕があれば
    そのまま採用される）。既定 keep_last=0: 単発の小さな transcript で「直近
    keep_last 本は保護」に巻き込まれないようにする（保護窓そのものは別の試験で見る）。"""
    return cb.plan(msgs, cb.Budget(n_ctx=n_ctx, gen=4096, fixed=100), counter(),
                   inputs=inputs, min_level=level, keep_last=keep_last)


# --------------------------------------------------------------------------
# 1. 最深段だけで効く
# --------------------------------------------------------------------------

def test_浅い段では受領書にならない_head_tailのまま():
    msgs, inputs = workload(n_files=3, n_cmds=0, big=200)
    p = _plan_at(msgs, inputs, level=6)
    assert p.counters["ctx_budget_band7_receipt"] == 0
    assert p.counters["ctx_budget_band2_stub"] > 0, "浅い段で head/tail が効いていない"


def test_最深段では受領書に差し替わる():
    msgs, inputs = workload(n_files=3, n_cmds=0, big=200)
    p = _plan_at(msgs, inputs, level=cb.FLOOR_LEVEL)
    assert p.counters["ctx_budget_band7_receipt"] > 0, "最深段なのに帯7が1件も動いていない"
    # 受領書に置き換わった項目は、その場では head/tail を経由していない
    # （band2_stub にも計上されない ＝ 二重の畳みをしていない）。
    joined = "\n".join(str(m.get("content", "")) for m in p.messages)
    assert "受領書:" in joined


def test_最深段でも短い本文は受領書にしない():
    """head+tail+1 行以下の本文は、そもそも head/tail でも削れない＝受領書化も不要。"""
    msgs, inputs = workload(n_files=1, n_cmds=0, big=2)   # 2行しかない
    p = _plan_at(msgs, inputs, level=cb.FLOOR_LEVEL)
    assert p.counters["ctx_budget_band7_receipt"] == 0


# --------------------------------------------------------------------------
# 2. 受領書の中身（要約ではなく、数えられる事実＋取り直し方）
# --------------------------------------------------------------------------

def test_受領書は文字数_先頭_末尾_取り直し方を書く():
    msgs = [{"role": "user", "content": "goal"},
            tool_msg(ev(1, "Read", lines(50, "z")))]
    inputs = {1: {"file_path": "/tmp/z.py"}}
    p = _plan_at(msgs, inputs, level=cb.FLOOR_LEVEL)
    joined = "\n".join(str(m.get("content", "")) for m in p.messages)
    assert "受領書:" in joined
    assert "文字" in joined and "行" in joined
    assert "先頭" in joined and "末尾" in joined
    assert "要約ではありません" in joined
    assert 'Read(file_path="/tmp/z.py"' in joined, "取り直し方（Read）が書かれていない"


def test_Bashの受領書は同じコマンドの取り直し方を書く():
    msgs = [{"role": "user", "content": "goal"},
            tool_msg(ev(1, "Bash", lines(50, "c")))]
    inputs = {1: {"command": "cat huge.log"}}
    p = _plan_at(msgs, inputs, level=cb.FLOOR_LEVEL)
    joined = "\n".join(str(m.get("content", "")) for m in p.messages)
    assert "cat huge.log" in joined


def test_tool_receipt関数は本文を丸ごと落として要約せずに書く():
    text = "[7] Read → " + "\n".join(f"line{i} 本文" for i in range(30))
    out, dropped = cb.tool_receipt(7, "Read", {"file_path": "/tmp/a.py"}, text)
    assert dropped == 30
    assert out.startswith("[7] Read → ")
    # 先頭/末尾の**短い抜粋**（受領書の一部）は残ってよいが、中間の行は1つも残らない。
    assert "line0 本文" in out and "line29 本文" in out
    for i in range(5, 25):
        assert f"line{i} 本文" not in out, "中間の行がそのまま残っている（要約ではなく除去のはず）"


def test_tool_receipt関数は1行以下なら何もしない():
    text = "[1] Bash → 1行だけ"
    out, dropped = cb.tool_receipt(1, "Bash", {}, text)
    assert out == text
    assert dropped == 0


# --------------------------------------------------------------------------
# 3. 誤爆止め: band3（エラー本文）・保護窓（keep_last / user）には触らない
# --------------------------------------------------------------------------

def test_エラー本文はband3が先取りしてband7は触らない():
    err_body = "Error: 何かに失敗しました\n" + "\n".join(f"traceback行{i}" for i in range(30))
    msgs = [{"role": "user", "content": "goal"},
            tool_msg(ev(1, "Bash", err_body))]
    p = _plan_at(msgs, {1: {"command": "boom"}}, level=cb.FLOOR_LEVEL)
    assert p.counters["ctx_budget_band3_errbody"] == 1
    assert p.counters["ctx_budget_band7_receipt"] == 0, "band3 が扱うべき失敗本文に band7 が触れている"


def test_直近keep_last本のtool結果は最深段でも全文で残る():
    msgs, inputs = workload(n_files=5, n_cmds=0, big=200)
    p = cb.plan(msgs, cb.Budget(n_ctx=1_000_000, gen=4096, fixed=100), counter(),
               inputs=inputs, min_level=cb.FLOOR_LEVEL, keep_last=2)
    tool_msgs = [m for m in p.messages if m.get("role") == "tool"]
    # 直近2本は「受領書:」を含まない（全文保護）
    for m in tool_msgs[-2:]:
        assert "受領書:" not in str(m.get("content", "")), \
            "直近 keep_last のツール結果が受領書に畳まれている（読んだはずの物が消える）"


def test_人が書いたuserは最深段でも触らない():
    msgs, inputs = workload(n_files=5, n_cmds=0, big=200)
    before_user = msgs[0]["content"]
    p = cb.plan(msgs, cb.Budget(n_ctx=1_000_000, gen=4096, fixed=100), counter(),
               inputs=inputs, min_level=cb.FLOOR_LEVEL)
    assert p.messages[0]["content"] == before_user


# --------------------------------------------------------------------------
# 4. 落とした行数の整合・冪等性
# --------------------------------------------------------------------------

def test_受領書での省略行数はcountersと一致する():
    import re
    msgs, inputs = workload(n_files=4, n_cmds=4, big=300)
    p = _plan_at(msgs, inputs, level=cb.FLOOR_LEVEL)
    said = 0
    for m in p.messages:
        for mm in re.finditer(r"(?:中略|失敗の詳細|経過の記述) (\d+) 行を省略",
                              str(m.get("content", ""))):
            said += int(mm.group(1))
    assert said > 0
    assert said == p.counters["ctx_budget_lines_dropped"], (
        f"受領書が言う省略行数 {said} と counters {p.counters['ctx_budget_lines_dropped']} が不一致")


def test_同じ入力なら受領書は毎回バイト一致する():
    """接頭辞キャッシュのため、段が同じなら投影はバイト単位で同じでなければならない。"""
    msgs, inputs = workload(n_files=3, n_cmds=0, big=200)
    p1 = _plan_at(msgs, inputs, level=cb.FLOOR_LEVEL)
    p2 = _plan_at(msgs, inputs, level=cb.FLOOR_LEVEL)
    assert [m.get("content") for m in p1.messages] == [m.get("content") for m in p2.messages]
