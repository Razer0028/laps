"""共有ヘルパ: `serve.py` が線に出しうる notice の `kind` を静的抽出する。

★2026-09-05（r4/app-term-clients）: 元は `test_windows_parity.py` にだけあった
  `_serve_notice_kinds()` を、term 側の completeness test（`test_term_render.py`）
  でも使うためここへ切り出した。

  ★あえて共有する理由（`test_the_extraction_itself_is_not_broken` 等の兄弟の
  「あちらは Android 用で、共有すると片方の都合で抽出条件が変わったとき両方の
  意味が同時に変わる」という注記とは逆の判断）: この関数は「serve.py が出しうる
  kind の一覧」という**事実そのもの**を1回だけ数える道具で、Windows/term どちらの
  表と比べるかは呼び出し側が決める。事実の数え方を2ファイルに複製すると、
  今回まさにこれが実際に起きた（term 側だけこの completeness test が無く、
  Windows 側だけが十数個の未知 kind に気づける状態だった）。数え方を1本にして
  おけば、次に serve.py が kind を増やしたとき **両方の試験が同時に**気づく。

  ★2026-09-05 追記（同ワーカー・run50 の実記録を再生していて発見）: この関数は
  元々 `agent.py`/`serve.py` の2ファイルしか見ておらず、`tools/task.py`（委譲した
  子の道具活動を `kind="subagent"` の notice 1本にまとめて親へ中継する）が
  出す kind を**一度も数えていなかった**。実記録では 120 件（うち 5 件は子の
  道具が本当に失敗した ✗）と、`verifier`/`red_tests` よりずっと高頻度に発火して
  いたのに、Windows 側の completeness test も term 側もこれに気づけない状態
  だった（"kind" を持つ辞書を線へ流す場所が3ファイル目にもあった）。
  `laps/tools/` 配下を `_parent("notice", {...})` の形で走査対象に加える。

  ★2026-10-21 旧中核（agent.py）と工程モード（phases.py）を外した。数えるのは serve.py・oc/・tools/ の 3 か所。
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
SERVE_PY = SRC / "laps" / "serve.py"
TOOLS_DIR = SRC / "laps" / "tools"
#: ★2026-10-05: oc 中核（0.1.272 から既定）。ここを走査していなかったので、oc が出す
#:   `compaction_failed` / `doom_loop` が表に無いまま出荷され、実機で「不明な事象」と出た。
OC_DIR = SRC / "laps" / "oc"


def serve_notice_kinds() -> set[str]:
    """serve.py が `notice` として出しうる `kind`（静的抽出）。

    ★`kind=etype`（工程・文脈系）で動的に決まる分も、etype の一覧から拾う。
    """
    src = SERVE_PY.read_text(encoding="utf-8")
    kinds: set[str] = set()
    # ★`_record(` は**記録（events.jsonl）だけ**で線には出ない（serve.py:592 の注記）。
    #   ここに入れると `record_gap` / `write_new` のような記録専用の kind が
    #   「知らない」と誤って赤くなる——**線に出るものだけ**を数える。
    for m in re.finditer(
        r'(?:out\.emit|_emit_recorded)\(\s*"notice"\s*,(.{0,400}?)\)\n', src, re.S
    ):
        body = m.group(1)
        k = re.search(r'kind\s*=\s*"([a-z_]+)"', body) or re.search(r'"kind"\s*:\s*"([a-z_]+)"', body)
        if k:
            kinds.add(k.group(1))
    # `kind=etype` / `kind=_kind` の形で流れる etype（_on_event の分岐）。
    kinds |= {"context_exhausted", "context_compacted", "context_reset",
              "checkpoint_error", "cancel_error", "notice"}
    # ★道具側（tools/task.py の委譲 等）が `_parent("notice", {...})` の形で
    #   直接 notice を組み立てて親（agent → serve）へ中継する分。generic な
    #   `"kind":\s*"..."` 全走査は tool_result 側の kind（ok/error/timeout 等・
    #   別の名前空間）まで拾ってしまうので、**`_parent("notice", {…})` の
    #   引数の中だけ**に絞る。
    for py in sorted(OC_DIR.glob("*.py")):
        kinds |= set(re.findall(r'_emit\("notice", \{"kind": "([a-z_]+)"', py.read_text(encoding="utf-8")))
    for py in sorted(TOOLS_DIR.glob("*.py")):
        src_t = py.read_text(encoding="utf-8")
        for m in re.finditer(r'_parent\(\s*"notice"\s*,\s*\{(.{0,400}?)\}\)', src_t, re.S):
            k = re.search(r'"kind"\s*:\s*"([a-z_]+)"', m.group(1))
            if k:
                kinds.add(k.group(1))
    return {k for k in kinds if k}
