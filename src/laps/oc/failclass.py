"""道具の失敗と推論機の誤りを、短い分類に分けて数える（2026-10-18・エージェント全体の点検）。

オーナー 2026-10-18「Blackwall エージェント全体を調べてアップデートが必要な部分を見つけてください」→「任せます」。
★なぜ要るか: 走行記録（直近 10 日・130 本）で道具の失敗の率は Bash 29%・Lab 32%・Mcp 37%・Edit 25% だったが、
  記録には「失敗した」しか残っておらず、理由を分けるには出力の本文を読むしかなかった。本文にはオーナーの私的な資料が
  入りうるので、読む形の調べは安全の判定に止められる（10-18 に実際に止められた）。
  ⇒ **Blackwall 自身が書く決まった文**（「その文字列が無い」「時間切れ」「exit code: N」など）と結果の種類だけで分ける。
  ★命令や道具が出した文（走行の中身）は見ない。頭 300 字と末尾 800 字の、Blackwall の決まった文だけを照らす。

使う所: oc/loop.py が道具ごとに `fail:<道具>:<分類>` を数え、推論機の誤りを `backend_error:<分類>` で数える
（数えの欄は記録で切り詰められない）。gateway_record.py は道具の行に `kind` と `cls` を残す。
"""
from __future__ import annotations

import re

_EXIT = re.compile(r"exit code:?\s*(-?\d+)")
_LAB_EXIT = re.compile(r"\[exit (-?\d+) ·")

#: 結果の**先頭**にあるときだけ当てる決まった文（Blackwall の誤りは結果の頭から始まる）。上から順に当てる。
#: ★先頭に限る理由（2026-10-18 の試験で出た）: 頭 300 字のどこかで当てると、命令の出力の途中にたまたま同じ語
#:   （「Could not find old_string」など）があるだけで別の分類になった＝中身で分けることになる。
_START = (
    ("Could not find old_string", "not_found"),
    ("Found multiple matches for old_string", "multiple"),
    ("No changes to apply", "no_change"),
    ("replace_all refused", "replace_all_refused"),
    ("old_string cannot be empty", "empty_old"),
    ("old_string and new_string are required", "missing_arg"),
    ("edits item(s)", "bad_edits"),
    ("Path is a directory", "is_dir"),
    ("workdir does not exist", "no_workdir"),
    ("File not found", "no_file"),
    ("Cannot read ", "cannot_read"),
    ("Cannot write ", "cannot_write"),
    ("[Harness] Not run: this is the same call", "same_call_blocked"),
    ("Tool execution aborted", "aborted"),
    ("Not run: the user stopped the task", "aborted"),
    ("This destructive command was not run", "denied"),
    ("This command matches deny_patterns", "denied"),
    ("the read-only policy allows only", "denied"),
    ("call needs server and tool", "missing_arg"),
    ("Invalid glob pattern", "bad_pattern"),
    ("Grep failed: invalid", "bad_pattern"),
    # ★道具の名前の誤り・引数の形の誤り（loop._run_one が書く文・2026-10-18 オーナー「独自のツールとかもローカル LLM でも
    #   ちゃんと認識できるようになっていますか？」）。それまでは "other" に混ざり、名前を取り違えたのか分からなかった。
    ("Model tried to call unavailable tool", "unknown_tool"),
    ("The arguments provided to the tool are invalid", "bad_args"),
    # ★アプリのブラウザ（Browser・oc/browsertool と app の browser.rs・serve の _app_call が書く文・2026-10-18）。
    #   オーナー機の 34 走行で Browser の失敗 6 件が全部 "other" だった（何が起きたか数えで分からない）。
    ("the app's browser could not be used", "browser_no_app"),
    ("the app did not answer within", "browser_no_app"),
    ("this app does not handle", "browser_no_app"),
    ("the in-app browser runs only on Windows", "browser_no_app"),
    ("the app sent an answer that could not be read", "browser_bad_reply"),
    ("unreadable answer from the page", "browser_bad_reply"),
    ("empty answer from the app", "browser_bad_reply"),
    ("no element matched", "browser_no_element"),
    ("so it was not clicked", "browser_covered"),
    ("has no place on the page", "browser_covered"),
    ("dialog is open", "browser_dialog"),
    ("the page command timed out", "browser_page_timeout"),
    ("the page did not take the input", "browser_page_timeout"),
    ("the page could not be opened", "browser_open_failed"),
    ("no input field matched", "browser_no_element"),
    ("bad selector", "browser_bad_selector"),
    ("the page did not answer in time", "browser_page_timeout"),
    ("the page gave no answer", "browser_page_timeout"),
    ("Can't open the page", "browser_nav_failed"),
    ("Not a valid address", "browser_bad_url"),
    ("Only http and https pages can be opened", "browser_bad_url"),
    ("no element has number", "browser_no_element"),     # 番号（look の札）の要素が無い（2026-10-19）
    ("nothing is at that position", "browser_no_element"),
    ("the page picture", "browser_no_picture"),
    ("unreadable picture answer", "browser_no_picture"),
    ("give ref (the number from", "missing_arg"),
    ("key is required for action=key", "missing_arg"),
    ("Enter an address", "missing_arg"),
    ("action must be one of", "bad_args"),
    ("Unknown browser action", "bad_args"),
)
#: 道具ごとの決まった文（結果の先頭）。★2026-10-18: 直近 10 日の走行で Check・Listen・知識サーバー・WebFetch の失敗が
#:   "other" に残っていた（各 1 件）。その道具が自分で書く文だけを、その道具に限って当てる（他の道具の出力には当てない）。
_BY_TOOL: dict[str, tuple[tuple[str, str], ...]] = {
    "Check": (
        ("Checked ", "check_problems"),                # 検査は走り、プロジェクトの側に指摘があった（道具の誤りではない）
        ("No check command was found here", "check_none"),
        ("kind must be one of", "bad_args"),
    ),
    "Listen": (
        ("Listened to ", "listen_all_failed"),         # 走ったが、どのファイルも文字にできなかった
        ("no audio files to listen to", "no_file"),
        ("not found: ", "no_file"),
        ("give `path`", "missing_arg"),
        ("language must be", "bad_args"),
        ("arguments must be a JSON object", "bad_args"),
        ("whisper-cli did not finish within", "timeout"),
        ("could not download", "download_failed"),
        ("downloaded ", "download_failed"),
        ("not enough disk space", "no_space"),
        ("cancelled", "aborted"),
    ),
    "WebFetch": (
        ("fetch failed:", "fetch_failed"),
        ("名前を解決できません", "dns"),
        ("blocked private/internal URL", "blocked_private"),
        ("url must start with http", "bad_url"),
        ("missing 'url'", "missing_arg"),
        ("転送が ", "too_many_redirects"),
        ("http(s) 以外への転送", "bad_redirect"),
    ),
}
_HTTP_HEAD = re.compile(r"^HTTP (\d{3}) for ")
#: 先頭の 1 行の中ならどこでもよい文（名前・パスが先に来る形）。★命令を走らせる道具には使わない（出力が先頭に来るため）。
_FIRST_LINE = (
    ("is out of range for this file", "bad_offset"),
    ("is not configured", "mcp_not_configured"),
    ("could not start", "mcp_start_failed"),
    ("has no tool", "mcp_no_tool"),
    ("no tool '", "mcp_no_tool"),
    ("needs argument(s)", "missing_arg"),
    (" is required", "missing_arg"),
    ("__unparsed_arguments__", "bad_args"),
)
#: 命令を走らせる道具（結果の頭は命令の出力＝中身）。終了コードと先頭の決まった文だけで分ける。
_EXEC = frozenset({"Bash", "BashOutput", "Lab", "Serve", "Monitor", "App"})


def classify(name: str, ok: bool, kind: str, text: str) -> str | None:
    """失敗の分類（成功なら None）。分からなければ "other"。

    ★見るのは Blackwall が書く決まった文と終了コードだけ。命令の出力の中の語では分けない（中身を読まない）。"""
    if ok:
        return None
    k = (kind or "error").strip().lower()
    if k in ("denied", "cancelled", "empty"):
        return k
    s = text or ""
    head = s[:300]
    head = head[len("Error: "):] if head.startswith("Error: ") else head
    first = head.split("\n", 1)[0]
    tail = s[-800:]
    if "User aborted the command" in tail:
        return "aborted"
    if "exceeding timeout" in tail:
        return "timeout"
    for pat, c in _START:
        if head.startswith(pat):
            return c
    for pat, c in _BY_TOOL.get(name, ()):
        if head.startswith(pat):
            return c
    if name == "Check":                                 # 場所（パス）が先に来る形
        if " is not inside a project" in first:
            return "check_no_project"
        if first.rstrip().endswith(" does not exist."):
            return "no_file"
    if name == "WebFetch":
        m = _HTTP_HEAD.match(head)
        if m:
            return f"http_{m.group(1)}"
        # 文章でない物を断る返事は、見出し（URL・Status・Content-Type・Bytes）の後に決まった文が来る
        if head.startswith("URL: ") and "文章ではない内容なので読み込みませんでした" in head:
            return "not_text"
    # ★終了コードは Blackwall が末尾の決まった欄に書く（Bash の shell_metadata・Lab の [exit N · …]）。
    m = _EXIT.search(tail) or _LAB_EXIT.search(tail)
    if m:
        n = int(m.group(1))
        return f"exit:{n}" if -1000 < n < 1000 else "exit:large"      # Windows の大きな値は 1 つにまとめる
    if name in _EXEC:
        return "timeout" if "timed out" in first else "other"
    for pat, c in _FIRST_LINE:
        if pat in first:
            return c
    if re.match(r"^\S+/\S+: ", first) and name in ("Mcp", "Tools"):
        return "mcp_tool_error"                                     # MCP の道具そのものが誤りを返した
    if re.match(r"^File \S.* not found", first):
        return "no_file"
    if "timed out" in first:
        return "timeout"
    return "other"


def backend_class(e: BaseException) -> str:
    """推論機との線の誤りの分類（推論機・HTTP の層の文だけを見る。会話の中身は入らない）。

    ★「枠が止められた」（llama.cpp は共有の KV が溢れると処理中の枠を止める）かを、設定を変える前に数えるため。"""
    t = f"{type(e).__name__}: {e}"[:600].lower()
    if any(w in t for w in ("kv cache", "context size", "n_ctx", "exceeds the available context", "context shift",
                            "context length", "too many tokens")):
        return "context"
    if any(w in t for w in ("slot", "no free", "unavailable", "busy")):
        return "slot"
    if "degenerate" in t or "出力が崩れ" in t:
        return "degenerate"
    m = re.search(r"\b(?:http|status(?:_code)?)[\s:=]*([45]\d\d)\b", t)
    if m:
        return f"http_{m.group(1)}"
    if any(w in t for w in ("timed out", "timeout", "readtimeout")):
        return "timeout"
    if any(w in t for w in ("connect", "refused", "unreachable", "reset by peer", "remoteprotocolerror")):
        return "connection"
    return type(e).__name__[:40] or "other"
