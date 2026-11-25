"""分かったことの帳面・要約の前の全文・思い出させる一言（2026-10-15）。

オーナー 2026-10-15「Blackwall は長時間作業を通して当初の目的を忘れてしまったり、自分で解析した結果を忘れてしまったり
してしまいます。記憶の部分でさらなる強化を入れたい」→ Claude Code・Anthropic の指針・Manus の作りを調べて 4 つ出し「入れていいです」。

旧: 解析の結果は約 3,000 字の要約の中にしか無く、要約のたびに「前の要約＋新しい分」をまた縮め直していた（10-14 の走行は 111 分で
要約 8 回）＝古い発見から順に消えた。ToDo の一覧は要約の後に添えず、要約より前の会話はモデルからは引き直せなかった。

・帳面 .blackwall/FINDINGS.md … 要約のたびに、要約の「Verified Facts」「Dead Ends」を積み足す（推論機は呼ばない＝時間を足さない）。
  帳面そのものは縮めない＝要約を何回くり返しても消えない。同じ事実の言い換え（要約のたびに書き直される）は文字の近さで足さない。
  要約の後と新しい会話の最初に添える（Claude Code が要約の後に CLAUDE.md と自動メモを読み直すのと同じ形・Anthropic の
  「構造化したメモ」）。エージェント自身も Edit で足せる・誤りと確かめた行は消せる。
・要約の前の全文 .blackwall/history/<会話>.md … 縮める前の会話（依頼・道具の呼び出しと結果・答え）を読める形で残し、要約の後の
  一言に場所を書く（Claude Code の要約の末尾「細部が要るならこの全文を」・Manus の「取り戻せる圧縮」）。思考は残さない（量が大きい）。
・思い出させる一言 … 長い走行では 20 手番ごとに、元の依頼の冒頭・ToDo の残り・帳面の件数を道具の結果の末尾に添える
  （Manus の todo.md の書き直し＝目的を文脈の末尾＝モデルがいちばんよく見る所に置き続ける）。
LAPS_FINDINGS=0 で全部止める。
"""
from __future__ import annotations

from .. import paths as _lp
import json
import os
import re
import time
from pathlib import Path

LEDGER_NAME = "FINDINGS.md"
HIST_NAME = "history"


def ledger_rel(cwd: str) -> str:
    """帳面の、作業フォルダからの相対の道（`.laps/FINDINGS.md`・旧い置き場が在れば `.blackwall/FINDINGS.md`）。"""
    return f"{_lp.workspace_name(cwd)}/{LEDGER_NAME}"


def hist_dir(cwd: str) -> Path:
    return _lp.workspace_dir(cwd) / HIST_NAME
H_FACTS = "## 確かめた事実"
H_DEAD = "## 行き止まり"
HEAD = ("# 分かったこと（Blackwall の帳面）\n"
        "（要約のたびに、確かめた事実と行き止まりを積み足す。帳面は縮めない。誤りと確かめた行は消してよい。"
        "各行の頭は書いた日時）\n")
#: 1 つの欄に残す行の上限（超えたら古い行から外す）。
MAX_ITEMS = 600
#: 要約の後・新しい会話の最初に添える量（字）。超えたら新しい行から入れ、残りはファイルを読むよう書く。
CTX_CHARS_COMPACT = 12000
CTX_CHARS_LEAN = 6000             # 要約の後（要約に無い行だけ・この会話の行だけ・2026-11-26）
CTX_CHARS_START = 6000
#: 同じ事実の言い換えとみなす近さ（文字の 3 つ組の重なり）。★識別子・数値（ファイル名・オフセット・"SAV1"・LZ4 など）が
#: 1 つでも違えば別の事実（文の近さが 0.84 でも「LZ4 圧縮」と「LZMA 圧縮」は別物・2026-10-15 に試して確かめた）。
#: 識別子が全部同じときだけ、文の近さで言い換えかを決める（要約のたびの言い換えは 0.44〜0.47・同じ識別子の別の事実は 0.31）。
SIMILAR_SAME_IDS = 0.40          # 識別子が 2 つ以上・全部同じ
SIMILAR_FEW_IDS = 0.60           # 識別子が 1 つ・同じ
SIMILAR_NO_IDS = 0.80            # 識別子が無い文どうし
#: 思い出させる一言の間隔（手番）。
RECITE_EVERY = 20
#: 全文の控え: 1 つの会話のファイルの上限・1 通の上限・残すファイルの数。
HIST_FILE_MAX = 24 * 1024 * 1024
HIST_MSG_MAX = 40000
HIST_KEEP_FILES = 60
#: 1 つの会話の全文の控えを分けるファイルの数の上限（<会話>.md・<会話>.part2.md …）。
HIST_PARTS_MAX = 20

_SECTIONS = {"verified facts": H_FACTS, "dead ends": H_DEAD}


def enabled() -> bool:
    return os.environ.get("LAPS_FINDINGS", "1").strip() != "0"


def ledger_path(cwd: str) -> Path:
    return _lp.workspace_dir(cwd) / LEDGER_NAME


def _gitignore(d: Path) -> None:
    """.blackwall/ をリポジトリに入れない（worklog.record と同じ 1 行・2026-10-16 の点検: 帳面と全文の控えだけを書く走行では
    .gitignore が無く、控えの中の命令や出力がコミットされうる状態だった）。"""
    try:
        root = d if d.name in _lp.WS_NAMES else next((x for x in d.parents if x.name in _lp.WS_NAMES), None)
        if root is None:
            return
        gi = root / ".gitignore"
        if not gi.exists():
            root.mkdir(parents=True, exist_ok=True)
            gi.write_text("# Blackwall の作業ログなど。リポジトリには入れない。\n*\n", encoding="utf-8")
    except OSError:
        pass


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    _gitignore(p.parent)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


# ── 帳面 ────────────────────────────────────────────────────────────────

def parse_summary(summary: str) -> dict[str, list[str]]:
    """要約（雛形の Markdown）から「Verified Facts」「Dead Ends」の箇条を抜く。(none) は外す。"""
    out: dict[str, list[str]] = {H_FACTS: [], H_DEAD: []}
    cur = None
    for raw in (summary or "").splitlines():
        line = raw.rstrip()
        m = re.match(r"^\s*#{2,3}\s+(.+?)\s*$", line)
        if m:
            cur = _SECTIONS.get(m.group(1).strip().lower())
            continue
        if cur is None:
            continue
        b = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$", line)
        if b:
            item = b.group(1).strip()
            if item.strip("()[] .").lower() not in ("none", "なし", ""):
                out[cur].append(item)
        elif line.strip() and out[cur]:
            out[cur][-1] += " " + line.strip()          # 箇条の続きの行
    return out


#: 行の頭の印「[日時 #会話]」「[#会話]」（会話は session id・2026-10-16）。
_TAG = re.compile(r"^\[(?:\d{4}-\d\d-\d\d[^\]#]*?)?\s*(?:#([\w.\-]+))?\]\s*")


def line_session(line: str) -> str:
    """帳面の行を書いた会話（頭の印の #会話）。印が無い行（古い行・手で足した行）は ""。"""
    m = _TAG.match(line or "")
    return (m.group(1) or "") if m else ""


def _norm(s: str) -> str:
    s = _TAG.sub("", s)   # 頭の日時と会話
    s = s.lower()
    return re.sub(r"[\W_]+", " ", s, flags=re.UNICODE).strip()


def _grams(s: str) -> set[str]:
    t = _norm(s).replace(" ", "")
    return {t[i:i + 3] for i in range(max(1, len(t) - 2))} if t else set()


_IDS = re.compile(r"[A-Za-z0-9_.\-/\\:]*\d[A-Za-z0-9_.\-/\\:]*|[A-Za-z0-9_\-]+\.[A-Za-z0-9_.\-]+|\b[A-Z][A-Z0-9_]{1,}\b"
                  r"|\"[^\"]{1,60}\"|「[^」]{1,60}」|`[^`]{1,80}`")


def _ids(s: str) -> frozenset[str]:
    """文の中の識別子・数値（ファイル名・オフセット・大文字の名前・引用した文字列）。"""
    s = _TAG.sub("", s)
    return frozenset(x.lower().strip('"「」`.') for x in _IDS.findall(s))


def _key(s: str) -> tuple[set[str], frozenset[str]]:
    return _grams(s), _ids(s)


def _similar(a: tuple[set[str], frozenset[str]], b: tuple[set[str], frozenset[str]]) -> bool:
    """同じ事実の言い換えか。識別子が 1 つでも違えば別物。"""
    (ga, ia), (gb, ib) = a, b
    if not ga or not gb or ia != ib:
        return False
    j = len(ga & gb) / len(ga | gb)
    need = SIMILAR_NO_IDS if not ia else SIMILAR_FEW_IDS if len(ia) < 2 else SIMILAR_SAME_IDS
    return j >= need


def _read_ledger(p: Path) -> dict[str, list[str]]:
    secs: dict[str, list[str]] = {H_FACTS: [], H_DEAD: []}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return secs
    cur = None
    for line in text.splitlines():
        if line.strip() in secs:
            cur = line.strip()
            continue
        if line.startswith("## "):
            cur = None
            continue
        if cur and line.startswith("- "):
            secs[cur].append(line[2:].strip())
        elif cur and line.strip() and secs[cur]:
            secs[cur][-1] += " " + line.strip()
    return secs


def _render(secs: dict[str, list[str]]) -> str:
    parts = [HEAD]
    for h in (H_FACTS, H_DEAD):
        parts.append(h)
        parts.extend(f"- {x}" for x in secs[h])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def absorb(cwd: str, summary: str, session_id: str = "") -> tuple[int, int]:
    """要約の事実と行き止まりを帳面へ積み足す。足した数（事実, 行き止まり）を返す。
    ★行の頭に書いた会話（#session_id）を付ける（context_block が別の会話の行を分ける・2026-10-16）。"""
    if not enabled():
        return 0, 0
    got = parse_summary(summary)
    if not got[H_FACTS] and not got[H_DEAD]:
        return 0, 0
    p = ledger_path(cwd)
    secs = _read_ledger(p)
    stamp = time.strftime("%Y-%m-%d %H:%M") + (f" #{_sid(session_id)}" if session_id else "")
    added = {H_FACTS: 0, H_DEAD: 0}
    for h in (H_FACTS, H_DEAD):
        have = [_key(x) for x in secs[h]]
        for item in got[h]:
            g = _key(item)
            if not g[0] or any(_similar(g, o) for o in have):
                continue
            secs[h].append(f"[{stamp}] {item}")
            have.append(g)
            added[h] += 1
        if len(secs[h]) > MAX_ITEMS:
            secs[h] = secs[h][-MAX_ITEMS:]
    if added[H_FACTS] or added[H_DEAD] or not p.exists():
        _write(p, _render(secs))
    return added[H_FACTS], added[H_DEAD]


def counts(cwd: str) -> tuple[int, int]:
    secs = _read_ledger(ledger_path(cwd))
    return len(secs[H_FACTS]), len(secs[H_DEAD])


def context_block(cwd: str, max_chars: int = CTX_CHARS_COMPACT, session_id: str = "", others: bool = True,
                  skip: str = "") -> str:
    """帳面を会話に添える塊（帳面が無い・空なら ""）。長ければ新しい行から入れる。

    ★2026-10-16 の点検: 帳面はフォルダの全部の会話で 1 つなのに、新しい会話（別の依頼・更新された後のファイル）にも
      「確かめなくてよい事実」として渡していた。session_id を渡すと、この会話が書いた行（頭の #会話 が同じ）だけを
      今までどおり渡し、ほかの会話の行（印の無い古い行も）は「前の会話の記録・使う前に確かめ直す」として分けて渡す。
      session_id を渡さなければ従来どおり（全部をこの会話の行として扱う）。
    others=False はほかの会話の行を付けない。skip（要約の本文）の Verified Facts・Dead Ends と同じ事実の行は付けない
    （2026-11-26 要約の後: 要約に入っている行を毎回付け直し、要約が重なるほど増えていた）。"""
    if not enabled():
        return ""
    p = ledger_path(cwd)
    secs = _read_ledger(p)
    if not secs[H_FACTS] and not secs[H_DEAD]:
        return ""
    sid = _sid(session_id) if session_id else ""

    def mine(x: str) -> bool:
        return not sid or line_session(x) == sid
    have = parse_summary(skip) if skip else {H_FACTS: [], H_DEAD: []}
    have_keys = [_key(x) for h in (H_FACTS, H_DEAD) for x in have[h]]

    def in_summary(x: str) -> bool:
        if not have_keys:
            return False
        k = _key(x)
        return any(_similar(k, o) for o in have_keys)
    budget = max_chars
    shown: dict[tuple[str, bool], list[str]] = {(h, m): [] for h in (H_FACTS, H_DEAD) for m in (True, False)}
    # この会話の行を先に、行き止まりは短く数も少ない＝先に取り、残りを事実に（どれも新しい行から）
    for own in ((True, False) if others else (True,)):
        for h in (H_DEAD, H_FACTS):
            for x in reversed([x for x in secs[h] if mine(x) == own and not (own and in_summary(x))]):
                if len(x) + 3 > budget:
                    break
                shown[(h, own)].insert(0, x)
                budget -= len(x) + 3
    n_shown = sum(len(v) for v in shown.values())
    if not n_shown and (skip or not others):
        tag0 = f"[#{sid}] " if sid else ""
        return (f"Your ledger of verified facts and dead ends is {p} ({len(secs[H_FACTS])} facts, {len(secs[H_DEAD])} dead ends; "
                "this conversation's lines are already in the summary above). When you verify something important, add "
                f"one line \"- {tag0}<the fact>\" under 「{H_FACTS}」 (or 「{H_DEAD}」 for an approach that failed).")
    left = len(secs[H_FACTS]) + len(secs[H_DEAD]) - n_shown
    body = []
    if shown[(H_FACTS, True)]:
        body.append("Verified facts (oldest first):\n" + "\n".join(f"- {x}" for x in shown[(H_FACTS, True)]))
    if shown[(H_DEAD, True)]:
        body.append("Dead ends (do not try these again):\n" + "\n".join(f"- {x}" for x in shown[(H_DEAD, True)]))
    if shown[(H_FACTS, False)] or shown[(H_DEAD, False)]:
        prior = []
        if shown[(H_FACTS, False)]:
            prior.append("Facts:\n" + "\n".join(f"- {x}" for x in shown[(H_FACTS, False)]))
        if shown[(H_DEAD, False)]:
            prior.append("Dead ends:\n" + "\n".join(f"- {x}" for x in shown[(H_DEAD, False)]))
        body.append("From earlier conversations in this folder (not checked in this conversation; the files or the "
                    "task may have changed since):\n" + "\n".join(prior))
    more = f"\n({left} older lines are only in the file.)" if left else ""
    own_any = bool(shown[(H_FACTS, True)] or shown[(H_DEAD, True)])
    if not sid:
        rely = ("Rely on these facts instead of re-checking them, and do not retry the dead ends, unless "
                "a new tool result contradicts a line — then fix or delete that line in the file.")
    elif own_any:
        rely = ("Rely on the facts this conversation established instead of re-checking them, and do not retry its dead "
                "ends, unless a new tool result contradicts a line — then fix or delete that line in the file. Lines from "
                "earlier conversations are leads only: verify one with a tool before you build on it.")
    else:
        rely = ("These lines come from earlier conversations in this folder. Treat them as leads, not facts: verify a "
                "line with a tool before you build on it, and fix or delete a line that turns out wrong.")
    tag = f"[#{sid}] " if sid else ""
    return (f'<findings path="{p}" facts="{len(secs[H_FACTS])}" dead_ends="{len(secs[H_DEAD])}">\n'
            + "\n\n".join(body) + more + "\n</findings>\n"
            "This is your ledger of what this work has established so far, kept outside the conversation so it is "
            f"never shortened. {rely} When you verify "
            f"something important, add one line \"- {tag}<the fact>\" under 「{H_FACTS}」 (or 「{H_DEAD}」 for an "
            "approach that failed), with how you verified it.")


# ── 要約の前の全文 ──────────────────────────────────────────────────────────

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    out.append(str(p.get("text") or ""))
                elif p.get("type") in ("image_url", "image"):
                    out.append("[image]")
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    return "" if content is None else str(content)


def _clip(s: str, n: int = HIST_MSG_MAX) -> str:
    return s if len(s) <= n else s[:n] + f"\n…（{len(s) - n:,} 字を省略）"


def render_messages(msgs: list[dict]) -> str:
    """会話を読める Markdown に（思考は入れない）。"""
    out: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        text = _text_of(m.get("content")).strip()
        if role == "user":
            out.append("### user\n" + _clip(text))
        elif role == "assistant":
            parts = [_clip(text)] if text else []
            for c in m.get("tool_calls") or []:
                fn = (c or {}).get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                parts.append(f"→ {fn.get('name')}({_clip(args or '', 8000)})")
            out.append("### assistant\n" + ("\n".join(parts) if parts else "（本文なし）"))
        elif role == "tool":
            out.append("### tool result\n" + _clip(text))
    return "\n\n".join(out) + "\n"


def _sid(session_id: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", str(session_id or "session"))[:80] or "session"


def _redact(text: str) -> str:
    """控えに書く前に秘密を伏せる（2026-10-16 の点検: 道具の出力の DB_PASSWORD=… や会話で渡した鍵が作業フォルダに平文で
    残っていた）。保存した API キーの値（apikeys）と、値の形で見分ける門（laps.redact）の両方。"""
    try:
        from .. import apikeys
        text = apikeys.redact(text)[0]
    except Exception:
        pass
    try:
        from .. import redact as _rd
        text = _rd.redact(text).text
    except Exception:
        pass
    return text


def history_files(cwd: str, session_id: str) -> list[Path]:
    """この会話の全文の控え（<会話>.md・上限で分けた <会話>.partN.md）。"""
    d = hist_dir(cwd)
    sid = _sid(session_id)
    out = [d / f"{sid}.md"] + [d / f"{sid}.part{i}.md" for i in range(2, HIST_PARTS_MAX + 1)]
    return [p for p in out if p.exists()]


def archive(cwd: str, session_id: str, msgs: list[dict], n: int) -> str:
    """要約で縮める前の会話を .blackwall/history/<会話>.md へ足す。作業フォルダからの相対パスを返す（書けなければ ""）。
    ★上限（HIST_FILE_MAX）に達したら次のファイル（<会話>.part2.md …）へ書く。書かなかったときは "" を返す
      （2026-10-16 の点検: 上限で書くのを止めた後も場所を返し、モデルには「全文はここに在る」と伝えていた）。"""
    if not enabled():
        return ""
    sid = _sid(session_id)
    d = hist_dir(cwd)
    try:
        d.mkdir(parents=True, exist_ok=True)
        _gitignore(d)
        p = None
        for i in range(1, HIST_PARTS_MAX + 1):
            cand = d / (f"{sid}.md" if i == 1 else f"{sid}.part{i}.md")
            if not cand.exists() or cand.stat().st_size < HIST_FILE_MAX:
                p = cand
                break
        if p is None:
            return ""
        size = p.stat().st_size if p.exists() else 0
        head = "" if size else (f"# 会話 {sid} の全文の控え（要約で縮める前・思考は除く）\n"
                                "（要約のたびに、その時点までの会話を下に足す。細部はここを Grep / Read で引き直す）\n\n")
        block = (f"{head}## 要約 {n} の前（{time.strftime('%Y-%m-%d %H:%M')}）\n\n" + _redact(render_messages(msgs)) + "\n")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(block)
        _tidy(d)
        return str(p.relative_to(cwd))
    except (OSError, ValueError):
        return ""


def scrub_history(cwd: str, session_id: str, fn) -> bool:
    """この会話の全文の控えを fn（文字列 → 文字列）で書き直す（会話で渡された API キーを保存した後・loop._scrub_saved_keys）。
    変えたファイルがあれば True。"""
    changed = False
    for p in history_files(cwd, session_id):
        try:
            text = p.read_text(encoding="utf-8")
            new = fn(text)
            if isinstance(new, str) and new != text:
                _write(p, new)
                changed = True
        except OSError:
            continue
    return changed


def _tidy(d: Path) -> None:
    try:
        files = sorted((f for f in d.glob("*.md") if f.is_file()), key=lambda f: f.stat().st_mtime)
        for f in files[:-HIST_KEEP_FILES]:
            f.unlink()
    except OSError:
        pass


# ── 思い出させる一言・ToDo ─────────────────────────────────────────────────────

def todo_block(todos: list[dict], max_chars: int = 2500) -> str:
    """ToDo の一覧（TodoWrite）を添える形に。空なら ""。"""
    rows = []
    for t in todos or []:
        if not isinstance(t, dict):
            continue
        content = " ".join(str(t.get("content") or t.get("activeForm") or "").split())
        if content:
            rows.append(f"- [{t.get('status') or 'pending'}] {content}")
    if not rows:
        return ""
    text = "\n".join(rows)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return "Your todo list (TodoWrite) as it stands:\n" + text


def recite_snapshot(todos: list[dict], cwd: str) -> dict[str, int]:
    """思い出させる一言を出した時点の進み（済んだ ToDo の数・帳面の件数）。次の一言で差を言うのに使う。"""
    done = sum(1 for t in (todos or []) if isinstance(t, dict) and t.get("status") == "completed")
    try:
        nf, nd = counts(cwd)
    except Exception:
        nf = nd = 0
    return {"done": done, "facts": nf, "dead": nd}


def _progress_line(prev: dict[str, int], now: dict[str, int], every: int, cwd: str = "") -> tuple[str, bool]:
    """(前の一言からの進みの一文, 進んだか)。"""
    d_done = now["done"] - int(prev.get("done", 0))
    d_facts = now["facts"] - int(prev.get("facts", 0))
    d_dead = now["dead"] - int(prev.get("dead", 0))
    parts = [f"{n} {what}" for n, what in ((d_done, "todo items completed"), (d_facts, "facts"),
                                           (d_dead, "dead ends")) if n > 0]
    if parts:
        return f"Since the last reminder ({every} turns ago): " + ", ".join(parts) + " added to your records.", True
    return (f"In the last {every} turns no todo item was completed and nothing was added to "
            f"{ledger_rel(cwd or os.getcwd())}.", False)


def recite_note(goal: str, todos: list[dict], cwd: str, prev: dict[str, int] | None = None) -> str:
    """長い走行の途中で、目的を文脈の末尾に置き直す一言。

    ★2026-10-15: 毎回同じ定型文だと読み流される。prev（前の一言の recite_snapshot）があれば、その後の進み
      （済んだ ToDo・帳面に足した件数）を書く＝毎回違う中身になる。進んでいれば「今の作業がその依頼のためか確かめて」は付けない
      （進んでいる走行に同じ促しを繰り返さない）。進んでいなければ付ける。"""
    g = " ".join((goal or "").split())
    if not g:
        return ""
    g = g if len(g) <= 400 else g[:400] + "…"
    open_items = [t for t in (todos or []) if isinstance(t, dict) and t.get("status") in ("pending", "in_progress")]
    lines = [f"[Harness] Reminder of the request you are working on: 「{g}」"]
    if open_items:
        lines.append("Open todo items: " + "; ".join(
            " ".join(str(t.get("content") or "").split())[:120] for t in open_items[:8]))
    now = recite_snapshot(todos, cwd)
    nf, nd = now["facts"], now["dead"]
    if nf or nd:
        lines.append(f"Your findings so far are in {ledger_rel(cwd)} ({nf} facts, {nd} dead ends); "
                     "add to it when you verify something.")
    moved = False
    if prev is not None:
        line, moved = _progress_line(prev, now, RECITE_EVERY, cwd)
        lines.append(line)
    if not moved:
        lines.append("Check that what you are doing now still serves that request.")
    return "\n".join(lines)


# ── セキュリティ発見（脆弱性情報）────────────────────────────────────────────

#: 帳面のセキュリティ発見セクション。security findings は普通の事実とは別に扱う（ severity で並べ替え可能）。
H_SECURITY = "## セキュリティ発見"
_SECURITY_SECTIONS = {**_SECTIONS, "security findings": H_SECURITY}
#: 発見を記録する際の上限数（超えたら低い severity から外す）。
SEC_MAX_ITEMS = 200


def record_finding(cwd: str, *, asset: str, severity: str, technique: str, evidence: str, remediation: str = "") -> None:
    """1 つの脆弱性情報を帳面に足す。severity は critical / high / medium / low / info のいずれか。

    既に同じ asset + severity + technique の組み合わせがあれば上書きする（更新）。
    """
    if not enabled():
        return
    p = ledger_path(cwd)
    secs = _read_ledger(p)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    # 重複チェック: asset + severity + technique が同じ行があれば更新
    key = f"{asset.lower()}|{severity.lower()}|{technique.lower()}"
    updated = False
    for i, line in enumerate(secs.get(H_SECURITY, [])):
        if key in line.lower():
            secs[H_SECURITY][i] = (f"[{stamp}] "
                                   f"**[{severity.upper()}]** {asset} — {technique}\n"
                                   f"  証拠: {evidence}\n"
                                   f"  対策: {remediation}" if remediation else
                                   f"[{stamp}] **[{severity.upper()}]** {asset} — {technique}\n"
                                   f"  証拠: {evidence}")
            updated = True
            break
    if not updated:
        entry = (f"[{stamp}] **[{severity.upper()}]** {asset} — {technique}\n"
                 f"  証拠: {evidence}\n"
                 f"  対策: {remediation}" if remediation else
                 f"[{stamp}] **[{severity.upper()}]** {asset} — {technique}\n"
                 f"  証拠: {evidence}")
        secs[H_SECURITY].append(entry)
    # 上限を超えたら低い severity から外す（info < low < medium < high < critical）
    _SEV_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    if len(secs[H_SECURITY]) > SEC_MAX_ITEMS:
        secs[H_SECURITY] = sorted(secs[H_SECURITY],
                                  key=lambda x: min(_SEV_ORDER.get(s, -1) for s in _SEV_ORDER if s in x.lower()),
                                  reverse=True)[-SEC_MAX_ITEMS:]
    _write(p, _render_with_security(secs))


def _read_security_findings(p: Path) -> list[str]:
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    in_sec = False
    for line in text.splitlines():
        if line.strip() == H_SECURITY:
            in_sec = True
            continue
        if line.startswith("## "):
            in_sec = False
            continue
        if in_sec and line.strip():
            out.append(line.strip())
    return out


def _render_with_security(secs: dict[str, list[str]]) -> str:
    parts = [HEAD]
    for h in (H_FACTS, H_DEAD, H_SECURITY):
        items = secs.get(h, [])
        if not items and h == H_SECURITY:
            continue
        parts.append(h)
        parts.extend(items)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def security_count(cwd: str) -> int:
    """帳面に記録されているセキュリティ発見の数。"""
    p = ledger_path(cwd)
    try:
        return len(_read_security_findings(p))
    except Exception:
        return 0


def security_context_block(cwd: str, max_chars: int = 4000) -> str:
    """セキュリティ発見を会話に添える塊（見出し・証拠・対策）。長ければ切捨てる。"""
    p = ledger_path(cwd)
    items = _read_security_findings(p)
    if not items:
        return ""
    budget = max_chars
    shown: list[str] = []
    for item in reversed(items):
        if len(item) + 3 > budget:
            break
        shown.insert(0, item)
        budget -= len(item) + 3
    left = len(items) - len(shown)
    more = f"\n({left} older findings are only in the file.)" if left else ""
    return (f'<security-findings path="{p}" count="{len(items)}">\n'
            + "\n".join(f"- {x}" for x in shown) + more + "\n</security-findings>\n")
