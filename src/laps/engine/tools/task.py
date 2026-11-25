"""子エージェント（手本: opencode の tool/task.ts）。

子の推論機が別にあれば（LAPS_CHILD_BACKEND・engine/childengine.py・2026-11-25）、子はそこで動き、親の席は触られない。
無ければ親と同じ推論機の席を使い、子の前後で親の席を保存して戻す（engine/slots.py）。自動では呼ばない。モデルが切り出したいときだけ使う。"""
from __future__ import annotations

from . import Ctx, Result, Tool, fail


def task(inp: dict, ctx: Ctx) -> Result:
    prompt = str(inp.get("prompt") or "").strip()
    if not prompt:
        return fail("prompt is required")
    desc = str(inp.get("description") or prompt[:60]).strip()
    if ctx.spawn is None:
        return fail("Sub-agents are not available here.", head=desc)
    kind = str(inp.get("subagent_type") or "explore")
    out = ctx.spawn(prompt, kind, desc)
    return Result(out or "(the sub-agent returned nothing)", kind="ok" if out else "empty", head=desc, tail=kind)


TASK = Tool(
    "Task",
    "Hand a wide search, a long investigation or a separate job to a sub-agent with its own context. It returns one final "
    "message that the user does not see; only that message enters your context, so delegating keeps your context small. "
    "Use Read, Grep or Glob directly when you know where to look, and do small changes yourself. Sub-agents run one at a "
    "time. subagent_type: explore (read-only, default) or general (can edit).",
    {"description": {"type": "string", "description": "A few words"},
     "prompt": {"type": "string", "description": "The full brief. The sub-agent knows nothing else, so include: the goal; what you "
                "already know or checked; where to look and what is out of scope; when to stop; what to return (e.g. paths "
                "with line numbers)."},
     "subagent_type": {"type": "string", "enum": ["explore", "general"]}},
    task, required=("description", "prompt"))
