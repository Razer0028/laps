"""端末描画 — serve.py のイベント列を Rich で描く（本文・道具の束・差分・通知・done の統計）。

★方針（DESIGN-linux-term.md §0 / §2）: **本体が送った構造（summary.head/tail/patch）を描くだけ**。
  文字列を解析して表示を作らない（道具ごとの要約はエンジンが送る。旧い tool_summary.py は 2026-11-19 に外した）。
★行の型は Claude Code と同じ（REPORTS/claude-code-display-design-20260823.md §1・§2）:
      ● 名前 (summary.head)
        ⎿  summary.tail
  点の色＝実行中 dim／成功 ok／失敗 red。連続する読み取り系は本文が来るまで溜めて 1 行に圧縮（§5-2）。
★Rich の Live は同時に 1 つ。Renderer が唯一の Live を持つ（波・本文・実行中の道具は全部この中）。
  Live は transient（done で消える）なので、残る画面は「上に流した行」だけ＝本文は end_turn で 1 回だけ確定印字する。
"""
from __future__ import annotations

import contextlib
import json
import threading
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pygments.token import Comment, Keyword, Name, Number, String, Token
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import CodeBlock, Markdown, TableElement
from rich.segment import Segment, Segments
from rich.style import Style
from rich.syntax import ANSISyntaxTheme, Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import theme as T

__all__ = ["Renderer", "read_kind", "fold_physical", "patch_lines", "wrap_with_gutter",
           "fit_parts", "cell_width"]

TAIL = "  ⎿  "          # Claude Code と同じ接頭（U+23BF）
INDENT = "     "         # 結果の続き行（⎿ の幅に揃える）
HEAD_ARG_MAX = 60        # summary が無いときの引数要約の長さ
# 畳みは**物理行**で数える（SPEC 項目 11）。論理 10 行が幅 80 で 16 物理行に膨らみ、
# 24 行の窓では「どの道具が失敗したか」の見出しが画面外へ押し出されていた（実測）。
BASH_HEAD_ROWS = 1       # Bash 成功: 先頭 1 物理行
BASH_TAIL_ROWS = 2       # ＋末尾 2 物理行（いちばん新しい行が見たい）
FAIL_HEAD_ROWS = 3       # 失敗: 先頭 3 物理行
FAIL_TAIL_ROWS = 2       # ＋末尾 2 物理行

# -- 画面に出る語は 1 か所に（SPEC 項目 23）-----------------------------------
# ★システムの生の値（exit 5・パス・識別子）は訳さない。訳すと grep できなくなる。
SEP = "  "               # 欄の区切り＝空白 2 桁。`·`(U+00B7) は East Asian Ambiguous で
                         # 幅が端末依存なので使わない。
DONE_SEP = SEP           # done 行も同じ区切り。★2026-08-29 の合流でここを SEP にし、
                         #   `·`(U+00B7) は画面から 0 個になった（scripts/check_tui_width.sh
                         #   が門として数えている）。名前は呼び出し側の意図が読めるので残す。
W_NO_RESULT = "中断（結果なし）"          # 旧 "no result returned"
W_THINK = "思考 {n} 秒"                   # 旧 "Thought for Ns"
W_ARGS = "引数"                           # 旧 "input"
W_RESULT = "結果"                         # 旧 "result"
W_RUNNING = "（実行中）"                  # 旧 "(running)"
W_EMPTY = "（空）"                        # 旧 "(empty)"
W_REST = "… 残り {n} 行"                  # 旧 "… +N lines (/expand)"
W_EXPAND = "→ /expand で全文"             # 失敗と畳みの「次の一手」（SPEC 項目 19）
W_CANCEL = "中断を要求しました（もう一度 Ctrl-C で終了）"
W_CANCEL_STALL = "まだ止まっていない（{secs}）・もう一度 Ctrl-C で強制終了"
W_HINT_RUN = "Ctrl-C 中断"                # 走行中の頭行の末尾（SPEC 項目 14）
# -- 文脈まわりの 4 状態（SPEC 項目 16）。本体が文面を持たないときの既定 ------------
W_CTX_WARN = "コンテキストウィンドウが狭くなっています"          # 警告（まだ何も失っていない）
W_CTX_COMPACT = "コンテキストを圧縮 {before} → {after}"    # 圧縮（自動で縮めた・失ったものは無い）
W_CTX_COMPACTED = "コンテキストを圧縮しました"             # 圧縮（本体が数字を送っていないとき）
W_CTX_RESET = "コンテキストをリセットしました"             # 仕切り直し（会話は捨てた・作業は続く）
W_CTX_RESET_KEPT = "ここまでの要約だけ引き継ぎ"     # ★何が失われたかを同じ行の中で言う
W_CTX_EXHAUSTED = "★コンテキストが足りなくなったので打ち切りました"   # 打ち切り（走行は終わった）
W_CTX_NEXT = "→ 作業を小さく分けるか、大きいコンテキストウィンドウで再起動してください"

# done の outcome → 日本語（SPEC 項目 8④）。
# ★ここに無い値は**そのまま出す**（内部識別子でも、隠すより出すほうが調べられる）。
_OUTCOME_JA = {
    "accepted": "",
    "accepted_after_pushback": "差し戻しあり",
    "cancelled": "中断",
    "user_stop": "ユーザーが終了",        # 2026-09-04: AskUser の答えが停止の意図（回答の強制）
    "max_iters": "反復上限",
    "context_exhausted": "コンテキスト切れ",
    "context_overflow": "コンテキストあふれ",
    "budget_exhausted": "予算切れ",
    "loop_detected": "同じ操作の繰り返し",
    "crashed": "エンジンが停止した",
    "backend_error": "推論サーバーに接続できない",
    "declined": "モデルが拒否した",       # 2026-10-30: 推論機が応答を断った（finish_reason=content_filter）
    "phase_failed": "ステップ 不合格",
    "unknown": "不明",
    "": "終了の通知が届いていない",
}
CODE_THEME = "bw"        # 旧: pygments の名前。★実際の描画は _CODE_THEME（下）＝pygments の "bw" は
                         #   background_color が白で、黒い端末ではコードが白い帯になる（検証で実測）。


class _CodeTheme(ANSISyntaxTheme):
    """コードブロックの色: 本文色 1 色＋太字／補助色だけ。背景は塗らない（§2「端末の黒のまま」）。

    pygments の既製テーマは全て背景色を持ち（"bw" でも #ffffff）、Rich の Syntax はそれを
    そのまま塗る。黒い端末に白い帯が出るので、背景を持たないテーマを自分で持つ。
    """

    def get_background_style(self) -> Style:
        return Style(color=T.TEXT)          # bgcolor 無し＝端末の背景のまま


_CODE_THEME = _CodeTheme({
    Token: Style(color=T.TEXT),
    Keyword: Style(bold=True),
    Name.Function: Style(bold=True),
    Name.Class: Style(bold=True),
    Name.Decorator: Style(color=T.TEXT_2),
    Comment: Style(color=T.TEXT_3),
    String: Style(color=T.TEXT_2),
    Number: Style(color=T.TEXT_2),
})


class _CodeBlock(CodeBlock):
    """Rich の CodeBlock は Markdown.code_theme（文字列）しか渡せないので、テーマ物を使う版。"""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        yield Syntax(code, self.lexer_name, theme=_CODE_THEME, word_wrap=True, padding=1)


class _TableElement(TableElement):
    """表の上下の空の枠線を出さない（2026-10-21 オーナー「Linux の TUI 一旦見直しますか。会話の部分」）。
    ★Rich の表（box.SIMPLE）は上と下に空白だけの枠線を 1 行ずつ持ち、段落の間の空行と合わせて表の前後が 2 行ずつ空いていた。"""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for t in super().__rich_console__(console, options):
            if isinstance(t, Table):
                t.show_edge = False
            yield t


class _Markdown(Markdown):
    elements = {**Markdown.elements, "fence": _CodeBlock, "code_block": _CodeBlock, "table_open": _TableElement}


# ── 日本語の折り返し（2026-10-21）────────────────────────────────────────────────────────────
# ★Rich は空白でしか行を割らない（rich._wrap.re_word = \s*\S+\s*）。日本語の文は空白の無い 1 語として扱われ、
#   「Console は Pi の systemd」の後ろの日本語のかたまりが丸ごと次の行へ送られて、行の右が大きく空いていた（TUI で実測）。
#   日本語は 1 字ずつを語にし、閉じ括弧・句読点は前の字に、開き括弧は後ろの字に付ける（行頭に「。」、行末に「（」を置かない）。
#   英数字の語はこれまでどおり空白で割る。Rich の内部を差し替えるので、形が変わっていたら何もしない。
_CJK = "　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯"
_CJK_OPEN = "「『（［【〔〈《｛“‘"
_CJK_CLOSE = "、。，．・：；？！」』）］】〕〉》｝”’ー…々ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ"
_CJK_WORD = re.compile(rf"\s*(?:[{_CJK_OPEN}]*[{_CJK}][{_CJK_CLOSE}]*|[^\s{_CJK}]+[{_CJK_CLOSE}]*)\s*")


def _cjk_wrap() -> None:
    try:
        import rich._wrap as _rw
        if isinstance(getattr(_rw, "re_word", None), re.Pattern) and callable(getattr(_rw, "words", None)):
            _rw.re_word = _CJK_WORD
    except Exception:
        pass


_cjk_wrap()

# 読み取り系の判定（Claude Code 458118 の集合を設計 §2 の範囲に絞ったもの）。
# Bash は先頭語で決める。ここに無い語は「実行」。
_READ_TOOLS = {"Read": "read", "Grep": "search", "Glob": "search"}
_BASH_WORDS = {
    "cat": "read", "head": "read", "tail": "read", "wc": "read", "stat": "read",
    "find": "search", "grep": "search", "rg": "search",
    "ls": "list", "tree": "list", "du": "list",
}
_KIND_LABEL = {"read": "読み取り", "search": "検索", "list": "一覧"}
_ACCEPTED = {"accepted", "accepted_after_pushback", "user_stop"}   # user_stop は利用者の意思どおり＝✓
_BAD_STATUS = {"error", "timeout", "notfound", "cancelled"}


# ── 録画のための切り替え（2026-10-06・オーナー「TUI を美麗に作り直して録画に使えないか」）──────
# ★どれも既定 off。既定の見た目（幅の門・色の実測で作り込んだ行の形）は変えない。
#   --record / --privacy / --card / --demo（app.py）がこの環境変数を立てる。
#   LAPS_RECORD   録画向け: カード表示＋状態行を 10 秒に 1 回だけ描き直す（asciinema の
#                      --idle-time-limit で待ち時間を詰められるように。波と脈動印は止める）
#   LAPS_PRIVACY  伏せ表示: IP・ホームのパス・ユーザー名・ホスト名を伏せ字にする
#   LAPS_TUI_STYLE=card  カード表示だけ（道具を枠つきのカード・思考を縦線の欄・依頼を ❯）
#   ★カードの罫線（╭─╮│）は East Asian Ambiguous＝曖昧幅を 2 桁にしている端末では枠が崩れる。
#     既定に入れない理由はこれ（既定は幅 1 で揺れない字だけで描いている・theme.py）。
def record_on() -> bool:
    return (os.environ.get("LAPS_RECORD") or "").strip().lower() in ("1", "on", "true", "yes")


def privacy_on() -> bool:
    return (os.environ.get("LAPS_PRIVACY") or "").strip().lower() in ("1", "on", "true", "yes")


def card_on() -> bool:
    return record_on() or (os.environ.get("LAPS_TUI_STYLE") or "").strip().lower() == "card"


RECORD_FPS = 0.1     # 録画モードの描き直し＝10 秒に 1 回

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
# 伏せない IPv4: 文書の例示用の予約範囲（RFC 5737）・ループバック・未指定・ブロードキャスト。
_IPV4_KEEP = ("192.0.2.", "198.51.100.", "203.0.113.", "127.", "0.0.0.0", "255.255.255.255")
_HOME_UNIX = re.compile(r"/home/[^/\s'\"`:]+|(?<![\w.-])/root(?=[/\s'\"`:)]|$)")
# ★Windows のユーザー名は空白（全角の U+3000 を含む）を持ちうる＝空白で止めない。次の区切りまでを名前とする。
_HOME_WIN = re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\n'\"`<>|]+?(?=[\\/'\"`\n]|$)")
_USER_AT = re.compile(r"(?<![\w.-])[A-Za-z0-9._-]+@(?=[A-Za-z0-9<\[])")


def _private_names() -> list[tuple[str, str]]:
    """伏せる名前と置き換え先（この機械のユーザー名→<user>・ホスト名→<host>）。
    短すぎる・一般語は伏せない（誤爆が多い）。長い名前から当てる。"""
    import getpass
    import socket
    out: dict[str, str] = {}
    names = [os.environ.get("USER"), os.environ.get("USERNAME"), os.environ.get("LOGNAME")]
    try:
        names.append(getpass.getuser())
    except Exception:
        pass
    for v in names:
        if v and len(v) >= 3 and v.lower() not in ("root", "user", "admin", "test"):
            out[v] = "<user>"
    try:
        h = socket.gethostname().split(".")[0]
        if h and len(h) >= 3:
            out.setdefault(h, "<host>")
    except Exception:
        pass
    return sorted(out.items(), key=lambda kv: len(kv[0]), reverse=True)


def mask_private(text: str) -> str:
    """伏せ表示（LAPS_PRIVACY）。IP・ホームのパス・ユーザー名・ホスト名を伏せ字にする。"""
    if not text:
        return text

    def ip4(m: "re.Match[str]") -> str:
        v = m.group(0)
        if v.startswith(_IPV4_KEEP) or any(int(x) > 255 for x in v.split(".")):
            return v                       # 例示用の範囲・版番号らしいもの（256 以上）は残す
        return "<ip>"

    def ip6(m: "re.Match[str]") -> str:
        v = m.group(0)
        # 時刻（12:34:56）を伏せない: 16 進の英字か "::" を含むものだけ。
        if "::" not in v and not re.search(r"[A-Fa-f]", v):
            return v
        return "<ip>"

    text = _IPV4.sub(ip4, text)
    text = _IPV6.sub(ip6, text)
    text = _HOME_WIN.sub("~", text)
    text = _HOME_UNIX.sub("~", text)
    text = _USER_AT.sub("user@", text)
    for name, repl in _private_names():
        text = re.sub(rf"(?<![\w-]){re.escape(name)}(?![\w-])", repl, text)
    return text


def _mask_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return mask_private(obj)
    if isinstance(obj, dict):
        return {k: _mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_obj(v) for v in obj]
    return obj


# 道具の種類の札（カード表示）。
_CARD_KIND = {"Bash": "実行", "Read": "読む", "Glob": "検索", "Grep": "検索", "Edit": "書く",
              "Write": "書く", "NotebookEdit": "書く", "WebSearch": "Web", "WebFetch": "Web",
              "Construct": "記憶", "Skill": "手順書", "Desktop": "画面", "TodoWrite": "計画",
              "AskUserQuestion": "質問"}

# Markdown の既定スタイル（cyan/magenta/blue）を黒と赤の画面に合わせる。赤は使わない＝
# 本文の飾りはアクセントではない（赤は波・自分の名前・失敗にだけ）。
_MD_THEME = Theme({
    "markdown.code": f"bold {T.TEXT}",
    "markdown.code_block": T.TEXT_2,
    "markdown.block_quote": T.TEXT_2,
    "markdown.list": T.TEXT,
    "markdown.item.bullet": f"bold {T.TEXT_3}",
    "markdown.item.number": T.TEXT_3,
    "markdown.h1": f"bold {T.TEXT}",
    "markdown.h2": f"bold {T.TEXT}",
    "markdown.h3": f"bold {T.TEXT}",
    "markdown.h4": f"bold italic {T.TEXT}",
    "markdown.link": f"underline {T.TEXT_2}",
    "markdown.link_url": f"underline {T.TEXT_3}",
    "markdown.table.border": T.TEXT_3,
    "markdown.table.header": f"bold {T.TEXT}",
    "markdown.kbd": f"bold {T.TEXT}",
    "markdown.hr": T.TEXT_3,
})


def read_kind(name: str, inp: dict | None) -> str | None:
    """読み取り系なら "read"/"search"/"list"、そうでなければ None。"""
    if name in _READ_TOOLS:
        return _READ_TOOLS[name]
    if name == "Bash":
        cmd = str((inp or {}).get("command") or "").strip()
        first = cmd.split(None, 1)[0] if cmd else ""
        return _BASH_WORDS.get(first)
    return None


def num(v: Any) -> str:
    """4 桁以上の整数に桁区切り（SPEC 項目 23②）。整数でない値はそのまま文字にする。"""
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v) if v is not None else "?"


def fmt_secs(sec: float) -> str:
    """走行中の経過（SPEC 項目 14）。60 秒未満は `12s`、以上は `8m12s`（桁が動かない）。"""
    sec = int(max(0.0, sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m{sec % 60:02d}s"


def fmt_secs2(sec: float) -> str:
    """ステータス行の経過（`3m 53s`・分と秒に空白）。オーナー引用の書式に寄せた版。

    ★`fmt_secs`（空白なし・`8m12s`）は締めの統計行が桁を縦に比べるのに使うので**触らない**。
      混ぜると done 行の桁揃えが崩れる（REPORTS/uiux-redesign-20260906 §B-6）。ここは
      ステータス行専用。秒は 2 桁固定なので monospace では幅が動かない。
    """
    sec = int(max(0.0, sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m {sec % 60:02d}s"


# -- 推論中ステータス行（REPORTS/uiux-redesign-20260906 §B。Claude Code の書式に倣う）------
# 脈動印の相（§A-2・実測: フレーム 6 種・120ms・reverseMirror＝往復）。
# ★色は増やさない（NOTE 単色）。字形が相で膨らむので単色でも脈打って見える。theme の
#   「赤を出してよいのは 4 か所」を守り、赤は波（wave.py）だけが担う。
# ★Claude Code の原フレームは `·` 谷・`✽` 山だが、この 2 つは端末版では使えない:
#   - `·`(U+00B7) は theme／check_tui_width.py で**追放した字**（East Asian Ambiguous＝
#     幅が端末依存）。谷は同形で幅の安定した `∙`(U+2219・eaw=N) に置き換える。
#   - `✽`(U+273D) も eaw=A で、幅 2 に振れる端末では脈動のたびに印が 1 桁伸び、
#     **行全体が横に揺れる**（§B-6「幅を動かさない」に反する）。山は同系で eaw=N の
#     `✺`(U+273A) に置き換える。**全フレームを eaw=N** で揃え、どの端末でも印は 1 桁。
_PULSE = ("∙", "✢", "✳", "✶", "✻", "✺")
_PULSE_MIRROR = _PULSE + _PULSE[-2:0:-1]     # ∙→…→✺→…→∙ で膨らんで縮む（端点は 1 度ずつ）
_PULSE_TICK = 0.12                            # 秒／相（Claude Code の updateInterval）
_PULSE_STILL = "✳"                            # reduced-motion / NO_COLOR / 非 TTY の静止印

# 純推論（道具も段階も無い＝本当に中身が不透明な区間）だけで回す気まぐれ語（§A-3/§B-5）。
# ★あくまで**装飾**。道具走行中・段階既知のときは substance（道具名）を主役にする——
#   「実際に起きたことを出す。Thinking と出して実は止まっている、が一番たちが悪い」
#   （下の end_turn まわりの明文禁止・§715）と両立させるための線引き。
_GERUND = (
    "Nebulizing", "Ruminating", "Percolating", "Untangling", "Marshalling",
    "Divining", "Forging", "Reticulating", "Coalescing", "Triangulating",
    "Distilling", "Extrapolating", "Cogitating", "Synthesizing", "Contemplating",
    "Conjuring", "Deliberating", "Puzzling", "Unraveling", "Fathoming",
)
_GERUND_TICK = 7.0                            # 秒／語（毎秒替えると散らかる・§B-3）

# 道具 → 現在進行の動詞（§B-2）。ここに無い道具は「実行」。read_kind と同じ発想で 1 か所に。
_TOOL_ING = {
    "Read": "読込", "Grep": "検索", "Glob": "検索", "Write": "書込",
    "Edit": "編集", "ApplyDiff": "編集", "ApplyPatch": "編集", "MultiEdit": "編集", "NotebookEdit": "編集",
    "Bash": "実行", "Task": "委譲", "RemoteRead": "取得", "WebFetch": "取得",
    "WebSearch": "検索", "TodoWrite": "TODO", "AskUserQuestion": "確認",
}


def _pulse_mark(now: float, animate: bool) -> str:
    """脈動印の 1 相。相は**壁時計**から出す（Live の刷新率に依存させない・波と同じ設計）。"""
    if not animate:
        return _PULSE_STILL
    return _PULSE_MIRROR[int(now / _PULSE_TICK) % len(_PULSE_MIRROR)]


def _gerund(elapsed: float) -> str:
    """純推論の気まぐれ語。ゆっくり（`_GERUND_TICK` 秒ごと）回す。"""
    return _GERUND[int(max(0.0, elapsed) / _GERUND_TICK) % len(_GERUND)]


def _fmt_tok_est(chars: int) -> str:
    """走行中の出力トークンの**推定**（chars/4）を `~3.1k` で。`~` で推定であることを明示する
    （done で実測 total_tokens に置き換わり `~` が外れる・§B-4）。"""
    k = max(0, chars) / 4 / 1000.0
    return f"~{k:.1f}k"


def _status_target(name: str, inp: dict | None) -> str:
    """ステータス行の主ラベルに出す短い対象。ファイル系は basename、Bash は先頭語。"""
    inp = inp or {}
    if name == "Bash":
        cmd = str(inp.get("command") or "").strip()
        return cmd.split(None, 1)[0] if cmd else ""
    for key in ("path", "file_path", "file", "notebook_path", "pattern", "query", "url"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            import os
            s = v.strip()
            base = os.path.basename(s.rstrip("/")) or s
            if cell_width(base) > 28:        # 主ラベルで幅を食い過ぎない（表示幅で丸める）
                from rich.cells import set_cell_size
                base = set_cell_size(base, 27).rstrip() + "…"
            return base
    return ""


def _tool_status_label(e: "ToolEntry") -> str:
    """`api.py を読込` / `sleep を実行中` のような現在進行の 1 句（§B-2）。"""
    verb = _TOOL_ING.get(e.name, "実行")
    subj = _status_target(e.name, e.input) or e.name
    return f"{subj} を{verb}中"


def fold_physical(lines: list[str], width: int, prefix: str,
                  head_rows: int, tail_rows: int) -> tuple[list[str], int, list[str]]:
    """先頭 head_rows・末尾 tail_rows **物理行**ぶんを残し、間を隠す（SPEC 項目 11）。

    戻り: (先頭の行, 隠した行数, 末尾の行)。隠すのが 1 行以下なら畳まない
    （「… 残り 1 行」は隠した行そのものより長い）。
    ★論理行ではなく物理行で数える: 幅 80 の端末で journalctl の 1 行は 2 物理行になり、
      論理 3 行の約束が 6 行の実害になっていた（frames/09 実測）。
    """
    lines = [str(x) for x in lines]
    n = len(lines)
    head: list[str] = []
    used, i = 0, 0
    while i < n and used < head_rows:
        head.append(lines[i])
        used += wrap_with_gutter(lines[i], width, prefix, prefix).count("\n") + 1
        i += 1
    tail: list[str] = []
    used, j = 0, n - 1
    while j >= i and used < tail_rows:
        tail.insert(0, lines[j])
        used += wrap_with_gutter(lines[j], width, prefix, prefix).count("\n") + 1
        j -= 1
    hidden = j - i + 1
    if hidden <= 1:
        return lines, 0, []
    return head, hidden, tail


def _first_str_arg(inp: dict | None) -> str:
    """summary が無いときの head: 最初の文字列引数を 1 行 60 字に。"""
    for v in (inp or {}).values():
        if isinstance(v, str) and v.strip():
            s = " ".join(v.split())
            # ★表示幅で切る（全角は 2 列。文字数で切ると日本語で行が約 2 倍に伸びた・レビュー）
            from rich.cells import cell_len, set_cell_size
            if cell_len(s) <= HEAD_ARG_MAX:
                return s
            return set_cell_size(s, HEAD_ARG_MAX - 1).rstrip() + "…"
    return ""


def _key(call: Any, owner: Any) -> Any:
    """_running の鍵。call が None／ハッシュ不能なら owner の id（固有）。"""
    if call is None:
        return ("_", id(owner))
    try:
        hash(call)
        return call
    except TypeError:
        return ("_", id(owner))


def _first_line(s: str) -> str:
    for ln in str(s or "").splitlines():
        if ln.strip():
            return ln.rstrip()
    return ""


_PLAN_STATUSES = ("pending", "in_progress", "completed", "cancelled")


def plan_items(result: str, inp: dict | None) -> list[tuple[str, str]]:
    """TodoWrite の段（内容, 状態）。本体が整えた結果（行頭の「[」から始まる JSON・作業中は 1 つ）を優先し、読めなければ引数。
    ★結果の頭には「[Host] …」のような注記が付くことがある（最初の道具の結果）＝行頭の「[」だけの行を探す。"""
    def norm(arr: Any) -> list[tuple[str, str]]:
        out = []
        for t in arr if isinstance(arr, list) else []:
            if isinstance(t, dict) and str(t.get("content") or t.get("task") or "").strip():
                st = str(t.get("status") or "pending")
                out.append((str(t.get("content") or t.get("task")), st if st in _PLAN_STATUSES else "pending"))
        return out
    m = re.search(r"(?m)^\[\s*$", result or "")
    if m:
        try:
            got = norm(json.JSONDecoder().raw_decode(result[m.start():])[0])
            if got:
                return got
        except ValueError:
            pass
    todos = (inp or {}).get("todos")
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except ValueError:
            todos = []
    return norm(todos)


def _norm(text: str) -> str:
    """同じ文面かを見るための正規化。折り返しと空白の入れ方だけが違うものを同じと見る。"""
    return "".join(str(text).split())


def _md(text: str) -> Markdown:
    return _Markdown(text, code_theme=CODE_THEME)


def _prose_limit(width: int, path: Any = None) -> int:
    """本文の**読み幅**（表示セル）。0 なら端末幅のまま。`path` は設定ファイルの場所。

    ★なぜ要るか（2026-09-01・実測）: この TUI の設計仕様は幅 **20/40/80/100** でしか
      検証していない。だが実機の端末は **200 桁**で、本文は最長 **282 セル**・平均 103 セルの
      板になって流れていた。折り返し・段・畳み・銘の桁合わせ——24 件の設計はどれも
      「狭いときに崩れない」ための仕事なので、**効く場所に文字が来ていなかった**。
      利用者から見て「何も変わっていない」ように見えた理由がこれ。

    ★効かせるのは**本文だけ**。道具の出力・コード・差分・表は幅が要るので触らない
      （そのぶん本文と道具が幅で見分けられる＝色や記号を増やさずに階層が出る）。
    ★端末幅より広くはしない。**狭めるだけ**なので幅の門（超過を数える）とは衝突せず、
      96 桁以下の端末では 1 バイトも変わらない。
    ★毎回 `config.resolve()` を呼ぶ（控えを持たない）。Live は毎フレームここを通るので
      気になるが、**実測 0.12 ms/回**（設定ファイルが実在する状態・2,000 回で 242 ms）。
      20fps でも 2.4 ms/秒なので、控えを持って「設定を変えたのに効かない」を作るより安い。
    ★`path` を渡す（2026-09-02）。旧実装は `config.resolve()` を**引数なし**で呼んでおり、
      `--config /別の場所` で起動した人の `prose_width` を**一度も読んでいなかった**
      （読んでいたのは既定の `~/.config/blackwall/config.toml`）。設定したのに効かない、
      しかも画面には何も出ない——この現場でいちばん多い形。`app.py` の履歴ファイルが
      同じ理由で `--config` に追随させてあるのに、ここだけ取り残されていた。
    """
    try:
        from . import config
        want = int(config.resolve(path=path).get("prose_width") or 0)
    except Exception:
        want = 96          # 設定が読めなくても、本文が板になるよりはよい
    return min(width, want) if want > 0 else width


def _md_block(console: Console, text: str, path: Any = None) -> Segments:
    """Markdown を**行末を詰めて**描く。

    ★Rich の Markdown は段落を端末幅いっぱいに空白で埋める（justify）。画面では見えないが
      export にも実バイトにも末尾空白として残り、SPEC §6-4 の「末尾空白を吐かない」を
      **本文だけが破っていた**（門が実測: 幅 100 の 1 行に 84 桁ぶんの空白）。
      遅い回線ではその空白がそのまま帯域になる。`render_lines(pad=False)` は同じ絵を
      埋めずに作る（`_LiveView` は最初からこの形で描いていた＝確定印字だけが揃っていなかった）。
    """
    opts = console.options.update(height=None, width=_prose_limit(console.width, path))
    segs: list[Segment] = []
    for line in console.render_lines(_md(text), opts, pad=False):
        # ★詰めるのは**行の末尾だけ**。行の途中の空白（表の桁揃え・コードの字下げ）は触らない。
        #   Markdown の段落は Text(justify="left") で幅いっぱいに空白を吐くので、
        #   pad=False では取れない（padding は render_lines ではなく Text の側にある）。
        while line and not line[-1].control and not line[-1].text.strip():
            line.pop()
        if line and not line[-1].control:
            line[-1] = line[-1]._replace(text=line[-1].text.rstrip())
        segs.extend(line)
        segs.append(Segment.line())
    return Segments(segs)


def _int(v: Any, default: int = 0) -> int:
    """本体は整数を送るが、壊れた値 1 つで描画ごと落ちないように（画面は止まらないのが先）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass
class ToolEntry:
    """道具 1 回ぶん。tool_call で生まれ tool_result で埋まる。/expand はこれを全文で印字する。"""
    call: Any
    name: str
    input: dict
    t0: float = field(default_factory=time.time)
    done: bool = False
    ok: bool | None = None
    kind: str = ""
    result: str = ""
    chars: int | None = None
    truncated: bool = False
    summary: dict | None = None
    secs: float | None = None
    read: str | None = None          # read/search/list/None
    head_printed: bool = False       # 見出しを既に流したか（TERM=dumb で 2 度出さない・項目 15④）

    @property
    def head(self) -> str:
        sm = self.summary or {}
        return str(sm.get("head") or "") or _first_str_arg(self.input)

    @property
    def tail(self) -> str:
        sm = self.summary or {}
        t = str(sm.get("tail") or "")
        if t:
            return t
        if self.done:
            return _first_line(self.result) or ("OK" if self.ok else "Error")
        return ""

    @property
    def bad(self) -> bool:
        st = str((self.summary or {}).get("status") or "")
        return self.ok is False or st in _BAD_STATUS

    @property
    def patches(self) -> list[dict]:
        sm = self.summary or {}
        pl = sm.get("patches") if isinstance(sm.get("patches"), list) else (
            [sm["patch"]] if isinstance(sm.get("patch"), dict) else [])
        return [p for p in pl if isinstance(p, dict) and p.get("hunks")]


def patch_lines(patch: dict, width: int = 0) -> list[Text]:
    """差分（app.js diffNode の移植）。ガターは旧ファイル基準の行番号、追加行は空。hunk 間は "…"。

    width > 0 なら、長い行を端末幅で折り返し、**続き行にも段と印（+/-）を付ける**
    （SPEC 項目 4）。旧実装は続き行が桁 0 に落ち、`+` が消えて「変えていない行」に見えた。
    """
    out: list[Text] = []
    hunks = [h for h in (patch.get("hunks") or []) if isinstance(h, dict)]
    # 行番号の桁は差分全体で揃える（hunk ごとに揃えると段がずれる）
    width_no = 1
    for h in hunks:
        lines = h.get("lines") if isinstance(h.get("lines"), list) else []
        n = _int(h.get("old_start")) + sum(1 for ln in lines if not str(ln).startswith("+"))
        width_no = max(width_no, len(str(n)))
    for i, h in enumerate(hunks):
        if i > 0:
            out.append(Text(INDENT + "…", style=T.S_DIFF_CTX))
        old_no = _int(h.get("old_start"))
        lines = h.get("lines") if isinstance(h.get("lines"), list) else []
        for raw in lines:
            ln = str(raw if raw is not None else "")
            c = ln[:1]
            if c == "+":
                no, mark, style = "", "+", T.S_DIFF_ADD
            elif c == "-":
                no, mark, style = str(old_no), "-", T.S_DIFF_DEL
                old_no += 1
            else:
                no, mark, style = str(old_no), " ", T.S_DIFF_CTX
                old_no += 1
            first = f"{INDENT}{no:>{width_no}} {mark} "
            body = ln[1:]
            if width > 0:
                cont = f"{INDENT}{'':>{width_no}} {mark} "
                out.append(Text(wrap_with_gutter(body, width, first, cont), style=style))
            else:
                out.append(Text(first + body, style=style))
    if patch.get("truncated"):
        msg = "… 差分が長いので以降は省略（/expand でも出ません。ファイルを開いて確認してください）"
        out.append(Text(wrap_with_gutter(msg, width, INDENT, INDENT) if width > 0 else INDENT + msg,
                        style=T.S_DIFF_CTX))
    return out


class _LiveView:
    """Live の中身: 本文（Markdown の**末尾**・画面に入る分だけ）＋実行中の道具＋波。

    Rich の Live は画面より高い物を上で切る（ellipsis）ので、長い本文は先頭しか見えなくなる。
    流れてくる本文は末尾を見たいので、行に描いてから後ろを取る。描画は本文が変わった時だけ
    （波の 20Hz 更新のたびに Markdown を組み直さない）。
    """

    def __init__(self, r: "Renderer") -> None:
        self._r = r
        self._cache_key: tuple[str, int] | None = None
        self._cache: list[list[Segment]] = []

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        r = self._r
        with r._lock:
            text = r._body_text
            running = [e for e in r._running.values()]
            wave = r._wave
            waiting = r._waiting
            quiet = r._quiet_secs
            t0 = r._t0
            open_ = r._open
            out_chars = len(r._turn_text) + len(r._think_buf)
        height = options.height or options.max_height or console.size.height
        # ★実行中の頭行は折り返して 2 物理行になることがある（幅 60 実測）。行数で数えると
        #   その分だけ本文が画面外へ押し出される。物理行で数える（SPEC 項目 3②）。
        heads = [r._head_line(e, running=True) for e in running]
        # ★予定のいまの段（2026-10-21）。走行中だけ、道具の行の上に 1 行。
        _pn = r.plan_now(int(options.max_width or console.width)) if open_ else None
        if _pn is not None:
            heads.insert(0, _pn)
        # ★推論中は常時ステータス行を 1 本（REPORTS/uiux-redesign-20260906 §B）。
        #   旧実装は心拍（15 秒沈黙）で立つ `waiting` のときだけ 1 行出しており、ターン開始
        #   から沈黙 15 秒までは「何も起きていないように見える」——これがオーナーの不満
        #   （推論中に何が起きているか分かりづらい）の核心だった。ここで置き換える。
        #   ★NO_COLOR / TERM=dumb では常時行を出さず、旧来どおり心拍の待ち 1 行だけに
        #   退避する（`_status_enabled`）。それ以外（色のある TTY・`--calm` 含む）は常時出す。
        if r._status_enabled() and (open_ or running):
            now = time.time()
            live_turn = bool(open_ and t0)
            elapsed = (now - t0) if live_turn else 0.0
            heads.append(r._status_row(
                running=running, waiting=waiting, quiet=quiet, elapsed=elapsed,
                out_chars=out_chars, width=int(options.max_width or console.width),
                animate=r._status_animate(), now=now, live_turn=live_turn))
        elif waiting:
            # NO_COLOR/dumb、またはターン外での心拍（test_term_heartbeat が固定する旧来の 1 行）。
            heads.append(Text(waiting, style=T.S_DIM, no_wrap=True, overflow="ellipsis"))
        reserve = 1 + sum(t.plain.count("\n") + 1 for t in heads)
        if wave is not None:
            # ★`rows`（作り置きの高さ）ではなく `live_rows`（いま実際に占める行数）を引く。
            #   波が off のときと遅延ゲートの最中は 1 行も描かないので、そこで 2 行引くと
            #   本文が理由もなく 2 行短くなる（wave.py の申し送り・SPEC 項目 21③）。
            #   `live_rows` を持たない相手（試験の偽物）には `rows`、それも無ければ 2。
            reserve += _int(getattr(wave, "live_rows", getattr(wave, "rows", 2)), 2)
        limit = max(3, height - reserve)
        if text.strip():
            # ★控えの鍵は**実際に描いた幅**で持つ（端末幅ではない・2026-09-01）。
            #   端末幅で覚えると、読み幅の設定を変えても古い絵が出る
            #   ＝この現場でいちばん多い「設定したのに効かない」。
            pw = _prose_limit(options.max_width, r.config_path)
            key = (text, pw)
            if key != self._cache_key:
                # height を外す: render_lines は options.height で**先頭**を切るが、欲しいのは末尾
                self._cache = console.render_lines(
                    _md(text), options.reset_height().update(width=pw), pad=False)
                self._cache_key = key
            for line in self._cache[-limit:]:
                yield from line
                yield Segment.line()
        for t in heads:
            yield t
        if wave is not None:
            yield wave


def fit_parts(parts: "list[tuple[str, str, int]]", width: int) -> Text:
    """`(本文, スタイル, 落としてよい順)` の列から、**幅に収まる** 1 行を作る。

    ★なぜ要るか（2026-09-02・実機の記録で実測）: 組み立てた `Text` が端末幅を超えると
      Rich が折り返し、`Text.wrap` は「幅の**外**にはみ出た空白」しか落とさないので、
      折り目に置いた区切りの空白 2 桁が**行末に残る**。SPEC §6-4 の「末尾空白を吐かない」を
      破るうえ、桁が崩れて締めの行を縦に比べられなくなる（項目 8 が桁を固定した目的が消える）。
      直近 14 日の 148 走行を幅 40 で描き直すと **64 行**が末尾空白つきで出た
      （62 行が `done` の締め・2 行が読み取りの圧縮行）。幅 60・80 では 0 行＝
      **狭い端末でだけ起きる**。
    ★直し方は `app.header_line` と同じ「後ろから削って収める」。rank が大きいものから落とす。
      rank 0 は落とさない（意味が消える欄＝印・失敗の件数・outcome の語）。
      各部品は**自分の前の区切りを含めて**持つので、落とすと区切りも一緒に消える。
    ★幅は `cell_width`（曖昧幅を 2 と数える安全側・裁定 E）。Rich は 1 と数えるので、
      こちらが先に落とす方向にしか外れない。
    """
    keep = [True] * len(parts)

    def built() -> str:
        return "".join(s for i, (s, _, _) in enumerate(parts) if keep[i])

    for rank in [None] + sorted({r for _, _, r in parts if r > 0}, reverse=True):
        if rank is not None:
            for i, (_, _, r) in enumerate(parts):
                if r == rank:
                    keep[i] = False
        if cell_width(built()) <= width:
            break
    # ★落とし切ってもまだ収まらないほど狭いときは、**自分で**段付きに折る（項目 2 と同じ規範）。
    #   Rich に任せると折り目の区切り 2 桁が行末に残る（末尾空白）うえ、続き行が桁 0 に落ちる。
    #   各部品は先頭に区切り 2 桁を持っているので、行頭に来ればそれがそのまま段になる。
    #   ※1 つの部品だけで幅を超える場合はどうにもならない（そのまま出して Rich に任せる）。
    t = Text()
    used = 0
    for i, (text, style, _) in enumerate(parts):
        if not keep[i]:
            continue
        w = cell_width(text)
        if used and used + w > width:
            t.append("\n")
            used = 0
        t.append(text, style=style)
        used += w
    return t


def cell_width(text: str) -> int:
    """表示幅（桁数）。**East Asian Ambiguous(A) も 2 と数える**（SPEC 項目 1・裁定 E）。

    ★安全側に倒す理由: A を 1 と数えて外すと端末が自前で折り返し、続き行が桁 0 に落ちて
      **段が消える**（`probe_ambiguous.py` 実測: 80 桁と判定した行が実際は 85 桁）。
      逆に 2 と数えて外しても 1 桁早く折れるだけで、読みにくくはならない。
    """
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1 for ch in text)


def wrap_with_gutter(text: str, width: int, first: str, cont: str) -> str:
    """端末幅で折り返し、**続き行にも段（ガター）を付ける**（2026-08-28・UI 監査）。

    ★なぜ要るか: Rich にそのまま渡すと、折り返された 2 行目以降が**桁 0 から**始まり、
      `⎿` の段が消える。実測（80 桁）では、長い失敗本文の続き行が他の出力と
      見分けられない状態で 4 行続いた——**いちばん読みたい失敗が、いちばん読みにくい**。
    ★幅の数え方（SPEC 項目 1・実測で直した 3 点）:
      ①接頭辞も**表示幅**で引く（`len()` だと `⎿` や日本語の接頭で 1 桁ずれる）
      ②安全余白を 1 桁残す（端末の右端で折り返しが二重に起きるのを避ける）
      ③`max(8, …)` を置かない（幅 8〜12 の端末に 13 桁の行を吐いていた）
    """
    out: list[str] = []
    for i, para in enumerate(text.splitlines() or [""]):
        prefix = first if i == 0 else cont
        # ★接頭辞が変われば席の数も変わる。1 回目の接頭辞で計算したまま使うと、
        #   続き行が接頭辞のぶんだけ溢れる（幅 8 で 8 桁・幅 80 で 80 桁を実測）。
        room = max(1, width - cell_width(prefix) - 1)
        cur, used = "", 0
        for ch in para:
            cw = cell_width(ch)
            if cur and used + cw > room:
                # ★半角の語の途中では折らない（2026-10-13・Gemma 4 の英語の思考が「explanati / on」と切れた）。
                #   折れ目が半角の語の中なら、行の後ろ半分にある最後の空白で折って語ごと次の行へ送る。
                #   日本語（全角）と、空白の無い長いパス・URL は従来どおり字で折る。
                cut = cur.rfind(" ")
                if (ch != " " and cw == 1 and cut > 0 and cut >= len(cur) // 2
                        and all(cell_width(c) == 1 for c in cur[cut + 1:])):
                    out.append((prefix + cur[:cut]).rstrip())
                    cur = cur[cut + 1:]
                else:
                    # ★行頭に「。」「、」「）」を置かない・行末に「（」を残さない（追い出し・2026-10-21 TUI の思考で
                    #   「…報告すること / 。手順が…」と折れていた）。前の 1 字ごと次の行へ送る。
                    #   送るのは空白でない字で、次の行に送った字と今の字が入るときだけ（幅 8 の端末で溢れた）。
                    carry = cur[-1] if (len(cur) > 1 and cur[-1] != " " and (ch in _CJK_CLOSE or cur[-1] in _CJK_OPEN)
                                        and cell_width(cur[-1]) + cw <= max(1, width - cell_width(cont) - 1)) else ""
                    out.append((prefix + cur[:len(cur) - len(carry)]).rstrip())
                    cur = carry
                prefix = cont
                room = max(1, width - cell_width(prefix) - 1)
                used = cell_width(cur)
                if not cur and ch == " ":
                    continue        # 折れ目の空白は捨てる（続き行が 1 桁ずれて見える）
            cur += ch
            used += cw
        out.append((prefix + cur).rstrip())
    return "\n".join(out)


def _show_think() -> bool:
    """思考の全文を流すか。★2026-10-30 オーナー「内部思考を出さないようにしてモデルに対して、タスク状況を報告しながら進める」:
    既定は出さず（秒だけ）、モデルが道具の欄に書いた一文（progress）を出す。LAPS_SHOW_THINK=1 で全文・0 で出さない。
    LAPS_PROGRESS=0（一文の欄を切った）のときは前どおり全文を出す（推論しているのか分からなくなるため・2026-09-21）。"""
    v = (os.environ.get("LAPS_SHOW_THINK") or "").strip()
    if v in ("0", "1"):
        return v == "1"
    return (os.environ.get("LAPS_PROGRESS") or "1").strip().lower() in ("0", "off", "false", "no")


_THINK_LEAD = re.compile(r"^(\s*)((?:[*\-•+]|\d+[.)])\s+)?")


def wrap_think(text: str, width: int) -> str:
    """思考の全文を "∴ " の段で折る。箇条書き（`*   …`・`1. …`）の続き行は箇条の本文の位置に揃える
    （2026-10-13・Gemma 4 の思考は字下げした箇条書きで来て、続き行だけ左へずれていた）。"""
    out: list[str] = []
    for i, ln in enumerate(text.splitlines() or [""]):
        head = "∴ " if i == 0 else "  "
        m = _THINK_LEAD.match(ln)
        lead = m.group(0) if m else ""
        cont = "  " + " " * cell_width(lead)
        if cell_width(cont) > width // 2:          # 深すぎる字下げで続き行が 1 字ずつにならないように
            cont = "  "
        out.append(wrap_with_gutter(ln[len(lead):], width, head + lead, cont))
    return "\n".join(out)


class Renderer:
    """serve のイベントを描く。1 ターン = start_turn … end_turn。"""

    def __init__(self, console: Console, *, verbose: bool = False,
                 config_path: Any = None) -> None:
        self.console = console
        self.verbose = verbose
        # ★どの設定ファイルを見るか（`--config`）。None なら既定の場所。読み幅（prose_width）が
        #   ここを見る。渡し忘れると `--config` の人だけ設定が効かない（上の _prose_limit）。
        self.config_path = config_path
        self.transcript: list[dict] = []
        console.push_theme(_MD_THEME)
        # 非 TTY（パイプ）では Live を使わない＝本文は end_turn でまとめて出る。
        self._use_live = bool(console.is_terminal) and not console.is_dumb_terminal
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._open = False          # start_turn 済みで end_turn がまだ
        self._recording = False
        self._wave: Any = None
        #: 心拍（serve `notice kind=heartbeat`）で立つ「何を何秒待っているか」の 1 行。
        #: 本物の事象が 1 つでも来たら消える。**本文にも履歴にも刷らない**（0.1.164 では
        #: message 無しの notice が `kind` の文字で刷られ、沈黙 15 秒ごとに dim の
        #: "heartbeat" が並んだ＝直す）。
        self._waiting: str = ""
        #: 心拍が伝えた沈黙秒（0 = 沈黙していない）。ステータス行が道具走行中に沈黙注記
        #: `（N 秒 応答なし）` を添えるのに使う。`_waiting`（心拍の一文）とは別に持つので、
        #: test_term_heartbeat の `_waiting` 文言をそのまま保てる。本物の事象が来たら 0 に戻る。
        self._quiet_secs: int = 0
        self._rid: str | None = None
        self._t0 = 0.0
        self._body_text = ""        # まだ確定印字していない本文
        self._turn_text = ""        # このターンに token で受けた本文の全部
        self._running: dict[Any, ToolEntry] = {}
        self._group: list[ToolEntry] = []      # 圧縮待ちの読み取り系
        self._bundle: list[ToolEntry] = []     # このターンの道具列（/expand 用）
        self._forgot = False                   # 前ターンの束を捨てたか（/expand の空状態）
        self._think_chars = 0
        self._think_buf = ""
        self._think_secs = 0.0
        self._think_t0: float | None = None
        self._done: dict | None = None
        self._errors: list[str] = []   # このターンに error で出した文面（締めで二度出さない）
        #: 中断（cancel）まわりの notice は 1 回の Ctrl-C で最大 3 回届く（①クライアント
        #: 自身の即時表示 ②serve の受理 ③agent が反復境界で確定）。3 つとも同じ定型文
        #: なので、そのまま描くと同じ行が並ぶ（実機・2026-09-03）。このターンで最初の
        #: 1 回だけ本文を出し、以降は `applied`（走行中／待ち行列／失敗）が新しく分かった
        #: ときだけ続報を 1 行足す。start_turn でターンごとに引き継がない。
        self._cancel_shown = False
        self._cancel_applied_seen: set[str] = set()
        #: 予定表（TodoWrite）のいまの段の組（内容, 状態）。会話をまたいで持つ（予定は会話の物）・/clear で捨てる。
        self._plan: list[tuple[str, str]] = []

    # -- 公開 ------------------------------------------------------------

    def user(self, text: str) -> None:
        # ★ターンの境目に空行を 1 行（項目 9）。旧実装は 6 ターン 63 行が 1 枚の塊で、
        #   どこからどこまでが 1 往復か読めなかった（frames/09 は空行 0 行）。
        #   起動直後だけは入れない。#8 で消えた done 行と相殺して 1 ターンあたり ±0 行。
        if self.transcript:
            self.console.print()
            if card_on():
                # カード表示: 往復の境目に暗い赤の細い線（飾り・読ませない）。
                self.console.print(Text("─" * max(1, self._width() - 1), style=T.S_WALL_RULE))
        self.transcript.append({"role": "user", "content": text})
        if privacy_on():
            text = mask_private(str(text))
        lines = str(text).splitlines() or [""]
        if card_on():
            # カード表示: 依頼は ❯ と太字の本文（いちばん目立つ行＝何を頼んだか）。
            # ★折り返しは字で（日本語は空白が無い＝Rich に任せると 1 語目の後で丸ごと次の行へ送られ、
            #   続き行が桁 0 から始まった・幅 72 の実測）。続き行は ❯ の幅だけ下げる。
            t = Text(wrap_with_gutter("\n".join(lines), self._width(), "❯ ", "  "),
                     style=f"bold {T.TEXT}")
            t.stylize(f"bold {T.ALERT}", 0, 1)
            self.console.print(t)
            return
        # 利用者の発言は統計より暗くしない（旧: S_DIM 5.50:1 ＜ done の S_META 7.07:1）
        t = Text("> " + lines[0], style=T.S_META)
        for ln in lines[1:]:
            t.append("\n  " + ln)
        self.console.print(t)

    def forget_tools(self) -> None:
        """直前の束（/expand の材料）を捨てる。`/clear` から呼ぶ。

        ★捨てないと `/clear` の直後の `/expand` が**前の会話**のコマンドと出力を全文で
          出す（実測）。捨てたことは `_forgot` に残すので、`/expand` は「まだ使われて
          いません」ではなく「もう残っていません」と正しく言える（SPEC 項目 22①②）。
        """
        self._forgot = bool(self._bundle) or self._forgot
        self._bundle = []
        self._plan = []

    def close(self) -> None:
        """どんな抜け方でも Live を畳む（例外の後始末専用・2026-09-02）。

        ★なぜ要るか（実測）: 描画の途中で例外が出ると `app.run_turn` はそのまま抜け、
          **Live が走ったまま**プロセスが終わる。Rich の `Live` はカーソルを
          `start()` で隠し `stop()` でしか戻さず、atexit も持たない——利用者の手元に残るのは
          「カーソルが見えないシェル」で、`reset` を打つまで直らない。
          実際に再現した: 事象 1 つで例外 → `\x1b[?25l` は 1 回出て `\x1b[?25h` は 0 回。
        ★締めの行は出さない。走り切っていないのに `done` を出すのは嘘（SPEC 項目 18①）。
        ★閉じ終わったターンに対しては何もしない（`_stop_live` が空振りする）。
        """
        try:
            self._stop_live()
        except Exception:
            # ★ここで投げると呼び手の `finally` の中なので**元の例外を隠す**。
            #   最低限、カーソルだけは戻す（それがこの後始末の目的）。
            with contextlib.suppress(Exception):
                self.console.show_cursor(True)
        finally:
            self._live = None
            self._open = False
            self._running = {}

    def set_verbose(self, on: bool) -> None:
        self.verbose = bool(on)

    def start_turn(self, rid: str) -> None:
        # 前のターンが閉じていない（cancel 等）。★Live の有無で判定しない＝非 TTY では Live が
        #   無いので、そう書くと未印字の本文が黙って消える（検証で実測）。
        if self._open:
            self.end_turn()
        self._open = True
        self._rid = rid
        self._t0 = time.time()
        self._body_text = ""
        self._turn_text = ""
        self._running = {}
        self._group = []
        self._forgot = bool(self._bundle)   # 前ターンの束を捨てた＝/expand は「もう無い」
        self._bundle = []
        self._think_chars = 0
        self._think_buf = ""
        self._think_secs = 0.0
        self._think_t0 = None
        self._done = None
        self._errors = []
        self._cancel_shown = False
        self._cancel_applied_seen = set()
        self._quiet_secs = 0
        self._said = False
        self._start_live()

    LIVE_FPS = 20.0          # 波が無いとき（＝波の刷新回数に合わせるものが無いとき）の既定

    def _refresh_rate(self) -> float:
        """Live を 1 秒に何回書き出すか。**波が持っている上限に合わせる**。

        ★`--calm` は実測で刷新回数を 1 回も減らしていなかった（`motion/refresh-*.log`
          が波 19.1〜20.0 回/s・calm 19.0 回/s）。原因は波が `period` を持っているのに、
          ここが 20 の固定値だったこと＝**書いたが繋いでいない**の典型。
          帯域は 4 KiB/s の回線で done が 16.3 秒遅れるほど効く（SPEC 項目 21①）。
        """
        if record_on():
            return RECORD_FPS
        rate = _float(getattr(self._wave, "refresh_per_second", None))
        if rate and rate > 0:
            return min(rate, self.LIVE_FPS)
        return self.LIVE_FPS

    def _start_live(self) -> None:
        if not self._use_live or self._live is not None:
            return
        # ★record=True の Console（試験）では Live の毎フレームが記録に残り、export_text に
        #   本文が何十回も出る。Live の間だけ記録を止め、上に流す行は Live を止めて記録付きで
        #   印字する（_print）。実端末（record=False）ではこの分岐は何もしない。
        self._recording = bool(self.console.record)
        if self._recording:
            self.console.record = False
        self._live = Live(_LiveView(self), console=self.console,
                          refresh_per_second=self._refresh_rate(),
                          transient=True, redirect_stdout=False, redirect_stderr=False)
        self._live.start()

    def pause(self) -> bool:
        """ターンを**閉じずに** Live だけ止める（ask で利用者に聞く間・2026-08-23）。

        ★end_turn で閉じると、走行中の AskUserQuestion が「no result returned」と赤い
          `● done … no done` を**質問の前に**出して失敗に見えた（実機）。ここまでの本文と
          溜めた読み取り系は先に確定印字する（transient の Live は止めると消えるため）。
        戻り値: ターンが開いていて一時停止できたか（非 TTY で Live が無くても、本文の確定印字は
        同じに要るので True。resume は Live を使う設定のときだけ作り直す）。
        """
        if not getattr(self, "_open", False):
            return False
        self._commit_body()
        self._flush_group()
        self._stop_live()
        return True

    def resume(self) -> None:
        """pause の対。Live を作り直す（ターンの状態はそのまま）。"""
        self._start_live()

    def set_wave(self, renderable: Any) -> None:
        with self._lock:
            self._wave = renderable

    def cancel_stall(self, elapsed: float) -> None:
        """cancel を送ってから長いこと `done` も届かない、を**1 回だけ**言う。

        ★なぜ要るか（実機・2026-09-03）: 中断が 73 秒止まらなかった走行があり、その間
          画面は何も出さず固まって見えた。serve の心拍（`notice kind=heartbeat`）は
          「走行中」の間しか出ない。`request_cancel` は飛んでいる HTTP を閉じるので、
          そこから次の反復境界に届くまでの区間は**心拍の対象の外側**で起き得る
          （心拍が鳴らない区間があるのは事実だが、その原因そのものは別の担当が追う）。
          ここはサーバの通知に頼らず、**クライアントの壁時計だけ**で判定する
          （app.run_turn が cancel を送った時刻からの経過を渡す）。
        ★これは確定印字（`_print`）で残す。心拍の `_waiting`（Live の頭行だけに出て
          消える）と違い、非 TTY や Live が畳まれた後にも読み返せる必要がある。
        """
        text = W_CANCEL_STALL.format(secs=fmt_secs(elapsed))
        self._print(Text(text, style=T.S_META))

    def on_event(self, ev: dict) -> None:
        if privacy_on():
            ev = _mask_obj(ev)          # ★入口で 1 回（色付けで文字列が割れる前に）
        et = str(ev.get("type") or "")
        if et == "notice" and str(ev.get("kind") or "") == "heartbeat":
            stage = str(ev.get("stage") or "推論機")
            # 段階の名前（推論機・道具 X）は本体との取り決め。画面の呼び名に替える
            shown = "推論サーバー" if stage == "推論機" else ("ツール " + stage[3:]) if stage.startswith("道具 ") else stage
            q = max(0, int(ev.get("quiet_secs") or 0))
            detail = str(ev.get("detail") or "").strip()     # 推論機の読み込みの進み具合（2026-10-07）
            with self._lock:
                self._quiet_secs = q
                self._waiting = (f"{shown} を実行中（{q} 秒 応答なし）" if stage.startswith("道具 ")
                                 else f"バックグラウンドタスクの完了を待っています（{q} 秒）" if stage == "背景の仕事"
                                 else f"{shown}: {detail}" if detail
                                 else f"{shown}の応答を待っています（{q} 秒）")
            return          # Live は refresh_per_second で自動再描画（明示の refresh は無い）
        with self._lock:
            self._waiting = ""
            self._quiet_secs = 0
        if et == "notice" and ev.get("kind") == "degenerate_retry":
            self._think_buf = ""            # 崩れた生成の思考は見せない（やり直しの思考が続いて来る）
        if et != "think":
            self._close_think()
        if et == "token":
            self._on_token(str(ev.get("text") or ""))
        elif et == "think":
            self._on_think(str(ev.get("text") or ""))
        elif et == "tool_call":
            self._on_tool_call(ev)
        elif et == "tool_result":
            self._on_tool_result(ev)
        elif et == "notice":
            self._on_notice(ev)
        elif et == "error":
            msg = str(ev.get("message") or "")
            self._errors.append(_norm(msg))
            first = f"{T.MARK_BAD} エラー（{ev.get('kind') or '?'}）: "
            self._print(Text(wrap_with_gutter(msg, self._width(), first, "  "),
                             style=T.S_BAD))
        elif et == "done":
            self._done = dict(ev)
        # ask はここでは扱わない（app が利用者に聞く）。ready/pong も app の持ち物。

    def end_turn(self) -> dict:
        self._close_think()
        self._flush_group()
        done = self._done or {}
        done_text = str(done.get("text") or "")
        # token で受けた本文があればその未印字分、1 文字も受けていなければ done.text
        # （native モードでは token が出ず done.text だけが本文になる）。同じ本文を二度は出さない。
        final = self._body_text if self._turn_text.strip() else done_text
        turn_text = self._turn_text if self._turn_text.strip() else done_text
        # 走行中のまま残った道具（done が先に来た／cancel）は未解決として記す
        stale = list(self._running.values())
        self._running = {}
        self._stop_live()
        for e in stale:
            # ★止まった道具は「実行中の印のまま永久に残る」のをやめ、失敗として閉じる（項目 15②）
            if card_on():
                # カード表示: 失敗のカードとして閉じる（行の形が 1 つだけ混ざると目が止まる）。
                e.ok, e.done = False, True
                e.summary = {**(e.summary or {}), "tail": W_NO_RESULT, "status": "error"}
                self._print_tool_card(e)
                continue
            self._print(self._head_line(e, mark=T.MARK_BAD, mark_style=T.S_MARK_BAD))
            self._print(Text(wrap_with_gutter(W_NO_RESULT, self._width(), TAIL, INDENT),
                             style=T.S_DIM))
        if self._think_chars and not card_on():
            # カード表示では思考の欄の見出しに秒が出る＝締めの前に同じ数を重ねない。
            self.console.print(Text(W_THINK.format(n=round(self._think_secs)), style=T.S_DIM))
        # ★同じ文面を 2 度出さない（項目 19 と同じ型の欠陥・合流の実機走行で発見）。
        #   届かない送り先に投げると、`error` の 1 行と `done.text` が**同じ文字列**で、
        #   80 桁の端末に同じ内容が続けて 2 度出ていた。
        if final.strip() and _norm(final) in self._errors:
            final = ""
        if final.strip():
            self._answer_label(self.console.print)
            self.console.print(_md_block(self.console, final, self.config_path))
        self._body_text = ""
        secs = _float(done.get("secs")) or (time.time() - self._t0 if self._t0 else 0.0)
        tc = done.get("tool_calls")
        n_tools = sum(_int(v) for v in tc.values()) if isinstance(tc, dict) and tc else len(self._bundle)
        line = self._done_line(done, secs, n_tools)
        if line is not None:
            self.console.print(line)
        if turn_text.strip():
            self.transcript.append({"role": "assistant", "content": turn_text})
        self._live = None
        self._open = False
        return {"text": turn_text, "secs": secs, "tools": n_tools}

    def expand_last(self) -> None:
        """直前の束（道具列）の全文: 引数と結果を切らずに出す。"""
        w = self._width()
        if not self._bundle:
            # ★空状態に次の一手（ui-ux-design §2）。この行を見る人は
            #   「壊れているのか、まだなのか」を知りたい。
            # ★旧実装は、道具を使った次のターンでも「まだ使われていません」と言っていた＝
            #   起きたことを起きていないと言う嘘だった（項目 22①）。
            if self._forgot:
                self.console.print(Text("この実行ログはもう残っていません（束はターンごとに入れ替わります）",
                                        style=T.S_DIM))
                self.console.print(Text("  → 見たいターンの直後に /expand を打ってください。",
                                        style=T.S_DIM))
            else:
                self.console.print(Text("ツールはまだ使われていません", style=T.S_DIM))
                self.console.print(Text(
                    "  → 実行が始まると、ここにツールの呼び出しと結果が並びます。",
                    style=T.S_DIM))
            return
        for e in self._bundle:
            self.console.print(self._head_line(e))
            self.console.print(Text(wrap_with_gutter(W_ARGS, w, TAIL, INDENT), style=T.S_META))
            try:
                args = json.dumps(e.input, ensure_ascii=False, indent=2)
            except Exception:
                args = repr(e.input)
            for ln in args.splitlines():
                self.console.print(Text(wrap_with_gutter(ln, w, INDENT, INDENT), style=T.S_DIM))
            for p in e.patches:
                if len(e.patches) > 1:
                    self.console.print(Text(
                        wrap_with_gutter(str(p.get("path") or ""), w, INDENT, INDENT + "  "),
                        style=T.S_META))
                for t in patch_lines(p, w):
                    self.console.print(t)
            self.console.print(Text(
                wrap_with_gutter(W_RESULT + ("" if e.done else W_RUNNING), w, TAIL, INDENT),
                style=T.S_META))
            body = e.result if e.done else W_RUNNING
            style = T.S_BAD if e.bad else T.S_TEXT
            for ln in (body.splitlines() or [W_EMPTY]):
                self.console.print(Text(wrap_with_gutter(ln, w, INDENT, INDENT), style=style))
            if e.truncated:
                self.console.print(Text(
                    wrap_with_gutter(f"… 切り詰め（実サイズ {num(e.chars)} 文字）", w, INDENT, INDENT),
                    style=T.S_DIM))

    # -- イベント ----------------------------------------------------------

    def _on_token(self, text: str) -> None:
        if not text:
            return
        self._flush_group()                 # 本文が来た＝読み取りの束を閉じる
        with self._lock:
            self._body_text += text
            self._turn_text += text

    def _on_think(self, text: str) -> None:
        if not text:
            return
        if self._group and not self._think_buf:
            self._flush_group()             # 思考が流れ始めた＝前の手番の読み取りの道具を先に出す（起きた順・2026-10-21）
        if self._think_t0 is None:
            self._think_t0 = time.time()
        self._think_chars += len(text)
        self._think_buf += text

    def _close_think(self) -> None:
        """思考の区間が終わった: 秒を足し、全文を "∴ " で出す。

        ★2026-09-21 オーナー指示「内部思考は出ていても問題ありません。
          というかでていないと推論しているんだかしていないんだか
          わかりません」→ **verbose 条件を外して既定で出す。**
          消したい人は `LAPS_SHOW_THINK=0`。
        """
        if self._think_t0 is not None:
            self._think_secs += time.time() - self._think_t0
            self._think_t0 = None
        if self._think_buf and _show_think():
            # ★溜めていた読み取りの道具（_group）を先に出す（2026-10-21・TUI で実測）。旧は次の手番の思考が先に刷られ、
            #   「∴ ExecStart は…次はサービスの状態。」の後に、その前に走った「✓ Read (deploy/console.service)」が並んでいた。
            self._flush_group()
            lines = self._think_buf.strip().splitlines() or [""]
            if card_on():
                # カード表示: 思考は左に縦線の欄（答えと道具から一目で分ける）。
                t = Text("┃ ", style=T.S_WALL)
                t.append("思考", style=f"bold {T.META}")
                if self._think_secs >= 1:
                    t.append(f"{SEP}{fmt_secs(self._think_secs)}", style=T.S_DIM)
                # 長い行も縦線の欄の中で折る（続き行に線が無いと答えの本文と混ざる）。
                for ln in wrap_with_gutter("\n".join(lines), max(4, self._width() - 2), "", "").splitlines():
                    t.append("\n┃ ", style=T.S_WALL)
                    t.append(ln, style=f"italic {T.NOTE}")
                self._print(t)
                self._think_buf = ""
                return
            # ★折り返しは自分でする（Rich に任せると続き行が桁 0 から始まり、思考と本文の境が崩れる・2026-10-13 TUI で実測）。
            self._print(Text(wrap_think("\n".join(lines), self._width()), style=T.S_DIM))
        self._think_buf = ""

    def _on_tool_call(self, ev: dict) -> None:
        name = str(ev.get("name") or "(unnamed)")
        inp = ev.get("input") if isinstance(ev.get("input"), dict) else {}
        e = ToolEntry(call=ev.get("call"), name=name, input=inp, read=read_kind(name, inp))
        self._commit_body()                 # 本文→道具の順序を残す
        # ★利用者への一文（本体の PROGRESS_FIELD・2026-10-30）。溜めた道具を先に出してから 1 行で（起きた順）。
        prog = str(inp.get("progress") or "").strip()
        if prog and prog != getattr(self, "_last_progress", ""):
            self._last_progress = prog
            self._flush_group()
            self._print(Text(wrap_think(prog, self._width()).replace("∴ ", "● ", 1), style=T.TEXT))
        if e.read is None:
            self._flush_group()             # 読み取り系でなくなった＝束を閉じる
        self._bundle.append(e)
        with self._lock:
            # call が無い／辞書の鍵にできない値なら固有の鍵で持つ（None で上書きすると先に走った
            # 道具が画面から消える）。結果側は名前の FIFO で当てる。
            self._running[_key(e.call, e)] = e
        # ★非 TTY（パイプ・TERM=dumb）でも走り出しの見出しは出さない（項目 6・15④）。
        #   旧実装はここで 1 行流していたので、その行の印は必ず「走行中」＝**色を持たない
        #   端末では成功も失敗も同じ字**になり、字で状態を表す設計が非 TTY でだけ効かなかった。
        #   見出しは結果が来てから 1 回だけ、本当の印（✓/✗）で出す。
        #   走行中の合図は項目 14③（app.py がターンの頭に 1 行出す）が持つ。

    def _on_tool_result(self, ev: dict) -> None:
        call = ev.get("call")
        name = str(ev.get("name") or "")
        with self._lock:
            e = self._running.pop(_key(call, None), None) if call is not None else None
            if e is None:                   # call の対応が取れない: 同名の最古を当てる
                for k, v in list(self._running.items()):
                    if v.name == name:
                        e = self._running.pop(k)
                        break
        if e is None:
            e = ToolEntry(call=call, name=name or "(unnamed)", input={}, read=read_kind(name, {}))
            self._bundle.append(e)
        e.done = True
        e.result = str(ev.get("result") or "")
        e.ok = bool(ev.get("ok")) if "ok" in ev else True
        e.kind = str(ev.get("kind") or "")
        e.chars = ev.get("chars") if isinstance(ev.get("chars"), int) else None
        e.truncated = bool(ev.get("truncated"))
        e.summary = ev.get("summary") if isinstance(ev.get("summary"), dict) else None
        e.secs = round(time.time() - e.t0, 1)
        self.transcript.append({"role": "tool", "name": e.name, "content": e.result,
                                "ok": e.ok, "input": e.input})
        if e.read is not None and not e.bad:
            self._group.append(e)           # 本文が来るまで溜める
            return
        self._print_tool(e)

    # kind → (印, 色, 文言が無いときの既定)。★内部の識別子は画面に出さない（項目 17）。
    # ★"cancel"/"cancelled" はここに置かない（2026-09-03）: 表引きの前に `_cancel_notice`
    #   が専用で扱う（1 回の Ctrl-C で最大 3 回届くので重複を畳む・下の分岐を見よ）。
    #   ここに残したままだと表と分岐の**二重の真実**になり、表だけ直して満足する事故を作る。
    _NOTICE: dict[str, tuple[str, str, str]] = {
        "phase_start": (T.MARK_RUN, T.S_META, ""),
        "phase_done": (T.MARK_OK, T.S_OK, ""),
        "phase_retry": (T.MARK_BAD, T.S_BAD, ""),
        "phase_failed": (T.MARK_BAD, T.S_BAD, ""),
        "phase_skip": ("", T.S_DIM, ""),
        "harness": ("", T.S_DIM, ""),
        # ★2026-09-04（W8・同じ捜索で見つけた、もう2つの同型の抜け）: serve.py 側は
        #   `cancel_error`/`checkpoint_error` に `message` を合成して渡している
        #   （serve.py `_RunState._on_event` の `elif etype in (…, "checkpoint_error",
        #   "cancel_error")` 分岐）——中身は「止めたのに後始末に失敗した」「控えを
        #   取れなかった」という、まさに見落とすと実害が大きい失敗の報せなのに、
        #   term 側の表に無いまま実際に無印・薄灰色で出ることを確認した（手で叩いて確認）。
        #   Windows は両方とも GATE_KINDS（wire.rs）。
        "cancel_error": (T.MARK_BAD, T.S_BAD, ""),
        "checkpoint_error": (T.MARK_BAD, T.S_BAD, ""),
        # ★2026-09-04（W8・4面プロトコル突き合わせの残件）: `answer_guard`/`user_stop`
        #   （agent.py `_answer_guard`/`_maybe_user_stop`）は `message` を持つので
        #   ws_guard と違って「識別子がそのまま出る」事故（項目 17）は起きないが、
        #   表に無いまま既定 ("", S_DIM, "") に落ちると**印も色も無い灰色**で埋もれる。
        #   Windows 側（`app/src-tauri/blackwall-core/src/wire.rs`）は
        #   `GATE_KINDS`/`PLAIN_KINDS` を持ち、`answer_guard` は GATE（左線・赤＝失敗の
        #   報せ、`test_windows_parity.py::test_the_gates_the_core_pushes_back_with_are_prominent_kinds`
        #   が固定）、`user_stop` は PLAIN（失敗ではない・ふつうの 1 行）に分類している。
        #   ★どちらも term/render.py の `_NOTICE` にはこれまで無く、この表と
        #   Windows の表を突き合わせる試験も無かった（端末側だけの独立した抜け）。
        #   answer_guard は ws_guard と同じ「ハーネスが実行を止めた」合図なので同じ扱い。
        "answer_guard": (T.MARK_BAD, T.S_BAD, ""),
        "offline": (T.MARK_BAD, T.S_BAD, ""),         # ★2026-09-08 監査 C2: offline 門の結末（message 付き）
        # user_stop は「利用者の『やめて』を仕組みで守って止まった」という**正しい**
        # 挙動（0.1.188 で outcome 側も「✗ 不明」から「✓ 利用者が終了」へ直した理由と同じ）。
        # 失敗（Gate）ではないので、phase_done と同じ OK 側の印にする。
        "user_stop": (T.MARK_OK, T.S_OK, ""),
        # ★2026-09-05（r4/app-term-clients）: `verifier`/`red_tests` は agent.py が
        #   出す2つの門（完了の主張と作業場の実体が食い違う／最後のテストが赤のまま
        #   完了にしない）の notice 本体（agent.py:1129, :1066）で、message を持つのに
        #   この表に無いまま既定 ("", S_DIM, "") ＝印も色も無い灰色に落ちていた。
        #   （kind="harness" 側の `[verifier N]`/`[red_tests 1]` という短い予告は
        #   `_GATE_MARKS` 経由で既に目立つ——漏れていたのは、その直後に来る
        #   objection_message 本体を運ぶこちらの notice。）
        #   Windows 側（wire.rs GATE_KINDS）は元から両方を目立つ側に置いており、
        #   `test_windows_parity.py::test_the_gates_the_core_pushes_back_with_are_prominent_kinds`
        #   が固定している。端末だけがこの2つを知らなかった。
        "verifier": (T.MARK_BAD, T.S_BAD, ""),
        "red_tests": (T.MARK_BAD, T.S_BAD, ""),
        # ★2026-09-06（owner 実機 P0）: 起動時に「この宛先はプロキシ経由になる」と
        #   気づいたときの報せ（serve.py `_announce_target`）。Windows の httpx は
        #   環境変数が空でも**レジストリ**の系プロキシを拾い、除外リスト
        #   （ProxyOverride）は読まない＝LAN の推論機宛てが黙ってプロキシへ回り、
        #   推論機には要求が 1 件も届かないまま「推論機の応答を待っています」だけが続く。
        #   実機で丸一日この形で止まった。**いちばん読ませたい 1 行**なので失敗側に立てる
        #   （Windows 側 wire.rs も GATE_KINDS）。
        "backend_proxy": (T.MARK_BAD, T.S_BAD, ""),
        # ★2026-09-05（同上・W1 の残り3種）: いずれも message を持つが表に無く、
        #   Windows（wire.rs PLAIN_KINDS）では「失敗ではないが見落とすと困る」
        #   ふつうの1行として `context_warn` と同じ扱い（無印・S_META）に分類されている
        #   ので、ここでも同じ扱いに合わせる（GATE ほど重くしない）。
        #   - engine_retry: 推論機が読み込みタイムアウトまで黙り、生存確認のうえ
        #     送り直した（agent.py `_on_backend_wait`）。黙って送り直すと利用者には
        #     沈黙が続くだけに見える。
        #   - session: 継続を頼まれたセッションが読めず、**黙って**新しく始めた
        #     （serve.py `_run`）＝会話の続きが実は途切れている。
        #   - ask_expired: 利用者が打った答えが時間切れの質問に届かず、
        #     本体は返事なしとして進んだ（serve.py `_dispatch`）＝入力が黙って捨てられた。
        "engine_retry": ("", T.S_META, ""),
        # ★2026-09-21: 生成が上限で切れたので続きを書かせる（0.1.231）。知らせるだけ。
        "gen_truncated": ("", T.S_META, ""),
        # ★2026-10-31: 子が要約した（記録に残すための知らせ・答えの下には残らない）
        "child_compacted": ("", T.S_META, ""),
        # ★2026-10-05: oc 中核の報せ（wire.rs と同じ分類）。要約に失敗＝この先で文脈切れになるので失敗の色。
        "compacting": ("", T.S_META, ""),
        "doom_loop": ("", T.S_META, ""),
        # ★2026-10-08: 見張り（oc/monitor.py）の新しい行・終わりの報せ（wire.rs PLAIN_KINDS と同じ）。
        "monitor": ("", T.S_META, ""),
        "monitor_error": (T.MARK_BAD, T.S_BAD, ""),
        "compaction_failed": (T.MARK_BAD, T.S_BAD, ""),
        "session": ("", T.S_META, ""),
        "ask_expired": ("", T.S_META, ""),
        # ★2026-09-05（r4b/app-term-clients・先取り配線）: `phase_hold_restored`
        #   （core-hold-fix が phases.py に足す予定の notice・まだこの worktree の
        #   phases.py/serve.py には無く `serve_notice_kinds()` にも出てこない）は
        #   「手番の終わりの検収がベストより減っていたので直前のベストへ戻した」という
        #   phase_retry と同格の**ハーネスの介入**の報せ。介入そのものは安全網として
        #   良い挙動だが、黙って直った体で流すと利用者は退行が起きたことに気づけない
        #   （verifier/red_tests と同じ理由で MARK_BAD/S_BAD 側に置く）。
        #
        #   ★2026-09-05 訂正（r4c/app-term-clients・反証8 must_fix#3）: 以前ここに
        #   「マージ後に completeness test が自動でこれを確かめる」と書いていたが
        #   **実測で偽だった**——`tests/_protocol_kinds.serve_notice_kinds()` は
        #   工程系 kind を直書きの固定集合でしか知らず、serve.py 側の転送ゲート
        #   （`_on()` の `if etype in (...)`）を読んでいなかったため、
        #   `phase_hold_restored` が実際に増えても門は発火しないままだった。
        #   `tests/_protocol_kinds.py` にそのゲートを動的に読む枝を足し
        #   （`tests/test_protocol_kinds.py` で固定）、これで初めて
        #   マージ後に `test_windows_knows_every_notice_kind_the_core_emits` /
        #   `test_term_knows_every_notice_kind_the_core_emits_or_names_the_gap`
        #   が本当に確かめられる状態になった。
        #
        #   ★統合時の注意（反証8 §C・反証12/14 が確認済み）: `r4/core-hold-fix` も
        #   `_NOTICE` に同一キー `phase_hold_restored` を足しており、どちらの
        #   順でマージしても CONFLICT (content) になる（両方向で実測済み）。
        #   **このブランチ（app-term-clients）側のハンクを採ること**——ここには
        #   `phase_hold_restored` 単体だけでなく `verifier`/`red_tests`/
        #   `engine_retry`/`session`/`ask_expired` の5種が同じ連続ブロックに
        #   入っており、core-hold-fix 側のハンクにはこの1行しか無い。値
        #   （`T.MARK_BAD, T.S_BAD`）はどちらも同じなので、app-term-clients 側を
        #   採っても core-hold-fix が意図した分類は失われない。詳細は
        #   REPORTS/app-term-clients-merge-order.md §6。
        "phase_hold_restored": (T.MARK_BAD, T.S_BAD, ""),
        # ★2026-10-13: oc 中核の促し・割り込み・途中経過（wire.rs PLAIN_KINDS と同じ・知らせるだけ）。
        #   表に無いと試験 test_term_knows_every_notice_kind… が落ちていた（2026-10-09〜12 に足した分）。
        "answer_nudge": ("", T.S_META, ""),
        "announce_nudge": ("", T.S_META, ""),
        "verify_nudge": ("", T.S_META, ""),
        "todo_nudge": ("", T.S_META, ""),
        "bg_wait_nudge": ("", T.S_META, ""),
        # 背景の仕事が動いている間は走行を閉じずに待つ（話しかければすぐ答える・oc/loop._park_for_jobs・2026-10-28）。
        "bg_park": ("", T.S_META, ""),
        # 台本から道具を呼んだ数（bwtool・0.1.585）。表に無く、知らせの表の試験が落ちていた。
        "bwtool": ("", T.S_META, ""),
        # 本文で言わなかったシステムの設定の変更を、ハーネスが 1 行知らせて走らせた（oc/disclose・2026-10-30）。
        "syschange": ("", T.S_META, ""),
        # 推論機が応答を断った（finish_reason=content_filter・2026-10-30）。
        "declined": (T.MARK_BAD, T.S_BAD, ""),
        # 長い Bash が背景の仕事（J1…）として続いた・走行の終わりに止めた（oc/loop._run_fg・2026-10-18）。
        "auto_bg": ("", T.S_META, ""),
        "explore_first": ("", T.S_META, ""),
        "checkpoint": ("", T.S_META, ""),
        "preempted": ("", T.S_META, ""),
        "degenerate_retry": (T.MARK_BAD, T.S_BAD, ""),   # wire.rs GATE_KINDS と同じ（崩れは目立たせる）
        # 画像をモデルが受け付けなかった（外して続ける）＝見落とすと「見てくれた」と誤解するので失敗の色。
        "image_rejected": (T.MARK_BAD, T.S_BAD, ""),
        # ★2026-10-16 監査: 表に無く既定の薄灰色（印なし）に落ちていた kind（wire.rs PLAIN_KINDS と同じ・知らせるだけ）。
        #   done_check＝道具を呼ばずに終えたので終わりか次の道具かを選ばせた／review＝別の目の点検／host_low＝ホストの資源が限界に近い／
        #   cancel_detail＝中断の副作用（推論機の線を閉じた）／queue_cleared＝「止める」で待ち行列も消した／
        #   inject_ignored＝割り込みが届かなかった（次の依頼へ持ち越す・走行が無い）／
        #   shutdown＝本体を終了する（文を持たないので既定の文を出す・wire.rs notice_view と同じ文）。
        "done_check": ("", T.S_META, ""),
        "review": ("", T.S_META, ""),
        "host_low": ("", T.S_META, ""),
        "cancel_detail": ("", T.S_META, ""),
        "queue_cleared": ("", T.S_META, ""),
        "inject_ignored": ("", T.S_META, ""),
        "shutdown": ("", T.S_META, "エンジンを終了します。"),
        # ★2026-10-18: 0.1.520 で足した 2 つ（wire.rs には足したが端末版の表に入れ忘れ、既定の色に落ちていた）。
        #   autoverify＝確かめずに終えようとしたのでハーネスが速い検査を走らせた／diffreview＝終える前の変更の見直し。
        "autoverify": ("", T.S_META, ""),
        "diffreview": ("", T.S_META, ""),
    }

    #: 画面に出さない kind（状態は app / `/status` の持ち物・問いが閉じた合図）。`_on_notice` の頭で返す。
    #: ★ask_closed（2026-10-16）: serve.py `_ask` が問いを閉じたときの合図。端末は問いを同期で聞くので閉じる箱が無い。
    _QUIET_KINDS = ("ready", "pong", "run_start", "queued", "ask_closed")

    #: 文脈まわりの 4 状態（SPEC 項目 16）。`harness` に潰さず、警告／圧縮／仕切り直し／
    #: 打ち切りを分ける。`notice`（kind を持たない素の通知）は agent.py:2780 の
    #: 「窓が狭い」の**事前警告**＝ここでは警告として扱う。
    #: ★2026-09-02 訂正: 以前ここには「serve.py が 4 種類とも `kind="harness"` に潰すので
    #:   この分岐は実機で発火しない」と書いてあったが、**それはもう事実ではない**
    #:   （`serve.py:1361` が `kind=str(payload.get("kind") or etype)` を渡している）。
    #:   実機の記録でも直近 14 日で `context_compacted` 332 件・`context_exhausted` 23 件が
    #:   出ている＝**この枝は生きている**。「死んだ枝」と書いたままにすると、次の担当が
    #:   掃除のつもりで消す。事実に反する案内は、案内が無いより悪い。
    _CONTEXT_KINDS = ("notice", "context_warn", "context_compacted",
                      "context_reset", "context_exhausted")

    #: この印で始まる通知は「いま書いた答えは捨てて書き直す」の合図。
    #: ★本文を捨ててよいのはここだけ（Windows 側 app/src-tauri/src/sidecar.rs:469 と同じ約束）。
    #: ★2026-10-13: 文の印ではなく**種類**で「いま流れた下書きは答えではない」と決まる通知（wire.rs RESTART_KINDS と同じ表）。
    #:   degenerate_retry＝崩れた生成をやり直す（崩れた本文も思考も捨てる）／preempted＝強制割り込みで生成を途中で止めた／
    #:   answer_nudge＝思考だけ・英語の分析・繰り返し・報告なしの終わり方を書き直させた（本文側に流れた分は思考として残す）。
    #:   Gemma 4 の TUI で、崩れた途中の本文がそのまま答えとして確定していたのを見て足した。
    _RESTART_KINDS = ("degenerate_retry", "preempted", "answer_nudge")

    _RESTART_MARKS = ("[fabrication ", "[pushback ", "[reset ",
                      "[verifier ", "[red_tests ", "[rerun ",   # 2026-09-03 の 3 つの門
                      # ★発明・矛盾の門（同日夜・sbx-B2）: 差し戻し前の「完了しました」の下書きを画面に残すと
                      #   同じ块が 2 回見える。この 2 つも下書きを捨てる側。
                      "[invent ", "[contradiction ")

    #: `harness`（kind="harness"）でくくられた通知のうち、**ハーネスがモデルの答えに
    #: 割り込んだ／モデルの見落としを指摘した**部類の印。既定の harness は無印・薄灰色
    #: だが、これらは見落とすと実害が大きい（差し戻し・堂々巡りの遮断・別ホスト
    #: 未接触の指摘…）。過去に「失敗の左線が存在しない」を実機で踏んでいる
    #: （2026-09-03）ので、目立つ形（印・色）に上げる。
    #: ★`_RESTART_MARKS`（本文を捨てるか）とは別の分類: host_scope や loop は
    #:   本文を捨てる対象ではないが、目立たせる対象ではある——重ねて手を入れない。
    _GATE_MARKS = ("[fabrication ", "[pushback ", "[reset ", "[verifier ",
                  "[red_tests ", "[rerun ", "[host_scope ", "[invent ", "[contradiction ", "[offline ", "[search_limit ",
                  "[loop]", "[Ω-", "[予算]", "[検査]")

    def _on_notice(self, ev: dict) -> None:
        kind = str(ev.get("kind") or "")
        msg = str(ev.get("message") or ev.get("text") or "")
        if kind in self._QUIET_KINDS:
            # 状態は app / /status の持ち物。★`run_start` と `queued` は **message を
            #   持たない**ので、下の `text = msg or default or kind` が内部識別子
            #   `run_start` をそのまま画面に出していた（実機で毎ターン 1 行・合流で発見）。
            #   利用者が今まさに送った直後で、「走り始めた」以上のことを言っていない。
            return
        # ★差し戻し／作り直しの通知なら、溜めた本文は**確定させず捨てる**（2026-08-30）。
        #   実機 20260830_183829_f36fba: 差し戻し2回＋停滞 Reset 2回で**同じ答えが5回**
        #   生成され、端末はそれを全部印字した（`_body_text` は道具呼び出しか通知の
        #   ときしか吐き出さず、iter 8 以降は道具を呼んでいない）。
        #   serve.py:1291 は `iter_start` で切れ目を出しており Windows 側は
        #   `[fabrication N]` で作り直しているのに、**端末版だけこの配線が無かった**。
        #   捨てるのは「次の生成に置き換えられる試作」だけ。道具の前に確定した本文は
        #   既に印字済みで、ここでは触らない（本文の欠落は作らない）。
        if kind == "answer_nudge":
            # 本文側に流れた分（英語の分析・繰り返し・短すぎる終わりの一言）は答えではないが、考えた跡なので思考として残す。
            with self._lock:
                draft, self._body_text = self._body_text, ""
                if draft.strip():
                    self._turn_text = self._turn_text[: max(0, len(self._turn_text) - len(draft))]
            if draft.strip() and _show_think():
                self._print(Text(wrap_think(draft.strip(), self._width()), style=T.S_DIM))
        elif kind in self._RESTART_KINDS or any(msg.startswith(m) for m in self._RESTART_MARKS):
            with self._lock:
                self._body_text = ""
                self._turn_text = ""
        else:
            # ★先に本文を確定させる（項目 15③）。旧実装は notice が**割り込んだ本文より先に**
            #   印字され、因果が逆に見えた。
            self._commit_body()
        w = self._width()
        if kind in ("cancel", "cancelled"):
            self._cancel_notice(ev, msg, w)
            return
        if kind == "inject_taken":       # 本体が差し込みを読んだ（2026-11-16・差し込みは別に出ているので重ねない）
            return
        if kind == "cancel_applied":
            self._cancel_applied_notice(ev, w)
            return
        if kind == "ws_guard":
            self._ws_guard_notice(ev, w)
            return
        if kind in self._CONTEXT_KINDS:
            self._context_notice(kind, msg, ev, w)
            return
        mark, style, default = self._NOTICE.get(kind, ("", T.S_DIM, ""))
        if kind == "harness" and any(msg.startswith(m) for m in self._GATE_MARKS):
            # ★上の一般則を上書きする（表には置かない・kind だけでは判定できない。
            #   "harness" は文言が全く違う十数種類を1つの型にくくっている）。
            mark, style = T.MARK_BAD, T.S_BAD
        elif kind == "subagent" and "✗" in msg:
            # ★2026-09-05（r4/app-term-clients・run50 の trace 再生で発見）: 委譲した
            #   子の道具呼び出しは `tools/task.py:88` が `kind="subagent"` の notice
            #   1本にまとめて親へ中継する（`"子: " + msg`）——成功も失敗も同じ kind
            #   なので、この表（kind 単位）では区別できない。無印のまま既定
            #   ("", S_DIM, "") に落ちると、**子が実際に失敗した道具呼び出し**
            #   （`msg` は `f"✗ {name} 失敗: …"`）まで親の画面では見えない。
            #   実測（run50-52 の trace 再生）: subagent notice 120 件中 5 件が
            #   ✗ 失敗（run51/52 は委譲そのものが0回）。既定の quiet（▶ 開始・
            #   ✓ 成功の中継）は据え置き、✗ だけを親の道具失敗と同じ重さで立てる。
            # ★r4b/app-term-clients（OSS 参照・第4回反証15 must_fix #5）:
            #   `refs/cline/sdk/packages/core/src/extensions/tools/team/
            #   delegated-agent.ts:79,154-155` の `onEvent` は子セッションの
            #   生イベントを型を保ったまま逐次そのまま親へ転送する（畳まない）。
            #   本実装は task.py 側で1本の `kind="subagent"` に畳んでいるため
            #   （そちらは本領域の範囲外・変えない）、cline のように「子のイベント
            #   の元の型」で成否を判別できず、代わりに message の `✗` 部分文字列に
            #   頼る必要がある——畳む設計を選んだ代償として記録しておく
            #   （詳細 REPORTS/app-term-clients-merge-order.md §5）。
            mark, style = T.MARK_BAD, T.S_BAD
        text = msg or default or kind
        if kind == "subagent" and text.startswith("子: "):   # 頭の「子: 」は本体との取り決め。画面の呼び名に替える
            text = "サブエージェント: " + text[3:]
        first = f"{mark} " if mark else ""
        self._print(Text(wrap_with_gutter(text, w, first, "  "), style=style))

    #: cancel の `applied`（serve.py `_accept_cancel`）を利用者の言葉にする。
    _CANCEL_APPLIED_JA = {
        "running": "実行中に届きました",
        "pending": "待ち行列に残っていたので取り消しました",
        "failed": "エンジンに伝えられませんでした（もう一度 Ctrl-C で強制終了してください）",
    }

    def _cancel_notice(self, ev: dict, msg: str, w: int) -> None:
        """中断（kind="cancel"/"cancelled"）は 1 回の Ctrl-C で最大 3 回届く

        （①クライアント自身の即時表示 `app._KeyWatch`/`interrupted()` ②serve の受理
        `_accept_cancel` ③agent が反復境界で確定させたとき）。3 つとも同じ定型文
        なので、そのまま描くと「中断を要求しました」が 2〜3 回並んでいた
        （実機・2026-09-03）。

        ★このターンで最初の 1 回だけ本文を出す。以降は**新しく分かったこと**
          （`applied`＝走行中／待ち行列／本体に伝わらなかった）があるときだけ、
          1 行の続報として足す（同じ `applied` は 2 度出さない）。`applied` を
          持たない 2〜3 回目（`cancelled` は message も applied も無いことが多い）は
          何も足さない——「もう受理されている」以上の情報が無いので黙る。
        """
        if not self._cancel_shown:
            self._cancel_shown = True
            text = msg or W_CANCEL
            self._print(Text(wrap_with_gutter(text, w, f"{T.MARK_BAD} ", "  "), style=T.S_BAD))
        applied = ev.get("applied")
        if applied is None:
            return
        applied = str(applied)
        if applied in self._cancel_applied_seen:
            return
        self._cancel_applied_seen.add(applied)
        note = self._CANCEL_APPLIED_JA.get(applied, applied)
        self._print(Text(wrap_with_gutter("→ " + note, w, "  ", "    "), style=T.S_META))

    def _cancel_applied_notice(self, ev: dict, w: int) -> None:
        """「止める」が**実際に何を切ったか**（serve `_accept_cancel` の cancel_applied）。

        ★これが無いと `_NOTICE` 表の既定に落ち、内部の識別子 `cancel_applied` が
          1 行そのまま画面に出る（項目 17 違反）。ws_guard で同じ穴を踏んだのに、
          同じ日に serve が足したこの kind は表に載っていなかった——**pty で
          端から端まで走らせて画面を読んだら 1 行目に出ていた**（2026-09-04・W2 実測）。
        ★出すのは「新しく分かったこと」だけ:
          ①`applied`（走行中／待ち行列／伝わらなかった）は `cancel` の受理でも届くので、
            同じ値は 2 度言わない（`_cancel_applied_seen` を共有する）。
          ②**推論器への接続を切れたか**。ここが 0 本のときが「止めたのに止まらない」の
            正体になりうる区間で、利用者に見えていなかった（実機で 73 秒）。
            `sockets` が無い（古い本体）なら黙る——推測を書かない。
        """
        applied = str(ev.get("applied") or "")
        if applied and applied not in self._cancel_applied_seen:
            self._cancel_applied_seen.add(applied)
            note = self._CANCEL_APPLIED_JA.get(applied)
            if note:
                self._print(Text(wrap_with_gutter("→ " + note, w, "  ", "    "), style=T.S_META))
        if ev.get("sockets") is None:
            return
        n = _int(ev.get("sockets"), 0)
        if n > 0:
            line = f"→ 推論サーバーへの接続を {n} 本切りました"
            style = T.S_META
        else:
            line = "→ 推論サーバーへの接続は掴めませんでした（返事が来るまで止まらないことがあります）"
            style = T.S_BAD
        # ★`bash_killed` は**書かない**（2026-09-04 実測）。agent.request_cancel は
        #   `kill_running()` が例外を投げなければ**数えずに 1 を足す**ので、走っている
        #   Bash が 1 つも無くても 1 になる（pty の実走で「走っていたコマンドも止めました」が
        #   出た＝嘘）。数えていない数を利用者の言葉にしない。
        self._print(Text(wrap_with_gutter(line, w, "  ", "    "), style=style))

    def _ws_guard_notice(self, ev: dict, w: int) -> None:
        """作業場の外へ触れようとした道具呼び出しを止めた、の合図（agent.py `_workspace_guard`）。

        ★payload に `message` は無く `tool`/`target`/`n` だけ——`_NOTICE` 表の
          「文言が無ければ既定」の仕組みでは組み立てられない。表に無いまま通すと
          既定 `("", S_DIM, "")` に落ち、**内部の識別子 `ws_guard` がそのまま画面に出る**
          （項目 17 違反。実機は未確認だがコードで確認・2026-09-03）。
        """
        tool = str(ev.get("tool") or "?")
        target = str(ev.get("target") or "?")
        n = _int(ev.get("n"), 1)
        text = f"ワークスペース外への {tool} を止めました（{target}）"
        if n > 1:
            text += f"・{n} 回目"
        self._print(Text(wrap_with_gutter(text, w, f"{T.MARK_BAD} ", "  "), style=T.S_BAD))

    def _context_notice(self, kind: str, msg: str, ev: dict, w: int) -> None:
        """文脈まわりの **4 状態**を字と色で分ける（SPEC 項目 16）。

        警告 = `S_META`（まだ何も失っていない）／圧縮 = `S_DIM`（自動で縮めた）／
        仕切り直し = `✗` ＋ 赤（会話を捨てた・作業は続く）／打ち切り = **太字の赤 ＋ 次の一手**
        （走行が終わった）。**重さが違う 4 つが同じ 1 種類の SGR で並んでいた**
        （`probe_states2.py` 実測: 4 種類の SGR が 1 種類だけ）。
        """
        if kind == "context_exhausted":
            rows = [ln.strip() for ln in (msg or W_CTX_EXHAUSTED).splitlines() if ln.strip()]
            rows = rows or [W_CTX_EXHAUSTED]
            self._print(Text(wrap_with_gutter(rows[0], w, "", "  "), style=T.S_BAD))
            # ★次の一手は本体の文面の 2 行目以降（無ければ既定）。失敗の赤い本文と混ぜない
            #   （項目 19②と同じ扱い）。段 2 に落として「→」で始める。
            steps = [ln if ln.startswith("→") else "→ " + ln for ln in rows[1:]] or [W_CTX_NEXT]
            for ln in steps:
                self._print(Text(wrap_with_gutter(ln, w, "  ", "    "), style=T.S_META))
            return
        if kind == "context_reset":
            # 会話は捨てたが走行は続く。**何が引き継がれたか**を同じ 1 行の中で言う
            #   （行を増やさない・SPEC 裁定 B）。
            text = f"{msg or W_CTX_RESET}{SEP}{W_CTX_RESET_KEPT}"
            self._print(Text(wrap_with_gutter(text, w, T.MARK_BAD + " ", "  "),
                             style=T.S_BAD_BODY))
            return
        if kind == "context_compacted":
            before, after = ev.get("before"), ev.get("after")
            text = (W_CTX_COMPACT.format(before=num(before), after=num(after))
                    if before is not None and after is not None
                    else (msg or W_CTX_COMPACTED))
            self._print(Text(wrap_with_gutter(text, w, "", "  "), style=T.S_DIM))
            return
        # 警告（窓が狭い）。本体が数字つきの文面を持つので、そのまま出す。
        self._print(Text(wrap_with_gutter(msg or W_CTX_WARN, w, "", "  "), style=T.S_META))

    # -- 描画部品 ----------------------------------------------------------

    @staticmethod
    def _mark_of(e: ToolEntry) -> tuple[str, str]:
        """状態を表す**字**と色（項目 6）。色が 1 バイトも出なくても字は残る。"""
        if not e.done:
            return T.MARK_RUN, T.S_MARK_RUN
        return (T.MARK_BAD, T.S_MARK_BAD) if e.bad else (T.MARK_OK, T.S_MARK_OK)

    def _width(self) -> int:
        return _int(getattr(self.console, "width", 80), 80) or 80

    def _status_enabled(self) -> bool:
        """常時ステータス行を出すか。`NO_COLOR` / `TERM=dumb` では**出さず**、旧来どおり
        心拍（heartbeat）の待ち 1 行だけに退避する（§E）。

        ★なぜ NO_COLOR で退避するか（実測・check_tui_width.py）: 色の無い端末では脈動印・
          トークンの見栄えが意味を持たない一方、画面下に**常時 1 行**を置くと、道具の頭行
          （`✓ Bash (...)`）を上に流すたびに Live の消去（カーソル上移動）がその行に食い込み、
          「色が無い端末で成功と失敗の頭が ✓/✗ で割れる」門を崩す。非 TTY（パイプ）は
          そもそも Live を持たないのでここには来ない（`_use_live` が False）。
        ★`--calm`（色はある・動きだけ弱い）は退避に含めない＝ここは True を返し、印だけ
          静止させる（`_status_animate`）。
        """
        if not self._use_live:
            return False
        # ★コンソールの `no_color` を先に見る（`Console(no_color=True)` は環境変数を
        #   立てない・実測）。環境変数はコンソールを介さない経路のための保険。
        if getattr(self.console, "no_color", False):
            return False
        import os
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("TERM", "").strip().lower() == "dumb":
            return False
        return True

    def _status_animate(self) -> bool:
        """脈動印を動かすか。reduced-motion 相当（`--calm` などで波が calm）では静止印に
        退避（§B-3/§B-6）。字（印）と主ラベルは動かないので、動きが止まっても意味は消えない。
        NO_COLOR/dumb/非 TTY は `_status_enabled` が False＝そもそもステータス行が出ない。"""
        if not self._status_enabled():
            return False
        if record_on():
            return False                # 録画: 印を止める（画が変わらない間は asciinema が詰められる）
        w = self._wave
        if w is not None and getattr(w, "calm", False):
            return False
        return True

    def _status_row(self, *, running: list, waiting: str, quiet: int, elapsed: float,
                    out_chars: int, width: int, animate: bool, now: float,
                    live_turn: bool) -> Text:
        """推論中の常時ステータス行 `<脈動印> <主ラベル>  <経過>  ↓ ~<tok>`（§B）。

        主役は「いま実際に何をしているか」（道具名・段階）。純推論のときだけ Gerund を回す。
        経過・トークンはターンが開いている間だけ足す（§B-4）。**幅を動かさない**ため、
        狭いときは `fit_parts` で tokens→経過 の順に落とし、印と主ラベルは残す（§B-6）。
        """
        mark = _pulse_mark(now, animate)
        if running:                                   # ①道具が走っている＝substance を主役に
            label = (_tool_status_label(running[-1]) if len(running) == 1
                     else f"ツール {len(running)} 個を実行中")
            if quiet > 0:                             # 走行中に沈黙が続くなら末尾へ添える
                label += f"（{quiet} 秒 応答なし）"
        elif waiting:                                 # ②心拍の一文（stage＋沈黙を既に含む）
            label = waiting
        else:                                         # ③純推論＝本当に中身が不透明な区間だけ装飾
            label = _gerund(elapsed) + "…"
        # (印, ラベル) は落とさない（rank 0）。経過→トークンは狭いとき後ろから落とす。
        parts: list[tuple[str, str, int]] = [
            (mark, T.S_WALL if card_on() else T.S_MARK_RUN, 0),
            (" " + label, T.S_META, 0),
        ]
        if live_turn:
            parts.append((f"{SEP}{fmt_secs2(elapsed)}", T.S_DIM, 2))
            # ★2026-09-13（オーナー指摘「推論中は 0 から動かず、終わってから変わる」）:
            #   ここは**受け取った文字数からの推定**なので、`token` が流れない経路では
            #   最後まで 0 のまま動かない。既定の native tool-calling では本文が
            #   `done.text` でまとめて来る（本ファイル 909 行の注記と同じ事情）＝
            #   **構造的に 0 が出続ける**。
            #   ★**測れていない数字を 0 として出さない**（この木の約束「分からないことは言わない」）。
            #     流れている経路では従来どおり増えていく。
            if out_chars > 0:
                parts.append((f"{SEP}↓ {_fmt_tok_est(out_chars)}", T.S_DIM, 3))
        return fit_parts(parts, max(1, int(width) - 1))

    def _head_line(self, e: ToolEntry, *, running: bool = False,
                   mark: str | None = None, mark_style: str | None = None) -> Text:
        """`✓ Bash (systemctl restart …)` の 1 行。

        ★項目 6: 状態は字（✓/✗/▸）＋色。★項目 10: 太字は名前ではなく**引数の先頭の語**
          （見分けたいのはサービス名やファイル名で、`Bash` の 4 文字ではない）。
        ★項目 3①: 端末幅で折り返し、続き行は段 2 に落とす（旧実装は桁 0 に落ちていた）。
        ★項目 14: 走行中は末尾に経過と中断の作法（**行は増えない**）。
        """
        d_mark, d_style = self._mark_of(e)
        mark = d_mark if mark is None else mark
        mark_style = d_style if mark_style is None else mark_style
        body = e.name
        head = " ".join(str(e.head).split())      # 改行は畳む（頭行は 1 つの文）
        if head:
            body += f" ({head})"
        if running and self._use_live:
            # 刷新される画（Live）のときだけ経過を出す。流しっぱなしの行に出すと 0s で固まる。
            body += f"{SEP}{fmt_secs(time.time() - e.t0)}{SEP}{W_HINT_RUN}"
        # ★席は「端末幅 − 印 1 桁 − 空白 1 桁」。**下限を 20 にしない**（2026-09-02 実測）:
        #   幅 8〜21 の端末でこの行だけが 21 桁を吐き、端末が自前で折り返して段が消えていた
        #   ——SPEC 項目 1③ が `wrap_with_gutter` から取り除いた `max(8, …)` と**同じ形の欠陥**が、
        #   その兄弟であるここに残っていた（「原因の形」で grep して見つけた）。
        #   どの道具が失敗したかを名指す行なので、崩れたときの損は本文より大きい。
        wrapped = wrap_with_gutter(body, max(1, self._width() - 2), "", "  ")
        t = Text()
        t.append(mark, style=mark_style)
        t.append(" ")
        start = len(t.plain)
        t.append(wrapped, style=T.S_META)
        if head:
            i = wrapped.find("(")
            if i >= 0:
                j = k = i + 1
                while k < len(wrapped) and not wrapped[k].isspace() and wrapped[k] != ")":
                    k += 1
                if k > j:
                    t.stylize(f"bold {T.TEXT}", start + j, start + k)
        return t

    def _body_lines(self, lines: list[str], style: str, w: int) -> None:
        """結果の本文を段付きで流す（項目 2: ここが段を通していない経路だった）。"""
        for ln in lines:
            self._print(Text(wrap_with_gutter(ln, w, INDENT, INDENT), style=style))

    def _print_tool(self, e: ToolEntry) -> None:
        # ★予定表（TodoWrite）は「⎿ [」（結果の JSON の頭）ではなく、段のチェックリストで出す（2026-10-21・アプリと同じ）。
        if e.name == "TodoWrite" and not e.bad and self._print_plan(e):
            return
        # ★終わりの合図（Done）は答えの後ろに「✓ Done ⎿ OK」を出さない（tools の表と /expand には残る）。
        if e.name == "Done" and not e.bad:
            return
        if card_on():
            self._print_tool_card(e)
            return
        self._print_tool_lines(e)

    # ── 予定表（TodoWrite・2026-10-21 オーナー「プランを明確に決定してからタスクを順番に進める…この画像のように」）──
    #   最初の予定（と段の組が変わったとき）は全部の段を、途中の更新と全部済んだときは変わった段だけを出す
    #   （会話の枠は書き足ししかできない＝毎回全部を出すと、同じ一覧が何度も並ぶ）。走行中の欄には、いまの段を 1 行出す。
    PLAN_MARKS = {"completed": T.MARK_OK, "in_progress": "◉", "pending": "◌", "cancelled": "-"}

    def _plan_row(self, content: str, status: str, w: int) -> Text:
        mark = self.PLAN_MARKS.get(status, "◌")
        t = Text(wrap_with_gutter(content, w, f"  {mark} ", "    "))
        if status == "completed":
            t.stylize(T.S_DIM)
            t.stylize(T.S_MARK_OK, 2, 3)
        elif status == "in_progress":
            t.stylize(f"bold {T.TEXT}")
            t.stylize(f"bold {T.ALERT}", 2, 3)
        elif status == "cancelled":
            t.stylize(f"strike {T.NOTE}")
        else:
            t.stylize(T.S_META)
            t.stylize(T.S_DIM, 2, 3)
        return t

    def _print_plan(self, e: ToolEntry) -> bool:
        items = plan_items(e.result, e.input)
        if not items:
            return False
        prev = list(getattr(self, "_plan", None) or [])
        self._plan = items
        done = sum(1 for _c, s in items if s in ("completed", "cancelled"))
        full = not prev or [c for c, _s in prev] != [c for c, _s in items]
        rows = items if full else [(c, s) for (c, s), (_pc, ps) in zip(items, prev) if s != ps]
        if not rows:
            return True                     # 何も変わっていない更新は出さない
        w = self._width()
        head = Text("☰ ", style=T.S_META)
        head.append("予定", style=f"bold {T.TEXT}")
        head.append(f"{SEP}{done}/{len(items)}", style=T.S_DIM)
        self._print(head)
        for c, s in rows:
            self._print(self._plan_row(c, s, w))
        return True

    def plan_now(self, width: int) -> Text | None:
        """走行中の欄に出す「いまの段」の 1 行（予定が無い・全部済んだなら None）。"""
        items = list(getattr(self, "_plan", None) or [])
        cur = next((i for i, (_c, s) in enumerate(items) if s == "in_progress"), None)
        if cur is None:
            return None
        done = sum(1 for _c, s in items if s in ("completed", "cancelled"))
        t = Text("◉ ", style=f"bold {T.ALERT}")
        t.append(f"{done + 1}/{len(items)}  ", style=T.S_DIM)
        body = " ".join(items[cur][0].split())
        room = max(4, width - cell_width(t.plain) - 1)
        if cell_width(body) > room:
            while body and cell_width(body) > room - 1:
                body = body[:-1]
            body += "…"
        t.append(body, style=f"bold {T.TEXT}")
        return t

    def _print_tool_card(self, e: ToolEntry) -> None:
        """カード表示: 行の形（_print_tool_lines）が出す物をそのまま集め、枠で包む。
        ★中身の作り（畳み・失敗の次の一手・差分・書いた中身）は行の形と同じ物を使う＝二重に持たない。"""
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        buf: list = []
        inner = max(20, self._width() - 4)
        self._print = buf.append                      # type: ignore[method-assign]
        self._width = lambda: inner                   # type: ignore[method-assign]
        was = e.head_printed
        e.head_printed = True                         # 見出しはカードの題に置く
        try:
            self._print_tool_lines(e)
        finally:
            del self._print
            del self._width
            e.head_printed = True if was else e.head_printed
        mark, mstyle = self._mark_of(e)
        # 題の札: 種類（実行・読む・書く…）を地つきの札にする。失敗は札ごと明るい赤に反転。
        chip = T.S_CHIP_BAD if e.bad else T.S_CHIP
        title = Text(" ")
        title.append(" ", style=chip)
        title.append(mark, style=chip if e.bad else f"bold {mstyle} on {T.CHIP_BG}")
        title.append(" " + _CARD_KIND.get(e.name, "ツール") + " ", style=chip)
        title.append(f" {e.name}", style=T.S_META)
        head = " ".join(str(e.head).split())
        room = max(0, self._width() - cell_width(title.plain) - 14)
        if head and room > 8:
            if cell_width(head) > room:
                cut = head
                while cut and cell_width(cut) > room - 1:
                    cut = cut[:-1]
                head = cut + "…"
            title.append("  " + head, style=T.S_META)
        title.append(" ")
        sub = Text(f" {e.secs:.1f}s ", style=T.S_DIM) if (e.secs or 0) >= 0.05 else None
        self._print(Panel(Group(*buf) if buf else Text(""), title=title, title_align="left",
                          subtitle=sub, subtitle_align="right", box=box.ROUNDED,
                          border_style=T.S_BAD if e.bad else T.S_WALL, padding=(0, 1), expand=True))

    def _print_tool_lines(self, e: ToolEntry) -> None:
        """道具 1 回 = 2 行（＋差分／結果の先頭と末尾）。"""
        if not e.head_printed:
            self._print(self._head_line(e))
            e.head_printed = True
        tail = e.tail
        if e.secs is not None and e.secs >= 2:
            tail = f"{tail}{SEP}{e.secs:.0f}s"
        style = T.S_BAD if e.bad else T.S_META
        # ★折り返しでも段を保つ（2026-08-28・UI 監査）。旧実装は改行だけ揃えており、
        #   端末幅で折り返された続き行は桁 0 に落ちて ⎿ の段が消えていた。
        w = self._width()
        self._print(Text(wrap_with_gutter(tail, w, TAIL, INDENT), style=style))
        if e.bad:
            # ★同じ本文を 2 度出さない（実測: 単行の失敗は tail と本文が同じ文字列で、
            #   80 桁の端末に**同一の 4 行が 2 回**出ていた）。旧実装は「本文全体が tail と
            #   同じか」しか見ておらず、**本文が複数行だと重複除けが効かなかった**（項目 19）。
            body = (e.result or "").strip().splitlines()
            head_t = (e.tail or "").strip()
            if body and body[0].strip() == head_t:
                body = body[1:]
            # 「→ …」は本体が付けた次の一手。失敗の赤い本文と混ぜない（項目 19②）
            advice = [ln for ln in body if ln.lstrip().startswith("→")]
            body = [ln for ln in body if not ln.lstrip().startswith("→")]
            shown, hidden, last = fold_physical(body, w, INDENT, FAIL_HEAD_ROWS, FAIL_TAIL_ROWS)
            # 失敗の本文は太字にしない（赤の非太字＝ALERT。全部太字は「叫び」になる）
            self._body_lines(shown, T.S_BAD_BODY, w)
            if hidden:
                self._print(Text(wrap_with_gutter(W_REST.format(n=num(hidden)), w, INDENT, INDENT),
                                 style=T.S_DIM))
                self._body_lines(last, T.S_BAD_BODY, w)
            for ln in advice:
                self._print(Text(wrap_with_gutter(ln.strip(), w, INDENT, INDENT + "  "),
                                 style=T.S_META))
            # ★失敗には必ず次の一手を 1 行（項目 19③）。短い失敗にも出す。
            # ★色は S_META（合流で決めた・画面担当の申し送り #3）。目盛りは NOTE(=S_DIM) を
            #   「読ませる本文には使わない」と自分で書いており（Solarized dark で 4.37:1）、
            #   **失敗のときの次の一手は読ませたい行**なので注記の色に置けない。
            #   SPEC §4-2 が NOTE 側に挙げているのは「畳んだ残り行数」など受け身の注記で、
            #   そちらは S_DIM のまま（下の Bash 成功側）。7.07:1 / 最悪 5.61:1。
            self._print(Text(wrap_with_gutter(W_EXPAND, w, INDENT, INDENT), style=T.S_META))
            return
        patches = e.patches
        if patches:
            for p in patches:
                if len(patches) > 1:
                    self._print(Text(
                        wrap_with_gutter(str(p.get("path") or ""), w, INDENT, INDENT + "  "),
                        style=T.S_META))
                for t in patch_lines(p, w):
                    self._print(t)
            return
        if e.name in ("Write", "NotebookEdit"):
            # ★書いた中身を出す。結果文字列ではなく**道具の引数**にしか無い。
            body = str((e.input or {}).get("content") or "")
            if body.strip():
                shown, hidden, last = fold_physical(body.splitlines(), w, INDENT,
                                                    BASH_HEAD_ROWS, BASH_TAIL_ROWS)
                self._body_lines(shown, T.S_DIM, w)
                if hidden:
                    self._print(Text(wrap_with_gutter(W_REST.format(n=num(hidden)),
                                                      w, INDENT, INDENT), style=T.S_DIM))
                    self._body_lines(last, T.S_DIM, w)
                    self._print(Text(wrap_with_gutter(W_EXPAND, w, INDENT, INDENT),
                                     style=T.S_DIM))
            return
        if e.name == "Bash":
            shown, hidden, last = fold_physical(e.result.splitlines(), w, INDENT,
                                                BASH_HEAD_ROWS, BASH_TAIL_ROWS)
            self._body_lines(shown, T.S_DIM, w)
            if hidden:
                self._print(Text(wrap_with_gutter(W_REST.format(n=num(hidden)), w, INDENT, INDENT),
                                 style=T.S_DIM))
                self._body_lines(last, T.S_DIM, w)
                self._print(Text(wrap_with_gutter(W_EXPAND, w, INDENT, INDENT), style=T.S_DIM))

    def _flush_group(self) -> None:
        """溜めた読み取り系を 1 行に圧縮して出す: 読み取り N件・検索 M件・一覧 K件 (/expand)。

        1 件だけなら圧縮しない（「読み取り 1件」より「Read (a.py) ⎿ Read 3 lines」のほうが情報が多い。
        Claude Code も 1 件の read/search は畳まない）。
        """
        if not self._group:
            return
        if len(self._group) == 1:
            e = self._group.pop()
            self._print_tool(e)
            return
        counts = {"read": 0, "search": 0, "list": 0}
        for e in self._group:
            counts[e.read or "read"] += 1
        parts = [f"{_KIND_LABEL[k]} {n}件" for k, n in counts.items() if n]
        self._group = []
        # ★`(/expand)` は狭い端末では落とす（同じ理由で末尾空白が出る・fit_parts の申し送り）。
        #   件数のほうが先に要る情報で、`/expand` は /help にも載っている。
        self._print(fit_parts([
            (T.MARK_OK, T.S_MARK_OK, 0),
            (" " + "・".join(parts), T.S_META, 0),
            (SEP + "(/expand)", T.S_DIM, 1),
        ], max(1, self._width() - 1)))

    def _commit_body(self) -> None:
        """ここまでの本文を確定印字して Live から外す（本文→道具→本文の順序を残す）。"""
        with self._lock:
            text, self._body_text = self._body_text, ""
        if text.strip():
            self._answer_label(self._print)
            # ★2 行以上の本文（答え・まとめ）は前後を 1 行空ける（2026-10-21 TUI の見直し: 「∴ 200 が返った。」の思考の直後に
            #   答えの見出しが続き、思考・道具・答えの境が見えなかった）。1 行の語り（「〜を読みます。」）は詰めたまま。
            gap = not card_on() and sum(1 for ln in text.strip().splitlines() if ln.strip()) > 1
            if gap:
                self._print(Text(""))
            self._print(_md_block(self.console, text, self.config_path))
            if gap:
                self._print(Text(""))

    def _answer_label(self, out: Any) -> None:
        """カード表示: ターンで最初の答えの前に銘を 1 度だけ置く（誰の言葉かを一目で）。"""
        if not card_on() or getattr(self, "_said", False):
            return
        self._said = True
        t = Text("▐ ", style=T.S_BAD)
        t.append("L.A.P.S", style=T.S_BAD)
        out(t)

    def _done_line(self, done: dict, secs: float, n_tools: int) -> Text | None:
        """締めの 1 行。**何も言うことが無いなら出さない**（項目 8）。

        出す条件は 4 つのいずれか: ①outcome が accepted 以外 ②束に失敗が 1 件以上
        ③道具を 1 つ以上使った ④10 秒以上かかった。
        ★桁を固定する（秒・道具数・tok/s）。左揃えだと走行どうしを縦に比べられない。
        """
        outcome = str((done or {}).get("outcome") or "")
        ok = outcome in _ACCEPTED
        n_bad = sum(1 for e in self._bundle if e.bad)
        if outcome == "accepted" and not n_bad and n_tools == 0 and secs < 10:
            return None
        word = _OUTCOME_JA.get(outcome, outcome)
        # ★印は**走行の結末**で決める（2026-09-03 dogfood ⑮⑱）。旧: 道具の失敗が 1 つでもあると ✗＝
        #   `python` が無くて python3 に切り替えただけの**成功した走行**が赤で締まり、成功が失敗に見えた。
        #   失敗の件数は `失敗 N` を赤で残す（旧実装の「1 文字も残らない」には戻さない）。
        good = ok
        # ★生成だけの速さ（推論機の timings）があればそちら。合計÷所要秒は読み込みの時間を含み、Gemma 4 の 1 手番目が 5 tok/s と出た。
        tok_s = _float((done or {}).get("gen_tok_s")) or _float((done or {}).get("tok_s"))
        # (本文, スタイル, 落としてよい順)。0 = 落とさない。**大きい順**に落とす。
        # ★何を先に捨てるか: tok/s（純粋な統計・3）→ 道具の数（2）→ 所要秒（1）。
        #   印・`done`・失敗の件数・outcome の語は落とさない（落とすと行の用が消える）。
        parts: list[tuple[str, str, int]] = [
            (T.MARK_OK if good else T.MARK_BAD, T.S_MARK_OK if good else T.S_MARK_BAD, 0),
            (" done", T.S_META, 0),
            (f"{DONE_SEP}{secs:>4,.0f}s", T.S_META, 1),
            (f"{DONE_SEP}ツール {n_tools:>2,}", T.S_META, 2),
        ]
        if n_bad:
            parts.append((f"{DONE_SEP}失敗 {n_bad:>2,}", T.S_BAD, 0))
            # ★どの道具が失敗したか（2026-10-27 SNS の撮影: tools の箱を閉じていると「失敗 1」が何の失敗か分からない）。
            #   名前だけ・多い順・3 種まで。狭い端末では道具の数と同じ順で先に落とす（件数は残す）。
            from collections import Counter as _Counter
            _bad = _Counter(e.name for e in self._bundle if e.bad).most_common()
            _names = "・".join(f"{k} ×{v}" if v > 1 else k for k, v in _bad[:3]) + (" 他" if len(_bad) > 3 else "")
            parts.append((f"（{_names}）", T.S_BAD, 2))
        # ★2026-09-11（vdir #v94）: 「調べただけ」と「直した」を締めで区別する。
        #   vdir が同じ故障・同じ依頼文で現場の大きさだけ変えたところ、大きい現場では
        #   **1本もファイルを書かずに accepted** で終わった。記録上は成功と同じに見え、
        #   利用者は直ったと読む。作業場が実際に変わったかは checkpoint が知っているので、
        #   **変わっていないなら、そう言う**。
        #   ★判定はしない（意図を当てにいくと、質問に答えただけの回を失敗と貼る）。
        #     事実を出すだけ。落とさない（順位 0）——この行の用はここにあるので。
        #   ★None は「判定できなかった」。False と混ぜない（控えが無いのに断定しない）。
        if (done or {}).get("changed") is False:
            parts.append((f"{DONE_SEP}変更なし", T.S_BAD, 0))
        # ★2026-09-12: 完了と言っているのに名指しの成果物が無い、を締めで出す。
        #   `変更なし` と**まったく同じ約束**: 判定しない・事実だけ・落とさない（順位 0）。
        #   ★モデルには渡らない（差し戻しは 2026-09-10 のオーナー指示で撤去した）。
        #     ここは「人が一目で気づける」ための行であって、モデルを直す仕掛けではない。
        #   根拠: 24 課題の不合格 16 本のうち 2 本が「report.txt が無い」「aggregates.json が
        #   無い」＝走り切って accepted なのに成果物が無い型だった。
        _miss = (done or {}).get("missing")
        if isinstance(_miss, list) and _miss:
            _names = "・".join(str(x) for x in _miss[:2])
            if len(_miss) > 2:
                _names += f" 他{len(_miss) - 2}"
            parts.append((f"{DONE_SEP}見当たらない {_names}", T.S_BAD, 0))
        # ★2026-09-14: 書いたのに一度も走らせていないファイル。`変更なし` `見当たらない` と
        #   同じ約束（判定しない・事実だけ・落とさない）。実機で「9 本書いて Bash は mkdir
        #   1 本」の走行が「動作確認が完了しました」と締めた（版 0.1.223・09-13 23:59）。
        _unrun = (done or {}).get("unrun")
        if isinstance(_unrun, list) and _unrun:
            import os as _os
            _un = "・".join(_os.path.basename(str(x)) for x in _unrun[:2])
            if len(_unrun) > 2:
                _un += f" 他{len(_unrun) - 2}"
            parts.append((f"{DONE_SEP}未実行 {_un}", T.S_BAD, 0))
        # 圧縮が走った回数。窓が足りなかったことを利用者が知れる（診断なので落として可）。
        _comp = (done or {}).get("compactions")
        if isinstance(_comp, int) and _comp > 0:
            parts.append((f"{DONE_SEP}圧縮 {_comp:>2,}", T.S_META, 3))
        # ★1 秒に満たないターンの「毎秒何トークン」は測れていない数字。実機で、届かない
        #   送り先へ 1 回投げただけの 0 秒のターンに `117.0 tok/s` と出た（合流の走行で発見）。
        #   出さないほうが正しい。**分からないことは言わない**（数字は嘘をつくと高い）。
        if tok_s and secs >= 1:
            parts.append((f"{DONE_SEP}{tok_s:>5.1f} tok/s", T.S_META, 3))
        # ★締めでは走行中の推定（`↓ ~k`）を**実測**に置き換える（§B-4）。本体が usage を
        #   返したときだけ出す（`total_tokens`/`prompt_tokens` があるとき）。`~` は付けない
        #   ＝これは実測。走行中の推定はステータス行が担い、Live 終了で消える。
        # ★2026-09-13（オーナー指摘「消費トークンとかみ合っていない」）: 矢印の意味が
        #   走行中と締めで**食い違っていた**。走行中の `↓` は「受け取った本文の推定」、
        #   締めの `↓` は **total_tokens（入力＋出力）**で、`↑` が入力だった。
        #   同じ記号が 2 つの意味を持てば、読む人は必ず取り違える。
        #   → 締めは **入力／出力**と書く。出力は usage にあればそれ、無ければ total−入力。
        total_tok = (done or {}).get("total_tokens")
        prompt_tok = (done or {}).get("prompt_tokens")
        out_tok = (done or {}).get("completion_tokens")
        if not isinstance(out_tok, int) and isinstance(total_tok, int) and isinstance(prompt_tok, int):
            out_tok = total_tok - prompt_tok
        if isinstance(prompt_tok, int) and prompt_tok > 0:
            parts.append((f"{DONE_SEP}入力 {num(prompt_tok)}", T.S_META, 4))
        if isinstance(out_tok, int) and out_tok > 0:
            parts.append((f"{DONE_SEP}出力 {num(out_tok)}", T.S_META, 3))
        if word:
            parts.append((f"{DONE_SEP}{word}", T.S_META if ok else T.S_BAD, 0))
        # 右端 1 桁は空けておく（端末が自前で折り返すのを避ける・wrap_with_gutter と同じ約束）
        if card_on():
            # カード表示: 締めを暗い赤の線で挟む（`── ✓ done · … ─────`）。線は飾り＝足りなければ出さない。
            room = max(1, self._width() - 1)
            line = fit_parts(parts, max(1, room - 3))
            out = Text("── ", style=T.S_WALL_RULE)
            out.append_text(line)
            fill = room - 3 - cell_width(line.plain) - 1
            if fill >= 2:
                out.append(" " + "─" * fill, style=T.S_WALL_RULE)
            return out
        return fit_parts(parts, max(1, self._width() - 1))

    # -- Live との付き合い ---------------------------------------------------

    def _print(self, renderable: Any) -> None:
        """Live の上に 1 つ流す。記録付き Console では Live を一旦止めて記録に載せる。"""
        live = self._live
        if live is None:
            self.console.print(renderable)
            return
        if self._recording:
            live.stop()
            self.console.record = True
            try:
                self.console.print(renderable)
            finally:
                self.console.record = False
                live.start()
        else:
            # ★Live の刷新スレッドと同じ錠を持って印字する。Rich の Console.print は
            #   「消す量の計算（process_renderables・錠あり）」と「描いて buffer に積む（錠なし）」が
            #   別々で、その隙に刷新スレッドが 1 フレーム描くと、古い高さぶんしか消されず
            #   前のフレームの残骸（波や本文の行）が画面に残る（実 pty で再現: 最初の印字で
            #   "~~wave~~∴ …" と同じ行に繋がった）。
            with getattr(live, "_lock", None) or contextlib.nullcontext():
                live.console.print(renderable)

    def _stop_live(self) -> None:
        live = self._live
        if live is None:
            return
        self._live = None
        try:
            live.stop()
        finally:
            if self._recording:
                self.console.record = True
                self._recording = False
