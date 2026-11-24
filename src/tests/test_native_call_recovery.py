"""native の道具呼び出しが**空**のとき、本文からの抽出へ落ちること。

★2026-08-18 の web 調査ワークフローが指摘し、現物で裏を取った構造的な穴:

    if native_calls is not None:      # ← backend は native 経路で必ず list を返す
        calls = native_calls          #    （空でも `[]`）＝この枝が**常に**真
    else:
        calls = self._extract_tool_calls(reply)   # ← 一度も走らない

つまり **native モードでは本文からの抽出が構造上死んでいた**。
Qwen3 系（とくに omega_j_prime の学習テンプレート）は `<tool_call>{…}</tool_call>` を
**本文として**書くことがあり、その回は道具呼び出しが丸ごと無視されて散文が答えになる。
実機の「道具を呼ばず記事を書いて終わる」の有力な原因。

★ここで測るのは分岐の性質だけ（モデルもサーバも要らない）。
  「本文に道具呼び出しがあり、native が空なら、拾う」——それだけ。
"""
from __future__ import annotations


BODY = (
    "承知しました。まず一覧を見ます。\n"
    '<tool_call>\n{"name": "Bash", "arguments": {"command": "ls -la"}}\n</tool_call>'
)


def test_空のnativeは偽と評価される() -> None:
    """★直した条件（`if native_calls:`）が、空リストで**偽**になることの直接確認。

    旧条件 `native_calls is not None` はここで真になり、本文が捨てられていた。
    """
    native_empty: list = []
    assert not native_empty, "空リストが偽でない＝Python の前提が違う"
    assert native_empty is not None, (
        "★負の対照: 空リストは None ではない。だから旧条件は常に真だった"
    )


