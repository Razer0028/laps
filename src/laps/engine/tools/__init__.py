"""道具の土台。1 つの道具は「説明・引数・実行」の組（手本: opencode の tool/tool.ts）。

説明は短く保つ（小さいモデルは読み込みの量がそのまま時間になる）。道具の定義だけで何千トークンにも
なると、新しい会話と要約の後に毎回その分を読み直すことになる。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..session import _clean

MAX_LINES = 2000
MAX_BYTES = 32 * 1024


@dataclass
class Result:
    output: str
    ok: bool = True
    kind: str = "ok"               # ok / empty / error / denied（画面が見る）
    head: str = ""                 # 画面の行の見出し（パス・コマンドなど）
    tail: str = ""                 # 画面の行の末尾（「3 行」など）
    truncated: bool = False
    effect: bool | None = None     # 世界を変えたか（None は道具の既定）
    images: list[str] = field(default_factory=list)   # 添える画像のパス（見られるモデルにだけ渡す）
    shot: str = ""                 # 画面の行に出す写真（会話のフォルダの shots/）
    # 利用者に「見せる」画像（2026-11-25 オーナー「エージェントがチャット欄に画像を提示してくれる機能…ちっちゃく画像を作業の途中に
    #   表示しているだけ」）。画面はこの回答の終わりに、答えの下へ大きく並べる（モデルが ![]() を書かなくても出る）。
    #   [{"path": shots/ の中, "caption": 一言}]。作業の途中に撮った写真（Browser・Desktop の look）は入れない＝見本の shot だけ。
    show: list[dict] = field(default_factory=list)


def fail(message: str, kind: str = "error", head: str = "") -> Result:
    text = message if message.startswith("Error:") else "Error: " + message
    return Result(text, ok=False, kind=kind, head=head)


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, Any]
    run: Callable[[dict, "Ctx"], Any]
    required: tuple[str, ...] = ()
    parallel: bool = False         # 同時に走らせてよい（読むだけの道具）
    effect: bool = False           # 世界を変える道具（終わりの確かめが数える）

    def schema(self, progress: dict | None = None) -> dict:
        props = dict(self.params)
        required = list(self.required)
        if progress is not None:           # 必須にする（任意の欄だと書かない走行がある・2026-10-30 実測）。書かなくても走らせる。
            # ★先頭に置く。llama.cpp の文法は必須の引数を定義の順に先に書かせるので、末尾に置くと file_path の直後に progress を強制され、
            #   モデルは最後の引数を書いたと思って閉じる（2026-10-31 Qwen3.6・同じ問いで old_string を書いた回数 末尾 3/6・先頭 6/6）。
            props = {"progress": progress, **props}
            required.insert(0, "progress")
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": {
            "type": "object", "properties": props, "required": required}}}


@dataclass
class Ctx:
    """道具が使えるもの。走行（loop.py）が 1 つ作る。"""
    workspace: str
    root: Path                                         # 会話の置き場
    cancel: threading.Event
    preempt: threading.Event                           # 強制割り込み（いまの道具を止める）
    jobs: Any = None
    ask: Callable[[str, list, list], str] | None = None
    app_call: Callable[[str, dict, float], dict] | None = None
    app_caps: tuple = ()
    todos: list = field(default_factory=list)
    todos_touched: bool = False                        # この走行で予定表を書いた（終わりの確かめが見る）
    read_files: set = field(default_factory=set)
    seen: dict = field(default_factory=dict)           # 読んだ・書いた時のファイルの指紋（mtime_ns, size）。上書きの前に変わっていないかを見る
    written: list = field(default_factory=list)        # 書いたファイル（順番どおり）
    created: list = field(default_factory=list)        # 新しく作ったファイル
    ran: list = field(default_factory=list)            # 走らせた命令
    spawn: Callable[[str, str, str], str] | None = None   # 子エージェント（Task）
    mode: str = "general"
    vision: bool = False
    tool_counts: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)       # 道具が数えた物（loop が走行の counters へ移す・2026-11-22）
    child: bool = False                                # 子エージェントの中（背景に回さない・走行の終わりに止める）
    started_jobs: list = field(default_factory=list)   # この走行が起こした仕事
    has_message: Callable[[], bool] | None = None      # 利用者の新しい一言が届いている（待ちを切り上げる）
    tokens: int = 0                                    # いま推論機が読んでいる量（出力の上限を下げる）
    request: str = ""                                  # 利用者の依頼の原文
    changes: Any = None                                # この依頼の取り消しの控え（engine/undo.py の RunChanges）
    answer_only: bool = False                          # Ask モード: 質問に答えるために道具を使う（Bash は読むだけ・書く道具は渡さない）
    poll_hinted: bool = False                          # 眠って覗く命令（sleep 30; tail log）に待ち方を 1 回伝えた
    web_redirected: set = field(default_factory=set)   # Desktop の launch で開かずに Browser を案内した Web ページ（2 回目は開く）
    skill_gate: str = ""                               # スキルの門の段（engine/aids.py _set_gate・Skill の none が見る）

    def stopped(self) -> bool:
        return self.cancel.is_set() or self.preempt.is_set()


def limit_bytes(ctx: Ctx, default: int = MAX_BYTES) -> int:
    """道具の出力の上限。会話が長くなったら下げる（全文はファイルに残る）。
    2026-10-13 オーナー「長時間の作業にも耐えられるような設計に」: 108 分の走行で読み込み待ちが 41 分・上限いっぱいの結果 1 つで 37〜84 秒。"""
    try:
        kb = int(os.environ.get("LAPS_TOOL_OUTPUT_KB") or 0)
    except ValueError:
        kb = 0
    limit = max(4, min(kb, 1024)) * 1024 if kb else default
    if os.environ.get("LAPS_TOOL_OUTPUT_ADAPT", "1").strip() != "0":
        if ctx.tokens >= 50_000:
            limit = min(limit, 8 * 1024)
        elif ctx.tokens >= 30_000:
            limit = min(limit, 10 * 1024)
    return limit


def clip(ctx: Ctx, text: str, max_lines: int = MAX_LINES, max_bytes: int | None = None) -> tuple[str, bool]:
    """長い出力は頭と尻を残し、全文はファイルに置く（手本: opencode の tool/truncate.ts）。"""
    if max_bytes is None:
        max_bytes = limit_bytes(ctx)
    raw = text.encode("utf-8", "replace")
    lines = text.count("\n") + 1
    if lines <= max_lines and len(raw) <= max_bytes:
        return text, False
    saved = ""
    try:
        d = ctx.root / "tool-output"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}.txt"
        p.write_text(_clean(text), encoding="utf-8", errors="replace")   # 記録と同じく伏せて残す（モデルがディスクを探すと読める）
        saved = str(p)
    except OSError:
        pass
    keep_head = max_bytes * 2 // 5
    keep_tail = max_bytes - keep_head
    head = raw[:keep_head].decode("utf-8", "ignore")
    tail = raw[-keep_tail:].decode("utf-8", "ignore")
    head_lines = head.split("\n")[:max_lines // 2]
    tail_lines = tail.split("\n")[-(max_lines // 2):]
    hidden = max(0, lines - len(head_lines) - len(tail_lines))
    hint = f"\n\n... {hidden} lines cut ...\n"
    if saved:
        hint += f"Full output: {saved} (use Grep on it, or Read with offset)\n\n"
    return "\n".join(head_lines) + hint + "\n".join(tail_lines), True


def coerce(tool: Tool, inp: Any) -> dict:
    """モデルの引数の揺れを直す（文字列で来た真偽や数）。合わない鍵はそのまま道具に渡す。"""
    if not isinstance(inp, dict):
        return {}
    out = dict(inp)
    for k, spec in tool.params.items():
        if k not in out or not isinstance(spec, dict):
            continue
        v, t = out[k], spec.get("type")
        if t == "boolean" and isinstance(v, str):
            out[k] = v.strip().lower() in ("true", "1", "yes")
        elif t in ("integer", "number") and isinstance(v, str):
            try:
                out[k] = int(v) if t == "integer" else float(v)
            except ValueError:
                out.pop(k)
        elif t == "array" and isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except ValueError:
                pass
    return out


def adapt_names(tool: Tool, inp: dict) -> tuple[dict, list[tuple[str, str]]]:
    """道具に無い名前で来た引数を本来の名前へ読み替える（file→file_path など）。(読み替えた後の引数, (元, 先) の並び)。"""
    try:
        from ...oc.tools import _PARAM_SYNONYMS
    except Exception:
        return inp, []
    out = dict(inp)
    moved: list[tuple[str, str]] = []
    for target, alts in _PARAM_SYNONYMS.items():
        if target not in tool.params or out.get(target) not in (None, ""):
            continue
        for a in alts:
            if a in out and a not in tool.params and out[a] not in (None, ""):
                out[target] = out.pop(a)
                moved.append((a, target))
                break
    return out, moved


def base_tools(toolset: str = "full") -> dict[str, Tool]:
    from . import files, misc, shell, web
    if toolset == "talk":
        # ★チャットモード（2026-11-23 オーナー「チャットモードがほしい（作業フォルダとかいらないやつ）」）。作業フォルダーを持たない
        #   会話。ウェブで調べるだけで、ファイル・シェル・利用者への質問の道具は渡さない（相手は目の前で話している）。
        #   ★名前に注意: "chat" は画面の Ask モード（読む道具つき・歴史的な名前）。こちらは "talk"。
        #   ★2026-11-24 ウェブの画像を会話に出す Image（url だけ・写す先は会話の shots/）を足した。
        from . import image
        return {t.name: t for t in (web.WEBSEARCH, web.WEBFETCH, image.WEB_IMAGE)}
    if toolset in ("chat", "plan"):         # plan（計画モード・2026-11-19）も見る道具だけ
        # ★Ask モード（2026-11-11 オーナー「askモードのときにWeb検索以外もagentモードと同じように使えるように。ただし…質問に
        #   答えるためにツールを実行させる」）: 読む・探す・読むだけの命令・利用者に聞く。書く道具（Write・Edit・ApplyPatch・
        #   TodoWrite・Remember）・子のエージェント（Task）・取り寄せる Skill・機能の口（Tools）は渡さない。Bash は shell.bash が
        #   ctx.answer_only を見て readonly の決まりで判定する（policy.check_answer_command）。
        return {t.name: t for t in (web.WEBSEARCH, web.WEBFETCH, files.READ, files.GLOB, files.GREP, shell.BASH,
                                    shell.BASH_OUTPUT, misc.ASK)}
    core = [shell.BASH, files.READ, files.WRITE, files.EDIT, files.GLOB, files.GREP]
    if toolset == "focused":
        return {t.name: t for t in (*core, misc.TODO)}
    from . import ext, patch, task
    # ★名前を os.environ.get に直に書く（env の表の突き合わせ tests/_env_scan.py が読めるように・2026-11-19）
    off = {name for name, val in (("Skill", os.environ.get("LAPS_OC_SKILLS")), ("Task", os.environ.get("LAPS_TASK")),
                                  ("Construct", os.environ.get("LAPS_CONSTRUCT")))
           if (val or "1").strip().lower() in ("0", "off", "false", "no")}
    if not patch.enabled():
        off.add("ApplyPatch")
    return {t.name: t for t in (*core, patch.APPLY_PATCH, shell.BASH_OUTPUT, misc.TODO, web.WEBSEARCH, web.WEBFETCH, misc.ASK,
                                misc.SKILL, misc.CONSTRUCT, misc.REMEMBER, task.TASK, ext.TOOLS)
            if t.name not in off}
