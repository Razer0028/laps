"""推論中の常時ステータス行（REPORTS/uiux-redesign-20260906 §B・0.1.19x）。

オーナー要望: 「推論中に何が起きているか分かりづらい」。旧実装は心拍（15 秒沈黙）が
立つときだけ 1 行出しており、ターン開始から沈黙 15 秒までは何も見えなかった。ここでは
本物の Renderer / _LiveView に**偽イベント列**を流し、目視でなくアサートで固定する:

  ①ターン開始直後から出る（心拍を待たない） ②経過が増える ③トークン推定が出る
  ④推定はステータス行だけ＝Live 終了（done）で消え、締めには実測が出る
  ⑤NO_COLOR / 非 TTY では脈動印を静止印へ退避 ⑥幅を動かさない（狭いと後ろから落とす）
"""
from __future__ import annotations

import io
import time

from rich.console import Console

from laps.term import render as R
from laps.term.render import Renderer, _LiveView, cell_width


def _console(tty: bool = True) -> Console:
    return Console(record=True, width=80, force_terminal=tty, file=io.StringIO())


def _frame(r: Renderer, width: int = 80) -> str:
    """`_LiveView` を 1 枚描いてテキストにする（Live のフレームそのもの）。"""
    c = Console(record=True, width=width, force_terminal=True, file=io.StringIO())
    c.print(_LiveView(r))
    return c.export_text()


def _open(r: Renderer, ago: float = 0.0) -> None:
    """Live スレッドを立てずにターンを開いた状態にする（表示ロジックだけ見る）。"""
    r._open = True
    r._t0 = time.time() - ago


# -- ①②③ ターン開始直後・経過・トークン ------------------------------------

def test_status_line_appears_from_turn_start_before_any_heartbeat():
    r = Renderer(_console())
    _open(r, ago=233)                               # 3m 53s 経過
    r.on_event({"type": "token", "id": "r1", "text": "x" * 12400})   # ~3.1k
    txt = _frame(r)
    assert "3m 53s" in txt, txt                     # 経過が出ている
    assert "↓ ~3.1k" in txt, txt                    # トークン推定（~ 付き）
    assert "待っています" not in txt                # 心拍を待っていない（常時出る）


def test_status_elapsed_grows_and_uses_spaced_format():
    r = Renderer(_console())
    _open(r, ago=5)
    assert "5s" in _frame(r)
    _open(r, ago=65)
    assert "1m 05s" in _frame(r)                    # `fmt_secs2`（空白入り）


# -- 主ラベルは substance（実際に起きていること）------------------------------

def test_status_shows_running_tool_substance_not_gerund():
    r = Renderer(_console())
    _open(r)
    r.on_event({"type": "tool_call", "id": "r1", "name": "Read",
                "input": {"file_path": "/srv/x/api.py"}, "call": 1})
    txt = _frame(r)
    assert "api.py を読込" in txt, txt              # ツール名＝主役
    assert "Nebulizing" not in txt                  # ツール実行中に Gerund を出さない


def test_status_two_running_tools_are_counted():
    r = Renderer(_console())
    _open(r)
    r.on_event({"type": "tool_call", "id": "r1", "name": "Bash",
                "input": {"command": "sleep 1"}, "call": 1})
    r.on_event({"type": "tool_call", "id": "r1", "name": "Bash",
                "input": {"command": "sleep 2"}, "call": 2})
    assert "ツール 2 個を実行中" in _frame(r)


def test_status_pure_inference_rotates_gerund():
    r = Renderer(_console())
    _open(r, ago=0.0)                               # elapsed≈0 → 表の先頭
    assert R._gerund(0.0) + "…" in _frame(r)        # 純推論のときだけ Gerund


# -- ④ 推定は Live 限り・締めは実測 -------------------------------------------

def test_estimate_stays_in_live_and_done_shows_actual():
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event({"type": "token", "id": "r1", "text": "hi"})
    r.on_event({"type": "done", "id": "r1", "outcome": "accepted", "text": "hi",
                "secs": 34.0, "tok_s": 40.0, "tool_calls": {},   # ≥10s ＝締めが出る条件
                "total_tokens": 1234, "prompt_tokens": 500})
    r.end_turn()
    txt = c.export_text()
    assert "↓ ~" not in txt, txt                    # 推定はステータス行だけ＝Live で消えた
    assert "入力 500" in txt, txt                    # 締めは実測（~ なし）
    assert "出力 734" in txt, txt


def test_done_line_omits_tokens_when_backend_gave_none():
    """usage が無ければ実測は出さない（推定を実測のように見せない）。"""
    c = _console()
    r = Renderer(c)
    r.start_turn("r1")
    r.on_event({"type": "done", "id": "r1", "outcome": "accepted", "text": "",
                "secs": 34.0, "tok_s": 61.0, "tool_calls": {"Bash": 12}})
    r.end_turn()
    txt = c.export_text()
    assert "↓" not in txt and "↑" not in txt, txt   # 実測が無いので出さない


# -- ⑤ reduced-motion / 非 TTY の退避 ----------------------------------------

def test_pulse_is_static_when_reduced_motion(monkeypatch):
    r = Renderer(_console(tty=False))              # 非 TTY
    assert r._status_animate() is False
    r2 = Renderer(_console())
    monkeypatch.setenv("NO_COLOR", "1")            # TTY だが NO_COLOR
    assert r2._status_animate() is False
    monkeypatch.setenv("TERM", "dumb")
    assert r2._status_animate() is False


def test_no_color_suppresses_always_on_line_but_keeps_heartbeat(monkeypatch):
    """NO_COLOR では常時ステータス行を出さず、旧来どおり心拍の待ち行だけに退避する
    （§E・check_tui_width.py の「色の無い端末で ✓/✗ に割れる」門を崩さないため）。"""
    monkeypatch.setenv("NO_COLOR", "1")
    r = Renderer(_console())
    assert r._status_enabled() is False
    _open(r, ago=5)
    r.on_event({"type": "token", "id": "r1", "text": "hi"})
    txt = _frame(r)
    assert "↓ ~" not in txt and "5s" not in txt         # 常時行は出ない
    # 心拍が来れば旧来の待ち行は出る（NO_COLOR でも消さない）
    r.on_event({"type": "notice", "id": "r1", "kind": "heartbeat",
                "stage": "推論機", "quiet_secs": 20})
    assert "推論サーバーの応答を待っています（20 秒）" in _frame(r)


def test_static_mark_and_full_row_when_wide():
    r = Renderer(_console())
    row = r._status_row(running=[], waiting="", quiet=0, elapsed=233.0,
                        out_chars=12400, width=80, animate=False, now=0.0,
                        live_turn=True)
    p = row.plain
    assert p.startswith(R._PULSE_STILL + " ")       # 静止印
    assert "\n" not in p                            # 1 物理行
    assert "3m 53s" in p and "↓ ~3.1k" in p


# -- ⑥ 幅を動かさない（狭いと tokens→経過 を落とす・印と主ラベルは残す）--------

def test_narrow_drops_tokens_first_and_never_exceeds_width():
    r = Renderer(_console())
    narrow = r._status_row(running=[], waiting="", quiet=0, elapsed=233.0,
                           out_chars=12400, width=18, animate=False, now=0.0,
                           live_turn=True)
    p = narrow.plain
    assert "↓" not in p                             # トークンが先に落ちる
    assert R._gerund(233.0) in p                    # 主ラベルは残る
    for ln in p.split("\n"):                        # どの物理行も幅を超えない
        assert cell_width(ln) <= 18, (ln, cell_width(ln))


# -- 心拍と共存（沈黙注記は末尾へ）--------------------------------------------

def test_running_tool_appends_silence_note_from_heartbeat():
    r = Renderer(_console())
    _open(r)
    r.on_event({"type": "tool_call", "id": "r1", "name": "Bash",
                "input": {"command": "pytest -q"}, "call": 1})
    r.on_event({"type": "notice", "id": "r1", "kind": "heartbeat",
                "stage": "道具 Bash", "quiet_secs": 40})
    txt = _frame(r)
    assert "pytest を実行中" in txt                 # ツール名（substance）が主役
    assert "（40 秒 応答なし）" in txt              # 沈黙は末尾へ添える
