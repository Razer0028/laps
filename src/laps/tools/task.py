"""Task tool: spawn a depth-limited sub-agent, optionally specialized.

Subagent types (subagent_type):
  - general-purpose: full tool set (default)
  - Explore: 調査向け — Bash/Read/Grep/Glob/WebFetch。
    ★「read-only」とは書かない（2026-08-30）。Bash が入っている以上、
      札だけ read-only にしても実効は違う。**実体に合わせて名乗る**。
      道具を減らす方向で辻褄を合わせない（オーナー方針: 制限を増やさない）。
  - Plan: planning-only — Read/Grep/Glob, no Write/Edit/Bash
  - Coder: write-focused — Bash/Read/Write/Edit/Grep/Glob
  - Research: web-focused — Read/Grep/Glob/WebFetch/WebSearch
  - Recon: 偵察向け（セキュリティモード）— Read/Grep/Glob/WebFetch/WebSearch/Bash。読む・調べることに特化。
  - Reporter: レポート作成向け（セキュリティモード）— Read/Write/Glob/Grep。構造化された報告書を書く。
"""
from __future__ import annotations
from laps import _envcompat
import os

from . import ToolSpec, ToolResult


def _find_parent_on_event():
    """委譲を実行している**親 Agent** の `on_event`。

    `Agent._exec_tool` が handler を呼ぶ間だけ `tools.CURRENT_AGENT` が立つ（agent.py）。
    ★以前はスタック歩き（`sys._getframe` で `self` が Agent のフレームを探す）だった。
      担当がこのファイルだけで agent.py を触れなかったための回り道。受け口を正規化した。
    見つからない/親の on_event が None のときは None＝**従来どおり無音の子**。
    """
    try:
        from . import CURRENT_AGENT
        parent = CURRENT_AGENT.get()
    except Exception:
        return None
    if parent is None:
        return None
    return getattr(parent, "on_event", None)


class _ChildRelay:
    """子 Agent の `on_event` として渡す中継。子の事象を **親の on_event へ**
    「子: 」の印つきで流し、同時に受領書のための集計を取る。

    ★親へは **`notice`（kind=subagent）** の形でだけ流す（生の tool_start/tool_end で
      流さない）。理由 2 つ:
        1. 親側 serve の `_on_event` は `tool_start` で `tool_counts[name]` を増やす
           ＝子の道具が**親の道具として二重計上**され、trace にも親の呼び出しとして
           混ざる（記録が汚れる）。
        2. `iter_start`/`token`/`assistant` は親の streaming 状態（streamed/sent_body）
           を**リセット**する＝道具の途中で親の本文表示が壊れる。
      `notice` は serve/phases が素通しで記録し、render は無視する（未知型）＝
      **どの消費者でも安全**。これが「子: の印つきで画面と記録に届く」の実体。
    """

    def __init__(self, parent_on_event, *, label: str = ""):
        self._parent = parent_on_event
        self._label = label            # 例: "type=Coder"
        self.tool_calls = 0            # 子が呼んだ道具の数
        self.tool_fails = 0            # うち失敗した数
        self.fail_lines: list[str] = []  # 失敗の 1 行要約（受領書用）

    def __call__(self, etype: str, payload: dict) -> None:
        # --- 集計（親 on_event が None でも取る＝受領書は常に出す） ---
        msg = None
        if etype == "tool_start":
            self.tool_calls += 1
            msg = f"▶ {payload.get('name', '?')}"
        elif etype == "tool_end":
            ok = payload.get("ok", True)
            name = str(payload.get("name", "?"))
            if not ok:
                self.tool_fails += 1
                snip = str(payload.get("result", "") or payload.get("summary", ""))
                snip = snip.strip().replace("\n", " ")[:200]
                self.fail_lines.append(f"{name}: {snip}" if snip else name)
                msg = f"✗ {name} 失敗: {snip}" if snip else f"✗ {name} 失敗"
            else:
                msg = f"✓ {name}"
        elif etype == "notice":
            # 子の engine_retry / context_exhausted / context_reset 等の一行。
            m = payload.get("message") or payload.get("text")
            if m:
                msg = str(m)[:300]
        # token / iter_start / assistant / completion 等は流さない（上の理由）。

        # --- 親へ「子: 」の印つきで流す（notice に統一） ---
        if self._parent is None or msg is None:
            return
        try:
            self._parent("notice", {
                "kind": "subagent",
                "message": "子: " + msg,
                "child_event": etype,
                "child_label": self._label,
            })
        except Exception:
            # 画面/記録の不具合で走行を落とさない（events.py と同じ作法）。
            pass

    def receipt(self) -> str:
        """要約でなく**受領書**: 何回道具を呼び・何が落ちたか。"""
        lines = [f"道具 呼び出し {self.tool_calls} 回 / 失敗 {self.tool_fails} 回"]
        if self.fail_lines:
            lines.append("落ちた道具:")
            for fl in self.fail_lines[:10]:
                lines.append(f"  - {fl}")
            if len(self.fail_lines) > 10:
                lines.append(f"  … 他 {len(self.fail_lines) - 10} 件")
        return "\n".join(lines)


_SUBAGENT_TOOLS = {
    "general-purpose": None,  # all
    "Explore": {"Bash", "Read", "Grep", "Glob", "WebFetch"},
    "Plan": {"Read", "Grep", "Glob", "TodoWrite"},
    "Coder": {"Bash", "Read", "Write", "Edit", "Grep", "Glob"},
    "Research": {"Read", "Grep", "Glob", "WebFetch", "WebSearch"},
    "Recon": {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "Bash"},
    "Reporter": {"Read", "Write", "Glob", "Grep"},
}


#: `Agent.outcome`（agent.py の run() が付ける）→ 人が読む1行。
#: ★2026-09-04（A4-a・OSS 由来: OpenHands の TaskObservation.status）:
#:   これまで `_run()` は子の `result` 文字列だけを親へ返しており、`sub.outcome`
#:   （agent.py:3472 以降で "accepted"/"max_iters"/"loop_detected" 等が入る）と
#:   `sub.state.iter`（実際に回った反復数）は**読めるのに使われていなかった**。
#:   `tools/__init__.py:23 tool_failed()` は前方一致の語彙表しか見ないので、
#:   この文字列は構造上ほぼ必ず「成功」と判定される——子が
#:   「（推論器が同じ文の繰り返しに入り、答えになりませんでした）」のまま
#:   ok=True で記録されていた実例（8走行・委譲34回の調査）。
_OUTCOME_LABELS = {
    "accepted": "完了",
    "accepted_after_pushback": "完了（差し戻し後）",
    "max_iters": "反復の上限で打ち切り",
    "budget_exhausted": "予算切れで打ち切り",
    "user_stop": "利用者の中断で打ち切り",
    "loop_detected": "ループ検知で打ち切り",
    "backend_error": "推論器エラーで打ち切り",
    "declined": "モデルが応答を断った",
    "context_exhausted": "文脈切れで打ち切り",
    "context_overflow": "文脈超過で打ち切り",
}

#: 子の走行が「失敗」寄りと見なす outcome だけ ok=False にする。
#: ★max_iters/budget_exhausted/user_stop は「打ち切り」であって「失敗」ではない
#:   ——予算が尽きるまでに実際の作業が進んでいることがある（agent.py の
#:   `_wrap_up` と同じ立場: 予算切れは失敗ではない）。ここを ok=False にすると、
#:   実際に手を動かした委譲まで一律「道具が失敗した」記録になる。
_ERROR_OUTCOMES = frozenset({
    "loop_detected", "backend_error", "context_exhausted", "context_overflow", "declined",
})


def _status_line(outcome: str | None, iters: int | None) -> str:
    """要約ではなく、outcome をそのまま人の読める1行にしただけ（捏造しない）。"""
    if outcome is None:
        label = "不明（子から実行結果を受け取れませんでした）"
    else:
        label = _OUTCOME_LABELS.get(str(outcome), f"不明な状態（{outcome}）")
    it_txt = f"（反復 {iters} 回）" if isinstance(iters, int) and iters >= 0 else ""
    return f"状態: {label}{it_txt}"


def _run(inp: dict) -> str:
    """★2026-11-19 旧い中核（oc/loop.py の OcAgent）を外した。子のエージェントは新しい中核の Task
    （engine/tools/task.py）が受け持つ。ここは旧い道具の一覧（oc/tools）から呼ばれたときに断るだけ。"""
    return "Error: this Task is no longer available (the old core was removed). Use the Task tool of the current engine."


SPEC = ToolSpec(
    name="Task",
    description=(
        "Delegate a focused sub-task to a depth-limited sub-agent. Single level only. "
        "Optional subagent_type: general-purpose | Explore (調査向け) | Plan | Coder | Research | Recon (偵察) | Reporter (レポート)。"
    ),
    schema={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "prompt": {"type": "string", "description": "Self-contained instructions for the sub-agent"},
            "subagent_type": {
                "type": "string",
                "enum": list(_SUBAGENT_TOOLS),
                "description": "Specialized type restricts which tools the sub-agent can use",
            },
        },
        "required": ["prompt"],
    },
    executes=True,
    handler=_run,
)
