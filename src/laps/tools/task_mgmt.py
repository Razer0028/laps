"""TaskList / TaskGet / TaskUpdate / TaskStop tools — Claude Code parity.

These manage *user-facing* tasks (planning units), distinct from `Task` which
spawns sub-agents. Persisted to ~/.blackwall/sessions/<sid>/tasks.json.

Schema per task:
  {"id": "1", "subject": str, "description": str, "status": "pending"|"in_progress"|"completed"|"deleted"}
"""
from __future__ import annotations
from .. import paths as _lp
import json
import os
from pathlib import Path

from . import ToolSpec, ToolResult
from .atomic import write_text_atomic


class _TasksUnavailable(Exception):
    """記帳できない状態。**「OK」と言わずにこれを投げる。**"""


def _tasks_path() -> Path:
    """課題ファイルの場所。session が無ければ投げる（黙って None を返さない）。

    ★2026-08-31（レビュー E7・再現済み）: ここは None を返し、`_save` は
      `if p:` で**何もせずに戻り**、`_create` は `OK: task #1 created` と答えていた。
      実測（LAPS_SESSION_ID 未設定）:
          TaskCreate → "OK: task #1 created"
          TaskList   → "(no tasks)"
          TaskGet    → "Error: task #1 not found"
      ＝**作った直後に消える課題。** モデルは「作った」と信じて先へ進み、
      次の呼び出しで矛盾した答えを受け取る。同じ現場の TodoWrite は最初から
      `Error: no active session (LAPS_SESSION_ID unset)` と正直に断っており、
      ここだけが揃っていなかった。
    """
    sid = os.environ.get("LAPS_SESSION_ID")
    home = str(_lp.home_dir())
    if not sid:
        raise _TasksUnavailable(
            "Error: no active session (LAPS_SESSION_ID unset) — 課題を保存できません"
            "（保存できないまま「作成しました」とは言いません）。"
            "セッションの中から呼んでください。")
    p = Path(home) / "sessions" / sid / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> list[dict]:
    p = _tasks_path()
    if not p.exists():
        return []
    try:
        # ★**符号化を明示する**（2026-09-02・`_save` との食い違い）。
        #   `_save` は `write_text_atomic`（既定 utf-8）で**必ず UTF-8 で書く**のに、
        #   ここは `read_text()`＝**その機械の既定**で読んでいた。UTF-8 が既定でない
        #   環境（日本語 Windows の cp932。この製品は Windows 版を出荷している）では、
        #   件名に日本語が 1 文字でも入った瞬間に読み直せなくなり、
        #   下の `_TasksUnavailable` に落ちて**課題表ごと使えなくなる**。
        #   ＝同じファイルの読みと書きが別の符号化を使っていた。
        #   再現（Linux でも同じ形が作れる。Windows の cp932 に相当）:
        #     LC_ALL=C python -X utf8=0 -c "pathlib.Path('tasks.json').read_text()"
        #       → UnicodeDecodeError: 'ascii' codec can't decode byte 0xe6
        items = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # ★空リストを返してはいけない。呼び出し側は「課題は 1 件も無い」と読み、
        #   次の TaskCreate が #1 から採番して**壊れたファイルを上書きする**
        #   （＝読めなくなっただけの課題表を、確実に消す）。
        raise _TasksUnavailable(
            f"Error: 課題ファイルを読めません（{p}: {e}）。中身を確かめてください。"
            "（読めないまま上書きすると、記録が消えます）") from e
    if not isinstance(items, list):
        raise _TasksUnavailable(f"Error: 課題ファイルの形が壊れています（{p}）。")
    return items


def _save(items: list[dict]) -> None:
    # 不可分に書く（E4 と同じ理由: 途中で失敗すると課題表ごと失う）。
    write_text_atomic(_tasks_path(), json.dumps(items, ensure_ascii=False, indent=2))


def _guard(fn):
    """記帳できない状態を、**成功ではなくエラーとして**返す。"""
    import functools

    @functools.wraps(fn)
    def wrapper(inp: dict) -> str:
        try:
            return fn(inp)
        except _TasksUnavailable as e:
            return ToolResult(str(e), ok=False, kind="error")
    return wrapper


#: 状態の正規化（2026-09-02）。**下流は文字列を厳密一致で見る。**
#:
#: ★なぜ要るか（実測した消え方）: `_update` は `if status: t["status"] = status` と
#:   受け取った値をそのまま書いており、enum の外の値も黙って通っていた。ところが
#:   ・`_list_tasks` は `status == "deleted"` の**完全一致**でしか隠さない
#:   ・`agent.py:1411` は `("pending","in_progress")` と `"completed"` の
#:     **完全一致**で「残っている予定／片付いた予定」を作り、文脈が切れたときに運ぶ
#:   ので、モデルが "done" や "in-progress"（TodoWrite 側では 08-23 に実測された
#:   書き方）を渡すと、その課題は**どちらの一覧にも載らない**——
#:   消えたわけではないのに、覚え書きから静かに落ちる。
#: ★TodoWrite は同じ問題を `_DONE_WORDS` で直している。**片方だけ直っていた。**
_STATUS_ALIASES = {
    "pending": "pending", "todo": "pending", "open": "pending", "new": "pending",
    "not_started": "pending", "not-started": "pending", "waiting": "pending",
    "in_progress": "in_progress", "in-progress": "in_progress",
    "inprogress": "in_progress", "active": "in_progress", "started": "in_progress",
    "doing": "in_progress", "wip": "in_progress",
    "completed": "completed", "complete": "completed", "done": "completed",
    "finished": "completed", "closed": "completed", "resolved": "completed",
    "deleted": "deleted", "cancelled": "deleted", "canceled": "deleted",
    "dropped": "deleted", "abandoned": "deleted",
}
_STATUS_CANON = ("pending", "in_progress", "completed", "deleted")


def _canon_status(status) -> tuple[str | None, str | None]:
    """(正規化した状態, 断り文) を返す。読めない値は**黙って書かない**。"""
    key = str(status or "").strip().lower().replace(" ", "_")
    if not key:
        return None, None
    hit = _STATUS_ALIASES.get(key)
    if hit:
        return hit, None
    return None, (f"Error: status {status!r} は使える状態ではありません。"
                  f"使えるのは {', '.join(_STATUS_CANON)}"
                  "（done / in-progress のような書き方は読み替えます）。")


def _next_id(items: list[dict]) -> str:
    nums = [int(t["id"]) for t in items if str(t.get("id", "")).isdigit()]
    return str(max(nums) + 1 if nums else 1)


# ------- TaskCreate (lightweight, not the same as `Task` sub-agent spawn) -------

@_guard
def _create(inp: dict) -> str:
    subject = inp.get("subject")
    desc = inp.get("description", "")
    if not subject:
        return "Error: missing 'subject'"
    items = _load()
    new = {"id": _next_id(items), "subject": subject, "description": desc, "status": "pending"}
    items.append(new)
    _save(items)
    return f"OK: task #{new['id']} created"


@_guard
def _list_tasks(_inp: dict) -> str:
    items = _load()
    if not items:
        return "(no tasks)"
    lines = []
    for t in items:
        if t.get("status") == "deleted":
            continue
        # ★`t['id']` は KeyError で落ちうる（一覧が壊れると TaskList ごと死ぬ）。
        #   壊れた 1 行のために一覧全部が読めなくなるほうが害が大きい。
        lines.append(f"#{str(t.get('id','?')):>3}  "
                     f"[{str(t.get('status','pending')):<11}] {t.get('subject','')}")
    return "\n".join(lines) or "(no active tasks)"


@_guard
def _get(inp: dict) -> str:
    tid = str(inp.get("task_id") or "")
    items = _load()
    for t in items:
        if str(t.get("id")) == tid:
            return json.dumps(t, ensure_ascii=False, indent=2)
    return f"Error: task #{tid} not found"


@_guard
def _update(inp: dict) -> str:
    tid = str(inp.get("task_id") or "")
    status = inp.get("status")
    subject = inp.get("subject")
    desc = inp.get("description")
    if status is not None:
        status, _bad = _canon_status(status)
        if _bad:
            return ToolResult(_bad, ok=False, kind="error")
    items = _load()
    for t in items:
        if str(t.get("id")) == tid:
            # ★変化なしを、変化なしと言う（TodoWrite と同じ理由）。
            #   実機 v0.1.53: TaskUpdate→TaskStop→TaskList→TaskGet の4周期が8巡し、
            #   道具33回を捨てた。どの呼び出しも「OK」を返すので、モデルには
            #   進んでいないことが分からない。**帳簿の道具が停滞の燃料になっていた。**
            before = (t.get("status"), t.get("subject"), t.get("description"))
            if status: t["status"] = status
            if subject: t["subject"] = subject
            if desc is not None: t["description"] = desc
            if (t.get("status"), t.get("subject"), t.get("description")) == before:
                return ToolResult(
                    f"変化なし: task #{tid} は既にその状態です（{t.get('status','')}）。"
                    f"同じ更新を繰り返さないこと。次の作業に進むか、答えを書くこと。",
                    ok=True, kind="empty")
            _save(items)
            return ToolResult(f"OK: task #{tid} updated ({t.get('status','')})", ok=True, kind="ok")
    return f"Error: task #{tid} not found"


@_guard
def _stop(inp: dict) -> str:
    """Mark a task as completed (or deleted)."""
    tid = str(inp.get("task_id") or "")
    items = _load()
    for t in items:
        if str(t.get("id")) == tid:
            if t.get("status") == "deleted":
                return ToolResult(
                    f"変化なし: task #{tid} は既に停止済みです。"
                    f"同じ停止を繰り返さないこと。次の作業に進むか、答えを書くこと。",
                    ok=True, kind="empty")
            t["status"] = "deleted"
            _save(items)
            return ToolResult(f"OK: task #{tid} stopped", ok=True, kind="ok")
    return f"Error: task #{tid} not found"


SPECS = [
    ToolSpec(
        name="TaskCreate",
        description=(
            "Create a new planning task in the current session — use it to plan work "
            "that still needs doing. "
            # ★2026-09-04（W4 実測）: 実機で TaskCreate 21 回のうち 2 回
            #   （同一走行）が、作業が終わった**後**に「〜完了」という件名だけを
            #   作る記帳だった。完了の記録は不要。作業の締めに TaskCreate を作らない
            #   ——完了報告は答えの本文で言えばよい。
            "★完了の記録は不要。作業の締めに TaskCreate を作らない（件名が「〜完了」"
            "「〜しました」になるだけの記帳は無意味）。完了はここに書かず、答えの本文で言うこと。"
        ),
        schema={"type": "object", "properties": {
            "subject": {"type": "string"},
            "description": {"type": "string"},
        }, "required": ["subject"]},
        handler=_create,
        bookkeeping=True,
    ),
    ToolSpec(
        name="TaskList",
        description="List all tasks in the current session.",
        schema={"type": "object", "properties": {}},
        handler=_list_tasks,
        bookkeeping=True,
    ),
    ToolSpec(
        name="TaskGet",
        description="Fetch a single task by id.",
        schema={"type": "object", "properties": {"task_id": {"type": ["integer", "string"]}}, "required": ["task_id"]},
        handler=_get,
        bookkeeping=True,
    ),
    ToolSpec(
        name="TaskUpdate",
        description="Update status/subject/description of a task by id.",
        schema={"type": "object", "properties": {
            "task_id": {"type": ["integer", "string"]},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
            "subject": {"type": "string"},
            "description": {"type": "string"},
        }, "required": ["task_id"]},
        handler=_update,
        bookkeeping=True,
    ),
    ToolSpec(
        name="TaskStop",
        description="Mark a task as deleted (soft remove).",
        schema={"type": "object", "properties": {"task_id": {"type": ["integer", "string"]}}, "required": ["task_id"]},
        handler=_stop,
        bookkeeping=True,
    ),
]
