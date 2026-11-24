"""生成が**同じ塊を繰り返し始めた**ことを、その場で見つけて切る。

★なぜ要るか（2026-09-21・オーナーが実機の画面を貼って報告）:
  同じ段取りの段落を **10 回以上そのまま繰り返し**、答えに着地しないまま
  **思考 26,015 トークン**を溶かした走行があった。利用者に見えたのは、
  同じ文章が延々と続く画面だけ。

★既存の仕掛けでは止まらない。理由を分けて書く:
  ・`repeat_penalty` の窓 `repeat_last_n` は **64 トークン**＝段落（120+ トークン）は窓外。
    窓を広げるのはサンプラの調整で、`tool_call` 率を落としうる（旧記録）。ここは触らない。
  ・道具の反復検知（`_loop_norm_key`）は **道具名＋引数**で数える。この走行は
    引数が毎回違った（Windows / Win64 / ThirdParty / UnrealPak*）ので一度も発火しない。
  ・そもそも今回の反復は**道具ではなく生成そのもの**。見る場所が違う。

⇒ ここは**出てくる字だけ**を見る。原因（モデル・課題・道具）に依らない。

★誤爆をどう避けるか:
  ・塊が `MIN_BLOCK` 文字以上のときだけ見る（短い繰り返しは箇条書きや表で普通に出る）
  ・**3 回連続**で完全に同じときだけ
  ・切ったら**1 回ぶんは残す**（利用者が何を繰り返したか読めるように）

★**この検知器が見ないもの**（自分の試験に落とされて分かった。黙って伏せない）:
  ・**長さが違う繰り返し**。固定長で切って比べるので、回ごとに空白や語が
    1 つでも増減すると一致しない。温度 0.05 の実機は逐語で繰り返すので実害は薄いが、
    高い温度では**取り逃す**。
  ・間に別の文が挟まる「A B A B A」型。
  ・道具の反復（そちらは `agent._loop_norm_key` の担当）。
"""
from __future__ import annotations

import re

#: 繰り返しと見なす最小の塊。日本語 120 字は段落 1 つぶん。
MIN_BLOCK = 120
#: 何回続いたら切るか。2 回は推敲でも起きる。3 回は事故。
TIMES = 3
#: 末尾のどこまでを見るか（長くしても得は無く、毎回の費用だけ増える）。
TAIL = 6000

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def plan_key(s: str) -> str:
    """反復をまたいで「同じ段取りか」を見るための鍵。**空白を全部落とす**。

    ★`_norm` とは別物にした（2026-09-21・自分の試験に落とされて分かった）。
      `_norm` は空白の**連なり**を 1 つに畳むだけなので、`。` の後に空白が
      1 つ増えただけで別物になる。`looping_block` は等しい長さで切って比べるので
      それでよいが、こちらは**等値の鍵**なので、もっと寛容でよい。
    ★英語は語が繋がるが、鍵として使うだけなので支障は無い。
    """
    return _WS.sub("", s)


def looping_block(text: str) -> str | None:
    """末尾で同じ塊が `TIMES` 回続いていれば、その 1 回ぶんを返す。無ければ None。

    ★末尾だけを見る。途中で 3 回繰り返して抜け出した文章を切ると、
      **直っている走行を殺す**（今まさに詰まっているかどうかが知りたい）。
    """
    if not text:
        return None
    tail = text[-TAIL:]
    n = len(tail)
    # 塊の長さを長いほうから試す（長い周期のほうが本物の反復）
    for p in range(min(n // TIMES, TAIL // TIMES), MIN_BLOCK - 1, -1):
        last = tail[n - p:]
        if _norm(last) == "":
            continue
        ok = True
        for k in range(2, TIMES + 1):
            prev = tail[n - k * p: n - (k - 1) * p]
            if _norm(prev) != _norm(last):
                ok = False
                break
        if ok:
            return last
    return None


def cut_loop(text: str) -> tuple[str, str | None]:
    """繰り返していれば **1 回ぶんを残して切り**、(切った本文, 注記) を返す。

    切らないときは (元の本文, None)。注記は呼び出し側が本文の末尾に足す。
    """
    blk = looping_block(text)
    if blk is None:
        return text, None
    # 末尾の (TIMES-1) 回ぶんを落とす＝1 回だけ残る
    cut = text[: len(text) - len(blk) * (TIMES - 1)]
    # ★2026-10-05: 注記は事実だけにした（オーナー指示「モデルの動きを制限する機能は削除」）。
    #   旧文は「別の手に替えるか、できないことは『できない』と言って」と次の方針まで指図していた。
    note = ("\n\n★同じ内容が続いたので、生成をここで切りました"
            f"（{len(blk)} 字の塊が {TIMES} 回続いた・1 回ぶんは残してあります）。\n")
    return cut, note


# ── 思考の中の堂々巡り（同じ行が言い回しを変えて戻ってくる）───────────────────────
# ★2026-10-11 の見回り（オーナーの走行 0.1.387・61 分）: 繰り返しを切られた手番 5 回が走行の 60%（2,196 秒）を使った。
#   2 回は 1 手番 12 分（思考 34,555 字・36,184 字）。思考は「Wait, I'll search for X. / Actually, I'll search for X.」と
#   頭の語だけ替えて往復していて、上の looping_block（同じ塊が 3 回続けて逐語で並ぶ）は終わり近くまで当たらなかった。
# ★見るのは思考だけ・思考が THINK_MIN 字を超えてから（ふだんの思考は数千字で終わる）。末尾 THINK_SPAN 字の中で、
#   頭の語（Wait・Actually・Hmm・OK…）を落とした同じ行（THINK_LINE 字以上）が THINK_TIMES 回出たら堂々巡り。
# ★実測（3 走行・思考のある手番 130）: 繰り返しを切られた 4 手番で 7,200 / 12,800 / 13,600 / 15,600 字目に当たる
#   （旧は 28,800〜35,600 字目）。ほかの手番では 1 度も当たらない（4,000 字未満は見ないので、短い手番には関わらない）。
THINK_MIN = 4000
THINK_SPAN = 1500
THINK_LINE = 25
THINK_TIMES = 3
_LEAD = re.compile(r"^(?:(?:wait|actually|hmm+|ok(?:ay)?|so|well|alright|right|no|yes|but|and|then|also|let me think|"
                   r"let's see|ah|oh)[,.!:]?\s+)+", re.I)


def _line_key(line: str) -> str:
    k = _WS.sub(" ", line).strip().strip("*-•> ").strip()
    return _LEAD.sub("", k).rstrip(".。…").lower()


def think_circle(think: str) -> str | None:
    """思考が堂々巡りしていれば、その行を返す。していなければ None。"""
    if len(think) < THINK_MIN:
        return None
    keys = [_line_key(x) for x in think[-THINK_SPAN:].split("\n")]
    seen: dict[str, int] = {}
    for k in keys:
        if len(k) >= THINK_LINE:
            seen[k] = seen.get(k, 0) + 1
            if seen[k] >= THINK_TIMES:
                return k
    return None


# ── 同じ思考の 2 回目（2026-10-12 オーナー「二回同じ思考が続いたら次に進めるように」→ 同日「一個前の思考と 100% 同じ思考だった
#    ときのみ止める」）── 直前の段落（段落が無ければ行）とまったく同じ物が続いたら切る（think_dup）。手番をまたいでは、前の手番の
#    思考を最初から最後までそのまま繰り返したら切る（same_as_before）。離れた所の同じ行・同じ短い決めでは切らない
#    （同じ行が 3 回の think_circle と、同じ塊が 3 回続く cut_loop は別の判定として残す）。
DUP_MIN = 300
DUP_LINE = 25
DUP_PARA = 60
DUP_UNIT_MIN = 10                  # これより短い考え（「Wait.」など）は続いても数えない


_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)


def think_dup(think: str) -> str | None:
    """直前の考え（段落・段落が無ければ行）とまったく同じ考えが続いたら、その文を返す。続いていなければ None。
    ★2026-10-12 オーナー「同じ考えを繰り返したの条件として、一個前の思考と 100% 同じ思考だったときのみ止めるようにしましょうか」:
      それまでは思考のどこかに同じ段落・同じ行（25 字以上）が 2 回出れば切っていた（直す前後のコードの見比べで誤って切れた）。
      いまは直前と 100% 同じ（前後の空白だけ除く）が続いたときだけ。コードの塊（``` で囲んだ部分）は数えない。"""
    think = _FENCE.sub("\n\n", think)
    paras = [p.strip() for p in re.split(r"\n\s*\n", think) if p.strip()]
    units = paras if len(paras) >= 2 else [x.strip() for x in think.split("\n") if x.strip()]
    # 最後の単位は書きかけかもしれない＝前と完全に同じになった時点で当たる（書きかけの途中では当たらない）
    for a, b in zip(units, units[1:]):
        if len(a) >= DUP_UNIT_MIN and a == b:
            return _line_key(a)
    return None


def same_as_before(think: str, prev: str) -> bool:
    """前の手番の思考を、最初から最後まで 100% 同じに繰り返した（前後の空白だけ除いて、今の思考が前の思考で始まり、前の思考の長さに届いた）。
    ★2026-10-12 オーナー（think_dup の注記）: それまでは今の思考の頭 400 字が前の思考の中にあれば切っていた。"""
    if not prev:
        return False
    a, b = think.strip(), prev.strip()
    return len(b) >= DUP_MIN and a.startswith(b)


# ── 決めかけては言い直す・一度決めたら道具へ（2026-10-12〜13）── 外した ──────────────
# ★2026-10-13 オーナー「一度決めたら考え直さずに道具を呼ばせる この仕組みを消しましょう。モデルには自由に考えさせます」。
#   PC の走行で、考え直しを切った 2 回（14:19・14:31）は、どちらも「参照先のファイル（resources.resource）が無い、どこにあるのか
#   探すべきだ」と気づき始めたところだった。正しい道に戻りかけた考え直しを、この仕組みが止めていた。
#   思考を切るのは、同じ文字列をそのまま繰り返したとき（think_circle・think_dup・same_as_before）と崩れ（degenerate_tail）だけ。


# ── 崩れ（短い周期の繰り返し）──────────────────────────────────────────
# ★2026-10-10（オーナー「なんかおかしくなってます」・推論機を Gemma 4 に替えた直後）: 手番の 1 語目から
#   `的に_v1.ss_edit_v1.ss_ss_ss_ss_ss…` と崩れ、オーナーが止めるまで 3,000 語流れた。上の looping_block は
#   120 字以上の塊が 3 回続く形しか見ないので、3 字周期の崩れは一度も引っかからない。
# ★見るのは本文と思考の字だけ（道具の引数は別の道を流れる＝Write で 0 を並べたファイルを書いても当たらない）。
#   空白を除いて末尾 DEGEN_SPAN 字が DEGEN_MAX_PERIOD 字以下の単位の繰り返しなら「崩れ」。散文・コードの説明では
#   まず起きない（表の区切り `|---|` や罫線は数十字で終わる）。
DEGEN_SPAN = 360
DEGEN_MAX_PERIOD = 16
#: 「ほぼ繰り返し」とみなす一致の割合。崩れた実物（2026-10-10）は `_ss` の連なりに `prog`・`Live2D` などが
#: 時々挟まり、ぴったりの繰り返しでは当たらなかった。散文はずらしの一致が 3 割に届かない。
DEGEN_RATIO = 0.9


def degenerate_tail(text: str) -> str | None:
    """末尾が短い単位の繰り返しに崩れていれば、その単位を返す。無ければ None。

    空白を除いた末尾 DEGEN_SPAN 字について、DEGEN_MAX_PERIOD 字以内のずらし p で `s[i] == s[i-p]` となる位置が
    DEGEN_RATIO 以上なら崩れ（ほぼ p 字ごとの繰り返し）。"""
    return _short_period(text) or _long_period(text) or _word_salad(text)


# ── 崩れ（長い単位の繰り返し）──────────────────────────────────────────
# ★2026-10-13（オーナー「エージェントの内部思考が途中でおかしくなったようです」）: Bash の引数（grep の正規表現の選択肢）が
#   31 字の単位をそのまま繰り返し始め、4 万字流れて生成の上限で切れた。上の判定は 16 字までの単位しか見ないので当たらない
#   （実物には 7,959 字を丸ごと渡したときに _short_period の割合で偶然当たっただけ）。
# ★LONG_REPS 回以上ぴったり同じ単位が続いたら崩れ。散文・説明には同じ 17 字以上の塊が 8 回並ぶことはまず無い
#   （120 字以上の塊の 3 回は looping_block が見る）。
LONG_MAX_PERIOD = 200
LONG_REPS = 8
LONG_MIN = 480


def _long_period(text: str, min_len: int = LONG_MIN, low: int = DEGEN_MAX_PERIOD + 1) -> str | None:
    """末尾（空白を除く）が low〜LONG_MAX_PERIOD 字の単位のぴったりの繰り返しなら、その単位を返す。
    繰り返しの長さは max(LONG_REPS 回, min_len 字)。"""
    if not text or len(text) < min_len:
        return None
    s = _WS.sub("", text[-max(LONG_MAX_PERIOD * LONG_REPS, min_len) * 2:])
    for p in range(low, LONG_MAX_PERIOD + 1):
        n = max(LONG_REPS * p, min_len)
        if len(s) < n:
            break                         # n は p とともに増えるだけ
        if s[-n:-p] == s[-n + p:]:
            return s[-p:]
    return None


# ── 道具の引数の崩れ ─────────────────────────────────────────────────
# ★2026-10-13: 上の実物は道具の引数の中で起きた。本文と思考は backend の逐次の検査を通るが、引数は別の道を流れて
#   一度も見られていなかった。引数は 1 字から 200 字までの単位の、ぴったりの繰り返しだけを見る（ずれを許すと
#   表やデータを書く Write に当たる）。中身を書く道具（Write など）は、正当な繰り返し（0 で埋めたデータ・同じ行の
#   並ぶ表）がありうるので長さの下限を上げる。
ARGS_MIN = 600
ARGS_MIN_CONTENT = 4000
CONTENT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Doc", "TodoWrite"})


def degenerate_args(name: str, args: str) -> str | None:
    """道具の引数（JSON の文字列のまま）の末尾が同じ単位の繰り返しに崩れていれば、その単位を返す。"""
    floor = ARGS_MIN_CONTENT if (name or "") in CONTENT_TOOLS else ARGS_MIN
    return _long_period(args, min_len=floor, low=1)


def _short_period(text: str) -> str | None:
    if not text or len(text) < DEGEN_SPAN:
        return None
    s = _WS.sub("", text[-DEGEN_SPAN * 3:])
    if len(s) < DEGEN_SPAN + DEGEN_MAX_PERIOD:
        return None                       # ほとんど空白（字下げ）＝判定しない
    span = s[-(DEGEN_SPAN + DEGEN_MAX_PERIOD):]
    for p in range(1, DEGEN_MAX_PERIOD + 1):
        same = sum(1 for i in range(len(span) - DEGEN_SPAN, len(span)) if span[i] == span[i - p])
        if same >= DEGEN_RATIO * DEGEN_SPAN:
            return span[-p:]
    return None


# ── 崩れ（文脈の語の切れ端の寄せ集め）──────────────────────────────────
# ★2026-10-10 2 回目（0.1.353 の守りの後・オーナー「治らなかった」）: 1 語目から
#   `なる_ver_ver_ver_files_files_Build_Build_Ext_Ext_一/ 一/_Ext_assets_Ext_assets…`（760 語・会話の中のパスの切れ端）。
#   周期が揃わないので上の判定には 1,071 字のどこでも当たらなかった。
# ★形の特徴は 3 つ: 空白がほとんど無い・英字の語が少ない種類で何度も出る・同じ語が隣り合う（`ver_ver`）。
#   末尾 SALAD_SPAN 字で 3 つを見る。実物 2 本はどちらも最初の 320 字で当たり、開発機 の過去 400 走行の本文と思考
#   5,530 箇所では 0 回（パスの一覧・表・16 進の羅列・base64・日本語の説明・CSV でも当たらない）。
SALAD_SPAN = 320
SALAD_MIN_WORDS = 24
SALAD_WS_MAX = 0.03          # 空白の割合の上限（パスの一覧や表は改行と空白でここを超える）
_SALAD_WORD = re.compile(r"[A-Za-z]{2,}")


def _word_salad(text: str) -> str | None:
    """末尾が語の切れ端の寄せ集めに崩れていれば、出てくる語を `_` でつないで返す。無ければ None。"""
    if not text or len(text) < SALAD_SPAN:
        return None
    t = text[-SALAD_SPAN:]
    if sum(1 for c in t if c.isspace()) > SALAD_WS_MAX * SALAD_SPAN:
        return None
    words = [w.lower() for w in _SALAD_WORD.findall(t)][1:]     # 先頭の語は窓の端で切れている（`Ext`→`xt`）
    n = len(words)
    if n < SALAD_MIN_WORDS:
        return None
    kinds = set(words)
    twins = sum(1 for a, b in zip(words, words[1:]) if a == b)
    if len(kinds) <= max(4, 0.1 * n) or (twins >= 0.3 * (n - 1) and len(kinds) <= 0.25 * n):
        return "_".join(sorted(kinds))[:40]
    return None


# ── `<br>` の繰り返し ─────────────────────────────────────────────────
# ★2026-10-10: Gemma 4 は本文を書いた後に道具を呼ぼうとして、`<|tool_call>` の代わりに `<br>` を挟むことがある
#   （オーナーの崩れた地点から 34 回書かせて 6 回）。一度出ると `<br>\n<br>\n…` の繰り返しに入り、上の判定に当たるまで
#   数百字かかっていた（実物は 129 回）。`<br>` だけの行が BR_RUN 回続いたら崩れとみなす。
BR_RUN = 4
_BR_TAIL = re.compile(r"(?:<br\s*/?>[ \t]*\n?\s*){%d,}$" % BR_RUN, re.I)
_BR_LINE = re.compile(r"^[ \t]*(?:<br\s*/?>[ \t]*)+$\n?", re.I | re.M)


def br_run(tail: str) -> bool:
    """末尾が `<br>` の連なり（BR_RUN 回以上）か。"""
    return bool(tail) and bool(_BR_TAIL.search(tail))


def strip_br_lines(text: str) -> str:
    """`<br>` だけの行を消す（Gemma が道具の前に挟む区切り。会話に残すと次の手番がまねる）。"""
    if not text or "<br" not in text.lower():
        return text
    return _BR_LINE.sub("", text).rstrip()



# ── 切ったときの注記を、モデルへ送り返す思考から外す（2026-10-14）──
# 推論機の側で思考を切ると（同じ行の繰り返し・直前と同じ考え・前の手番と同じ思考・長すぎる思考）、backend は画面のために
# 「★…ここで切りました（同じ行が 3 回: '…'）。」を思考の流れに足す。その思考は手番をまたいでモデルへ送り返される
# （Qwen3.6 の preserve_thinking・Gemma の task 基準）ので、注記が①繰り返していた行をもう一度モデルに読ませ、
# ②生成した文字列と送り返す文字列が食い違って推論機の読み込みの使い回しがそこで切れていた。切ったことは次の
# [Harness] の一言（英語）が伝えるので、送り返す思考は生成したとおり（切った所まで）に戻す。画面と記録には注記が残る。
_CUT_NOTE = re.compile(r"\n*★(?:直前とまったく同じ考えが続いたので|前の手番の思考をそのまま繰り返したので|思考が長くなりすぎたので"
                       r"|考えが同じ所を回り始めたので)[^\n]*(?:\n|$)")


def strip_cut_notes(text: str) -> str:
    """思考から、backend が足した「ここで切りました」の注記を外す（無ければそのまま）。"""
    if not text or "★" not in text:
        return text
    return _CUT_NOTE.sub("\n", text).rstrip()
