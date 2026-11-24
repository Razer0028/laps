"""道具が返す量を**畳む**（黙って切らない）ことの門。

★測って直した（2026-08-29・run29/31/32/34 の実測 6,524 呼び出し・8,094,347 バイト）:

      Glob  896,469 B → 258,278 B  (−71.2%, 41 回発火)   ← 文字数の上限が無かった
      Grep  375,542 B → 199,927 B  (−46.8%, 27 回発火)   ← 行の途中で無言に切っていた
      Bash  878,221 B → 660,019 B  (−24.8%, 94 回発火)   ← 先頭から切って末尾を捨てていた
      合計 8,094,347 B → 7,062,339 B (−12.7%)

  再生器は `bench/replay_tool_caps.py`（同じ物差しで前後を測る）。

★この試験が守るのは「量」ではなく**約束**のほう:
  ① 切ったら本文に書く（何件のうち何件か）
  ② 末尾を残す
  ③ 削るつもりで増やさない
"""
from __future__ import annotations

import pytest

from laps.tools import bash as bash_mod
from laps.tools import fold
from laps.tools import glob as glob_mod
from laps.tools import grep as grep_mod
from laps.tools import limits


# --------------------------------------------------------------------------
# limits.body_cap — 割合ではなく**絶対量**
# --------------------------------------------------------------------------

def test_窓が分からなければ従来値(monkeypatch) -> None:
    monkeypatch.delenv("LAPS_N_CTX", raising=False)
    assert limits.body_cap(30000) == 30000


def test_32K窓での上限は足し算で決まる(monkeypatch) -> None:
    """★(32768 − 4096 − 1024 − 5200) × 0.50 / 8 / 0.34 ≒ 4,126 バイト。

    「窓の 25%」という割合ではなく、`context_budget.Budget` の会計を
    そのまま持ってきた値であること。式を変えたらこの数字も変わる——
    そのときは**なぜ変えたか**を limits.py の本文に書くこと。
    """
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    cap = limits.body_cap(30000)
    assert cap == 4126, cap
    # 旧 out_cap（16,384 文字）より必ず小さい。これが今回の変更の要点。
    assert cap < limits.out_cap(30000)


def test_狭い窓でも床を割らない(monkeypatch) -> None:
    """道具が何も返せない状態にはしない。"""
    monkeypatch.setenv("LAPS_N_CTX", "8192")
    assert limits.body_cap(30000) == limits.BODY_FLOOR


def test_広い窓でも従来の天井を超えない(monkeypatch) -> None:
    monkeypatch.setenv("LAPS_N_CTX", "1000000")
    assert limits.body_cap(30000) == 30000


def test_壊れた値は無視する(monkeypatch) -> None:
    for bad in ("", "0", "-5", "ごみ", "32k"):
        monkeypatch.setenv("LAPS_N_CTX", bad)
        assert limits.body_cap(30000) == 30000, bad


# --------------------------------------------------------------------------
# fold — 末尾を残す・公平に配る
# --------------------------------------------------------------------------

def test_中央を畳んで末尾を残す() -> None:
    """★実行結果の結論は末尾にある。先頭 cap 文字で切るとそこだけ消える。"""
    text = "\n".join(f"line{i}" for i in range(2000))
    out = fold.fold_head_tail(text, 1000)
    assert fold.nbytes(out) <= 1000 + 200
    assert out.startswith("line0")
    assert out.rstrip().endswith("line1999"), "末尾が落ちている"
    assert "中略" in out and "省きました" in out, "黙って切っている"


def test_短ければ何もしない() -> None:
    assert fold.fold_head_tail("abc", 1000) == "abc"


def test_1行が巨大でも畳める() -> None:
    out = fold.fold_head_tail("x" * 50000, 1000)
    assert fold.nbytes(out) < 2000
    assert "中略" in out


def test_行が全部予算超でも本文が残る() -> None:
    """★2026-09-05 r4c（査読 must_fix#1・live Umbra で実測された欠陥）。

    「4 行以上あり、どの 1 行も head/tail 予算を超える」形——日本語の段落で
    書かれたノートはこれになる（実測: 1 行 357〜408 バイト、head 予算 232 /
    tail 予算 348）。旧実装は head も tail も空になり、**中略マーカーだけ**を
    返していた（実文字 0 バイト）。live の Umbra が返す T4 ノート
    `agent://blackwall/8f466ccdddfd4e5f` がまさにこの形で、切り捨て版が
    698 バイト残すところを 0 バイトにしていた＝**畳んだほうが情報が少ない**。
    """
    text = "\n".join(["あ" * 100] * 6)      # 1 行 300 バイト × 6 行
    out = fold.fold_head_tail(text, 700)
    assert fold.nbytes(out) <= 700 + 200, fold.nbytes(out)
    body = out.replace("\n", "")
    for mark in ("… (中略:",):
        assert mark in out, "黙って切っている"
    # マーカーを除いた**実文字**が残っていること（これが欠陥の本体）。
    import re as _re
    real = _re.sub(r"… \(中略:[^)]*\) …", "", out).strip()
    assert fold.nbytes(real) > 300, f"中略マーカーだけになっている: {out!r}"
    assert real.startswith("あ") and real.endswith("あ"), real[:80]


def test_頭に短い1行しか入らなくてもバイトで畳む() -> None:
    """★同じ欠陥の隣（r4c で一緒に塞いだ）。

    先頭が `` ``` `` のような短い 1 行だと head に 1 本だけ入り、残りは
    どれも予算超——「マーカーだけ」ではないが、予算 700 に対して実文字は
    3 バイトしか残らない。予算の半分も使えていないならバイトで畳む。
    """
    text = "```\n" + "\n".join(["い" * 100] * 5)
    out = fold.fold_head_tail(text, 700)
    import re as _re
    real = _re.sub(r"… \(中略:[^)]*\) …", "", out).strip()
    # 行の枝だと 3 バイト（``` だけ）＋末尾1行の 301 バイト＝304。
    # バイトの枝なら予算いっぱいの 580 バイト。**予算の 8 割**を境に見る。
    assert fold.nbytes(real) > 450, f"予算の大半を捨てている: {out!r}"


def test_短い行が並ぶ普通の出力は行のまま畳む() -> None:
    """★上の2つのフォールバックが、普通の形まで巻き込んでいないこと（陰性対照）。"""
    text = "\n".join(f"line{i}" for i in range(2000))
    out = fold.fold_head_tail(text, 1000)
    assert out.startswith("line0") and out.rstrip().endswith("line1999")
    assert "行 /" in out and "文字を省きました" in out, "行の畳み方でなくなっている"


def test_公平配分はどの束からも見せる() -> None:
    """★先頭から詰めると、行数の多い1本が枠を食い尽くし他が1件も見えない。"""
    from collections import OrderedDict
    groups = OrderedDict()
    groups["huge.log"] = [f"huge.log:{i}:x" for i in range(500)]
    groups["a.py"] = ["a.py:1:x"]
    groups["b.py"] = ["b.py:1:x"]
    shown, omitted = fold.round_robin(groups, 300)
    assert any(s.startswith("a.py") for s in shown), shown
    assert any(s.startswith("b.py") for s in shown), shown
    assert omitted.get("huge.log", 0) > 0


def test_省いた分は必ず本文に出る() -> None:
    assert fold.omitted_note({"x.py": 3}) != ""
    assert fold.omitted_note({}) == ""
    c = fold.coverage(100, 10)
    assert "100" in c and "10" in c and "90" in c
    assert "すべて表示" in fold.coverage(5, 5)


# --------------------------------------------------------------------------
# Grep — 無言の切断をやめ、件数を書く
# --------------------------------------------------------------------------

def test_grepは何件のうち何件かを必ず書く(monkeypatch) -> None:
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    lines = [f"f{i % 20}.py:{i}:これは十分に長い一致行です" * 3 for i in range(4000)]
    out = grep_mod._render(lines, 200)
    assert "★一致 4,000件" in out, out[-400:]
    assert "を表示しています" in out
    assert "表示を省いた内訳" in out


def test_grepは行の途中で切らない(monkeypatch) -> None:
    """★旧実装 `"\\n".join(lines)[:cap]` は行の途中で切っていた。"""
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    lines = [f"f{i % 7}.py:{i}:" + "y" * 300 for i in range(500)]
    out = grep_mod._render(lines, 500)
    for ln in out.splitlines():
        if ln.startswith("f") and ":" in ln:
            assert ln.endswith("y" * 10), f"行の途中で切れている: {ln[-40:]}"


def test_grepはmax_resultsの取りこぼしも申告する(monkeypatch) -> None:
    """★旧実装は `[:max_count]` で 200 件超を黙って捨てていた。"""
    monkeypatch.setenv("LAPS_N_CTX", "1000000")
    lines = [f"f{i}.py:1:hit" for i in range(300)]
    out = grep_mod._render(lines, 5)
    assert "★一致 300件のうち 5件" in out, out[-300:]


def test_grepは予算内に収まる(monkeypatch) -> None:
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    lines = [f"f{i % 30}.py:{i}:" + "z" * 200 for i in range(9000)]
    out = grep_mod._render(lines, 100000)
    assert fold.nbytes(out) < limits.out_cap(30000)


# --------------------------------------------------------------------------
# Glob — 上限が無かった／500 件で無言に切っていた
# --------------------------------------------------------------------------

def test_globは大量の一覧をディレクトリごとに畳む(monkeypatch) -> None:
    """★実測の最大は `pattern='**'` の 26,952 バイト（500 行）で、
    その大半が同じフォルダの画像ファイル名だった。"""
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    paths = [f"/x/images/人物{i % 3}/写真{i:05d}.png" for i in range(3000)]
    out = glob_mod._render(paths)
    assert fold.nbytes(out) <= limits.out_cap(30000)
    assert fold.nbytes(out) < 6000, fold.nbytes(out)
    assert "★一致 3,000件のうち" in out
    assert "省いたディレクトリ" in out


def test_globは小さければそのまま(monkeypatch) -> None:
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    paths = ["a/b.py", "a/c.py"]
    assert glob_mod._render(paths) == "a/b.py\na/c.py"


def test_globは列挙を打ち切ったら申告する(monkeypatch) -> None:
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    out = glob_mod._render([f"d{i % 4}/f{i}" for i in range(20000)], capped=True)
    assert "打ち切りました" in out


def test_globの列挙上限は無言ではない() -> None:
    """★`sorted(...)[:500]` に戻したら、501 件目以降が消えたことが誰にも見えない。"""
    import inspect
    src = "\n".join(ln.split("#", 1)[0]
                    for ln in inspect.getsource(glob_mod._run).splitlines())
    assert "[:500]" not in src, "無言の件数上限が戻っている"
    assert "_capped" in src


# --------------------------------------------------------------------------
# Bash — 末尾を残す／同じコマンドの2回目以降を短くする
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_repeat_memory():
    bash_mod._reset_repeat_memory()
    yield
    bash_mod._reset_repeat_memory()


def test_bashは末尾を残して畳む(monkeypatch) -> None:
    """★旧実装 `out[:cap]` は**末尾を捨てる**。失敗の理由は末尾にある。"""
    monkeypatch.setenv("LAPS_N_CTX", "32768")
    text = "\n".join(f"row {i}" for i in range(20000)) + "\nFAILED: 3 tests"
    out = fold.fold_head_tail(text, limits.body_cap(30000))
    assert out.rstrip().endswith("FAILED: 3 tests"), "末尾（結論）が落ちている"


def test_同じコマンドの2回目は短くなる() -> None:
    body = "\n".join(f"line {i}" for i in range(200))
    first = bash_mod._fold_repeat("ls -la", "/tmp", body, body)
    assert first == body, "初回は全文でなければならない"
    second = bash_mod._fold_repeat("ls -la", "/tmp", body, body)
    assert second.startswith("(前回と同じ出力")
    assert fold.nbytes(second) < fold.nbytes(body) / 2
    assert "line 199" in second, "末尾の1行は残す"


def test_3回目からは次の一手を言う() -> None:
    body = "\n".join(f"line {i}" for i in range(200))
    for _ in range(2):
        bash_mod._fold_repeat("ls", None, body, body)
    third = bash_mod._fold_repeat("ls", None, body, body)
    assert "別の手を打ってください" in third


def test_出力が変われば畳まない() -> None:
    a = "\n".join(f"line {i}" for i in range(200))
    b = a + "\nnew line"
    bash_mod._fold_repeat("ls", None, a, a)
    assert bash_mod._fold_repeat("ls", None, b, b) == b


def test_畳んだ結果が長くなるなら畳まない() -> None:
    """★削るつもりで増やさない。"""
    body = "x" * 210
    bash_mod._fold_repeat("ls", None, body, body)
    again = bash_mod._fold_repeat("ls", None, body, body)
    assert fold.nbytes(again) <= fold.nbytes(body)


def test_短い出力は触らない() -> None:
    body = "ok"
    bash_mod._fold_repeat("ls", None, body, body)
    assert bash_mod._fold_repeat("ls", None, body, body) == body


def test_作業場が違えば別のコマンド扱い() -> None:
    body = "\n".join(f"line {i}" for i in range(200))
    bash_mod._fold_repeat("ls", "/a", body, body)
    assert bash_mod._fold_repeat("ls", "/b", body, body) == body


# --------------------------------------------------------------------------
# ★書いたら繋ぐ（この現場で 5 回踏んだ形の門）
# --------------------------------------------------------------------------

def test_畳みが実際に呼ばれている() -> None:
    """実装しただけで呼ばれていない、を止める。"""
    import inspect
    for mod, must in ((grep_mod, ("fold.round_robin", "fold.coverage", "limits.body_cap")),
                      (glob_mod, ("fold.round_robin", "fold.coverage", "limits.body_cap")),
                      (bash_mod, ("fold.fold_head_tail", "_fold_repeat", "limits.body_cap"))):
        src = inspect.getsource(mod)
        for name in must:
            assert name in src, f"{mod.__name__} が {name} を呼んでいない"
    # 呼び出し側（_run）から辿れること。定義だけして繋いでいない、を弾く。
    assert "_render(" in inspect.getsource(grep_mod._run)
    assert "_render(" in inspect.getsource(glob_mod._run)


# --------------------------------------------------------------------------
# Umbra — 1件 700 字の上限はあったが、**総量の上限が無かった**
# --------------------------------------------------------------------------

def _old_server_payload(n_pending: int) -> dict:
    """confidence を返さない**旧サーバ**の応答（`_server_confident` → None）。

    本番の Umbra は confidence を返すので `verdict is True` になり、`pending`
    （埋め込み待ちで確度を判定できない欄）は構造的に必ず空になる。
    ＝この経路は**本番では踏まない**。だからこそ `limits.body_cap` の約束が
    「confidence つきの payload に限る」に痩せていたことに 7 巡目まで
    気づけなかった（査読 must_fix#3・8 巡目に実測して塞いだ）。
    """
    body = "\n".join(["あ" * 120] * 6)
    res = [{"doc_id": i, "tier": "T2", "score": 1.6, "sem_sim": 0.95,
            "title": f"確度のある文献{i}", "uri": f"lab://ok{i}.md", "license": "L",
            "source": "lab", "tech": "", "excerpt": body + f"\n対処{i}: 結論"}
           for i in range(9)]
    for i in range(n_pending):
        res.append({"doc_id": 900 + i, "tier": "T3", "score": 1.30, "sem_sim": None,
                    "title": f"埋め込み待ちの新しいノート{i}（確度を判定できない欄）",
                    "uri": f"agent://blackwall/pending-note-{i}-{'x' * 24}",
                    "license": "L", "source": "agent", "tech": "", "excerpt": body})
    return {"q": "q", "n": len(res), "results": res}


def _real_bytes(out: str) -> int:
    """中略マーカーを除いた**実文字**の UTF-8 バイト数。"""
    return fold.nbytes(_MARK_RE.sub("", out).strip())


def test_末尾を残せない小さな窓では畳まずに先頭を残す() -> None:
    """★査読 must_fix#1（**本番の窓で起きていた退行**）。

    `_fold_bytes` は `tail_b = cap - int(cap*0.4) - 120` を末尾に配る。
    これは **cap <= 200 で必ず 0**——末尾を 1 バイトも残さないまま
    「末尾は残しています」と書いていた。しかも本番の窓がこの帯に入る:
    `limits.body_cap` は `LAPS_N_CTX <= 16,848` で床の 1,200 に落ち、
    `umbra.py` の `_per` は 2 件以上で必ず 150 になる。

    live の実測（Umbra の実応答 12 文書・`_per` は窓と件数で決まる値）:
        _per=150  畳み 実文字  60B ／ 切り捨て 150B   末尾に到達 0/12 文書
        _per=177  畳み 実文字  70B ／ 切り捨て 177B   末尾に到達 0/12
        _per=224  畳み 実文字 104B ／ 切り捨て 224B   末尾に到達 0/12
    ＝**畳んだほうが情報が少ない**（1 巡目に直したはずの症状が、別の帯で
    そのまま残っていた）。末尾に 1 文（`fold.MIN_TAIL_BYTES`）も置けない
    cap では畳まない。
    """
    text = "\n".join(["あ" * 100] * 6)          # 1 行 300 バイト × 6 行
    for cap in (150, 177, 224, 300, 350, 398):
        out, tail_kept = fold.fold_head_tail_ex(text, cap)
        assert not tail_kept, f"cap={cap} で末尾を残したと主張している"
        assert out == fold.clip_bytes(text, cap), f"cap={cap}: 先頭を残していない"
        assert "中略" not in out, f"cap={cap}: 末尾が無いのにマーカーを付けている"
        assert fold.nbytes(out) <= cap
    # 境目は `cap - int(cap*0.4) - MARKER_ROOM >= MIN_TAIL_BYTES` を満たす最小の
    # cap＝399（int の切り捨てが効くので 400 ちょうどではない）。境目そのものを
    # 固定しておく——定数を触ったときにここが動くのが分かるように。
    assert fold.fold_head_tail_ex(text, 398)[1] is False
    for cap in (399, 400, 700):
        out, tail_kept = fold.fold_head_tail_ex(text, cap)
        assert tail_kept and "中略" in out, cap
        assert out.endswith("あ"), (cap, out[-40:])


def test_畳むと言ったときは必ず末尾の実文字が入る() -> None:
    """「末尾は残しています」と書くなら、本当に末尾が入っていること。"""
    tail_mark = "ここが結論"
    text = "\n".join(["あ" * 100] * 6) + tail_mark
    for cap in (400, 500, 700, 1000, 4000):
        out, tail_kept = fold.fold_head_tail_ex(text, cap)
        if not tail_kept:
            continue
        if "末尾は残しています" in out:
            assert out.rstrip().endswith(tail_mark), f"cap={cap}: {out[-60:]!r}"


import re as _re

_MARK_RE = _re.compile(r"…\s*\(中略:.*?\)\s*…")


def test_実文字が残らない行の詰め方はバイトの枝へ落ちる() -> None:
    """★査読 must_fix#2。判定を「予算を使ったか」→「**実文字を残せたか**」へ。

    空行は 1 行 1 バイトなので、300 本並べば予算の 99% を使い切る——旧判定は
    これを「行で畳めた」と見なし、**中略マーカーと空行だけ**を返していた
    （実測: 空行 300 本 + 本文 5 行 + 空行 400 本、cap=700／空白 1 字の行でも同じ）。
    """
    body = ["FTS5 の content= は同一接続内のテーブルを指す設計です。" * 3] * 5
    for filler in ("", " ", "\t"):
        text = "\n".join([filler] * 300 + body + [filler] * 400)
        out = fold.fold_head_tail(text, 700)
        assert _real_bytes(out) > 0, f"filler={filler!r}: 実文字が 0（マーカーと空行だけ）"


def test_極小の予算でもマーカーだけを返さない() -> None:
    """`_fold_bytes` が cap 0〜9 でマーカーだけを返さないこと（査読 must_fix#2）。"""
    text = "\n".join(["あ" * 100] * 6)
    for cap in range(0, 10):
        out = fold.fold_head_tail(text, cap)
        assert "中略" not in out, f"cap={cap}: マーカーが本文より大きい: {out!r}"
        assert fold.nbytes(out) <= cap, f"cap={cap}: {fold.nbytes(out)}B"


def _shapes() -> dict[str, str]:
    """property test が回す文書の形。

    ★2026-09-05 r4c 3 巡目（査読 must_fix#3）: **`短い行が並ぶ` が入っていな
      かった**——2 巡目の property test は「長い行」「空行」「空白1字」しか
      持っておらず、そのどれも行の枝で `real_kept == 0` か予算余りに当たって
      バイトの枝へ落ちる。つまり**行の枝で畳んで返す経路を 1 度も通っていな
      かった**ので、行の枝に `MIN_TAIL_BYTES` の門が無いことを検出できなかった
      （cap 0..398 のうち 253 個で退行していたのに 5 形すべて緑）。
      `line0..line1999` は man ページ・`ls -1`・スタックトレース・`grep -n` の
      出力そのもので、**この道具がいちばん多く受け取る形**。
    """
    return {
        "空行のみ": "\n" * 500,
        "空白1字のみ": "\n".join([" "] * 500),
        "CJK": "\n".join(["あ" * 100] * 6),
        "絵文字": "\n".join(["😀" * 50] * 6),
        "空行に埋もれた本文": "\n".join([""] * 200 + ["対処: ここが結論" * 5] + [""] * 200),
        "短い行が並ぶ_manページ形": "\n".join("line%d" % i for i in range(2000)),
        "短い行の末尾が空行": ("\n".join("line%d" % i for i in range(200))
                              + "\n" * 800),
        "長い行の末尾が空行": ("\n".join(["本文行%d の実際の内容がここに書いてある" % i
                                          for i in range(40)]) + "\n" * 800),
    }


def test_総当り_どの形でも切り捨てを下回らず上限も超えない() -> None:
    """★property test（査読 must_fix#2 の受け入れ条件・#3 で形を追加）。

    8 つの形 × cap 0..398（**畳まないと決めた帯の全域**）を総当りして、
    次の 3 つを固定する:
      ① 出力は cap を超えない
      ② 実文字が `clip_bytes` を下回らない（＝畳んで損をしない帯）
      ③ 元に実文字があるのに出力の実文字が 0 にならない

    ★上限を 300 → 398 に広げた（3 巡目）。境目は `cap >= 399` なので、
      300 で止めると **301..398 の 98 個を測っていない**——実際そこに
      `短い行が並ぶ` 形の退行が 98 個ぶん埋まっていた。
    """
    for name, text in _shapes().items():
        for cap in range(0, 399):
            out = fold.fold_head_tail(text, cap)
            assert fold.nbytes(out) <= cap, f"{name} cap={cap}: 上限超過 {fold.nbytes(out)}B"
            assert "中略" not in out, (
                f"{name} cap={cap}: 末尾に 1 文も置けない帯なのに畳んでいる")
            clip = fold.clip_bytes(text, cap)
            assert _real_bytes(out) >= fold.nbytes(clip.strip()), (
                f"{name} cap={cap}: 畳み {_real_bytes(out)}B < 切り捨て "
                f"{fold.nbytes(clip.strip())}B")
            if text.strip() and fold.nbytes(clip.strip()) > 0:
                assert _real_bytes(out) > 0, f"{name} cap={cap}: 実文字が消えた"


def test_総当り_畳む帯では末尾の主張が嘘にならない() -> None:
    """★property test の**上の帯**（cap >= 399・査読 must_fix#1／#3）。

    この帯では畳んでよい（マーカーの 120 バイトを払って末尾へ届く取引が成立
    する）ので、`clip_bytes` 以上という条件は**成り立たない**——払った 120 バイト
    ぶんだけ実文字は減る。その代わりに固定するのは「払った対価が本当に手に
    入っているか」:
      ① 出力は cap を超えない
      ② 実文字が 0 にならない
      ③ **「末尾は残しています」と書くなら、マーカーより後ろに実文字がある**
         （2 巡目はここが破れていた: `末尾が空行` の形で cap=400/500/700 の
         すべてで末尾の実文字が 0 だった。head と tail を**合算**して判定して
         いたため、head の 220B が tail の 0B を隠していた）
      ④ 返り値の 2 つめ（`tail_kept`）と本文の文言が食い違わない
         （呼び出し側はこの値で「先頭だけです」と書き分ける）
    """
    for name, text in _shapes().items():
        for cap in range(399, 1400, 7):
            out, tail_kept = fold.fold_head_tail_ex(text, cap)
            assert fold.nbytes(out) <= cap, f"{name} cap={cap}: 上限超過"
            clip = fold.clip_bytes(text, cap)
            if text.strip() and fold.nbytes(clip.strip()) > 0:
                assert _real_bytes(out) > 0, f"{name} cap={cap}: 実文字が消えた"
            said = "末尾は残しています" in out
            assert said == ("中略" in out), f"{name} cap={cap}: マーカーと文言の不一致"
            assert said == tail_kept or fold.nbytes(text) <= cap, (
                f"{name} cap={cap}: 返り値 {tail_kept} と本文 {said} が食い違う")
            if said:
                after = _MARK_RE.split(out)[-1]
                assert fold.nbytes(after.strip()) > 0, (
                    f"{name} cap={cap}: 末尾を残したと書いているのに末尾の実文字が 0 "
                    f"({after[:30]!r})")


_DISCLOSURE = ("中略", "切りました", "先頭だけ")


def _said_cut(out: str) -> bool:
    """このファイル冒頭の方針①「切ったら必ず本文に書く」を満たしているか。"""
    return any(w in out for w in _DISCLOSURE)


def _tail_blank_shapes(cap: int) -> dict[str, str]:
    """**末尾が空白/空行だけ**の形（無言の切り捨てが起きる入力）。

    必要な末尾空白は `fold._tail_room(cap, 0.4)` ぶん＝16,384 窓の bash なら
    600B、32,768 窓なら 2,356B。`print` やログの末尾の空行で普通に起きる量。
    """
    room = fold._tail_room(cap, 0.4)
    pad = max(room + 1, 1)
    n_long = cap // 60 + 50      # 1 行 ≒ 60 バイト。必ず cap を超える本数にする
    n_short = cap // 7 + 50      # 1 行 ≒ 7 バイト
    return {
        "1 行巨大＋末尾が空白": "A" * (cap * 3) + " " * pad,
        "長い行＋末尾が空行": ("\n".join("本文行%d の実際の内容がここに書いてある" % i
                                        for i in range(n_long)) + "\n" * pad),
        "短い行＋末尾が空行": ("\n".join("line%d" % i for i in range(n_short))
                              + "\n" * pad),
    }


def test_畳めずに先頭だけになったときも本文に書く() -> None:
    """★査読 must_fix#1（**新規回帰**）の受け入れ条件。

    `_fold_bytes` の末尾空白枝は `clip_bytes` へ落として `False` を返す。
    2 値版を見る呼び出し側（`tools/umbra.py`）はそれで書き分けられるが、
    1 値版 `fold_head_tail` しか使っていない `tools/bash.py` と
    `phases._trim_umbra_doc` は**何も書かずに切っていた**。
    `fold_head_tail_told` は畳めなかったときも印を置く。
    """
    for cap in (1200, 4126, 10150, 30000):
        for name, text in _tail_blank_shapes(cap).items():
            assert fold.nbytes(text) > cap, f"{name} cap={cap}: 入力が cap 以下"
            silent = fold.fold_head_tail(text, cap)          # 直さない 1 値版
            told_in = fold.fold_head_tail_told(text, cap)
            told_out = fold.fold_head_tail_told(text, cap, inside_cap=False)
            if not _said_cut(silent):
                # ここが「無言の切り捨て」だった帯。印が必ず入る。
                assert _said_cut(told_in), f"{name} cap={cap}: inside で無言のまま"
                assert _said_cut(told_out), f"{name} cap={cap}: outside で無言のまま"
            # ① inside_cap は上限を守る（呼び出し側に外枠が無い場合に使う）
            assert fold.nbytes(told_in) <= cap, (
                f"{name} cap={cap}: inside_cap なのに上限超過 {fold.nbytes(told_in)}B")
            # ② outside は本文を 1 バイトも削らない（外枠を持つ側で使う）
            assert told_out.startswith(silent[:200]), f"{name} cap={cap}: 本文が変わった"
            # ③ 印に「中略」を混ぜない（畳めた開示と区別がつかなくなる）
            assert "中略" not in fold.HEAD_ONLY_MARK


def test_畳めたときは印を二重に付けない() -> None:
    """畳めた（中略マーカーが出た）ときに `HEAD_ONLY_MARK` を足さないこと。"""
    text = "\n".join(["あ" * 100] * 6) + "ここが結論"
    for cap in (399, 700, 1500):
        out = fold.fold_head_tail_told(text, cap)
        assert "中略" in out, cap
        assert fold.HEAD_ONLY_MARK not in out, f"cap={cap}: 開示が二重"
        assert fold.nbytes(out) <= cap


def test_切っていないなら印を付けない() -> None:
    """cap に収まっている出力に印を付けない（嘘の開示を作らない）。"""
    for text in ("短い出力", "あ" * 10, "line\n" * 3):
        for cap in (1200, 30000):
            out = fold.fold_head_tail_told(text, cap)
            assert out == text, (text[:20], cap, out[:40])
            assert not _said_cut(out)


def test_印より小さい窓では印を出さない() -> None:
    """印が本文より大きい帯では**本文を採る**（`MIN_MARKED_BODY`）。

    ★`MIN_TAIL_BYTES` と同じ考え。ここで印を出すと「畳んだら情報が減った」
      という 1 巡目の症状が、別の名前で戻ってくる。
    """
    text = "\n".join(["あ" * 100] * 6)
    mark = fold.nbytes("\n" + fold.HEAD_ONLY_MARK)
    for cap in range(0, mark + fold.MIN_MARKED_BODY):
        out = fold.fold_head_tail_told(text, cap)
        assert fold.nbytes(out) <= cap, f"cap={cap}: 上限超過"
        assert fold.HEAD_ONLY_MARK not in out, f"cap={cap}: 印のほうが本文より大きい"


def test_末尾の実文字が数バイトしかないなら末尾を主張しない() -> None:
    """★査読 must_fix#4。

    バイトの枝の末尾判定は `not tail.strip()`＝**実質 0 バイト**だったので、
    cap=399〜450（`tail_b` は 120〜150）の帯では末尾に助詞 1 文字（1〜3 バイト）
    でも残っていれば「末尾は残しています」を通していた。マーカーに 120 バイト
    払って手に入るのが 1 文字では、`MIN_TAIL_BYTES` が禁じた取引と同じ。
    """
    head = "A" * 4000
    for real_tail in ("。", "…", "です"):          # 3〜6 バイト
        for cap in range(399, 451):
            room = fold._tail_room(cap, 0.4)
            text = head + " " * (room * 2) + real_tail + " " * (room - 8)
            out, tail_kept = fold.fold_head_tail_ex(text, cap)
            assert not tail_kept, (
                f"cap={cap} 末尾の実文字 {fold.nbytes(real_tail)}B で末尾を主張している")
            assert "中略" not in out
            assert out == fold.clip_bytes(text, cap)
    # 陰性対照: 実文字が下限を超えていれば従来どおり畳む
    for cap in range(399, 451):
        room = fold._tail_room(cap, 0.4)
        text = head + " " * (room * 2) + "結" * 20 + " " * max(0, room - 70)
        out, tail_kept = fold.fold_head_tail_ex(text, cap)
        assert tail_kept and "中略" in out, f"cap={cap}: 十分な末尾まで畳まなくなった"


def test_min_foldable_capは定数から導かれる() -> None:
    """境目を数値で書き写さないための関数（`umbra` の救済の門が使う）。"""
    b = fold.min_foldable_cap()
    assert b == 399, f"境目が動いた: {b}（定数を変えたなら試験の記録も直すこと）"
    assert fold._tail_room(b, 0.4) >= fold.MIN_TAIL_BYTES
    assert fold._tail_room(b - 1, 0.4) < fold.MIN_TAIL_BYTES
    text = "\n".join(["あ" * 100] * 6)
    assert fold.fold_head_tail_ex(text, b - 1)[1] is False
    assert fold.fold_head_tail_ex(text, b)[1] is True


# --------------------------------------------------------------------------
# 「黙って切る書き方」の走査 ― **許可形だけを通す**（禁止形は数えない）
# --------------------------------------------------------------------------
# ★8 巡目（査読 must_fix#2・複製木で 9 形が素通りした）: 5〜7 巡目は
#   「黙って切る書き方」を**列挙**していた（正規表現 3 本＋AST 5 規則）。
#   列挙は必ず漏れる。査読は HEAD の複製木に 1 形ずつ注入して、申告済みの
#   7 形が 7/7 赤になる一方で、次の **9 形が 2 passed（緑で素通り）**すること
#   を実測した:
#
#       body = fold.fold_head_tail_ex(text, cap)[-2]
#       body, *_rest = fold.fold_head_tail_ex(text, cap)
#       fh = fold.__dict__["fold_head_tail"]
#       body = next(iter(fold.fold_head_tail_ex(text, cap)))
#       body = fold.fold_head_tail_ex(text, cap)[0:1][0]
#       body = list(fold.fold_head_tail_ex(text, cap))[0]
#       operator.itemgetter(0)(fold.fold_head_tail_ex(text, cap))
#       name = "fold_head_" + "tail"; getattr(fold, name)
#       for body, _ in [fold.fold_head_tail_ex(text, cap)]:
#
#   ＝「あと 3 形足す」では終わらない種類の穴。**列挙をやめ、許可形の
#   ホワイトリストにする**（この現場が gcp_apply.sh の execve choke で
#   採ったのと同じ形——通ってよい形を 1 つ決めて、それ以外を全部落とす）。
#
#   許可するのはただ 1 形:
#       <名前A>, <名前B> = …fold_head_tail_ex(…)      かつ  B を同じ関数の中で読む
#   これ以外の場所に現れた `fold_head_tail_ex(` の呼び出しは全部赤。添字も
#   スター展開も `for` 束縛も `next()` も、木の形が違うので通らない。
#
#   加えて `fold_head_tail`（1 値版）は、識別子でも**静的に決まる文字列**でも
#   本番に出てはいけない（`__dict__["fold_head_tail"]`・`"fold_head_" + "tail"`
#   の連結前の定数を捕まえる）。docstring での**言及**は誤爆しないよう外す。
#
#   検知器は**この 1 本だけ**（`_silent_cut_flags`）。走査本体と陽性対照が
#   同じ実体を呼ぶ（7 巡目に正規表現を両側へ書き写していた形の再発防止）。
#   正規表現の網（`_SILENT_CUT_PATTERNS`）と、そのためのコメント落とし
#   （`_code_lines`）は 8 巡目に**削除**した。ホワイトリストが同じ形を
#   すべて赤にするので二重に持つ理由が無く、持てば「2 箇所で別々に決める」
#   ——この木が何度も踏んだ形に戻る。
#
#   ★★9 巡目（査読 must_fix M2'）: **③はまだ「列挙」だった。**
#   8 巡目は 1 値版の名前を「静的に決まる文字列」で捕まえたが、静的に
#   決まらない書き方は素通りする。査読が HEAD の複製木に注入して実測:
#
#       fh = getattr(fold, "".join(["fold_head_", "tail"]))   → 2 passed（素通り）
#       fh = vars(fold)["fold_head_%s" % "tail"]              → 2 passed（素通り）
#
#   `"".join([...])` も `%` も `_static_str` が組み立てられないので、鍵の側を
#   いくら賢くしても追いかけっこになる（`"".join` の次は `str.format`、
#   その次は `chr(102)+...`）。**鍵ではなく入口を落とす**——①で採ったのと
#   同じ形に寄せる:
#
#     ⑤ `blackwall/` では `fold` モジュールへの**動的な属性参照**を、
#        鍵の形に関係なく全部赤にする（`getattr/setattr/delattr(fold, …)`・
#        `vars(fold)`・`fold.__dict__`・`fold.__getattribute__` と、
#        モジュール辞書からの名前引き `globals()[…]` / `vars()[…]`）。
#        本番が `fold` を触ってよいのは**静的な属性**（`fold.nbytes` のような
#        `.` 付きの直書き）だけ、という線にする。
#     `fold` を指す名前は import の別名も追う（`from .tools import fold as
#     _fold` が本番に 3 か所ある: serve.py:528・phases.py:1112,1176）。
#
#   残る限界（自己申告・⑤でも見えない）:
#     - **`b` を「読む」だけで「使わない」形**（①の許可形の穴・査読が実測）:
#           body, told = fold.fold_head_tail_ex(text, cap); told
#           body, told = fold.fold_head_tail_ex(text, cap); _ = told
#       どちらも 2 passed（緑）。①が課しているのは「同じ関数スコープで
#       `Load` されること」だけで、**読んだ値で何かをするか**は静的な木から
#       決められない（`if told:` と `_ = told` は木の上では同格）。
#       ＝この走査は「畳めたかを受け取っていない」を止められるが、
#       「受け取って捨てている」は止められない。
#     - `fold` モジュールを**引数や属性で渡してから**動的に引く形
#       （`def f(m): getattr(m, k)` ／ `self.fold`）。名前が `fold` に
#       結び付かないので⑤の対象にならない。
#     - 完全に動的な名前解決（`importlib.import_module(...)` ＋ 実行時に
#       組み立てた文字列）。静的な木に名前も定数も現れないので原理的に見えない。
#     - 別モジュールに 1 値版を包む薄い関数を作り、それを呼ぶ形
#       （包んだ側のファイルが blackwall/ にあれば①で赤になるが、
#       blackwall/ の外に置かれると見えない）。
#     - `fold_head_tail_told` は許可形を課していない（畳めなくても言う版なので
#       そもそも黙って切らない）。


def _silent_cut_flags(src: str) -> list[tuple[int, str]]:
    """許可形以外を全部返す（`(行, 理由)` の一覧）。

    ①  `fold_head_tail_ex(...)` の呼び出しが
        「1 つの代入・2 名のタプル・両方 `Name`・2 つめを同じ関数スコープで
        読んでいる」形の**右辺そのもの**でなければ赤。
    ②  `fold_head_tail` という**識別子**（`Name`/属性/import の別名/引数名/
        関数名/`global`）が出てきたら赤。
    ③  `fold_head_tail` を含む**静的に決まる文字列**（定数・定数どうしの `+`・
        f-string の定数部分）が出てきたら赤。docstring 位置の裸の文字列は除く。
    ④  `ast.parse` に失敗したら赤（fail-closed）。走査対象は本番の `blackwall/`
        なので、構文が壊れた `.py` に**素通りの権利は無い**。7 巡目までは
        `except SyntaxError: return []` で黙って通していた。
    ⑤  `fold` モジュールへの**動的な属性参照**は鍵の形に関係なく赤
        （9 巡目・査読 must_fix M2'）。`getattr/setattr/delattr(fold, …)`・
        `vars(fold)`・`fold.__dict__`・`fold.__getattribute__` と、モジュール
        辞書からの名前引き `globals()[…]` / `vars()[…]`。③は鍵が静的に
        決まるときしか効かず、`"".join([...])` や `"%s" %` で素通りした
        （実測・上のコメント）。**鍵ではなく入口**を落とす。
        `fold` を指す名前は import の別名も追う（`from .tools import fold as
        _fold` が本番に 3 か所ある）。

    ★②で「2 つめを読んでいるか」を名前の形（`_` 始まり等）で判定しない:
      この木は局所変数に `_` を付ける流儀で、本番 `umbra.py` の
      `_folded, _tail_kept = fold.fold_head_tail_ex(_raw_ex, per)` は
      直後の `if _tail_kept:` で**読んでいる**。見るのは使われ方だけ。
      スコープは「その代入を含むいちばん内側の関数（無ければモジュール）の
      部分木」＝入れ子の関数から閉包で読む形も通る。7 巡目はモジュール全体で
      見ていたので、別の関数の同名変数で誤って通っていた。
    """
    import ast

    try:
        tree = ast.parse(src)
    except SyntaxError as e:                       # ④ fail-closed
        return [(getattr(e, "lineno", 1) or 1,
                 f"走査対象の .py が構文解析できない（黙って通さない）: {e.msg}")]

    parent: dict[int, ast.AST] = {}
    scope_of: dict[int, ast.AST] = {}

    def _walk(node: ast.AST, scope: ast.AST) -> None:
        for ch in ast.iter_child_nodes(node):
            parent[id(ch)] = node
            inner = ch if isinstance(
                ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else scope
            scope_of[id(ch)] = scope
            _walk(ch, inner)

    scope_of[id(tree)] = tree
    _walk(tree, tree)

    # docstring 位置の裸の文字列（言及は誤爆させない）
    doc_ids = {id(n.value) for n in ast.walk(tree)
               if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
               and isinstance(n.value.value, str)}

    def _static_str(node: ast.AST) -> str | None:
        """静的に決まる文字列を組み立てる（決まらなければ None）。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
            a, b = _static_str(node.left), _static_str(node.right)
            return None if a is None or b is None else a + b
        if isinstance(node, ast.JoinedStr):
            # 差し込み部分は実行時に決まるので、名前がまたがって出来ないよう
            # 区切りを入れて連結する（定数部分だけを見る）。
            return "\x00".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str))
        return None

    def _is_ex_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        f = node.func
        if isinstance(f, ast.Attribute):
            return f.attr == "fold_head_tail_ex"
        return isinstance(f, ast.Name) and f.id == "fold_head_tail_ex"

    def _loads_in_scope(node: ast.AST) -> set[str]:
        sc = scope_of.get(id(node), tree)
        return {n.id for n in ast.walk(sc)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}

    # ⑤ `fold` モジュールを指す名前。素の `fold` に加えて import の別名も追う
    #    （本番は `from . import fold` が 5 か所・`from .tools import fold as
    #    _fold` が 3 か所）。`x = fold` のような単純な付け替えも追いかける
    #    （増えなくなるまで回す＝別名の別名も入る）。
    fold_names: set[str] = {"fold"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for al in node.names:
                if al.name == "fold":
                    fold_names.add(al.asname or al.name)
        elif isinstance(node, ast.Import):
            for al in node.names:
                if al.name == "fold" or al.name.endswith(".fold"):
                    fold_names.add(al.asname or al.name.split(".")[0])
    while True:
        grown = set(fold_names)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
                    and node.value.id in fold_names):
                grown |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        if grown == fold_names:
            break
        fold_names = grown

    def _is_fold(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in fold_names

    def _callee(node: ast.AST) -> str:
        """`f(...)` / `mod.f(...)` の呼ばれている名前（それ以外は空）。"""
        if not isinstance(node, ast.Call):
            return ""
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return ""

    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        # ① 許可形以外の `fold_head_tail_ex(` の呼び出し
        if _is_ex_call(node):
            par = parent.get(id(node))
            ok = False
            if isinstance(par, ast.Assign) and par.value is node and len(par.targets) == 1:
                tgt = par.targets[0]
                if (isinstance(tgt, ast.Tuple) and len(tgt.elts) == 2
                        and all(isinstance(e, ast.Name) for e in tgt.elts)):
                    ok = tgt.elts[1].id in _loads_in_scope(par)
            if not ok:
                bad.append((node.lineno,
                            "`fold_head_tail_ex(...)` が許可形ではない。通るのは"
                            "「`a, b = …fold_head_tail_ex(…)` かつ `b` を同じ関数で"
                            "読む」1 形だけ（添字・スター展開・`for` 束縛・"
                            "`next()`/`list()` 経由・受け捨ては全部これに当たる）"))
        # ② 1 値版の識別子
        _names: list[str] = []
        if isinstance(node, ast.Name):
            _names.append(node.id)
        elif isinstance(node, ast.Attribute):
            _names.append(node.attr)
        elif isinstance(node, ast.alias):
            _names += [node.name.rsplit(".", 1)[-1], node.asname or ""]
        elif isinstance(node, ast.arg):
            _names.append(node.arg)
        elif isinstance(node, ast.keyword):
            _names.append(node.arg or "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _names.append(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            _names += list(node.names)
        if "fold_head_tail" in _names:
            bad.append((getattr(node, "lineno", 1),
                        "1 値版 `fold_head_tail` を識別子として参照している"
                        "（畳めないと無言で切る）"))
        # ③ 静的に決まる文字列（`__dict__[...]`・連結前の定数）
        if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)) and id(node) not in doc_ids:
            v = _static_str(node)
            if v is not None and "fold_head_tail" in v:
                bad.append((getattr(node, "lineno", 1),
                            "1 値版 `fold_head_tail` が静的に決まる文字列に出ている"
                            "（`__dict__[...]`／定数の連結による名前越しの迂回）"))
        # ⑤ `fold` への動的な属性参照（**鍵の形は見ない**）
        _cal = _callee(node)
        if (_cal in ("getattr", "setattr", "delattr", "vars")
                and isinstance(node, ast.Call) and node.args
                and _is_fold(node.args[0])):
            bad.append((node.lineno,
                        f"`{_cal}(fold, …)` ＝ `fold` の属性を**動的に**引いている。"
                        "鍵が静的に決まらない書き方（`\"\".join([...])`・`\"%s\" %`）は"
                        "名前の走査を素通りするので、入口ごと赤にする"
                        "（本番が触ってよいのは `fold.nbytes` のような直書きだけ）"))
        if (isinstance(node, ast.Attribute) and _is_fold(node.value)
                and node.attr in ("__dict__", "__getattribute__", "__getattr__")):
            bad.append((node.lineno,
                        f"`fold.{node.attr}` ＝ モジュールの中身を辞書として"
                        "引いている（名前の走査を素通りする迂回路）"))
        if (isinstance(node, ast.Subscript)
                and _callee(node.value) in ("globals", "vars")
                and isinstance(node.value, ast.Call) and not node.value.args):
            bad.append((node.lineno,
                        "モジュール辞書（`globals()[…]`／`vars()[…]`）からの"
                        "名前引き。鍵が実行時に決まると静的には追えないので、"
                        "この形自体を落とす"))
    return sorted(set(bad))


def test_本番の畳みは黙って切らない版を使う() -> None:
    """★「書いたら繋ぐ」の門（この木で 5 回踏んだ形）。

    `fold_head_tail`（1 値版）は畳めなかったときに**黙って切る**。それでよいのは
    測定用の bench とこの試験だけで、本番の道具は 2 値版 `fold_head_tail_ex` か
    `fold_head_tail_told` を使うこと。4 巡目の欠陥は、まさに 1 値版が本番に
    2 箇所残っていたために起きた（`tools/bash.py` と `phases._trim_umbra_doc`）。

    走査は `_silent_cut_flags` **1 本**（陽性対照も同じ実体を呼ぶ）。
    禁止形を数えるのではなく、**許可形以外を全部落とす**（8 巡目に切替）。
    """
    from pathlib import Path
    root = Path(fold.__file__).resolve().parents[1]     # blackwall/
    offenders = []
    scanned = 0
    for py in sorted(root.rglob("*.py")):
        if py.name == "fold.py":
            continue                                    # 定義そのもの
        src = py.read_text(encoding="utf-8")
        scanned += 1
        lines = src.splitlines()
        for lineno, why in _silent_cut_flags(src):
            src_line = lines[lineno - 1].strip() if lineno <= len(lines) else ""
            offenders.append(f"{py.relative_to(root)}:{lineno}: {why}: {src_line}")
    assert scanned > 10, f"走査したファイルが {scanned} 本しかない（走査が空振り）"
    assert not offenders, (
        "本番が畳みの失敗を黙って捨てている:\n" + "\n".join(offenders))


def test_黙って切る書き方を探す走査は本当に見つける() -> None:
    """★上の走査の陽性対照。**走査本体と同じ実体**（`_silent_cut_flags`）に食わせる。

    走査試験は「offenders が空」を見るので、**検知器が何にも当たらなくても
    緑**になる。4 巡目に 2 形、6 巡目に別名 import と `#` 迂回、8 巡目に
    **さらに 9 形**が素通りしていたのは、まさにその形。

    下の `bad` は 7 巡目までに素通りした形を**全部**並べてある
    （7 形は当時赤・9 形は当時緑）。ホワイトリストなので、ここに無い新しい
    書き方も自動的に赤になる——それがこの巡の要点。
    """
    bad = [
        # 5〜7 巡目でも赤だった 7 形
        "out = fold.fold_head_tail(text, cap)\n",
        "body, _ = fold.fold_head_tail_ex(text, cap)\n",
        'f = getattr(fold, "fold_head_tail")\n',
        "from .fold import fold_head_tail as _fht\nout = _fht(text, cap)\n",
        "body = fold.fold_head_tail_ex(text, cap)[0]\n",
        "body, _unused = fold.fold_head_tail_ex(text, cap)\n",
        'tag = "#"; out = fold.fold_head_tail(text, cap)\n',
        # ★8 巡目（査読 must_fix#2）: 7 巡目までは **2 passed で素通り**した 9 形
        "body = fold.fold_head_tail_ex(text, cap)[-2]\n",
        "body, *_rest = fold.fold_head_tail_ex(text, cap)\n",
        'fh = fold.__dict__["fold_head_tail"]\n',
        "body = next(iter(fold.fold_head_tail_ex(text, cap)))\n",
        "body = fold.fold_head_tail_ex(text, cap)[0:1][0]\n",
        "body = list(fold.fold_head_tail_ex(text, cap))[0]\n",
        "import operator\noperator.itemgetter(0)(fold.fold_head_tail_ex(text, cap))\n",
        'name = "fold_head_" + "tail"\ngetattr(fold, name)\n',
        "for body, _ in [fold.fold_head_tail_ex(text, cap)]:\n    pass\n",
        # ★9 巡目（査読 must_fix M2'）: 8 巡目までは **2 passed で素通り**した
        #   2 形。どちらも鍵が静的に決まらないので③では見えない（⑤で落とす）。
        'fh = getattr(fold, "".join(["fold_head_", "tail"]))\n',
        'fh = vars(fold)["fold_head_%s" % "tail"]\n',
        # 別名 import 越しでも同じ（本番は `from .tools import fold as _fold`）。
        'from .tools import fold as _fold\nfh = getattr(_fold, k)\n',
        # 単純な付け替えを 1 段はさむ形。
        'm = fold\nfh = getattr(m, k)\n',
        # モジュール辞書からの名前引き（鍵が実行時に決まると追えない）。
        'fh = globals()["fold"].fold_head_tail\n',
        "fh = globals()[k]\n",
        'fh = fold.__dict__[k]\n',
        "fold.__getattribute__(k)\n",
    ]
    for bad_src in bad:
        assert _silent_cut_flags(bad_src), f"走査が {bad_src.strip()!r} を見逃す"

    # ★許可形（＝本番 `umbra.py` が実際に書いている形）は誤爆しない。
    good_src = (
        "def f(raw, per):\n"
        "    folded, tail_kept = fold.fold_head_tail_ex(raw, per)\n"
        "    if tail_kept:\n"
        "        return folded\n"
    )
    assert not _silent_cut_flags(good_src), "走査が許可形を誤爆する"
    # ★この木の流儀（局所変数に `_` を付ける）でも誤爆しないこと。
    #   6 巡目の査読が推奨した正規表現 `,\s*_\w*\s*=\s*fold\.fold_head_tail_ex\s*\(`
    #   はここで当たってしまう（`umbra.py` が実際にこの形）。
    underscored = (
        "def f(_raw_ex, per):\n"
        "    _folded, _tail_kept = fold.fold_head_tail_ex(_raw_ex, per)\n"
        "    if _tail_kept:\n"
        "        return _folded\n"
    )
    assert not _silent_cut_flags(underscored), (
        "`_` 始まりの名前を「捨てている」と決めつけている（本番 umbra.py が赤になる）")
    # ★入れ子の関数から閉包で読む形も通す（スコープを部分木で見ている根拠）。
    closure = (
        "def outer(raw, per):\n"
        "    folded, tail_kept = fold.fold_head_tail_ex(raw, per)\n"
        "    def inner():\n"
        "        return tail_kept\n"
        "    return inner\n"
    )
    assert not _silent_cut_flags(closure), "閉包で読む形を誤爆する"
    # ★**別の関数**の同名変数では通さない（7 巡目はモジュール全体で見ていた）。
    other_scope = (
        "def a(raw, per):\n"
        "    folded, tail_kept = fold.fold_head_tail_ex(raw, per)\n"
        "    return folded\n"
        "def b():\n"
        "    return tail_kept\n"
    )
    assert _silent_cut_flags(other_scope), (
        "別の関数で同名を読んでいるだけで通している（スコープを見ていない）")
    # ★`fold_head_tail_told`（畳めなくても言う版）と他の `_ex` は誤爆しない。
    assert not _silent_cut_flags("out = fold.fold_head_tail_told(text, cap)\n")
    assert not _silent_cut_flags("x, _ = fold.something_else_ex(text, cap)\n")
    # docstring や註での**言及**は誤爆しない（本番 `phases.py:1099,1152` が該当）。
    assert not _silent_cut_flags('"""`fold_head_tail` は使わない"""\n'), (
        "docstring の言及で赤になる（本番 phases.py の註が該当する）")
    # ★構文が壊れた .py は fail-closed（7 巡目までは黙って通していた）。
    assert _silent_cut_flags("def f(:\n"), "壊れた .py を黙って通している"
    # ★⑤は `fold` **以外**への `getattr` を誤爆しない（本番 `blackwall/` には
    #   `getattr(agent, …)` 等が 40 か所以上ある。ここが赤くなると走査試験は
    #   `fold` と無関係な行で落ち、意味を失う）。
    for ok_src in ('cp = getattr(agent, "checkpoint", None)\n',
                   'out = getattr(result, "output", "") or ""\n',
                   "names = vars(args)\n",
                   'x = fold.nbytes(s)\n',
                   'from . import fold\ny = fold.min_foldable_cap()\n'):
        assert not _silent_cut_flags(ok_src), f"⑤が {ok_src.strip()!r} を誤爆する"

    # ★★申告した限界を**その場で固定する**（緑なのが仕様＝止められない形）。
    #   ①が課しているのは「2 つめを同じ関数スコープで `Load` する」だけで、
    #   読んだ値で何をするかは静的な木から決められない。査読が実測した
    #   「読むが使わない」2 形は、いまも通る:
    for gap_src in ("body, told = fold.fold_head_tail_ex(text, cap); told\n",
                    "body, told = fold.fold_head_tail_ex(text, cap); _ = told\n"):
        assert not _silent_cut_flags(gap_src), (
            "限界の申告と実際がずれた（この形は通ると上のコメントに書いてある）")
