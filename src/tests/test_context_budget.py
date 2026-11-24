"""文脈予算（laps/context_budget.py）の約束を固定する。

この機能は**既定 ON** で、しかも全 run の入力を書き換える。だから壊れ方が静か
になりやすい: 誰も落ちず、誰も怒らず、モデルだけが「読んだはずのもの」を見失って
埋め合わせに嘘をつく。ここでの試験は、その静かな壊れ方を1つずつ音の出る形に
置き換えることを狙っている。

とくに次の3本は防波堤として扱う（これが緑でないなら既定 ON にしてはいけない）:

  1. 予算内なら**バイト単位で恒等**（誤爆ゼロを直接測る）
  2. 圧縮しても**記録（session.messages）は変わらない**
  3. 落とした行数は**必ず counters に出る**（黙って消えていない証拠）

近似の試験は llama-server を必要としない。実測値は 2026-08-11 に 推論機 の
qwen3-30b-a3b（Q4_K_M / n_ctx 32768）へ実際に /tokenize を投げて取った数字を
そのまま埋めてある＝サーバが居なくても「近似が実測を下回らない」を検査できる。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps import context_budget as cb  # noqa: E402


# --------------------------------------------------------------------------
# 道具
# --------------------------------------------------------------------------

def counter() -> cb.TokenCounter:
    """ネットに触らない計数器。近似（必ず過大側）で動く。"""
    return cb.TokenCounter(None)


def ev(n: int, tool: str, body: str) -> str:
    """_record_evidence（agent.py:436-454）と同じ書式の1件。"""
    return f"[{n}] {tool} → {body}"


def tool_msg(*items: str) -> dict:
    """agent.py:664, 910 と同じ連結（★1本に複数結果が入る）。"""
    return {"role": "tool", "content": "\n\n".join(items)}


def lines(n: int, tag: str = "x") -> str:
    return "\n".join(f"{tag}{i:04d} " + "ダミー本文 " * 4 for i in range(n))


def workload(n_files: int = 8, n_cmds: int = 8, big: int = 400) -> tuple[list[dict], dict]:
    """20 Read + 30 Bash 相当の形をした、決定論的な transcript。"""
    msgs: list[dict] = [{"role": "user", "content": "このリポジトリを調べて報告して"}]
    inputs: dict[int, dict] = {}
    n = 0
    for i in range(n_files):
        n += 1
        inputs[n] = {"file_path": f"/tmp/f{i}.py"}
        msgs.append({"role": "assistant", "content": f"/tmp/f{i}.py を読みます。"})
        msgs.append(tool_msg(ev(n, "Read", lines(big, f"f{i}"))))
    for i in range(n_cmds):
        n += 1
        inputs[n] = {"command": f"echo cmd{i}"}
        msgs.append({"role": "assistant", "content": f"cmd{i} を実行します。"})
        msgs.append(tool_msg(ev(n, "Bash", lines(big, f"c{i}"))))
    return msgs, inputs


def snapshot(msgs: list[dict]) -> list[tuple]:
    return [(m.get("role"), m.get("content")) for m in msgs]


# --------------------------------------------------------------------------
# 1-2. 誤爆しないこと（予算内では何も起きない）
# --------------------------------------------------------------------------

def test_予算内なら送信リストは元のメッセージとバイト一致する():
    msgs, inputs = workload(n_files=1, n_cmds=1, big=3)
    tc = counter()
    b = cb.Budget(n_ctx=200000, gen=4096, fixed=100)
    p = cb.plan(msgs, b, tc, inputs=inputs)
    assert p.fired is False, "予算内なのに発火している"
    assert snapshot(p.messages) == snapshot(msgs), "予算内なのに中身が変わった"
    assert cb.apply_to(msgs, p) == msgs


def test_予算内では_counters_が全て_0_のままである():
    msgs, inputs = workload(n_files=1, n_cmds=1, big=3)
    p = cb.plan(msgs, cb.Budget(n_ctx=200000, gen=4096, fixed=100), counter(),
                inputs=inputs)
    assert set(p.counters.values()) == {0}, f"発火していないのに計数された: {p.counters}"
    assert p.stub_ns == []
    assert p.level == 0


def test_短い会話では梯子を1段も降りない():
    """予算に収まっているのに段が深くなると、次ターン以降ずっと削り続ける。"""
    msgs, inputs = workload(n_files=2, n_cmds=2, big=5)
    p = cb.plan(msgs, cb.Budget(n_ctx=100000, gen=4096, fixed=100), counter(),
                inputs=inputs)
    assert p.level == 0 and p.exhausted is False


# --------------------------------------------------------------------------
# 3. 分解と結合（★tool メッセージは1本＝1結果ではない）
# --------------------------------------------------------------------------

def test_tool_メッセージの分割と結合は原文を復元する():
    block = "\n\n".join([
        ev(1, "Read", "  1  import os\n  2  import sys"),
        ev(2, "Bash", "total 12\ndrwxr-xr-x 2 root root"),
        ev(3, "Grep", "hit"),
    ])
    items = cb.split_items(block)
    assert [i[0] for i in items] == [1, 2, 3]
    assert [i[1] for i in items] == ["Read", "Bash", "Grep"]
    assert cb.join_items(items) == block, "往復で原文に戻らない＝静かに1バイト落ちる"


def test_印のない本文は丸ごと1件として扱われ決して捨てられない():
    block = "生の出力\nこれには [n] の印がない"
    items = cb.split_items(block)
    assert items == [(None, "", block)]
    assert cb.join_items(items) == block


def test_往復が一致しない連結は分割せず丸ごと1件にする():
    """区切りが "\\n\\n" でない位置に印が現れると復元できない。
    そこで諦めるほうが、1バイトずれた transcript を送るより安い。"""
    block = ev(1, "Read", "本文") + "\n" + ev(2, "Bash", "本文2")   # 区切りが1つ
    items = cb.split_items(block)
    assert items == [(None, "", block)]
    assert cb.join_items(items) == block


def test_並列実行で連結された6件は1件のつもりで6件消えたりしない():
    """agent.py:664/910 の "\\n\\n".join を単位にすると、1件落とすつもりで
    並列実行の6件が消える。分割単位が evidence 項目であることの確認。"""
    block = "\n\n".join(ev(i, "Read", lines(300, f"p{i}")) for i in range(1, 7))
    msgs = [{"role": "user", "content": "goal"},
            {"role": "assistant", "content": "6本読みます"},
            tool_msg(block),
            {"role": "assistant", "content": "続けます"},
            tool_msg(ev(7, "Bash", "ok")),
            tool_msg(ev(8, "Bash", "ok2"))]
    p = cb.plan(msgs, cb.Budget(n_ctx=8000, gen=1000, fixed=100), counter())
    assert p.fired
    survived = cb.split_items(p.messages[2]["content"])
    assert [i[0] for i in survived] == [1, 2, 3, 4, 5, 6], "項目が消えた"


# --------------------------------------------------------------------------
# 4-6. 記録・grounding・番号を壊さないこと
# --------------------------------------------------------------------------

def test_圧縮しても元のメッセージ列は変更されない():
    """★ここが壊れると fabrication_guard.unsupported_in_session（agent.py:526）が
    記録を証拠に使っているので、捏造ガードが正しい報告を告発する装置に反転する。"""
    msgs, inputs = workload()
    before = snapshot(msgs)
    p = cb.plan(msgs, cb.Budget(n_ctx=8000, gen=2048, fixed=500), counter(),
                inputs=inputs)
    assert p.fired
    assert snapshot(msgs) == before, "記録そのものが書き換わった"
    assert p.messages is not msgs


def test_evidence_番号は圧縮で振り直されない():
    """番号がずれると grounding.check と stub が同時に、静かに壊れる。"""
    msgs, inputs = workload()
    p = cb.plan(msgs, cb.Budget(n_ctx=8000, gen=2048, fixed=500), counter(),
                inputs=inputs)
    seen = []
    for m in p.messages:
        if m.get("role") == "tool":
            seen += [n for n, _, _ in cb.split_items(m["content"]) if n is not None]
    assert seen == sorted(seen) == list(range(1, len(seen) + 1)), seen


def test_中略された項目にも番号の印が残る():
    msgs, inputs = workload()
    p = cb.plan(msgs, cb.Budget(n_ctx=8000, gen=2048, fixed=500), counter(),
                inputs=inputs)
    assert p.stub_ns, "何も stub 化していない"
    for m in p.messages:
        if m.get("role") == "tool":
            for n, tool, text in cb.split_items(m["content"]):
                if n in p.stub_ns:
                    assert text.startswith(f"[{n}] "), "stub から番号が消えた"


def test_stub_の件数と全文の件数の和は_evidence_件数に等しい():
    msgs, inputs = workload()
    p = cb.plan(msgs, cb.Budget(n_ctx=8000, gen=2048, fixed=500), counter(),
                inputs=inputs)
    total = sum(len([1 for n, _, _ in cb.split_items(m["content"]) if n is not None])
                for m in p.messages if m.get("role") == "tool")
    assert total == len(inputs), "項目が消えている（stub 化ではなく消失）"


# --------------------------------------------------------------------------
# 7. 絶対に落とさないもの
# --------------------------------------------------------------------------

def test_最初のユーザ発話は決して落ちない():
    msgs, inputs = workload()
    goal = msgs[0]["content"]
    p = cb.plan(msgs, cb.Budget(n_ctx=4000, gen=1000, fixed=100), counter(),
                inputs=inputs)
    assert p.messages[0]["content"] == goal


def test_差し戻しメッセージは決して落ちない():
    msgs, inputs = workload()
    objection = ("まだ完了していません。完了条件のコマンドが失敗しています:\n"
                 + lines(200, "obj"))
    msgs.append({"role": "user", "content": objection})
    p = cb.plan(msgs, cb.Budget(n_ctx=4000, gen=1000, fixed=100), counter(),
                inputs=inputs)
    assert any(m.get("content") == objection for m in p.messages), \
        "差し戻しの理由が消えた＝同じ失敗を繰り返す"


def test_直近2ターンのツール結果は全文で残る():
    """★ここを削ると『直前に読んだファイルの中身』が消えて嘘が始まる。"""
    msgs, inputs = workload()
    tools = [m for m in msgs if m.get("role") == "tool"]
    p = cb.plan(msgs, cb.Budget(n_ctx=4000, gen=1000, fixed=100), counter(),
                inputs=inputs, keep_last=2)
    kept = [m for m in p.messages if m.get("role") == "tool"]
    # ★2026-08-29: 最後の道具結果には**受領書が 1 行足される**ことがある
    #   （何を削ったかをモデルに伝える・`_append_receipt`）。守りたい不変条件は
    #   「直前に読んだ中身が **1 バイトも減らない**」ことなので、等号ではなく
    #   前方一致＋足した分だけが増えていること、で見る。
    assert kept[-1]["content"].startswith(tools[-1]["content"])
    extra = kept[-1]["content"][len(tools[-1]["content"]):]
    assert extra == "" or cb._RECEIPT_MARK in extra, f"受領書以外が足されている: {extra!r}"
    assert kept[-2]["content"] == tools[-2]["content"]
    assert kept[-3]["content"] != tools[-3]["content"], \
        "保護窓の外まで全文のまま＝帯2が効いていない（この試験が空振りしている）"


# --------------------------------------------------------------------------
# 8-10. 落としたものを数えること／梯子が締まること
# --------------------------------------------------------------------------

def test_落とした行数は必ず_counters_に計上される():
    """出力に書いた「省略 N 行」の合計と counters が合うこと。
    ★合わないなら、どこかで黙って消している。"""
    import re
    msgs, inputs = workload()
    p = cb.plan(msgs, cb.Budget(n_ctx=8000, gen=2048, fixed=500), counter(),
                inputs=inputs)
    said = 0
    for m in p.messages:
        for mm in re.finditer(r"(?:中略|失敗の詳細|経過の記述) (\d+) 行を省略", str(m.get("content", ""))):
            said += int(mm.group(1))
    assert said > 0
    assert said == p.counters["ctx_budget_lines_dropped"], (
        f"出力が言う省略行数 {said} と counters {p.counters['ctx_budget_lines_dropped']} が不一致")


def test_予算に収まるまで_head_tail_が段階的に締まる():
    """帯を1本入れるだけでは収まらない（固定閾値では 555 tok 足りなかった実測の回帰）。

    予算は数え上げた結果から作る。手で書いた定数だと、近似の係数を変えた日に
    「1段で収まってしまってこの試験が何も見ていない」状態に静かに落ちる。
    """
    msgs, inputs = workload(n_files=12, n_cmds=12, big=200)
    tc = counter()
    huge = cb.Budget(n_ctx=10 ** 9, gen=0, fixed=0, slack=0)
    lvl2 = cb.plan(msgs, huge, tc, inputs=inputs, min_level=2)
    assert lvl2.level == 2
    # 予算 = ちょうど2段目の大きさ。つまり2段目では HIGH を割れない。
    b = cb.Budget(n_ctx=lvl2.tokens_after + 8192 + 1024 + 500, gen=8192, fixed=500)
    assert b.prompt == lvl2.tokens_after
    p = cb.plan(msgs, b, tc, inputs=inputs)
    assert p.level > 2, f"2段目で止まっている（level={p.level}）"
    assert p.level < cb.FLOOR_LEVEL, "最下段まで落ちている＝段階的でない"
    assert p.tokens_after <= b.prompt, "梯子を降りきっても予算を超えている"
    assert p.tokens_after < p.tokens_before
    assert p.exhausted is False


def test_重複した出力はポインタ1行に畳まれる():
    body = lines(50, "same")
    msgs = [{"role": "user", "content": "goal"},
            tool_msg(ev(1, "Bash", body)),
            tool_msg(ev(2, "Bash", body)),
            tool_msg(ev(3, "Bash", body)),
            {"role": "assistant", "content": "a"},
            tool_msg(ev(4, "Bash", "x")),
            tool_msg(ev(5, "Bash", "y"))]
    p = cb.plan(msgs, cb.Budget(n_ctx=3000, gen=500, fixed=100), counter())
    assert p.counters["ctx_budget_band1_dedup"] >= 1
    assert "[1] と同一" in p.messages[2]["content"]


def test_中略の跡には取り直し方が書いてある():
    """要約ではなく受領書＋取り戻し方。ここが要約になると設計が壊れる。"""
    msgs, inputs = workload(n_files=3, n_cmds=3, big=400)
    p = cb.plan(msgs, cb.Budget(n_ctx=6000, gen=1000, fixed=200), counter(),
                inputs=inputs)
    joined = "\n".join(str(m.get("content", "")) for m in p.messages)
    assert "要約ではありません" in joined
    assert "Read(file_path=" in joined, "Read の取り直し方（offset/limit）が無い"


# --------------------------------------------------------------------------
# 11. 近似は実測を下回らない
# --------------------------------------------------------------------------

# 2026-08-11 に 推論機（qwen3-30b-a3b Q4_K_M）へ /tokenize を投げて取った実測。
# 過小評価は 400 で即死するので、近似は必ずこの値以上でなければならない。
_MEASURED = [
    ("hello world", 2),
    ("1234567890 " * 10, 110),
    ("The quick brown fox jumps over the lazy dog. " * 5, 51),
    ("あいうえおかきくけこかきくけこ" * 10, 130),
    ("驫麤龘鱻靐齾爩鸜" * 10, 160),
    ("🙂🚀🧩🔥" * 10, 50),
    ("가나다라마바사" * 10, 70),
    ("абвгдеж" * 10, 40),
    ("/usr/bin/python3 -c 'import sys; print(sys.version)'", 16),
    ("!@#$%^&*(){}[]<>|\\~`" * 10, 70),
]


@pytest.mark.parametrize("text,measured", _MEASURED)
def test_近似トークン数は実測を下回らない(text, measured):
    got = cb.approx_tokens(text)
    assert got >= measured, f"近似 {got} < 実測 {measured}（過小評価は 400 で即死する）"


def test_計数器はサーバが無ければ近似に落ちる():
    tc = cb.TokenCounter(None)
    assert tc.count("hello") > 0
    assert tc.exact() is False
    assert tc.approx_calls >= 1


def test_空文字は_0_トークン():
    assert cb.approx_tokens("") == 0
    assert counter().count("") == 0


# --------------------------------------------------------------------------
# 12. 生成余白の会計
# --------------------------------------------------------------------------

def test_予算は生成余白と固定費と余裕を引いた残り():
    b = cb.Budget(n_ctx=32768, gen=8192, fixed=5214, slack=1024)
    assert b.prompt == 32768 - 8192 - 1024 - 5214 == 18338


# --------------------------------------------------------------------------
# 13-14. 止まり方と冪等性
# --------------------------------------------------------------------------

def test_FLOOR_が予算を超えたら切り詰めずに_exhausted_で止まる():
    """直近2ターンのツール結果は守る決まりなので、Read 1本が窓を食い潰す形は
    梯子では救えない。黙って切るより名前をつけて止める。

    ★このとき段は 0 のまま＝**削れるものが何も無かった**ことがそのまま残る。
    削っていないのに「最下段まで適用した」と記録すると、次ターンにその嘘の段を
    持ち越して、削る必要のないものまで削り始める。
    """
    msgs = [{"role": "user", "content": "goal"},
            {"role": "assistant", "content": "読みます"},
            tool_msg(ev(1, "Read", lines(4000, "huge"))),
            tool_msg(ev(2, "Read", lines(4000, "huge2")))]
    p = cb.plan(msgs, cb.Budget(n_ctx=2000, gen=500, fixed=100), counter())
    assert p.exhausted is True
    assert p.level == 0, "何も削っていないのに段だけ深くなっている"
    assert snapshot(p.messages) == snapshot(msgs), "止まると言いながら切り詰めている"


def test_削っても足りなければ削ったうえで_exhausted_と言う():
    msgs = [{"role": "user", "content": "goal"}]
    for i in range(1, 7):
        msgs.append({"role": "assistant", "content": f"読みます{i}"})
        msgs.append(tool_msg(ev(i, "Read", lines(500, f"h{i}"))))
    p = cb.plan(msgs, cb.Budget(n_ctx=2000, gen=500, fixed=100), counter())
    assert p.exhausted is True
    assert p.level > 0 and p.counters["ctx_budget_lines_dropped"] > 0


def test_何も削れない段まで降りない():
    """帯3（失敗結果）も帯4（散文）も帯7（受領書）も出番が無い transcript で、段だけ
    7・8 に深くならないこと。実測（推論機）で level=8 と記録されながら帯3/4 の計数が 0
    だった回帰。

    ★2026-09-04（帯7 追加後）: この workload（Read/Bash のみ・エラーも assistant 散文も
    無し）では、帯7（本体を受領書へ）が level=8 で実際に回収する——だから
    「level>=8 なら帯4 が動いたはず」という旧来の言い分だけでは足りない。
    帯4 と帯7 のどちらかが実際に動いたことを見る（段が実態より深い数字を
    名乗っていない、という元の不変条件はそのまま）。
    """
    msgs, inputs = workload(n_files=6, n_cmds=6, big=300)
    p = cb.plan(msgs, cb.Budget(n_ctx=20000, gen=2048, fixed=500), counter(),
                inputs=inputs)
    if p.level >= 7:
        assert (p.counters["ctx_budget_band3_errbody"] > 0
                or p.counters["ctx_budget_band7_receipt"] > 0), \
            "帯3／帯7 まで降りたのに1件も畳んでいない"
    if p.level >= 8:
        assert (p.counters["ctx_budget_band4_prose"] > 0
                or p.counters["ctx_budget_band7_receipt"] > 0), \
            "帯4／帯7 まで降りたのに1件も畳んでいない"


def test_固定費だけで窓を食い潰すなら何もせず通す():
    """会計の誤り（近似モードでは fixed が実測の2〜3倍に出る）で run を殺さない。"""
    msgs, inputs = workload(n_files=2, n_cmds=2)
    p = cb.plan(msgs, cb.Budget(n_ctx=1000, gen=500, fixed=9000), counter(),
                inputs=inputs)
    assert p.unusable is True and p.fired is False
    assert snapshot(p.messages) == snapshot(msgs)


def test_凍結した区間は再圧縮しても変化しない():
    """冪等性。ここが壊れると段を変えていないのに毎ターン先頭が書き換わり、
    接頭辞キャッシュが毎ターン飛ぶ（自測: 追記 0.10s / 先頭書換 26.00s）。"""
    msgs, inputs = workload()
    tc = counter()
    b = cb.Budget(n_ctx=8000, gen=2048, fixed=500)
    p1 = cb.plan(msgs, b, tc, inputs=inputs)
    p2 = cb.plan(p1.messages, b, tc, inputs=inputs, min_level=p1.level)
    assert snapshot(p2.messages) == snapshot(p1.messages), "2回目で中身が動いた"
    assert p2.level == p1.level


def test_ターンをまたいでも保護窓より前の投影はバイト単位で同じ():
    """接頭辞キャッシュが効く範囲を、思い込みではなく試験で固定する。

    ★ここで保証できるのは「保護窓に入る手前までは同じ」だけ。実測（推論機・
    14ファイル読取）では **31本中26本がバイト一致するのに、トークンでは 15.1%
    しか一致しない** — 保護窓に残った全文のツール結果が大きいと、そこから先は
    毎ターン再 prefill になる。「追記は無料」は投影の前半についてだけ正しい。
    """
    msgs, inputs = workload(n_files=6, n_cmds=6, big=300)
    tc = counter()
    b = cb.Budget(n_ctx=40000, gen=2048, fixed=500)
    p1 = cb.plan(msgs, b, tc, inputs=inputs)
    assert p1.fired
    msgs2 = msgs + [{"role": "assistant", "content": "次を実行します。"},
                    tool_msg(ev(len(inputs) + 1, "Bash", "ok"))]
    p2 = cb.plan(msgs2, b, tc, inputs=inputs, min_level=p1.level)
    # 保護窓 = 直近2本の tool。それより前の tool メッセージは動いてはいけない。
    tool_pos = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    boundary = tool_pos[-2]      # turn N で保護されていた最初の1本
    assert snapshot(p1.messages[:boundary]) == snapshot(p2.messages[:boundary]), \
        "保護窓より前が書き換わっている＝毎ターン全再 prefill になる"


def test_段は単調にしか深くならない():
    """緩めると先頭が書き換わって全再 prefill。収まっていても段は戻さない。"""
    msgs, inputs = workload(n_files=2, n_cmds=2, big=5)
    p = cb.plan(msgs, cb.Budget(n_ctx=200000, gen=4096, fixed=100), counter(),
                inputs=inputs, min_level=4)
    assert p.level == 4 and p.fired is True


def test_frozen_upto_より前は一切触らない():
    msgs, inputs = workload()
    p = cb.plan(msgs, cb.Budget(n_ctx=4000, gen=1000, fixed=100), counter(),
                inputs=inputs, frozen_upto=5)
    assert snapshot(p.messages[:5]) == snapshot(msgs[:5])


# --------------------------------------------------------------------------
# 15. Read の打ち切りを黙ってやらない
# --------------------------------------------------------------------------

def test_Read_は_5万字で切ったことを出力に明記する(tmp_path, monkeypatch):
    """★印はスライスの *前* に足されていたので、大きいファイルでは印ごと
    切り落とされていた＝モデルは切られたことを知らされていなかった。"""
    monkeypatch.setenv("LAPS_POLICY", "off")
    from laps.tools import read as readtool
    f = tmp_path / "big.txt"
    f.write_text("\n".join("あ" * 200 for _ in range(2000)), encoding="utf-8")
    out = readtool.SPEC.handler({"file_path": str(f)})
    assert "打ち切りました" in out, "黙って切っている"
    assert "offset=" in out, "どこから読み直せばよいか書いていない"
    assert len(out) < 60000


def test_Read_は上限に届かなければ何も足さない(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "off")
    from laps.tools import read as readtool
    f = tmp_path / "small.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    out = readtool.SPEC.handler({"file_path": str(f)})
    assert "打ち切りました" not in out
    assert out.strip().endswith("c")


# --------------------------------------------------------------------------
# 16. backend: 400 をリトライしない
# --------------------------------------------------------------------------

def test_文脈溢れの_400_はリトライされない():
    """実測（推論機 に 60,009 tok）: 1回なら 0.3s のところ 11.8s = 1.5+3+6 の
    backoff ＝ 4 回投げていた。決定的な拒否を投げ直しても答えは変わらない。"""
    from laps.backend import Backend, BackendConfig, ContextOverflow

    calls = {"n": 0}

    class FakeResp:
        status_code = 400
        request = None
        text = ""

        def json(self):
            return {"error": {"message": "the request exceeds the available context size",
                              "type": "exceed_context_size_error",
                              "n_prompt_tokens": 60009, "n_ctx": 32768}}

        def raise_for_status(self):
            raise AssertionError("400 が raise_for_status まで到達した")

    def fake_post(path, json):
        calls["n"] += 1
        return FakeResp()

    b = Backend(BackendConfig(retry_base=0.01, max_retries=3))
    b._client.post = fake_post          # type: ignore[method-assign]
    with pytest.raises(ContextOverflow) as e:
        b.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1, f"400 を {calls['n']} 回投げている"
    assert e.value.n_prompt_tokens == 60009 and e.value.n_ctx == 32768
    b.close()


def test_文脈溢れは_RuntimeError_ではない():
    """★retry 節が RuntimeError を「クライアントが閉じられた」の回復用に
    捕まえているので、継承させるとリトライされてしまう。"""
    from laps.backend import ContextOverflow, BackendRejected
    assert not issubclass(ContextOverflow, RuntimeError)
    assert not issubclass(BackendRejected, RuntimeError)


def test_溢れ以外の_400_も投げ直さない():
    from laps.backend import Backend, BackendConfig, BackendRejected

    calls = {"n": 0}

    class FakeResp:
        status_code = 400
        request = None
        text = "bad request"

        def json(self):
            return {"error": {"message": "unknown field", "type": "invalid_request_error"}}

        def raise_for_status(self):
            raise AssertionError("到達しないはず")

    def fake_post(path, json):
        calls["n"] += 1
        return FakeResp()

    b = Backend(BackendConfig(retry_base=0.01, max_retries=3))
    b._client.post = fake_post          # type: ignore[method-assign]
    with pytest.raises(BackendRejected):
        b.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1
    b.close()


def test_直近の_prompt_tokens_が読める():
    """予測と実測を突き合わせる較正の材料。累積だけでは今回の当たり外れが分からない。"""
    from laps.backend import Backend, BackendConfig

    class FakeResp:
        status_code = 200
        request = None

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1234, "completion_tokens": 5,
                              "total_tokens": 1239}}

    b = Backend(BackendConfig())
    b._client.post = lambda path, json: FakeResp()   # type: ignore[method-assign]
    assert b.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert b.last_prompt_tokens == 1234
    b.close()


# --------------------------------------------------------------------------
# 配線（agent.py 側）
# --------------------------------------------------------------------------

class _FakeBackend:
    """モデルには触らない。送られてきた messages だけ記録する。"""

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []
        self.last_prompt_tokens = 42

    def chat(self, messages, **k):
        self.seen.append(messages)
        return "終わりました。"

    def chat_with_tools(self, messages, tools, **k):
        self.seen.append(messages)
        return "終わりました。", None

    def close(self):
        pass


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path / "sess"))
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_GROUNDING", "0")
    monkeypatch.setenv("LAPS_BENCH_CLEAN", "1")
    monkeypatch.delenv("LAPS_NATIVE_TOOLS", raising=False)
    from laps.oc.base import Agent
    a = Agent()
    a.backend = _FakeBackend()
    return a


def test_畳みはモデルが真似できる札を残さない():
    """★2026-09-13 夜・オーナーの記録 2 本で実証した退行の門。

    経緯: 畳んだ跡に目印を置いたところ、モデルは**それを自分の前の発言として読み**、
    次の答えに書き写した。文言を散文調から角括弧の札に変えても直らず、
    **新しい形をそのまま真似た**（版 0.1.221 の走行・token 事象に 14 回）。
    ＝「置くものを変える」では直らない。**置かない**のが唯一の直し方。
    """
    from laps import context_budget as cb

    折った, 落ちた = cb._fold_prose("スクリプトを作ります\n本文1\ndef f():\n    return 1")
    assert 落ちた == 3
    assert 折った == "スクリプトを作ります", "1 行目以外が残っている"
    # 真似できる形が 1 文字も残っていないこと（角括弧・省略記号・「省いて」等）。
    for かけら in ("[", "]", "…", "省い", "畳み", "ハーネス"):
        assert かけら not in 折った, f"モデルが真似できる跡が残っている: {かけら!r}"


def test_省いた行数はハーネスの声で伝える():
    """本文からは消すが、**事実は伝える**——ただし役割を変えて（assistant ではなく末尾の案内）。"""
    from laps import context_budget as cb

    cb.PROSE_FOLDED = 0
    cb._fold_prose("あ\nい\nう\nえ")
    assert cb.PROSE_FOLDED == 3, "省いた行数を数えていない＝ハーネスの声で伝えられない"
    cb.PROSE_FOLDED = 0


def test_発言は既定で畳まない(monkeypatch):
    """★2026-09-17: 窓の中身を実機 8 セッションで測った結果に基づく。

        道具の結果 95.4% ／ ★発言 3.6% ／ 依頼 0.9%（中央値・範囲 90.9〜97.7%）

    ⇒ 発言を畳んで浮くのは**最大 3.6%**。しかもその 3.6% は
      **意図（なぜ・次に何を）の唯一の写し**で、ハーネスは観測できない
      （事実は道具の結果・書いたファイル名・走らせたコマンドとして別に持っている）。

    外の測定も同じ向き（SWE-agent ACI は「畳むのは観測」／ACE は「書き直すと崩壊」／
    Useful Memories は「生の記録が 2 倍」）。
    """
    from laps.context_budget import _params

    monkeypatch.delenv("LAPS_FOLD_PROSE", raising=False)
    assert _params(8).band4 is False, "既定で発言を畳んでいる"
    assert _params(8).ht is not None, "観測の畳みまで止めてしまっている"


def test_発言の畳みは測るために戻せる(monkeypatch):
    """★「切ったほうが良い」は**まだ測っていない**。A/B できる形を残す。

    ⚠ 浮かなくなった 3.6% のぶん、最深段で収まらず仕切り直しが増える可能性がある。
    """
    from laps.context_budget import _params

    monkeypatch.setenv("LAPS_FOLD_PROSE", "1")
    assert _params(8).band4 is True


def test_観測の畳みは段の浅いうちに効く():
    """★順番は元から正しかった（私の「反転すべき」という見立ては誤りだった）。

    観測は段2 から畳み始め、段6 で 4 行 + 2 行まで削る。発言はその後。
    """
    from laps.context_budget import _params

    assert _params(2).ht == (60, 20)
    assert _params(6).ht == (4, 2)
    assert _params(1).ht is None
