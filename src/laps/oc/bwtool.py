"""`bwtool` の支え: 起動の台本を道具の置き場に置く・呼んだ記録を数える（本体は laps/cli_bwtool.py）。

★2026-10-23 オーナー「Blackwallエージェントのツールをモデルがスクリプトを書く際にそこに組み込めるようにするのはどうでしょう？」
  →「作ってください…ローカルLLMが使うことを意識して」。道具の呼び出し（1 手番に 1〜数個）では、同じ道具を 20 回使うと手番も 20 回
  かかる（ローカルの推論機では 1 手番 11 秒前後＋読み込み）。モデルが書くスクリプトの中から道具を関数のように呼べれば、まとめた作業を
  1 回で走らせ、最後の結果だけを読める（Anthropic の Programmatic Tool Calling・Code Mode と同じ考え）。
  ★先例: 知識サーバー は 2026-09-12 に道具ではなく `umbra` コマンドとして置いた（同じモデル・8 課題で、道具 20 本の中核 2〜3/8 に対し
  薄い中核 6〜7/8・弱いモデルは JSON の引数を埋めるより shell を書く方が得意）。

★起動の台本を道具の置き場（toolenv.tools_dir()/blackwall/）に置く理由: Bash の子の PATH に必ず入るのはここ（toolenv.child_path）。
  pip の console script（同梱の Python の Scripts など）は、Windows のアプリでは Bash（cmd.exe）の PATH に入らない。
  子の PYTHONPATH は起動元が書き換える前の値に戻される（toolenv.child_env・2026-10-22）ので、本体を読めるパスを台本の中で明示する。
"""
from __future__ import annotations

from .. import paths as _lp
from laps import _envcompat
import json
import os
import sys
import time
import uuid
from pathlib import Path

#: スクリプトから呼べる道具（シェルで代わりがきかない物だけ）。★入れない物: Grep・Glob・Edit・Write（シェルでできる）・
#: Desktop・App・VM（人の画面・承認が要る）・Task（推論機の枠は 1 つ・子をスクリプトから起こさない）・TodoWrite・AskUserQuestion（会話の道具）。
SCRIPT_TOOLS = ("WebSearch", "WebFetch", "Read", "Listen", "Doc", "Construct", "Api")

#: `bwtool list` の一行（用途と呼び方の例）。★道具の長い説明の 1 行目は言語も書き出しもばらばら（WebFetch は
#: 「- Fetches…」）で、小さいモデルが読む一覧には向かないので、ここで揃える。
SCRIPT_SUMMARY = {
    "WebSearch": ('search the web; returns title, URL and snippet per hit', 'bwtool WebSearch query="ffmpeg trim last 10 seconds" n=5'),
    "WebFetch": ("read a web page or file by URL as text", "bwtool WebFetch url=https://example.com/page"),
    "Read": ("a text file or folder; for a binary file its structure, bytes (hex=true byte_offset=N) or text inside it (strings=true)",
             "bwtool Read file_path=/abs/path/data.bin strings=true"),
    "Listen": ("transcribe speech in audio files (several per call)", 'bwtool Listen paths=\'["/abs/a.wav","/abs/b.mp3"]\''),
    "Doc": ("read or create Word, PowerPoint, Excel and PDF files", "bwtool Doc path=/abs/path/report.pdf"),
    "Construct": ("recall your own memory of earlier work on this machine", 'bwtool Construct query="libGL.so.1 ImportError"'),
    "Api": ("call a web API (REST/JSON) with saved API keys", "bwtool Api method=GET url=https://api.example.com/v1/items"),
}


def enabled() -> bool:
    return (os.environ.get("LAPS_BWTOOL") or "1").strip().lower() not in ("0", "off", "false", "no")


def home() -> Path:
    return Path(_lp.home_dir())


def log_path() -> Path:
    return home() / "bwtool.jsonl"


#: 呼んだ記録に付ける、この走行（のプロセス）の印。Bash の子は親の環境を受け継ぐので、台本の bwtool がこれを記録に書く。
#: ★2026-10-25 敵対レビュー: 数えは時間の窓だけで決めていて、同じ機械で同時に走る別の会話（ゲートウェイの 2 つの会話・アプリと
#:   Linux 版）の呼び出しまで「この Bash の中で呼んだ」と数えていた。印が同じ記録だけを数える。
TAG_ENV = "LAPS_BWTOOL_TAG"
#: 記録がこれより大きくなったら .1 へ回す（消さないと際限なく育つ）
_LOG_MAX_BYTES = 4 * 1024 * 1024


def run_tag() -> str:
    """この走行の印（無ければ作って環境に置く）。既にある印（親の Blackwall から受け継いだ物）はそのまま使う。"""
    t = (os.environ.get(TAG_ENV) or "").strip()
    if not t:
        t = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        _envcompat.setenv(TAG_ENV, t)
    return t


def _launcher_dir() -> Path:
    from .toolenv import tools_dir
    return tools_dir() / "blackwall"


def _pythonpath() -> str:
    """台本が本体を読むためのパス: 本体の包みの親 ＋ いまの PYTHONPATH（起動元が足した物を含む）。"""
    import laps
    parts = [str(Path(laps.__file__).resolve().parent.parent)]
    parts += [p for p in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if p]
    return os.pathsep.join(dict.fromkeys(parts))


def ensure_launcher() -> Path | None:
    """道具の置き場に `bwtool`（Linux・mac）と `bwtool.cmd`（Windows）を置く。中身が同じなら書かない。置いた場所（失敗は None）。"""
    if not enabled():
        return None
    run_tag()
    try:
        d = _launcher_dir()
        d.mkdir(parents=True, exist_ok=True)
        py, pp = sys.executable, _pythonpath()
        if sys.platform == "win32":
            f = d / "bwtool.cmd"
            body = (f'@echo off\r\nset "PYTHONPATH={pp}"\r\nset "PYTHONIOENCODING=utf-8"\r\n'
                    f'"{py}" -m laps.cli_bwtool %*\r\n')
        else:
            f = d / "bwtool"
            q = lambda s: "'" + s.replace("'", "'\\''") + "'"
            body = (f"#!/bin/sh\n# Blackwall の道具をスクリプトから呼ぶ口（laps/cli_bwtool.py）。走行の始めに書き直す。\n"
                    f"PYTHONPATH={q(pp)} exec {q(py)} -m laps.cli_bwtool \"$@\"\n")
        try:
            same = f.read_text(encoding="utf-8") == body
        except OSError:
            same = False
        if not same:
            f.write_text(body, encoding="utf-8", newline="")
        if sys.platform != "win32":
            f.chmod(0o755)
        return f
    except Exception:
        return None


def record(tool: str, ok: bool, secs: float) -> None:
    """呼んだ記録を 1 行（走行の側が数えて画面に出す）。書けなくても道具は止めない。"""
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.stat().st_size > _LOG_MAX_BYTES:
                os.replace(p, p.with_name(p.name + ".1"))
        except OSError:
            pass
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 3), "tool": tool, "ok": bool(ok), "secs": round(secs, 2),
                                 "cwd": os.getcwd(), "pid": os.getpid(), "ppid": os.getppid(),
                                 "tag": os.environ.get(TAG_ENV, "")}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _tail_since(p: Path, t0: float, cap: int = 16 * 1024 * 1024) -> list[str]:
    """記録の末尾から、t0 より前の行が出るまで（または cap バイトまで）を行で返す。
    ★旧は末尾 200 行だけを見ていて、1 回の Bash で 200 回より多く呼ぶ台本の数えが黙って 200 で止まっていた。"""
    try:
        size = p.stat().st_size
    except OSError:
        return []
    want = 64 * 1024
    while True:
        n = min(size, want, cap)
        try:
            with p.open("rb") as fh:
                fh.seek(size - n)
                lines = fh.read(n).decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        if n < size:
            lines = lines[1:]                       # 切った所は行の途中
        if n >= size or n >= cap:
            return lines
        try:
            if lines and float(json.loads(lines[0]).get("ts") or 0) < t0:
                return lines
        except (ValueError, AttributeError):
            return lines
        want *= 4


def calls_between(t0: float, t1: float, tag: str | None = None) -> list[dict]:
    """t0〜t1 に記録された呼び出し。tag を渡すと、その印の記録だけ（印の無い古い記録は数えない）。"""
    p = log_path()
    lines = _tail_since(p, t0)
    try:
        if lines and float(json.loads(lines[0]).get("ts") or 0) >= t0:
            lines = _tail_since(p.with_name(p.name + ".1"), t0) + lines   # 窓の途中で .1 へ回った分
    except (ValueError, AttributeError):
        pass
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if tag is not None and d.get("tag") != tag:
            continue
        try:
            ts = float(d.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if t0 <= ts <= t1:
            out.append(d)
    return out


#: 前置きに足す一行（Bash があり bwtool が使えるときだけ）。
PROMPT_LINE = ("To use a L.A.P.S tool inside a script you write (many web searches or pages, reading many binary files, "
               "transcribing many audio files), call `bwtool <Tool> key=value ...` from the script: it prints the tool's "
               "result and exits 1 on failure; `bwtool list` shows the tools it has. Filter big results inside the script "
               "and print only what you need. "
               "When you hand work to another machine or a queue (ssh, a job queue, a remote build), you are not woken when it "
               "ends unless something here waits for it: start `bwtool wait --until '<command that succeeds when it is done>' "
               "--every 60` with Bash run_in_background=true, then end your turn; you are woken when it exits. Without such a "
               "waiter, do not promise to report later.")
