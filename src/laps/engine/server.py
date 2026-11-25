"""線の窓口。`python -m laps.serve` から呼ばれる（アプリ・端末版・スマホの中継が同じ口で話す）。

受ける（stdin・1 行 1 JSON）: run / send・cancel・inject・answer・app_reply・ping・status・quit
出す（stdout）: wire.py の封筒。1 回の run は必ず done か（受け付けないとき）error kind=rejected で閉じる。"""
from __future__ import annotations

from laps import _envcompat
from laps import paths as _lp
import json
import os
import re
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..session import is_valid_session_id
from . import prompt as _prompt
from . import undo
from .aids import Aids
from .finish import Finish
from .llm import LLM
from .loop import Agent, Limits
from .memory import Memory
from .session import EventLog, Transcript, clean_json
from .tools import Ctx, base_tools
from .tools.misc import load_todos
from .tools.shell import Jobs, resolve_shell
from .wire import PROTOCOL, Out, log

DEFAULT_BACKEND = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "default"
#: 実行の設定はアプリが起動時の環境変数で決める。線の向こうからは変えられない（実行の可否をモデルの外に置くため）。
FORBIDDEN = {"until", "until_cmd", "policy", "yes", "env", "home", "sessions", "prompt_dir", "native_tools", "bench_clean"}
TOOLSETS = ("full", "focused", "chat", "plan", "talk")   # plan: 計画モード（2026-11-19）・talk: チャットモード（2026-11-23・作業フォルダーなし）
HANDS_OFF = ("chat", "plan", "talk")    # 作業場に書かない組（取り消しの控え・記憶・TODO を行わない）
MODES = ("general", "development", "modeling", "analysis")
GUIDANCE_MAX = 4000
#: スマホの「追う画面」が受け箱（inbox.ndjson）に書いてよい物。
LIVE_INBOX_TYPES = ("inject", "cancel", "answer")


class Rejected(Exception):
    pass


@dataclass
class Spec:
    id: str
    prompt: str
    seed: list = field(default_factory=list)
    session: str | None = None
    workspace: str = ""
    model: str = ""
    backend: str = ""
    toolset: str = "full"
    mode: str = "general"
    think: bool = True
    effort: str = ""
    max_iters: int = 0
    guidance: str = ""
    app_caps: tuple = ()
    notes: list = field(default_factory=list)
    source: str = ""               # 依頼を出した画面（"android"・"web"・"app" など）


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


_RID_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")


def parse_run(req: dict, seq: int) -> Spec:
    rid = str(req.get("id") or f"r{seq}")
    # ★走行の id は控えのフォルダの名前になる（undo/<id>）。「..」や「/」入りの id で会話のフォルダごと消せた（2026-11-16 全体のレビュー）。
    if not _RID_OK.fullmatch(rid) or ".." in rid:
        rid = f"r{seq}"
    bad = sorted(k for k in req if k.lower() in FORBIDDEN)
    if bad:
        raise Rejected(f"受け付けない鍵: {', '.join(bad)}（実行設定はアプリが起動時の環境変数で決める。要求では変えられない）")
    notes: list[str] = []
    seed: list[dict] = []
    msgs = req.get("messages")
    if isinstance(msgs, list):
        clean: list[dict] = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "system":
                notes.append("Ignored a system message")
                continue
            if role in ("user", "assistant"):
                clean.append({"role": role, "content": _text_of(m.get("content") or "")})
        if not clean or clean[-1]["role"] != "user":
            raise Rejected("messages の最後は role=user でなければならない")
        prompt = clean[-1]["content"]
        seed = clean[:-1]
    elif isinstance(req.get("prompt"), str):
        prompt = req["prompt"]
    else:
        raise Rejected("messages か prompt のどちらかが要る")
    if not prompt.strip():
        raise Rejected("空のタスクは受け付けない")
    ws = req.get("workspace")
    if ws is not None and (not isinstance(ws, str) or not ws.strip()):
        raise Rejected("workspace は空でない文字列で")
    toolset = str(req.get("tools") or os.environ.get("LAPS_TOOLSET") or "full").lower()
    if toolset not in TOOLSETS:
        raise Rejected("tools は " + " / ".join(TOOLSETS) + " のどれか")
    if toolset == "talk" and not ws:
        # ★チャットモード（2026-11-24 Android から）: 作業フォルダーを送らない会話は、記録の置き場の chat/ で走らせる（ゲートウェイの
        #   作業場を借りない）。talk の道具（base_tools）はファイルに触れない＝許可根の検査は要らない。道具の組はこの toolset で
        #   ここが決めるので、利用者が talk と名乗って別の道具を得ることはない（tests/test_talk_mode_20261123.py）。
        wsp = (_lp.home_dir() / "chat").resolve()
    else:
        wsp = Path(ws).resolve() if ws else Path(os.environ.get("LAPS_WORKSPACE") or os.getcwd()).resolve()
        root = os.environ.get("LAPS_SERVE_WORKSPACE_ROOT", "").strip()
        if root:
            r = Path(root).resolve()
            if r != wsp and r not in wsp.parents:
                raise Rejected(f"workspace が許可された根の外にある: {wsp}")
    mode = req.get("mode") or os.environ.get("LAPS_MODE") or "general"
    if mode not in MODES:
        raise Rejected("mode は " + " / ".join(MODES) + " で")
    try:
        max_iters = max(0, int(req.get("max_iters") or 0))
    except (TypeError, ValueError):
        raise Rejected("max_iters は整数で") from None
    think = req.get("think")
    if think is not None and not isinstance(think, bool):
        raise Rejected("think は真偽値で")
    effort = req.get("effort")
    if effort is not None and effort not in ("low", "high"):
        raise Rejected("effort は \"low\" か \"high\" で")
    for key in ("model", "backend", "session"):
        v = req.get(key)
        if v is not None and not isinstance(v, str):
            raise Rejected(f"{key} は文字列で")
    sid = req.get("session") or None
    if sid and not is_valid_session_id(sid):
        raise Rejected("session は英数字で始まる 1 要素の名前で（区切り・`..`・空白・改行は使えない）")
    guidance = str(req.get("guidance") or "").strip()
    if len(guidance) > GUIDANCE_MAX:
        notes.append(f"App guidance too long; used the first {GUIDANCE_MAX} of {len(guidance)} chars")
        guidance = guidance[:GUIDANCE_MAX]
    caps = req.get("app_caps")
    return Spec(id=rid, prompt=prompt, seed=seed, session=sid, workspace=str(wsp),
                model=str(req.get("model") or os.environ.get("LAPS_MODEL") or DEFAULT_MODEL),
                backend=str(req.get("backend") or os.environ.get("LAPS_BACKEND") or DEFAULT_BACKEND),
                toolset=toolset, mode=mode, think=True if think is None else think, effort=effort or "",
                max_iters=max_iters, guidance=guidance,
                app_caps=tuple(str(c) for c in caps) if isinstance(caps, list) else (), notes=notes,
                source=str(req.get("source") or ""))


def _net(target: str) -> dict:
    """推論機への経路（プロキシの有無）。値は伏せてある。"""
    try:
        from .. import netdiag
        return netdiag.summary(target)
    except Exception:
        return {"target": target, "platform": sys.platform, "proxy_env": {}, "proxy_applies": False, "proxy": "", "proxy_from_registry": False}


def _env_float(name: str) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or 0))
    except ValueError:
        return 0.0


class Server:
    def __init__(self, out: Out) -> None:
        self._rem_note = ""              # 覚え書きが変わったことを、次の依頼で渡す一言（_system が作る）
        self.out = out
        self.jobs = Jobs()
        self.pending: "queue.Queue[Spec | None]" = queue.Queue()
        self._queued: list[Spec] = []
        self._lock = threading.Lock()
        self._seq = 0
        self.current: Spec | None = None
        self.agent: Agent | None = None
        self.llm: LLM | None = None
        self._asks: dict[str, dict] = {}
        self._apps: dict[str, dict] = {}
        self._n = 0
        self._stop = threading.Event()
        self._inbox_path: Path | None = None
        self._inbox_thread: threading.Thread | None = None
        # 走行の準備中（current は立ったが agent がまだ無い間）に届いた止めると一言。agent ができたら渡す
        self._early_cancel = False
        self._early_inject: list[tuple[str, bool]] = []

    # ── 状態 ──────────────────────────────
    def status(self) -> dict:
        with self._lock:
            return {"busy": self.current.id if self.current else None, "queued": len(self._queued)}

    def ready(self) -> None:
        ws = os.environ.get("LAPS_WORKSPACE") or os.getcwd()
        backend = os.environ.get("LAPS_BACKEND") or DEFAULT_BACKEND
        toolset = os.environ.get("LAPS_TOOLSET") or "full"
        self.out.notice("ready", None, protocol=PROTOCOL, pid=os.getpid(), cwd=os.getcwd(),
                        policy=os.environ.get("LAPS_POLICY") or "trusted", workspace=ws, backend=backend,
                        model=os.environ.get("LAPS_MODEL") or "", toolset=toolset,
                        mode=os.environ.get("LAPS_MODE") or "general", net=_net(backend), native_tools=True,
                        tools=list(base_tools(toolset if toolset in TOOLSETS else "full")), engine="engine", **self.status())
        try:                               # プロキシ経由のときだけ画面にも出す（推論機に届かないときの切り分けの材料）
            from .. import netdiag
            info = netdiag.summary(backend)
            log(netdiag.describe(backend))
            if info.get("proxy_applies"):
                self.out.notice("backend_proxy", None, message=netdiag.describe(backend), **info)
        except Exception as e:
            log(f"宛先の診断に失敗: {e!r}")

    # ── 受ける ────────────────────────────
    def handle(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except ValueError as e:
            self.out.emit("error", None, kind="bad_request", message=f"読み取れない行: {e}", line=line[:200])
            return True
        t = obj.get("type")
        rid = obj.get("id")
        if t in ("run", "send"):
            self._accept_run(obj)
        elif t == "cancel":
            self._accept_cancel(obj)
        elif t == "inject":
            self._accept_inject(obj)
        elif t == "answer":
            self._accept_answer(obj)
        elif t == "app_reply":
            self._accept_app_reply(obj)
        elif t == "ping":
            self.out.notice("pong", rid, **self.status())
        elif t == "status":
            self.out.notice("status", rid, **self.status())
        elif t in ("quit", "shutdown"):
            self.out.notice("shutdown", rid)
            return False
        else:
            self.out.emit("error", rid, kind="unknown_command", message=f"不明な type: {t!r}")
        return True

    def _accept_run(self, obj: dict) -> None:
        self._seq += 1
        try:
            spec = parse_run(obj, self._seq)
        except Rejected as e:
            self.out.emit("error", str(obj.get("id") or f"r{self._seq}"), kind="rejected", message=str(e))
            return
        with self._lock:
            busy = self.current is not None or bool(self._queued)
            self._queued.append(spec)
            position = len(self._queued)
        if busy:
            self.out.notice("queued", spec.id, position=position, message="Waiting for the current task to finish")
        self.pending.put(spec)

    def _accept_cancel(self, obj: dict) -> None:
        rid = obj.get("id")
        t0 = time.monotonic()
        with self._lock:
            agent, cur = self.agent, self.current
            hit = next((s for s in self._queued if s.id == rid), None)
            if hit is not None:
                self._queued.remove(hit)
            elif agent is None and cur is not None and (not rid or rid == cur.id):
                self._early_cancel = True
        if hit is not None:
            self.out.notice("cancel", rid, accepted=True, applied="pending", dropped=1)
            self.out.emit("done", rid, outcome="cancelled", text="", session=hit.session)
            return
        if (agent is None and cur is None) or (rid and cur is not None and rid != cur.id):
            self.out.notice("cancel", rid, accepted=False, applied="failed", dropped=0)
            return
        # 止めるを押したら順番待ちの依頼も取り消す（押した人は全部止めたつもりでいる）
        with self._lock:
            dropped = list(self._queued)
            self._queued.clear()
        self.out.notice("cancel", rid, accepted=True, applied="running", dropped=len(dropped))
        if agent is not None:
            agent.cancel()
        for q in dropped:
            self.out.emit("done", q.id, outcome="cancelled", text="", session=q.session, secs=0.0)
        if dropped:
            self.out.notice("queue_cleared", rid, message=f"Also cancelled {len(dropped)} queued requests", dropped=[q.id for q in dropped])
        self._wake_asks()
        self.out.notice("cancel_applied", rid, applied="running", request_cancel_ms=round((time.monotonic() - t0) * 1000, 1),
                        sockets=0, inflight=1, connections=0, bash_killed=0, kill_failed=0)

    def _accept_inject(self, obj: dict) -> None:
        text = str(obj.get("text") or "").strip()
        force = bool(obj.get("force"))
        rid = obj.get("id")
        with self._lock:
            agent, cur = self.agent, self.current
            early = bool(text) and agent is None and cur is not None
            if early:
                self._early_inject.append((text, force))
        if not text:
            return
        if early:
            self.out.notice("injected", rid or cur.id, message=text[:400], force=force, source=str(obj.get("source") or ""))
            return
        if agent is None or cur is None:
            self.out.notice("inject_ignored", rid, message="No run in progress; this message was not delivered: " + text[:400], text=text[:400])
            return
        if not agent.inject(text, force=force):
            self.out.notice("inject_ignored", rid or cur.id, message="The task had just finished; this message was not delivered: " + text[:400], text=text[:400])
            return
        self.out.notice("injected", rid or cur.id, message=text[:400], force=force, source=str(obj.get("source") or ""))
        if force:
            self._wake_asks()

    def _accept_answer(self, obj: dict) -> None:
        aid = str(obj.get("ask") or obj.get("id") or "")
        with self._lock:
            slot = self._asks.get(aid)
        if slot is None or slot["ev"].is_set():
            self.out.notice("ask_expired", obj.get("id"), message="Question expired; continued without an answer")
            return
        slot["text"] = str(obj.get("text") or "")
        slot["ev"].set()

    def _accept_app_reply(self, obj: dict) -> None:
        cid = str(obj.get("call") or "")
        with self._lock:
            slot = self._apps.get(cid)
        if slot is not None:
            r = obj.get("result")
            slot["reply"] = r if isinstance(r, dict) else {"ok": bool(obj.get("ok")), "error": str(obj.get("error") or "")}
            slot["ev"].set()

    def _wake_asks(self) -> None:
        with self._lock:
            slots = list(self._asks.values())
        for s in slots:
            s["ev"].set()

    # ── 走行から呼ばれる ───────────────────
    def _ask(self, spec: Spec, ctx: Ctx, question: str, options: list, descs: list) -> str:
        with self._lock:
            self._n += 1
            aid = f"a{self._n}"
            slot = {"ev": threading.Event(), "text": ""}
            self._asks[aid] = slot
        self.out.emit("ask", spec.id, ask=aid, question=question, options=options, descriptions=descs)
        try:
            while not slot["ev"].wait(0.2):
                if ctx.stopped():
                    break
        finally:
            with self._lock:
                self._asks.pop(aid, None)
            self.out.notice("ask_closed", spec.id, ask=aid)
        if not slot["text"] and ctx.preempt.is_set():
            return "(The user did not answer and sent a new instruction instead.)"
        return slot["text"]

    def _app_call(self, spec: Spec, ctx: Ctx, op: str, args: dict, timeout: float) -> dict:
        with self._lock:
            self._n += 1
            cid = f"c{self._n}"
            slot = {"ev": threading.Event(), "reply": None}
            self._apps[cid] = slot
        try:
            self.out.emit("app_call", spec.id, call=cid, op=op, args=args)
            deadline = time.monotonic() + max(1.0, float(timeout))
            while not slot["ev"].wait(0.2):
                if ctx.stopped():          # 止めるが答えの待ちで遅れないように
                    return {"ok": False, "error": "the user stopped this"}
                if time.monotonic() >= deadline:
                    return {"ok": False, "error": f"the app did not answer within {int(timeout)} seconds"}
            return slot["reply"] or {"ok": False, "error": "empty answer from the app"}
        finally:
            with self._lock:
                self._apps.pop(cid, None)

    # ── 走らせる ──────────────────────────
    def worker(self) -> None:
        while not self._stop.is_set():
            spec = self.pending.get()
            if spec is None:
                return
            with self._lock:
                if spec not in self._queued:      # 待っている間に取り消された
                    continue
                self._queued.remove(spec)
                self.current = spec
                self._early_cancel, self._early_inject = False, []
            try:
                self._run(spec)
            except Exception as e:                # ここまで落ちても done は必ず出す（画面が固まらないように）
                import traceback
                traceback.print_exc()
                self.out.emit("error", spec.id, kind="crash", message=f"The agent crashed: {type(e).__name__}: {e}")
                self.out.emit("done", spec.id, outcome="crashed", text="")
            finally:
                with self._lock:
                    self.current = None
                    self.agent = None
                    self.llm = None
                self.out.tap = None

    @staticmethod
    def _system_key(t: Transcript, spec: Spec, toolset: str) -> tuple[str, str]:
        """前置きを作り直すかを決める鍵と、利用者の言語。"""
        # ★この依頼だけで決めない（日本語の会話で「ok」と送ると言語の行が消え、前置きが変わって全部読み直しになる）
        said = [spec.prompt] + [str(m.get("content") or "") for m in reversed(t.raw) if m.get("role") == "user" and not m.get("bw_note")]
        lang = next((x for x in map(_prompt.user_language, said) if x), "")
        return f"{spec.mode}|{toolset}|{lang}|{spec.guidance}|{_prompt.remembered_key()}", lang

    @staticmethod
    def _key_fields(key: str) -> tuple | None:
        """前置きの鍵から、作り直しを決める項目だけを取り出す（モード・道具の組・言語・覚えた事の印）。アプリの指示（guidance）は見ない。"""
        head = key.split("|", 3)
        if len(head) < 4:
            return None
        return head[0], head[1], head[2], head[3].rsplit("|", 1)[-1]

    def _ask_keeps_prefix(self, t: Transcript, spec: Spec) -> bool:
        """Ask の依頼を、Agent の前置きと道具の一覧で走らせるか。
        ★2026-11-13 の記録: Agent→Ask の切り替え 4 回すべてで会話を頭から読み直していた（53K〜154K トークン・138〜435 秒。質問の答えは
          30 秒）。前置きの書き出しと道具の一覧が Ask と Agent で違い、推論機の控えが 1 文字目から使えない。書く道具は実行のときに断る。
        ★2026-11-14: 新しい会話を Ask で始めたときも Agent の前置きにする（後で Agent に切り替えても読み直さない）。前の版が Ask の短い
          前置きで始めた会話だけは、そのまま続ける（LAPS_ASK_KEEP_PREFIX=0 で止める）。"""
        if spec.toolset == "plan":             # 計画モードは必ず Agent の前置き（計画の後に Agent で実行するときに読み直さない）
            return True
        if os.environ.get("LAPS_ASK_KEEP_PREFIX", "1").strip() == "0":
            return False
        if spec.toolset == "talk":
            # ★2026-11-25 オーナー「タスクを連続して頼んでもコンテキストの読み直しが発生する」: Agent（Ask・Plan も同じ前置き）で始めた
            #   会話をチャットに切り替えると、前置きと道具の一覧が 1 文字目から変わって頭から読み直していた。前の前置きのまま走らせる。
            #   新しい会話・チャットで始めた会話は、軽い前置きのまま（チャットから Agent へ移るときは道具が要るので作り直す）。
            try:
                stored = self._key_fields((t.root / "system.key").read_text(encoding="utf-8"))
            except OSError:
                return False
            if not stored or stored[1] not in ("full", "chat", "plan"):
                return False
            return True
        if spec.toolset != "chat":
            return False
        try:
            stored = self._key_fields((t.root / "system.key").read_text(encoding="utf-8"))
        except OSError:
            return True
        return not (stored and stored[1] == "chat")

    def _system(self, t: Transcript, spec: Spec, shell: str, progress: bool, toolset: str = "") -> str:
        """前置きは会話の間変えない（変えると推論機の控えが全部効かなくなる）。モードと道具の組が変わったときだけ作り直す。"""
        toolset = toolset or spec.toolset
        key, lang = self._system_key(t, spec, toolset)
        kp, sp = t.root / "system.key", t.root / "system.txt"
        self._rem_note = ""
        # ★アプリの指示（guidance）の違いでは作り直さない（2026-11-14 実機: アプリは追加の行を Agent にだけ足すので、Ask と Agent
        #   を切り替えるたびに鍵が変わり、20 万トークンの会話を頭から読み直していた）。会話の途中で変わるのは、作業のモード・道具の組・
        #   言語が変わったときだけ。
        # ★覚え書き（Remember）が変わっても作り直さない（2026-11-25 オーナー「タスクを連続して頼んでもコンテキストの読み直しが発生する」:
        #   覚え書きは前置きに入るので、1 行足すと次の依頼で会話を頭から読み直していた。覚え書きは全部の会話で共通なので、ほかの会話も
        #   次の依頼で読み直していた）。変わったことは、この依頼の直後の一言で渡し（_run が agent.start_notes に足す）、鍵の印だけを今に揃える。
        try:
            stored = self._key_fields(kp.read_text(encoding="utf-8"))
            now = self._key_fields(key)
            if stored and now and stored[:3] == now[:3] and sp.exists():
                if stored[3] != now[3]:
                    self._rem_note = _prompt.remembered_note()
                    kp.write_text(key, encoding="utf-8")
                return sp.read_text(encoding="utf-8")
        except OSError:
            pass
        text = _prompt.system(spec.workspace, spec.model, shell, mode=spec.mode, guidance=spec.guidance,
                              toolset=toolset, progress=progress, language=lang)
        try:
            sp.write_text(text, encoding="utf-8")
            kp.write_text(key, encoding="utf-8")
        except OSError:
            pass
        return text

    # ── 走行の写しと受け箱（スマホの「追う画面」） ──
    def _live_begin(self, t: Transcript, rid: str) -> None:
        """事象の写し（live.ndjson）を開き、受け箱（inbox.ndjson）を空にして見張る。
        アプリに待ち受けの口を作らずに、走っている会話をスマホで見て割り込める（2026-10-10 オーナー）。LAPS_LIVE=0 で止める。"""
        if os.environ.get("LAPS_LIVE", "1").strip() == "0":
            return
        inbox = t.root / "inbox.ndjson"
        try:
            inbox.write_text("", encoding="utf-8")
        except OSError as e:
            log(f"受け箱を用意できない: {e!r}")
            return
        self.out.tee_open(t.root / "live.ndjson", clean=clean_json, rid=rid)
        self._inbox_path = inbox
        if self._inbox_thread is None or not self._inbox_thread.is_alive():
            self._inbox_thread = threading.Thread(target=self._inbox_poll, name="bw-inbox", daemon=True)
            self._inbox_thread.start()

    def _inbox_poll(self) -> None:
        at: dict[str, int] = {}
        while not self._stop.is_set():
            time.sleep(0.3)
            inbox = self._inbox_path
            if inbox is None:
                continue
            key = str(inbox)
            try:
                size = inbox.stat().st_size
            except OSError:
                continue
            pos = at.get(key, 0)
            if size < pos:                 # 次の走行で作り直された
                pos = 0
            if size == pos:
                at[key] = pos
                continue
            try:
                with open(inbox, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read(size - pos)
            except OSError:
                continue
            cut = chunk.rfind(b"\n")       # 書きかけの最後の行は次の周へ
            if cut < 0:
                continue
            at[key] = pos + cut + 1
            for raw in chunk[:cut].split(b"\n"):
                self._inbox_line(raw)

    def _inbox_line(self, raw: bytes) -> None:
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(obj, dict) or obj.get("type") not in LIVE_INBOX_TYPES:
            return
        obj.setdefault("source", "remote")
        if obj["type"] == "cancel":        # 受け箱はいま走っている会話の物。相手の知っている id が古くても止める
            obj.pop("id", None)
        try:
            self.handle(json.dumps(obj, ensure_ascii=False))
        except Exception as e:
            log(f"受け箱の行を扱えない: {e!r}")

    def _run(self, spec: Spec) -> None:
        try:                               # 機能の道具と oc の部品は今いる場所を作業場として読む
            Path(spec.workspace).mkdir(parents=True, exist_ok=True)
            os.chdir(spec.workspace)
        except OSError as e:
            log(f"作業場へ移れない: {e}")
        _envcompat.setenv("LAPS_WORKSPACE", spec.workspace)
        _envcompat.setenv("LAPS_TOOLSET", spec.toolset)
        _envcompat.setenv("LAPS_MODE", spec.mode)
        llm = LLM(spec.backend, spec.model)
        t = Transcript(spec.session)
        _envcompat.setenv("LAPS_SESSION_FILE", str(t.path))     # モデルは Bash で自分の会話の記録を読み返せる
        if not t.has_history():
            for m in spec.seed:
                t.append(m)
        llm.effort_pin = _effort_pin(t.root, spec.effort)
        log_ = EventLog(t.root / "events.jsonl")
        self.out.tap = log_.write
        self._live_begin(t, spec.id)
        log_.write("run_spec", {"id": spec.id, "session": t.id, "prompt": spec.prompt[:64 * 1024], "prompt_chars": len(spec.prompt),
                                "model": spec.model, "toolset": spec.toolset, "workspace": spec.workspace,
                                "max_iters": spec.max_iters, "source": spec.source, "via": os.environ.get("LAPS_RUN_VIA") or ""})
        t.write_meta(id=t.id, model=spec.model, workspace=spec.workspace, started=time.time(), engine="engine", toolset=spec.toolset)
        if spec.toolset not in HANDS_OFF and undo.is_command(spec.prompt):
            self._run_undo(spec, t, llm, log_)     # 推論機を呼ばずに、直前の依頼の変更を戻す
            return
        keep_prefix = self._ask_keeps_prefix(t, spec)
        prefix = "full" if keep_prefix else spec.toolset
        tools = base_tools(prefix)
        cancel, preempt = threading.Event(), threading.Event()
        info = llm.info(fresh=True)
        ctx = Ctx(workspace=spec.workspace, root=t.root, cancel=cancel, preempt=preempt, jobs=self.jobs,
                  app_caps=spec.app_caps, mode=spec.mode, vision=info.vision, answer_only=spec.toolset in HANDS_OFF)
        ctx.ask = lambda q, o, d: self._ask(spec, ctx, q, o, d)
        if spec.toolset not in HANDS_OFF:   # 依頼ごとの取り消しの控え（engine/undo.py）
            ctx.changes = undo.begin(t.root, spec.id)
        if spec.app_caps:
            ctx.app_call = lambda op, a, to: self._app_call(spec, ctx, op, a, to)
        shell = resolve_shell()[0]
        system = self._system(t, spec, shell, prefix != "chat", toolset=prefix)
        limits = Limits(max_iters=spec.max_iters, max_seconds=_env_float("LAPS_MAX_SECONDS"),
                        max_tool_calls=int(_env_float("LAPS_MAX_TOOL_CALLS")))
        agent = Agent(llm=llm, out=self.out, rid=spec.id, transcript=t, ctx=ctx, tools=tools, system=system, think=spec.think,
                      effort=spec.effort, limits=limits)
        if keep_prefix:                        # 道具の一覧は Agent のまま見せ、そのモードの道具だけを走らせる
            agent.ask_allowed = frozenset(base_tools(spec.toolset if spec.toolset == "talk" else "chat"))
            agent.plan_mode = spec.toolset == "plan"
            agent.keep_mode = spec.toolset if spec.toolset == "talk" else ""
        if self._rem_note:                     # 覚え書きが変わった（前置きは作り直さずに、この依頼で渡す）
            agent.start_notes.append(self._rem_note)
            self._rem_note = ""
        ctx.spawn = agent.spawn
        ctx.has_message = lambda: bool(agent._inbox)
        if spec.toolset not in HANDS_OFF:   # 会話・計画のモードは作業場に触らない＝記憶も読み書きしない
            load_todos(ctx)
            agent.memory = Memory(spec.workspace, t.id, agent._count)
            agent.aids = Aids(agent)
            agent.finish = Finish(agent, agent.aids)
        with self._lock:
            self.agent, self.llm = agent, llm
            stop_now, early = self._early_cancel, self._early_inject
            self._early_cancel, self._early_inject = False, []
        for text, force in early:
            agent.inject(text, force=force)
        if stop_now:
            agent.cancel()
        self.out.notice("run_start", spec.id, session=t.id, model=spec.model, tools=list(tools), cwd=spec.workspace,
                        max_iters=spec.max_iters, backend=spec.backend, net=_net(spec.backend), prompt=spec.prompt[:2000], source=spec.source,
                        via=os.environ.get("LAPS_RUN_VIA") or "", mode=spec.mode, toolset=spec.toolset)
        for note in spec.notes:
            self.out.notice("request", spec.id, message=note)
        beat = threading.Thread(target=self._heartbeat, args=(spec, agent, llm), daemon=True)
        beat.start()
        try:
            from . import slots                # 別の会話の後なら、前の会話の席を保存し、この会話の保存を戻す（2026-11-16）
            agent.stage = "席の控え"
            slots.begin(llm, t.id, agent._count)
        except Exception as e:  # noqa: BLE001 — 控えの不具合で走行を止めない
            log(f"席の控えを扱えない: {e!r}")
        try:
            text = agent.run(spec.prompt)
        finally:
            agent.stage = ""
            try:
                slots.end(llm, t.id)
            except Exception:  # noqa: BLE001
                pass
        created = (agent.memory.new_files() if agent.memory else []) or list(dict.fromkeys(ctx.created))
        if created:
            top = ", ".join(os.path.basename(p) for p in created[:6])
            self.out.notice("harness", spec.id, message=f"New files: {len(created)}; {top}")  # 2026-11-20 オーナー「答えの下にまだ出る」→ 状態行だけ
        if ctx.changes is not None:
            try:
                changed = ctx.changes.finish()
            except Exception as e:             # 控えの不具合で走行の終わりを壊さない
                log(f"取り消しの控えを閉じられない: {e!r}")
                changed = []
            if changed:
                self.out.notice("changes", spec.id, files=changed, undo=True, message=undo.summary(changed, agent._ja()),
                                final=True)
        fields = agent.done_fields(text)
        t.write_meta(ended=time.time(), outcome=agent.outcome)
        t.close()
        llm.close()
        with self._lock:                  # done を読んだ相手がすぐ次を送っても「順番待ち」にならないよう、先に空ける
            self.current = None
            self.agent = None
            self.llm = None
        self.out.emit("done", spec.id, session=t.id, **fields)
        self.out.tap = None
        log_.close()

    def _run_undo(self, spec: Spec, t: Transcript, llm: LLM, log_: EventLog) -> None:
        """`/undo`: この会話の、変更があってまだ戻していない一番新しい依頼を戻す（推論機は呼ばない）。
        モデルが次に読めるよう、戻した事を記録に一言残す。利用者の `/undo` そのものは記録に入れない（依頼と読まれないように）。"""
        t0 = time.monotonic()
        ja = any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for m in t.raw[-20:] if m.get("role") == "user"
                 for c in str(m.get("content") or "")[:400])
        self.out.notice("run_start", spec.id, session=t.id, model=spec.model, tools=[], cwd=spec.workspace,
                        max_iters=0, backend=spec.backend, net=_net(spec.backend), prompt=spec.prompt[:2000], source=spec.source,
                        via=os.environ.get("LAPS_RUN_VIA") or "", mode=spec.mode)
        outcome = "accepted"
        try:
            cmd = undo.parse_command(spec.prompt) or {"since": None}
            if cmd["since"] is not None:       # アプリの「Rewind」: その発言から後の依頼を、新しい順に全部戻す
                runs = undo.undoable_since(t.root, cmd["since"])
            else:
                rc = undo.last_undoable(t.root)
                runs = [rc] if rc is not None else []
            if not runs:
                text = "戻せる変更がありません（この会話で、まだ戻していない依頼の変更が見つかりません）。" if ja else \
                       "There is nothing to undo in this conversation."
            else:
                res = undo.merge([rc.undo() for rc in runs])
                text = undo.report(res, ja)
                if len(runs) > 1:
                    text = (f"依頼 {len(runs)} 件ぶんの変更を戻しました。\n" if ja else f"Undid the changes of {len(runs)} requests.\n") + text
                t.append({"role": "user", "content": undo.model_note(res, len(runs)), "bw_note": True})
        except Exception as e:                 # 戻す途中の不具合は利用者へ言う（黙って半分だけ戻さない）
            outcome = "crashed"
            text = f"戻せませんでした: {type(e).__name__}: {e}" if ja else f"Could not undo: {type(e).__name__}: {e}"
        # ★戻している間に送られた一言は、走行が無いので届かない。黙って捨てずに言う（2026-11-16 全体のレビュー）。
        with self._lock:
            pending, self._early_inject = self._early_inject, []
        for said, _force in pending:
            self.out.notice("inject_ignored", spec.id, message="Sent while undoing; this message was not delivered: " + said[:400],
                            text=said[:400])
        self.out.emit("token", spec.id, text=text, iter=0)
        t.write_meta(ended=time.time(), outcome=outcome)
        t.close()
        llm.close()
        with self._lock:
            self.current = None
            self.agent = None
            self.llm = None
        self.out.emit("done", spec.id, session=t.id, outcome=outcome, text=text, model=spec.model, files_written=[],
                      changed=outcome == "accepted", compactions=0, missing=[], unrun=[], iters=0,
                      secs=round(time.monotonic() - t0, 2))
        self.out.tap = None
        log_.close()

    def _heartbeat(self, spec: Spec, agent: Agent, llm: LLM) -> None:
        """黙っている間も生きていると伝える（画面の無出力の見張りと、読み込みの進み具合の表示）。"""
        every = max(1.0, _env_float("LAPS_HEARTBEAT_SECS") or 5.0)
        dump_at = _env_float("LAPS_STALL_DUMP_SECS") if "LAPS_STALL_DUMP_SECS" in os.environ else 90.0
        next_at = every
        dumped = False
        while agent.stage:
            time.sleep(1)
            quiet = time.monotonic() - self.out.last
            if quiet < every:
                next_at = every
                continue
            if dump_at and not dumped and quiet >= dump_at and agent.stage == "推論機" and not llm.progress:
                dumped = True              # 読み込みの進みも来ずに黙っている＝固まっている疑い。あとで調べられるようにスタックを残す
                _dump_stacks(spec.id, quiet)
            if quiet < next_at:
                continue
            next_at = quiet + every
            stage = agent.stage or "推論機"
            if stage == "要約":
                detail = f"Writing summary ({getattr(agent, '_summary_chars', 0):,} chars)"
            elif stage == "背景の仕事":
                n = len(self.jobs.waiting())
                detail = f"{n} running" if n else ""
            elif stage == "推論機":
                detail = llm.progress
            else:
                # ★道具の段は「道具 <名前>」で送る（画面・ウェブ版・端末・Android との取り決め・旧い中核と同じ）。名前だけで送っていたので
                #   どの画面も「推論機を待っています」と出し、道具の中の長い待ち（BashOutput の wait）が考え中に見えた（2026-11-12）。
                stage, detail = f"道具 {stage}", ""
            self.out.notice("heartbeat", spec.id, stage=stage, quiet_secs=int(quiet), detail=detail)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            agent = self.agent
        if agent is not None:
            agent.cancel()
        self._wake_asks()
        self.jobs.kill_all()
        self.pending.put(None)


def _dump_stacks(rid: str, quiet: float) -> None:
    """全スレッドのスタックを stderr と <home>/stall-*.txt に書く（1 走行 1 回）。実機の「固まった」を後から調べるため。"""
    import faulthandler
    try:
        from ..session import sessions_dir
        p = sessions_dir().parent / f"stall-{time.strftime('%Y%m%d_%H%M%S')}-{rid}.txt"
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"quiet {int(quiet)} s\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh)
    except (OSError, ValueError):
        pass
    try:
        faulthandler.dump_traceback(file=sys.stderr)
    except (OSError, ValueError):
        pass


def _effort_pin(root: Path, effort: str) -> str:
    """この会話で Strata へ渡す思考の強さ。会話の最初の依頼の値を控え、後の依頼でも同じ値を返す（llm.effort_pin の注を読む）。
    アプリで Think を切り替えても、変わるのは思考の上限だけになる。"""
    p = root / "effort.pin"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        p.write_text(effort or "", encoding="utf-8")
    except OSError:
        pass
    return effort or ""


def _fix_streams() -> None:
    for name, kw in (("stdin", {"encoding": "utf-8", "errors": "replace"}),
                     ("stdout", {"encoding": "utf-8", "errors": "replace", "newline": "\n", "line_buffering": True}),
                     ("stderr", {"encoding": "utf-8", "errors": "replace"})):
        try:
            getattr(sys, name).reconfigure(**kw)
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _fix_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    ws = os.environ.get("LAPS_WORKSPACE")
    if ws:
        try:
            Path(ws).mkdir(parents=True, exist_ok=True)
            os.chdir(ws)
        except OSError as e:
            log(f"作業場へ移れない: {e}")
    out = Out()
    srv = Server(out)
    srv.ready()
    if "--probe" in args:
        out.close()
        return 0

    def _bye(*_a: Any) -> None:
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _bye)
    except (ValueError, OSError):
        pass
    _guard_process()
    threading.Thread(target=srv.worker, daemon=True).start()
    why = "入力が閉じた（アプリが止めた）"
    try:
        for line in sys.stdin:
            if not srv.handle(line):
                why = "終わりの依頼を受けた"
                break
    except KeyboardInterrupt:
        why = "Ctrl+C を受けた"
    except SystemExit:
        why = "SIGTERM を受けた"
    finally:
        log(f"終わる: {why}")          # ★黙って終わらない（2026-11-12 実機で走行の途中に消え、どう終わったか誰にも分からなかった）
        srv.shutdown()
        out.close()
    return 0


def _guard_process() -> None:
    """落ちた理由を残し、アプリが送らない合図では死なない（2026-11-12）。
    ・faulthandler: メモリ違反などで落ちたとき、Python のスタックを stderr（＝アプリの sidecar.log）へ書く。
    ・Windows でアプリから動かすとき（入力が端末でない）は Ctrl+C / Ctrl+Break を無視する。アプリは入力を閉じるか
      プロセスを止めて終わらせるので、この 2 つを受ける理由が無い。届けば黙って終わり、走行の途中で消えて見える。"""
    import faulthandler
    try:
        faulthandler.enable(file=sys.stderr)
    except (AttributeError, ValueError, OSError):
        pass
    if os.name != "nt":
        return
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return
    except (AttributeError, ValueError, OSError):
        return
    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
