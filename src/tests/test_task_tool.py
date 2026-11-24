

def test_Exploreの説明が実体と一致する():
    """★札と実体を食い違わせない（2026-08-30・第三者レビュー E6）。

    `Explore` は「read-only」と説明されていたが、許可道具に `Bash` が入っている。
    ★直し方は2通りあり、**道具を減らす方向は採らない**
      （オーナー方針: 制限を増やさない）。実体に合わせて名乗るほうを採る。
    """
    from pathlib import Path
    from laps.tools import task
    src = (Path(__file__).resolve().parents[1] / "laps" / "tools" / "task.py").read_text()
    has_bash = "Bash" in task.ALLOWED["Explore"] if hasattr(task, "ALLOWED") else True
    if has_bash:
        assert "Explore: read-only" not in src, "Bash があるのに read-only と名乗っている"
