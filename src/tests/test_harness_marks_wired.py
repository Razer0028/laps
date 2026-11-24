"""ハーネスが挟む印が、serve と端末の「モデルの本文ではない」経路に繋がっていること。

★由来（2026-09-12）: 元は `tests/test_gate_marks_wired.py`＝2026-09-03 の 3 つの門
  （verifier / red_tests / rerun）の印を見るファイルだった。差し戻し（強制再ループ）は
  オーナー指示で撤去したので、その 3 つの試験は役目を終えた。
  ★**印の一覧そのものは残す価値がある**——「印が一覧に無いと GUI・端末が前の下書きを
  捨てる経路を通らない＝書いたが繋いでいない」という、この現場が繰り返す欠陥の見張りだから。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from rich.console import Console

from laps import serve as srv
from laps.term.render import Renderer

#: ★2026-10-21 旧中核（agent.py）を外した。印を出しうるのは今の中核（oc/）と道具（tools/）。
_LIVE = [*(Path(__file__).resolve().parents[1] / "laps" / d for d in ("oc", "tools"))]






def test_端末は差し戻しの通知で下書きを捨てる():
    """差し戻しは撤去したが、**通知で下書きを捨てる経路**自体は残っている
    （`[reset ]` と、将来また挟む文のため）。ここが切れると本文が二重に出る。"""
    c = Console(record=True, width=80, force_terminal=True, file=io.StringIO())
    r = Renderer(c)
    r.on_event({"type": "token", "id": "r1", "text": "下書きの本文"})
    r.on_event({"type": "notice", "id": "r1", "kind": "pushback",
                "message": "[reset 2] 同じ返答が続いたので、いったん捨てます"})
    assert r._body_text == "" and r._turn_text == ""
