"""`/trace` — この走行で**何を実行したか**を通しで見る（2026-09-13）。

★オーナー指摘:「内部思考とか、何を実行したのかとかが圧縮で見えなくなってしまっているのでは？」
  ★記録は消えていない（`context_budget.py` 冒頭「送信するビューだけを削る。
    記録（session.messages）には触らない」）。消えていたのは**画面**:
      ・`/expand` は直前のターンの束だけ（`start_turn` で前の束を捨てる）
      ・`/verbose` は思考を出すが、次のターンから（過去は出ない）
  → 後から通しで見る手段が無かった。`/trace` がそこを埋める。
"""
from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from laps.term import slash

WIRE = [
    {"type": "tool_call", "id": "r1", "name": "Bash", "call": "c1",
     "input": {"command": "ls -la"}},
    {"type": "tool_result", "id": "r1", "name": "Bash", "call": "c1",
     "ok": True, "result": "a\nb\nc"},
    {"type": "tool_call", "id": "r1", "name": "Read", "call": "c2",
     "input": {"file_path": "/x/agent.py"}},
    {"type": "tool_result", "id": "r1", "name": "Read", "call": "c2",
     "ok": False, "result": "No such file"},
    {"type": "result", "id": "r1", "counters": {"ctx_budget_fired": 6}},
]
#: 記録（events.jsonl）の形 — 欄は `payload` の中。★線と形が違うのがこの現場の罠。
REC = [{"type": e["type"], "seq": i, "ts": "t",
        "payload": {k: v for k, v in e.items() if k != "type"}}
       for i, e in enumerate(WIRE)]


class _Ctx:
    def __init__(self, events, session=""):
        self.console = Console(record=True, width=200, force_terminal=False,
                               file=io.StringIO())
        self.events = events
        self.session = session


def _run(events, args="", session="") -> str:
    c = _Ctx(events, session)
    slash._cmd_trace(c, args)
    return re.sub(r"\s+", " ", c.console.file.getvalue())


def test_線の形で読める():
    out = _run(WIRE)
    assert "道具 2 回" in out and "圧縮 6 回" in out
    assert "ls -la" in out and "/x/agent.py" in out
    assert "失敗: No such file" in out


def test_記録の形でも読める():
    """★`payload` の中に欄がある形。片方だけ対応するのがこの現場の典型的な欠陥。"""
    out = _run(REC)
    assert "道具 2 回" in out and "圧縮 6 回" in out
    assert "ls -la" in out


def test_圧縮は_モデルの視界の話だと明示する():
    """★ここを曖昧にすると「記録も消えた」と読まれる（実際にそう誤解された）。"""
    out = _run(WIRE)
    assert "記録は削られていません" in out


def test_番号で1件を全文():
    out = _run(WIRE, "2")
    assert "file_path" in out and "No such file" in out


def test_範囲外と読めない引数は黙って捨てない():
    assert "ありません" in _run(WIRE, "9")
    assert "読めません" in _run(WIRE, "zzz")


def test_空状態には次の一手():
    out = _run([])
    assert "まだ記録がありません" in out and "/trace" in out


def test_道具ゼロの走行をそう言う():
    out = _run([{"type": "result", "id": "r1", "counters": {}}])
    assert "道具は使われていません" in out


def test_受け取った分は_取りこぼしを明示する():
    """★deque(maxlen=200) なので古い方は落ちている。『全部です』と見せない。"""
    out = _run(WIRE)
    assert "古い方は落ちています" in out


def test_結果が無い呼び出しもそう言う():
    ev = [{"type": "tool_call", "id": "r1", "name": "Bash", "call": "c1",
           "input": {"command": "sleep 99"}}]
    out = _run(ev)
    assert "（結果なし）" in out
    assert "（結果が記録にありません" in _run(ev, "1")


def test_長い結果は中央を省いて頭と尾を残す():
    big = "\n".join(f"line{i}" for i in range(500))
    ev = [{"type": "tool_call", "id": "r1", "name": "Bash", "call": "c1",
           "input": {"command": "seq 500"}},
          {"type": "tool_result", "id": "r1", "name": "Bash", "call": "c1",
           "ok": True, "result": big}]
    out = _run(ev, "1")
    assert "line0" in out and "line499" in out
    assert "中略" in out, "黙って切り詰めている"


def test_対応_id_が無くても名前で当てる():
    """★`call` が取れない経路は実在する（render.py も同じ当て方をしている）。"""
    ev = [{"type": "tool_call", "id": "r1", "name": "Bash", "input": {"command": "x"}},
          {"type": "tool_result", "id": "r1", "name": "Bash", "ok": True, "result": "ok"}]
    out = _run(ev)
    assert "（結果なし）" not in out, "結果を呼び出しに当てられていない"
    assert "2 字" in out, out


def test_登録されている():
    """★「作ったが繋いでいない」の見張り。/help にも出ること。"""
    assert "trace" in slash.COMMANDS
    cmd = slash.COMMANDS["trace"]
    assert cmd.handler is slash._cmd_trace
    assert not cmd.hidden, "/help の一覧に出ない"
    assert "圧縮" in cmd.help, "何のための道具かが一覧から読めない"
