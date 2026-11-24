"""記憶（作業ログ・分かったことの帳面・失敗と直し方・Construct）を走行につなぐ窓口。

中身は既存の部品（laps/oc/worklog.py・findings.py・lessons.py・construct.py）で、置き場も今までと同じ:
    <作業場>/.blackwall/WORKLOG.md     走行の終わりと長い走行の途中に書く。新しい会話の最初に読む
    <作業場>/.blackwall/FINDINGS.md    要約のたびに積み足す。要約の後と新しい会話の最初に読む
    <作業場>/.blackwall/history/       要約の前の全文。モデルが必要なときに読む
    <home>/lessons.jsonl               同じ失敗を越えた手。同じ型の失敗のときに道具の結果へ添える
    <home>/construct.db                手掛かりで思い出す記憶。思い出すのはモデル（Construct の道具）

走行（loop.py）から見える口はこのクラスだけ。記憶の不具合で走行を落とさない（どの口も失敗したら何もしない）。
読み手が要るのは道具の結果か、依頼に添える一言だけで、前置きは変えない（変えると推論機の控えが全部効かなくなる）。"""
from __future__ import annotations

import functools
import os
import re
import time
from typing import Any, Callable

_EXIT_LINE = re.compile(r"\n*<shell_metadata>.*?</shell_metadata>\s*$|\n+Exit code: -?\d+\s*$", re.S)
_CACHE_PART = {".pytest_cache", "__pycache__", ".ruff_cache", ".mypy_cache", "node_modules", ".git", ".venv", "venv", ".blackwall", ".laps", ".cache", ".next"}


def _is_cache(path: str) -> bool:
    """道具が勝手に作る物（pytest の控えなど）。「新しいファイル」と数えない。"""
    return bool(_CACHE_PART & set(str(path).replace("\\", "/").split("/")))
PROGRESS_EVERY = 300.0             # 長い走行の途中で作業ログを更新する間隔（途中で止められても記録が残るように）


def _quiet(default: Any):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, *a, **k):
            try:
                return fn(self, *a, **k)
            except Exception as e:
                self.count("memory_error")
                self.last_error = f"{fn.__name__}: {type(e).__name__}: {e}"
                return default
        return inner
    return wrap


class Memory:
    def __init__(self, workspace: str, session: str, count: Callable[[str, int], None]) -> None:
        self.ws = workspace
        self.sid = session
        self._count = count
        self.last_error = ""
        self.started = time.time()
        self.run_id = f"{session}-{int(self.started)}"
        self.lessons: Any = None
        self.snap: dict | None = None
        self.changes: dict[str, set] = {"created": set(), "modified": set(), "deleted": set()}
        self.recite_at = 0
        self.recite_snap: dict | None = None
        self.progress_at = time.monotonic()
        self.gated: set[int] = set()
        self.shown: dict[int, set] = {}
        self.auto_ids: set[int] = set()     # 依頼の始めに自動で見せた記憶（開いたら construct_auto_opened を数える）
        self.opened: dict[int, tuple[set, int]] = {}
        self.calls: list[tuple[int, str, bool]] = []
        self.n = 0
        self.last_step: list[str] = []

    def count(self, key: str, n: int = 1) -> None:
        self._count(key, n)

    # ── 走行の始め ─────────────────────────
    @_quiet("")
    def begin(self, request: str, *, new_conversation: bool, last_action: str = "") -> str:
        """始めの支度。新しい会話なら、依頼に添える作業ログと帳面を返す。"""
        from ..oc import construct, findings, lessons, worklog
        self.lessons = lessons.RunState()
        if worklog.enabled():
            self.snap = worklog.snapshot(self.ws)
            if self.snap is None:
                self.count("ws_disabled")
        construct.set_current(self.sid, self.ws)
        if construct.enabled():
            if not new_conversation:
                self.correction(request, request, last_action)
            for k, v in construct.ingest(self.ws).items():
                if v:
                    self.count(f"construct_ingested:{k}", v)
        parts: list[str] = []
        if new_conversation:
            if worklog.enabled():
                wl = worklog.context_block(self.ws)
                if wl:
                    parts.append(wl)
                    self.count("worklog_loaded")
            if findings.enabled():
                fd = findings.context_block(self.ws, findings.CTX_CHARS_START, session_id=self.sid)
                if fd:
                    parts.append(fd)
                    self.count("findings_loaded")
        # 依頼の始めに、強く当たる記憶を断片で添える（2026-11-20・続きの依頼でも・添えるのは依頼の後ろ＝前の文字列は変えない）
        blk, ids = construct.start_block(request, self.ws, context=parts, session=self.sid)
        if blk:
            parts.append(blk)
            self.count("construct_auto_shown", len(ids))
            for i in ids:
                self.shown.setdefault(i, construct.cue_terms(request))
                self.auto_ids.add(i)
        return "\n\n".join(parts)

    @_quiet(None)
    def correction(self, text: str, request: str, last_action: str = "") -> None:
        """利用者の訂正を覚える（「違う、そうじゃなくて」・訂正の言い方のときだけ）。"""
        from ..oc import construct
        if not (construct.enabled() and construct.is_correction(text)):
            return
        iid, repeated = construct.note_correction(text, folder=self.ws, request=request, action=last_action, session=self.sid)
        if iid:
            self.count("construct_correction")
            if repeated:
                self.count("construct_correction_repeat")

    # ── 道具 ──────────────────────────────
    @_quiet("")
    def gate(self, name: str, inp: dict) -> str:
        """前に利用者に訂正された操作をまたしようとしたら、一度だけ止めて理由を返す。"""
        from ..oc import construct
        if not construct.enabled():
            return ""
        h = construct.correction_for(name, inp, folder=self.ws, session=self.sid)
        if h is None or h.id in self.gated:
            return ""
        self.gated.add(h.id)
        self.count("construct_correction_gate")
        return construct.correction_gate_message(h)

    @_quiet("")
    def after_tool(self, name: str, inp: dict, ok: bool, text: str) -> str:
        """道具 1 回ぶんを見て、結果に添える一言を返す（同じ失敗の前回の直し方など）。"""
        from ..oc import construct, evidence, lessons
        self.n += 1
        note = ""
        text = _EXIT_LINE.sub("", text)    # 末尾の終了コードの行を失敗の型にしない（全部の失敗が同じ型になる）
        if self.lessons is not None and lessons.enabled():
            sig, pick = ("", "") if ok else evidence.failure_signature(name, text)
            note = lessons.observe(self.lessons, name, evidence._attempt(name, inp), inp, ok, text, sig, pick, True) or ""
            if "earlier run hit this same error" in note:
                self.count("lesson_recalled")
            if "search the web for the error" in note:
                self.count("lesson_web_nudge")
        if construct.enabled():
            if name == "Construct":
                self._watch_recall(inp, ok, text)
            elif len(self.calls) < 400:
                self.calls.append((self.n, construct.call_text(name, inp)[:600], ok))
        return note

    def _watch_recall(self, inp: dict, ok: bool, text: str) -> None:
        """思い出した記憶が効いたかを走行の終わりに返すために、開いた物を控える。"""
        from ..oc import construct
        if inp.get("open") not in (None, ""):
            self.count("construct_open")
            iid = construct._as_id(inp.get("open"))
            if iid is not None and iid in self.auto_ids:
                self.count("construct_auto_opened")
            if ok and iid is not None and text.startswith("Memory #"):
                self.opened.setdefault(iid, (self.shown.get(iid, set()), self.n))
            return
        self.count("construct_recall")
        q = construct.cue_terms(str(inp.get("query") or ""))
        for m in re.finditer(r"^#(\d+) \[", text, re.M):
            self.shown.setdefault(int(m.group(1)), q)

    @_quiet("")
    def after_step(self, changed: bool) -> str:
        """手番の最後に、作業フォルダの変化を 1 行で返す（2026-10-12 オーナー「ツール実行時の一番最後に毎回差分記録」）。"""
        from ..oc import worklog
        if not changed or self.snap is None:
            return ""
        new = worklog.snapshot(self.ws)
        if new is None:
            self.snap = None
            self.count("ws_disabled")
            return ""
        created, modified, deleted = worklog.diff(self.snap, new)
        self.snap = new
        self.last_step = [f for f in [*created, *modified] if not _is_cache(f)]
        ch = self.changes
        for f in created:
            if f in ch["deleted"]:
                ch["deleted"].discard(f)
                ch["modified"].add(f)
            else:
                ch["created"].add(f)
        for f in modified:
            if f not in ch["created"]:
                ch["modified"].add(f)
        for f in deleted:
            if f in ch["created"]:
                ch["created"].discard(f)
            else:
                ch["modified"].discard(f)
                ch["deleted"].add(f)
        return worklog.change_note(created, modified, deleted)

    # ── 走行の途中 ─────────────────────────
    @_quiet("")
    def recite(self, iters: int, goal: str, todos: list[dict]) -> str:
        """長い走行で目的を見失わないように、20 手番ごとに依頼と予定を思い出させる。"""
        from ..oc import findings
        if not findings.enabled() or iters - self.recite_at < findings.RECITE_EVERY:
            return ""
        note = findings.recite_note(goal, todos, self.ws, prev=self.recite_snap)
        if note:
            self.recite_at = iters
            self.recite_snap = findings.recite_snapshot(todos, self.ws)
            self.count("recite")
        return note or ""

    @_quiet("")
    def compacted(self, summary: str, head: list[dict], n: int, todos: list[dict], iters: int) -> str:
        """要約した。帳面に積み足し、要約の前の全文を置き、要約の後に添える物を返す。"""
        from ..oc import findings, worklog
        hist = ""
        if findings.enabled():
            added = findings.absorb(self.ws, summary, session_id=self.sid)
            self.count("findings_added", sum(added))
            hist = findings.archive(self.ws, self.sid, head, n)
            if hist:
                self.count("history_archived")
        parts: list[str] = []
        if worklog.enabled():
            wl = worklog.context_block(self.ws)
            if wl:
                parts.append(wl)
        td = findings.todo_block(todos)
        if td:
            parts.append(td)
            self.count("todos_after_compaction")
        if findings.enabled():
            fd = findings.context_block(self.ws, session_id=self.sid)
            if fd:
                parts.append(fd)
        if hist:
            parts.append(f"The full text of the conversation before this summary (your messages, tool calls and tool results, "
                         f"without thinking) is saved in {hist}. If you need an exact detail the summary does not have, Grep or "
                         "Read that file instead of redoing the work.")
        self.recite_at = iters
        return "\n\n".join(parts)

    @_quiet(False)
    def due(self) -> bool:
        """途中の記録を書く頃か。"""
        try:
            every = float(os.environ.get("LAPS_WORKLOG_EVERY") or PROGRESS_EVERY)
        except ValueError:
            every = PROGRESS_EVERY
        if every <= 0 or time.monotonic() - self.progress_at < every:
            return False
        self.progress_at = time.monotonic()
        return True

    # ── 走行の終わり ───────────────────────
    @_quiet(None)
    def record(self, *, request: str, outcome: str, final: str, steps: int, files: list[str], tools: dict[str, int],
               interjections: list[str], in_progress: bool = False) -> None:
        """作業ログに 1 件書く。道具を使わなかった走行（ただの質問）は書かない。"""
        from ..oc import worklog
        if not worklog.enabled() or not tools:
            return
        e = worklog.entry(request=request, outcome=outcome, steps=steps, secs=time.time() - self.started, files=files,
                          verdict=None, interjections=interjections, answer=final or "", tools=tools, run_id=self.run_id,
                          in_progress=in_progress, changes=self.changes)
        struct = worklog.structure(self.ws, self.snap) if self.snap is not None else None
        if worklog.record(self.ws, e, run_id=self.run_id, struct=struct):
            self.count("worklog_progress" if in_progress else "worklog_written")

    @_quiet(None)
    def close(self, *, final: str, ok: bool, todos: list[dict], request: str) -> None:
        """思い出した記憶が効いたかを返し、予定表の残りを「やり残し」として覚える。"""
        from ..oc import construct
        if not construct.enabled():
            return
        if self.opened:
            entries = []
            for iid, (trig, at) in self.opened.items():
                after = "\n".join(t for c, t, _ in self.calls if c > at) + "\n" + (final or "")
                failed = "\n".join(t for c, t, good in self.calls if c > at and not good)
                entries.append((iid, trig, after, failed))
            for k, v in construct.feedback(entries, ok).items():
                if v:
                    self.count(f"construct_{k}", v)
        left = [str(t.get("content") or "") for t in todos if t.get("status") in ("pending", "in_progress")]
        done = [str(t.get("content") or "") for t in todos if t.get("status") == "completed"]
        if left or done:
            added, closed = construct.note_intentions(left, done, folder=self.ws, request=request, session=self.sid)
            if added:
                self.count("construct_intentions", added)
            if closed:
                self.count("construct_intentions_done", closed)

    def new_files(self) -> list[str]:
        return sorted(f for f in self.changes["created"] if not _is_cache(f))

    def changed(self) -> bool:
        return any(self.changes.values())
