"""★2026-10-21 オーナー「Linux の TUI 一旦見直しますか。会話の部分」。偽の推論機で本物の serve と TUI を疑似端末で流して見つけた点。

- 予定表（TodoWrite）が「⎿ [」（結果の JSON の頭）としか出ない → 段のチェックリスト（最初は全部・更新は変わった段だけ）
- 答えの後に「✓ Done ⎿ OK」が出る → 出さない
- 次の手番の思考が、その前に走った読み取りの道具より先に並ぶ → 起きた順に
- 日本語の文が英単語の後ろで丸ごと次の行へ送られ、行の右が大きく空く → 日本語は 1 字ずつ折る（句読点は行頭に置かない）
- 表の前後が 2 行ずつ空く → 1 行
- 起動の銘が会話の枠に収まらず「BLACKWF」と欠ける → 幅に合わせて選ぶ・副題も入る分だけ
"""
from __future__ import annotations

import io
import json

from rich.console import Console

from laps.term import banner as B
from laps.term.render import Renderer, _md_block, cell_width, plan_items, wrap_with_gutter
from tests.test_term_render import _call, _console, _done, _result, _turn


def _todo(*pairs):
    return {"todos": [{"content": c, "status": s} for c, s in pairs]}


def _todo_result(*pairs, head=""):
    return head + json.dumps([{"content": c, "status": s} for c, s in pairs], ensure_ascii=False, indent=2) + "\n\nNow: x"


def _plain(c: Console) -> str:
    return c.export_text(clear=False)


def test_plan_items_reads_the_result_after_a_host_note_and_falls_back_to_the_input():
    res = _todo_result(("a", "completed"), ("b", "in_progress"), head="[Host] devbox now: CPU 6%\n\n")
    assert plan_items(res, {}) == [("a", "completed"), ("b", "in_progress")]
    assert plan_items("[{\"content\": \"a\"", _todo(("a", "pending"))) == [("a", "pending")]      # 切れた結果は引数


def test_plan_is_a_checklist_first_then_only_the_changes_and_done_is_hidden():
    c = _console()
    r = Renderer(c)
    items = ["配備を読む", "サービスを確かめる", "報告する"]
    _turn(r, [
        _call("TodoWrite", _todo((items[0], "in_progress"), (items[1], "pending"), (items[2], "pending")), 1),
        _result("TodoWrite", 1, _todo_result((items[0], "in_progress"), (items[1], "pending"), (items[2], "pending"))),
        _call("TodoWrite", _todo((items[0], "completed"), (items[1], "in_progress"), (items[2], "pending")), 2),
        _result("TodoWrite", 2, _todo_result((items[0], "completed"), (items[1], "in_progress"), (items[2], "pending"))),
        _call("Done", {}, 3),
        _result("Done", 3, "OK"),
        _done("できました。"),
    ])
    text = _plain(c)
    assert "⎿  [" not in text and "Done" not in text
    assert "予定  0/3" in text and "◉ 配備を読む" in text and "◌ サービスを確かめる" in text
    assert "予定  1/3" in text and "✓ 配備を読む" in text and "◉ サービスを確かめる" in text
    assert text.count("報告する") == 1                    # 変わっていない段は 2 回目に出さない


def test_live_line_names_the_current_step():
    r = Renderer(_console())
    r._plan = [("a", "completed"), ("b", "in_progress"), ("c", "pending")]
    assert r.plan_now(80).plain == "◉ 2/3  b"
    r._plan = [("a", "completed")]
    assert r.plan_now(80) is None


def test_next_thought_comes_after_the_read_tool_that_ran_before_it():
    c = _console()
    r = Renderer(c)
    _turn(r, [
        _call("Read", {"file_path": "a.txt"}, 1),
        _result("Read", 1, "x", summary={"head": "a.txt", "tail": "Read 1 lines", "n": 1, "status": "ok"}),
        {"type": "think", "id": "r1", "text": "次はサービスの状態を見る。"},
        _call("Bash", {"command": "echo hi"}, 2),
        _result("Bash", 2, "hi\n"),
        _done("おわり"),
    ])
    text = _plain(c)
    assert text.index("Read (a.txt)") < text.index("次はサービスの状態を見る")


def test_japanese_fills_the_line_after_an_english_word():
    c = Console(record=True, width=60, force_terminal=False, file=io.StringIO())
    c.print(_md_block(c, "Console は Pi の systemd のサービスとして動いていて、公開サイトも応答しています。"))
    first = _plain(c).splitlines()[0]
    assert cell_width(first) >= 56, first                  # 旧は「Console は Pi の systemd」（24 桁）で折れていた


def test_no_line_starts_with_a_closing_mark():
    out = wrap_with_gutter("依頼は Console の配備を読み、Pi のサービスを確かめて報告すること。手順が 4 つある。", 40, "∴ ", "  ")
    assert not any(ln.lstrip().startswith(("。", "、")) for ln in out.splitlines()), out


def test_table_has_one_blank_line_around_it():
    c = Console(record=True, width=60, force_terminal=False, file=io.StringIO())
    c.print(_md_block(c, "前の段落。\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n後の段落。"))
    lines = _plain(c).splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.strip().startswith("a"))
    assert lines[i - 1].strip() == "" and lines[i - 2].strip() == "前の段落。"
    j = next(k for k, ln in enumerate(lines) if ln.strip().startswith("1"))
    assert lines[j + 1].strip() == "" and lines[j + 2].strip() == "後の段落。"


def test_small_banner_fits_the_pane():
    for w in (20, 40, 60, 72, 76):
        assert cell_width(B.banner(w).plain.splitlines()[0]) <= w, w
    assert "─" in B.banner(80).plain                       # 広ければ大きな銘
