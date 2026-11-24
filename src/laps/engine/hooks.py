"""フック（2026-11-19 オーナー「Blackwall の強化案… 進めて」・手本は Claude Code の hooks）。

道具を呼ぶ前後・依頼を受けたとき・走行を終える前に、利用者が決めた命令を走らせる。モデルに「気をつけて」と
頼むのではなく、決まりをモデルの外で確実に効かせるための口（例: このフォルダには書かせない・Edit の後に整形する）。

置き場: <LAPS の家>/hooks.json（利用者だけの設定）。書き方は Claude Code の settings.json の "hooks" と同じ:

    {"hooks": {
      "PreToolUse":  [{"matcher": "Bash|Write", "hooks": [{"type": "command", "command": "python3 ~/guard.py", "timeout": 30}]}],
      "PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "ruff format \"$LAPS_HOOK_FILE\""}]}],
      "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "date"}]}],
      "Stop": [{"hooks": [{"type": "command", "command": "bash ~/check.sh"}]}]}}

命令は標準入力に JSON を受ける: hook_event_name・session_id・cwd・tool_name・tool_input（・tool_response・prompt・last_answer）。
終わり方の意味（Claude Code と同じ）:
  0      … 続ける。PostToolUse・UserPromptSubmit の標準出力はモデルへの添え書きになる
  2      … 止める。標準エラーが理由としてモデルへ返る（PreToolUse は道具を走らせない・Stop は終えずに続けさせる・
           UserPromptSubmit は依頼を止める・PostToolUse は結果に理由を添える）
  ほか   … 命令の失敗。止めずに画面へ知らせるだけ
標準出力が JSON なら {"decision": "block", "reason": "…"} と {"additionalContext": "…"} を読む。

★作業フォルダの中の設定（.laps/hooks.json など）は読まない: 取ってきたリポジトリが命令を走らせられてしまうため。
LAPS_HOOKS=0 で全部止める。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
#: 標準出力・標準エラーをモデルへ渡す長さの上限（長いと文脈を食う）。
MAX_TEXT = 4000
#: Stop のフックで続けさせる回数の上限（止まらない走行にしない）。
MAX_STOP_BLOCKS = 3

_lock = threading.Lock()
_cache: dict[str, Any] = {"path": None, "mtime": None, "conf": {}, "error": ""}


def enabled() -> bool:
    return (os.environ.get("LAPS_HOOKS") or "1").strip().lower() not in ("0", "off", "false", "no")


def config_path() -> Path:
    from .. import paths as _lp
    return _lp.home_dir() / "hooks.json"


def load() -> tuple[dict, str]:
    """(設定, 読めなかった理由)。ファイルが変わったら読み直す（走行の途中でも次の呼び出しから効く）。"""
    p = config_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}, ""
    with _lock:
        if _cache["path"] == str(p) and _cache["mtime"] == mtime:
            return _cache["conf"], _cache["error"]
        conf, err = {}, ""
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            hooks = raw.get("hooks", raw) if isinstance(raw, dict) else {}
            for ev in EVENTS:
                groups = hooks.get(ev) or []
                if not isinstance(groups, list):
                    err = f"{ev} は配列で書いてください"
                    continue
                ok = []
                for g in groups:
                    if not isinstance(g, dict):
                        continue
                    cmds = [h for h in (g.get("hooks") or []) if isinstance(h, dict) and h.get("command")
                            and (h.get("type") or "command") == "command"]
                    if cmds:
                        ok.append({"matcher": str(g.get("matcher") or ""), "hooks": cmds})
                if ok:
                    conf[ev] = ok
        except (OSError, ValueError) as e:
            err = f"hooks.json を読めません: {e}"
        _cache.update(path=str(p), mtime=mtime, conf=conf, error=err)
        return conf, err


def _matches(matcher: str, tool: str) -> bool:
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return matcher == tool


@dataclass
class Outcome:
    block: bool = False
    reason: str = ""
    context: str = ""
    errors: list[str] = field(default_factory=list)
    ran: int = 0


def _shell_argv(command: str) -> list[str]:
    from .tools.shell import resolve_shell
    kind, exe = resolve_shell()
    if kind == "bash":
        return [exe, "-c", command]
    if kind == "powershell":
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    return [exe, "/d", "/s", "/c", command]


def _clip(s: str) -> str:
    s = (s or "").strip()
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"\n…（{len(s) - MAX_TEXT} 字を省いた）"


def run(event: str, payload: dict, *, tool: str = "", cwd: str = "") -> Outcome:
    """event のフックのうち、tool に当たる物を順に走らせる。1 つでも止めたら止める（残りは走らせない）。"""
    out = Outcome()
    if not enabled() or event not in EVENTS:
        return out
    conf, err = load()
    if err:
        out.errors.append(err)
    data = {"hook_event_name": event, "cwd": cwd or os.getcwd(), **payload}
    if tool:
        data["tool_name"] = tool
    blob = json.dumps(data, ensure_ascii=False, default=str)
    env = dict(os.environ)
    env["LAPS_HOOK_EVENT"] = event
    env["LAPS_PROJECT_DIR"] = data["cwd"]
    if tool:
        env["LAPS_HOOK_TOOL"] = tool
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    f = ti.get("file_path") or ti.get("path") or ""
    if isinstance(f, str) and f:
        env["LAPS_HOOK_FILE"] = f            # 整形の命令などに渡しやすいように
    contexts: list[str] = []
    for group in conf.get(event, []):
        if event in ("PreToolUse", "PostToolUse") and not _matches(group["matcher"], tool):
            continue
        for h in group["hooks"]:
            cmd = str(h["command"])
            try:
                timeout = max(1, min(MAX_TIMEOUT, int(h.get("timeout") or DEFAULT_TIMEOUT)))
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT
            out.ran += 1
            try:
                p = subprocess.run(_shell_argv(cmd), input=blob, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout, cwd=data["cwd"] if os.path.isdir(data["cwd"]) else None,
                                   env=env)
            except subprocess.TimeoutExpired:
                out.errors.append(f"{event} のフックが {timeout} 秒で終わらなかった: {cmd[:80]}")
                continue
            except OSError as e:
                out.errors.append(f"{event} のフックを起こせない: {e}")
                continue
            so, se = (p.stdout or "").strip(), (p.stderr or "").strip()
            j = None
            if so.startswith("{"):
                try:
                    j = json.loads(so)
                except ValueError:
                    j = None
            if isinstance(j, dict):
                if str(j.get("decision") or "").lower() in ("block", "deny"):
                    out.block, out.reason = True, _clip(str(j.get("reason") or se or "blocked by a hook"))
                    return out
                ctx = j.get("additionalContext") or (j.get("hookSpecificOutput") or {}).get("additionalContext")
                if ctx:
                    contexts.append(_clip(str(ctx)))
            elif p.returncode == 0 and so and event in ("PostToolUse", "UserPromptSubmit"):
                contexts.append(_clip(so))
            if p.returncode == 2:
                out.block, out.reason = True, _clip(se or so or "blocked by a hook")
                out.context = "\n".join(contexts)
                return out
            if p.returncode not in (0, 2):
                out.errors.append(f"{event} のフックが {p.returncode} で終わった: {cmd[:80]}" + (f"（{se[:200]}）" if se else ""))
    out.context = "\n".join(contexts)
    return out
