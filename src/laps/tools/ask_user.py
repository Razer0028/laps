"""AskUserQuestion tool: mid-task clarification.

Renders the question + options to the terminal and reads a single line.
In headless mode (LAPS_HEADLESS=1) returns a sentinel so the agent can
choose a default. Honors LAPS_YES=1 by picking the first option.
"""
from __future__ import annotations
import os
import sys

from . import ToolSpec, ToolResult


# 利用者に聞く窓口。差し込まれていればそちらを使う（アプリ経由のとき）。
# ★None のままなら従来どおり（TTY があれば端末で聞き、無ければ諦める）。
_ASKER = None


def set_asker(fn) -> None:
    """利用者に聞く手段を差し込む。`fn(question, options) -> str`。"""
    global _ASKER
    _ASKER = fn


def _run(inp: dict) -> str:
    question = inp.get("question") or inp.get("prompt")
    options = inp.get("options") or []
    if not question:
        return "Error: missing 'question'"
    if not isinstance(options, list):
        return "Error: 'options' must be a list"

    headless = os.environ.get("LAPS_HEADLESS") == "1"
    auto_yes = os.environ.get("LAPS_YES") == "1"

    # Render
    lines = [f"\n[AskUser] {question}"]
    for i, opt in enumerate(options, 1):
        if isinstance(opt, dict):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(f"  {i}. {label}" + (f"  — {desc}" if desc else ""))
        else:
            lines.append(f"  {i}. {opt}")
    prompt_str = "\n".join(lines) + "\n> "

    # ★アプリ経由（サイドカー）では stdin は通信路なので TTY ではない。しかし
    #   **画面はある**。諦めて「利用者の入力なし」を返すと、道具があるのに使えない。
    #   serve.py が窓口を差し込んでいれば、そちらへ回す。
    asker = _ASKER
    if asker is not None:
        try:
            return asker(question, options)
        except Exception as e:
            # ★**成否を名乗る**（2026-09-02）。旧: 素の `(利用者に聞けませんでした: …)`。
            #   `Error:` で始まらないので `tool_failed` の語彙表に当たらず、
            #   **聞けなかったのに ok=True**（画面に緑）で記録されていた。
            #   さらに悪いことに、この文字列は「利用者の答え」が入る位置に返るので、
            #   モデルはこれを**利用者の返事**として読み、括弧の中身に沿って進む
            #   ことがある。何が起きたかを名指しし、次の一手も書く。
            # ★2026-10-16 監査: モデルが読む文なので英語に（日本語の文がモデルへ渡る所を減らす）。
            return ToolResult(
                f"Error: could not ask the user ({type(e).__name__}: {e}). This is not the user's answer. "
                "To go on without asking, decide yourself and state the assumption you made.",
                ok=False, kind="error")

    if headless or not sys.stdin.isatty():
        if auto_yes and options:
            first = options[0]
            ans = first["label"] if isinstance(first, dict) else str(first)
            return f"(auto-yes) {ans}"
        # ★2026-09-08 監査（tools high）: 旧: 素の "(no-tty; …)" を **ok=True の文字列**で返していた＝
        #   モデルはこれを利用者の返事として読む。聞けなかったことを Error として名乗る。
        return ToolResult("Error: could not ask the user (no-tty: there is no interactive terminal). This is not the "
                          "user's answer. To go on without asking, decide yourself and state the assumption you made.",
                          ok=False, kind="error")

    try:
        sys.stdout.write(prompt_str)
        sys.stdout.flush()
        raw = sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt):
        return ToolResult("Error: could not ask the user (user aborted the input). This is not the user's answer. "
                          "Do not ask the same question again: go on with a stated assumption, or stop the work.",
                          ok=False, kind="error")

    if not raw:
        if auto_yes and options:
            first = options[0]
            return f"(default) {first['label'] if isinstance(first, dict) else first}"
        return ToolResult("Error: the user's answer was empty (empty answer). This is not the user's answer. "
                          "If there were options, go on with the default and say so; otherwise state your assumption "
                          "and go on.", ok=False, kind="error")

    # Numeric pick
    if raw.isdigit() and options:
        n = int(raw)
        if 1 <= n <= len(options):
            chosen = options[n - 1]
            return chosen["label"] if isinstance(chosen, dict) else str(chosen)

    return raw


SPEC = ToolSpec(
    name="AskUserQuestion",
    description=(
        "Ask the user a clarifying question mid-task with optional choices. "
        "Not for asking permission to run a command or take the next step: in auto mode just do the work. "
        "Returns the user's answer as a string. In headless/no-tty mode, "
        "returns a sentinel so the model can decide without input."
    ),
    schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question text"},
            "options": {
                "type": "array",
                "description": "Optional choices; each is a string or {label, description}",
                "items": {"type": ["string", "object"]},
            },
        },
        "required": ["question"],
    },
    handler=_run,
)
