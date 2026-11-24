"""`/` コマンド — 表・実装・補完（設計 §4・§11）。

ここは**画面の都合だけ**を扱う。本体の状態（policy/ethics/道具）は子プロセスにあり、
切替は ServeClient.restart() で環境変数として渡し直す（線の向こうからは送れない）。

★ handler(ctx, args) は例外を外に出さない方針。/ コマンド 1 つの失敗で REPL が落ちると
  走行中の会話まで道連れになる。失敗は赤 1 行で言って戻る。
★ HTTP（/status の疎通・/sendlog）は HTTP_TRANSPORT を通す。試験は MockTransport を差す。
"""
from __future__ import annotations

from laps import _envcompat
import json
import os
import re
import socket
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

import httpx
from prompt_toolkit.completion import Completer, Completion
from rich.text import Text

from . import __version__
from . import config
from . import models
from . import theme as T

__all__ = [
    "Command", "COMMANDS", "dispatch", "SlashCompleter",
    "POLICIES", "ETHICS", "SENDLOG_MAX_BYTES", "HTTP_TRANSPORT",
    "backend_root", "probe_backend", "build_sendlog", "install_id", "list_sessions",
]

POLICIES = ("readonly", "workspace", "trusted")
ETHICS = ("strict", "standard", "direct", "raw")

# 受け口（server/ingest.py）の 1 行上限と同じ。超えたら events から削る。
SENDLOG_MAX_BYTES = 256 * 1024
PROBE_TIMEOUT = 3.0
SENDLOG_TIMEOUT = 10.0
RESUME_LIST_N = 10
RESUME_SNIPPET = 60

# 試験が httpx.MockTransport を差す口。None なら本物の線。
HTTP_TRANSPORT: Optional[httpx.BaseTransport] = None

Handler = Callable[[Any, str], None]


# --------------------------------------------------------------------------
# 表示幅（桁）の物差し — 端末版で唯一の数え方
# --------------------------------------------------------------------------
# ★`len()` で数えない。日本語は 1 字 2 桁で、/help の 2 列表が幅 80 で崩れていた（実測）。
# ★East Asian Ambiguous（`·` `●` `…` `→` 等）は **2 と数える**（安全側）。1 と数えて
#   外すと端末が自前で折り返して段が消えるが、2 と数えて外しても早く折れるだけ。
def disp_width(s: str) -> int:
    w = 0
    for ch in str(s):
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
    return w


def _wrap(text: str, width: int, first: str = "", cont: str = "  ") -> str:
    """端末幅で折り、続き行にも段を付ける（`render.wrap_with_gutter` を借りる）。

    ★数え方（曖昧幅を 2）と余白 1 桁の約束を、画面の中で**2 通り持たない**ため
      借りている。読み込み時に import しないのは循環（app → slash）を作らないため。
    """
    from .render import wrap_with_gutter
    return wrap_with_gutter(text, width, first, cont)


def _pad(s: str, width: int) -> str:
    """表示幅で右詰め（`f"{s:<10}"` は日本語で 2 桁ずつずれる）。"""
    return str(s) + " " * max(0, width - disp_width(s))


@dataclass(frozen=True)
class Command:
    """`/name` 1 つ。実体は run(ctx, args)。

    ★属性名を `handler` にしない: test_checkpoint が「`.handler(` の呼び出し＝ツール実行経路」
      と数えて落ちる（道具の実行は agent._exec_tool の 1 点だけ、という門）。ここは道具ではない。
    """
    name: str
    help: str
    run: Handler
    usage: str = ""
    hidden: bool = False    # /help の一覧に出さない（受け付けはする）。別名の /quit だけ

    @property
    def handler(self) -> Handler:
        return self.run


# --------------------------------------------------------------------------
# 画面の部品
# --------------------------------------------------------------------------

def _say(ctx: Any, text: str, style: str = T.S_TEXT) -> None:
    ctx.console.print(Text(text, style=style))


def _bad(ctx: Any, text: str) -> None:
    _say(ctx, text, T.S_RED)


def _dim(ctx: Any, text: str) -> None:
    _say(ctx, text, T.S_DIM)


def _kv(ctx: Any, key: str, value: Any, style: str = T.S_TEXT) -> None:
    t = Text()
    t.append(_pad(key, 10), style=T.S_META)     # ★表示幅で詰める（日本語の鍵でもずれない）
    t.append(str(value), style=style)
    ctx.console.print(t)


def _reprint_header(ctx: Any) -> None:
    """銘（起動画面の 1 行）を出し直す。`/clear` の後は画面の上が全部「前の会話」なので、
    いまの送り先・作業場・実行ゲートがどこにも見えなくなる（実測）。
    ★app は slash を import している。ここでの import は関数の中（読み込み時の循環を避ける）。"""
    try:
        from .app import header_line
    except Exception:
        return
    try:
        ctx.console.print(header_line(ctx))
    except Exception:
        pass


def _settings(ctx: Any) -> dict:
    s = getattr(ctx.client, "settings", None)
    return dict(s) if isinstance(s, dict) else {}


def _set_setting(ctx: Any, key: str, value: Any) -> None:
    """model/backend/workspace は restart 不要（次の send に載る）。settings は属性から
    作られるので属性を書く。"""
    setattr(ctx.client, key, value)


def _client_home(ctx: Any) -> str | None:
    h = _settings(ctx).get("home")
    return str(h) if h else None


def _http(timeout: float) -> httpx.Client:
    return httpx.Client(transport=HTTP_TRANSPORT, timeout=timeout, trust_env=False)


# --------------------------------------------------------------------------
# 疎通
# --------------------------------------------------------------------------

def served_models(backend: str, timeout: float = PROBE_TIMEOUT) -> "list[str] | None":
    """送り先が**実際に出している**モデル名（`/v1/models`）。取れなければ None（＝判断しない）。

    ★画面が言うモデル名は、設定から来ている。llama-server は**知らない名前でも黙って応える**ので、
      設定が古くても走ってしまい、見出しだけが嘘をつく（2026-09-20 に実際に見た:
      環境変数に残っていた昔の名前が、1 か月そのまま見出しに出ていた）。
    """
    b = str(backend or "").strip().rstrip("/")
    if not b:
        return None
    try:
        with _http(timeout) as h:
            r = h.get(b + "/models")
            if r.status_code != 200:
                return None
            data = r.json().get("data")
            names = [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")] if isinstance(data, list) else []
            return names or None
    except Exception:
        return None


def model_mismatch(model: str, served: "list[str] | None") -> "str | None":
    """食い違っていれば、**次の一手つき**の 1 行。合っている・判断できないなら None。"""
    if not served or not model or model in served:
        return None
    if len(served) == 1:
        return (f"モデル {model} は送り先にありません（出しているのは {served[0]}）。"
                f"そのまま応えますが表示が食い違います → /model {served[0]}")
    shown = "・".join(served[:4]) + (" ほか" if len(served) > 4 else "")
    return f"モデル {model} は送り先にありません（出しているのは {shown}）→ /model <名前>"


def backend_root(backend: str) -> str:
    """OpenAI 互換の `/v1` を外した根（llama-server の /health・/slots はここ）。"""
    b = str(backend or "").strip().rstrip("/")
    return re.sub(r"/v1$", "", b)


def probe_backend(backend: str, timeout: float = PROBE_TIMEOUT) -> dict:
    """{"ok": bool, "health": str, "slots": int|None, "processing": int|None, "error": str}。"""
    root = backend_root(backend)
    out: dict[str, Any] = {"ok": False, "health": "", "slots": None, "processing": None, "error": ""}
    try:
        with _http(timeout) as h:
            r = h.get(root + "/health")
            try:
                body = r.json()
            except ValueError:
                body = {}
            status = body.get("status") if isinstance(body, dict) else None
            out["health"] = str(status or r.status_code)
            out["ok"] = r.status_code == 200
            if not out["ok"]:
                out["error"] = f"HTTP {r.status_code}"
                return out
            # /slots は無効化されていることがある（501）。取れなくても疎通は ok のまま。
            try:
                rs = h.get(root + "/slots")
                slots = rs.json() if rs.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                slots = None
            if isinstance(slots, list):
                out["slots"] = len(slots)
                out["processing"] = sum(
                    1 for s in slots if isinstance(s, dict)
                    and (s.get("is_processing") or (isinstance(s.get("state"), int) and s["state"] != 0)))
    except httpx.HTTPError as e:
        out["error"] = f"{type(e).__name__}: {e}".strip(": ")
    except Exception as e:  # URL が変・DNS 等
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# --------------------------------------------------------------------------
# /sendlog の記録
# --------------------------------------------------------------------------

def install_id(path: Path) -> str:
    """~/.config/blackwall/install_id。無ければ uuid4 で作って保持する。"""
    p = Path(path)
    try:
        s = p.read_text(encoding="utf-8").strip()
        if s:
            return s
    except OSError:
        pass
    s = str(uuid.uuid4())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(s + "\n", encoding="utf-8")
    except OSError:
        pass   # 書けなくても送れる（毎回変わるだけ）
    return s


def _host_label() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _redact_tree(obj: Any) -> tuple[Any, int]:
    """JSON の木を歩き、文字列の葉を laps.redact.redact で伏せる。戻り値は (新しい木, 伏せた数)。"""
    from ..redact import redact as _redact
    n = 0

    def walk(x: Any) -> Any:
        nonlocal n
        if isinstance(x, str):
            r = _redact(x)
            n += int(r.count)
            return r.text
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    return walk(obj), n


def build_sendlog(ctx: Any, *, max_bytes: int = SENDLOG_MAX_BYTES) -> tuple[dict, bytes]:
    """§11 のレコードを組み、1 行（末尾 \\n）の bytes と一緒に返す。

    上限を超えたら events を**古い方から**削る（done と直前の事象のほうが診断に効く）。
    events を空にしても超えるなら transcript も古い方から削る。
    """
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    sid = ctx.session or "nosession"
    events = [dict(e) for e in list(getattr(ctx, "events", []) or [])]
    transcript = [dict(m) for m in list(ctx.renderer.transcript or [])]
    rec: dict[str, Any] = {
        "rec": 1,
        "id": f"term-{sid}-{int(time.time())}",
        "ts": ts,
        "client": {
            "install_id": install_id(ctx.install_id_path),
            "app": "blackwall-term",
            "version": __version__,
            "host_label": _host_label(),
        },
        "kind": "sendlog",
        "input": {"text": str(getattr(ctx, "last_input", "") or "")},
        "transcript": transcript,
        "events": events,
        "stop": {"reason": str(getattr(ctx, "last_outcome", "") or "")},
    }

    # ★2026-09-14: **計器を最上位にも載せる。**
    #   counters は done 事象の中には入っていた（実機 0.1.223 で 46 鍵）が、記録の
    #   最上位には無く、読む側（server/report.py）からは「この走行は計器ゼロ」に見えていた。
    #   ＝この現場が何度も踏んだ「書いたが繋いでいない」型。私自身、これで
    #   「門が鳴っていない」と読み違えた（2026-09-14）。
    #   ★events より**先に**置く: 上限超過で events は古い方から削られ、最後には
    #     空にもなる。最上位に写しておけば、その場合でも計器だけは残る。
    for _e in reversed(events):
        if _e.get("type") != "done":
            continue
        _p = _e.get("payload") or _e
        for _k in ("counters", "tool_calls", "unrun", "missing", "changed",
                   "files_written", "outcome", "iters", "secs",
                   "prompt_tokens", "completion_tokens", "tokens_source", "model"):
            if _k in _p and _k not in rec:
                rec[_k] = _p[_k]
        break

    # ★伏字を**送る側で**通す（2026-08-23 レビュー critical）。受け口（ingest）にも濾し器はあるが、
    #   平文の HTTP を LAN に流す前に、こちらでも同梱の laps.redact で値を伏せる。
    #   文字列の葉（本文・道具の入出力・パス）を全部通す。数は rec["redacted"] に残す。
    rec, n_redacted = _redact_tree(rec)
    rec["redacted"] = n_redacted

    def line() -> bytes:
        return (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")

    body = line()
    dropped = 0
    while len(body) > max_bytes and rec["events"]:
        rec["events"].pop(0)
        dropped += 1
        body = line()
    while len(body) > max_bytes and rec["transcript"]:
        rec["transcript"].pop(0)
        body = line()
    if dropped:
        rec["events_dropped"] = dropped
        body = line()
    return rec, body


# --------------------------------------------------------------------------
# セッション一覧
# --------------------------------------------------------------------------

# serve が利用者の発言の手前に付ける印（serve.py: "[アプリからの指示]\n…\n\n[利用者]\n{prompt}"）。
# ★messages.jsonl の user 行は「利用者が打った文」だけではない: この包みに加え、締めの差し戻し
#   （pushback）も user 役で記録される。印の無い行を最後の発言として出すと、一覧に
#   「依頼は物を作ることを求めていますが…」が並ぶ（実機で確認）。
_USER_MARK = "[利用者]\n"


def _user_said(content: str) -> tuple[str, bool]:
    """serve の包みを外す。印があれば (本文, True)、無ければ (そのまま, False)。"""
    i = content.rfind(_USER_MARK)
    if i >= 0:
        return content[i + len(_USER_MARK):], True
    return content, False


TAIL_BYTES = 64 * 1024      # /resume の一覧で読む末尾の量（全文を読むと 1,200 件で 50ms/打鍵）


def _tail_lines(path: Path, nbytes: int = TAIL_BYTES) -> tuple[list[str], bool]:
    """末尾 nbytes 分の行と、「全部読んだか」。★先頭が欠けた 1 行目は捨てる（壊れた JSON）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                whole = False
            else:
                whole = True
            data = f.read()
    except OSError:
        return [], True
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not whole and lines:
        lines = lines[1:]
    return lines, whole


def _scan_user(lines: Iterable[str], n: int) -> str:
    """JSONL の行から「最後の利用者の発言」を選ぶ（印つきを優先）。"""
    last_marked = ""
    last_any = ""
    for ln in lines:
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        if isinstance(m, dict) and m.get("role") == "user" and not m.get("bw_note"):   # bw_note＝ハーネスの添え書き（engine）
            c = m.get("content")
            if not isinstance(c, str):
                c = json.dumps(c, ensure_ascii=False)
            text, marked = _user_said(c)
            last_any = c
            if marked:
                last_marked = text
    s = " ".join((last_marked or last_any).split())
    return s if len(s) <= n else s[:n] + "…"


def _last_user_text(messages_path: Path, n: int = RESUME_SNIPPET) -> str:
    """最後の利用者の発言。

    ★末尾だけ読む。この機体は記録 1,201 件・合計 5.9MB で、全文走査は 1 件あたり 50ms
      掛かっていた（補完は打鍵ごとに呼ぶ）。末尾に user 行が 1 つも無かったときだけ
      全文に落ちるので、結果は全文走査と同じ。"""
    lines, whole = _tail_lines(messages_path)
    s = _scan_user(lines, n)
    if s or whole:
        return s
    try:
        with messages_path.open(encoding="utf-8", errors="replace") as f:
            return _scan_user(list(f), n)
    except OSError:
        return ""


def list_sessions(home: str | None, limit: int = RESUME_LIST_N) -> list[dict]:
    """新しい順（messages.jsonl の更新時刻）に limit 件。LAPS_HOME は client と同じにする。"""
    from laps.session import Session, sessions_dir

    prev = os.environ.get("LAPS_HOME")
    if home:
        _envcompat.setenv("LAPS_HOME", home)
    try:
        root = sessions_dir()
        ids = Session.list_all()
    finally:
        if home:
            if prev is None:
                _envcompat.unsetenv("LAPS_HOME")
            else:
                _envcompat.setenv("LAPS_HOME", prev)
    rows: list[dict] = []
    for sid in ids:
        mp = root / sid / "messages.jsonl"
        try:
            mtime = mp.stat().st_mtime if mp.exists() else (root / sid).stat().st_mtime
        except OSError:
            mtime = 0.0
        rows.append({"id": sid, "mtime": mtime, "last": _last_user_text(mp) if mp.exists() else ""})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


# --------------------------------------------------------------------------
# 各コマンド
# --------------------------------------------------------------------------

# /help の群 — 「いつ使うか」で分ける（20 行の平坦な一覧は、探す手がかりが名前しかない）。
# ★ここに載っていないコマンドは自動で「その他」に落ちる。新設して載せ忘れても消えない。
HELP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("走行中", ("cancel", "expand", "verbose")),
    ("会話", ("clear", "resume")),
    ("接続と設定", ("status", "rc", "model", "backend", "workspace", "policy", "ethics", "tools")),
)
HELP_HEAD_CAP = 24      # これを超える見出し（/policy 等）は 2 段にする
HELP_KEYS = (
    # ★Esc も走行中は中断（app.py の _KeyWatch が単独の ESC を数えている）。
    #   どこにも書いていなかった＝押した人にしか分からない挙動だった。
    "Ctrl-C／Esc 走行中は中断（Ctrl-C 2 回で終了）  Ctrl-D 終了  Alt+Enter 改行",
    "↑↓ 履歴  Ctrl-R 履歴を検索  Tab 補完  起動時の引数は blackwall --help",
    # ★SPEC §7-1: 明るい地の端末では本文が 1.17:1 ＝読めない（実測）。地の色を当てる手段が
    #   無いので第 2 パレットは作らないと決めたが、**逃げ道の存在を伝えないのは行き止まり**。
    #   手段があるのに黙っているのは、手段が無いのと変わらない。
    "背景が明るくて読みづらいときは NO_COLOR=1 blackwall",
    "  色は出ませんが 成功 ✓・失敗 ✗・実行中 ▸ は字で分かれます",
)


def _help_rows() -> list[tuple[str, tuple[Command, ...]]]:
    """(群の名前, コマンド列)。どの群にも入らないものは「その他」へ（登録順のまま）。"""
    placed = {n for _, names in HELP_GROUPS for n in names}
    rows = [(title, tuple(COMMANDS[n] for n in names if n in COMMANDS))
            for title, names in HELP_GROUPS]
    rest = tuple(c for n, c in COMMANDS.items() if n not in placed and not c.hidden)
    if rest:
        rows.append(("その他", rest))
    return rows


def _cmd_help(ctx: Any, args: str) -> None:
    heads = {c.name: f"/{c.name}" + (f" {c.usage}" if c.usage else "")
             for c in COMMANDS.values() if not c.hidden}
    # ★列は「多数派が収まる幅」で決める。`/policy [readonly|workspace|trusted]`（36 桁）に
    #   合わせると、説明の始まる桁が 38 になって右が潰れる。外れ値は 2 段に落とす。
    ws = [disp_width(h) for h in heads.values()]
    fits = [w for w in ws if w <= HELP_HEAD_CAP] or ws
    col = max(fits) + 2
    # ★端末の**実際の**幅で決める（2026-09-02）。旧 `max(40, …)` は、40 桁より狭い端末で
    #   「40 桁なら収まる」と判断して 1 行に詰め、端末が自前で桁 0 まで折り返していた
    #   ——この関数のコメントが「桁 0 に落として折り返すと左端が揃わない」として
    #   避けたはずの形そのもの。狭いときは 2 段に落ちるのが正しい退避先。
    #   下限は 20（それ未満はどう組んでも収まらない。段だけは保つ）。
    width = max(20, int(getattr(ctx.console, "width", 80) or 80))
    first = True
    for title, cmds in _help_rows():
        if not cmds:
            continue
        if not first:
            ctx.console.print("")
        first = False
        ctx.console.print(Text(title, style=T.S_META))
        for c in cmds:
            head = heads[c.name]
            t = Text()
            t.append("  " + _pad(head, col), style=f"bold {T.TEXT}")
            # ★1 行に収まらないなら 2 段にする。桁 0 に落として折り返すと左端が揃わず、
            #   「どこまでが説明か」が読めなくなる（幅 80 で 4 行が崩れていた・実測）。
            if disp_width(head) <= col - 2 and 2 + col + disp_width(c.help) <= width:
                t.append(c.help, style=T.S_META)
                ctx.console.print(t)
            else:
                # ★2 段のときも幅で折る（折らないと結局 Rich が桁 0 に落とす）。
                #   段は見出し 2 桁・説明 4 桁で、続き行はさらに 2 桁下げる。
                for ln in _wrap(head, width, "  ", "    ").split("\n"):
                    ctx.console.print(Text(ln, style=f"bold {T.TEXT}"))
                for ln in _wrap(c.help, width, "    ", "      ").split("\n"):
                    ctx.console.print(Text(ln, style=T.S_META))
    ctx.console.print("")
    for ln in HELP_KEYS:
        # ★キーの行も幅で折る（2026-09-02）。素の 74 桁・71 桁で、70 桁台の端末では
        #   Rich が桁 0 まで折り返し、折り目の空白 2 桁が**行末に残っていた**
        #   （SPEC §6-4「末尾空白を吐かない」）。続き行は段 2 に落とす。
        _dim(ctx, _wrap(ln, width, "", "  "))


# 危険な設定と、その意味（値だけでは何が起きるか分からない）。★ここに無い値は普通の色。
DANGEROUS = {"policy": {"trusted": "すべて実行できます"},
             "ethics": {"raw": "刻みなし"}}


def _cmd_status(ctx: Any, args: str) -> None:
    """★並びは「いま変わるもの」から。版番号は最後（起動から一度も変わらない）。"""
    s = _settings(ctx)
    st = getattr(ctx.client, "status", None) or {}
    backend = str(s.get("backend") or "")

    # 1. 届いているか（実際に叩く）。ここが赤いときは他の行を読む前に直す行。
    pr = probe_backend(backend)
    if pr["ok"]:
        parts = [f"health {pr['health']}"]
        if pr["slots"] is not None:
            parts.append(f"slots {pr['slots']}（処理中 {pr['processing']}）")
        _kv(ctx, "reach", "・".join(parts), T.S_OK)
    else:
        _kv(ctx, "reach", f"届きません: {pr['error'] or pr['health'] or '?'}", T.S_RED)
    # 2. 本体（子プロセス）
    alive = getattr(ctx.client, "alive", None)
    if alive is not None:
        pid = getattr(ctx.client, "pid", None)
        _kv(ctx, "engine", f"pid {pid} 稼働中" if alive else "停止（次の send で立て直します）",
            T.S_OK if alive else T.S_META)
    # 3. 実行ゲートと倫理の刻み（危険な組み合わせは赤で、値の意味を添える）
    for key in ("policy", "ethics"):
        val = str(s.get(key) or st.get(key) or "")
        why = DANGEROUS.get(key, {}).get(val)
        _kv(ctx, key, f"{val}（{why}）" if why else val, T.S_RED if why else T.S_TEXT)
    # 4. どこで・どの続きか
    _kv(ctx, "workspace", s.get("workspace") or "")
    _kv(ctx, "session", ctx.session or "（なし・次の send で作られます）")
    # 5. 送り先とモデル
    model = str(s.get("model") or "")
    bad = model_mismatch(model, served_models(backend) if pr["ok"] else None)
    _kv(ctx, "model", model, T.S_RED if bad else T.S_TEXT)
    if bad:
        from .render import wrap_with_gutter
        ctx.console.print(Text(wrap_with_gutter(bad, ctx.console.width, "          ", "          "), style=T.S_META))
    _kv(ctx, "backend", backend)
    tools = st.get("tools") if isinstance(st.get("tools"), list) else None
    if tools is not None:
        _kv(ctx, "tools", f"{len(tools)} 件（/tools）")
    # 6. 動かないもの（版）と、今回の累計
    _kv(ctx, "version", __version__)
    stats = dict(ctx.stats or {})
    _kv(ctx, "this run",
        f"ターン {int(stats.get('turns', 0)):,}・道具 {int(stats.get('tools', 0)):,}・"
        f"{float(stats.get('secs', 0.0)):,.0f} 秒・{int(stats.get('tokens', 0)):,} トークン")


def _cmd_sendlog(ctx: Any, args: str) -> None:
    url = str((ctx.cfg or {}).get("telemetry") or "").strip()
    if not url:
        _bad(ctx, "telemetry の送り先が設定にありません（config.toml の telemetry）")
        return
    rec, body = build_sendlog(ctx)
    # ★何を送るかを先に言う（会話・道具の入出力・作業場のパス・ホスト名が外へ出る）。
    _kv(ctx, "送る中身",
        f"会話 {len(rec.get('transcript') or [])} 件・事象 {len(rec.get('events') or [])} 件・"
        f"{len(body):,} バイト・伏字 {rec.get('redacted', 0)} 箇所（作業場のパスとホスト名を含む）→ {url}", T.S_DIM)
    try:
        with _http(SENDLOG_TIMEOUT) as h:
            r = h.post(url, content=body, headers={"Content-Type": "application/x-ndjson"})
    except Exception as e:
        _bad(ctx, f"届きません: {type(e).__name__}: {e}")
        return
    try:
        res = r.json()
    except ValueError:
        res = {}
    if not isinstance(res, dict):
        res = {}
    t = Text()
    t.append(f"{rec['id']} → {url} ", style=T.S_META)
    t.append(f"HTTP {r.status_code}", style=T.S_OK if r.status_code == 200 else T.S_RED)
    ctx.console.print(t)
    _kv(ctx, "accepted", res.get("accepted", "?"))
    _kv(ctx, "duplicate", res.get("duplicate", "?"))
    rej = res.get("rejected", "?")
    _kv(ctx, "rejected", rej, T.S_RED if isinstance(rej, int) and rej > 0 else T.S_TEXT)
    if res.get("reasons"):
        _kv(ctx, "reasons", json.dumps(res.get("reasons"), ensure_ascii=False))
    if rec.get("events_dropped"):
        _dim(ctx, f"（{SENDLOG_MAX_BYTES // 1024}KB に収めるため古い事象 {rec['events_dropped']} 件を落とした）")


# --------------------------------------------------------------------------
# /model — Claude API と推論機のモデルを 1 つの一覧で（2026-10-19 オーナー「Windows や Linux 版のアプリから
# API のモデルを自由に切り替えられるようにしてほしい」）。中身は models.py（全画面の F5 と同じ）。
# --------------------------------------------------------------------------

def _cfg(ctx: Any) -> dict:
    return ctx.cfg if isinstance(getattr(ctx, "cfg", None), dict) else {}


def model_listing(ctx: Any) -> "models.Listing":
    """いまの送り先から見た一覧（Claude の中継と推論機の両方）。全画面の F5 も使う。"""
    return models.fetch(_cfg(ctx), str(_settings(ctx).get("backend") or ""), HTTP_TRANSPORT)


def _remember_where(ctx: Any, new: str, old: str) -> str:
    """選んだ送り先を設定に残す（次に開いたときも同じ所へ）。Claude へ移るときは戻り先の推論機も覚える。
    返すのは画面に添える一言（残せなかった・環境変数が勝つ）。"""
    cfg = _cfg(ctx)
    claude = str(cfg.get("claude_backend") or "")
    upd: dict[str, Any] = {"backend": new}
    if models.is_claude(new, claude) and old and not models.is_claude(old, claude):
        upd["server_backend"] = old
    cfg.update(upd)
    path = getattr(ctx, "config_path", None)
    if not path:
        return ""
    try:
        config.update_file(upd, path)
    except (OSError, TypeError) as e:
        return f"（設定に残せませんでした: {e}）"
    if str(os.environ.get("LAPS_BACKEND") or "").strip():
        return "（環境変数 LAPS_BACKEND があるので、次に開いたときはそちらが勝ちます）"
    return ""


def switch_choice(ctx: Any, lst: "models.Listing", c: "models.Choice") -> tuple[bool, str]:
    """`c` に切り替え、送り先も合わせる。(できたか, 画面に出す一言)。全画面の F5 も使う。"""
    if ctx.running_rid:
        return False, "走行中は切り替えられません（/cancel してから）"
    old = str(_settings(ctx).get("backend") or "")
    r = models.switch(lst, c, old, HTTP_TRANSPORT)
    if not r.ok:
        return False, r.message
    note = ""
    if r.backend:
        _set_setting(ctx, "backend", r.backend)
        note = _remember_where(ctx, r.backend, old)
    return True, r.message + note


def _mark(lst: "models.Listing", c: "models.Choice") -> str:
    if not c.active:
        return ""
    if c.kind == lst.where:
        return "← いま"
    return "← 中継で選ばれている" if c.kind == "claude" else "← 推論機に載っている"


def _show_models(ctx: Any, lst: "models.Listing") -> None:
    s = _settings(ctx)
    cur = lst.current()
    where = "Claude API（中継）" if lst.where == "claude" else "推論機"
    _kv(ctx, "送り先", f"{where} {s.get('backend') or ''}")
    if cur:
        now = cur.label
    elif lst.where == "server" and lst.server_stopped:
        now = f"推論機は{lst.stopped_label()}"
    else:
        now = str(s.get("model") or "") + "（一覧から分かりません）"
    _kv(ctx, "いま", now)
    groups = [("claude", "Claude API" + (f"  {lst.spent}" if lst.spent else "")),
              ("server", f"推論機  {lst.server_url}")]
    for kind, title in groups:
        rows = [(i, c) for i, c in enumerate(lst.choices, 1) if c.kind == kind]
        if not rows:
            continue
        ctx.console.print(Text(title, style=f"bold {T.S_TEXT}"))
        if kind == "server" and lst.server_stopped:
            # ★載っているモデルが無い＝GPU は空いている（2026-11-03）。どれにも「← いま」が付かない理由を言う。
            ctx.console.print(Text(f"  {lst.stopped_label()}", style=T.S_META))
        wl = max(disp_width(c.label) for _, c in rows)
        for i, c in rows:
            t = Text()
            t.append(f"  {i:>2}  ", style=T.S_META)
            t.append(_pad(c.label, wl), style=T.S_TEXT if c.ok else T.S_DIM)
            if c.detail:
                t.append(f"  {c.detail}", style=T.S_DIM)
            m = _mark(lst, c)
            if m:
                t.append(f"  {m}", style=T.S_OK if c.kind == lst.where else T.S_META)
            ctx.console.print(t)
    if lst.stopping:
        _dim(ctx, "推論機を止めている途中です（GPU が空くまで数十秒）")
    elif lst.switching:
        _dim(ctx, f"推論機は {lst.switching} を{'起動' if lst.server_stopped else 'へ入れ替え'}している途中です（1〜3 分）")
    elif lst.switch_note:
        _dim(ctx, f"推論機の最後の操作: {lst.switch_note}")
    for n in lst.notes:
        _dim(ctx, n)
    if lst.choices:
        _dim(ctx, "/model <番号か名前> で切り替え（Claude なら送り先も中継へ・推論機のモデルなら推論機へ）"
             + ("。止まっている推論機に選ぶと起動します" if lst.server_stopped and not lst.lease_stopped else ""))
    if lst.stoppable and lst.lease_stopped and not lst.stopping:
        _dim(ctx, "/model stop で、GPU の仕事が終わった後も推論機を戻さないようにできます")
    elif lst.stoppable and not lst.server_stopped and not lst.stopping:
        _dim(ctx, "/model stop で推論機を止めて GPU を空ける（ほかのセッションの GPU の仕事に回せます）")


STOP_WORDS = ("stop",)
STOP_FORCE = ("--force", "-f")
STOP_YES = ("--yes", "-y")


def _confirm(ctx: Any, question: str) -> bool:
    """y/N で聞く（2026-11-03・/model stop）。全画面は小窓（ctx.confirm_fn）、行の形の対話は 1 行読む。
    ★聞く相手がいない（-p・パイプ）ときは聞かずに進める＝コマンドそのものが明示の依頼。"""
    fn = getattr(ctx, "confirm_fn", None)
    if callable(fn):
        try:
            return bool(fn(question))
        except Exception:  # noqa: BLE001 — 聞けなければ止めない側へ
            return False
    if not getattr(ctx, "interactive", False):
        return True
    reader = getattr(ctx, "ask_fn", None) or input
    try:
        ans = str(reader(f"{question} [y/N] ") or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    return ans in ("y", "yes", "はい")


def stop_server(ctx: Any, lst: "models.Listing", force: bool = False) -> tuple[bool, str]:
    """推論機を止めて GPU を空ける（モデル切り替えの口 の /v1/stop）。(できたか, 画面に出す一言)。全画面も使う。"""
    if ctx.running_rid:
        return False, "走行中は止められません（/cancel してから）"
    r = models.stop(lst, force=force, transport=HTTP_TRANSPORT)
    msg = r.message
    if r.ok and r.pending and lst.where == "server":
        msg += "。止まっている間、推論機へ送る依頼は届きません（Claude に送るなら /model <Claude のモデル>）"
    return r.ok, msg


def _cmd_model_stop(ctx: Any, lst: "models.Listing", rest: list[str]) -> None:
    bad = [w for w in rest if w not in STOP_FORCE + STOP_YES]
    if bad:
        _bad(ctx, f"/model stop に分からない引数: {' '.join(bad)}（使えるのは --force・--yes）")
        return
    force = any(w in STOP_FORCE for w in rest)
    # ★聞く前に、止められない理由が先に分かるもの（止める口が無い・もう止まっている）は聞かずに答える。
    if lst.server_read and lst.stoppable and (not lst.server_stopped or lst.lease_stopped) and not lst.stopping \
            and not lst.switching and not ctx.running_rid and not any(w in STOP_YES for w in rest):
        cur = next((c for c in lst.choices if c.kind == "server" and c.active), None)
        what = cur.label if cur else "推論機"
        q = (f"GPU の仕事が終わった後も {lst.lease_stopped} を戻さないようにしますか（{lst.server_url}）"
             if lst.server_stopped else f"{what} を止めて GPU を空けますか（{lst.server_url}）")
        if not _confirm(ctx, q):
            _dim(ctx, "止めませんでした")
            return
    ok, msg = stop_server(ctx, lst, force)
    (_say if ok else _bad)(ctx, msg)


def _cmd_model(ctx: Any, args: str) -> None:
    lst = model_listing(ctx)
    words = args.split()
    if words and words[0].lower() in STOP_WORDS:
        _cmd_model_stop(ctx, lst, [w.lower() for w in words[1:]])
        return
    if not args:
        _show_models(ctx, lst)
        return
    c, cands = models.pick(lst, args)
    if c is None and cands:
        _bad(ctx, f"{args} に当たる物が {len(cands)} つあります: " + "・".join(x.id for x in cands[:6])
             + "（番号か全部の名前で）")
        return
    if c is None and args.strip().isdigit():
        _bad(ctx, f"番号 {args.strip()} は一覧にありません（/model で一覧）")
        return
    if c is None:
        # 一覧に無い名前＝従来どおり、本体へ送るモデル名だけを変える（llama-swap の名前など）。
        _set_setting(ctx, "model", args)
        _say(ctx, f"model → {args}（次の send から）")
        return
    ok, msg = switch_choice(ctx, lst, c)
    (_say if ok else _bad)(ctx, msg)


def _cmd_backend(ctx: Any, args: str) -> None:
    if not args:
        b = str(_settings(ctx).get("backend") or "")
        _kv(ctx, "backend", b + ("（Claude API の中継）" if models.is_claude(b, str(_cfg(ctx).get("claude_backend") or "")) else ""))
        return
    word = args.strip().lower()
    if word in ("claude", "server"):
        # ★送り先だけ切り替える（モデルは向こうでいま選ばれている物のまま）。モデルも選ぶなら /model。
        cfg = _cfg(ctx)
        old = str(_settings(ctx).get("backend") or "")
        target = str(cfg.get("claude_backend") or "").strip() if word == "claude" else models.server_url(cfg, old)
        if not target:
            _bad(ctx, "設定に claude_backend（Claude の中継の住所）がありません")
            return
        if ctx.running_rid:
            _bad(ctx, "走行中は切り替えられません（/cancel してから）")
            return
        _set_setting(ctx, "backend", target)
        note = _remember_where(ctx, target, old)
        where = "Claude API（中継・費用は共有の API の枠から）" if word == "claude" else "推論機"
        _say(ctx, f"backend → {target}（{where}・次の send から。モデルは /model）{note}")
        return
    u = urlparse(args)
    if u.scheme not in ("http", "https") or not u.netloc:
        _bad(ctx, f"URL の形ではありません: {args}（例: http://192.0.2.160:8741/v1）")
        return
    _set_setting(ctx, "backend", args)
    _say(ctx, f"backend → {args}（次の send から。疎通は /status）")


def _cmd_rc(ctx: Any, args: str) -> None:
    """繋がっているかと、この会話をほかの端末で開く QR（Claude の /remote-control と同じ・2026-10-31）。"""
    from .. import qr, relayauth
    info = relayauth.rc_info(str(getattr(ctx, "session", "") or ""))
    _say(ctx, f"{'● 接続中' if info.get('connected') else '○ 未接続'} · {info.get('name') or ''}")
    if not info.get("connected") and info.get("error"):
        _say(ctx, str(info["error"]), T.S_DIM)
    rows = info.get("qr")
    if rows:
        ctx.console.print(Text(qr.halfblocks(rows)))
        _say(ctx, "スマホで読むと L.A.P.S で開きます", T.S_DIM)
        _say(ctx, str(info.get("web") or ""), T.S_DIM)


def _cmd_workspace(ctx: Any, args: str) -> None:
    if not args:
        _kv(ctx, "workspace", _settings(ctx).get("workspace") or "")
        return
    p = Path(args).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    _set_setting(ctx, "workspace", str(p))
    note = "" if p.is_dir() else "（まだ無い。次の send で作られる）"
    _say(ctx, f"workspace → {p}{note}")


def _switch_restart(ctx: Any, key: str, value: str, allowed: Iterable[str]) -> None:
    allowed = tuple(allowed)
    if not value:
        _kv(ctx, key, _settings(ctx).get(key) or "")
        _dim(ctx, f"/{key} <{'|'.join(allowed)}> で切替（子を再起動）")
        return
    if value not in allowed:
        _bad(ctx, f"{key} は {' / '.join(allowed)} のどれか: {value}")
        return
    if ctx.running_rid:
        _bad(ctx, "走行中は切替えられません（/cancel してから）")
        return
    try:
        ctx.client.restart(**{key: value})
    except Exception as e:
        _bad(ctx, f"再起動に失敗: {e}")
        return
    _say(ctx, f"{key} → {value}。切替えました（子を再起動）")


def _cmd_policy(ctx: Any, args: str) -> None:
    _switch_restart(ctx, "policy", args, POLICIES)


def _cmd_ethics(ctx: Any, args: str) -> None:
    _switch_restart(ctx, "ethics", args, ETHICS)


def _columns(names: list[str], width: int, indent: int = 2, gap: int = 2) -> list[str]:
    """幅から列数を決めて段 2 の表にする。★1 本の長い行にすると桁 0 に落ちて折り返す。"""
    if not names:
        return []
    cell = max(disp_width(n) for n in names) + gap
    cols = max(1, (width - indent) // cell)
    rows = (len(names) + cols - 1) // cols
    out = []
    for r in range(rows):
        # 縦に読める並び（左の列を上から下へ）。横並びは目が行を跨いで戻る
        items = [names[r + c * rows] for c in range(cols) if r + c * rows < len(names)]
        out.append((" " * indent + "".join(_pad(x, cell) for x in items)).rstrip())
    return out


def _cmd_tools(ctx: Any, args: str) -> None:
    st = getattr(ctx.client, "status", None) or {}
    names = [str(n) for n in (st.get("tools") or [])] if isinstance(st.get("tools"), list) else []
    if not names:
        # ★空状態には次の一手（ui-ux-design §2）。
        _dim(ctx, "（本体が道具を告げていません）")
        _dim(ctx, "  → /status で本体が動いているかを見てください。")
        return
    _say(ctx, f"{len(names)} 件（toolset {st.get('toolset', '?')}・native {st.get('native_tools', '?')}）",
         T.S_META)
    # ★実際の幅で列を組む（`/help` と同じ理由・2026-09-02）。旧 `max(40, …)` は
    #   狭い端末で 40 桁の表を作り、端末が折り返して列が崩れていた。
    width = max(20, int(getattr(ctx.console, "width", 80) or 80))
    for ln in _columns(names, width):
        _say(ctx, ln)


def _forget_tools(renderer: Any) -> None:
    """直前の束（/expand の材料）も捨てる。★捨てないと `/clear` の直後の `/expand` が
    前の会話のコマンドと出力を全文で出す（実測）。renderer 側に forget_tools が
    入ったらそれを使い、無い間はここで束を空にする（＝どちらでも必ず消える）。"""
    fn = getattr(renderer, "forget_tools", None)
    if callable(fn):
        fn()
        return
    if isinstance(getattr(renderer, "_bundle", None), list):
        renderer._bundle.clear()


def _cmd_clear(ctx: Any, args: str) -> None:
    ctx.session = None
    try:
        ctx.renderer.transcript.clear()
    except AttributeError:
        ctx.renderer.transcript = []
    _forget_tools(ctx.renderer)
    ev = getattr(ctx, "events", None)
    if ev is not None:
        ev.clear()
    _say(ctx, "新しい会話（ここより上は本体に渡りません）", T.S_META)
    _dim(ctx, "  → 続きから始めるなら /resume <id>")
    _reprint_header(ctx)


def _cmd_resume(ctx: Any, args: str) -> None:
    if args:
        from ..session import is_valid_session_id
        if not is_valid_session_id(args):
            _bad(ctx, f"session id が不正です: {args!r}（一覧の id をそのまま渡してください）")
            return
        ctx.session = args
        _say(ctx, f"session → {args}（次の send から続き）")
        return
    try:
        rows = list_sessions(_client_home(ctx))
    except Exception as e:
        _bad(ctx, f"一覧を読めません: {e}")
        return
    if not rows:
        # ★空状態には「次の一手」を置く（ui-ux-design §2・最重要条項）。
        #   「まだありません」だけだと、壊れているのか、これからなのか分からない。
        _dim(ctx, "(セッションはまだありません)")
        _dim(ctx, "  → 何か話しかけると最初のセッションができます。"
                  "続きから始めるときは /resume <id>。")
        return
    for r in rows:
        t = Text()
        when = time.strftime("%m-%d %H:%M", time.localtime(r["mtime"])) if r["mtime"] else "     "
        mark = "*" if r["id"] == ctx.session else " "
        t.append(f"{mark} {r['id']}  ", style=f"bold {T.TEXT}")
        t.append(when + "  ", style=T.S_DIM)
        t.append(r["last"], style=T.S_META)
        # 1 行 1 セッション（日本語は 1 字 2 列なので 60 字が折り返して 3 行になった。切る）
        ctx.console.print(t, no_wrap=True, overflow="ellipsis")
    _dim(ctx, "/resume <id> で続ける")


def _cmd_expand(ctx: Any, args: str) -> None:
    if args:
        # ★黙って捨てない。捨てると「効かない引数」を利用者が正しいと思い込む。
        _dim(ctx, f"/expand に引数はありません（{args} は無視）。直前の束を全文で出します。")
    ctx.renderer.expand_last()


def _cmd_verbose(ctx: Any, args: str) -> None:
    on = not bool(getattr(ctx.renderer, "verbose", False))
    ctx.renderer.set_verbose(on)
    _say(ctx, f"verbose {'on（思考を全文出す）' if on else 'off'}", T.S_META)



# ── /trace — この走行で**何を実行したか**を通しで見る ──────────────────────────
# ★なぜ要るか（オーナー指摘 2026-09-13）
#   「内部思考とか、何を実行したのかとかが圧縮で見えなくなってしまっているのでは？」
#   ★記録は消えていない（`context_budget.py` 冒頭: 「送信するビューだけを削る。
#     記録（session.messages）には触らない」）。消えているのは**画面**のほう:
#       ・`/expand` は**直前のターンの束だけ**（`start_turn` で前の束を捨てる）
#       ・`/verbose` は思考を出すが、**次のターンから**であって過去は出ない
#     ＝「何を実行したのか」を後から通しで見る手段が無かった。ここを埋める。
#   ★圧縮が奪うのは**モデルの視界**で、利用者の視界とは別。両方を混ぜて説明しない。

def _ev(e: dict, key: str, default: Any = None) -> Any:
    """事象の欄を取る。★**線の形と記録の形が違う**（この現場が繰り返す食い違い）。

    線（client が受け取る dict）は欄が**最上位**。
    記録（`events.jsonl`）は `payload` の中（`seq`/`ts`/`type` だけが最上位）。
    どちらでも読めるようにする。
    """
    if key in e:
        return e[key]
    pay = e.get("payload")
    if isinstance(pay, dict) and key in pay:
        return pay[key]
    return default


def _trace_source(ctx: Any) -> tuple[list[dict], str]:
    """(事象の列, どこから読んだか)。記録が読めればそちら（全件）、無ければ受け取った分。"""
    sid = str(getattr(ctx, "session", "") or "")
    if sid:
        try:
            from ..session import sessions_dir
            f = sessions_dir() / sid / "events.jsonl"
            if f.exists():
                out: list[dict] = []
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        out.append(obj)
                if out:
                    return out, f"記録 {f}"
        except Exception:
            pass
    # ★受け取った分は deque(maxlen=200) なので**古い方が落ちている**ことがある。
    #   黙って「全部です」と見せない（下の見出しで明示する）。
    return [dict(e) for e in list(getattr(ctx, "events", []) or [])], "受け取った分（直近 200 件・古い方は落ちています）"


def _arg_label(inp: Any, limit: int = 46) -> str:
    """道具の引数を 1 行に。最初の文字列の値（パスやコマンド）を選ぶ。"""
    if not isinstance(inp, dict) or not inp:
        return ""
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            t = " ".join(v.split())
            return t if len(t) <= limit else t[:limit - 1] + "…"
    return ""


def _trace_steps(events: list[dict]) -> list[dict]:
    """道具の呼び出しを呼び出し→結果で突き合わせ、古い順に並べる。"""
    steps: list[dict] = []
    by_call: dict[Any, dict] = {}
    for e in events:
        t = str(e.get("type") or "")
        if t == "tool_call":
            st = {"name": str(_ev(e, "name") or "?"), "input": _ev(e, "input") or {},
                  "call": _ev(e, "call"), "result": None, "ok": None}
            steps.append(st)
            if st["call"] is not None:
                by_call[st["call"]] = st
        elif t == "tool_result":
            call = _ev(e, "call")
            st = by_call.get(call) if call is not None else None
            if st is None:                       # call が取れない＝同名の最古に当てる
                nm = str(_ev(e, "name") or "")
                st = next((x for x in steps if x["result"] is None and x["name"] == nm), None)
            if st is None:
                st = next((x for x in reversed(steps) if x["result"] is None), None)
            if st is not None:
                st["result"] = str(_ev(e, "result") or "")
                st["ok"] = bool(_ev(e, "ok", True))
    return steps


def _trace_counts(events: list[dict]) -> dict:
    """圧縮の回数など、走行の要約（記録にあるものだけ。無い物は 0 と言わない）。"""
    out: dict[str, Any] = {}
    for e in events:
        if str(e.get("type") or "") in ("result", "done"):
            c = _ev(e, "counters")
            if isinstance(c, dict):
                out.update({k: v for k, v in c.items() if isinstance(v, (int, float))})
            for k in ("compactions", "iters", "outcome"):
                v = _ev(e, k)
                if v is not None:
                    out[k] = v
    return out


def _cmd_trace(ctx: Any, args: str) -> None:
    events, src = _trace_source(ctx)
    if not events:
        _dim(ctx, "まだ記録がありません")
        _dim(ctx, "  → 何か走らせたあとに /trace を打つと、実行した道具が通しで出ます。")
        return
    steps = _trace_steps(events)
    info = _trace_counts(events)
    arg = args.strip()

    if arg and arg != "all":
        try:
            n = int(arg)
        except ValueError:
            _bad(ctx, f"/trace の引数は番号か all です（{arg} は読めません）")
            return
        if not (1 <= n <= len(steps)):
            _bad(ctx, f"#{n} はありません（1〜{len(steps)}）")
            return
        st = steps[n - 1]
        mark = T.MARK_OK if st["ok"] is not False else T.MARK_BAD
        _say(ctx, f"{mark} #{n} {st['name']}", T.S_TEXT)
        _say(ctx, "  引数:", T.S_META)
        try:
            body = json.dumps(st["input"], ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            body = str(st["input"])
        for ln in body.splitlines():
            _say(ctx, "    " + ln, T.S_DIM)
        _say(ctx, "  結果:", T.S_META)
        res = st["result"]
        if res is None:
            _dim(ctx, "    （結果が記録にありません＝走行中に終わった／記録が欠けた）")
            return
        lines = res.splitlines()
        if len(lines) > 200:
            for ln in lines[:100]:
                _say(ctx, "    " + ln, T.S_DIM)
            _dim(ctx, f"    … 中略（全 {len(lines)} 行のうち中央 {len(lines) - 200} 行）")
            for ln in lines[-100:]:
                _say(ctx, "    " + ln, T.S_DIM)
        else:
            for ln in lines:
                _say(ctx, "    " + ln, T.S_DIM)
        return

    head = f"{src}／事象 {len(events):,} 件・道具 {len(steps)} 回"
    comp = info.get("ctx_budget_fired") or info.get("compactions")
    if isinstance(comp, (int, float)) and comp:
        # ★圧縮が奪うのは**モデルの視界**。記録と画面は無関係だと明示する。
        head += f"・圧縮 {int(comp)} 回（モデルの視界を削った回数。記録は削られていません）"
    _say(ctx, head, T.S_META)
    if not steps:
        _dim(ctx, "  道具は使われていません（読んだだけ／答えただけの走行）")
        return
    for i, st in enumerate(steps, 1):
        mark = T.MARK_OK if st["ok"] is not False else T.MARK_BAD
        lbl = _arg_label(st["input"])
        res = st["result"]
        if res is None:
            tail = "（結果なし）"
        else:
            n_lines = len(res.splitlines())
            tail = f"{n_lines:,} 行" if n_lines > 1 else f"{len(res):,} 字"
            if st["ok"] is False:
                first = next((x for x in res.splitlines() if x.strip()), "")
                tail = f"失敗: {first[:60]}"
        t = Text()
        t.append(f"  {mark} #{i:<3}", style=T.S_MARK_OK if st["ok"] is not False else T.S_MARK_BAD)
        t.append(f"{st['name']}", style=T.S_TEXT)
        if lbl:
            t.append(f"({lbl})", style=T.S_DIM)
        t.append(f"  {tail}", style=T.S_BAD if st["ok"] is False else T.S_META)
        ctx.console.print(t)
    _dim(ctx, f"  → /trace <番号> で 1 件を全文（1〜{len(steps)}）")


def _cmd_cancel(ctx: Any, args: str) -> None:
    rid = ctx.running_rid
    if not rid:
        _dim(ctx, "走行中ではありません")
        _dim(ctx, "  → 止めるものがない状態です。走らせるには、やりたいことをそのまま書いてください。")
        return
    try:
        ctx.client.cancel(rid)
    except Exception as e:
        _bad(ctx, f"cancel を送れません: {e}")
        return
    _say(ctx, f"中断を要求しました（{rid}・done を待ちます）", T.S_META)


def _cmd_exit(ctx: Any, args: str) -> None:
    ctx.exit_requested = True


# --------------------------------------------------------------------------
# /update — 公開版には自動更新の配信先が無い
# --------------------------------------------------------------------------

def _cmd_update(ctx: Any, args: str) -> None:
    """公開版は自動更新を持たない（配信先はそれぞれの利用者が持つ物なので）。入れ直し方だけ出す。"""
    _dim(ctx, "この版には自動更新がありません。ソースから入れた場合は git pull の後に `pip install -e src` で入れ直してください。")


COMMANDS: dict[str, Command] = {}


def _register(name: str, help_: str, handler: Handler, usage: str = "",
              hidden: bool = False) -> None:
    COMMANDS[name] = Command(name, help_, handler, usage, hidden)


_register("help", "この一覧", _cmd_help)
_register("status", "疎通・本体・設定・作業場・今回の累計", _cmd_status)
_register("sendlog", "今のセッションの記録を受け口へ 1 レコードで送る", _cmd_sendlog)
_register("model", "Claude API と推論機のモデルの一覧／切替（送り先も合わせる・止まっている推論機は起動）。"
          "stop で推論機を止めて GPU を空ける（--force で生成中でも）", _cmd_model, "[番号|名前|stop]")
_register("backend", "送り先の表示／切替（claude・server・URL。疎通は /status）", _cmd_backend, "[claude|server|URL]")
_register("rc", "ほかの端末で開く QR と、繋がっているか", _cmd_rc)
_register("remote-control", "ほかの端末で開く QR と、繋がっているか", _cmd_rc, hidden=True)
_register("workspace", "作業場の表示／切替（道具はここにしか書けない）", _cmd_workspace, "[パス]")
_register("policy", "実行ゲートの表示／切替（子を再起動）", _cmd_policy, "[readonly|workspace|trusted]")
_register("ethics", "倫理の刻みの表示／切替（子を再起動）", _cmd_ethics, "[strict|standard|direct|raw]")
_register("tools", "本体が告げた道具の一覧", _cmd_tools)
_register("clear", "会話を空に（新しいセッション）", _cmd_clear)
_register("resume", "セッションの一覧／再開", _cmd_resume, "[id]")
_register("expand", "直前の道具の束を全文で", _cmd_expand)
_register("verbose", "思考の全文表示をトグル", _cmd_verbose)
_register("trace", "この走行で実行した道具を通しで（圧縮とは無関係に記録から）",
          _cmd_trace, "[番号|all]")
_register("cancel", "走行中の send を中断", _cmd_cancel)
_register("update", "入れ直し方を表示する（公開版には自動更新はない）", _cmd_update)
_register("exit", "終了", _cmd_exit)
_register("quit", "終了（/exit と同じ）", _cmd_exit, hidden=True)


def dispatch(ctx: Any, line: str) -> bool:
    """`/name args` を実行する。知らない名前なら赤で言う。実行したら True。"""
    s = str(line or "").strip()
    if not s.startswith("/"):
        return False
    parts = s[1:].split(None, 1)
    name = parts[0].strip().lower() if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""
    if not name:
        _cmd_help(ctx, "")
        return True
    cmd = COMMANDS.get(name)
    if cmd is None:
        # 前方一致で 1 つに決まれば通す（/st → /status）。曖昧なら候補を出す。
        cands = [c for c in COMMANDS if c.startswith(name)]
        if len(cands) == 1:
            cmd = COMMANDS[cands[0]]
        else:
            hint = f"（候補: {' '.join('/' + c for c in cands)}）" if cands else "（/help で一覧）"
            _bad(ctx, f"知らないコマンド: /{name} {hint}")
            return True
    try:
        cmd.run(ctx, args)
    except KeyboardInterrupt:
        _dim(ctx, "(中断)")
    except Exception as e:
        _bad(ctx, f"/{cmd.name} が失敗: {type(e).__name__}: {e}")
    return True


# --------------------------------------------------------------------------
# 補完
# --------------------------------------------------------------------------

SESSION_CACHE_TTL = 3.0     # 補完のためのセッション一覧の消費期限（秒）


class SlashCompleter(Completer):
    """`/` 直後は全コマンド。引数の位置では policy/ethics の値・セッション id・ready の model。

    ★補完は**打鍵ごとに**呼ばれる。`/resume ` の後は 1 文字ごとに記録を全部読んでいて、
      この機体（1,201 件）で 50ms 掛かっていた（実測）。数秒だけ覚える。
      `/resume` の一覧（`_cmd_resume`）は覚えない＝そちらは常に最新。"""

    def __init__(self, ctx: Any = None) -> None:
        self.ctx = ctx
        self._ids: list[str] = []
        self._ids_at = 0.0

    def _session_ids(self) -> list[str]:
        now = time.monotonic()
        if now - self._ids_at < SESSION_CACHE_TTL:
            return self._ids
        try:
            self._ids = [r["id"] for r in list_sessions(_client_home(self.ctx))]
        except Exception:
            self._ids = []
        self._ids_at = now
        return self._ids

    def _arg_candidates(self, name: str) -> list[str]:
        if name == "policy":
            return list(POLICIES)
        if name == "ethics":
            return list(ETHICS)
        ctx = self.ctx
        if ctx is None:
            return []
        if name == "model":
            s = _settings(ctx)
            st = getattr(ctx.client, "status", None) or {}
            names = [str(x) for x in (s.get("model"), st.get("model")) if x] + ["stop"]
            return list(dict.fromkeys(names))
        if name == "resume":
            return list(self._session_ids())
        if name == "backend":
            return ["claude", "server"]
        return []

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        body = text[1:]
        if " " not in body:
            for c in COMMANDS.values():
                if c.name.startswith(body.lower()):
                    yield Completion(c.name, start_position=-len(body), display=f"/{c.name}",
                                     display_meta=c.help)
            return
        name, _, rest = body.partition(" ")
        if " " in rest.strip():
            return
        word = rest.lstrip()
        for cand in self._arg_candidates(name.lower()):
            if cand.startswith(word):
                yield Completion(cand, start_position=-len(word))
