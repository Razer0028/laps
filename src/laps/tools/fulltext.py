"""畳んで捨てた**全文をファイルに残し、場所を教える**（2026-09-21）。

★OSS 2 本が同じことをしている（実コードで確認・2026-09-21）:
    goose     `agents/large_response_handler.rs:13-45`
              既定 20 万字を超えたら**切らずに**一時ファイルへ書き、
              「N 文字あります。ファイル <path> を他の道具で調べて」とだけ返す
    opencode  `tool/truncate.ts:14-15`（2000 行 / 50KB）
              全文をディスクに保存し、先頭のプレビュー＋パスを返す

★BLACKWALL はこれまで `fold_head_tail_told` で**中央を畳んで**いた。畳み方は丁寧で
  「切った」と本文に書くので黙って消えはしないが、★**畳んだ中身はどこにも残らない**。
  実測（2026-09-21・同じ課題）: 1 走行で prompt 165,072 トークン。
  必要なのは全部ではなく「その中の 1 行」であることが多い。

★置き場は**作業場の中**（`.blackwall-phases/fulltext/`）。理由は 2 つ:
  ・`/tmp` に置くと、★**この機械の方針で作業場の外は読めない**ので
    モデルが Read / Grep で開けない＝教えても意味がない
  ・走行が死んでも残り、次の試行がそのまま読める（`ledger.json` と同じ考え）

⚠ 作業場が無い／書けないときは**何もしない**（`None` を返す）。
   ★保存できないのに「保存しました」と言わない。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

DIR_NAME = "fulltext"
#: 置き場の親（旧 phases.STATE_DIR_NAME・2026-10-21 工程の流し方を外したので、ここで持つ。既存の写しの場所を変えないよう名前はそのまま）。
STATE_DIR_NAME = ".blackwall-phases"
#: 置き場全体の上限。超えたら**古いものから**消す（作業場を太らせない）。
MAX_DIR_BYTES = 50 * 1024 * 1024
#: これより短い出力は残さない（畳んでいないので意味が無い）。
MIN_BYTES = 8 * 1024


def _dir() -> Path | None:
    ws = (os.environ.get("LAPS_WORKSPACE") or "").strip()
    if not ws:
        return None
    return Path(ws) / STATE_DIR_NAME / DIR_NAME


def _prune(d: Path) -> None:
    """置き場が上限を超えたら古い順に消す。★消したことは黙っていてよい
    （消したのは**写し**で、元の結果は既にモデルへ渡っている）。"""
    try:
        files = sorted((p for p in d.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > MAX_DIR_BYTES:
            p = files.pop(0)
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
    except OSError:
        pass


def keep(text: str, label: str = "out") -> str | None:
    """全文を残して**作業場からの相対パス**を返す。残せなければ `None`。

    ★相対で返す理由: モデルにそのまま Read / Grep へ渡させるため。
      絶対パスだと、作業場が移動した記録を後から再生できない。
    """
    if not text or len(text.encode("utf-8", "ignore")) < MIN_BYTES:
        return None
    d = _dir()
    if d is None:
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:8]
        safe = "".join(c for c in label if c.isalnum() or c in "-_")[:24] or "out"
        name = f"{time.strftime('%H%M%S')}-{safe}-{h}.txt"
        p = d / name
        if not p.exists():                 # 同じ中身は書き直さない
            p.write_text(text, encoding="utf-8", errors="replace")
        _prune(d)
        return f"{STATE_DIR_NAME}/{DIR_NAME}/{name}"
    except Exception:                                    # noqa: BLE001 — 写しで走行を殺さない
        return None


def note(rel: str | None, full_len: int, shown_len: int) -> str:
    """本文の末尾に足す 1 行。残せなかったときは**何も言わない**。"""
    if not rel:
        return ""
    return (f"\n★全文（{full_len:,} 字のうち {shown_len:,} 字を表示）はここに在ります:\n"
            f"   {rel}\n"
            "   欲しい所だけ Grep で探すか、Read で範囲を指定して読んでください"
            "（全部を読み直す必要はありません）。\n")
