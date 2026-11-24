

def test_切り詰めたら型で名乗る(tmp_path):
    """★2026-09-02・記録の `truncated` 欄が 10,904 件すべて false だった件。

    受け口の `truncated` は「100,000 字の上限で切ったか」しか見ていない。
    一方、本文に「切り詰め」と書かれた記録は 161 件ある＝**道具が各自で切っていた**。
    欄の名前は「切り詰められたか」と読めるのに中身が違い、
    **道具ごとの上限の効果を測ろうとして見えない欄を使いかけた**。
    → 道具が `ToolResult(truncated=True)` で名乗り、受け口が合成して報告する。
    """
    from laps.tools import as_result
    from laps.tools import read as R

    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line {i} " + "x" * 200 for i in range(4000)),
                   encoding="utf-8")
    out = R.SPEC.handler({"file_path": str(big)})
    assert "打ち切りました" in str(out), "そもそも切り詰めていない（前提が変わった）"
    assert getattr(as_result(out), "truncated", False) is True, \
        "切り詰めたのに名乗っていない（記録から効果が測れない）"


def test_切り詰めていなければ名乗らない(tmp_path):
    """★誤って常に True にすると、今度は「切っていないのに切った」になる。"""
    from laps.tools import as_result
    from laps.tools import read as R

    small = tmp_path / "s.txt"
    small.write_text("a\nb\nc\n", encoding="utf-8")
    out = R.SPEC.handler({"file_path": str(small)})
    assert getattr(as_result(out), "truncated", False) is False


def test_複数のファイルを1回で読める(tmp_path):
    """★2026-09-02・道具の**形**を変える（一言では変わらないと実測で出たため）。

    実測: 単発の読み取りが連続する手番が **2,234/9,397 = 23.8%**。
    「まとめて呼べます」の一言は 488 回発火したが、次の手番が複数呼び出しに
    なったのは **1 回（0.20%）**で、全手番の基準 2.32% を**下回った**。
    ＝結果の中の一言は「続け方」は変えても「**出力の形**」は変えない。
    """
    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BBB\n", encoding="utf-8")
    from laps.tools import read as R

    out = str(R.SPEC.handler({"file_path": [str(tmp_path / "a.txt"),
                                            str(tmp_path / "b.txt")]}))
    assert "AAA" in out and "BBB" in out, "両方の中身が返っていない"
    assert out.count("=====") >= 4, "どのファイルの内容か分かる区切りが無い"


def test_読めない1本があっても他は返る(tmp_path):
    """★黙って落とすと「読んだつもり」になる（この現場で何度も踏んだ形）。"""
    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    from laps.tools import read as R

    out = str(R.SPEC.handler({"file_path": [str(tmp_path / "a.txt"),
                                            str(tmp_path / "no.txt")]}))
    assert "AAA" in out, "読めた側が消えている"
    assert "no.txt" in out and ("not found" in out or "見つかり" in out), \
        "読めなかったファイルの理由が、その場所に出ていない"


def test_文字列を渡す従来の呼び方は変わらない(tmp_path):
    """★互換。既存の呼び出しは 1 文字も変えなくてよい。"""
    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    from laps.tools import read as R

    out = str(R.SPEC.handler({"file_path": str(tmp_path / "a.txt")}))
    assert "AAA" in out and "=====" not in out, "単体の呼び方に区切りが混ざっている"


def test_一度に読める本数に上限がある(tmp_path):
    """★上限が無いと 1 回の結果で窓を食い潰す（画像 Read に上限が無く 16K 窓が死んだ前例）。"""
    from laps.tools import read as R

    paths = []
    for i in range(R._MANY_MAX + 1):
        f = tmp_path / f"f{i}.txt"; f.write_text("x\n", encoding="utf-8"); paths.append(str(f))
    out = str(R.SPEC.handler({"file_path": paths}))
    assert "Error" in out and str(R._MANY_MAX) in out, "上限を超えても黙って受けている"
