"""道具の分類が、呼ぶ側の手書き一覧ではなく**道具自身の宣言**から来ること。

★2026-08-18 敵対レビューの構造的指摘:
「並行してよいか」「Plan モードで止めるか」「実行の主張の裏付けになるか」を、
呼ぶ側が書いた**道具名の frozenset 3つ**で決めていた。道具を足した人が一覧に
載せ忘れると**黙って無防備になり、しかも気づけない**。
実際 `action_guard._EXEC_TOOLS` には**実在しない RunTests** が入っていた。
"""
from __future__ import annotations


from laps.tools import NEUTRAL_TOOLS, all_specs, tools_where


def test_全ての道具が分類されている() -> None:
    """★宣言し忘れをここで落とす。新しい道具は必ずどれかを名乗る
    （本当に中立なら NEUTRAL_TOOLS に明記する）。"""
    classified = tools_where(readonly=True) | tools_where(mutates=True) | tools_where(executes=True)
    unclassified = {t.name for t in all_specs()} - classified - NEUTRAL_TOOLS
    assert not unclassified, (
        f"能力を宣言していない道具: {sorted(unclassified)}\n"
        "  readonly / mutates / executes のどれかを SPEC に書くか、"
        "本当に中立なら tools.NEUTRAL_TOOLS に加えること。")


def test_分類は実在する道具だけを指す() -> None:
    """★手書き一覧の壊れ方: 実在しない名前が混じっても誰も気づかない。"""
    real = {t.name for t in all_specs()}
    for flag in ("readonly", "mutates", "executes", "parallel_safe"):
        assert tools_where(**{flag: True}) <= real, f"{flag} に実在しない道具がある"


def test_並行して安全は読み取り専用と別の旗() -> None:
    """★2026-09-13: RemoteRead を廃止したので RemoteShell で見る。
    RemoteShell は書き込みもするので読み取り専用ではないが、**同じホストへ並列 SSH すると締め出される**
    （自ラボで実際に起きた事故）。1つの旗にまとめない。"""
    ro, ps = tools_where(readonly=True), tools_where(parallel_safe=True)
    assert "RemoteShell" not in ro, "RemoteShell は書き込みもするので読み取り専用ではない"
    assert "RemoteShell" not in ps, "同じホストへ並列 SSH すると締め出される（incidents/2026-08-04）"
    assert ps < ro, "並行して安全な集合は、読み取り専用の真部分集合であるべき"


def test_歴史的な振る舞いを変えていない() -> None:
    """★この差し替えは**分類の出どころ**を変えるだけで、判断は変えない。
    旧: 手書きの frozenset。新: 宣言からの導出。結果が一致することを固定する。"""
    assert tools_where(parallel_safe=True) == frozenset(
        {"Read", "Grep", "Glob", "WebFetch", "WebSearch"})
    # ★2026-09-10: RemoteShell を追加（遠隔ホストで任意コマンド＝mutates かつ executes）。
    # ★2026-10-18: RemoteShell は 2026-10-05 に道具ごと外した（tools/__init__.py の注記）＝期待値からも外す。
    assert tools_where(mutates=True) == frozenset(
        {"Write", "Edit", "ApplyDiff", "NotebookEdit", "Bash"})
    # executes は旧一覧から**実在しない RunTests を落とし、Task を加えた**
    #（子エージェントは外を動かすので、実行の主張の裏付けになる）。
    # ★2026-09-21: Verify を追加（コマンドを実際に走らせて結果を突きつける道具）。
    #   executes に入るのは正しい——外を動かすので「実行した」の裏付けになる。
    assert tools_where(executes=True) == frozenset(
        {"Bash", "BashOutput", "Task", "Verify"})


