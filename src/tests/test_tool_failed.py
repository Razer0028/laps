"""道具が失敗したかを1箇所で決める。**実機の文面を種にしている。**

★2026-08-16 実測: 明らかに失敗している道具呼び出し **5件が、記録では5件とも ok=True**。
本体が `ok` を一度も送っておらず（serve.py）、アプリ側は「明示が無ければ成功」として
扱っていた（sidecar.rs:425「推測しない。明示された ok だけを見る」）。
アプリ側の作りは正しく、**送っていない側**が穴だった。

実害は2つ:
  1. **失敗率が測れない。** 私は watch_turn で `ok is False` を旗にしていたが、一度も立たない。
  2. エージェント自身も「道具が失敗した」と「道具が0件を返した」を区別できない。
     実機では、成功した `dir` の結果を「Bash では dir は利用不可」と誤読する例も出ている。

★判定は保守的に。**成功を失敗と呼ばない**ほうを優先する（偽の失敗はやり直しを招く）。
"""
from __future__ import annotations

import pytest

from laps.tools import tool_failed

FAILED = [
    "Error: policy denied: `cmd` は許可リストに無い（mode=workspace）",
    "Error: policy denied: 解析できないため不可（fail-closed）: No closing quotation",
    "Error: edit[0]: old_string not found: 'chrome_driver笑'",
    "Error: grep timeout",
    "'python' は、内部コマンドまたは外部コマンド、\n操作可能なプログラムまたはバッチ ファイルとして認識されていません。\n",
    "指定されたパスが見つかりません。\n",
    "アクセスが拒否されました。\n",
    "bash: foo: command not found\n",
]

OK = [
    "\r\nMicrosoft Windows [Version 10.0.26200.9168]\r\n",
    " ドライブ C のボリューム ラベルは Windows です\r\n ボリューム シリアル番号は 34CE-30D9 です\r\n",
    "OK: wrote 2911 bytes to probe/filestat.py",
    "OK: 1 replacement(s) across 1 edit(s)",
    "(no matches)",                       # ★0件は失敗ではない
    "(exit 0, no output)",                # ★無出力も失敗ではない（mkdir 等）
    "",
    "VDIR\nVDIR\\0001remove.py\n",
]


@pytest.mark.parametrize("text", FAILED)
def test_失敗を失敗として拾う(text: str) -> None:
    assert tool_failed(text) is True, f"取り逃している: {text[:60]!r}"


@pytest.mark.parametrize("text", OK)
def test_成功を失敗と呼ばない(text: str) -> None:
    assert tool_failed(text) is False, f"誤検知: {text[:60]!r}"


def test_0件と失敗を混同しない() -> None:
    """★grep の欠陥と対になる話。0件は答えであって失敗ではない。
    逆に、検索そのものが失敗したときは Error: で始まるので拾える。"""
    assert tool_failed("(no matches)") is False
    assert tool_failed("Error: 検索そのものが失敗しました（regex parse error）") is True


def test_本文の後ろに出てくる語では鳴らない() -> None:
    """判定は先頭 400 字に限る。長い出力の末尾に紛れた語で失敗にしない。"""
    body = "OK\n" * 400 + "command not found という文字列を含むログ\n"
    assert tool_failed(body) is False
