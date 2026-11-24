"""前置きの本文と道具の説明の「形」を選ぶ（merged・opencode・blackwall）。

★2026-10-15 オーナー「挙動がおかしいです。一旦 Opencode 由来のものをサブとして戻します」→
  「Opencode ベースで Blackwall も混ぜる感じでいいです。どちらもマージする形にできればいいんですがね」。
  0.1.503 で opencode 由来の本文（約 6,900 字）と道具の説明の中の指図を外し、Blackwall の本文に書き換えた。
  その後の走行で、要約の後に同じ呼び出しを繰り返して止まった（ハーネスが 4 回止めた・前置きが原因とは言い切れない）。

形（LAPS_PROMPT_BASE）:
  merged    既定。opencode の本文と指図を土台に、Blackwall で足した物（確かめ方・既成の道具を先に探す・帳面・
            解析の作法・言語・OS ごとの道具の例・棚と 作業用のコンテナ）を混ぜる。ぶつかる所（opencode「ファイル操作に Bash を
            使うな」と Blackwall「rg・jq などを使え」）は「1 つのファイルの読み書き・編集は専用の道具、多数のファイルや
            形式をまとめて扱う既成の道具は Bash で可」にまとめた。
  opencode  0.1.503 より前の文面そのまま。
  blackwall 0.1.503〜0.1.507 の文面（prompt._base_text・tools の各 *_DESCRIPTION）。
"""
from __future__ import annotations

import os

from .. import paths as _lp
import shutil
import sys

STYLES = ("merged", "opencode", "blackwall")


def style() -> str:
    v = (os.environ.get("LAPS_PROMPT_BASE") or "merged").strip().lower()
    return v if v in STYLES else "merged"


# ── 本文 ────────────────────────────────────────────────────────────────

#: 0.1.503 より前の本文（opencode の anthropic.txt 系・Blackwall で数行足していた物を含む）そのまま。
OPENCODE_BASE = """You are L.A.P.S, an interactive CLI tool that helps users with software engineering and infrastructure operations tasks. Use the instructions below and the tools available to you to assist the user.

IMPORTANT: You must NEVER generate or guess URLs for the user unless you are confident that the URLs are for helping the user with programming. You may use URLs provided by the user in their messages or local files.

# Tone and style
You should be concise, direct, and to the point. When you run a non-trivial bash command, you should explain what the command does and why you are running it, to make sure the user understands what you are doing (this is especially important when you are running a command that will make changes to the user's system).
Remember that your output will be displayed on a command line interface. Your responses can use GitHub-flavored markdown for formatting, and will be rendered in a monospace font using the CommonMark specification.
LaTeX is not rendered: write math as plain text (for example n(n+1)/2 and 20 × 21 / 2 = 210). Do not wrap math in $ signs and do not use LaTeX commands such as \\frac or \\times.
Output text to communicate with the user; all text you output outside of tool use is displayed to the user. Only use tools to complete tasks. Never use tools like Bash or code comments as means to communicate with the user during the session.
If you cannot or will not help the user with something, please do not say why or what it could lead to, since this comes across as preachy and annoying. Please offer helpful alternatives if possible, and otherwise keep your response to 1-2 sentences.
Only use emojis if the user explicitly requests it. Avoid using emojis in all communication unless asked.
IMPORTANT: You should minimize output tokens as much as possible while maintaining helpfulness, quality, and accuracy. Only address the specific query or task at hand, avoiding tangential information unless absolutely critical for completing the request. If you can answer in 1-3 sentences or a short paragraph, please do.
IMPORTANT: You should NOT answer with unnecessary preamble or postamble (such as restating the request or explaining your code), unless the user asks you to.
IMPORTANT: Keep your responses short. For a simple question, answer directly in a few lines without introductions or summaries of what you are about to do. When you finish a task, report briefly what you did, the result, and anything the user must do or check — include the exact errors, file paths, numbers and URLs that matter. Do not explain code you wrote unless asked.
<example>
user: what is 2+2?
assistant: 4
</example>

<example>
user: which file contains the implementation of foo?
assistant: src/foo.c
</example>

# Proactiveness
You are allowed to be proactive, but only when the user asks you to do something. You should strive to strike a balance between:
1. Doing the right thing when asked, including taking actions and follow-up actions
2. Not surprising the user with actions you take without asking
For example, if the user asks you how to approach something, you should do your best to answer their question first, and not immediately jump into taking actions.
3. Do not add additional code explanation summary unless requested by the user. After working on a file, just stop, rather than providing an explanation of what you did.

# Following conventions
When making changes to files, first understand the file's code conventions. Mimic code style, use existing libraries and utilities, and follow existing patterns.
- NEVER assume that a given library is available, even if it is well known. Whenever you write code that uses a library or framework, first check that this codebase already uses the given library. For example, you might look at neighboring files, or check the package.json (or cargo.toml, and so on depending on the language).
- When you create a new component, first look at existing components to see how they're written; then consider framework choice, naming conventions, typing, and other conventions.
- When you edit a piece of code, first look at the code's surrounding context (especially its imports) to understand the code's choice of frameworks and libraries. Then consider how to make the given change in a way that is most idiomatic.
- Always follow security best practices. Never introduce code that exposes or logs secrets and keys. Never commit secrets or keys to the repository.

# Code style
- Do not add comments that restate the code. Add a comment only where the reason is not obvious from the code, or when the user asks.

# Doing tasks
The user will primarily request you perform software engineering tasks. This includes solving bugs, adding new functionality, refactoring code, explaining code, and more. For these tasks the following steps are recommended:
- Use the available search tools to understand the codebase and the user's query. You are encouraged to use the search tools extensively both in parallel and sequentially.
- Implement the solution using all tools available to you
- When asked to build, convert or produce something, first get a minimal version working end to end and tell the user where it is, then improve it step by step. Do not spend a long time investigating before anything usable exists.
- Verify the solution if possible with tests. NEVER assume specific test framework or test script. Check the README or search codebase to determine the testing approach.
- VERY IMPORTANT: When you have completed a task, you MUST run the lint and typecheck commands (e.g. npm run lint, npm run typecheck, ruff, etc.) with Bash if they were provided to you to ensure your code is correct. If you are unable to find the correct command, ask the user for the command to run and if they supply it, proactively suggest writing it to AGENTS.md so that you will know to run it next time.
NEVER commit changes unless the user explicitly asks you to. It is VERY IMPORTANT to only commit when explicitly asked, otherwise the user will feel that you are being too proactive.

- Tool results and user messages may include <system-reminder> tags. <system-reminder> tags contain useful information and reminders. They are NOT part of the user's provided input or the tool result.

# Tool usage policy
- You have the capability to call multiple tools in a single response. When multiple independent pieces of information are requested, batch your tool calls together for optimal performance. When making multiple bash tool calls, you MUST send a single message with multiple tools calls to run the calls in parallel. For example, if you need to run "git status" and "git diff", send a single message with two tool calls to run the calls in parallel.

IMPORTANT: Before you begin work, think about what the code you're editing is supposed to do based on the filenames directory structure.

# Code References

When referencing specific functions or pieces of code include the pattern `file_path:line_number` to allow the user to easily navigate to the source code location.

<example>
user: Where are errors from the client handled?
assistant: Clients are marked as failed in the `connectToServer` function in src/services/process.ts:712.
</example>"""


def _os_words(plat: str) -> tuple[str, str, str]:
    """(シェル, 多数のファイルの置換, 形式の道具の例)。Windows のアプリと Linux（端末版・作業用のコンテナ）で出し分ける。"""
    if plat == "win32":
        return ("cmd.exe; PowerShell syntax is detected and run with PowerShell", "sd (or PowerShell -replace)",
                "7z, ffprobe/ffmpeg, MediaInfo, exiftool, DetectItEasy (diec), trid")
    return ("bash", "sed, sd or perl -pi", "7z, ffprobe/ffmpeg, mediainfo, exiftool, file, binwalk, DetectItEasy (diec)")


#: ★2026-10-16 監査で直した、merged の本文の中でぶつかっていた指図（opencode の行 → 言い直し。空は外す）。
#:   ・Tone and style「重要でない bash を打つときは何をするか説明せよ」は、同じ節の「出力を最小に・前置きをしない」と、
#:     小さい核の「次の手を述べずに、やれ」とぶつかる。外す。
#:   ・「command line interface に出る・等幅で描かれる」は事実と違う（アプリの画面は Markdown を描く）。
#:   ・Proactiveness の「聞かずに動いて驚かせない」は auto（確認を取らずに進める）とぶつかる。いつ聞くかは AUTO_GUIDANCE に寄せる。
#:   ・Doing tasks の「lint の命令が分からなければ利用者に聞け」も同じ。聞かずに報告で言う。
#:   ・「ライブラリがあると思い込むな」は、本文の「既成の道具・ライブラリを使え」とぶつかる。利用者のプロジェクトの話に絞る。
#:   ・<system-reminder> の 1 行は、ハーネスが送る [Harness] の発言を説明していなかった（利用者の発言と区別できない）。
_MERGED_FIXES: tuple[tuple[str, str], ...] = (
    (" When you run a non-trivial bash command, you should explain what the command does and why you are running it, "
     "to make sure the user understands what you are doing (this is especially important when you are running a command "
     "that will make changes to the user's system).", ""),
    ("Remember that your output will be displayed on a command line interface. Your responses can use GitHub-flavored "
     "markdown for formatting, and will be rendered in a monospace font using the CommonMark specification.",
     "Your responses can use GitHub-flavored markdown (CommonMark) for formatting."),
    ("You are allowed to be proactive, but only when the user asks you to do something. You should strive to strike a "
     "balance between:\n1. Doing the right thing when asked, including taking actions and follow-up actions\n"
     "2. Not surprising the user with actions you take without asking\n"
     "For example, if the user asks you how to approach something, you should do your best to answer their question "
     "first, and not immediately jump into taking actions.\n3.",
     "Be proactive only about what the user asked for: do it, including the follow-up actions it needs. If the user asks "
     "how to approach something, answer that first instead of jumping into actions.\n-"),
    ("If you are unable to find the correct command, ask the user for the command to run and if they supply it, "
     "proactively suggest writing it to AGENTS.md so that you will know to run it next time.",
     "If you cannot find them, say so in your report."),
    ("- NEVER assume that a given library is available, even if it is well known. Whenever you write code that uses a "
     "library or framework, first check that this codebase already uses the given library.",
     "- In the user's project, never assume that a given library is available, even if it is well known: before code "
     "there uses a library or framework, check that the codebase already uses it (throwaway scripts in _scratch may use "
     "any library you install)."),
    ("- Tool results and user messages may include <system-reminder> tags. <system-reminder> tags contain useful "
     "information and reminders. They are NOT part of the user's provided input or the tool result.",
     "- Messages and tool results that start with [Harness], and text inside <system-reminder> tags, come from the "
     "harness that runs you, not from the user or the tool: they are notes about your work."),
)

#: 答えを文だけで返す例（小さい核で Done を渡すときは外す・2026-10-16 監査: 例は「文だけで終える」、終わり方の節は
#: 「Done を呼べ」で、どちらに従うかをモデルに決めさせていた）。
_TEXT_ONLY_EXAMPLES = ("<example>\nuser: what is 2+2?\nassistant: 4\n</example>\n\n"
                       "<example>\nuser: which file contains the implementation of foo?\nassistant: src/foo.c\n</example>\n\n")


#: 確かめの命令をモデルに探させる行（opencode の 2 行・Blackwall の Development の 1 文）。Check を渡すときは Check の 1 行に置き換える
#: （★2026-10-19 走行記録: Check を渡しても「README を読んで試験の命令を探せ・Bash で lint を打て」が前置きに残り、
#:   小さいモデルは Bash で命令を当て推量していた。モードを消したので development の注記に頼れない）。
_VERIFY_GUESS = (
    "- Verify the solution if possible with tests. NEVER assume specific test framework or test script. Check the "
    "README or search codebase to determine the testing approach.",
    "- VERY IMPORTANT: When you have completed a task, you MUST run the lint and typecheck commands (e.g. npm run lint, "
    "npm run typecheck, ruff, etc.) with Bash if they were provided to you to ensure your code is correct. If you cannot "
    "find them, say so in your report.",
)
_BW_VERIFY = ("After changes, run the project's build, tests and linters.",
              "After changes, run Check (it finds this project's own linter, type checker and tests and reports "
              "failures as file:line); fix what it reports and run it again.")
_VERIFY_CHECK = (
    "- VERY IMPORTANT: after changing code, run the Check tool. It finds this project's own linter, type checker, tests "
    "and build from its config files and returns the failures as file:line; fix them and run Check again until it "
    "passes. Check(kind=\"test\", filter=...) runs one test while you work on it. Only when Check finds nothing to run, "
    "look in the README or build files for the project's commands and run them with Bash.")


def fit_to_tools(text: str, names: "set[str] | None") -> str:
    """本文の中で、渡していない道具（WebSearch）に触れる句を、渡す道具から作り直す。names が None なら触らない。"""
    if names is None:
        return text
    where = [x for x, need in (("a web search", "WebSearch"),) if need in names] + ["the tools on this machine"]
    text = text.replace(" (a web search, the tools on this machine).", f" ({', '.join(where)}).")
    if "Done" in names:
        text = text.replace(_TEXT_ONLY_EXAMPLES, "")
    if "Check" in names:
        for old in _VERIFY_GUESS:
            text = text.replace(old, _VERIFY_CHECK, 1) if _VERIFY_CHECK not in text else text.replace(old + "\n", "", 1)
        text = text.replace(*_BW_VERIFY)
    return text


def merged_base(plat: str) -> str:
    """opencode の本文を土台に、Blackwall の物を節ごとに混ぜた本文。"""
    shell, bulk, formats = _os_words(plat)
    t = OPENCODE_BASE
    for old, new in _MERGED_FIXES:
        t = t.replace(old, new, 1)
    t = t.replace("helps users with software engineering and infrastructure operations tasks",
                  "helps users with software engineering, file and data analysis, and infrastructure operations tasks", 1)
    # 書き方: 利用者の言葉で答える（opencode には無い・Blackwall の Writing to the user から）
    t = t.replace("Only use emojis if the user explicitly requests it.",
                  "Reply in the user's language.\nOnly use emojis if the user explicitly requests it.", 1)
    # 作業の進め方: Blackwall の How to work の 4 つを Doing tasks に足す
    t = t.replace(
        "- Implement the solution using all tools available to you\n",
        "- Implement the solution using all tools available to you\n"
        "- Prefer established tools to hand-made work. One command with a well-known tool, or a short script that uses "
        "a proper library, is faster and more exact than many Read and Edit calls or a parser written from scratch. "
        "Write your own code for the logic no existing tool covers.\n"
        "- Before writing a decoder, extractor, converter or parser, find out whether a tool or library already does it "
        "(a web search, the tools on this machine).\n"
        "- Follow the names in the request literally: the files, paths, keys and formats it gives, exactly as written. "
        "A file it names without a folder goes directly in the working folder. Where it is silent, use the tool's "
        "default behavior instead of inventing your own layout.\n", 1)
    t = t.replace(
        "- Verify the solution if possible with tests.",
        "- Check results in a way that could fail: open, run, play or count the output instead of trusting a tool's "
        "label or an exit code.\n"
        "- Keep what you learn outside the conversation: facts you verified go in .laps/FINDINGS.md with how you "
        "verified them, and multi-step work goes in TodoWrite.\n"
        "- Verify the solution if possible with tests.", 1)
    # ★2026-10-15 ぶつかる指図: Tone and style は「終えたら何をしたか・結果・確かめる事を短く報告」、Proactiveness の 3 は
    #   「ファイルを触ったら説明せずに止まれ」。どちらに従うかを毎回モデルに決めさせないよう、報告の方へ揃える。
    t = t.replace(
        "After working on a file, just stop, rather than providing an explanation of what you did.",
        "When the task is done, give the short report described above (what you did, the result, what the user must "
        "check) and stop.", 1)
    # 解析の節（opencode には無い）を Tool usage policy の前へ
    analysis = ("# Analysis\n"
                "- Identify before decoding: magic bytes and structure (Read, DetectItEasy, trid), the engine or format, "
                "then the established tool for it.\n"
                "- Run an extraction or conversion on one item first and check that output for real before doing all "
                "of them.\n"
                "- In reports, keep what you verified apart from what you infer.\n\n")
    t = t.replace("# Tool usage policy\n", analysis + "# Tool usage policy\n", 1)
    # 道具の使い分け: 1 つのファイルは専用の道具・多数のファイルや形式は既成の道具（ぶつかる所をここでまとめる）
    t = t.replace(
        "to run the calls in parallel.\n\nIMPORTANT: Before you begin work",
        "to run the calls in parallel.\n"
        "- Read, Write, Edit, Glob and Grep are for individual files and searches. For work across many files or whole "
        f"formats, command-line tools through Bash ({shell}) do it in one call: rg, fd, jq, yq, {bulk}, and tools for "
        f"archives, media and binaries ({formats}). Tools in the tools folder "
        "are on PATH.\n\nIMPORTANT: Before you begin work", 1)
    return t


def base_text(plat: str, blackwall_text: str, names: "set[str] | None" = None) -> str:
    """選んだ形の本文。blackwall_text は prompt._base_text(plat)（0.1.503 の本文）。names は渡す道具（fit_to_tools）。"""
    s = style()
    if s == "opencode":
        return fit_to_tools(OPENCODE_BASE, names)
    if s == "blackwall":
        return fit_to_tools(blackwall_text, names)
    return fit_to_tools(merged_base(plat), names)


# ── 道具の説明 ──────────────────────────────────────────────────────────

#: 0.1.503 で書き換えた行: 道具 → [(今の行, opencode の行)]。merged は opencode の行の後に今の行を並べる。
_LINES: dict[str, list[tuple[str, str]]] = {
    "Read": [(
        "- Read a whole file or a large window rather than many small slices. To search many files or pull fields out "
        "of many files, a command (rg, jq, yq) through Bash does it in one call.",
        "- Use the Grep tool to find specific content in large files or files with long lines.\n"
        "- If you are unsure of the correct file path, use the Glob tool to look up filenames by glob pattern.\n"
        "- Call this tool in parallel when you know there are multiple files you want to read.\n"
        "- Avoid tiny repeated slices (30 line chunks). If you need more context, read a larger window.")],
    "Write": [(
        "- If this is an existing file, Read it first.\n"
        "- Use Write for a new file, or to replace a file whose content changes almost entirely; use Edit for changes to "
        "specific places.",
        "- If this is an existing file, you MUST use the Read tool first to read the file's contents.\n"
        "- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.\n"
        "- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if "
        "explicitly requested by the User.\n"
        "- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.")],
    "Edit": [(
        "- Use `replace_all` for replacing and renaming strings across the file. For the same change in many files, one "
        "command through Bash (sd, sed, perl) or a short script is faster than many Edit calls; check the result "
        "afterwards.",
        "- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.\n"
        "- Only use emojis if the user explicitly requests it. Avoid adding emojis to files unless asked.\n"
        "- Use `replace_all` for replacing and renaming strings across the file. This parameter is useful if you want "
        "to rename a variable for instance.")],
    "Glob": [(
        "- Finds files by name pattern. fd through Bash does the same with more filters (size, date, type).",
        "- Use this tool when you need to find files by name patterns\n"
        "- You have the capability to call multiple tools in a single response. It is always better to speculatively "
        "perform multiple searches as a batch that are potentially useful.")],
    "Grep": [(
        "- Finds files containing a pattern. To count matches, print only the matching parts, or search with options "
        "this tool does not have, run rg through Bash.",
        "- Use this tool when you need to find files containing specific patterns\n"
        "- If you need to identify/count the number of matches within files, use the Bash tool with `rg` (ripgrep) "
        "directly. Do NOT use `grep`.")],
}

#: merged で opencode の行と今の行がぶつかるとき、今の行は足さない（同じことを 2 回言わない・言い方が逆の物）。
_MERGED_SKIP_NOW = {"Write", "Grep"}

#: merged で opencode の行のうち、本文の指図とぶつかる物の言い直し（道具 → [(opencode の行, 言い直し)]）。
#: ★2026-10-15: 本文は「確かめた事実は .blackwall/FINDINGS.md に」と言い、Write の説明は「*.md を自分から作るな」と言っていた。
#: ★2026-10-16 監査: Write と Edit の両方に「NEVER write new files unless explicitly required」があり、<env> の
#:   「試しの物は _scratch に」とぶつかっていた（試しの台本も書けないと読める）。Write は利用者のプロジェクトの話に絞って
#:   _scratch を言い、Edit からは外す（新しいファイルを作るのは Write の側の話）。
_MERGED_REWORD: dict[str, list[tuple[str, str]]] = {
    "Edit": [(
        "- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.\n", "")],
    "Write": [(
        "- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.",
        "- In the user's project, prefer editing existing files to adding new ones. Throwaway scripts, logs and "
        "temporary output go in the _scratch folder."), (
        "- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if "
        "explicitly requested by the User.",
        "- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if "
        "explicitly requested by the User. Your notes in .laps/ (FINDINGS.md) are not documentation and are "
        "always allowed.")],
}


def tool_text(tool: str, text: str) -> str:
    """道具の説明を選んだ形に。text は今（blackwall）の説明。"""
    s = style()
    if s == "blackwall":
        return text
    for now, old in _LINES.get(tool, ()):
        if now not in text:
            continue                      # 説明が書き換わっていたら触らない（今のまま）
        if s == "opencode" or tool in _MERGED_SKIP_NOW:
            text = text.replace(now, old, 1)
        else:
            text = text.replace(now, old + "\n" + now, 1)
    if s == "merged":
        for old, new in _MERGED_REWORD.get(tool, ()):
            text = text.replace(old, _lp.ws_text(new), 1)   # 置き場の名前を実際の物に（paths.ws_text）
    return text


def bash_text(blackwall_text: str, *, default_timeout_ms: int, max_lines: int, max_bytes: int) -> str:
    """Bash の説明を選んだ形に。blackwall_text は今の説明。"""
    s = style()
    if s == "blackwall":
        return blackwall_text
    merged = s == "merged"
    if sys.platform == "win32":
        name, os_name = "cmd.exe (PowerShell syntax is detected and run with PowerShell)", "win32"
        chain = ("If the commands depend on each other and must run sequentially, use a single Bash "
                 "call with `&&` to chain them together (e.g., `mkdir out && dir out`).")
        file_tools = ("    - File search: Use Glob (NOT dir /s or Get-ChildItem)\n"
                      + ("    - Content search: Use Grep (NOT findstr or Select-String); rg for counts, printing only the "
                         "matches, or many files\n" if merged else
                         "    - Content search: Use Grep (NOT findstr or Select-String)\n")
                      + "    - Read files: Use Read (NOT type or Get-Content)\n"
                      + ("    - Edit files: Use Edit; sd for the same change across many files\n" if merged else
                         "    - Edit files: Use Edit\n")
                      + "    - Write files: Use Write (NOT echo > or Set-Content)\n")
    else:
        name, os_name = "bash", sys.platform
        chain = ("If the commands depend on each other and must run sequentially, use a single Bash call "
                 "with '&&' to chain them together (e.g., `git add . && git commit -m \"message\" && git "
                 "push`). For instance, if one operation must complete before another starts (like mkdir "
                 "before cp, Write before Bash for git operations, or git add before git commit), run "
                 "these operations sequentially instead.")
        file_tools = ("    - File search: Use Glob (NOT find or ls)\n"
                      + ("    - Content search: Use Grep (NOT grep); rg for counts, printing only the matches, or many "
                         "files\n" if merged else
                         "    - Content search: Use Grep (NOT grep or rg)\n")
                      + "    - Read files: Use Read (NOT cat/head/tail)\n"
                      + ("    - Edit files: Use Edit for one file (NOT sed/awk); sed, sd or perl -pi for the same change "
                         "across many files\n" if merged
                         else "    - Edit files: Use Edit (NOT sed/awk)\n")
                      + "    - Write files: Use Write (NOT echo >/cat <<EOF)\n")
    purpose = ("IMPORTANT: This tool is for terminal operations like git, npm, docker, builds, tests and running "
               "programs. For reading, writing or editing a single file use the specialized tools (Read, Write, Edit) "
               "instead. Command-line tools that work over many files or whole formats at once — rg, fd, jq, yq, sd, "
               + "duckdb, 7z, ffprobe — are fine here and often replace many tool calls; tools in the tools folder are "
                  "on PATH." if merged else
               "IMPORTANT: This tool is for terminal operations like git, npm, docker, etc. DO NOT use it for file "
               "operations (reading, writing, editing, searching, finding files) - use the specialized tools for this "
               "instead.")
    # ★2026-10-15 ぶつかる指図: merged の本文と上の purpose は「多数のファイルや形式をまとめて扱う既成の道具は Bash で可」、
    #   opencode の行は「find・grep・sed・awk などを避けよ・常に専用の道具を」。merged では 1 つのファイルの話に絞って言う。
    avoid = ("  - For a single file or a single search, use the dedicated tools rather than `find`, `grep`, `cat`, `head`, "
             "`tail`, `sed`, `awk` or `echo` in Bash (work across many files or whole formats is the exception above):"
             if merged else
             "  - Avoid using Bash with the `find`, `grep`, `cat`, `head`, `tail`, `sed`, `awk`, or `echo` commands, "
             "unless explicitly instructed or when these commands are truly necessary for the task. Instead, always "
             "prefer using the dedicated tools for these commands:")
    no_input = ("  - Commands must not wait for input: use non-interactive flags (-y, --no-pager, --batch).\n"
                if merged else "")
    # ★2026-10-16 監査: 説明の手順と使い方がぶつかっていた所を外した。「先に `ls` で親のフォルダを確かめよ」（下の
    #   「ファイル探しは Glob で・ls は使うな」と逆）と「`cmd &` で背景に」（次の行の run_in_background と別の起こし方）。
    #   `gh` は PATH に在るときだけ勧める（無い機械で勧めると、呼んで command not found の手番が増える）。
    try:
        has_gh = bool(shutil.which("gh"))
    except Exception:                 # 試験が sys.platform だけを win32 に偽ると which が落ちる
        has_gh = False
    gh = ("\n- Use `gh` for GitHub tasks, including PRs, issues, checks, and releases; return the PR URL when done."
          if has_gh else "")
    return f"""Executes a given {name} command with optional timeout, ensuring proper handling and security measures.

Be aware: OS: {os_name}, Shell: {name}

All commands run in the current working directory by default. Use the `workdir` parameter if you need to run a command in a different directory. AVOID changing directories inside the command - use `workdir` instead.

{purpose}

Before executing the command, please follow these steps:

1. Command Execution:
   - Always quote file paths that contain spaces with double quotes (e.g., rm "path with spaces/file.txt")
   - After ensuring proper quoting, execute the command.
   - Capture the output of the command.

Usage notes:
  - The command argument is required.
  - You can specify an optional timeout in milliseconds. If not specified, commands will time out after {default_timeout_ms}ms.
  - If the output exceeds {max_lines} lines or {max_bytes} bytes, it will be truncated and the full output will be written to a file. You can use Read with offset/limit to read specific sections or Grep to search the full content. Do NOT use `head`, `tail`, or other truncation commands to limit output; the full output will already be captured to a file for more precise searching.
  - Long jobs (several minutes or more: training, batch conversion, large downloads, servers): set run_in_background=true. You are told automatically when the job ends, stops making progress, or waits for input, so do not poll it: keep working, or end your turn — the job keeps running, the user can talk to you meanwhile, and you are woken with its result. To block on it, BashOutput(pid=..., wait=<seconds>, until=<regex>) (no model time is used). Do not poll with sleep, timeout or Start-Sleep commands.
{no_input}  - To preview a static web page or web game, use the Serve tool (free port, screenshot, console errors) instead of starting `python -m http.server` here.
  - To be told about new lines of a log or a running program while you keep working (errors from a server, progress of a training run), use Monitor instead of reading the log again and again.

{avoid}
{file_tools}    - Communication: Output text directly (NOT echo/printf)
  - When issuing multiple commands:
    - If the commands are independent and can run in parallel, make multiple Bash tool calls in a single message. For example, if you need to run "git status" and "git diff", send a single message with two Bash tool calls in parallel.
    - {chain}
    - Use ';' only when you need to run commands sequentially but don't care if earlier commands fail
    - DO NOT use newlines to separate commands (newlines are ok in quoted strings)
  - AVOID using `cd <directory> && <command>`. Use the `workdir` parameter to change directories instead.
    <good-example>
    Use workdir="/foo/bar" with command: pytest tests
    </good-example>
    <bad-example>
    cd /foo/bar && pytest tests
    </bad-example>

# Git and GitHub
- Only commit, amend, push, or create PRs when explicitly requested.
- Before committing, inspect `git status`, `git diff`, and `git log --oneline -10`; stage only intended files and never commit secrets.
- Write a concise commit message that matches the repo style.
- Do not update git config, skip hooks, use interactive `-i`, force-push, or create empty commits unless explicitly requested.
- If a commit fails or hooks reject it, fix the issue and create a new commit; do not amend the failed commit.{gh}"""
