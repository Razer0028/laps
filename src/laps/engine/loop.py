"""1 走行。手本: opencode の session/prompt.ts（loop）と session/processor.ts。

    読む → 生成 → 道具 → 生成 → … → 道具を呼ばない手番で終わる

差し戻しは持たない。例外は 3 つだけ: 繰り返しで切った手番・空の手番・終わりの確かめ（endgate.py）。"""
from __future__ import annotations

from .. import paths as _lp
import base64
import json
import mimetypes
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import compact, endgate
from .aids import Aids
from .finish import Finish
from ..oc import qwencall
from .llm import LLM, BackendError, Cancelled, ContextOverflow, Turn, _env_float, estimate_tokens, family, salvage_calls
from .repeat import similar
from .session import Transcript, _clean, clean_json
from .tools import Ctx, Result, Tool, adapt_names, coerce, fail
from .tools.ext import LISTED
from .tools.files import _walk
from .tools.misc import open_todos
from .tools.shell import read_only, running_job

#: 道具の呼び出しに添える利用者への一文（2026-10-30 オーナー「タスク状況を報告しながら進める」）。
#: 説明は短く保つ（道具の数だけ毎回読むので、長いと 2,000 トークンになった・0.1.614）。
PROGRESS_FIELD = {"type": "string", "description": "One sentence to the user: what you learned, what this does next."}
try:
    UI_RESULT_MAX = int(os.environ.get("LAPS_SERVE_MAX_RESULT") or 64 * 1024)
except ValueError:
    UI_RESULT_MAX = 64 * 1024
INJECT_ESCALATE = 30.0             # 普通の一言を待たせる上限（秒）。これを過ぎても道具が動いていたら止めて読む
_NOT_A_TOOL = {"推論機", "要約", "背景の仕事"}
DOOM_BLOCK = 3                     # 同じ呼び出しがこの回数続いたら走らせずに返す（opencode と同じ 3）・★既定で切る（LAPS_DOOM_LOOP）
DOOM_STOP = 6
MAX_CUTS = 6                           # 旧い中核と同じ（2026-10-12 オーナー「二回同じ思考が続いたら次に進めるように」）
#: これより短い依頼（「続けて」「結果は」）は続きとして扱い、元の依頼を前の発言から探す（旧い中核と同じ）。
TASK_MIN_CHARS = 20
#: 走行の途中に届いた利用者の一言を包む枠の書き出し。
INTERJECTION_HEAD = "[The user sent this while you were working]"
MAX_EMPTY = 2
MAX_UNREAD = 3                 # 崩れた道具の呼び出しを書き直させる回数（続けて）
TRUNCATED_CALL = ("Your output hit the token limit while you were writing this call, so its arguments may be cut off. It was not run "
                  "and nothing was changed. Send it again in smaller pieces (for example write a shorter file, then add the rest with Edit).")
UNREAD_NOTE = ("[Harness] Your last tool call could not be read, so nothing ran: the function name was not one of your tools. "
               "Write the call again with the exact tool name and put each argument in its own parameter tag.")
FALLBACK_CTX = {"claude": 200_000}



def _max_call_id(raw: list) -> int:
    """記録に出てくる呼び出しの番号（c<N>）の一番大きい物。道具の結果の数ではなく番号で数える（2026-11-16 全体のレビュー:
    途中で落ちて結果が欠けると数が番号より小さくなり、次の走行が使用中の番号を使い回した）。"""
    n = sum(1 for m in raw if m.get("role") == "tool")
    for m in raw:
        ids = [str(tc.get("id") or "") for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]
        if m.get("role") == "tool":
            ids.append(str(m.get("tool_call_id") or ""))
        for i in ids:
            mm = re.fullmatch(r"c(\d+)", i)
            if mm:
                n = max(n, int(mm.group(1)))
    return n


def hooks_max_stop() -> int:
    from .hooks import MAX_STOP_BLOCKS
    return MAX_STOP_BLOCKS


def _flag(name: str) -> bool:
    """既定で切ってある上限を戻す合図（1 で戻す・2026-11-16 オーナー「その他の制限も全部取り払って」）。"""
    return (os.environ.get(name) or "0").strip().lower() not in ("0", "off", "false", "no", "")


def _limit(name: str) -> int:
    """既定 0＝上限なしの数（2026-11-16）。"""
    try:
        return max(0, int(float(os.environ.get(name) or 0)))
    except ValueError:
        return 0


def _env_int(name: str, default: int) -> int:
    """既定のある数。空なら既定・0 で切る。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return default


#: 子の文脈の目安（トークン）。越えたら一度まとめを促す（止めない）。
CHILD_CTX_SOFT = 60000
#: 子の答えがこの字数を越えたら、全文をファイルに置き、親には頭と置き場を返す。
CHILD_RESULT_MAX = 12000


def _tools_shape(schemas: list[dict]) -> str:
    """道具の一覧の形（説明の文を除いた全部）。名前・引数の名前と順番・型・選択肢・必須が同じなら同じ値。"""
    def strip(v: Any, under_props: bool = False) -> Any:
        if isinstance(v, dict):                # properties の中の鍵は引数の名前（Bash の description 引数を消さない）
            return {k: strip(x, k == "properties") for k, x in v.items() if under_props or k != "description"}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return json.dumps(strip(schemas), ensure_ascii=False)


def _first_kept_image(total: int, keep: int) -> int:
    """送る画像の先頭の番号。★窓を 1 枚ずつ動かさない（2026-11-03）。

    推論機の読み込みの控えは頭から一致する所までしか使えないので、古い画像を文字に替えるとそこから先を全部読み直す。
    1 枚ごとに動かすと画像を撮るたびに読み直しになる。keep 枚ぶん増えたときにまとめて動かす（残るのは keep〜keep*2-1 枚）。
    前回の結果を覚えなくても枚数だけで決まるので、同じ会話は毎回同じ形になる。"""
    return ((total - keep) // keep) * keep if total > keep else 0


#: 古い画像を文字に替えるのは、そこから後ろが短いときだけ（推定トークン）。替えると推論機はその画像から後ろを全部読み直すので、
#: 会話の初めの方の画像を替えるとほぼ頭から読み直しになる（2026-11-16 オーナー「Desktop を使った後の手番で急に最初から読み直す」・
#: 推論機 の記録: 写真の後に会話が 300 トークン縮み 17.2 万トークンを約 6 分読み直した）。遠い画像は画像のまま残す（1 枚 約 1,000 トークン）。
#: 要約の後はどのみち頭から読み直すので、そこでまとめて文字に替える。
IMAGE_SWAP_TAIL = 12000
IMAGE_TOKENS = 1000


class MemoryTranscript(Transcript):
    """子エージェントの記録（ファイルに残さない）。"""
    def __init__(self) -> None:           # noqa: D107 — 親の初期化（ファイルを開く）は呼ばない
        self.id = "child"
        self.raw = []

    def append(self, msg: dict) -> None:
        self.raw.append(msg)

    def write_meta(self, **meta: Any) -> None:
        pass

    def close(self) -> None:
        pass


@dataclass
class Limits:
    max_iters: int = 0
    max_seconds: float = 0.0
    max_tool_calls: int = 0


#: 下見の結果に添える一言。★下見は見るだけなので「走らせられなかった」と答えることがある。親は自分の権限を疑わなくてよい
#:（2026-10-31 課題集 longbuild: 親が ls と echo で確かめて 20 秒使った）。
_SCOUT_NOTE = ("(The harness asked a read-only helper this before your first step, so you start with the lay of the land. The helper "
               "could not run or change anything; you can. Read a file yourself before you change it.)")


#: これ以下のファイルしか無いフォルダでは下見をしない（前置きの地図と親の Read で足りる。2026-10-31 課題集 3 本: 下見は全体の 18〜38%＝19〜30 秒で、
#: 親は直後に同じファイルを読み直していた）。
SCOUT_SMALL_FOLDER = 20


#: 答えの後に足す一言の包み（Qwen 系の書式はこの形の user を利用者の発言と数えない）。_send_messages を見る。
_NOTE_OPEN, _NOTE_CLOSE = "<tool_response>\n", "\n</tool_response>"


#: 利用者が依頼を止めたとき、次の依頼の前に残す一言（手本: Codex の core/src/context/turn_aborted.rs）。
#: 止めても記録には何も残らず、次の依頼でモデルは途中で切れた作業を終わったものとして読んでいた（2026-11-11 Codex との突き合わせ）。
#: Ask の依頼を Agent の前置きと道具の一覧のまま走らせるときに、依頼の後ろへ足す一言と、断るときの文。
ASK_NOTE = ("[Harness] Ask mode, for the message above only: answer the question. You may look to find the answer (Read, Glob, Grep, "
            "WebSearch, WebFetch, AskUserQuestion, BashOutput, and Bash commands that only look). Change nothing: every "
            "other tool, and any command that changes files or state, is refused for this message. If the answer needs a change, "
            "say what you would do and tell the user to switch to Agent mode. Do not continue earlier work.")
ASK_REFUSED = ("Not run in Ask mode: {name} is not available for this message. In Ask mode you answer the question from what you can "
               "read (Read, Glob, Grep, WebSearch, WebFetch, Bash commands that only look). Answer now, or tell the user to "
               "switch to Agent mode to have it done.")


PLAN_NOTE = ("[Harness] Plan mode, for the message above only: do not change anything yet. Look first (Read, Glob, Grep, WebSearch, "
             "WebFetch, AskUserQuestion, BashOutput, and Bash commands that only look), then reply with a plan the user can approve. "
             "The plan: (1) the goal in one line, (2) what you found that matters, (3) the steps, each naming the files or commands it "
             "touches and what changes, (4) how you will check the result, (5) risks and open questions. Every tool that changes files or "
             "state is refused for this message. When the user approves, the plan will be run in Agent mode. Do not continue earlier work.")
PLAN_REFUSED = ("Not run in Plan mode: {name} changes things, and nothing is changed while planning. Look with Read, Glob, Grep, "
                "WebSearch, WebFetch or Bash commands that only look, then write the plan; it will be run in Agent mode after the user approves it.")


# ★2026-11-25 オーナー「タスクを連続して頼んでもコンテキストの読み直しが発生する」: Agent の前置きで始めた会話をチャットに
#   切り替えると、前置きと道具の一覧が 1 文字目から変わって会話を頭から読み直していた（偽の推論機で再現）。Ask と同じく、前置きと道具は
#   そのまま見せて、モードの決まりはこの一言で渡し、使えない道具は走らせるときに断る（server._ask_keeps_prefix）。
TALK_NOTE = ("[Harness] Chat mode, for the message above only: talk with the user — answer, explain, draft and think things through. "
             "Do not read, change or run anything on the user's files or machines. Only WebSearch, WebFetch and showing a web "
             "image (Tools with name Image and a url) are available for this message; every other tool is refused. Do not continue "
             "earlier work.")
TALK_REFUSED = ("Not run in Chat mode: {name} is not available for this message. Answer from what you know, or look it up with "
                "WebSearch or WebFetch. If the user wants files read or changed, tell them to switch to Agent mode.")


STOPPED_NOTE = ("[Harness] The user stopped the previous request on purpose. A command or edit that was running may have "
                "stopped part-way, and servers started in the background may still be running. Check the state you depend on "
                "before you go on, and do not resume the stopped work unless the user asks.")


def _small_folder(root: str) -> bool:
    n = 0
    for dp, fn in _walk(root):
        if not any(x in _lp.WS_NAMES for x in dp.replace("\\", "/").split("/")):
            n += len(fn)
            if n > SCOUT_SMALL_FOLDER:
                return False
    return True


class ChildOut:
    """子の道具の動きを、親の画面へ 1 行ずつ渡す。本文と思考は流さない。"""
    def __init__(self, out: Any, rid: str | None, label: str) -> None:
        self.out, self.rid, self.label = out, rid, label
        self.error = ""                    # 子が落ちた理由（親へ返す）

    def emit(self, type_: str, rid: str | None = None, **f: Any) -> None:
        if type_ == "error":
            self.error = str(f.get("message") or "")
            return
        if type_ == "tool_call":
            msg = f"{f.get('name')} {_oneline(f.get('input'))}"
        elif type_ == "tool_result":
            mark = "✓" if f.get("ok") else "✗"
            msg = f"{mark} {f.get('name')}"
        else:
            return
        self.out.notice("subagent", self.rid, message="子: " + msg[:300], child_event=type_, child_label=self.label)

    def notice(self, kind: str, rid: str | None = None, **f: Any) -> None:
        if kind == "context_compacted":    # 子の要約は記録に残す（読み直しの原因を後から数えられるように）。答えの下には残らない種類で
            self.out.notice("child_compacted", self.rid, message=str(f.get("message") or "")[:300], child_label=self.label)


def _child_result(text: str, rec: dict, rid: str | None) -> str:
    """子の答えを親へ渡す形にする（2026-11-18）。

    - 長い答えは全文をファイルに置き、親には頭と置き場を返す（親の文脈は子の答えの分だけ増え、以後の手番でずっと読み続ける）。
    - 終わり方と大きさを 1 行添える（途中で止まった・目安で促した、を親が見分けられるように）。
    - 子の報告は手掛かりとして扱い、要の所は自分で確かめるよう添える（作法: 子の報告は検証するまで手掛かり）。"""
    limit = _env_int("LAPS_CHILD_RESULT_MAX", CHILD_RESULT_MAX)
    body = text
    if limit and len(text) > limit:
        path = ""
        try:
            from .. import paths as _lp
            d = _lp.home_dir() / "tasks"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{(rid or 'run')[:12]}-{len(text)}.md"
            p.write_text(text, encoding="utf-8")
            path = str(p)
        except OSError:
            path = ""
        cut = text[:limit].rsplit("\n", 1)[0] or text[:limit]
        body = cut + (f"\n\n[The rest of the report ({len(text) - len(cut):,} chars) is in {path}. Read it if you need it.]" if path
                      else f"\n\n[The report was cut here; {len(text) - len(cut):,} chars were dropped.]")
    how = "" if rec["outcome"] in ("ok", "accepted", "completed", "") else f", ended: {rec['outcome']}"
    return (f"{body}\n\n(Sub-agent: {rec['iters']} steps, {rec['ctx_max'] // 1000}K tokens, {int(rec['secs'])} s{how}. "
            "Treat its claims as leads: check the ones you act on.)")


def _oneline(inp: Any) -> str:
    if isinstance(inp, dict):
        for k in ("command", "file_path", "pattern", "q", "query", "url", "name"):
            if inp.get(k):
                return str(inp[k]).replace("\n", " ")[:160]
    return ""


def _looked_at(raw: list[dict]) -> str:
    """子が呼んだ道具の一覧（読んだファイルと走らせた命令）。"""
    rows: list[str] = []
    for m in raw:
        for c in m.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            rows.append(f"- {fn.get('name')} {_oneline(args)}".rstrip())
    return "\n".join(dict.fromkeys(rows))


def _tool_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _repair(view: list[dict]) -> list[dict]:
    """道具の呼び出しと結果の組を揃える（走行が途中で落ちた記録・旧い中核の記録のため）。
    結果の無い呼び出しには短い印を足し、呼び出しの無い結果は外す。書式によっては組が欠けると推論機が断る。"""
    out: list[dict] = []
    i = 0
    while i < len(view):
        m = view[i]
        i += 1
        if m.get("role") == "tool":
            continue                       # 呼び出しの後ろで拾わなかった結果＝孤児
        out.append(m)
        ids = [c.get("id") for c in m.get("tool_calls") or []] if m.get("role") == "assistant" else []
        if not ids:
            continue
        got: dict = {}
        while i < len(view) and view[i].get("role") == "tool":
            got[view[i].get("tool_call_id")] = view[i]
            i += 1
        for cid in ids:
            out.append(got.get(cid) or {"role": "tool", "tool_call_id": cid, "content": "(no result: the run was interrupted)"})
    return out


class Agent:
    def __init__(self, *, llm: LLM, out: Any, rid: str | None, transcript: Transcript, ctx: Ctx, tools: dict[str, Tool],
                 system: str, think: bool = True, effort: str = "", limits: Limits | None = None, child: bool = False,
                 memory: Any = None) -> None:
        self.llm, self.out, self.rid, self.t, self.ctx = llm, out, rid, transcript, ctx
        self.memory = memory               # engine/memory.py（子と会話モードには無い）
        self.aids: Aids | None = None      # engine/aids.py（子と会話モードには無い）
        self.finish: Finish | None = None  # engine/finish.py
        self.child_kind = ""
        self.ask_allowed: frozenset | None = None   # Ask の依頼を Agent の前置きのまま走らせるとき: 走らせてよい道具の名前（server._ask_keeps_prefix）
        self.review_asked = False
        self.review_mark = 0
        self.started_at = time.time()
        self._turn_text = ""
        self._turn_finish = ""
        self._fold = False
        self.last_fold = False             # 最後の手番の本文は思考の欄へ流した（答えに足さない）
        self._held: str | None = None      # 見直しが残っている間に預かった本文（道具を呼んだら出す・終える手番で見直しを頼んだら出さない）
        self._fresh = False                # 崩れの後のやり直し（読み込みの控えを使わない）
        self._next_child_limits: Limits | None = None
        self.interjections: list[str] = []
        self._step_effects = 0
        self.tools, self.system, self.think, self.effort = tools, system, think, effort
        self.limits = limits or Limits()
        self.child = child
        self.parent: Agent | None = None   # 子のとき: 親（システムの変更の知らせ・鍵の伏せ・訂正の関門は親の物を使う）
        self._doom_stopped = False
        self.counters: dict[str, Any] = {}
        self.outcome = ""
        self._stop_hooks = 0                # Stop のフックで続けさせた回数（hooks.MAX_STOP_BLOCKS まで）
        self.plan_mode = False              # 計画モード（ask_allowed と一緒に使う・2026-11-19）
        self.keep_mode = ""                 # 前の前置きのまま走らせるモード: "" (Ask/Plan) / "talk"（2026-11-25）
        self.start_notes: list[str] = []    # 依頼の直後に足す一言（覚え書きが変わった等・前置きを作り直さずに渡す・2026-11-25）
        self.finish_reason = ""
        self.stage = "推論機"
        self.iters = 0
        self.tool_calls = 0
        self.tool_counts: dict[str, int] = {}
        self.effects = 0
        self.compactions = 0
        self.think_chars = 0
        self.think_tokens = 0              # 思考のトークンの見積もり（手番ごとに estimate_tokens・2026-11-16）
        self.answer_chars = 0
        self.last_tokens = 0
        self.started = time.monotonic()
        self._inbox: deque[str] = deque()
        self._lock = threading.Lock()
        self._closing = False
        self._inbox_watch = False
        self._schemas: list[dict] | None = None
        self._slot_park = ""               # 子を起こす前に保存した親の席（engine/slots.py）
        self._slot_restore = ""            # 次の手番の前に戻す席のファイル
        self._other_llms: list[LLM] = []   # 別の推論機で動いている子の線（止めるときに一緒に切る・engine/childengine.py）
        self._ctx_nudges = 0               # 子の文脈の目安を越えて促した回数
        self.children: list[dict] = []     # 起こした子の記録（done の children）
        self._call_n = _max_call_id(transcript.raw)
        self._last_call: tuple[str, str] | None = None
        self._last_call_n = 0
        self._prev_think = ""
        self._stale = 0
        self._answered_at = 0              # 問いに答えた後で続きを促した一言の数（同じ一言で 2 回促さない）
        self._answers: list[str] = []      # 途中の問いへの答え（最後の答えにも残す。画面は走行の途中の文を畳むため）
        self._run_from = 0                 # この走行の記録の始まり（最初の道具の記録を思い出させるため）

    # ── 外から（別のスレッド） ─────────────
    def inject(self, text: str, force: bool = False) -> bool:
        """走行中の一言。走行が終わり際で受け取れないときは False。"""
        with self._lock:
            if self._closing:
                return False
            self._inbox.append(text)
        if force:
            self.ctx.preempt.set()
            self.llm.abort()
        else:
            self._watch_inbox()
        return True

    def _watch_inbox(self) -> None:
        """普通の一言は区切りで読む。道具が長く動いて区切りが来ないときは、その道具を止めて読む（Ctrl+Enter と同じ）。
        ★2026-10-31 の実例: 道具が 4 分動いている間、問いに 3 分 44 秒反応せず、「？」を強制で送ってやっと止まった。
        Bash と BashOutput は話しかけると自分で切り上げるので、ここで止まるのはそれ以外の長い道具だけ。
        子の作業は止めない（途中の結果を失う・急ぐなら Ctrl+Enter）。問いの答え待ちは答えが別の道で届く。"""
        secs = _env_float("LAPS_INJECT_ESCALATE", INJECT_ESCALATE)
        with self._lock:
            if secs <= 0 or self._inbox_watch:
                return
            self._inbox_watch = True

        def watch() -> None:
            t0 = time.monotonic()
            told = False
            try:
                while True:
                    time.sleep(0.5)
                    with self._lock:
                        if self._closing or not self._inbox:
                            return
                    stage = self.stage
                    if time.monotonic() - t0 < secs or stage in _NOT_A_TOOL or stage == "AskUserQuestion":
                        continue
                    if stage == "Task":
                        if not told:
                            told = True
                            self.out.notice("harness", self.rid, message="Your message will be read when the subagent finishes (Ctrl+Enter stops it now)")
                        continue
                    self._count("inject_escalated")
                    self.ctx.preempt.set()
                    return
            finally:
                with self._lock:
                    self._inbox_watch = False
        threading.Thread(target=watch, daemon=True, name="bw-inbox-watch").start()

    def cancel(self) -> None:
        self.ctx.cancel.set()
        self.llm.abort()
        for other in list(self._other_llms):     # 別の推論機で動いている子も切る
            other.abort()

    # ── 小物 ──────────────────────────────
    def _count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def _count_defer(self, job: dict) -> None:
        """BashOutput(wait) を待たずに返した後の様子を数える（2026-11-22・効いたかを記録で見る）。
        tools_while_deferred＝先送りした仕事が動いている間に呼んだ他の道具／wait_after_defer＝もう一度待った回数。"""
        moved = self.ctx.counters
        just = bool(moved.get("wait_deferred"))      # この呼び出しが先送りした本人（再び待ったとは数えない）
        if moved:
            for k, n in moved.items():
                self._count(k, n)
            moved.clear()
        jobs = self.ctx.jobs
        if jobs is None or just:
            return
        deferred = [j for j in jobs.running() if getattr(j, "deferred", False)]
        if not deferred:
            return
        if job["name"] == "BashOutput" and job["inp"].get("wait"):
            self._count("wait_after_defer")
        elif job["name"] != "TodoWrite":
            self._count("tools_while_deferred")

    def _note(self, text: str) -> None:
        """モデルへの一言を記録に足す（続いた user は送るときにまとめる）。"""
        self.t.append({"role": "user", "content": text, "bw_note": True})

    def _send_messages(self) -> list[dict]:
        """モデルへ送る形にする。続いた user は 1 つにまとめる（順番に厳しい書式のため）。画像は _settle_images が決めた物だけ。"""
        self._settle_images()
        out: list[dict] = [{"role": "system", "content": self.system}]
        view = _repair(self.t.view())
        # ★[Harness] の一言を利用者の発言の形で送らない（2026-11-02 Terminal-Bench の難しい課題の見直しで実測）。Qwen 系の書式は最後の
        #   利用者の発言より前の思考を描かないので、一言ごとに全部の思考が消え、プロンプトが最初の手番で変わって推論機が窓を読み直す
        #   （9.5 万トークンで 167 秒・制限 360 秒の課題の 46%）。道具の結果の後なら結果の末尾に足す。答えの後（道具の結果が無い所）は
        #   書式が発言と数えない形で包む。送る形だけを変え、記録は変えない。
        wrap_notes = "last_query_index" in (self.llm.info().template or "")
        for i, m in enumerate(view):
            role = m.get("role")
            if role == "assistant":
                msg: dict = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("reasoning_content"):
                    msg["reasoning_content"] = m["reasoning_content"]
                if m.get("tool_calls"):
                    msg["tool_calls"] = m["tool_calls"]
            elif role == "tool":
                msg = {"role": "tool", "tool_call_id": m.get("tool_call_id"), "content": m.get("content") or ""}
            else:
                content: Any = m.get("content") or ""
                if m.get("bw_image"):
                    content = self._image_parts(str(m["bw_image"]), str(content))
                msg = {"role": "user", "content": content}
            prev = out[-1]
            if m.get("bw_note") and isinstance(msg["content"], str):
                if prev["role"] == "tool" and isinstance(prev["content"], str):
                    prev["content"] = prev["content"] + "\n\n" + msg["content"]
                    continue
                if prev["role"] == "user" and isinstance(prev["content"], str) and prev["content"].endswith(_NOTE_CLOSE):
                    prev["content"] = prev["content"][:-len(_NOTE_CLOSE)] + "\n\n" + msg["content"] + _NOTE_CLOSE
                    continue
                if prev["role"] == "assistant" and wrap_notes:
                    out.append({"role": "user", "content": _NOTE_OPEN + msg["content"] + _NOTE_CLOSE})
                    continue
            if msg["role"] == "user" and prev["role"] == "user" and isinstance(prev["content"], str) \
                    and isinstance(msg["content"], str):
                prev["content"] = prev["content"] + "\n\n" + msg["content"]
            else:
                out.append(msg)
        return out

    def _settle_images(self, *, all_old: bool = False) -> None:
        """古い画像の行き先を一度だけ決めて記録に残す。★決めた事は後で変えない（同じ会話を毎回同じ形で送る＝読み込みの控えが切れない）。

        まだ決めていない画像が keep*2 枚たまったら、古い方からまとめて決める（_first_kept_image）。その画像から後ろが短ければ
        文字の 1 行に替え（読み直しは短い）、長ければ画像のまま残す（替えると、そこから後ろを全部読み直すため）。
        all_old: 要約の直後（どのみち頭から読み直す）。最近の keep 枚より古い画像を全部文字に替える。"""
        raw = self.t.raw
        last = self.t.last_compaction()
        floor = int(last[1]["bw_compaction"].get("keep_from") or last[0]) if last else 0
        texted, kept = self.t.image_marks()
        try:
            keep_n = max(1, int(os.environ.get("LAPS_IMAGE_HISTORY") or 3))
        except ValueError:
            keep_n = 3
        images = [i for i in range(floor, len(raw)) if raw[i].get("bw_image") and i not in texted]
        if all_old:
            if len(images) > keep_n:
                self.t.mark_images(images[:-keep_n], [])
            return
        if not texted and not kept:
            # 前の版（印を残さず、送るたびに枚数だけで決めていた）が続けていた会話: 前の要求で文字に替えていた分はそのまま替える。
            # 前の要求に載っていたのは最後の assistant より前の画像（2026-11-16 までの版で 6 枚以上撮った会話だけが当たる）。
            last_a = next((i for i in range(len(raw) - 1, floor - 1, -1) if raw[i].get("role") == "assistant"), -1)
            before = [i for i in images if i < last_a]
            old = before[:_first_kept_image(len(before), keep_n)]
            if old:
                self.t.mark_images(old, [])
                texted = set(old)
                images = [i for i in images if i not in texted]
        live = [i for i in images if i not in kept]
        cut = _first_kept_image(len(live), keep_n)
        if not cut:
            return
        tail: dict[int, int] = {}
        n = 0
        for j in range(len(raw) - 1, live[0] - 1, -1):
            m = raw[j]
            tail[j] = n
            if m.get("role") != "system":
                n += compact._size(m) + (IMAGE_TOKENS if m.get("bw_image") and j not in texted else 0)
        decide = live[:cut]
        text = [i for i in decide if tail[i] <= IMAGE_SWAP_TAIL]
        self.t.mark_images(text, [i for i in decide if i not in text])
        self._count("images_to_text", len(text))
        self._count("images_kept_far", cut - len(text))

    @staticmethod
    def _image_parts(path: str, text: str) -> Any:
        try:
            with open(path, "rb") as fh:
                data = base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            return text
        mime = mimetypes.guess_type(path)[0] or "image/png"
        return [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]

    def _tool_schemas(self) -> list[dict] | None:
        if not self.tools:
            return None
        if self._schemas is None:
            prog = None if self.child else PROGRESS_FIELD
            fresh = [t.schema(prog) for t in self.tools.values()]
            self._schemas = fresh if self.child else self._frozen_schemas(fresh)
        return self._schemas

    def _frozen_schemas(self, fresh: list[dict], *, refresh: bool = False) -> list[dict]:
        """道具の一覧は会話の間変えない（前置きの system.txt と同じ考え・2026-11-16）。
        ★道具の一覧は前置きのすぐ後ろに描かれるので、説明が 1 字変わるだけで会話を頭から読み直す。0.1.685 と 0.1.686 の
          更新で Skill の説明が変わり、20 万トークンの会話を読み直していた（1 回 5〜7 分）。形（名前・引数・型・選択肢・順番）が
          同じなら、前に送った説明のまま送る。形が変わったとき（道具や引数が増えた）と要約で頭から読むときは今の物にする。"""
        if self.child or os.environ.get("LAPS_FREEZE_TOOLS", "1").strip() == "0":
            return fresh
        path = self.t.root / "tools.json"
        if not refresh:
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stored = None
            if isinstance(stored, list) and _tools_shape(stored) == _tools_shape(fresh):
                if stored != fresh:
                    self._count("tools_frozen")       # 説明だけ違う＝前の物を送った
                return stored
            if stored is not None:
                self._count("tools_reshaped")         # 形が変わった＝今の物にする（読み直しになる）
        try:
            path.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return fresh

    def n_ctx(self) -> int:
        info = self.llm.info()
        return info.n_ctx or FALLBACK_CTX.get(family(self.llm.model, info), 0)

    def _ja(self) -> bool:
        return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for c in getattr(self, "request", ""))

    def lang(self) -> str:
        return "Japanese" if self._ja() else "the user's language"

    def _hook(self, event: str, tool: str = "", **payload: Any):
        """利用者のフック（engine/hooks.py）を走らせる。命令の失敗は止めずに画面へ知らせる。"""
        from . import hooks
        if not hooks.enabled():
            return hooks.Outcome()
        payload.setdefault("session_id", str(getattr(self.t, "id", "") or ""))
        r = hooks.run(event, payload, tool=tool, cwd=self.ctx.workspace or os.getcwd())
        if r.ran:
            self._count(f"hook_runs:{event}", r.ran)
        for e in r.errors[:3]:
            self.out.notice("harness", self.rid, message=f"フック: {e}")
        return r

    def _say(self, ja: str, en: str) -> str:
        """ハーネスが利用者へ直接言う一言。本文として画面にも流す。"""
        self._flush_held()
        text = ja if self._ja() else en
        self.out.emit("token", self.rid, text=text, iter=self.iters)
        self.answer_chars += len(text)
        return text

    def _goal(self) -> str:
        """この走行の依頼。途中で一言あれば元の依頼と並べる（目的を見失わないように）。"""
        first = getattr(self, "first_request", "")
        latest = getattr(self, "request", "")
        return f"{first}\n\n(latest message: {latest})" if first and latest and first != latest else (latest or first)

    def _first_calls(self, n: int = 10) -> str:
        """この走行の最初の道具の記録（窓の奥の方は小さいモデルが見落とす・2026-10-18 オーナー「最初の方に行っていた作業で
        得た情報について忘れてしまったり」）。"""
        from ..oc.localsummary import call_line
        rows: list[str] = []
        msgs = self.t.raw[self._run_from:]
        results = {m.get("tool_call_id"): str(m.get("content") or "") for m in msgs if m.get("role") == "tool"}
        for m in msgs:
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                rows.append(call_line(str(fn.get("name") or ""), args, results.get(c.get("id"), "")))
                if len(rows) >= n:
                    return "\n".join(rows)
        return "\n".join(rows)

    def _last_action(self) -> str:
        """直前にモデルがしたこと（利用者の訂正を覚えるときに一緒に残す）。"""
        for m in reversed(self.t.raw):
            if m.get("role") != "assistant":
                continue
            calls = m.get("tool_calls") or []
            if calls:
                fn = calls[-1].get("function") or {}
                return f"{fn.get('name')} {str(fn.get('arguments'))[:300]}"
            return str(m.get("content") or "")[:300]
        return ""

    def _record(self, final: str = "", in_progress: bool = False) -> None:
        self.memory.record(request=self._goal(), outcome=self.outcome or "accepted", final=final, steps=self.iters,
                           files=list(dict.fromkeys(self.ctx.written)), tools=dict(self.tool_counts),
                           interjections=list(self.interjections), in_progress=in_progress)

    def _close_memory(self, text: str) -> None:
        m = self.memory
        if m is None:
            return
        m.close(final=text, ok=(self.outcome or "accepted") == "accepted", todos=self.ctx.todos, request=self._goal())
        self._record(text)
        if m.last_error:
            from .wire import log
            log(f"記憶: {m.last_error}")

    def _frame_message(self, text: str) -> str:
        """走行の途中の一言を、依頼より先に読ませる枠で包む（2026-10-31 オーナー「割り込みの指示を今はあんまり聞いてくれない」・
        作り直しで落としていた旧い中核の守り）。素の文のままだと、モデルは続きの作業の補足として読み流す。
        計画や一覧より先だと書き、止める・待つならそこで手番を終えさせる（判断はモデル）。"""
        goal = " ".join((self.first_request or "").split())
        goal = goal if len(goal) <= 300 else goal[:300] + "…"
        return (f"{INTERJECTION_HEAD}\n{text}\n\n"
                f"(The request you were working on: 「{goal}」. This message from the user comes first: it overrides that request, "
                "your plan, your todo list and any [Harness] notes. If it asks you to stop, pause or wait, do only what it asks "
                "before stopping (for example, write down where you are so the work can be resumed), then end your turn with a "
                "short reply to the user and do not carry on with the task. If it is a question, answer it in your next message, "
                "in plain text (look it up first only if you have to), and do not ask whether to carry on: the work goes on unless "
                "the user said to stop. If it changes the request, follow it. If it only adds information or a step, apply it and "
                "continue.)")

    def _take_inbox(self) -> None:
        with self._lock:
            items = list(self._inbox)
            self._inbox.clear()
        if items:                          # 生成が切れる前に手番が終わったとき、止める合図が次の道具を撃たないように
            self.ctx.preempt.clear()
        for text in items:
            if self.memory:
                self.memory.correction(text, self._goal(), self._last_action())
            self.request = text
            if not self.child and len(text.strip()) >= TASK_MIN_CHARS:
                self.ctx.request = text
            self.interjections.append(text)
            self.t.append({"role": "user", "content": self._frame_message(text)})
            # 画面に「読みました」を知らせる（2026-11-15 UI/UX 計画 B2・送った一言が渡ったかが見えなかった）。
            #   札（kind）は画面まで届かないので、文の頭の決まった言葉で見分ける（app.js の chat-notice）。他の端末には経過の 1 行として出る。
            self.out.notice("inject_taken", self.rid, message="受け取りました: " + text[:400])
            extra = self.aids.on_message(text) if self.aids else ""
            if extra:
                self._note(extra)

    def _answered_question(self) -> bool:
        """最後の一言が問いで、まだ続きを促していないか。問いは「止めて」ではないので、答えた後は仕事に戻す
        （North は本文と道具を同じ手番に書けない＝答えると手番が終わり、作業の途中で走行まで閉じてしまう）。"""
        n = len(self.interjections)
        if not n or self._answered_at >= n:
            return False
        last = self.interjections[-1]
        return endgate.is_question(last) and not endgate.asks_to_stop(last)

    def _close_inbox(self) -> None:
        """走行を閉じる。間に合わなかった一言は届かなかったと知らせる（黙って捨てない）。"""
        with self._lock:
            self._closing = True
            left = list(self._inbox)
            self._inbox.clear()
        for text in left:
            self.out.notice("inject_ignored", self.rid, text=text[:400],
                            message="The task had just finished; this message was not delivered: " + text[:400])

    def _take_preempt(self) -> None:
        """強制の一言で道具を止めた後の片付け。子は消さない（親がこの合図で止まる）。"""
        if self.ctx.preempt.is_set() and not self.child:
            self.ctx.preempt.clear()
            self._count("preempted_tool")
            self.out.notice("preempted", self.rid, message="Stopped the tool to take your message")

    def _take_job_notes(self) -> None:
        jobs = self.ctx.jobs
        if jobs is None or self.child:
            return
        for note in jobs.notes():
            if self.aids:
                self.aids.job_note(note)
            self._note("[Harness] " + note)

    def _take_monitor_notes(self) -> None:
        if self.aids:
            text = self.aids.monitor_text()
            if text:
                self._note(text)

    def _child_budget_note(self) -> None:
        """子の文脈の目安（2026-11-18）。止めはしない（オーナー 11-16「制限を全部取り払って」）。目安を越えたら一度だけ、
        答えられるなら今まとめるよう促す。2 倍を越えたらもう一度。11-18 の子は 15 万トークンまで育ち、1 手ごとの読み込みが重くなっていた。"""
        soft = _env_int("LAPS_CHILD_CTX_SOFT", CHILD_CTX_SOFT)
        if soft <= 0 or self.last_tokens < soft * (self._ctx_nudges + 1):
            return
        if self._ctx_nudges >= 2:
            return
        self._ctx_nudges += 1
        self._count("child_ctx_nudged")
        self._note(f"[Harness] This sub-task has used about {self.last_tokens // 1000}K tokens of context. If you can answer the "
                   "caller now, stop calling tools and write the final report. Continue only for something the answer cannot do without.")

    def _over_limit(self) -> str:
        lim = self.limits
        if lim.max_iters and self.iters >= lim.max_iters:
            return "max_iters"
        if lim.max_seconds and time.monotonic() - self.started >= lim.max_seconds:
            return "budget_exhausted"
        if lim.max_tool_calls and self.tool_calls >= lim.max_tool_calls:
            return "budget_exhausted"
        return ""

    def _continued_summary_body(self) -> dict:
        """会話の続きとして要約を頼む要求。★いま送っている形のまま後ろに足す（片付けの前に作る: 片付けると途中から変わり読み直しになる）。"""
        wrap = (_NOTE_OPEN, _NOTE_CLOSE) if "last_query_index" in (self.llm.info().template or "") else None
        msgs = compact.continued_messages(self._send_messages(), wrap)
        # ★思考を切らない（budget=0 にしない）: Qwen3.8 の書式は enable_thinking と reasoning_effort を system に描く＝切ると先頭から変わり、
        #   全部読み直しになる（2026-11-03 書式を読んで確認）。上限だけ小さくする。上限は描かれない。
        return self.llm.payload(msgs, self._tool_schemas(), mode=self.ctx.mode, think=self.think, effort=self.effort,
                                max_tokens=compact.SUMMARY_TOKENS + compact.SUMMARY_THINK, budget=compact.SUMMARY_THINK)

    def _boundary_compact(self) -> None:
        """新しい依頼の頭で、長くなった会話を 1 回だけ要約する（2026-11-25 オーナー「会話が続くとどんどん蓄積されていって
        しまうので Claude アプリみたいな感じの仕組みにしたい」）。作業の途中ではやらない（前を書き換えるとそこから全部読み直す）。
        古い道具の出力の片付けは飛ばして要約へ直行する: 片付けは一番古い片付けた所から後ろを全部読み直すが、続きの形の要約なら
        読み込み済みの分をそのまま使い、読み直すのは要約と残した最近の分（約 2 万）だけ。"""
        if self.child or not any(m.get("role") == "assistant" for m in self.t.raw[:self._run_from]):
            return
        n_ctx = self.n_ctx()
        if not compact.early_at(n_ctx):
            return
        # ★長さは、席にこの会話が入っていれば推論機が数えた本物の数（日本語・コード・道具の出力で文字からの見積もりはずれる）。
        #   分からない時（Strata でない・席が別の会話）だけ見積もる。
        real = 0
        try:
            from . import slots
            real = slots.held_for(self.llm, self.t.id)
        except Exception:  # noqa: BLE001
            real = 0
        est = real or sum(compact._size(m) for m in self.t.view()) + estimate_tokens(self.system)
        if not compact.boundary_due(est, n_ctx, self.llm):
            return
        self.last_tokens = est
        self._count("compaction_boundary")
        self._count("compaction_boundary_measured" if real else "compaction_boundary_estimated")
        self._compact(boundary=True)

    def _compact(self, *, forced: bool = False, boundary: bool = False) -> None:
        n_ctx = self.n_ctx()
        continued = self._continued_summary_body() if compact.continued_enabled(self.llm, forced) else None
        if not forced and not boundary:
            saved = compact.prune(self.t)
            if saved:
                self._count("compaction_pruned")
                self.last_tokens -= saved
                if self.last_tokens < compact.usable(n_ctx) * 0.8:
                    return
        before = self.last_tokens
        self.stage = "要約"
        self.out.notice("compacting", self.rid, message="Summarizing the conversation to free context")
        t0 = time.monotonic()
        self._summary_chars = 0

        def tick(n: int) -> None:
            self._summary_chars = n
        after = None
        if self.memory:
            self._record(in_progress=True)

            def after(summary: str, head: list[dict]) -> str:
                text = self.memory.compacted(summary, head, self.compactions + 1, self.ctx.todos, self.iters)
                return "\n\n".join(x for x in (text, self.aids.resources() if self.aids else "") if x)
        mark_before = (self.t.last_compaction() or (-1,))[0]
        info = compact.summarize(self.t, self.llm, self._goal(), n_ctx, tick, after, continued, boundary=boundary)
        if (self.t.last_compaction() or (-1,))[0] != mark_before:
            self._settle_images(all_old=True)    # 要約で頭から読み直すので、残した遠い画像もここで文字に替える
            if self.tools and not self.child:     # 頭から読み直すついでに、道具の説明も今の版の物へ
                prog = PROGRESS_FIELD
                self._schemas = self._frozen_schemas([t.schema(prog) for t in self.tools.values()], refresh=True)
        if info.get("continued"):
            self._count("compaction_continued")
        if info.get("retried"):
            self._count("compaction_continued_retry")
        if info.get("fell"):               # 続きの形で書けず、会話を文字にまとめ直して頭から読んだ（理由つき）
            self._count(f"compaction_fell:{info['fell']}")
        if self.aids:                      # 中身を失ったので、同じ Read でも意味が変わる
            self.aids.forget_repeats()
        self.compactions += 1
        self._count("compactions")
        if info["failed"]:
            self._count("compaction_failed")
            self.out.notice("compaction_failed", self.rid, message="Could not write a summary; continuing from the record")
        self.last_tokens = sum(compact._size(m) for m in self.t.view()) + estimate_tokens(self.system)
        self.out.notice("context_compacted", self.rid, message=f"Context summarized ({before:,} → {self.last_tokens:,} tokens)",
                        before=before, after=self.last_tokens, secs=round(time.monotonic() - t0, 1))

    def _generate(self, *, with_tools: bool = True, tool_choice: str = "", presence: float | None = None) -> Turn:
        body = self.llm.payload(self._send_messages(), self._tool_schemas() if with_tools else None, mode=self.ctx.mode,
                                think=self.think, effort=self.effort, tool_choice=tool_choice, presence=presence)

        fold = self._fold                  # 確かめの手番の語りは思考の欄へ（答えは画面に残っている）
        self._fold = False
        self.last_fold = fold
        if self._fresh:                    # 崩れの後は控えを使わず、温度を少し下げて 1 回だけやり直す
            self._fresh = False
            body["cache_prompt"] = False
            body["temperature"] = min(float(body.get("temperature") or 0.7), 0.7)
        if fold and not self.child:
            self.out.emit("think", self.rid, text="（終える前の確かめ）\n")
        self._flush_held()
        # ★見直しが残っている間は本文を預かる（2026-11-06 オーナー「報告してからレビューを行って更に報告を行ってくる。
        #   レビューを最後に行うタスクの場合はレビュー後に報告させる」）。道具を呼んだ手番なら次の手番の前に出す。
        keep = bool(self.finish and not self.child and not fold and self.finish.review_pending())
        kept_buf: list[str] = []

        hold = qwencall.Hold()             # 呼び出しの形は画面へ流さない（説明だったら手番の後で流す）

        def on_text(t: str) -> None:
            t = hold.feed(t)
            if t and keep:
                kept_buf.append(t)
            elif t and not self.child:
                self.out.emit("think" if fold else "token", self.rid, text=t, **({} if fold else {"iter": self.iters}))

        def on_think(t: str) -> None:
            if not self.child:
                self.out.emit("think", self.rid, text=t)

        def on_retry(n: int, why: str, delay: float) -> None:
            self._count("engine_retry")
            self.out.notice("engine_retry", self.rid, message=f"The inference server failed; retry {n} in {int(delay)}s: {why}")
        leak_ja = bool(not fold and self.request and self._ja() and family(self.llm.model, self.llm.info()) == "gemma")
        turn = self.llm.stream(body, on_text=on_text, on_think=on_think, on_retry=on_retry, leak_ja=leak_ja)
        held = hold.rest()
        if held and qwencall.unread(turn.text) is None and not self.child:
            if keep:
                kept_buf.append(held)
            else:
                self.out.emit("think" if fold else "token", self.rid, text=held, **({} if fold else {"iter": self.iters}))
        if keep:
            self._held = "".join(kept_buf)
        return turn

    def _flush_held(self) -> None:
        """預かった本文を画面へ（見直しを頼まなかった・道具を呼んだ手番）。"""
        if self._held and not self.child:
            self.out.emit("token", self.rid, text=self._held, iter=self.iters)
        self._held = None

    def _commit(self, turn: Turn, calls: list[dict]) -> list[dict]:
        """手番を記録に足す。思考は生成したまま残す（送り返す文字列が変わると読み直しになり、North は全部渡すのが推奨）。"""
        tcs: list[dict] = []
        for c in calls:
            self._call_n += 1
            tcs.append({"id": f"c{self._call_n}", "type": "function",
                        "function": {"name": c.get("name") or "", "arguments": c.get("arguments") or "{}"}})
        msg: dict = {"role": "assistant", "content": turn.text}
        if turn.thinking:
            msg["reasoning_content"] = turn.thinking
        if tcs:
            msg["tool_calls"] = tcs
        self.t.append(msg)
        return tcs

    def _park(self) -> bool:
        """背景の仕事が動いている間は走行を閉じない（Claude Code と同じ）。話しかけにはすぐ応じ、仕事の知らせで起きる。
        止まっていると一度伝えた仕事（サーバなど）では待たない。"""
        jobs = self.ctx.jobs
        if self.child or jobs is None:
            return False
        if os.environ.get("LAPS_BG_PARK", "1").strip() == "0":
            return False
        waiting = jobs.waiting()
        if not waiting:
            return False
        try:
            wake_every = float(os.environ.get("LAPS_BG_PARK_MAX_SEC") or 21600)
        except ValueError:
            wake_every = 21600.0
        # ★見直しのために預かった本文は、待つ前に出す（2026-11-12 オーナー「タスク完了待ちのときに Thinking 扱いになって
        #   まとめを出してくれない」: 「作成を開始しました・終わったら報告します」が待ちの終わる 2 分半後まで画面に出なかった）。
        #   仕事が動いている間の文は途中の報告で、見直しの後に書き直す最後の報告ではない。
        self._flush_held()
        self._count("bg_park")
        self.out.notice("bg_park", self.rid, message=f"Waiting for {len(waiting)} background job(s). You can send a message any time.")
        self.stage = "背景の仕事"
        t0 = time.monotonic()
        mon_at = t0
        while jobs.waiting():
            if self.ctx.cancel.is_set() or self._inbox:
                break
            notes = jobs.notes()
            if notes:
                for note in notes:
                    if self.aids:
                        self.aids.job_note(note)
                    self._note("[Harness] " + note)
                break
            # ★見張り（Monitor）に届く物があれば起きる（2026-11-13: 仕事を待つ間は見張りを読まず、エラーの行が最長 6 時間届かなかった）。
            #   渡すのは次の周の頭（_take_monitor_notes）。
            if self.aids and time.monotonic() >= mon_at:
                mon_at = time.monotonic() + 1.0
                if self.aids.monitor_ready():
                    break
            if wake_every > 0 and time.monotonic() - t0 >= wake_every:
                self._note(f"[Harness] {len(jobs.waiting())} background job(s) still running after {int(time.monotonic() - t0)} seconds. "
                           "Tell the user how far they have got.")
                break
            time.sleep(0.5)
        self._count("bg_park_secs", int(time.monotonic() - t0))
        self._take_job_notes()
        return True

    # ── 走行 ──────────────────────────────
    def _task_of(self, request: str) -> str:
        """この会話の元の依頼。「続けて」のような短い依頼では、前の中身のある依頼を指す（2026-10-13 オーナー「長時間の作業にも
        耐えられるような設計に」: 道具の案内・要約・割り込みの枠が「続けて」しか見ていなかった）。"""
        if len(request.strip()) >= TASK_MIN_CHARS:
            return request
        for m in reversed(self.t.raw):
            c = m.get("content")
            if (m.get("role") == "user" and not m.get("bw_note") and not m.get("bw_compaction") and isinstance(c, str)
                    and len(c.strip()) >= TASK_MIN_CHARS and not c.startswith(INTERJECTION_HEAD)):
                return c
        return request

    def run(self, request: str) -> str:
        self.request, self.first_request = request, self._task_of(request)
        self._stop_hooks = 0
        if not self.child:
            self.ctx.request = self.first_request
        self._run_from = len(self.t.raw)
        new_conversation = not any(m.get("role") == "assistant" for m in self.t.raw)
        last_action = self._last_action()
        self.t.append({"role": "user", "content": request})
        if not self.child:                 # 利用者のフック（依頼を受けたとき・2026-11-19）
            hk = self._hook("UserPromptSubmit", prompt=request)
            if hk.block:
                self._count("hook_prompt_blocked")
                self.outcome = "declined"
                self._close_inbox()
                text = self._say(f"フックがこの依頼を止めました: {hk.reason}", f"A hook stopped this request: {hk.reason}")
                self._close_memory(text)
                return text
            if hk.context:
                self._note("[Hook] " + hk.context)
        if self.ask_allowed is not None:
            self._note({"talk": TALK_NOTE}.get(self.keep_mode) or (PLAN_NOTE if self.plan_mode else ASK_NOTE))
        for note in self.start_notes:
            self._note(note)
        if self.memory:
            block = self.memory.begin(request, new_conversation=new_conversation, last_action=last_action)
            if block:                      # 作業ログと帳面は新しい会話だけ・当たる記憶は毎回（依頼の後ろに足す＝前置きは変えない）
                self.t.append({"role": "user", "content": block, "bw_note": True})
        if self.aids:
            block = self.aids.begin(request)
            if block:
                self.t.append({"role": "user", "content": block, "bw_note": True})
        text = ""
        try:
            self._boundary_compact()
            self._first_look(request, new_conversation)
            text = self._loop()
        except Cancelled:
            self.outcome = "cancelled"
        except Exception as e:             # 走行の不具合で本体ごと落とさない。理由は必ず画面に出す。
            import traceback
            traceback.print_exc()
            self.outcome = "crashed"
            self.out.emit("error", self.rid, kind="crash", message=f"The agent crashed: {type(e).__name__}: {e}")
        self._close_inbox()
        text = self._wrap_up(text)
        if self.outcome == "cancelled" and not self.child:
            self.t.append({"role": "user", "content": STOPPED_NOTE, "bw_note": True})
        self._close_memory(text)
        return text

    def _wrap_up(self, text: str) -> str:
        """走行の終わりの片付け。残った背景の仕事を止め（サーバは残す）、変えたシステムの設定の一覧を答えの下に付ける。"""
        if self.child:
            return text
        if self.aids:
            self.aids.close()
        jobs = self.ctx.jobs
        if jobs is not None:
            stopped, kept = jobs.stop_work()
            if stopped:
                self._count("jobs_stopped_at_end", len(stopped))
                self.out.notice("harness", self.rid, message=f"Stopped {len(stopped)} background jobs (run end): "
                                + ", ".join(j.command[:60] for j in stopped[:4]))  # 2026-11-20 オーナー「答えの下にまだ出る」→ 状態行だけ
            if kept:
                self._count("servers_left_running", len(kept))
                self.out.notice("harness", self.rid, message=f"Left {len(kept)} servers running: "
                                + ", ".join(j.command[:60] for j in kept[:4]), final=True)
        if self.finish:
            self.finish.tidy_scratch(text)
            foot = self.finish.footer(text)
            if foot:
                self.out.emit("token", self.rid, text=foot, iter=self.iters)
                text += foot
        return text

    def _first_look(self, request: str, new_conversation: bool) -> None:
        """最初の手番の前に子（explore）を 1 回走らせ、親が呼んだ結果の組として会話に入れる。
        2026-10-18 オーナー「子を最初から使わせる仕組みを導入してください」（渡しても呼ばなかったため）。
        ただの質問・空のフォルダ・続きの依頼では走らせない。"""
        if self.child or "Task" not in self.tools or self.ctx.spawn is None or not self.aids:
            return
        from ..oc import explore as ex
        mode = ex.first_mode()
        if mode == "off" or not ex.enabled() or endgate.only_question(request) or (mode == "first" and not new_conversation):
            return
        if 0 < _env_float("LAPS_TIME_LIMIT_SECONDS", 0.0) < 900:   # 締めが短い走行では親が自分で見る（旧い中核の explore_first:short_limit）
            self._count("explore_first:short_limit")
            return
        try:
            with os.scandir(self.ctx.workspace) as it:
                if not any(e.name not in _lp.WS_NAMES for e in it):
                    self._count("explore_first:empty_folder")
                    return
        except OSError:
            return
        if _small_folder(self.ctx.workspace):
            self._count("explore_first:small_folder")
            return
        self._call_n += 1
        cid = f"c{self._call_n}"
        inp = {"description": "Look around the folder", "subagent_type": "explore",
               "prompt": f"{ex.SCOUT_QUESTION}\n\nThe user's request:\n{request[:2000]}"}
        self.out.emit("tool_call", self.rid, call=cid, name="Task", input=inp)
        self.stage = "Task"
        self._next_child_limits = Limits(max_iters=ex.SCOUT_MAX_ITERS, max_seconds=ex.scout_max_seconds())
        t0 = time.monotonic()
        try:
            r = self.tools["Task"].run(inp, self.ctx)
        except Exception as e:             # 下見が落ちても依頼は進める
            self._count("explore_first:crashed")
            r = fail(f"Task crashed: {type(e).__name__}: {e}", head="Task")
        self._next_child_limits = None
        self._take_preempt()
        self._count("explore_first_sec", int(time.monotonic() - t0))
        r = r if isinstance(r, Result) else Result(str(r), head="Task")
        lines = _tool_lines(r.output)
        self.out.emit("tool_result", self.rid, call=cid, name="Task", result=_clean(r.output[:UI_RESULT_MAX]), chars=len(r.output),
                      truncated=r.truncated, ok=r.ok, kind=r.kind,
                      summary={"head": "Look around the folder", "tail": r.tail, "n": lines, "status": "ok" if r.ok else "error", "lines": lines})
        if self.ctx.cancel.is_set():
            return
        if not r.ok or ex.scout_found_nothing(r.output):   # 失敗や「見る物が無い」は親の文脈に入れない
            self._count("explore_first:failed" if not r.ok else "explore_first:nothing")
            return
        self._count("explore_first:ran")
        self.t.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "Task", "arguments": json.dumps(inp, ensure_ascii=False)}}]})
        self.t.append({"role": "tool", "tool_call_id": cid, "name": "Task", "content": f"{r.output}\n\n{_SCOUT_NOTE}"})

    def _stopped_by_cancel(self) -> bool:
        """生成か要約が切られた。本当に止められたら True。強制の一言なら False（次の周で一言を読む。子は親へ返る）。"""
        if self.ctx.cancel.is_set():
            self.outcome = "cancelled"
            return True
        if not self.child:
            self.ctx.preempt.clear()
            self._count("preempted_generation")
            self.out.notice("preempted", self.rid, message="Stopped generating to take your message")
        return False

    def _loop(self) -> str:
        cuts = empties = handoffs = todo_nudges = lengths = overflows = unreads = 0
        todo_at = -1
        tool_choice, presence = "", None
        last_turn_no_tools = tools_off = False
        while True:
            if self.ctx.cancel.is_set():
                self.outcome = "cancelled"
                return ""
            if self._doom_stopped:
                self.outcome = "loop_detected"
                return self._say("同じ呼び出しを繰り返して先へ進めなくなったので、ここで止めました。依頼を分けるか、手掛かりを足して送り直してください。",
                                 "The model kept making the same call and could not move on, so I stopped here. "
                                 "Please split the request or add a hint and send it again.")
            if self.child and self.ctx.preempt.is_set():
                # 利用者の一言は親が読む。子は続けず親へ返す（2026-10-31: 子が止める合図を自分で消して最後まで走り、一言が届かなかった）
                self.outcome = "preempted"
                self._count("child_preempted")
                return "(Stopped before finishing: the user sent a new message.)"
            self._take_inbox()
            self._take_job_notes()
            self._take_monitor_notes()
            limit = self._over_limit() if not last_turn_no_tools else ""
            if limit:
                self.outcome = limit
                last_turn_no_tools = True
                self._note("[Harness] The limit for this task was reached. Do not call tools. Write the final report now: "
                           "what is done, what is not, and what is left.")
            elif self.child and not last_turn_no_tools:
                self._child_budget_note()
            if self._slot_restore:              # 子が使った席を、親の控えに戻してから読む
                from . import slots
                name, self._slot_restore = self._slot_restore, ""
                slots.unpark(self.llm, name, self._count)
            n_ctx = self.n_ctx()
            if compact.overflowing(self.last_tokens, n_ctx):
                try:
                    self._compact()
                except Cancelled:
                    if self._stopped_by_cancel():
                        return ""
                    continue
            self.iters += 1
            fields: dict[str, Any] = {"n_ctx": n_ctx, "compact_at": compact.ring_at(n_ctx, self.llm)} if n_ctx else {}
            if self.last_tokens:
                fields["ctx"] = self.last_tokens
            self.ctx.tokens = self.last_tokens
            self.out.emit("iter_start", self.rid, iter=self.iters - 1, **fields)
            self.stage = "推論機"
            try:
                # 上限に達した最後の手番も道具の一覧は外さない（一覧が変わると全部読み直しになる・2026-10-30）。呼んでも走らせない。
                turn = self._generate(with_tools=not tools_off, tool_choice=tool_choice, presence=presence)
            except Cancelled:
                if self._stopped_by_cancel():
                    return ""
                continue
            except ContextOverflow:
                overflows += 1
                if overflows > 3:
                    self.outcome = "context_overflow"
                    return self._say("会話が長すぎて推論機の窓に収まらなくなりました。新しい会話で続けてください。",
                                     "The conversation no longer fits the model's window. Please continue in a new conversation.")
                try:
                    self._compact(forced=True)
                except Cancelled:
                    if self._stopped_by_cancel():
                        return ""
                continue
            except BackendError as e:
                if self._drop_bad_image(e):
                    continue
                self.outcome = "backend_error"
                try:
                    from ..oc.failclass import backend_class
                    self._count(f"backend_error:{backend_class(e)}")
                except Exception:
                    pass
                text = str(e)
                try:                       # 「応答しません」だけではどこへ送っていたのかが分からない（2026-09-06 オーナー機）
                    from ..netdiag import append_target
                    text = append_target(text, self.llm.base_url)
                except Exception:
                    pass
                self.out.emit("error", self.rid, kind="backend", message=text)
                return ""
            tool_choice, presence = "", None
            if turn.usage:
                self.last_tokens = int(turn.usage.get("total_tokens") or 0) or self.last_tokens
            if turn.timings and not self.child:
                # 読み直しを記録から数えられるように（2026-11-13: 依頼の最初の手番で会話を丸ごと読み直していても、走行の合計からは分けられなかった）
                rd, ca = int(turn.timings.get("prompt_n") or 0), int(turn.timings.get("cache_n") or 0)
                if "first_read_tokens" not in self.counters:
                    self.counters["first_read_tokens"], self.counters["first_cached_tokens"] = rd, ca
                if rd >= 20000 and ca < rd // 10:
                    self._count("reread_from_start")
                    self._count("reread_from_start_tokens", rd)
            self.finish_reason = turn.finish or self.finish_reason
            self.think_chars += len(turn.thinking)
            self.think_tokens += estimate_tokens(turn.thinking)
            self.answer_chars += len(turn.text)

            # 繰り返しで切った手番。切った物は記録に残さず（次の手番がまた同じ所へ戻るため）、一言添えて生成し直す。
            if turn.cut:
                cuts += 1
                self._count("hedge_cut" if turn.cut == "hedge" else "repeat_cut")
                if turn.degenerate:
                    self._count("degenerate_cut")
                    # ★控えを使わずに読み直すのは、続けて切った 1 回目だけ（2026-11-16 全体のレビュー: 上限を外した後は、崩れ続けると
                    #   毎回 10 万トークン超を頭から読み直し続けた）。
                    self._fresh = cuts == 1
                    self.out.notice("degenerate_retry", self.rid, message="The output broke down; generating again")
                if cuts >= MAX_CUTS and _flag("LAPS_REPEAT_STOP"):     # ★既定では止めない（切って生成し直すのは続ける・2026-11-16）
                    self.outcome = "loop_detected"
                    self.out.notice("doom_loop", self.rid, message="Stopped: the model kept repeating itself")
                    return self._say("同じ考えを繰り返して先へ進めなくなったので、ここで止めました。依頼を分けるか、手掛かりを足して送り直してください。",
                                     "The model kept repeating the same thought and could not move on, so I stopped here. "
                                     "Please split the request or add a hint and send it again.")
                if turn.cut == "hedge":
                    self.out.notice("answer_nudge", self.rid, message="Cut a thought going back and forth; asked to act")
                    self._note(f"[Harness] Your thinking kept going back and forth between possibilities (\"{turn.cut_unit[:120]}\") and "
                               "was cut off. More guessing will not settle it: find out with a tool (read the file, run the command, "
                               "look it up). If what you were assuming may be wrong, check that first. If only the user can decide, "
                               "ask them. " + (self.aids.resources() if self.aids else ""))
                else:
                    if not turn.degenerate:
                        self.out.notice("answer_nudge", self.rid, message="Cut a repeating thought; asked to go on")
                    self._note(f"[Harness] Your thinking started repeating itself (\"{turn.cut_unit[:120]}\") and was cut off. Continue "
                               "from where you are, without repeating what you already wrote. If the work is not finished, call the "
                               "next tool now; otherwise write the answer. If you are blocked, say what blocks you.")
                # 次の手番の思考は減らさず、道具も強いない（2026-10-13 オーナー「一度決めたら考え直さずに道具を呼ばせる この仕組みを
                # 消しましょう。モデルには自由に考えさせます」: その形は誤った前提のまま 40 分進ませた）。作り直しで戻してしまっていた。
                presence = 1.0 if self.llm.info().llama else None
                continue
            cuts = 0

            calls = self._salvage(turn)
            if last_turn_no_tools and calls:
                self._count("last_turn_calls_dropped", len(calls))
                calls = []
                if not turn.text.strip() and not tools_off:
                    tools_off = True       # 本文が無い。この 1 回だけ道具なしで書かせる（読み直しになる）
                    self._count("last_turn_retry_no_tools")
                    continue
            if calls:
                self._turn_text = turn.text
                self._turn_finish = turn.finish
                tcs = self._commit(turn, calls)
                self._execute(tcs)
                self._after_tools(turn)
                empties = unreads = 0
                if self.aids:
                    self.aids.scrub_saved_keys()
                if self.memory:
                    note = self.memory.recite(self.iters, self._goal(), self.ctx.todos)
                    if note:
                        early = self._first_calls() if self.llm.info().llama else ""
                        if early:
                            self._count("recite_early_log")
                            note += "\nYour first tool calls in this task and what they returned (do not redo them):\n" + early
                        self._note(note)
                    if self.memory.due():
                        self._record(in_progress=True)
                continue

            # ここから道具を呼ばなかった手番。
            if self.tools and not last_turn_no_tools and not tools_off:
                before = qwencall.unread(turn.text)
                if before is not None:     # 呼ぶつもりで書式が崩れた。答えにしない
                    if _limit("LAPS_MAX_UNREAD") and unreads >= _limit("LAPS_MAX_UNREAD"):   # 既定 0＝上限なし（2026-11-16）
                        self.outcome = self.outcome or "accepted"
                        return self._say("（道具の呼び出しの書式が崩れたまま、書き直せませんでした。続けるには「続けて」と送ってください。）",
                                         "(The tool call stayed malformed after being asked to rewrite it. Send \"continue\" to go on.)")
                    unreads += 1
                    self._count("tool_call_unread")
                    self.out.notice("answer_nudge", self.rid, message="The tool call could not be read; asked to write it again")
                    turn.text = before     # 崩れた形を履歴に残さない（次の手番が写す）
                    if before or turn.thinking:
                        self._commit(turn, [])
                    self._note(UNREAD_NOTE)
                    continue
            if turn.finish == "content_filter" and not turn.text.strip():
                self.outcome = "declined"
                self._count("model_declined")
                self.out.notice("declined", self.rid, message="The model declined this request")
                return self._say("モデルがこの依頼への応答を断りました。何も実行していません。",
                                 "The model declined to respond to this request. Nothing was run.")
            if turn.finish == "length" and lengths < 3:
                lengths += 1
                self._commit(turn, [])
                self.out.notice("gen_truncated", self.rid, message="The answer hit the output limit; asked to go on")
                self._note("[Harness] Your output was cut by the token limit. Continue exactly where it stopped.")
                continue
            if not turn.text.strip():
                empties += 1
                max_empty = _limit("LAPS_MAX_EMPTY")                # 既定 0＝上限なし（2026-11-16）
                if (not max_empty or empties <= max_empty) and not last_turn_no_tools:
                    self._count("empty_continue")
                    self.out.notice("answer_nudge", self.rid, message="Empty turn; nudged to go on")
                    self._note("[Harness] Your last turn had no answer and no tool call. Call the next tool now, or write the answer.")
                    if empties >= MAX_EMPTY and self.tools:
                        tool_choice = "required"
                    continue
                self.outcome = self.outcome or "accepted"
                return self._say("（作業の途中で手番が終わり、答えの本文がありません。続けるには「続けて」と送ってください。）",
                                 "(The turn ended in the middle of the work without an answer. Send \"continue\" to go on.)")
            self._commit(turn, [])
            if last_turn_no_tools:
                self._flush_held()
                return self._with_answers(self.finish.merge(turn.text) if self.finish else turn.text)
            if self._inbox:
                continue
            if self._park():
                continue
            # 途中で一言あった走行には「続けさせる」促しをかけない。手番を終えるかはモデルが一言に従って決める
            # （2026-10-12 オーナー「Todoリストをエージェント側が優先してモデルに作業を続けさせたりしました」: 中断の指示の後、
            #   答えを書いたところで一覧の未完了を理由に続けた。作り直しで落としていた旧い中核の決まり）。問いに答えただけのときだけ戻す。
            keep_going = not self.interjections
            if not keep_going and self._answered_question():
                self._answered_at = len(self.interjections)
                self._count("question_answered_go_on")
                self._answers.append(turn.text.strip())
                self._note("[Harness] You answered the user's question; it did not ask you to stop. If the work you were doing is not "
                           "finished, continue it now with the next tool call and do not repeat the answer. If it is finished, end "
                           "with one short line.")
                continue
            if keep_going and self.aids and not self.child:
                note = self.aids.monitors_left()
                if note:
                    self._note(note)
                    continue
            kind, note = ("", "")
            used = sum(n for k, n in self.tool_counts.items() if k != "TodoWrite")
            if keep_going and not self.child and "Bash" in self.tools and not self.ctx.answer_only:   # Ask モードは「やれ」と押さない
                # 予定表の促しの後に道具を 1 度も呼ばずにまた終えたら、それを答えとして受け取る（旧い中核と同じ）
                kind, note = endgate.check(self.request, turn.text, tools_used=used, effects=self.effects, ran=self.ctx.ran,
                                           todos=open_todos(self.ctx) if self.ctx.todos_touched else [],
                                           handoffs=handoffs, todo_nudges=2 if used == todo_at else todo_nudges)
            if kind == "handoff":
                handoffs += 1
                self._count("end_handoff")
                self.out.notice("answer_nudge", self.rid, message="No work was done yet; asked to do it")
                self._note(note)
                tool_choice = "required"
                continue
            if kind == "todo":
                todo_nudges += 1
                todo_at = used
                self._count("end_todo")
                self.out.notice("todo_nudge", self.rid, message="The plan has unfinished items; asked to go on")
                self._note(note)
                continue
            if keep_going and self.finish:
                note, force = self.finish.unfinished(turn.text)
                if note:
                    self._note(note)
                    if force and self.tools:
                        tool_choice = "required"
                    continue
                self.finish.draft_held = self._held is not None
                note = self.finish.end_checks(turn.text)
                self.finish.draft_held = False
                if note:
                    if self.finish.draft_hidden and self._held is not None:
                        self._held = None          # 見直しの前の報告は出さない（見直しの後に 1 回だけ書かせる）
                        self.out.notice("review", self.rid, message="Review first; the report comes after it")
                    self._note(note)
                    self._fold = self.finish.fold_next
                    self.finish.fold_next = False
                    continue
                note = self.finish.weak_report(turn.text)
                if note:
                    self._note(note)
                    continue
            if not self.child and self._stop_hooks < hooks_max_stop():   # 利用者のフック（終える前・2026-11-19）
                hk = self._hook("Stop", last_answer=turn.text[:20000])
                if hk.block:
                    self._stop_hooks += 1
                    self._count("hook_stop_continue")
                    self.out.notice("harness", self.rid, message="フックが終わるのを止め、続けさせました")
                    self._note("[Hook] Do not finish yet: " + hk.reason)
                    continue
            with self._lock:
                if self._inbox:
                    continue
                self._closing = True
            self.outcome = self.outcome or "accepted"
            if handoffs and not self.effects:
                self.out.notice("harness", self.rid, message="Nothing was changed or run in this task.", final=True)
            return self._final_text(turn.text)

    def _drop_bad_image(self, err: Exception) -> bool:
        """推論機が添えた画像を開けなかった。一番新しい画像を外して走行を続ける（壊れた .png で 2 回止まった・2026-10-09）。"""
        if "Failed to load image" not in str(err):
            return False
        for m in reversed(self.t.raw):
            if m.get("bw_image"):
                name = m.pop("bw_image")
                m["content"] = (f"[Harness] The inference server could not decode the image you read, so it was removed: {name}. The "
                                "file is probably damaged, encrypted, or in an unsupported format. Check the file itself (for "
                                "example its first bytes) instead of reading it as an image again.")
                self._count("image_rejected")
                self.out.notice("image_rejected", self.rid, message="Model could not read an image; dropped it")
                return True
        return False

    def _with_answers(self, text: str) -> str:
        """途中の問いに答えた文を最後の答えの前に足す（最後の答えが同じことを書いていれば足さない）。"""
        return "\n\n".join([a for a in self._answers if a not in text] + [text])

    def _final_text(self, body: str) -> str:
        """最後の答え。差し戻す前に画面に出した答えがあればそれに足す。確かめで見つかった ✗ の行は答えに残す。"""
        self._flush_held()
        if not self.finish:
            return self._with_answers(body)
        self.finish.report(body)
        text = self.finish.merge(body)
        if self.finish.kept.strip():
            try:
                from ..oc.selfreview import NG_LINE
                ng = [ln.strip() for ln in (body or "").splitlines() if NG_LINE.match(ln)][:8]
            except Exception:
                ng = []
            if ng and not all(x in text for x in ng):
                extra = "\n\n" + ("確かめで見つかった点:" if self._ja() else "Found while checking:") + "\n" + "\n".join(ng)
                self.out.emit("token", self.rid, text=extra, iter=self.iters)
                text += extra
        return self._with_answers(text)

    def _salvage(self, turn: Turn) -> list[dict]:
        """呼び出しが思考や本文に文字で書かれてしまった手番から拾う（書式によって起きる）。"""
        if turn.calls or not self.tools:
            return turn.calls
        calls: list[dict] = []
        if not turn.text.strip() and turn.thinking:
            calls = salvage_calls(turn.thinking, set(self.tools))
            if not calls:
                try:                       # Gemma の書き方（<|tool_call>call:…）
                    from ..oc.gemmacall import salvage as gemma_salvage
                    found, _rest = gemma_salvage(turn.thinking)
                except Exception:
                    found = []
                calls = [{"name": c["name"], "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False)}
                         for c in found if c.get("name") in self.tools]
            if calls:
                self._count("thought_call_salvaged")
                return calls
        if "<tool_call>" in turn.text:
            try:
                from ..oc.qwencall import salvage
                found, body = salvage(turn.text)
            except Exception:
                found, body = [], turn.text
            if found:
                turn.text = body
                self._count("text_call_salvaged", len(found))
                return [{"name": c["name"], "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False)} for c in found]
        return []

    def _after_tools(self, turn: Turn) -> None:
        """同じ考えが手番をまたいで続いていないかを見る（手本: pi-deadloop の思考の停滞）。"""
        head = turn.thinking[:600]
        if head and self._prev_think and similar(head, self._prev_think) >= 0.85:
            self._stale += 1
        else:
            self._stale = 0
        self._prev_think = head
        if self._stale >= 2:
            self._stale = 0
            self._count("think_stale")
            self._note("[Harness] Your reasoning has been the same for the last turns. Stop re-deriving it. Try a different "
                       "approach, or say what is blocking you.")
        self._take_preempt()
        if self.aids:
            note = self.aids.time_note()
            if note:
                self._note(note)
        if self.aids and turn.text.strip():
            note = self.aids.lang_note(turn.text)
            if note:
                self._note(note)

    # ── 道具 ──────────────────────────────
    def _resolve(self, name: str, inp: Any) -> tuple[Tool | None, str, Any]:
        """道具の名前の揺れを直す。一覧に無い機能の道具を直接呼んだときは Tools の中へ回す。"""
        if name in self.tools:
            return self.tools[name], name, inp
        low = name.lower()
        for k, t in self.tools.items():
            if k.lower() == low:
                return t, k, inp
        if "Tools" in self.tools:
            for k in LISTED:
                if k.lower() == low:
                    return self.tools["Tools"], "Tools", {"name": k, "input": inp}
        return None, name, inp

    def _prepare(self, tc: dict) -> dict:
        fn = tc.get("function") or {}
        raw_name = str(fn.get("name") or "")
        job: dict = {"id": tc["id"], "name": raw_name, "tool": None, "inp": {}, "error": None, "shown": {}}
        raw = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as e:
            job["error"] = fail(f"the arguments are not valid JSON ({e}). Send them again as a JSON object.", head=raw_name)
            return job
        if not isinstance(parsed, dict):
            parsed = {}
        job["shown"] = parsed
        tool, name, inp = self._resolve(raw_name, {k: v for k, v in parsed.items() if k != "progress"})
        job["name"] = name
        if tool is None:
            job["error"] = fail(f"unknown tool {raw_name!r}. Available: {', '.join(self.tools)}", head=raw_name)
            return job
        job["tool"] = tool
        job["inp"], job["renamed"] = adapt_names(tool, coerce(tool, inp))
        return job

    @staticmethod
    def _real_call(job: dict) -> tuple[str, dict]:
        """Tools の中から呼んだ道具は、中の道具の名前と引数で見る。"""
        if job["name"] == "Tools":
            inner = job["inp"].get("input")
            return str(job["inp"].get("name") or job["inp"].get("call") or "Tools"), inner if isinstance(inner, dict) else {}
        return job["name"], job["inp"]

    @staticmethod
    def _changes(job: dict) -> bool:
        tool = job["tool"]
        if tool is None or not tool.effect:
            return False
        return not (job["name"] == "Bash" and read_only(str(job["inp"].get("command") or "")))

    def _doom(self, job: dict) -> Result | None:
        """同じ呼び出しが続いたら走らせない（手本: opencode の doom_loop）。動いている仕事の待ちは繰り返しではない。
        ★2026-11-16 既定で切った（オーナー「同じ手の繰り返し…これなくして。Desktop ツールとか何回も使うと止まっちゃう」）。
          Desktop の look・wait・key のように、同じ引数で呼んで毎回違う結果が返る道具を止めていた。LAPS_DOOM_LOOP=1 で戻る。"""
        if not _flag("LAPS_DOOM_LOOP"):
            return None
        if job["name"] == "BashOutput" and running_job(job["inp"], self.ctx):
            self._last_call, self._last_call_n = None, 0
            return None
        key = (job["name"], json.dumps(job["inp"], sort_keys=True, ensure_ascii=False, default=str))
        if key == self._last_call:
            self._last_call_n += 1
        else:
            self._last_call, self._last_call_n = key, 1
        if self._last_call_n < DOOM_BLOCK:
            return None
        self._count("doom_loop")
        self.out.notice("doom_loop", self.rid, message=f"The same {job['name']} call {self._last_call_n} times in a row; not run")
        if self._last_call_n >= DOOM_STOP:   # ★cancel は立てない（outcome が cancelled に上書きされ、親とも共有なので親ごと止まる）
            self._doom_stopped = True
        return fail(f"You have made exactly this call {self._last_call_n} times in a row, so it was not run. It will not give a "
                    "different result. Change the approach, or say what is blocking you.", head=job["name"])

    def _via_tools_ok(self, job: dict) -> bool:
        """Agent の道具の一覧では Image は Tools の奥にある。チャット・画像のモードで、それを使ってよい道具として通す。"""
        if job["name"] != "Tools" or not self.ask_allowed:
            return False
        inner = str((job.get("inp") or {}).get("name") or "")
        return bool(inner) and inner in self.ask_allowed

    def _run_one(self, job: dict) -> Result:
        if job["error"] is not None:
            return job["error"]
        if self.ctx.stopped():
            return fail("Not run: the user stopped this or sent a new instruction.", kind="denied", head=job["name"])
        if self.ask_allowed is not None and job["name"] not in self.ask_allowed and not self._via_tools_ok(job):
            self._count("ask_refused")
            msg = {"talk": TALK_REFUSED}.get(self.keep_mode) or (PLAN_REFUSED if self.plan_mode else ASK_REFUSED)
            return fail(msg.format(name=job["name"]), kind="denied", head=job["name"])
        blocked = self._doom(job)
        if blocked is not None:
            return blocked
        real, real_inp = self._real_call(job)
        if self.child_kind == "explore" and real == "Bash":   # 下見の子は見るだけ（直してしまった実例・10-30／組み立てを走らせて 174 秒使い、二重に走らせた実例・10-31）
            from ..oc import explore as ex
            if not read_only(str(real_inp.get("command") or "")) or ex.bash_writes(str(real_inp.get("command") or "")):
                return fail(ex.READONLY_REFUSED, kind="denied", head=job["name"])
        if self.aids:
            gate = self.aids.skill_gate_hold(job["name"], job["inp"])
            if gate:                       # スキルの門は失敗ではない＝「止めた」で返す（画面で Error と出ていた・2026-11-16）
                self.out.notice("harness", self.rid, message="Waiting for the skill to be chosen before other tools")
                return Result("[Harness] " + gate, ok=False, kind="denied", head=job["name"])
            why = self.aids.blocked(job["name"], job["inp"], self._turn_text) or self.aids.before_call(real, real_inp, self._turn_text)
            if why:
                self.out.notice("harness", self.rid, message=f"Held a {real} call")
                return fail(why, head=job["name"])
        elif self.parent and self.parent.aids:
            why = self.parent.aids.before_call(real, real_inp, self._turn_text)
            if why:
                return fail(why, head=job["name"])
        memory = self.memory or (self.parent.memory if self.parent else None)
        if memory and self._changes(job):
            held = memory.gate(real, real_inp)
            if held:
                self.out.notice("harness", self.rid, message="Construct: held a call the user corrected before")
                return fail(held, head=job["name"])
        hk = self._hook("PreToolUse", real, tool_input=real_inp)
        if hk.block:                       # 利用者のフックが止めた（モデルには理由を返す・2026-11-19）
            self._count("hook_blocked")
            self.out.notice("harness", self.rid, message=f"フックが {real} を止めました")
            return fail(f"Blocked by the user's hook (PreToolUse): {hk.reason}", kind="denied", head=job["name"])
        if real == "Bash" and self._changes(job) and self.ctx.changes is not None:
            from .tools.patch import SHELL_FORM
            cmd = str(real_inp.get("command") or "")
            if not SHELL_FORM.match(cmd):  # Bash の apply_patch は ApplyPatch の道で控えを取る
                self.ctx.changes.note_bash(cmd)   # 何を変えたか分からない＝戻せないと言うため
        self.stage = job["name"]
        job["t0"] = time.time()
        try:
            out = job["tool"].run(job["inp"], self.ctx)
        except Exception as e:             # 道具の不具合はモデルへ返す（走行は続ける）
            import traceback
            traceback.print_exc()
            self._count("tool_crash")
            return fail(f"{job['name']} crashed: {type(e).__name__}: {e}", head=job["name"])
        return out if isinstance(out, Result) else Result(str(out), head=job["name"])

    def _finish_one(self, job: dict, r: Result, last: bool = False) -> None:
        name = job["name"]
        tool = job["tool"]
        self.tool_calls += 1
        self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
        self._count(f"tool_calls:{name}")
        self._count_defer(job)
        if job.get("t0"):                  # 道具ごとの時間（記録の tools は長い走行で切れる・28/78 走行。数えは切れない・2026-11-16）
            self._count(f"tool_ms:{self._real_call(job)[0]}", int((time.time() - job["t0"]) * 1000))
        if name == "Tools":                # 中の道具の名前でも数える（2026-11-13: 記録から Monitor・Browser・Desktop の回数を出せなかった）
            inner = str(job["inp"].get("name") or job["inp"].get("call") or "")
            if inner and isinstance(job["inp"].get("input"), dict):
                self._count(f"tool_calls_in:{inner}")
                if not r.ok:
                    self._count(f"tool_failed_in:{inner}")
        if not r.ok:
            self._count("tool_failed")
        elif tool is not None:
            effect = r.effect if r.effect is not None else tool.effect
            if name == "Bash" and read_only(str(job["inp"].get("command") or "")):
                effect = False
            if effect:
                self.effects += 1
        real, real_inp = self._real_call(job)
        if self.aids:
            self.aids.mask(r)
        elif self.parent and self.parent.aids:
            self.parent.aids.child_call(real, real_inp, r)
        if self.memory:
            r.output += self.memory.after_tool(real, real_inp, r.ok, r.output)
            if last:                       # 手番の最後の道具の結果に、作業フォルダの変化を添える
                note = self.memory.after_step(self.effects > self._step_effects)
                if note:
                    r.output += "\n\n" + note
                    if self.aids:
                        more = self.aids.step_note(self.memory.last_step, r)
                        if more:
                            r.output += "\n" + more
        if job.get("renamed"):
            self._count("args_renamed", len(job["renamed"]))
            said = "; ".join(f"`{a}` was read as `{b}`" for a, b in job["renamed"])
            r.output += f"\n\n(Note: {said}. {name}'s parameter names are those on the right.)"
        if self.aids and job.get("t0"):    # 走らせなかった呼び出しには添えない
            self.aids.after_tool(name, real, real_inp, r, time.time() - job["t0"], job["t0"])
        if job.get("t0"):                  # 走らせた呼び出しだけ（止めた・断った呼び出しにはフックを走らせない）
            hk = self._hook("PostToolUse", real, tool_input=real_inp, tool_response={"ok": r.ok, "output": r.output[:20000]})
            if hk.context:
                r.output += "\n\n[Hook] " + hk.context
            if hk.block:
                self._count("hook_post_feedback")
                r.output += "\n\n[Hook] " + hk.reason
        lines = _tool_lines(r.output)
        self.out.emit("tool_result", self.rid, call=job["id"], name=name, result=_clean(r.output[:UI_RESULT_MAX]),
                      chars=len(r.output), truncated=r.truncated, ok=r.ok, kind=r.kind,
                      summary={"head": r.head or name, "tail": r.tail, "n": lines, "status": "ok" if r.ok else "error", "lines": lines,
                               **({"shot": r.shot} if r.shot else {}), **({"show": r.show} if r.show else {})})
        self.t.append({"role": "tool", "tool_call_id": job["id"], "name": name, "content": r.output})

    def _execute(self, tcs: list[dict]) -> None:
        jobs = [self._prepare(tc) for tc in tcs]
        if getattr(self, "_turn_finish", "") == "length" and jobs and jobs[-1]["error"] is None and self._changes(jobs[-1]):
            # 出力の上限で切れた手番の最後の呼び出しは、引数が途中までかもしれない（推論機が閉じて渡すことがある）。書き換える道具は走らせない。
            # 手本: Qwen Code の reject-incomplete-file-writes（incomplete-tool-call-args.ts）。
            self._count("truncated_change_held")
            jobs[-1]["error"] = fail(TRUNCATED_CALL, head=jobs[-1]["name"])
        self._step_effects = self.effects
        for job in jobs:
            shown = json.loads(clean_json(json.dumps(job["shown"], ensure_ascii=False, default=str)))
            self.out.emit("tool_call", self.rid, call=job["id"], name=job["name"], input=shown)
        images: list[str] = []
        i = 0
        while i < len(jobs):
            j = i
            while j < len(jobs) and jobs[j]["tool"] is not None and jobs[j]["tool"].parallel and jobs[j]["error"] is None:
                j += 1
            if j - i >= 2:                 # 読むだけの道具が並んだら同時に走らせる
                results: list[Result | None] = [None] * (j - i)

                def work(k: int, job: dict) -> None:
                    results[k] = self._run_one(job)
                threads = [threading.Thread(target=work, args=(k, jobs[i + k]), daemon=True) for k in range(j - i)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join()
                for k in range(j - i):
                    r = results[k] or fail("the tool returned nothing", head=jobs[i + k]["name"])
                    self._finish_one(jobs[i + k], r, last=i + k == len(jobs) - 1)
                    images += r.images
                i = j
                continue
            r = self._run_one(jobs[i])
            self._finish_one(jobs[i], r, last=i == len(jobs) - 1)
            images += r.images
            i += 1
        for p in images:
            self.t.append({"role": "user", "content": f"[image] {p}", "bw_image": p, "bw_note": True})

    # ── 子エージェント ─────────────────────
    def spawn(self, prompt: str, kind: str, label: str, _try_other: bool = True) -> str:
        import dataclasses

        from . import childengine
        from . import prompt as _prompt
        from .tools.shell import resolve_shell
        keep = ("Bash", "BashOutput", "Read", "Glob", "Grep", "WebFetch", "WebSearch")
        if kind == "general":
            keep += ("Write", "Edit")
        tools = {k: v for k, v in self.tools.items() if k in keep}
        ctx = dataclasses.replace(self.ctx, todos=[], spawn=None, ask=None, read_files=set(self.ctx.read_files), seen=dict(self.ctx.seen),
                                  written=[], created=[], ran=[], child=True, started_jobs=[])
        # ★子の推論機が別にあれば、子はそこで動く（2026-11-25 オーナー「Gyro-M が子に命令を送って作業をさせて結果を受け取れるように」）。
        #   親の席を触らないので、席の保存と戻し（3〜15 秒）も、その食い違いによる親の読み直しも無い。無い・止まっていれば今までどおり。
        other = childengine.open_for(self.llm) if (_try_other and not self.child) else None
        llm = other or self.llm
        child = Agent(llm=llm, out=ChildOut(self.out, self.rid, label), rid=self.rid, transcript=MemoryTranscript(),
                      ctx=ctx, tools=tools, system=_prompt.child_system(self.ctx.workspace, llm.model, resolve_shell()[0]),
                      think=self.think, effort=self.effort, limits=self._next_child_limits or Limits(max_iters=_limit("LAPS_CHILD_MAX_ITERS")), child=True)
        child.child_kind = kind
        child.parent = self
        self._count("task_calls")
        # ★子は同じ推論機の席を使うので、親の控えが消えて次の手番で会話を頭から読み直していた（2026-11-16）。保存して戻す。
        # ★同じ手番で Task を続けて呼ぶと、2 本目の前の保存が「1 本目の子が使い終えた席」で親の控えを上書きし、
        #   親は戻した後に会話を頭から読み直していた（2026-11-18・子 1 が 15 万トークンまで育ち、親の 15.8 万を約 7 分読み直した）。
        #   まだ戻していない控えがあれば、それが親の物なので保存し直さない。
        first = False
        if not self.child and other is None:
            with self._lock:
                first = not self._slot_park and not self._slot_restore
                if first:
                    self._slot_park = "pending"
                elif self._slot_restore:
                    self._count("slot_park_kept")
        parked = ""
        retry_same = False
        try:
            if first:
                from . import slots
                parked = slots.park(self.llm, self.t.id, self.last_tokens, self._count)
            t_child = time.monotonic()
            if other is not None:
                with self._lock:
                    self._other_llms.append(other)
                self._count("child_other_engine")
            text = child.run(prompt)
            if other is not None and child.outcome == "backend_error" and child.iters <= 1 and not child.tool_calls:
                # 子の推論機が最初の要求から応えなかった（GPU の貸し出しで止まった・落ちた）。何もしていないので、親と同じ推論機でやり直す。
                childengine.forget(other.base_url)
                self._count("child_other_engine_fallback")
                retry_same = True
        finally:
            if first:
                self._slot_park = ""
                if parked:
                    self._slot_restore = parked
            if other is not None:
                with self._lock:
                    if other in self._other_llms:
                        self._other_llms.remove(other)
                if child.outcome == "backend_error":
                    childengine.forget(other.base_url)
                other.close()
        if retry_same:
            return self.spawn(prompt, kind, label, _try_other=False)
        for job in ctx.started_jobs:       # 子が背景に起こした仕事を残さない（知らせを受ける相手がいない）
            if ctx.jobs is not None and not job.done.is_set():
                ctx.jobs.kill(job)
        if self.aids:                      # 子の書き換えも親の書き換えとして数える（終える前の確かめが働くように）
            for p in dict.fromkeys(ctx.written):
                self.aids.ledger.seq += 1
                self.aids.ledger.edits.append((self.aids.ledger.seq, p))
        self.ctx.written.extend(ctx.written)
        self.ctx.created.extend(ctx.created)
        self.ctx.ran.extend(ctx.ran)
        self.effects += child.effects
        for k, n in child.tool_counts.items():
            self._count(f"child_tool_calls:{k}", n)
        rec = {"label": label[:80], "kind": kind, "engine": "other" if other is not None else "same",
               "iters": child.iters, "tool_calls": child.tool_calls,
               "ctx_max": child.last_tokens, "secs": round(time.monotonic() - t_child, 1), "result_chars": len(text),
               "outcome": child.outcome or "ok", "nudged": child._ctx_nudges, "compactions": child.compactions}
        self.children.append(rec)
        self._count("child_iters", child.iters)
        self._count("child_secs", int(rec["secs"]))
        self.out.notice("subagent", self.rid, child_event="done", child_label=label,
                        message=f"子: 終わり（{child.iters} ターン・{child.last_tokens // 1000}K トークン・{int(rec['secs'])} 秒）")
        if not text.strip() and child.outcome in ("backend_error", "crashed"):
            raise RuntimeError(f"the sub-agent failed: {child.out.error or child.outcome}")   # 成功に見せない（Task の失敗として親へ返る）
        if kind == "explore" and child.tool_calls:
            from ..oc import explore as ex
            if ex.scout_found_nothing(text):   # 「見る物が無い」は道具を呼ぶ前の答え。調べた後なら、見た物だけでも親へ渡す
                return "The helper wrote no summary. It looked at:\n" + _looked_at(child.t.raw)
        return _child_result(text, rec, self.rid)

    # ── 締め ──────────────────────────────
    def done_fields(self, text: str) -> dict:
        """done の中身。走行の記録（アプリの journal）がこの名前で読む。"""
        tot = self.llm.totals
        secs = round(time.monotonic() - self.started, 2)
        c = dict(self.counters)
        c["tools_offered"] = len(self.tools)
        for name in self.tools:
            c[f"offered:{name}"] = 1
        info = self.llm.info()
        c[f"backend:{os.path.basename(info.model_path or self.llm.model).lower()}"] = 1
        for k in ("gen_ms", "gen_tokens", "read_ms", "read_tokens", "cached_tokens"):
            if tot.get(k):
                c[f"engine:{k}"] = int(tot[k])
        if self.think_chars:
            c["think_chars_full"] = self.think_chars
            c["think_turns_full"] = self.iters
        gen_s = tot["gen_ms"] / 1000 if tot["gen_ms"] else 0
        total = tot["prompt_tokens"] + tot["completion_tokens"]
        # 字数からの見積もり（推論機は思考の分を分けて数えない）。★2026-11-16: 前は「字数 ÷ 2」で、英語の思考を約 2 倍に数えていた
        #   （11-09〜16 の記録で 345 万トークン＝生成の 92%・推論機のログでは 172 万＝48%）。かなと漢字は 1 字 1 トークン・ほかは 4 字で数える。
        think_tok = self.think_tokens
        written = list(dict.fromkeys(self.ctx.written))
        return {
            "outcome": self.outcome or "accepted", "text": text, "model": self.llm.model, "files_written": written,
            "changed": bool(written) or self.effects > 0 or bool(self.memory and self.memory.changed()), "compactions": self.compactions, "missing": [], "unrun": [], "iters": self.iters,
            "secs": secs, "tok_s": round(tot["completion_tokens"] / secs, 1) if secs else 0,
            "gen_tok_s": round(tot["gen_tokens"] / gen_s, 1) if gen_s else 0, "prompt_tokens": tot["prompt_tokens"],
            "answer_tokens": max(0, tot["completion_tokens"] - think_tok), "think_tokens": think_tok, "total_tokens": total,
            "completion_tokens": tot["completion_tokens"], "tokens_source": "backend", "finish_reason": self.finish_reason,
            "answer_chars": self.answer_chars, "think_chars": self.think_chars,
            "usage": {"prompt_tokens": tot["prompt_tokens"], "completion_tokens": tot["completion_tokens"], "total_tokens": total,
                      "calls": tot["calls"], "gen_tokens": tot["gen_tokens"], "gen_ms": int(tot["gen_ms"]),
                      "read_tokens": tot["read_tokens"], "read_ms": int(tot["read_ms"]), "cached_tokens": tot["cached_tokens"]},
            "tool_calls": dict(self.tool_counts), "counters": c,
            **({"children": list(self.children)} if self.children else {}),
        }
