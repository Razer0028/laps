"""道具が返してよい量を**1箇所で決める**。

★なぜ1箇所か（2026-08-19・実機で溢れた）:
  WebFetch が中身の種類を見ずに 29MB の実行ファイルを取得し、バイナリを
  50,175 文字ぶん文脈へ流し込んで走行が context_overflow で終わった。
  調べると、上限の考え方が道具ごとにばらばらだった:
      read.py      窓に対して相対（正しい）
      webfetch.py  固定 50,000
      bash.py      固定 30,000
      grep.py      固定 30,000
      websearch.py **上限なし**
  固定値は、窓 16,384 トークンの推論器では**窓そのものより大きい**。
  ばらばらだと、次に道具を足す人がまた別の値を選ぶ。

★窓の 25% までとする。残り 75% は システム接頭辞・これまでの会話・生成余白に要る。
  1トークン≒2文字（日本語混在の保守側の見積り）で換算する。
  窓が分からないときは各道具の従来値を使う（**振る舞いを変えない**）。
"""
from __future__ import annotations

import os


#: 1回の道具の結果に許す、窓に対する割合（トークン基準）。
#: ★2026-09-13 にここを可変にした理由（オーナー指摘「文脈の圧縮が頻繁でスムーズじゃない」）:
#:   既定 0.25 は「結果 4 本で窓が埋まる」という意味で、ファイルを 5 本読むと
#:   **構造的に圧縮が避けられない**。そして圧縮はプロンプトの前方を書き換えるので
#:   接頭辞キャッシュが無効になる（この木の実測: 末尾追記 0.10s 対 先頭書き換え 26.00s＝**260倍**）。
#:   ＝圧縮の回数がそのまま体感の遅さになる。
#:   ★既定は動かさない。**測ってから**動かす（推測で定数を動かして外した日があるので）。
#: ★2026-09-13: 0.25 → 0.10 に下げた。根拠（同じ依頼・同じ現場・交互に 2 本ずつ）:
#:       割合   圧縮  道具   秒    圧縮/道具  秒/道具
#:       0.25    12    17   359      0.71      21.1
#:       0.25     4     7   274      0.57      39.1
#:       0.10     3    15   110      0.20       7.3
#:       0.10     5    11   140      0.45      12.7
#:   ★**所要秒が重ならない**（0.25 は 274〜359・0.10 は 110〜140）。しかも 0.10 のほうが
#:     道具を**多く**呼んでいる（26 対 24）のに 40% の時間で終わっている。
#:   ★答えの質は落ちていない（本文 886〜1,198 字・実在するモジュール名の一致は同等）。
#:   ⚠ 正直に書く: **n=2／腕**で、圧縮の回数そのものは重なっている（4 対 5）。
#:     強いのは**時間**の側。有意差の検定はしていない。
#:   ⚠ **未測定**: これが「課題の達成率」に効くかは測っていない（上の測定は調査の依頼）。
#:     1 回の Read が 16,384 → 6,553 文字（約 160 行）になるので、読み直しの手番が増える。
#:     24 課題のベンチで確かめること（腕E の後）。
DEFAULT_OUT_CAP_FRACTION = 0.10


def out_cap_fraction() -> float:
    """窓に対する割合。`LAPS_OUT_CAP_FRACTION` で変えられる（0.01〜0.90）。"""
    raw = os.environ.get("LAPS_OUT_CAP_FRACTION")
    if not raw:
        return DEFAULT_OUT_CAP_FRACTION
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_OUT_CAP_FRACTION
    if not (0.01 <= v <= 0.90):          # ★読めない値で黙って極端にしない
        return DEFAULT_OUT_CAP_FRACTION
    return v


def out_cap(ceiling: int) -> int:
    """1回の道具呼び出しが返してよい文字数。

    `ceiling` は「窓が分からないときの値」＝その道具の従来の上限。
    """
    env = os.environ.get("LAPS_N_CTX")
    if not (env and env.isdigit() and int(env) > 0):
        return ceiling
    # 1トークン≒2文字（日本語混在の保守側の見積り）。
    return max(4000, min(ceiling, int(int(env) * out_cap_fraction() * 2)))


def note(total: int, cap: int, hint: str = "") -> str:
    """切ったことを**必ず書く**ための一文。

    ★黙って切ると「全部読めた」と思い込む。この木で実際に踏んだ形。
    """
    tail = f" {hint}" if hint else ""
    return (f"\n\n[ここで切りました。全体は {total:,} 文字です"
            f"（この推論器の窓に対する上限 {cap:,} 文字）。{tail}]")


# ---------------------------------------------------------------------------
# ★32K 窓に対する**絶対量**で決め直す（2026-08-29）
# ---------------------------------------------------------------------------
#
# 上の `out_cap` は「窓の 25%」という**割合**で決めている。割合は窓が広い
# うちは無害だが、32,768 の窓では次のようになる:
#
#     out_cap(30000) @ n_ctx=32768  =  16,384 文字
#
# これが妥当かどうかは、割合ではなく**足し算**でしか判定できない。
# `context_budget.Budget` の会計をそのまま持ってくると、
#
#     transcript に使ってよい = n_ctx − gen − slack − 固定接頭辞
#                             = 32,768 − 4,096 − 1,024 − 5,200 = 22,448 トークン
#     削り終える目標(LOW_WATER 0.50)                          = 11,224 トークン
#
# ＝**会話全体が 11,224 トークンに収まっていなければならない。**
# ところが 16,384 文字の道具結果は、
#     実測 /tokenize 換算 (0.34 tok/byte) で 5,570 トークン ＝ 目標の **50%**
#     近似 (approx_tokens = UTF-8 バイト数) では 16,384 トークン ＝ 目標の **146%**
# つまり **1 回の道具呼び出しが、会話に許された席をひとりで埋める。**
#
# 実際そうなっている。run29/31/32/34 の実測:
#     実際に縮んだ文脈整理 5,086 回 / 道具呼び出し 6,524 回（≒毎ターン）
#     整理直前の会話は中央値 20,600・p90 37,500 トークン（窓 32,768 より大きい）
#
# ★そこで「1 回の結果が占めてよいのは、削り終えた会話の 1/8」と決める。
#   1/8 ＝ 直近 8 件の道具結果が同時に載る、という意味。仕切り直しの直前に
#   実際に何件見えているべきかを、割合ではなく件数で言い切った値である。
#
# 換算は 0.34 トークン/バイト（4 走行・n=5,283 の突き合わせ、r=0.943、
# IQR 0.31〜0.38。中央値を採る）。バイトで持つのは `approx_tokens` が
# UTF-8 バイト数だからで、文字数だと日本語で 3 倍ずれる。
#
#: agent.py の通常ターンの max_tokens。
GEN_RESERVE = 4096
#: Budget.slack の既定。
SLACK = 1024
#: system + 道具 22 個のスキーマ（native モードの実測 5,214 を丸めた）。
FIXED_PREFIX = 5200
#: context_budget.LOW_WATER。削り始めたらここまで落とす。
LOW_WATER = 0.50
#: 削り終えた会話に同時に載っていてほしい道具結果の件数。
LIVE_RESULTS = 8
#: 実測のトークン/バイト（中央値）。
TOK_PER_BYTE = 0.34
#: これ以下には落とさない。道具が何も返せないほうが害が大きい。
BODY_FLOOR = 1200


def body_cap(ceiling: int, *, live: int = LIVE_RESULTS) -> int:
    """1 回の道具呼び出しが返してよい **UTF-8 バイト数**。

    `ceiling` は「窓が分からないときの値」＝その道具の従来の上限。
    ★`out_cap` を置き換えない。`out_cap` は外側の安全網として残し、
      こちらは**その内側で先に畳む**ための値（畳めば `out_cap` には当たらない）。
    """
    env = os.environ.get("LAPS_N_CTX")
    if not (env and env.isdigit() and int(env) > 0):
        return ceiling
    n_ctx = int(env)
    transcript = n_ctx - GEN_RESERVE - SLACK - FIXED_PREFIX
    if transcript <= 0:
        return BODY_FLOOR
    per_call_tokens = (transcript * LOW_WATER) / max(1, live)
    cap = int(per_call_tokens / TOK_PER_BYTE)
    return max(BODY_FLOOR, min(ceiling, cap))


# ---------------------------------------------------------------------------
# ★**歩く時間**の上限（2026-09-06・owner 実機 P0「道具が (running) のまま戻らない」）
# ---------------------------------------------------------------------------
#
# 上の 2 つは「返してよい**量**」の上限で、返すところまで辿り着ける前提だった。
# 実機で壊れたのはその手前——`D:\Blackwall`（ゲームの入ったフォルダ）で
# `Glob(pattern="**/*")` を呼ぶと `Path.rglob` が木を歩ききるまで返らず、画面は
# `(running)` のまま止まった。件数の上限（Glob の `_ENUM_MAX=20000`）は
# **一致したものしか数えない**ので、`**/*.exe` のように一致が少ないパターンでは
# 一度も発火しない＝上限が在るのに効かない形。`agent._fake_media_files` を
# 2026-09-06 に境界化したときと**同じ壁時計**をここに置き、歩く道具が全部これを見る。
#
# ★秒で止めるのが要点。「何件見たか」では止められない（歩く費用は件数ではなく
#   ディレクトリの深さと I/O の遅さで決まる。ネットワークドライブ・スピンダウンした
#   HDD・Windows の junction で、同じ件数が 100 倍の時間になる）。
#: 歩く道具（Glob / Read の推測・双子探し）の既定の持ち時間（秒）。
#: Grep は自前で 30 秒を持っている（`LAPS_GREP_TIMEOUT`）ので触らない。
WALK_SECS = 10.0


def walk_secs(default: float = WALK_SECS) -> float:
    """歩いてよい秒数。`LAPS_WALK_SECS` で変えられる。0 以下＝無制限。

    ★無制限にできる口を残す理由: 大きな木を本当に全部数えたい走行（検収・棚卸し）が
      あり、そこで黙って切ると「無い」という嘘になる。既定は止まらないほうを選ぶ。
    """
    env = os.environ.get("LAPS_WALK_SECS")
    if env is None or not env.strip():
        return default
    try:
        return float(env)
    except ValueError:
        return default


class Walk:
    """歩く間ずっと持ち歩く「あと何秒あるか」。

    使い方は 1 つだけ: ループの頭で `if walk.expired(): break`。
    ★`expired()` は**切れた瞬間に一度だけ** True を覚える（`self.hit`）ので、
      呼び出し側は「切ったかどうか」を別に数えなくてよい＝
      「切ったのに黙っている」（この現場でいちばん高くつく形）を作りにくい。
    """

    __slots__ = ("deadline", "hit")

    def __init__(self, secs: float | None = None) -> None:
        s = walk_secs() if secs is None else secs
        import time as _t
        self.deadline = (_t.monotonic() + s) if s > 0 else None
        self.hit = False

    def expired(self) -> bool:
        if self.deadline is None:
            return False
        import time as _t
        if _t.monotonic() > self.deadline:
            self.hit = True
            return True
        return False
