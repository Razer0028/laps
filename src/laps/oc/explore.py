"""調べ物を任せる子エージェント（Explore・2026-10-15）。

★オーナー「ClaudeCode 視点でエージェントに修正を加えてください」。Claude Code が長い作業を持たせている一番の仕組みは、
  多数のファイルを横断する調べ物を子に任せ、親には結論だけを返させること（途中で読んだ中身は子の文脈に残り、親を埋めない）。
  Blackwall は 111 分の走行で要約が 8 回入った（10-14）＝調べ物の結果が親の文脈を埋めていく形。
  旧中核の Task（tools/task.py）は oc の道具に入っていなかった。音声アシスタントの子（subagent）は別の用途。

子は同じ推論機の別の要求として順に走る（親はその間待つ）。子に渡すのは「見る」道具だけ（書き換えない・ToDo を持たない・
入れ子にしない）。子の最後の答えだけを親へ返し、長ければ切る。子の道具の呼び出しは親の画面と記録へ notice（kind=subagent）で流す
（tools/task.py の _ChildRelay と同じ。親の道具として数えない）。
既定で入れる（2026-10-18〜・enabled の注記）。LAPS_EXPLORE=0 で外す。効き目の数え: explore_calls・explore_child_tools・explore_chars。

★2026-10-18 ローカルの小さいモデル向けに作り変えた（オーナー「ClaudeCode とかは子に作業をやらせて親に結果を返す形が多いと
  聞きますが」→「ローカル LLM なので ClaudeCode と全く同じ仕組みにするとよくないことになりそうなので合わせて作ってみて」）:
  - 頼みを書くのはモデルだが、前提はハーネスが添える（利用者の元の依頼・親がもう読んだファイル）＝小さいモデルは前提の抜けた
    頼みを書きがちなので、子が同じ物を読み直したり的外れに探したりしない。
  - 子の答えは決まった形（ANSWER / EVIDENCE / NOT FOUND）。根拠の path:line と引用をハーネスが実ファイルと突き合わせ、
    合えば ✓・合わなければ ✗ を付けて親へ返す（小さい子は根拠をでっち上げうる＝確かめていない根拠を親の文脈へ入れない）。
  - 子の手番は 12 まで（迷走させない）。書き換える道具は渡さない（今までどおり）。
  - 既定で入れる（LAPS_EXPLORE=0 で外す）。親が読み続けて文脈が膨らんだら、loop がハーネスの注記で 1 回だけ勧める。
"""
from __future__ import annotations

from laps import _envcompat
import os
import re

from ..tools import ToolResult, ToolSpec

#: 子に渡す道具（あるものだけ）。書き換える道具・ToDo・入れ子・利用者に聞く道具は渡さない。
CHILD_TOOLS = ("Read", "Glob", "Grep", "Bash", "BashOutput", "WebSearch", "WebFetch")
#: 子の手番の上限・親へ返す答えの上限（字）。★小さいモデルは長く動かすほど迷走する（2026-10-18: 25 → 12）。
CHILD_MAX_ITERS = 12
RESULT_MAX_CHARS = 4000
#: 調べる深さ（Claude Code の Explore の thoroughness: quick / medium / very thorough に合わせた）→ 子の手番の上限。
#: ★ローカルでは深さを手番の数で決める（小さいモデルに「念入りに」と書いても長さは決まらない）。指定が無ければ CHILD_MAX_ITERS。
DEPTHS = {"quick": 4, "medium": 8, "thorough": 12}
#: 子へ添える「親がもう読んだファイル」の数の上限。
SEEN_MAX = 30

DESCRIPTION = (
    "Hand an investigation to a helper and get back only its conclusion. The helper has its own context: it reads files, "
    "searches (Glob, Grep, rg through Bash), runs read-only commands and can search the web, then reports what it found "
    "with file paths and line numbers. Everything it read stays out of your context.\n"
    "- Use it when answering needs looking across many files, directories or long outputs: where something is defined or "
    "handled, which files use a name, what engine or format a folder of files is, what a format's specification says.\n"
    "- Do not use it for one file or value whose location you already know: Read it yourself, it is faster.\n"
    "- The helper cannot edit files or ask the user. Write the question so it stands alone: what to find, where to look, "
    "and what the answer should contain.\n"
    "- It takes as long as a few of your own turns. Ask separate questions in separate calls.\n"
    "- depth: quick (a few searches), medium, or thorough (many places and names; the default)."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string",
                     "description": "What to find out, written so a helper with no other context can do it"},
        "paths": {"type": "string", "description": "Optional: folders or files to start from"},
        "depth": {"type": "string", "enum": ["quick", "medium", "thorough"],
                  "description": "Optional: how far to search (default thorough)"},
    },
    "required": ["question"],
}

_CHILD_PREFIX = (
    "You are a helper doing one investigation for another agent. You can read and search, but you must not change "
    "anything: do not edit, create or delete files, even to fix what you find (Bash commands that write are refused). "
    "If something should change, say what and where in your answer. "
    "Answer the question below and then stop. Your last message is the whole answer the other agent gets.\n"
    "Reply in exactly this form:\n"
    "ANSWER: the conclusion in 1-5 lines.\n"
    "EVIDENCE:\n"
    "- path:line — \"the text of that line, copied exactly from the file\"\n"
    "(one line per piece of evidence, at most 8)\n"
    "NOT FOUND: what you looked for but could not find (or none).\n"
    "Only list evidence you actually read. Each quote is checked against the file.\n\n"
)


# ★見るだけの子が Bash で書き換えるのを止める（2026-10-21 の Construct の試し: 下見の子が「mul のバグはどこか」を調べるうちに
#   自分で直し、親はそれを知らずに作業を続けて、答えで原因を取り違えた）。言い回しで当てるので漏れはある＝漏れた分は run が
#   走らせた前後の作業フォルダを比べて親へ知らせる。
_W_REDIR_TARGET = re.compile(r"(?<![0-9&<>])>>?\s*([^\s;|&<>]+)")
_W_SAFE_TARGET = re.compile(r"^(?:&\d|/dev/(?:null|stdout|stderr)$|/tmp/)")
_W_CMDS = re.compile(r"(?:^\s*|[;&|(]\s*|\b(?:sudo|xargs|exec|then|do|else)\s+)"
                     r"(?:rm|mv|cp|tee|truncate|touch|mkdir|rmdir|ln|chmod|chown|install|patch|dd|unzip|"
                     r"tar\s+-?[a-zA-Z]*[cxr][a-zA-Z]*)\b")
_W_INPLACE = re.compile(r"\b(?:sed|perl)\b[^;|&]*?\s-[a-zA-Z]*i")
_W_GIT = re.compile(r"\bgit\s+(?:apply|am|checkout|switch|reset|restore|commit|stash|clean|mv|rm|merge|rebase|cherry-pick|"
                    r"revert|pull|push|add)\b")
_W_SCRIPT = re.compile(r"""open\([^)]*,\s*['"][wax]|\.write_text\(|\.write_bytes\(|os\.(?:remove|rename|replace|unlink)\(|"""
                       r"""shutil\.(?:copy|move|rmtree)|fs\.(?:writeFile|unlink|rm)""")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")


def bash_writes(command: str) -> bool:
    """Bash の命令が作業フォルダのファイルを書き換えうるか（見るだけの子で止めるため）。/dev/null と /tmp への書き出しは通す。"""
    raw = str(command or "")
    if _W_SCRIPT.search(raw):
        return True
    bare = _QUOTED.sub("''", raw)                       # 引用の中の > や rm は命令ではない
    if _W_CMDS.search(bare) or _W_INPLACE.search(bare) or _W_GIT.search(bare):
        return True
    return any(not _W_SAFE_TARGET.match(m.group(1)) for m in _W_REDIR_TARGET.finditer(bare))


#: 走らせると作られるキャッシュの置き場（子が変えたファイルに数えない）。
#: ★組み立ての出力（build・dist・target）は数える（見るだけの子が組み立てたのは変化）。
_CACHE_PARTS = frozenset((".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".tox", ".nox", ".cache",
                          "__pycache__", ".coverage", ".eslintcache", ".parcel-cache"))


def _is_cache(rel: str) -> bool:
    parts = re.split(r"[/\\]", str(rel or ""))
    return any(p in _CACHE_PARTS for p in parts) or str(rel).endswith((".pyc", ".pyo"))


READONLY_REFUSED = ("[Harness] Not run: you are a read-only helper. Only look (ls, cat, grep, find, git status/log/diff): do not run builds, tests or programs. Do not change, create or delete files, even to fix "
                    "what you found. Finish the investigation and say in your answer what should change and where.")


def enabled() -> bool:
    """★2026-10-18 から既定で入れる（ローカル向けに作り変えた・頭の注記）。LAPS_EXPLORE=0 で外す。
    旧（2026-10-15 オーナー「サブエージェントは一旦後で考えましょう」）は既定で切っていた: 当時の推論機は 4 枠で文脈の置き場を
    共有（kv_unified）し、子の分が積まれて溢れると処理中の枠を全部止めた。今は枠ごとに置き場が分かれている（kv_unified=false）。"""
    return (os.environ.get("LAPS_EXPLORE") or "1").strip().lower() not in ("0", "false", "off", "no")


# ── 最初の下見（2026-10-18 オーナー「子を最初から使わせる仕組みを導入してください」） ──────────────────
# ★0.1.532〜535 の走行記録（数えの欄）: Explore を渡した 3 走行で、モデルが呼んだのは 1 回（53 手の走行でも Read 10・Grep 4・Explore 0）。
#   手引きに書いても呼ばない＝言葉では振る舞いが変わらない（10-17 の実測と同じ）。そこで、会話の最初の依頼ではハーネスが
#   最初の手番の前に子を 1 回走らせ、その結果を「親が Explore を呼んで返ってきた結果」の形で会話に入れる（loop._scout_first）。
#   親は地図を持って始め、Explore の使い方も自分の会話の中の実例として見る。
# ★下見が要るかは子（モデル）が決める（言い回しの決め打ちで依頼を振り分けない・2026-10-12 オーナー）。要らない依頼なら子は道具を
#   呼ばずに SCOUT_NOTHING を返し、ハーネスはその組を会話に入れない（親の文脈を汚さない）。
#: 下見の子の手番の上限（調べ物の子より短く・親を待たせない）。
SCOUT_MAX_ITERS = 5


def scout_max_seconds() -> float:
    """最初の下見の子の持ち時間（LAPS_EXPLORE_FIRST_SECONDS・既定 45 秒・0 で上限なし）。過ぎたら子は道具なしでまとめて終える。
    ★2026-10-22 Terminal-Bench の診断の走行: 制限 360 秒の課題で下見が 155 秒（chess-best-move・子が課題そのものを解き始めた）・
    126 秒（run-pdp11-code）を使い、どちらも時間切れで落ちた。手番の上限（8）だけでは 1 手番が長いと止まらない。"""
    try:
        v = float(os.environ.get("LAPS_EXPLORE_FIRST_SECONDS") or 45)
    except ValueError:
        v = 45.0
    return v if v > 0 else 0.0


#: 「見る物が無い」の答え（子にこの文で答えさせ、ハーネスが見分ける）。
SCOUT_NOTHING = "nothing to look at for this request"
SCOUT_QUESTION = (
    "Before the other agent starts on the user's request (given below as context), look around the working folder for "
    "what that request needs: the files and places that are relevant (with path:line), how the project is built, run "
    "or tested if that matters (read it from its files: you cannot run anything here), and anything likely to get in the "
    "way. Keep it to what the request needs; do not try "
    "to solve the request. If the request needs nothing from the files here (a greeting, a general question, or work "
    f"unrelated to this folder), do not call any tool: reply only `ANSWER: {SCOUT_NOTHING}` and stop."
)
#: 下見の結果に添える一言（親が自分で呼んだのではないことと、読み方）。
SCOUT_NOTE = ("(The harness asked the helper this before your first step, so you start with the lay of the land. "
              "Read a file yourself before you change it. Call Explore again whenever another question needs a wide search.)")


def first_mode() -> str:
    """LAPS_EXPLORE_FIRST: 0（既定・しない）／1（会話の最初の依頼だけ）／every（依頼ごと）。
    ★2026-11-09 オーナー「このエージェントがタスク前に動くこと自体いらないかも」で既定を切った（10-31 の課題集で走行の 18〜38% を使い、
    親は直後に同じファイルを読み直していた）。"""
    v = (os.environ.get("LAPS_EXPLORE_FIRST") or "0").strip().lower()
    if v in ("0", "false", "off", "no"):
        return "off"
    return "every" if v in ("every", "all", "2") else "first"


def scout_found_nothing(text: str) -> bool:
    return SCOUT_NOTHING in " ".join(str(text or "")[:400].split()).casefold()


#: 根拠の行: `- path:line — "引用"`（path は C:\… の形も）。引用は “” や「」でもよい。
_EV = re.compile(r'^(?P<lead>\s*[-*•]\s*)(?P<path>(?:[A-Za-z]:)?[^:\n"“「]+?):(?P<line>\d+)(?:-(?P<end>\d+))?'
                 r'(?P<rest>\s*(?:[—–-]+\s*)?(?:["“「](?P<quote>.+?)["”」])?.*)$')


def _norm(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def _find_quote(lines: list[str], q: str) -> int | None:
    """引用をファイル全体から探し、見つかった行番号（1 始まり）を返す。1 行に収まらない引用は 3 行ずつの窓で探す。"""
    for i, ln in enumerate(lines):
        if q in _norm(ln):
            return i + 1
    for i in range(len(lines)):
        if q in _norm("\n".join(lines[i:i + 3])):
            return i + 1
    return None


def check_evidence(text: str, cwd: str) -> tuple[str, int, int, int]:
    """子の答えの EVIDENCE の行を実ファイルと突き合わせ、行頭に ✓（合う）か ✗（合わない）を付ける。
    返すのは (書き換えた答え, 合った数, 合わなかった数, 確かめた数)。ファイルがここに無い（ウェブ・別の機械）行は触らない。
    ★2026-10-19 小さいモデルは引用を正しく写しても行番号を数え違える（AGENTS.md の 17 行目を :23 と書いた・6 ずれ）。
    近くに無い引用はファイル全体から探し、在れば ✓ にして本当の行番号を添える（正しい根拠を「でっち上げ」と捨てさせない）。"""
    out, ok, bad = [], 0, 0
    for ln in text.split("\n"):
        m = _EV.match(ln)
        if not m:
            out.append(ln)
            continue
        path = m.group("path").strip().strip("`")
        fp = path if os.path.isabs(path) else os.path.join(cwd, path)
        if not os.path.isfile(fp):
            out.append(ln)
            continue
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            out.append(ln)
            continue
        a = int(m.group("line"))
        b = int(m.group("end") or a)
        quote = m.group("quote")
        q = _norm(quote).strip(".…") if quote else ""
        moved = None
        if quote and not q:
            good = False
        elif q:
            near = _norm("\n".join(lines[max(0, a - 3):min(len(lines), b + 2)])) if 1 <= a <= len(lines) else ""
            good = q in near or q[:60] in near
            if not good:
                moved = _find_quote(lines, q) or _find_quote(lines, q[:60])
                good = moved is not None
        else:
            good = 1 <= a <= len(lines)                # 行はある（引用が無いので中身は確かめていない）
        ok += good
        bad += not good
        if moved:
            out.append(f"{m.group('lead')}✓ {path}:{moved} (the helper wrote :{m.group('line')})" + m.group("rest"))
            continue
        mark = "✓" if good else "✗ (not in the file)"
        out.append(f"{m.group('lead')}{mark} {path}:{m.group('line')}" + (f"-{m.group('end')}" if m.group("end") else "")
                   + m.group("rest"))
    return "\n".join(out), ok, bad, ok + bad
