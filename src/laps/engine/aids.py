"""走行の助け。決まった場面で、モデルの判断の材料になる事実を添える。

推論機は呼ばない（時間を足さない）。添えるのは依頼の写しと道具の結果だけで、前置きは変えない（変えると全部読み直しになる）。
中身は blackwall/oc の部品（disclose・whereis・hostload・failclass・reading・toolhints・bwtool・monitor）を
呼び、ここは「いつ・どこへ添えるか」だけを決める。走行（loop.py）から見える口はこのクラスだけ。どの口も失敗したら何もしない。
終わる前の確かめは finish.py。"""
from __future__ import annotations

import collections
import functools
import hashlib
import json
import math
import os
import re
import time
from typing import Any

from .llm import _env_float
from .memory import _EXIT_LINE
from .tools.shell import read_only, running_job

PLAN_EVERY = 8                     # 予定表を更新しないまま作業の道具をこの回数呼んだら一言
REVIEW_CALLS = 8                   # こちらが頼んだ見直しの道具の数の目安（10-31 の修正前の課題集で 4〜8 本）
PLAN_MAX = 6
STUCK_EVERY = 3                    # 同じ型の失敗の 6・9・12…回目で一度立ち止まらせる
STUCK_FIRST = 6
STUCK_WINDOW = 10                  # 直近 10 回のうち 8 回の失敗でも同じ
STUCK_RATE = 8
STUCK_BLOCK_MAX = 2
GIT_LOOK_NOTE = 6                  # git の様子見だけが続いたこの回数で一言（手本: Qwen Code の shell inspection stagnation・6 で一言／8 で止める）
REPEAT_MAX = 5                     # 同じ呼び出しが同じ結果を返した回数（連続でなくても）。次は走らせない
REA_NATIVE = (".exe", ".dll", ".so", ".dylib", ".sys", ".elf", ".o", ".node")
SHELF_SCRIPT = (".py", ".ps1", ".js", ".mjs", ".sh", ".bat", ".cmd", ".rb", ".pl", ".lua")
_LOOK_TOOLS = {"Read", "Glob", "Grep", "BashOutput", "WebSearch", "WebFetch", "Construct", "Skill"}
_VOLATILE = re.compile(r"\[Harness\].*|\[Host\].*|\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d+(?:\.\d+)?\s*(?:ms|s|秒|分)\b|\bbg\d+\b", re.M)
_BG_ID = re.compile(r"in the background as (bg\d+)")
_JOB_END = re.compile(r"Background job (bg\d+) finished with exit code (-?\d+)")
_OUT_END = re.compile(r"^\[(bg\d+) finished with exit code (-?\d+)\]")
_GIT_LOOK = re.compile(r"^git(?:\s+(?:-C\s+\S+|--no-pager|-c\s+\S+))*\s+(status|diff|ls-files)\b(.*)$", re.I | re.S)
_IMAGE_WORDS = re.compile(r"\.(?:png|jpe?g|gif|bmp|webp)\b|\b(?:image|picture|photo|screenshot)s?\b|画像|写真|スクリーンショット", re.I)


def git_look_only(command: str) -> bool:
    """鎖の全部が git の様子見（status・diff・ls-files）か。ファイルを指す diff・コミット・ほかの命令が混ざれば進んでいる。"""
    parts = [x.strip() for x in re.split(r"&&|\|\||[;&|\n]", command) if x.strip()]
    if not parts:
        return False
    for seg in parts:
        m = _GIT_LOOK.match(seg)
        if not m:
            return False
        if m.group(1).lower() == "diff":
            words = m.group(2).split()
            if any(not (w.startswith("-") or w in ("HEAD", "--")) for w in words):
                return False       # ファイルや範囲を指す diff は中身を見ている
    return True


def _on(name: str, default: str = "1") -> bool:
    return (os.environ.get(name) or default).strip().lower() not in ("0", "off", "false", "no")


#: 走行を止める・呼び出しを止める上限は既定で切る（2026-11-16 オーナー「同じ手の繰り返し…これなくして。Desktop ツールとか
#:   何回も使うと止まっちゃうみたい。完全に制限なくしていい。その他の制限も全部取り払って」）。環境変数で 1 にすれば戻る。
_LIMIT_OFF = "0"


def _quiet(default: Any):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, *a, **k):
            try:
                return fn(self, *a, **k)
            except Exception as e:
                self.a._count("aids_error")
                self.last_error = f"{fn.__name__}: {type(e).__name__}: {e}"
                return default
        return inner
    return wrap


def _time_limit() -> int:
    """呼び出し側が知っているこの走行の締め（秒）。0 は無し（アプリ・TUI は渡さない）。inf・nan・負も無し。"""
    v = _env_float("LAPS_TIME_LIMIT_SECONDS", 0.0)
    return max(0, int(v)) if math.isfinite(v) else 0


# 続きの一言（スキルの門を張らない）。新しい作業の文ではない物。
_FOLLOW_UP = re.compile(r"^(続けて|続き|続行|進めて|そのまま|お願い|おねがい|はい|うん|ok|okay|yes|go|continue|proceed|やって|それで)", re.I)

class Aids:
    def __init__(self, agent: Any) -> None:
        from ..oc import evidence, hostload
        self.a = agent
        self.last_error = ""
        self.ledger = evidence.Ledger()
        self.watch = hostload.Watch() if hostload.enabled() else None
        self.first_tool = True
        self._time_level = 0
        self.sys_changes: list[tuple[str, bool]] = []
        self.sys_gated = False
        # スキルの門（2026-11-16 オーナー「Skills を必ず読ませるようにしたい」「インターネット上の Skills を優先させて」）。
        #   "" = 無し／"search" = まず探す（skills.sh が先）／"pick" = 探した・読むか none を言う。
        self.skill_gate = ""
        self.skill_gate_blocks = 0
        self.since_plan = 0
        self.review_noted = False
        self.plan_notes = 0
        self.look_streak = 0
        self.look_noted = 0
        self.fail_counts: collections.Counter = collections.Counter()
        self.recent: collections.deque = collections.deque(maxlen=STUCK_WINDOW)
        self.last_ok = True                # 直近の道具が成功したか
        self.stuck_armed = False
        self.stuck_blocks = 0
        self.stuck_what = ""
        self.results: dict[tuple, tuple[int, str]] = {}
        self.pairs: collections.deque = collections.deque(maxlen=6)
        self.alt_pair: set = set()
        self.alt_noted = False
        self.git_streak = 0                # 続けて打った git の様子見（status・diff・ls-files）の数
        self.monitor_asked = False
        self.scripts_created = 0
        self.churn_noted = False
        self.rea_noted = False
        self.rea_down_noted = False
        self.dup_noted: set = set()
        self.keys_saved = False
        self.step_hints: set = set()
        self.lang_notes = 0
        self.lang_at = -100

    # ── 走行の始め ─────────────────────────
    @_quiet("")
    def begin(self, request: str) -> str:
        """始めの支度。依頼に添える一言（git の状態・試験の命令・読み方の注記・道具の案内・条件の書き出し・見直しの手順）を返す。"""
        from ..oc import bgwait, hostfacts, monitor, reading, selfreview, skilltool, toolhints
        from ..oc import tools as old
        from . import endgate
        from .tools.ext import LISTED
        a = self.a
        cwd = a.ctx.workspace
        old._AUTOLINT_RUNS[0] = 0
        monitor.begin_run()
        bgwait.begin_run()                 # App で起こした物（keep=false）はこの走行の終わりに止める
        names = set(a.tools)
        if "Bash" in names:
            self._launcher()
        parts: list[str] = []
        if "Bash" in names:
            for snap in (hostfacts.git_snapshot(cwd), hostfacts.project_commands(cwd)):
                # 同じ物を会話に積まない（続きの依頼で変わっていなければ添えない）
                if snap and not any(m.get("bw_note") and snap in str(m.get("content")) for m in a.t.raw[-400:]):
                    parts.append(snap)
        history = [m for m in a.t.raw[:-1] if m.get("role") in ("user", "assistant") and not m.get("bw_note")]
        if reading.enabled():
            rn, kinds = reading.notes(request, history, cwd)
            if rn:
                parts.append(rn)
                for k in kinds:
                    a._count(f"reading_note:{k}")
        gate = self._skill_gate_note(request, names)
        if gate:
            parts.append(gate)
        if toolhints.enabled():
            sk = skilltool.discover(cwd) if "Skill" in names and skilltool.enabled() else []
            hint, named = toolhints.suggest(a.first_request or request, names, sk, history, extra_names=set(LISTED) if "Tools" in names else set())
            if hint:
                parts.append(hint)
                for n in named:
                    a._count(f"tool_hint:{n}")
        req = (request or "").strip()
        img = self._image_hint(req)
        if img:
            parts.append(img)
        if (_on("LAPS_DONE_CRITERIA") and "TodoWrite" in names and len(req) >= 40
                and (endgate._MAKE.search(req) or endgate._GO.search(req))):
            a._count("criteria_note")
            parts.append("[Harness] Before you start, write down what must be true for this request to count as done: one "
                         "TodoWrite item per thing the user asked for (each feature, screen, branch, ending, file or number), "
                         "in the user's words. If the request asks for something to match an existing original, add an item "
                         "to compare the counts with the original. Work through the items, and check each one with a tool "
                         "before you finish.")
        # ★持ち時間を知らせる（LAPS_TIME_LIMIT_SECONDS）。旧い中核にあり、作り直しで落としていた（2026-11-02 の見直しで発見）。
        lim = _time_limit()
        if lim:
            parts.append(f"[Harness] This task has a time limit of {lim} s (about {max(1, round(lim / 60))} min). The run is "
                         "stopped then and only what is already written counts, so write the files and answer the request "
                         "asks for early, then improve them.")
        if selfreview.enabled() and selfreview.asks_for_review(req):
            a._count("review_mode")
            a.review_asked = True
            a.review_mark = a.tool_calls
            parts.append(selfreview.on_request(a.lang()))
            a.out.notice("review", a.rid, message="Review: reviewing adversarially (the user asked)")
        return "\n\n".join(parts)

    def time_note(self) -> str:
        """締めの半分と 8 割を過ぎた最初の道具の結果に 1 回ずつ足す一言（無ければ ""）。知らせるだけで止めない。"""
        lim = _time_limit()
        if lim <= 0:
            return ""
        spent = time.monotonic() - self.a.started
        level = 2 if spent >= lim * 0.8 else 1 if spent >= lim * 0.5 else 0
        if level <= self._time_level:
            return ""
        self._time_level = level
        self.a._count(f"time_note_{50 if level == 1 else 80}")
        return (f"[Harness] {int(spent)} s of this task's {lim} s time limit have passed ({max(0, lim - int(spent))} s left). "
                "If a file or answer the request asks for is not written yet, write your best version of it now, then keep "
                "improving.")

    @_quiet("")
    def on_message(self, text: str) -> str:
        """走行の途中の一言に添える物。変換違い・名前の打ち間違いの注記と、「レビューして」のときの見直しの頼み。"""
        from ..oc import reading, selfreview
        a = self.a
        parts: list[str] = []
        if reading.enabled():
            rn, kinds = reading.notes(text, [], a.ctx.workspace)
            if rn:
                parts.append(rn)
                for k in kinds:
                    a._count(f"reading_note:{k}")
        if selfreview.enabled() and selfreview.asks_for_review(text):
            a._count("review_mode")
            a.review_asked = True
            a.review_mark = a.tool_calls
            parts.append(selfreview.on_request(a.lang()))
            a.out.notice("review", a.rid, message="Review: reviewing adversarially (the user asked)")
        return "\n\n".join(parts)

    def resources(self) -> str:
        """考え直す場面（思考を切った後・要約の後・同じ失敗の繰り返し）に添える、手元の外で使える物の一行。
        前置きにも書いてあるが、会話が長いと遠くなる。"""
        names = self.a.tools
        bits: list[str] = []
        if "WebSearch" in names:
            bits.append("WebSearch")
        if not bits:
            return ""
        return ("Remember what you can use besides this machine: " + "; ".join(bits)
                + ". When you are guessing how something works or lack a tool, ask one of them instead.")

    @_quiet("")
    def monitors_left(self) -> str:
        """見張りを立てたまま終えようとしたら 1 回だけ確かめる（オーナー「監視中にエラーが出た場合などもモデルが対応できるように」）。
        終えると見張りは止まり、その後のエラーには誰も気づけない。"""
        from ..oc import monitor
        if self.monitor_asked:
            return ""
        running = monitor.running_labels()
        if not running:
            return ""
        self.monitor_asked = True
        self.a._count("monitor_nudge")
        return ("[Monitors still running: " + ", ".join(running) + "] They stop when you finish this reply, and nobody will see "
                "errors after that. If the task needs them (for example to catch errors from a program you started), call "
                "Monitor(wait=<seconds>) and deal with anything that arrives; if not, finish your answer.")

    @_quiet(False)
    def monitor_ready(self) -> bool:
        """見張りに届ける物があるか（読むだけ）。"""
        from ..oc import monitor
        return monitor.has_ready()

    def _image_hint(self, req: str) -> str:
        """依頼が画像を扱い、フォルダに画像があり、推論機が見られるなら一言（画像を見ずに Python を 19 手番書き続けた実例・2026-10-22）。"""
        from .tools.files import IMAGE_EXT
        a = self.a
        if not a.ctx.vision or not _IMAGE_WORDS.search(req):
            return ""
        cwd, found, dirs_seen = a.ctx.workspace, [], 0
        for root, dirs, files in os.walk(cwd):
            dirs_seen += 1
            if root[len(cwd):].count(os.sep) >= 2 or dirs_seen > 200:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", "venv")]
            found += [os.path.relpath(os.path.join(root, f), cwd) for f in sorted(files) if os.path.splitext(f)[1].lower() in IMAGE_EXT]
            if len(found) >= 5 or dirs_seen > 200:
                break
        if not found:
            return ""
        a._count("image_hint")
        return ("[Harness] Image files in the working folder: " + ", ".join(found[:5]) + ". You can see images: Read shows an image "
                "file to you as a picture. Look at the picture with Read before you write code to analyze it.")

    def _launcher(self) -> None:
        from ..oc import bwtool
        bwtool.ensure_launcher()

    # ── 道具の前 ──────────────────────────
    @_quiet("")
    def before_call(self, real: str, inp: dict, said: str) -> str:
        """システムの設定を変える呼び出しを、走らせる前に画面へ知らせる（2026-10-30 オーナー「進めていいです」＝止めずに知らせる）。
        返すのは走らせない理由（LAPS_SYSCHANGE_GATE=block のときだけ）。"""
        from ..oc import disclose
        hits = disclose.call_changes(real, inp)
        if not hits:
            return ""
        self.a._count("syschange", len(hits))
        mode = (os.environ.get("LAPS_SYSCHANGE_GATE") or "1").strip().lower()
        if mode in ("0", "off", "false", "no") or len(said.strip()) >= 6:
            return ""
        if mode == "block":
            if self.sys_gated:
                return ""
            self.sys_gated = True
            self.a._count("syschange_gate_blocked")
            return disclose.gate_message(hits)
        self.a._count("syschange_announced")
        self.a.out.notice("syschange", self.a.rid, message="Changing system settings outside the work folder: "
                          + "; ".join(f"`{c}`" for c in hits[:3]) + (f" (and {len(hits) - 3} more)" if len(hits) > 3 else ""))
        return ""

    def _key(self, name: str, inp: dict) -> tuple:
        return (name, json.dumps(inp, sort_keys=True, ensure_ascii=False, default=str))

    # ── スキルの門 ─────────────────────────
    def _set_gate(self, v: str) -> None:
        """門の段（Skill 道具も見る＝ctx に写す: 探す前の none に門の言葉を返すため）。"""
        self.skill_gate = v
        try:
            self.a.ctx.skill_gate = v
        except Exception:  # noqa: BLE001
            pass

    def _skill_gate_note(self, request: str, names: set) -> str:
        """新しい作業の始めに、スキルを探して読ませる（モデル任せにすると読まない: 0.1.680〜684 の 10 走行で勧めた 5 回のうち読んだのは 1 回）。
        ★インターネット（skills.sh）を先に探す（オーナー指示）。続きの一言（「続けて」など）と Ask と子では張らない。"""
        req = (request or "").strip()
        from . import endgate
        if ("Skill" not in names or not _on("LAPS_SKILL_GATE") or self.a.child or len(req) < 20 or req.startswith("/")
                or _FOLLOW_UP.match(req) or endgate.only_question(req)):     # 問いだけの依頼は作業ではない（2026-11-16 全体のレビュー）
            return ""
        self._set_gate("search")
        self.skill_gate_blocks = 0
        self.a._count("skill_gate_armed")
        return ("[Harness] Before any other tool, find the skills for this task:\n"
                "1. Skill(action=\"search\", q=\"<phrasing 1> | <phrasing 2> | <phrasing 3>\") — 2 to 4 different plain English "
                "phrasings of what the task needs (e.g. \"unity modding | bepinex plugin | harmony patch\"), so more candidates come back. "
                "The public skills.sh directory on the internet is searched first and preferred; each result says what it is for. "
                "q goes to the internet: generic topic words only, never the name of a game, product, person, host, path or file.\n"
                "2. Compare the candidates and load the one or two that fit best: Skill(action=\"read\", name=\"<owner/repo@skill>\") "
                "(name=\"a/b@x, c/d@y\" reads several), or Skill(name=\"<name>\") for "
                "one in the system prompt. Load more if the task has several parts.\n"
                "3. Only if nothing in the results fits, call Skill(name=\"none\").\n"
                "Then follow the loaded skill while you work. Other tools wait until you have done this.")

    def skill_gate_hold(self, real: str, inp: dict) -> str:
        """スキルの門で止める理由（無ければ ""）。★2026-11-16 オーナーが画面で「Error: Not run yet: load the skill …」を見た:
        門は失敗ではないので、走行は Error ではなく「止めた」（kind=denied）として返す（loop.py）。"""
        try:
            return self._skill_gate_block(real, inp)
        except Exception:  # noqa: BLE001 — 門の不具合で走行を止めない
            return ""

    def _skill_gate_block(self, real: str, inp: dict) -> str:
        if not self.skill_gate or real in ("Skill", "AskUserQuestion"):
            return ""
        self.skill_gate_blocks += 1
        if self.skill_gate_blocks > 2:          # 2 回止めても選ばないなら諦めて通す（走行を固めない）
            self.a._count("skill_gate_gave_up")
            self._set_gate("")
            return ""
        self.a._count("skill_gate_blocked")
        if self.skill_gate == "search":
            return ("Not run yet: first search for skills for this task with Skill(action=\"search\", q=\"<2-4 plain English topic "
                    "words>\") (skills.sh on the internet first), then load the best fit. Call this tool again after that.")
        return ("Not run yet: load the skill that fits best from the search results — Skill(action=\"read\", "
                "name=\"<owner/repo@skill>\") for skills.sh, Skill(name=\"<name>\") for a listed one — or call "
                "Skill(name=\"none\") if none fits. Then call this tool again.")

    def _skill_gate_after(self, real: str, inp: dict, r: Any) -> None:
        if not self.skill_gate or real != "Skill":
            return
        name = str(inp.get("name") or "").strip().lower()
        action = str(inp.get("action") or "").strip().lower()
        if action == "search" or (inp.get("q") and not name):
            if not getattr(r, "ok", False):     # 探す所が読めなかった＝選ぶ物が無い。止め続けない（2026-11-16）
                self._set_gate("")
                self.a._count("skill_gate_done:search_failed")
                return
            self._set_gate("pick")
            return
        if name == "none":
            if self.skill_gate == "search":
                return                          # 探さずに none は認めない（門の言葉で探させる）
            self._set_gate("")
            self.a._count("skill_gate_done:none")
            return
        if name or action == "read":
            # ★2026-11-16: 読もうとしたなら、読めなかった時（置き場所が違う・網が切れた・危ない形で渡さなかった）も門を開ける。
            #   読めなかった理由はその結果でモデルに届く。開けずにいると、次の道具が「Not run yet」で止まり続けた（オーナーが画面で見た）。
            self._set_gate("")
            outside = action == "read" or ("@" in name and "/" in name)
            where = "outside" if outside else "local"
            self.a._count("skill_gate_done:" + (where if getattr(r, "ok", False) else where + "_failed"))

    @_quiet("")
    def blocked(self, name: str, inp: dict, said: str) -> str:
        """走らせない理由（間を挟んだ繰り返し・往復・行き詰まりのまま説明なし）。手本: Qwen Code の loopDetection・OpenHands の stuck_detector。"""
        a = self.a
        if _on("LAPS_LOOP_GUARD", _LIMIT_OFF) and not (name == "BashOutput" and running_job(inp, a.ctx)):
            key = self._key(name, inp)
            n = self.results.get(key, (0, ""))[0]
            if n >= REPEAT_MAX:
                a._count("repeat_anywhere_blocked")
                return (f"You have made this exact call {n} times in this task and it returned the same result each time, so it "
                        "was not run. It will not change. Use what you already have, try something different, or say what is blocking you.")
            if self.alt_pair:
                if key in self.alt_pair:
                    a._count("alternate_blocked")
                    return ("You keep alternating between the same two calls and they keep returning the same results, so this "
                            "one was not run. Do something different now, or say what is blocking you.")
                self.alt_pair = set()
        if self.stuck_armed and _on("LAPS_STUCK_GATE", _LIMIT_OFF) and len(said.strip()) < 6:
            if self.stuck_blocks < STUCK_BLOCK_MAX:
                self.stuck_blocks += 1
                a._count("stuck_gate_blocked")
                return (f"Not run. {self.stuck_what} Before the next call, write three short lines to the user: what you are trying "
                        "to do, the exact error, and a different way to get there. Do not change one argument and try again.")
            self.stuck_armed = False
        if len(said.strip()) >= 6 and self.stuck_armed:
            self.stuck_armed = False
            a._count("stuck_gate_passed")
        return ""

    # ── 道具の後 ──────────────────────────
    def forget_repeats(self) -> None:
        """「同じ結果が続いた」の数えを戻す（ファイルを書き換えた・要約で中身を失った＝同じ呼び出しでも結果や意味が変わる）。"""
        self.results.clear()
        self.pairs.clear()
        self.alt_pair = set()

    @_quiet(None)
    def child_call(self, real: str, inp: dict, r: Any) -> None:
        """子の道具 1 回ぶん（子に Aids は付けない）。保存した鍵を伏せ、システムの設定の変更を最後の答えの一覧に足す。"""
        from ..oc import disclose
        self.mask(r)
        for c in disclose.call_changes(real, inp):
            self.sys_changes.append((c, r.ok))

    @_quiet(None)
    def mask(self, r: Any) -> None:
        """保存した API キーの値が結果に混ざっていたら伏せる。モデルには {{NAME}} だけが渡る。"""
        from .. import apikeys
        if apikeys.has_keys():
            masked, n = apikeys.redact(r.output)
            if n:
                r.output = masked
                self.a._count("apikey_masked", n)

    @_quiet(None)
    def after_tool(self, name: str, real: str, inp: dict, r: Any, secs: float, t0: float) -> None:
        """道具 1 回ぶんを見て、結果の頭と末尾に事実を添える。"""
        from ..oc import disclose, failclass
        a = self.a
        raw = r.output
        head: list[str] = []
        tail: list[str] = []
        self._skill_gate_after(real, inp, r)
        for c in disclose.call_changes(real, inp):
            self.sys_changes.append((c, r.ok))
        self._ledger(real, inp, r, raw)
        if real == "Bash" and raw.startswith("Still running after"):
            a._count("auto_bg")
            # 画面の帯に出る一言＝利用者あての文なので日本語（app/DESIGN.md §2.5・2026-11-15 実機の帯に英文が出ていた）。
            m = re.match(r"Still running after (\d+)", raw)
            a.out.notice("auto_bg", a.rid, message=(f"{m.group(1)} 秒を過ぎたので背景で続けています" if m else "背景で続けています"))
        cls = failclass.classify(real, r.ok, r.kind, raw)
        if cls:
            a._count(f"fail:{real}:{cls}")
        if r.ok and real in ("Write", "Edit"):
            self.forget_repeats()
        self._track_repeat(name, inp, raw, tail)
        self._git_look(real, inp, tail)
        self._track_failure(real, inp, raw, r.ok, tail)
        self._whereis(real, inp, r, raw, head, tail)
        self._churn(real, inp, r, tail)
        self._rea(real, inp, r, raw, tail)
        if self.watch is not None:
            line = self.watch.note(real, secs, self.first_tool)
            if line:
                a._count("host_note")
                if "Near the limit" in line:
                    a._count("host_low")
                    a.out.notice("host_low", a.rid, message=line.split("Near the limit: ", 1)[-1][:300])
                (head if r.ok else tail).append(line)
        self.first_tool = False
        self._plan(real, tail)
        self._review_long(tail)
        self._look(real, inp, tail)
        if real in ("Bash", "BashOutput"):
            self._bwtool_calls(t0)
        if real == "Api" and r.ok and inp.get("save_key"):
            self.keys_saved = True
        if head:
            r.output = "\n\n".join(head) + "\n\n" + r.output
        if tail:
            r.output = r.output + "\n\n" + "\n\n".join(tail)

    def _ledger(self, real: str, inp: dict, r: Any, raw: str) -> None:
        from ..oc import evidence
        before = len(self.ledger.checks)
        evidence.observe(self.ledger, real, inp, r.ok, raw)
        if real == "Bash":                 # 背景に回った確かめは、終わったときに数える
            m = _BG_ID.search(raw[:300])
            if m and evidence.is_check(str(inp.get("command") or ""), self.ledger.edited_names()):
                del self.ledger.checks[before:]
                self.ledger.pending[m.group(1)] = " ".join(str(inp.get("command") or "").split())[:160]
        elif real == "BashOutput":
            m = _OUT_END.match(raw)
            if m:
                self.job_ended(m.group(1), int(m.group(2)), raw)

    def job_ended(self, jid: str, code: int, text: str = "") -> None:
        from ..oc import evidence
        cmd = self.ledger.pending.pop(jid, None)
        if cmd:
            self.ledger.seq += 1
            self.ledger.checks.append({"seq": self.ledger.seq, "cmd": cmd, "ok": code == 0, "exit": code,
                                       "tail": "" if code == 0 else evidence._tail(text)})

    @_quiet(None)
    def job_note(self, note: str) -> None:
        m = _JOB_END.search(note)
        if m:
            self.job_ended(m.group(1), int(m.group(2)), note)

    def _track_repeat(self, name: str, inp: dict, raw: str, tail: list[str]) -> None:
        if not _on("LAPS_LOOP_GUARD", _LIMIT_OFF) or (name == "BashOutput" and running_job(inp, self.a.ctx)):
            return
        sig = hashlib.sha1(_VOLATILE.sub("", raw).encode("utf-8", "replace")).hexdigest()
        key = self._key(name, inp)
        n, old = self.results.get(key, (0, ""))
        self.results[key] = (n + 1 if old == sig else 1, sig)
        self.pairs.append((key, sig))
        p = list(self.pairs)
        if (len(p) == 6 and p[0] == p[2] == p[4] and p[1] == p[3] == p[5] and p[0][0] != p[1][0]):
            self.alt_pair = {p[0][0], p[1][0]}
            if not self.alt_noted:
                self.alt_noted = True
                self.a._count("alternate_noted")
                tail.append("[Harness] You have alternated between the same two calls three times and both keep returning the same "
                            "results. This is not making progress. Do something different.")

    def _git_look(self, real: str, inp: dict, tail: list[str]) -> None:
        """git の様子見（status・ファイルを指さない diff・ls-files）だけを続けて打っていたら一言。
        手本: Qwen Code の loopDetectionService（鎖の全部が様子見のときだけ数え、ほかの呼び出しで 0 に戻す）。"""
        if not _on("LAPS_LOOP_GUARD", _LIMIT_OFF):
            return
        if real == "Bash" and git_look_only(str(inp.get("command") or "")):
            self.git_streak += 1
        else:
            self.git_streak = 0
            return
        if self.git_streak == GIT_LOOK_NOTE:
            self.a._count("git_look_noted")
            tail.append(f"[Harness] Your last {self.git_streak} commands only looked at the repository (git status, diff or "
                        "ls-files) without acting on what they showed. Act on the result now, or say what is blocking you.")

    def _track_failure(self, real: str, inp: dict, raw: str, ok: bool, tail: list[str]) -> None:
        """同じ型の失敗の 3 回ごとに、それまでの試みを並べて一言。6 回目からは説明なしの次の呼び出しを止める。"""
        from ..oc import evidence
        self.recent.append(ok)
        self.last_ok = ok
        fired = ""
        if not ok:
            n, note = evidence.repeat_note(self.ledger, real, inp, _EXIT_LINE.sub("", raw))
            if note:
                self.a._count("repeat_failure")
                tail.append(note.strip())
                more = self.resources()
                if more:
                    tail.append(more)
            if n >= STUCK_FIRST and n % STUCK_EVERY == 0:
                fired = f"This is failure #{n} of the same kind."
            elif len(self.recent) == STUCK_WINDOW and sum(1 for x in self.recent if not x) >= STUCK_RATE:
                fired = f"{sum(1 for x in self.recent if not x)} of your last {STUCK_WINDOW} tool calls failed."
                self.recent.clear()
                tail.append(f"[Harness] {fired} Stop and look at the error itself: read the source, the documentation or the "
                            "state it depends on, or search for the exact message. Then take a different approach.")
        if fired and _on("LAPS_STUCK_GATE", _LIMIT_OFF):
            self.stuck_armed, self.stuck_what, self.stuck_blocks = True, fired, 0
            self.a._count("stuck_gate_armed")

    def _whereis(self, real: str, inp: dict, r: Any, raw: str, head: list[str], tail: list[str]) -> None:
        from ..oc import whereis
        cwd = self.a.ctx.workspace
        empty = real in ("Glob", "Grep") and raw.startswith(("No files found", "No matches found"))
        if not r.ok or empty:
            note = whereis.elsewhere_note(real, inp, cwd, remote=False)
            if note:
                self.a._count("whereis_elsewhere")
                tail.append(note.strip())
        if r.ok:
            note = whereis.content_note(real, inp, cwd)
            if note:
                self.a._count("whereis_content")
                head.append(note.strip())
            for fn, counter in ((lambda: whereis.duplicate_note(real, inp, cwd), "whereis_duplicate"),
                                (lambda: whereis.glob_duplicates_note(real, raw, cwd), "whereis_glob_dup")):
                note = fn()
                if note and note not in self.dup_noted:   # 同じ物は 1 走行 1 回
                    self.dup_noted.add(note)
                    self.a._count(counter)
                    head.append(note.strip())

    def _churn(self, real: str, inp: dict, r: Any, tail: list[str]) -> None:
        """使い捨てのスクリプトを 3 本書いても、データの構造も上流のコードも見ていない → 1 回だけ調べ方を添える（2026-10-10 オーナー
        「BlackwallはひたすらPythonスクリプトを作成していろいろ試しているようですが」→「導入してください」: API を推測で書いては走らせて外していた）。"""
        a = self.a
        path = str(inp.get("file_path") or "")
        if self.churn_noted or real != "Write" or not r.ok or not path.lower().endswith(SHELF_SCRIPT):
            return
        if not any(os.path.basename(p) == os.path.basename(path) for p in a.ctx.created):
            return                         # 書き直しは数えない
        self.scripts_created += 1
        if self.scripts_created < 3 or not a.llm.info().llama:
            return
        self.churn_noted = True
        a._count("script_churn_note")
        tail.append("[Harness] You have written several new throwaway scripts in this task. Before writing the next one: look at "
                    "the data itself with Read (on a binary it reports the structure; map=true and hex=true show what is where); "
                    "look up how the library really works in its source code or documentation instead of guessing the API; then keep "
                    "one script and change it with Edit.")

    def _rea(self, real: str, inp: dict, r: Any, raw: str, tail: list[str]) -> None:
        """REA を使うかをモデルが自分で決められるように、事実を 1 回ずつ添える（2026-11-04 オーナー「REA とかをモデルが自律的に
        使うか判断できるようにしたい」）。
        ①REA の得意なファイル（実行ファイル・APK・SQLite・IL2CPP の metadata）を Read で読んだ → その機械で動く REA の道具と呼び方。
        ②REA が provider_unavailable を返した → この機械では逆コンパイルの道具は動かない（同じ道具を試し直す空回りを止める）。
        ★使えと命じない。Read で足りる場面（ヘッダや文字列を見るだけ）もある。どちらも 1 走行に 1 回。"""
        from ..oc import mcp
        if real == "Mcp" and str(inp.get("server") or "") == "rea":
            self.rea_noted = True          # もう使っている＝①は要らない（Mcp は Tools の中から呼ぶので tool_counts には出ない）
        if real == "Mcp" and not self.rea_down_noted and str(inp.get("server") or "") == "rea" and "provider_unavailable" in raw:
            self.rea_down_noted = True
            self.a._count("rea_down_note")
            tail.append("[Harness] REA's native-code tools (binary_overview, list_procedures, search_procedures, "
                        "procedure_pseudo_code …) cannot run on this machine: no analysis provider (Ghidra) is set up. Retrying "
                        "them will fail the same way. Read with map=true, strings=true or hex=true shows a binary's structure and "
                        "text; REA's static readers (inspect_sqlite_database, inspect_il2cpp_metadata, inspect_managed_artifact, "
                        "inspect_apk_signature) still work.")
            return
        if self.rea_noted or real != "Read" or not r.ok:
            return
        path = str(inp.get("file_path") or inp.get("path") or "")
        low = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        kind = ("il2cpp" if low == "global-metadata.dat" else "sqlite" if low.endswith((".db", ".sqlite", ".sqlite3"))
                else "apk" if low.endswith((".apk", ".aab")) else "native" if low.endswith(REA_NATIVE) else "")
        if not kind:
            return
        info = mcp.rea_info()
        if not info:
            return
        call = 'Mcp(action="call", server="rea", tool="{t}", arguments={{"path": "<absolute path>"}})'
        if kind == "native" and info["native"]:
            what = (call.format(t="open_binary") + ", then binary_overview, search_procedures and procedure_pseudo_code"
                    "(procedure=...) decompile it")
        elif kind == "native":
            if not low.endswith(".dll") and not low.endswith(".exe"):
                return                     # ネイティブだけの形式で、この機械では逆コンパイルできない＝添える事実が無い
            what = (call.format(t="inspect_managed_artifact") + " reads it if it is a .NET assembly (types, methods). "
                    "Decompiling native code needs Ghidra, which is not set up here")
        else:
            what = call.format(t={"sqlite": "inspect_sqlite_database", "il2cpp": "inspect_il2cpp_metadata",
                                  "apk": "inspect_apk_signature"}[kind])
        self.rea_noted = True
        self.a._count("rea_note")
        tail.append(f"[Harness] REA (MCP server \"rea\") can analyse this file beyond what Read shows: {what}. Use it if you "
                    "need to know what is inside; Read is enough for headers and strings.")

    def _review_long(self, tail: list[str]) -> None:
        """こちらが頼んだ見直しが長くなったら 1 回だけ一言（2026-10-31 課題集: 見直しの道具は 12・19・12 本で 81〜265 秒。
        作業が終わった合格が見直しの間に時間切れになった・addflag）。利用者が頼んだ見直しには言わない。"""
        f = self.a.finish
        if f is None or f.review_at is None or self.review_noted or f.work_calls() - f.review_at < REVIEW_CALLS:
            return
        self.review_noted = True
        self.a._count("review_long_note")
        tail.append("[Harness] You have run enough checks for this change. Fix only what you found really wrong, then give the "
                    "✓/✗ list and finish. Do not start new checks.")

    def _plan(self, real: str, tail: list[str]) -> None:
        """予定表を作ったのに更新しないまま進んでいたら一言（2026-10-23 オーナー「プラン状況の更新が入るのがタスク終了時のみかも」）。"""
        if real == "TodoWrite":
            self.since_plan = 0
            return
        from .tools.misc import open_todos
        if not _on("LAPS_PLAN_REMIND") or not self.a.ctx.todos_touched or not open_todos(self.a.ctx):
            return
        self.since_plan += 1
        if self.since_plan >= PLAN_EVERY and self.plan_notes < PLAN_MAX:
            self.plan_notes += 1
            self.a._count("plan_stale_note")
            tail.append(f"[Harness] Your plan has not been updated for {self.since_plan} tool calls. If an item is done or the plan "
                        "changed, update it with TodoWrite now; the user reads it to follow your progress.")
            self.since_plan = 0

    def _look(self, real: str, inp: dict, tail: list[str]) -> None:
        """何も変えずに見るだけの呼び出しが続いたら、決めて動くよう一言（記録では最長 19 回連続・ふだんは 9 回まで）。"""
        if real in ("TodoWrite", "AskUserQuestion") or (real == "BashOutput" and running_job(inp, self.a.ctx)):
            return                         # 動いている仕事を待つのは「見るだけ」ではない
        look = real in _LOOK_TOOLS or (real == "Bash" and read_only(str(inp.get("command") or "")))
        if not look:
            self.look_streak = self.look_noted = 0
            return
        self.look_streak += 1
        try:
            every = int(float(os.environ.get("LAPS_LOOK_STREAK") or 0))    # 既定 0＝言わない（2026-11-16 上限を外す）
        except ValueError:
            every = 0
        if every > 0 and self.look_streak - self.look_noted >= every:
            self.look_noted = self.look_streak
            self.a._count("look_streak_nudge")
            tail.append(f"[Harness] Your last {self.look_streak} tool calls only looked at things without changing anything. From "
                        "what you have learned, decide the next concrete step and do it now. If you are blocked, tell the user "
                        "what you found and what is missing.")

    def _bwtool_calls(self, t0: float) -> None:
        """スクリプトの中から呼ばれた Blackwall の道具を数えて画面に出す。"""
        from ..oc import bwtool
        calls = bwtool.calls_between(t0 - 0.5, time.time() + 0.5, tag=bwtool.run_tag())
        if not calls:
            return
        by: dict[str, int] = {}
        for c in calls:
            k = str(c.get("tool") or "?")
            by[k] = by.get(k, 0) + 1
            self.a._count(f"bwtool:{k}")
        bad = sum(1 for c in calls if not c.get("ok"))
        # 画面の帯に出る（モデルには渡らない）＝日本語（2026-11-05 実機で英語の文がほかの日本語と並んでいた）。
        self.a.out.notice("bwtool", self.a.rid, message="スクリプトから使った道具: "
                          + ", ".join(f"{k} ×{v}" for k, v in sorted(by.items())) + (f"（失敗 {bad}）" if bad else ""))

    # ── 手番の区切り ──────────────────────
    @_quiet("")
    def step_note(self, rel_files: list[str], r: Any) -> str:
        """この手番で作った物の中身を確かめて一言（復号の誤りを利用者が教えるまで直せなかった・2026-10-12）。文書なら読み返せると添える。"""
        from ..oc import mediacheck
        a = self.a
        if not rel_files:
            return ""
        ws = a.ctx.workspace
        rel = lambda p: os.path.relpath(p, ws)  # noqa: E731
        out: list[str] = []
        docs = [f for f in rel_files if os.path.splitext(f)[1].lower() in (".docx", ".pptx", ".xlsx", ".pdf")]
        if docs and "doc" not in self.step_hints:
            self.step_hints.add("doc")
            a._count("doc_hint")
            out.append(f"[Documents were written in this step ({', '.join(docs[:3])}). The Doc tool can read them back (through "
                       'Tools(name="Doc")) to check what they really contain before you report them.]')
        n, bad, good = mediacheck.sample_check([os.path.join(ws, f) for f in rel_files])
        if n:
            a._count("media_auto_checked", len(bad) + len(good))
        if bad:
            a._count("media_auto_bad", len(bad))
            by: dict[str, list[str]] = {}
            for p, why in bad:
                by.setdefault(why, []).append(rel(p))
            lines = "\n".join(f"  - {', '.join(ps[:3])}{f' and {len(ps) - 3} more' if len(ps) > 3 else ''}: {why}" for why, ps in by.items())
            out.append(f"[Check of the files made in this step (media and data, by content) — {len(bad)} of {len(bad) + len(good)} checked "
                       f"(out of {n}) are broken:\n{lines}\nThe files were written but are not valid. Find why from the reason above "
                       "(where the data first goes wrong tells you which part of the conversion is wrong), fix the conversion on ONE "
                       "file, check that file, and only then run it on all files again.]")
        elif good:
            out.append(f"[Check of the files made in this step (media and data, by content) — {len(good)} of {n} checked, all valid: {good[0][1]}]")
            audio = [p for p, _ in good if os.path.splitext(p)[1].lower() in (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".opus", ".aac")]
            if audio and "listen" not in self.step_hints:
                self.step_hints.add("listen")
                a._count("listen_hint")
                out.append(f"[These are valid audio files, but a valid format and a volume level do not show what they hold. Listen to "
                           f"one or a few with the Listen tool (e.g. {rel(audio[0])}) to hear whether they are speech before you report them.]")
            pick = next((p for p, _ in good if os.path.splitext(p)[1].lower() in (".png", ".jpg", ".jpeg")), "")
            if pick and a.ctx.vision and "image" not in self.step_hints and os.path.getsize(pick) <= 8 * 1024 * 1024:
                self.step_hints.add("image")   # 開けても真っ黒・ノイズということはある。1 枚だけ見せる
                a._count("media_sample_shown")
                from .tools.files import keep_image
                r.images.append(keep_image(pick, a.ctx) or pick)   # 読んだ時点の写し（後で描き直されても前の画像のまま）
                out.append(f"[One of the new images ({rel(pick)}) is attached next so you can look at it: check that it shows a real "
                           "picture, not noise or a blank.]")
        return "\n".join(out)

    @_quiet("")
    def lang_note(self, text: str) -> str:
        """日本語の依頼なのに本文が英語に流れたら一言（要約の後の長い走行で実際に起きた・8 手番に 1 回・1 走行 4 回まで）。"""
        from ..oc import reading
        a = self.a
        if not a._ja() or not reading.english_only(text) or self.lang_notes >= 4 or a.iters - self.lang_at < 8:
            return ""
        self.lang_notes += 1
        self.lang_at = a.iters
        a._count("reply_lang_nudge")
        return ("[Harness] Your last message to the user was in English. The user writes in Japanese: write every message to the "
                "user in Japanese (your thinking may stay in English).")

    @_quiet("")
    def monitor_text(self) -> str:
        """見張り（Monitor）に新しく出た行を画面へ知らせ、モデルへ渡す文を返す。"""
        from ..oc import monitor
        events = monitor.take_events()
        if not events:
            return ""
        a = self.a
        a._count("monitor_deliveries", len(events))
        a._count("monitor_lines", sum(e["lines"] for e in events))
        for e in events:
            if e["alert"]:
                a._count("monitor_alerts")
            msg = f"Monitor {e['label']}"
            if e["errors"]:
                msg += f": {e['errors']} error lines ({e['lines']} new)"
            elif e["lines"]:
                msg += f": {e['lines']} new lines"
            if e["preview"]:
                msg += "\n" + "\n".join(x[:200] for x in e["preview"])
            if e["ended"]:
                msg += f"\n(monitor ended: {e['ended']})"
            a.out.notice("monitor_error" if e["alert"] else "monitor", a.rid, message=msg, iter=a.iters)
        return "\n\n".join(e["text"] for e in events)

    @_quiet(None)
    def scrub_saved_keys(self) -> None:
        """会話で渡された API キーを保存したら、それまでの発言から値を消す（記録と全文の控えも）。"""
        from .. import apikeys
        from ..oc import findings
        if not self.keys_saved:
            return
        self.keys_saved = False

        def walk(o: Any) -> Any:
            if isinstance(o, str):
                return apikeys.redact(o)[0]
            if isinstance(o, list):
                return [walk(x) for x in o]
            if isinstance(o, dict):
                return {k: walk(v) for k, v in o.items()}
            return o
        a = self.a
        if a.t.rewrite(walk):
            a._count("apikey_scrubbed_msgs")
        if findings.scrub_history(a.ctx.workspace, a.t.id, walk):
            a._count("apikey_scrubbed_history")
        a.request, a.first_request = walk(a.request), walk(a.first_request)

    # ── 走行の終わり ───────────────────────
    @_quiet(None)
    def close(self) -> None:
        from ..oc import bgwait, monitor
        n = monitor.end_run()
        if n:
            self.a._count("monitor_stopped_at_end", n)
            self.a.out.notice("monitor", self.a.rid, message=f"Stopped {n} monitors (run end)")
        stopped, _kept = bgwait.end_run()
        if stopped:
            self.a._count("bg_stopped_at_end", len(stopped))
            self.a.out.notice("harness", self.a.rid, message=f"Stopped {len(stopped)} apps started for this task (run end)")  # 2026-11-20 オーナー「答えの下にまだ出る」→ 状態行だけ
