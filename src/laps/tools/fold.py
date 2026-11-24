"""道具が返す量を**畳む**（黙って切らない）。

★なぜ切り捨てでは駄目か（2026-08-29・run29/31/32/34 の実測）:
  4 走行の道具結果 8,094,347 バイトを数え直したところ、

      Grep  158 回のうち 4 回が上限 16,384 文字ちょうどで終わっていた。
            `"\\n".join(lines)[:cap]` という**行の途中で切る無言の切断**で、
            しかも「何件のうち何件を見せたか」がどこにも書かれていない。
      Glob  239 回・896,469 バイト。**文字数の上限が無く**、`[:500]` の
            件数上限も無言。`**` 1回で 26,952 バイト（500 行）が入り、
            その大半が `images/田中花子/*.png` の並びだった。
      Bash  878,221 バイト。上限は先頭から切るので、**一番効く末尾**
            （終了時のエラー行・要約行）が落ちる。

  同じ 4 走行で、文脈の整理は**道具呼び出し 1 回につき 1 回**発火している
  （実測: 実際に縮んだ整理 5,086 回 / 道具呼び出し 6,524 回、
  整理直前の会話は中央値 20,600 トークン・p90 37,500＝窓 32,768 より大きい）。
  ＝この現場は**常時圧縮**の状態にある。道具が返す量を減らすことは、
  「圧縮で失われる量」をそのまま減らすことに等しい。

★方針は3つだけ。
  ① 切ったら**必ず本文に書く**（何件のうち何件か）。
  ② **末尾を残す**（末尾に結論がある。先頭から cap 文字は最悪の切り方）。
  ③ 一覧は**まとめる**（同じディレクトリ・同じファイルの並びは件数に畳む）。
"""
from __future__ import annotations

from collections import OrderedDict


def nbytes(text: str) -> int:
    """`context_budget.approx_tokens` と同じ通貨＝UTF-8 バイト数。"""
    return len(text.encode("utf-8", "replace"))


def clip_bytes(text: str, cap: int) -> str:
    """UTF-8 で `cap` バイトに収まるところまで（**文字の途中で割らない**）。"""
    if cap <= 0:
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= cap:
        return text
    return raw[:cap].decode("utf-8", "ignore")


#: 中略マーカーのために空けておくバイト数。実測のマーカーは 83〜114 バイト
#: （桁数で伸びる）なので、余裕を見た固定値。
MARKER_ROOM = 120
#: 畳んだ末尾がこれだけの実バイトを持てないなら、**畳まずに先頭を残す**。
#: ★根拠（2026-09-05 r4c・査読 must_fix#1・live 12 文書で実測）: 畳みは
#:   「マーカー分のバイトを払って末尾に届く」取引である。払った末尾が数十
#:   バイト（日本語で 5〜20 字）しか無いなら、それは文の断片であって結論では
#:   ない——**払っただけ損**になる。実測（知識サーバー の引用・`_per` は窓と件数で決まる）:
#:       _per=150  畳み 実文字  60B ／ 切り捨て 150B   末尾に到達 0/12 文書
#:       _per=177  畳み 実文字  70B ／ 切り捨て 177B   末尾に到達 0/12
#:       _per=224  畳み 実文字 104B ／ 切り捨て 224B   末尾に到達 0/12（tail 15B）
#:       _per=700  畳み 実文字 582B ／ 切り捨て 698B   末尾に到達 12/12
#:   ＝小さい窓では畳みが**master の切り捨てより情報を減らしていた**。
#:   120 バイト（日本語 40 字 ≒ 1 文）を末尾に置けない cap では畳まない。
#: ★2026-09-05 r4c 3 巡目（査読 must_fix#1・**この門は片方にしか無かった**）:
#:   1 巡目・2 巡目の門は `_fold_bytes`（バイトの枝）だけに置かれていた。行の枝は
#:   独自に `tail_budget = (cap-120) - int((cap-120)*0.4)` を配るので**同じ cap で
#:   47 バイト多く**、cap=135〜398 でも「畳めた」と判断してマーカーを出していた。
#:   実測（短い行 `line0..line1999` を 2,000 本＝man ページの形。この形は
#:   2 巡目の property test に**入っていなかった**ので黙って通っていた）:
#:       cap 0..398 のうち **253 個**（最小 cap=135）が畳んでマーカーを出す
#:       cap=150  畳みの実文字  30B ／ 切り捨て 150B
#:       cap=284  畳みの実文字 159B ／ 切り捨て 283B
#:       cap=398  畳みの実文字 271B ／ 切り捨て 398B
#:   凍結した live 3 fixture（52 文書）でも **20 件**が切り捨てを下回っていた:
#:       _per=224   1 件（最悪 man7 ip-route.8   98B < 224B）
#:       _per=284   4 件（最悪 postgresql indexes-types 124B < 284B。
#:                        `lab://notes.md` は 131B < 284B）
#:       _per=398  15 件（最悪 `lab://Genspark-CLIの使い方.md` 213B < 398B）
#:   **`_per=284` は本番で実際に出る値**（32,768 窓・fts5_trigram の実 payload）。
#:   → 門を**両方の枝の手前**（`fold_head_tail_ex` の入口）へ移した。境目は
#:   `_tail_room()` だけで決まり、**行数にも行の長さにも依存しない**
#:   （2 巡目の申告「境目は cap>=399」は行の枝には効いていなかった＝嘘だった）。
#:   head_frac=0.4 なら成立するのは **cap >= 399**
#:   （`cap - int(cap*0.4) - 120 >= 120`。int の切り捨てが効くので 400 ちょうど
#:   ではない。境目は `test_末尾を残せない小さな窓では畳まずに先頭を残す` が固定）。
#: ★もう 1 つの門（同じく 3 巡目）: cap がこの帯を超えていても、**行の詰め方
#:   しだいで末尾に実文字が入らない**ことがある（末尾が空行だけの文書）。
#:   予算の話（`_tail_room`）とは別に、**head 側・tail 側それぞれの実文字**を
#:   見る（合算すると head の実文字が tail の 0 を隠す——2 巡目の `real_kept`
#:   はまさに合算していて、cap=400/500/700 で「末尾は残しています」と書きながら
#:   末尾の実文字が 0 だった）。
MIN_TAIL_BYTES = 120
#: 畳んだあとの**末尾に実際に入っていなければならない実文字**の下限。
#: `MIN_TAIL_BYTES` が「予算」の話（席を用意できるか）なのに対し、こちらは
#: 「その席に本当に中身が座ったか」の話。
#: ★根拠（2026-09-05 r4c 4 巡目・査読 must_fix#4）: `_fold_bytes` の末尾判定は
#:   `not tail.strip()`＝**実質 0 バイト**だったので、cap=399〜450（`tail_b` は
#:   120〜150）の帯では末尾に助詞 1 文字（1〜3 バイト）でも残っていれば
#:   「末尾は残しています」を通していた。マーカーに 120 バイト払って 1 文字を
#:   得るのは `MIN_TAIL_BYTES` が禁じたのと同じ取引。
#: ★40 にした理由: 120（`MIN_TAIL_BYTES`）を課すと cap=399 で `tail_b` が
#:   ちょうど 120 なので、UTF-8 の文字境界で 1〜2 バイト落ちるだけで畳めなくなり、
#:   **境目が「文字の並び」依存**に戻る（3 巡目に潰した性質）。40 はその余裕の
#:   内側に十分収まるので、境目は `_tail_room` だけで決まったまま動かない。
MIN_REAL_TAIL_BYTES = 40

#: 畳めずに「先頭だけ」を返したときに本文へ置く印（`fold_head_tail_told`）。
#: ★なぜ要るか（2026-09-05 r4c 4 巡目・査読 must_fix#1・**新規回帰**）:
#:   3 巡目で足した「末尾が空白しか無いなら畳まない」枝は、`clip_bytes` へ
#:   落として `False` を返す。ところが 1 値版 `fold_head_tail` の呼び出し側
#:   （`tools/bash.py` と 旧い中核の部品）は 2 つめを受け取れないので、
#:   **切ったことがどこにも書かれない**。実機 e2e（`LAPS_N_CTX=16384`・
#:   Bash 道具・`'DATA'*1500 + '\n'*700`）で 6,700B → 1,200B に切られたのに
#:   本文の開示語は 0 だった＝このファイル冒頭の方針①「切ったら必ず本文に書く」
#:   に真っ向から反する。必要な末尾空白は `_tail_room(cap)` ぶん（16,384 窓で
#:   600B・32,768 窓で 2,356B）＝ログの末尾に空行が並ぶだけで**普通に起きる**。
#: ★「中略」を含めないこと。property test は「`中略` があること」と
#:   「`末尾は残しています` と書いてあること」を同値として固定している。
HEAD_ONLY_MARK = "…（ここで切りました。先頭だけです）"
#: 印を出すために本文から払ってよいと決める最小の残り。印を付けた結果、本文が
#: これを割るなら**印より本文を採る**（払う価値が無い＝`MIN_TAIL_BYTES` と同じ考え）。
#: その場合は呼び出し側の外枠（`limits.note` 等）で書くこと。
MIN_MARKED_BODY = 40


def _tail_room(cap: int, head_frac: float) -> int:
    """畳んだときに**末尾へ配れる**バイト数。境目を 1 箇所で決めるための関数。

    ★行の枝とバイトの枝が別々の式で末尾の席を計算していたのが r4c 2 巡目までの
      欠陥（バイトの枝だけが `MIN_TAIL_BYTES` の門を通っていた）。畳むか畳まない
      かは**行の並び方と無関係**に決まるべきなので、この 1 本に集約する。
    """
    return cap - int(cap * head_frac) - MARKER_ROOM


def _fold_bytes(text: str, cap: int, head_frac: float) -> tuple[str, bool]:
    """行では畳めないときの畳み方。**バイトで**先頭と末尾を残す。

    行が長すぎて 1 行も予算に入らない形（日本語の段落＝1 行 300〜400 バイト）
    でも、必ず実文字が残る。`fold_head_tail` の 2 つの枝が共有する。

    返り値: `(本文, 末尾を残せたか)`。**末尾を残せないときは畳まない**
    （`MIN_TAIL_BYTES` の説明を読むこと）。畳まない場合は先頭 `cap` バイト
    ＝`clip_bytes` と同じもので、マーカーも付けない——付けるとその分だけ
    実文字が切り捨てより減り、「畳んだのに情報が少ない」が再発する。
    切ったことを書くのは**呼び出し側の仕事**で、末尾を残せたかは
    この返り値の 2 つめで受け取る（`fold_head_tail_ex`）。
    """
    if cap <= 0:
        return "", False
    head_b = int(cap * head_frac)
    tail_b = _tail_room(cap, head_frac)
    if tail_b < MIN_TAIL_BYTES:
        # ★「末尾は残しています」と書けないので、書かない。先頭を最大限残す。
        #   （通常は `fold_head_tail_ex` の入口で既に弾かれている。この関数を
        #   直接呼ぶ経路のための二重の守り。）
        return clip_bytes(text, cap), False
    raw = text.encode("utf-8", "replace")
    cut = len(raw) - head_b - tail_b
    head = raw[:head_b].decode("utf-8", "ignore")
    tail = raw[len(raw) - tail_b:].decode("utf-8", "ignore")
    if not (head + tail).strip() and text.strip():
        # ★頭も末尾も**空白しか無い**＝本文が中央に埋もれている（2026-09-05 r4c
        #   2 巡目・査読の `atk_fold2.py` F/H/I が突いた形: 空行 300〜12,000 本 →
        #   本文 5 行 → 空行 400〜20,000 本）。ここで畳むと実文字が 0 になるが、
        #   先頭 cap バイトを取れば本文に届くことがある（cap=30,000 の実測で
        #   切り捨ては本文を含み、畳みは 0 だった＝**畳んだほうが少ない**）。
        #   行の枝と同じ基準——**使った予算ではなく残った実文字**——で決める。
        return clip_bytes(text, cap), False
    if nbytes(tail.strip()) < MIN_REAL_TAIL_BYTES:
        # ★3 巡目（must_fix#1）: 頭に本文があると上の条件は通るが、**末尾が
        #   空白しか無い**ことがある（末尾に空行が並ぶ文書）。そのまま返すと
        #   「末尾は残しています」がマーカーに入るのに末尾の実文字は 0。
        #   `head` 側と `tail` 側は**別々に**見る（合算は 0 を隠す）。
        #   ★ここが行の枝 (c) の落とし先でもあるので、**この関数が
        #   「末尾主張の真偽」の最終判定になる**（行の枝から降りてきた場合も含め、
        #   末尾に実文字が無ければ畳まず先頭を残す＝査読 must_fix#1 の結論）。
        #   ⚠ 実文字の閾値に `MIN_TAIL_BYTES`（120）は使わない。予算としての
        #     120 バイトは入口の門（`_tail_room`）で既に保証済みで、ここで再度
        #     120 を課すと cap=399（`tail_b` がちょうど 120）で文字境界の切り
        #     落とし 1〜2 バイトだけで畳めなくなる——境目が「行数非依存」でなく
        #     「文字の並び依存」に戻る。
        #   ★4 巡目（査読 must_fix#4）: だからといって **0 バイト**でもいけない。
        #     0 だと「末尾に実文字が 1 バイトでもあれば末尾主張を通す」ので、
        #     cap=399〜450（`tail_b`=120〜150）の帯で **末尾の実文字 1〜3 バイト**
        #     ——助詞や記号 1 文字——でも「末尾は残しています」と書けてしまう。
        #     マーカーに 120 バイト払って手に入るのが 1 文字では、払った意味が無い
        #     （`MIN_TAIL_BYTES` の取引の話そのもの）。`MIN_REAL_TAIL_BYTES`
        #     （40B ≒ 日本語 13 字）を下限に置く。**境目は動かない**——40 は
        #     `tail_b` の最小値 120 より十分小さいので、cap=399 で文字境界の
        #     切り落としに当たることはない（`test_総当り_畳む帯では…` が固定）。
        return clip_bytes(text, cap), False
    return (f"{head}\n… (中略: {cut:,} バイトを省きました。末尾は残しています) …\n{tail}",
            True)


def fold_head_tail_ex(text: str, cap: int, *,
                      head_frac: float = 0.4) -> tuple[str, bool]:
    """先頭と末尾を残して中央を畳む。**末尾を残せたかも返す。**

    ★末尾に多く配る（既定 6:4）。実行結果は**終わりに結論がある**——
      失敗の理由・要約・最後の行。先頭 cap バイトで切ると、そこだけが消える。

    ★2026-09-05（r4c・査読が live 知識サーバー で実測）: **行で畳む枝は、
      どの 1 行も予算に入らないとき「中略マーカーだけ」を返していた**（実文字 0）。
      行の予算は `(cap - 120) * 0.4` と `その残り`＝cap=700 なら 232 / 348 バイト
      なので、**日本語で書かれた段落 1 行（実測 357〜408 バイト）はどちらにも
      入らない**。ラボの T3/T4 ノートはまさにこの形（長い段落が 5 行）で、
      知識サーバー の引用をこの関数に替えた版は、実サーバの正答（doc
      `agent://blackwall/8f466ccdddfd4e5f`）の本文を **0 バイト**にしていた
      （切り捨て版 `clip_bytes` は 698 バイト残す＝**畳んだほうが少ない**）。
      → 行で畳んだ結果が行の予算の 3/4 も使えていないなら、バイトで畳む枝へ落とす。
        同じ実データで実文字 0 → 582 バイト（症状の先頭＋**末尾の対処**）。
      **中略マーカーだけを返してはいけない**（`test_行が全部予算超でも本文が残る`）。

    ★2026-09-05 r4c 2 巡目（査読 must_fix#1・#2 — 上の直しがまだ足りなかった）:
      ① **予算を使ったか**で行の枝を判定していた。空行が並ぶ出力（実測の形）は
         1 行 1 バイトで予算を使い切るので判定を通り、**実文字 0 のまま**
         「中略マーカーと空行だけ」を返す。判定を**実文字を残せたか**に変えた。
      ② `cap <= 200` では `tail_b` が必ず 0 になり、末尾を 1 バイトも残さずに
         「末尾は残しています」と書いていた（本番の窓で起きる:
         `LAPS_N_CTX<=16848` なら 知識サーバー の `_per` は 2 件以上で必ず 150）。
         末尾に 1 文（120 バイト）も置けない cap では**畳まない**
         （`MIN_TAIL_BYTES`）。返り値の 2 つめが False になるので、
         呼び出し側は「先頭だけ」と書ける。

    ★2026-09-05 r4c 3 巡目（査読 must_fix#1 — ②の門が**片方の枝にしか無かった**）:
      ②の門は `_fold_bytes` の中に書かれていたので、**行で畳める文書には一度も
      効いていなかった**。行の枝は自前で `tail_budget = (cap-120) - int(...)` を
      配る（同じ cap で 47 バイト多い）ので、`line0..line1999` のような短い行が
      並ぶ文書は cap=135 から畳んでマーカーを出していた——申告は「cap<399 では
      畳まない」。実測は `MIN_TAIL_BYTES` のコメント（man 形で cap 0..398 のうち
      253 個・凍結 fixture 52 文書で 20 件が切り捨てを下回る）。
      → **門を入口へ**。畳むか畳まないかは `_tail_room(cap, head_frac)` だけで
        決まり、行数にも行の長さにも依存しない。
      → あわせて、行の枝で **head 側と tail 側の実文字を別々に**見る。
        2 巡目の `real_kept` は両者を**合算**していたので、頭に本文があれば
        末尾が空行だけでも通り、「末尾は残しています」と書いていた。

    返り値: `(本文, 末尾を残せたか)`。`fold_head_tail` は 1 つめだけを返す
    後方互換の薄い包み。**小さい cap を渡す呼び出し側はこちらを使い、
    「末尾を残しています」と書かないこと**（それが今回の欠陥の本体）。
    """
    if cap <= 0:
        # ★予算 0 に「全文を返す」は上限として意味を成さない（旧実装はそうして
        #   いた）。切り捨てと同じ空文字にする。実際にこの値を渡す本番の
        #   呼び出し側は無い（bash は body_cap>=1200・知識サーバー の _per は >=150）。
        return "", False
    if nbytes(text) <= cap:
        return text, True
    if _tail_room(cap, head_frac) < MIN_TAIL_BYTES:
        # ★入口の門（3 巡目）。**行の枝もバイトの枝も通る前に**ここで決める。
        #   末尾に 1 文（`MIN_TAIL_BYTES`）も置けない cap では畳まない＝
        #   マーカーの 120 バイトを払う価値が無い。先頭を最大限残す。
        return clip_bytes(text, cap), False
    lines = text.splitlines()
    if len(lines) < 4:
        # 改行がほとんど無い（1行が巨大）。行では畳めないのでバイトで畳む。
        return _fold_bytes(text, cap, head_frac)

    marker_room = MARKER_ROOM
    head_budget = max(0, int((cap - marker_room) * head_frac))
    tail_budget = max(0, (cap - marker_room) - head_budget)

    head: list[str] = []
    used = 0
    for ln in lines:
        c = nbytes(ln) + 1
        if used + c > head_budget:
            break
        head.append(ln)
        used += c

    tail: list[str] = []
    used = 0
    for ln in reversed(lines[len(head):]):
        c = nbytes(ln) + 1
        if used + c > tail_budget:
            break
        tail.append(ln)
        used += c
    tail.reverse()

    # ★行では畳めなかった。落とす条件は2つ。
    #   (a) **head 側の実文字が 1 バイトも残っていない**（2026-09-05 r4c 2 巡目・
    #       3 巡目で head 側だけを見る形へ）。空行が並ぶ出力は 1 行 1 バイトで
    #       予算を使い切るので (b) は通る。実測（`atk_fold2.py` の F/I: 空行
    #       300 本＋本文 5 行＋空行 400 本、cap=700／空白 1 字の行でも同じ）:
    #       旧判定は「中略マーカーと空行だけ」を返していた。
    #       **使った予算ではなく、残った実文字で決める。**
    #   (b) 行の詰め込みは貪欲なので、**行が長いほど予算が余る**（1 行も入らな
    #       ければ中略マーカーだけ、頭に短い 1 本だけ入れば `` ``` `` のような
    #       無意味な 1 行だけが残る）。バイトで畳めば `cap - 120` バイトまで
    #       実文字が残るので、余りが 1/4 を超えたら落とす。
    #   実測（live 知識サーバー の T4 ノート・1 行 300〜408 バイト × 5〜6 行、cap=700）:
    #       行の枝  実文字 0〜301 バイト   バイトの枝  実文字 580 バイト
    #   短い行が並ぶ普通の出力（`line0..line1999`）は貪欲詰めで予算の 99% を
    #   使うので、この判定に掛からず行のまま畳まれる（陰性対照つき）。
    line_budget = max(1, cap - marker_room)
    head_real = sum(nbytes(x.strip()) for x in head)
    tail_real = sum(nbytes(x.strip()) for x in tail)
    if head_real == 0 or sum(nbytes(x) + 1 for x in head + tail) < line_budget * 0.75:
        return _fold_bytes(text, cap, head_frac)
    #   (c) ★3 巡目（must_fix#1）: **末尾の実文字**を head と別に見る（合算しない）。
    #       ここに来ている時点で予算（`_tail_room`）は足りているので、末尾が
    #       痩せているのは**行の詰め方**のせい——末尾に空行が並ぶ文書では
    #       `tail` が空行だけで埋まる。実測（本文 40 行＋空行 800 本）:
    #       cap=400/500/700 のすべてで tail の実文字が **0** なのに
    #       「末尾は残しています」と書いていた（2 巡目の `real_kept` は head の
    #       220B と合算するので 0 を隠す）。
    #   ⚠ **査読の指定（「clip_bytes へ」）から 1 段ずらしている。理由は実測。**
    #     指定どおり (a)(b) より**前**に置いて直接 clip へ落とすと、live 52 文書
    #     × cap 8 通り = 260 組のうち **64 組（24.6%）で末尾を失う**——うち 14 組は
    #     1 巡目の動機になった正答 `agent://blackwall/8f466ccdddfd4e5f`（日本語の
    #     長い段落は 1 行も行予算に入らないので head も tail も空＝実文字 0 で、
    #     この門に真っ先に当たる）。それは直したはずの欠陥の再発なので、
    #     **(a)(b) の後**に置き、落とし先も**まずバイトの枝**にした。バイトの枝は
    #     末尾に実文字を置けなければ自分で `clip_bytes(text, cap), False` を
    #     返すので、**結論は査読の指定と同じ**（末尾が本当に無いなら畳まない）。
    #     実測（同じ 260 組）:
    #         指定どおり(前・clip直行)  畳み 196 / 切り捨て 64 / 末尾実文字0 の主張 0
    #         この版((a)(b)の後・byte)  畳み 260 / 切り捨て  0 / 末尾実文字0 の主張 0
    #     どちらも「嘘の末尾主張」は 0。違うのは**末尾を何組取り戻せるか**だけ。
    if tail_real < MIN_TAIL_BYTES:
        return _fold_bytes(text, cap, head_frac)

    dropped = len(lines) - len(head) - len(tail)
    if dropped <= 0:
        return text, True
    dropped_chars = sum(len(x) for x in lines[len(head):len(lines) - len(tail)])
    mid = (f"… (中略: {dropped:,} 行 / {dropped_chars:,} 文字を省きました。"
           f"末尾は残しています) …")
    return "\n".join(head + [mid] + tail), True


def fold_head_tail(text: str, cap: int, *, head_frac: float = 0.4) -> str:
    """`fold_head_tail_ex` の 1 つめだけ（既存の呼び出し側のため）。

    ⚠ `cap < 399` では**畳まずに先頭 cap バイトを返す**（`MIN_TAIL_BYTES`）。
      その帯を渡しうる呼び出し側は `fold_head_tail_ex` を使い、返り値の
      2 つめで「末尾を残しています」と書いてよいかを判定すること。
    """
    return fold_head_tail_ex(text, cap, head_frac=head_frac)[0]


def min_foldable_cap(head_frac: float = 0.4) -> int:
    """**畳める最小の cap**（これ未満はどう詰めても「先頭だけ」になる）。

    ★数値を書き写さないための関数（2026-09-05 r4c 4 巡目）。head_frac=0.4 の
      いまは 399 だが、それは `MARKER_ROOM` と `MIN_TAIL_BYTES` から出る値で
      あって独立した定数ではない。この帯を気にする側（知識サーバーの引用の幅 の救済の門）
      が 399 と書き写すと、定数を触ったときに**片方だけ動く**。
    """
    cap = MARKER_ROOM + MIN_TAIL_BYTES
    # 単調（cap を増やすと `_tail_room` も増える）なので、素直に上げて最初に
    # 満たしたところが境目。上限は「増やしても満たさない」ことが無いのを示す
    # ための安全弁で、実際には 399 で抜ける。
    for _ in range(4 * (MARKER_ROOM + MIN_TAIL_BYTES) + 16):
        if _tail_room(cap, head_frac) >= MIN_TAIL_BYTES:
            return cap
        cap += 1
    return cap


def fold_head_tail_told(text: str, cap: int, *, head_frac: float = 0.4,
                        inside_cap: bool = True) -> str:
    """畳む。**畳めずに「先頭だけ」になったときも、必ず本文にそう書く。**

    ★方針①（このファイルの冒頭）を 1 値の呼び出し側でも守るための包み
      （2026-09-05 r4c 4 巡目・査読 must_fix#1）。`fold_head_tail` は
      「畳めなかったときは黙って `clip_bytes` と同じものを返す」——2 値版
      `fold_head_tail_ex` の 2 つめを見る呼び出し側（（知識サーバーの道具・公開版には無い））は
      それで書き分けられるが、1 値版しか使っていない `tools/bash.py` と
      旧い中核の部品 は**無言の切り捨て**になっていた（実機 e2e で
      6,700B → 1,200B・開示語 0）。

    `inside_cap`:
      True（既定）  印のぶんも `cap` の中で払う。返り値は必ず `cap` 以下。
                    外枠を持たない呼び出し側（引用 1 件を `room` に収める
                    旧い中核の部品 など）向け。
      False         印を `cap` の**外**に足す。外側にもう一段の上限がある
                    呼び出し側（`bash.py` は `limits.out_cap` を持つ）向けで、
                    本文を 1 バイトも削らずに開示できる。

    ⚠ 印は「中略」を含まない。畳めた場合の開示は既存の中略マーカーが担う。
    """
    out, tail_kept = fold_head_tail_ex(text, cap, head_frac=head_frac)
    if tail_kept or nbytes(text) <= cap:
        # 収まった、または畳めた（本文に中略マーカーがある）＝既に書いてある。
        return out
    mark = "\n" + HEAD_ONLY_MARK
    if not inside_cap:
        return out + mark
    room = cap - nbytes(mark)
    if room < MIN_MARKED_BODY:
        # 印のほうが本文より大きい帯。ここで印を出すと「畳んだら情報が減った」
        # の再発になるので出さない（呼び出し側の外枠で書くこと）。
        return out
    out2, tail_kept2 = fold_head_tail_ex(text, room, head_frac=head_frac)
    if tail_kept2:
        # 縮めたら畳めた（起きにくいが、起きたら中略マーカーが開示を担う）。
        return out2
    return out2 + mark


#: 端数を使い切る後追いの走査で、`nbytes` を呼んでよい回数の上限（2026-09-05・第4回b）。
#: `tests/test_refuted_defects.py::test_大きな束でも即座に返る` が仕事量を
#: 20,000 回未満に縛っており、本体の配分がすでに数千回使う。ここは 4,000 に留める。
_TOPUP_SCANS = 4000


def round_robin(groups: "OrderedDict[str, list[str]]", cap: int,
                *, reserve: int = 0) -> tuple[list[str], dict[str, int]]:
    """束ごとに公平に配る。**どの束からも最低1本は見える**ように。

    ★なぜ公平配分か: 先頭から cap 文字だと、行数の多い1ファイル
      （実測では `site/logs/server.log` の同じ警告 71 行）が枠を食い尽くし、
      他のファイルの一致が**1件も見えない**。「どこに書いてあるか」を
      知りたい問いに対して、これは答えを消している。

    ★2026-08-31 実測（レビュー D の続き・実物の Grep 結果 168 行で測った）:
      旧実装は「入らない 1 本」に当たった時点で **配分そのものを打ち切って
      いた**（`return` が内側のループの中にあった）。長い 1 行がどこに来るかは
      走査順で変わるので、**同じ 168 件の一致が、順番しだいで 16〜29 行**に
      なっていた（同じ問い・同じ木・同じ予算）。しかも打ち切るので
      予算は毎回 900〜2,400 バイト余ったまま——**公平どころか、
      たまたま長い行を持つ束が他の束を巻き添えにしていた。**
      入らない 1 本は**その 1 本だけ飛ばす**。

    返り値: (表示する行, 束ごとの省いた件数)
    """
    shown: list[str] = []
    used = reserve
    omitted = {k: len(v) for k, v in groups.items()}
    taken = {k: 0 for k in groups}
    depth = 0
    # ★2026-09-01（反証で再現）: `continue` にした版は**止まらなくなっていた**。
    #   予算が cap ちょうどで埋まらない限り、残りの全深さ × 全束を舐め続ける。
    #   実測（同じ長さの行が並ぶログ＝実在の形。`site/logs/server.log`）:
    #       50,000 行 + 小 2,000 本 → 2.88 秒 ／ 100,000 + 5,000 → 15.6 秒
    #       200,000 + 10,000       → 65 秒（表示 509 行のために CPU 65 秒。
    #                                       Grep の 30 秒上限の**外側**）
    #   旧実装（`return`）はこの暴走を偶然塞いでいた。公平さを保ったまま止めるには、
    #     ① 尽きた束を毎周 live から落とす（大きい 1 本だけが残れば周は O(1)）
    #     ② **その周に 1 本も置けなかったら終える**（残り予算にどの束の頭も入らない）
    #   ②は「もっと後ろに短い行があれば入ったかもしれない」を捨てる。
    #   ただし旧実装は最初の 1 本で全部を打ち切っていたので、**これでも厳密に多い**。
    live = [k for k, v in groups.items() if v]
    while live:
        if used >= cap:
            break                      # もう 1 バイトも入らない
        placed = False
        still: list[str] = []
        for k in live:
            items = groups[k]
            if depth >= len(items):
                continue               # この束は尽きた（次の周から見ない）
            still.append(k)
            c = nbytes(items[depth]) + 1
            if used + c > cap:
                continue               # ★この 1 本が入らないだけ。他の束は続ける
            shown.append(items[depth])
            used += c
            omitted[k] -= 1
            taken[k] += 1
            placed = True
        live = still
        if not placed:
            break
        depth += 1
    # ── 端数を使い切る 1 回だけの走査（2026-09-05・第4回b）──────────────────
    # ★上の②「その周に 1 本も置けなかったら終える」は、**もっと後ろに短い行が
    #   あれば入ったかもしれない**を捨てている（その旨は上のコメントにある）。
    #   実際に捨てていた: `tests/test_fold_fairness.py::test_予算を使い切る` は
    #   「残り予算 < 省いた行の最短」を不変条件にしており、私が bash.py に
    #   ★コメントを足して**入力（この試験は実物のソースを読む）が変わった**だけで
    #   赤くなった——残り 119 バイトに対し、まだ 110 バイトの行が入るのに止めていた。
    #   ＝順番しだいで見える量が変わる、という直したはずの性質が端数に残っていた。
    # ★暴走しない形にする。ここは **2 つの要求が正面からぶつかる**ので、両方を書く:
    #     (1) `test_予算を使い切る`（公平さ）: 端数に入る行が残っていてはいけない。
    #     (2) `test_大きな束でも即座に返る`（暴走の反証）: 予算が埋まったあとも
    #         舐め続けてはいけない——**時間ではなく仕事量**で縛られている
    #         （`nbytes` の呼び出し 20,000 回未満。時間だけ見る版は素通りする、と
    #         その試験自身が書いている）。最初に書いた「残りを全部 1 回舐める」版は
    #         (1) は満たすが (2) を破った（実測 105,001 回）。
    #   → **走査の予算を切る**。束をまたいで深さ順に見て、`_TOPUP_SCANS` 回まで。
    #     大きな束が 1 つだけ残る形（暴走の再現条件）では、深さが進んでも同じ長さの
    #     行しか出てこないので、早く打ち切っても失うものは無い。逆に実物の Grep
    #     結果のような雑多な入力では、短い行はすぐ近くに居る。
    #   ⚠ したがって (1) は**この予算の範囲での**保証であって、絶対ではない。
    #     入力が桁違いに大きいときは端数が残りうる（残りは `omitted` に出る）。
    if used < cap:
        scans = 0
        seen = set(map(id, shown))
        d = depth
        maxlen = max((len(v) for v in groups.values()), default=0)
        while d < maxlen and used < cap and scans < _TOPUP_SCANS:
            for k, items in groups.items():
                if d >= len(items) or used >= cap or scans >= _TOPUP_SCANS:
                    continue
                it = items[d]
                if id(it) in seen:
                    continue
                scans += 1
                c = nbytes(it) + 1
                if used + c > cap:
                    continue
                shown.append(it)
                used += c
                omitted[k] -= 1
                taken[k] += 1
            d += 1
    return shown, {k: v for k, v in omitted.items() if v > 0}


def omitted_note(omitted: dict[str, int], *, unit: str = "件",
                 label: str = "表示を省いた内訳", top: int = 8) -> str:
    """省いた分の内訳を1行で。**黙って消さない。**"""
    if not omitted:
        return ""
    items = sorted(omitted.items(), key=lambda kv: -kv[1])
    head = "、".join(f"{k} 他 {n:,}{unit}" for k, n in items[:top])
    more = f"、ほか {len(items) - top:,} 箇所" if len(items) > top else ""
    return f"  {label}: {head}{more}"


def coverage(total: int, shown: int, *, unit: str = "件",
             what: str = "一致", hint: str = "") -> str:
    """**何件のうち何件を見せているか**を必ず書く1行。

    ★切ったことを書かないと「これで全部だ」と読む。この現場で実際に
      起きた形（Glob が `.py` に 0 件を返し、モデルが「Python ファイルは
      存在しません」と正直に報告した——3 つあるフォルダで）。
    """
    if shown >= total:
        return f"★{what} {total:,}{unit}（すべて表示）。"
    tail = f" {hint}" if hint else ""
    return (f"★{what} {total:,}{unit}のうち {shown:,}{unit}を表示しています"
            f"（残り {total - shown:,}{unit}は省きました）。{tail}")


# ==========================================================================
# 同じ道具・同じ引数が**同じ出力**を返したときに畳む（2 回目以降だけ）
#
# ★ここに移した理由（2026-09-02・8 走行 10,904 件の実測）:
#   「道具＋引数＋本文がバイト単位で完全一致した反復」は 2,397 件 = 22.0%
#   （2.3 MB）で、内訳は Read 1,438/3,402・SystemState 132/192・
#   Glob 129/369・Bash 473/3,614。ところが畳みは **Bash にしかなかった**。
#   ＝この現場の持病「片方で直して片方に残す」がそのままの形で出ていた。
#
#   実際に本番のこの関数を 8 走行に掛け直した結果（走行ごとに記憶を消す）:
#       Glob        57 件 / 331,453 B 節約
#       SystemState 70 件 /  21,146 B 節約
#   合計 352,599 B ≒ 44 KB/走行。この現場は**常時圧縮**（整理直前の会話が
#   中央値 20,600 トークン・窓 32,768）なので、道具が返す量を減らすことは
#   圧縮で失われる量をそのまま減らすことに等しい。
#
# ★Read には掛けない（測って**やめた**）。畳めた Read と直前の同一 Read の
#   間隔は中央値 24 呼び出し・p90 137 で、連続はほぼ無い（1,179 件中 3 件）。
#   常時圧縮の窓では「その間に元の全文が落ちている」ことをほぼ意味する
#   ＝再読の目的が**文脈から消えた中身の取り戻し**なので、畳むと中身に
#   二度と戻れない（同じファイル名では回避できない。Bash と違う）。
#   安全にするには「直前の全文がまだ窓に残っているか」を見る文脈認識が要る。
#   無エラーのソース再読 605 件 / 555,720 B が危険母数。→ 別案件。
# ==========================================================================
import hashlib as _hashlib
import re as _re
import threading as _threading

#: 走行の間だけ覚える「同じ道具・同じ引数が返した出力」。
#: key = (道具名, 引数の署名, 作業場) → (sha1, バイト数, 回数)
#: ★道具名を鍵に含める。含めないと Bash の `ls` と Glob の `*` が同じ鍵で
#:   ぶつかり、片方の出力を「前回と同じ」と誤って畳む。
_OUT_SEEN: dict[tuple[str, str, str], tuple[str, int, int]] = {}
_OUT_SEEN_N = [0]
#: 道具は並行に呼ばれる（Glob / SystemState は readonly・parallel_safe）。
_REPEAT_LOCK = _threading.Lock()

#: これより短い出力は畳まない（畳んだ文のほうが長くなる）。実測で決めた値。
REPEAT_MIN_BYTES = 200

#: 「失敗の理由」らしい行。畳むときも**これだけは必ず見せる**（2026-09-01）。
#: ★語ではなく**形**で拾う: 実機の出力は日本語・英語・PHP の HTML 混じり。
ERRISH = _re.compile(
    r"\b(Warning|Notice|Fatal error|Parse error|Deprecated|Traceback|"
    r"Error|error:|ERROR|Exception|No such file|not found|Permission denied|"
    r"command not found|cannot|failed|FAILED)\b")


def repeat_key(tool: str, args: str, cwd: str | None) -> tuple[str, str, str]:
    return (str(tool), " ".join(str(args or "").split())[:400], str(cwd or ""))


def fold_repeat(tool: str, args: str, cwd: str | None, raw: str, out: str,
                *, what: str = "このコマンド") -> str:
    """同じ道具・同じ引数が**同じ出力**を返したら、本文を落として指し示す。

    ★本文を消してよい理由は「もう会話に載っているから」ではない
      （載っていた分は文脈の整理で落ちているかもしれない）。**同じ呼び方を
      もう一度すれば同じものが出ると分かっている**からで、その旨と再取得の
      方法を本文に書く。指し示すだけで黙って消さない。

    ★2 回目以降だけを畳む。初回は必ず全文（初回まで畳むと、その呼び出しの
      出力を一度も見られない走行ができてしまう）。

    ★**失敗の理由を残す**（2026-09-01・run44 の解剖で判明した実害）。
      run44 の工程4 は 3 試行 3 時間すべてが `json.tool` の 1 本で落ちたが、
      その原因である `Undefined array key` を**モデルは 4 回しか見ていない**。
      畳んだ 39 件は 0 件が警告を含まず、末尾（＝正しい JSON）だけを見せていた。
      ＝**同じコマンドを 197 回打った**のは、直す手がかりを道具が隠したから。
      文脈を節約する仕組みが、節約の対象でないもの（失敗の理由）まで削っていた。

    引数:
      tool  道具名。鍵の名前空間。他の道具の反復と混ざらないために要る。
      args  引数の署名（Bash はコマンド、Glob は path|pattern、
            SystemState は verb）。
      raw   同一判定に使う**畳む前**の出力（畳んだ後で比べると、省いた中央
            だけが変わった回を「同じ」と誤判定する）。
      out   実際に返す本文。
    """
    if nbytes(out) < REPEAT_MIN_BYTES:
        return out
    key = repeat_key(tool, args, cwd)
    sig = _hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()
    with _REPEAT_LOCK:
        prev = _OUT_SEEN.get(key)
        _OUT_SEEN_N[0] += 1
        if prev is None or prev[0] != sig:
            # ★回数は **1 から**（2026-09-02・移設中に見つけた既存の欠陥）。
            #   旧実装はここに `_OUT_SEEN_N[0]`＝**畳み関数が呼ばれた通算回数**を
            #   入れていた。同じコマンドしか打たない試験では通算＝回数なので
            #   気づけないが、本番では 50 回目の Bash で初めて反復した瞬間に
            #   「★このコマンドは同じ結果を **51 回**返しています」と出る。
            #   1 回しか返していないのに。**間違った助言は、助言が無いより悪い**
            #   （行き詰まりの合図を、行き詰まっていない場で出す）。
            #   道具をまたいで数える今の形では、これは必ず毎回起きる。
            _OUT_SEEN[key] = (sig, nbytes(raw), 1)
            return out
        _, size, count = prev
        count += 1
        _OUT_SEEN[key] = (sig, size, count)
    # ★指し示す文そのものが長いと、短い出力では**畳んだほうが増える**。
    #   実測: 旧案の 397 バイトでは Bash の反復 458 件のうち 69 件しか
    #   畳めなかった（中央値 154 バイト・平均 309 バイトなので、下限 400 に
    #   届かない）。文を 1/4 に縮めて下限を 200 バイトへ下げたら 274 件に増えた。
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    head = lines[0][:120] if lines else ""
    tail = lines[-1][:120] if len(lines) > 1 else ""
    err = next((ln.strip()[:160] for ln in out.splitlines()
                if ERRISH.search(ln)), "")
    bits = []
    if err:
        bits.append("／★" + err)      # 失敗の理由があるなら、それを最優先で見せる
    if head:
        bits.append("／先頭: " + head)
    if tail:
        bits.append("／末尾: " + tail)
    msg = f"(前回と同じ出力: {size:,} バイト。省きました{''.join(bits)})"
    if count >= 3:
        # 3 回目からは**行き詰まりの場**とみなして次の一手を言う。
        # 説明文に書いても従わないが、結果の中に置くと効く（この現場の実測）。
        msg += (f"\n  ★{what}は同じ結果を {count} 回返しています。"
                f"繰り返しても変わりません。別の手を打ってください。")
    # ★畳んだ結果が元より長いなら畳まない。**削るつもりで増やさない。**
    if nbytes(msg) < nbytes(out):
        # ★発火の計器（2026-09-02 検査係の提案）。再生では 17 件/走行が保証されるので、
        #   本番の counters が 0 なら配線ミスと即断できる。親 Agent は contextvar から。
        try:
            from . import CURRENT_AGENT
            _ag = CURRENT_AGENT.get()
            if _ag is not None and hasattr(_ag, "counters"):
                _ag.counters["fold_repeat_fired"] = _ag.counters.get("fold_repeat_fired", 0) + 1
        except Exception:
            pass
        return msg
    return out


def reset_repeat_memory() -> None:
    """試験・走行の切り替え用。走行をまたいで覚えたままにしない。"""
    with _REPEAT_LOCK:
        _OUT_SEEN.clear()
        _OUT_SEEN_N[0] = 0
