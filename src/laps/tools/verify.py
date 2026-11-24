"""Verify — 「終わった」と言う前に、自分で確かめるための道具。

★2026-09-21 新設。オーナー指示 ①（2026-09-10 の方針の延長）:
    「開発工程や動作に関するフローを組み込んでおいて、**それをモデルが適宜判断する**
      ようにしないと」
    「エージェントが勝手に動きすぎ。基本的にモデルのサポート的な立ち位置じゃなきゃ
      いけないのにエージェントが主導権を握り過ぎている」

★だからこれは**差し戻しではない**。2026-09-10 に撤去した `_completion_objection` 系
  （ハーネスが反論を積んでモデルに再応答させる形）を復活させるものではない。
  ★**道具として置くだけ**。呼ぶかどうかはモデルが決める。落ちても押し戻さない。

## OSS 5 本を突き合わせた結果（goose 1 本で設計しない）

    goose    ターン終わりに判定コマンド＋自動やり直し（`agents/types.rs:17-31`）
             → ★**自動やり直しは採らない**（それが差し戻し）
    aider    編集があったときだけ走らせる（`base_coder.py:1616`）
             落ちたら利用者に聞く（`:1619`）
             → ★**「編集があったときだけ」の発想を説明文に書く**
    opencode 編集直後に LSP 診断（`tool/edit.ts:198`）
             → ★採らない（言語ごとの常駐サーバが要る。うちは構文ゲートで代替済み）
    cline    相当機構なし（実測 0 ファイル）

## なぜ Bash と別に置くか
Bash でも同じことはできる。★分ける理由は **意味**:
  ・「確かめる」という**工程があることをモデルに見せる**（道具一覧に出る＝思い出す）
  ・落ちた出力を**そのまま**返す約束をここに書ける（要約して理由を隠さない）
  ・記録に `verify` として残る＝**確かめた走行と確かめていない走行を後から区別できる**
"""
from __future__ import annotations

import shlex

from . import ToolResult, ToolSpec

_CAP = 4000


def _handler(inp: dict) -> ToolResult:
    cmd = str(inp.get("command") or "").strip()
    if not cmd:
        return ToolResult("Error: missing 'command'", ok=False, kind="error")
    try:
        shlex.split(cmd)
    except ValueError as e:
        return ToolResult(f"Error: コマンドの引用が閉じていません: {e}",
                          ok=False, kind="error")
    # ★Bash と同じ経路で走らせる。**別の実行系を新設しない**——
    #   policy の門・作業場の判定・cp932 の扱い・プロセスグループの kill を
    #   もう一度書くと、必ずどちらかが古くなる（この現場が何度も踏んだ型）。
    from . import bash as _bash
    # ★`SPEC.handler` ではなく `_run` を直に呼ぶ（2026-09-21）。
    #   `test_checkpoint.py::test_ツールを実行する場所は一箇所しかない` は
    #   **`.handler` の呼び出し箇所が `agent.py::_exec_tool` の 1 つだけ**であることを
    #   AST で見ている。控え（rollback 用）はそこにしか置いていないので、
    #   2 つ目の `.handler` 経路ができると「戻したと言うのに戻っていない」が起きうる。
    #   ★この道具は編集しない（Bash と同じく実行するだけ）ので実害は無いが、
    #     **門を緩めるより、門に触れない形にするほうが正しい**——
    #     緩めた瞬間、次に本当に危ない経路が増えても落ちなくなる。
    res = _bash._run({"command": cmd, "timeout": int(inp.get("timeout") or 300)})
    out = str(res)
    code = 0 if getattr(res, "ok", True) else 1
    body = out.strip()
    if len(body) > _CAP:
        # ★末尾を残す。試験もリントも**結論は最後に出る**。
        body = f"…（先頭を切りました: 全 {len(out):,} 文字）\n" + body[-_CAP:]
    if code == 0:
        return ToolResult(f"[確認] `{cmd}` は通りました（終了コード 0）。\n{body}",
                          ok=True, kind="ok")
    # ★落ちた出力を**そのまま**返す。run44 の教訓（畳みが理由を隠して 197 回打った）。
    return ToolResult(
        f"[確認] `{cmd}` は **終了コード {code}** で落ちました。\n{body}\n"
        "★まだ終わっていません。この出力を読んで直してから、もう一度確かめてください。",
        ok=False, kind="error")


SPEC = ToolSpec(
    name="Verify",
    description=(
        "作った物が本当に動くかを、コマンドを走らせて確かめる（試験・リント・起動確認など）。\n"
        "★**ファイルを書き換えたら、終わりと言う前にこれを呼ぶ。** 書き換えていない"
        "調べ物だけの作業では呼ばなくてよい。\n"
        "落ちたら出力をそのまま返すので、それを読んで直し、また呼んで確かめる。\n"
        "例: Verify(command=\"python3 -m pytest -q\") / Verify(command=\"node --check app.js\")"
    ),
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "走らせる確認のコマンド。例: `python3 -m pytest -q`"},
            "timeout": {"type": "integer", "default": 300},
        },
        "required": ["command"],
    },
    handler=_handler,
    executes=True,
)
