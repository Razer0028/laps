"""W5（gateway + Android）: serve が出す事象と、線の先（gateway 経由・Android）が扱う事象の
食い違いを**機械で**突き合わせる。

★なぜ（過去の反省）: `scripts/check_protocol.py` の docstring にある通り、この現場は
  「両側を別々に書いた」食い違いを**目視で3回**外している。同じ形の食い違いが
  `serve.py` の `_HARNESS_MARKERS`（本文の印の一覧）と Android の `HarnessMarker`
  （その写し）のあいだにも起きていた——「3 つの門」だったはずが 4 つ目
  （host_scope・`agent.py:807`）が増えたのに、**両方の一覧が揃って更新されなかった**
  （既存の `tests/test_gate_marks_wired.py::test_serve_knows_the_new_marks` も
  verifier/red_tests/rerun の 3 つしか確かめておらず、同じ盲点を引き継いでいる）。

  この試験は 2 つのことをする:
    A. 本物の `laps.serve._RunState`（gateway が中継する行を実際に作る変換器）を
       **その関数のまま**呼び、agent.py が実際に出す事象が線にどう出る／出ないかを確かめる。
    B. その結果を、Android 側の実ソース（`Notice.kt` / `EventReducer.kt`）を読んで
       静的に突き合わせる（Kotlin はこの環境で実行できないので、`check_protocol.py` が
       Rust の match アームを読むのと同じ理由で静的読解にする）。

★ゲートウェイ自身（`laps/gateway.py`）は行を検証も変換もしない単純な中継なので、
  「serve が出す行」＝「gateway を通った後の行」。ゲートウェイ側の対象は
  `tests/test_gateway.py` にある（子プロセスの異常終了・再接続・引き継ぎ等）。
"""
from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from laps import serve as servemod

SRC = Path(__file__).resolve().parents[1]
AGENT_PY = SRC / "laps" / "agent.py"
SERVE_PY = SRC / "laps" / "serve.py"
ANDROID = Path(__file__).resolve().parents[2] / "android" / "app" / "src" / "main" / "kotlin" / "jp" / "example" / "laps"
NOTICE_KT = ANDROID / "protocol" / "Notice.kt"
NDJSON_PARSER_KT = ANDROID / "protocol" / "NdjsonParser.kt"
EVENT_REDUCER_KT = ANDROID / "ui" / "model" / "EventReducer.kt"


# ---------------------------------------------------------------------------
# A. 本物の _RunState を動かす小さな台（本番の Emitter をそのまま使う）
# ---------------------------------------------------------------------------

@dataclass
class _FakeSession:
    messages: list


@dataclass
class _FakeAgent:
    session: _FakeSession


class _FakeSrv:
    """`_RunState` が触る欄だけを持つ殻。`out` は**本物の** `serve.Emitter`
    （行の組み立て・JSON 化はここが本番と同じコードで行う）。"""

    def __init__(self, stream: io.StringIO) -> None:
        self._lock = threading.Lock()
        self._agent: _FakeAgent | None = None
        self.out = servemod.Emitter(stream)


def _run_state(stream: io.StringIO) -> tuple[servemod._RunState, _FakeSrv]:
    srv = _FakeSrv(stream)
    spec = servemod.SendSpec(id="r1", prompt="x")
    return servemod._RunState(srv, spec), srv


def _wire_lines(stream: io.StringIO) -> list[dict]:
    import json
    return [json.loads(l) for l in stream.getvalue().splitlines() if l.strip()]


def _fire_gate_marker(marker_text: str) -> list[dict]:
    """`_push_back_on_*` が実際にする順番を再現する:
    1) モデルの本物の返答を session に assistant として積み、同じ文面で `assistant` イベントを出す
       （`agent.py:3561-3570` と同じ順序）。
    2) 差し戻しの印つきテキストを **session に積む前に** `assistant` イベントとして出す
       （`agent.py:749/807/841/893` は marker を emit した「後で」user メッセージを積む）。
    こうして初めて、serve 側の `_is_model_text` が実際に踏む分岐と同じ状態になる。
    """
    stream = io.StringIO()
    state, srv = _run_state(stream)
    srv._agent = _FakeAgent(session=_FakeSession(messages=[]))
    real_reply = "作業は完了しました。"
    srv._agent.session.messages.append({"role": "assistant", "content": real_reply})
    state.on_agent_event("assistant", {"text": real_reply, "iter": 1})
    state.on_agent_event("assistant", {"text": marker_text, "iter": 1})
    state.flush()
    return _wire_lines(stream)


# 4 つの完了門（すべて `agent.py` の同じ形の押し戻し: pushback カウンタ→
# `_force_tool_next=False`→marker を assistant として emit→user メッセージを積む→continue）。
# ★この一覧は `tests/test_gate_marks_wired.py` の対象と同じだが、あちらは
#   `_HARNESS_MARKERS` に「載っているか」だけを見ていて host_scope を持たない。
#   ここでは実際の**線への出力**を見る。
_GATE_MARKERS = {
    "verifier": "[verifier 1] 完了の主張と作業場の実体が食い違う",   # agent.py:893
    "red_tests": "[red_tests 1] 最後のテストが 2 件失敗のまま完了にしない",  # agent.py:841
    "rerun": "[rerun 1] 合格を 1 回しか見ていない試験（証拠 [3]）を再現してから締める",  # agent.py:749
    "host_scope": "[host_scope 1] 依頼は 192.0.2.160 の話だが、そのホストに触れた道具呼び出しが 0 回",  # agent.py:807
}










# ---------------------------------------------------------------------------
# B. Android 側の実ソースを静的に読んで突き合わせる
# ---------------------------------------------------------------------------

def _kotlin_string_literals_after(text: str, anchor: str, *, within_chars: int = 4000) -> list[str]:
    """`anchor` の出現位置から `within_chars` 文字ぶんの範囲にある `"..."` 文字列リテラルを集める。
    Kotlin を構文解析する道具はこの環境に無いので、`scripts/check_protocol.py` と同じ流儀で
    正規表現の静的読解にする（実行できない言語を「読まないよりは桁違いに良い」扱いにする）。"""
    i = text.find(anchor)
    assert i >= 0, f"アンカーが見つからない: {anchor!r}"
    window = text[i:i + within_chars]
    return re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', window)


def _agent_gate_marker_prefixes() -> dict[str, str]:
    """agent.py の実ソースから `_push_back_on_*` メソッド（完了の主張を差し戻す門——
    pushback カウンタを立て、user メッセージを積んで `continue` する、という同じ形を
    共有する族）を見つけ、各メソッド本体の `f"[<name> ...` 印を拾う。

    ★スコープをここまで絞る理由: `self._emit("assistant", {"text": f"[..."...})` は
      agent.py に他にも複数ある（例: postedit の検査失敗 `f"[検査 {f.name}] ..."`）が、
      あれは「本文と同じ型で流れる通知」ではあっても、**完了の主張を差し戻して
      下書きを畳む**という `discardsDraft()` の対象ではない（tool 結果の処理中に
      鳴るのであって、まだ「締めの答え」が出ていない）。族を広げすぎると、
      畳んではいけないところを畳む回帰を作りかねない——ここは `_push_back_on_*`
      という**構造で**絞ることで、手で書き写さずに、かつ誤検出も避ける。
    """
    src = AGENT_PY.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for m in re.finditer(r'\n    def (_push_back_on_\w+)\(self', src):
        start = m.end()
        nxt = re.search(r'\n    def ', src[start:])
        body = src[start: start + nxt.start()] if nxt else src[start:]
        fm = re.search(r'f"\[(\w+)[ }]', body)
        if fm:
            name = fm.group(1)
            out[name] = f"[{name} "
    return out






# ---------------------------------------------------------------------------
# C. 主要な type は Unknown へ落とさず明示で扱われている（回帰止め）
# ---------------------------------------------------------------------------

_CORE_TYPES = ("notice", "ask", "error", "done", "iter_start", "token", "think",
               "tool_call", "tool_result")


