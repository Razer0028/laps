"""文脈があふれたときの処理。手本: opencode の session/compaction.ts と core/session/compaction.ts。

順番: ①古い道具の出力を片付ける → ②足りなければ要約する。最近のやり取りはそのまま残す。
要約の手番に道具は渡さない。要約を書けなかったら、記録から機械で作った要約で続ける（走行を止めない）。"""
from __future__ import annotations

import json
import os
from typing import Callable

from ..oc import qwencall
from .llm import LLM, BackendError, Cancelled, ContextOverflow, estimate_tokens

RESERVE = 20_000                # 窓のうち、次の生成のために空けておく量
PRUNE_PROTECT = 40_000          # 新しい道具の出力はこの量まで片付けない
PRUNE_MINIMUM = 20_000          # これより少ししか減らないなら片付けない（読み直しの方が高くつく）
TOOL_OUTPUT_CHARS = 2_000       # 要約へ渡す道具の出力の長さ
THINK_CHARS = 300               # 要約へ渡す思考の頭と尾の長さ（結論は長い思考では頭に、短い思考では尾に出る）
#: 要約の本文の上限。★2026-11-09 に 4,096 → 6,144: Gyro-M の要約は本文が 4 千トークンを超え、続きとして頼む要約（思考 1,024＋本文）が
#: 11-05〜11-09 に 10 回上限で切れて捨てられ、文字にまとめ直す形で頼み直していた（1 回あたり 3.5〜12 分余計・頼み直しも 4,087 などで上限に届きかけた）。
SUMMARY_TOKENS = 6_144
SUMMARY_THINK = 1_024            # 続きとして頼む要約の思考の上限
SUMMARY_TOKENS_CLAUDE = 16_384   # Claude は思考も上限に入る（旧い中核の 4,608 で Opus の要約が 94 回切れた）

TEMPLATE = """Output exactly this Markdown structure and keep the section order. Do not add anything before or after it.

## Objective
- [what the user is trying to accomplish, in one or two sentences]

## Important Details
- [constraints, preferences, decisions and why, facts needed to continue, or "(none)"]

## Work State
### Completed
- [finished work and verified facts, or "(none)"]

### Active
- [current work and partial changes, or "(none)"]

### Blocked
- [blockers, failing commands, unknowns, or "(none)"]

## Verified Facts
- [things you confirmed with tool output: paths, versions, commands that worked, measured values; or "(none)"]

## Dead Ends
- [approaches you tried that did not work, and why; or "(none)"]

## Next Move
1. [the immediate concrete action, or "(none)"]

## Relevant Files
- [path: why it matters, or "(none)"]

Rules: keep every section. Use terse bullets. Keep exact file paths, commands, error strings, URLs and identifiers. Keep per-item results the assistant already stated (file: value). Tool results here are cut at 2,000 characters: never state a count or an absence you cannot see in them. Do not mention the summary process."""

UPDATE = """The <prior-summary> covers everything before the <conversation>. Write a new summary that combines both. Carry forward objectives, constraints and decisions; where they conflict, the conversation wins. Move finished work to Completed."""

#: 会話の続きとして要約を頼むときの一言（文字にまとめ直さない＝読み込み済みの分をそのまま使える）。
CONTINUE_ASK = """[Harness] The conversation has grown long, so this turn is a summary. Do not call any tool and do not continue the work now. Summarize the whole conversation above, including any earlier summary at its start, so that you can continue from the summary alone. The most recent messages will stay available as they are.

"""

CONTINUE = "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."


def usable(n_ctx: int) -> int:
    try:
        at = int(os.environ.get("LAPS_COMPACT_AT") or 0)
    except ValueError:
        at = 0
    if at >= 8192:                     # 窓の手前で要約を始める（読み込みが遅い機械で手前に置くとき）
        return min(at, n_ctx) if n_ctx else at
    # ★小さい窓（子の推論機の 3.2 万など）では空ける量を窓の 4 分の 1 までにする（2026-11-25・2 万を空けると 1.3 万で要約が始まる）。
    #   8 万以上の窓は今までどおり 2 万。
    return max(0, n_ctx - min(RESERVE, n_ctx // 4))


def overflowing(tokens: int, n_ctx: int) -> bool:
    return bool(n_ctx) and tokens >= usable(n_ctx)


#: タスクの境目で要約を始める量（2026-11-25 オーナー「会話が続くとどんどん蓄積されていってしまうので Claude アプリみたいな感じの仕組みに」）。
#: ★それまでは窓の手前（約 24 万）まで何もせず、道具の出入りと思考がたまり続けていた（開発機 の 190 万字の会話で道具の結果 42%・引数 29%・
#:   思考 22%・本文 1.4%）。手元の推論機は前を書き換えるとそこから後ろを全部読み直すので、作業の途中ではやらず、新しい依頼を受けた時に
#:   1 回だけ要約して約 2 万に戻す（続きの形の要約は読み込み済みの分をそのまま使う＝要約を書く時間と残した分の読み直しだけ）。
EARLY_AT = 100_000


def early_at(n_ctx: int) -> int:
    """境目の要約を始める量。0 は使わない（LAPS_COMPACT_EARLY=0・窓が小さくて非常用の要約より後になる時）。"""
    raw = os.environ.get("LAPS_COMPACT_EARLY")
    try:
        at = int(raw) if raw not in (None, "") else EARLY_AT
    except ValueError:
        at = EARLY_AT
    if at <= 0:
        return 0
    at = max(at, 16_000)
    u = usable(n_ctx) if n_ctx else 0
    return at if not u or at < u else 0


def boundary_due(tokens: int, n_ctx: int, llm) -> bool:
    """新しい依頼の頭で要約するか。手元の推論機だけ（Claude などは控えの仕組みが違い、窓の手前の要約のまま）。"""
    at = early_at(n_ctx)
    return bool(at) and tokens >= at and bool(getattr(llm.info(), "llama", False))


def ring_at(n_ctx: int, llm) -> int:
    """画面の輪の「自動圧縮まで」の分母。境目の要約が効く推論機ならその量、ほかは窓の手前。"""
    at = early_at(n_ctx) if getattr(llm.info(), "llama", False) else 0
    return at or usable(n_ctx)


def _size(m: dict) -> int:
    n = estimate_tokens(str(m.get("content") or "")) + estimate_tokens(str(m.get("reasoning_content") or ""))
    if m.get("tool_calls"):
        n += estimate_tokens(json.dumps(m["tool_calls"], ensure_ascii=False))
    return n


def prune(transcript) -> int:
    """最近の 2 依頼より前の道具の出力を、新しい順に数えて、PRUNE_PROTECT を超えた分を片付ける。"""
    raw = transcript.raw
    done: set[int] = set()
    for m in raw:
        if "bw_prune" in m:
            done.update(int(x) for x in m["bw_prune"])
    last = transcript.last_compaction()
    floor = int(last[1]["bw_compaction"].get("keep_from") or 0) if last else 0
    total = saved = turns = 0
    marks: list[int] = []
    for i in range(len(raw) - 1, floor - 1, -1):
        m = raw[i]
        if m.get("role") == "user":
            turns += 1
        if turns < 2 or m.get("role") != "tool" or i in done:
            continue
        n = estimate_tokens(str(m.get("content") or ""))
        total += n
        if total > PRUNE_PROTECT:
            saved += n
            marks.append(i)
    if saved < PRUNE_MINIMUM:
        return 0
    transcript.mark_pruned(marks)
    return saved


def _tail_start(raw: list[dict], floor: int, budget: int) -> int:
    """残す部分の始まり。道具の呼び出しと結果の組を割らないよう、assistant の発言で始める。"""
    used = 0
    cut = len(raw)
    for i in range(len(raw) - 1, floor - 1, -1):
        if raw[i].get("role") == "system":
            continue
        used += _size(raw[i])
        if used > budget:
            break
        cut = i
    for i in range(cut, len(raw)):
        if raw[i].get("role") == "assistant":
            return i
    return len(raw)


def _thought(text: str) -> str:
    t = text.strip()
    return t if len(t) <= THINK_CHARS * 2 else f"{t[:THINK_CHARS]} […] {t[-THINK_CHARS:]}"


def _serialize(messages: list[dict]) -> str:
    """要約へ渡す文字列。★道具を呼ぶ手番に本文を書かないモデルは、途中の結果（ファイルごとの数など）が思考にしか無い。ここで落とすと
    要約者は書きようがなく、要約の後に読み直す（2026-10-31 課題集 recall: 子が 12 本を 27 回読み、要約 3 回で 582 秒・表は 7/12）。"""
    rows: list[str] = []
    for m in messages:
        role = m.get("role")
        text = str(m.get("content") or "")
        if role == "user":
            rows.append(f"[User]: {text}")
        elif role == "assistant":
            if text:
                rows.append(f"[Assistant]: {text}")
            if m.get("tool_calls") and m.get("reasoning_content"):
                rows.append(f"[Assistant thinking, unverified]: {_thought(str(m['reasoning_content']))}")
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                rows.append(f"[Assistant tool call]: {fn.get('name')}({str(fn.get('arguments'))[:1000]})")
        elif role == "tool":
            cut = text if len(text) <= TOOL_OUTPUT_CHARS else text[:TOOL_OUTPUT_CHARS] + "\n[truncated]"
            rows.append(f"[Tool result]: {cut}")
    return "\n".join(rows)


#: 要約で消える側にある利用者の発言を、原文のまま残す量（手本: Codex の core/src/compact.rs の COMPACT_USER_MESSAGE_MAX_TOKENS＝2 万）。
#: ★手元の推論機は読み込みが重いので小さくした。途中の訂正や細かい指定は要約に溶けて消えやすい（2026-11-11 Codex との突き合わせ）。
USER_WORDS_TOKENS = 4_000
USER_WORD_CHARS = 2_000            # 1 つの発言の上限（越えたら頭だけ）


def _user_words(raw: list[dict], keep: int, request: str) -> str:
    """要約する側（keep より前）の利用者の発言を、新しい方から上限まで取り、古い順に並べる。前置きの一言・要約の印・
    いまの依頼（request に入っている物）は除く。途中の一言は枠を外して本人の文だけにする。"""
    from .loop import INTERJECTION_HEAD
    have = " ".join((request or "").split())
    picked: list[str] = []
    left = USER_WORDS_TOKENS
    for m in reversed(raw[:keep]):
        if m.get("role") != "user" or m.get("bw_note") or "bw_compaction" in m:
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(str(p.get("text") or "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        text = str(c or "").strip()
        if text.startswith(INTERJECTION_HEAD):
            text = text[len(INTERJECTION_HEAD):].split("\n\n(The request you were working on:", 1)[0].strip()
        flat = " ".join(text.split())
        if not flat or flat in have:
            continue
        if len(text) > USER_WORD_CHARS:
            text = text[:USER_WORD_CHARS] + "…"
        n = estimate_tokens(text)
        if n > left:
            break
        left -= n
        picked.append(text)
    if not picked:
        return ""
    picked.reverse()
    return "The user's earlier messages in this conversation, verbatim (oldest first):\n" + "\n".join(f"- 「{t}」" for t in picked)


def _mechanical(head: list[dict], prior: str, request: str) -> str:
    """要約を書けなかったときの代わり。依頼と、触ったファイルと、最後の道具の記録だけは失わない。"""
    asks = [str(m.get("content") or "")[:400] for m in head if m.get("role") == "user"][-5:]
    calls: list[str] = []
    for m in head:
        for c in m.get("tool_calls") or []:
            fn = c.get("function") or {}
            calls.append(f"{fn.get('name')}({str(fn.get('arguments'))[:160]})")
    parts = ["## Objective", f"- {request[:600]}", "## Work State", "### Completed", "- (summary could not be written; see below)"]
    if prior:
        parts += ["## Earlier summary", prior[:4000]]
    if asks:
        parts += ["## Earlier requests"] + [f"- {a}" for a in asks]
    if calls:
        parts += ["## Last tool calls"] + [f"- {c}" for c in calls[-25:]]
    return "\n".join(parts)


def continued_messages(msgs: list[dict], wrap: tuple[str, str] | None) -> list[dict]:
    """いま送っている発言の後ろに、要約の依頼を新しい発言として足す。
    ★前の発言に書き足さない: Strata は「次の応答の始まり」と 16K ごとにしか控えを取らない（DETAILS.md 687 行）。
      最後の道具の結果に書き足すとその控えより前が変わり、ゼロから読み直す（2026-11-03 実測 cache_n 0・1.1 万トークン 83 秒）。
    wrap は Qwen 系の書式が利用者の発言と数えない包み（数えると前の思考を描かなくなる）。"""
    ask = CONTINUE_ASK + TEMPLATE
    out = list(msgs)
    if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], str) and not wrap:
        out[-1] = {**out[-1], "content": out[-1]["content"] + "\n\n" + ask}
    else:
        out.append({"role": "user", "content": (wrap[0] + ask + wrap[1]) if wrap else ask})
    return out


def continued_enabled(llm: LLM, forced: bool) -> bool:
    """会話の続きとして要約を頼むか。あふれた後（forced）は続きが入らないので使わない。Claude は控えの仕組みが違う。"""
    return not forced and llm.info().llama and os.environ.get("LAPS_COMPACT_CONTINUE", "1") != "0"


def _continued(body: dict, llm: LLM, tick: Callable[[str], None]) -> tuple[str, str]:
    """続きとして頼んだ要約。(要約, だめだった理由)。形になっていない・道具を呼んだ・失敗したなら要約は空。"""
    try:
        turn = llm.stream(body, on_text=tick, watch=False)
    except (BackendError, ContextOverflow) as e:
        return "", "overflow" if isinstance(e, ContextOverflow) else "backend"
    text = turn.text.strip()
    # ★切れた・崩れた・本文に道具呼び出しを書いた要約を採ると、古い発言を捨てたうえ欠けたまま続く。
    if turn.finish == "length":
        return "", "length"
    if turn.calls or qwencall.unread(text) is not None:
        return "", "called_tool"
    if turn.cut or turn.degenerate:
        return "", "broken"
    return (text, "") if "## Objective" in text else ("", "shape")


def summarize(transcript, llm: LLM, request: str, n_ctx: int, on_chars: Callable[[int], None] | None = None,
              after: Callable[[str, list[dict]], str] | None = None, continued: dict | None = None) -> dict:
    """要約して記録に印を付ける。戻り値は {"chars","failed","kept","continued"}。
    after(要約, 要約した発言) は要約の後に添える物を返す（記憶の口・「Verified Facts」「Dead Ends」は帳面に積まれる）。
    continued は会話の続きとして作った要求。★文字にまとめ直すと先頭から一致せず、毎回ゼロから読む
    （2026-11-03 Gyro-M で 76,126 トークン・約 6 分）。続きなら読み込み済みの分をそのまま使える。だめなら従来の形へ。"""
    raw = transcript.raw
    last = transcript.last_compaction()
    floor = int(last[1]["bw_compaction"].get("keep_from") or 0) if last else 0
    prior = str(last[1].get("content") or "") if last else ""
    budget = min(15_000, max(2_000, usable(n_ctx) // 4)) if n_ctx else 8_000
    keep = _tail_start(raw, floor, budget)
    head = [m for m in raw[floor:keep] if m.get("role") != "system"]
    words = _user_words(raw, keep, request)
    words = f"\n\n{words}" if words else ""
    convo = _serialize(head)
    cap = max(8_000, (n_ctx or 32_000) - SUMMARY_TOKENS - 6_000) * 3      # 文字数のおおよその上限
    if len(convo) > cap:
        convo = convo[-cap:]
    seen = [0]

    def _tick(t: str) -> None:
        seen[0] += len(t)
        if on_chars:
            on_chars(seen[0])
    text = ""
    retried, fell = False, ""
    if continued is not None:
        if getattr(llm.info(), "strata", False):
            continued["strata_checkpoint"] = False
        text, why = _continued(continued, llm, _tick)
        retried = False
        if not text and why in ("length", "called_tool", "broken", "shape"):
            # ★文字にまとめ直す形へ落とす前に、続きの形で 1 回やり直す（2026-11-13 の記録: 要約 98 回のうち 22 回が落ちていた。落ちると
            #   会話を頭から読み直す＝数分。続きの形のやり直しは読み込み済みの分をそのまま使える）。上限で切れたなら上限を 1.5 倍にする。
            retried = True
            again = dict(continued)
            if why == "length":
                again["max_tokens"] = int(int(continued.get("max_tokens") or SUMMARY_TOKENS + SUMMARY_THINK) * 1.5)
            seen[0] = 0
            text, why = _continued(again, llm, _tick)
        fell = "" if text else why
        if text:
            extra = (after(text, head) if after else "") or ""
            body_text = text + (f"\n\n{extra}" if extra else "") + words + f"\n\nThe user's latest request, verbatim:\n「{request[:2000]}」\n\n{CONTINUE}"
            transcript.mark_compacted(body_text, keep)
            return {"chars": len(text), "failed": False, "kept": len(raw) - keep, "continued": True, "retried": retried, "fell": ""}
        seen[0] = 0
    prompt = TEMPLATE + "\n\n"
    if prior:
        prompt += UPDATE + f"\n\n<prior-summary>\n{prior}\n</prior-summary>\n\n"
    prompt += f"<conversation>\n{convo}\n</conversation>"
    failed = False
    try:
        body = llm.payload([{"role": "user", "content": prompt}], None, budget=0,
                           max_tokens=SUMMARY_TOKENS if llm.info().llama else SUMMARY_TOKENS_CLAUDE)
        if getattr(llm.info(), "strata", False):
            body["strata_checkpoint"] = False    # 要約の手番は次の要求が続けない＝Strata の控えの枠を使わせない
        text = llm.stream(body, on_text=_tick, watch=False).text.strip()
    except Cancelled:
        raise
    except (BackendError, ContextOverflow):
        failed = True
    if "## Objective" not in text:          # 要約の形になっていない＝使わない
        failed = True
        text = _mechanical(head, prior, request)
    extra = (after(text, head) if after else "") or ""
    body_text = text + (f"\n\n{extra}" if extra else "") + words + f"\n\nThe user's latest request, verbatim:\n「{request[:2000]}」\n\n{CONTINUE}"
    transcript.mark_compacted(body_text, keep)
    return {"chars": len(text), "failed": failed, "kept": len(raw) - keep, "continued": False, "retried": retried, "fell": fell}
