"""送信するビューだけを削る。記録（session.messages）には触らない。

n_ctx を一度も見ずに transcript を毎ターン丸ごと再送していた。実測では
`agent.py` `serve.py` `policy.py` の3本を Read しただけで 37,214 tok、固定接頭辞
5,214 tok を足して 42,428 > 32,768 ＝ **3ファイル読むだけで死ぬ**。しかも死に方が
悪い: llama-server は 400 を返し、backend.py の retry 節がそれを 4 回投げ直して
11.8 秒を捨て、agent.py のフォールバック条件（closed/EOF/502/503）に掛からないので
**それまでに集めた全ツール結果を捨てて `[backend error]` を返す**。

ここでやらないこと:

* **要約しない。** ★2026-08-19 訂正: ここは長らく「弱いモデルの自己要約は当てにならない
  （bench 51/60 → 23/60）」と書いていたが、**その数字は自己要約の実験ではない**。
  実体は**ペルソナに `[ACTION]/[RESULT]` の出力様式を足した**ときの結果で、
  モデルが道具を呼ばずに枠を埋めた（`incidents/2026-08-06-output-format-caused-fabrication.md`）。
  ＝**自己要約については、効くとも効かないとも測っていない。**
  それでも要約しない方針は保つ: 要約は**取り違えても気づけない**形で情報を失い、
  こちらには元と突き合わせる手段が無い（中略なら「取り直せ」と言える）。
  ★入れるなら A/B を組んでからにすること。足りない分は
  「取りに行かせる」で埋める＝中略の跡に *取り直し方* を書く。
* **窓をスライドさせない。** 前方が変わると接頭辞キャッシュが全部無効になる。
  自測: 末尾追記 0.10s に対し先頭書き換え 26.00s（**260倍**）。だから削るのは稀に、
  深く、そして一度決めた段は緩めない（`min_level` の単調性）。

落とすものと、落とすと壊れるもの:

| 帯 | 落とすもの                     | 失うもの                     |
|----|--------------------------------|------------------------------|
| 0  | 何もしない                     | ―                            |
| 1  | 出力が完全に同一の再取得       | なし（先の番号を指すだけ）   |
| 5  | 古い道具呼び出しの**引数**     | 何を書いたか（結果と実物は残る） |
| 2  | 巨大なツール結果の中央部       | 中央の行（頭と尾は残る）     |
| 6  | ハーネスが書いた依頼文の低優先節 | 台帳・成果物一覧（実体は残る） |
| 3  | 失敗したツール結果の本文       | traceback（1行目は残る）     |
| 4  | 中間の assistant 散文          | 語り（元のツール結果は残る） |
| 7  | 最深段でだけ・成功した結果の全文 | 頭/尾の行（文字数・先頭/末尾の短い抜粋・取り直し方は残る） |

★帯5（2026-08-29 追加）が最大の穴だった。native tool-calling では道具の**呼び出し**は
  `content` ではなく `tool_calls` に乗るのに、**梯子のどの段もこの欄を見ていなかった**。
  実トレース 4 本（run29/31/32/34・12 試行）で Edit の引数だけで推定 506,596 トークン
  ＝道具呼び出し引数の 64.3%。一度書かれたら二度と縮まないまま最後まで居座る。
  → 引数は JSON のまま値だけ詰める（キーと構造は保つ＝テンプレートを壊さない）。
  失うのは「どう書いたか」だけで、**書いた結果はファイルにあり、結果の行も残る**。

★帯6（同）: `role == "user"` は「小さいので削る意味が無い」という理由で全段保護
  されていたが、工程プロンプトは user メッセージで、台帳・成果物一覧・失敗の証拠が
  育って最大 13,000 字＝作業文脈の 1/3 を**保護されたまま**占めていた。
  ハーネスが自分で書いた依頼文（工程プロンプト・仕切り直しの覚え書き）だけを、
  **節の優先順位つきで**段階的に落とす。人が書いた依頼文には触らない。

★**絶対に落とさないもの**（どの段でも触らない）:
  - 人が書いた `role == "user"`（最初の目標・差し戻し・reset。ここを削ると
    `_completion_objection` が突き返した理由が消えて同じ失敗を繰り返す）
    ＝ハーネス自身が書いた依頼文（`HARNESS_USER_MARKS`）だけが帯6の対象
  - 直近 `keep_last` 本の `role == "tool"`（直前に読んだファイルの中身。ここを
    削ると「読んだはずのものが無い」状態になり、モデルは埋め合わせに嘘をつく）
  - 直近の `role == "assistant"`
  - `messages[:frozen_upto]`（呼び出し側が固定したい先頭）

★evidence 番号は決して振り直さない。`grounding.check` は `[n]` で本文を引くので、
番号がずれると grounding と stub が同時に、静かに壊れる。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import httpx


def _trust_env() -> bool:
    """推論機（LAN/ローカル）へは OS の HTTP(S)_PROXY を使わない（backend.py と同じ理由）。"""
    import os
    return (os.environ.get("LAPS_HTTP_TRUST_ENV", "") or "").strip().lower() not in ("0","false","no","off")



# --------------------------------------------------------------------------
# 予算
# --------------------------------------------------------------------------

#: 予算のこのぶんを超えたら削り始める（**箍**。下の絶対トークンが主で、これは
#: 窓が極端に狭い/広いときに絶対値が暴れないための上下限）。
HIGH_WATER = 0.75
#: 削り始めたら、ここまで落として止める（ヒステリシス）。毎ターン境界で削ると
#: 前方書き換えが毎ターン起きて 50 ターンで +20〜35 分になる。
LOW_WATER = 0.50

#: ★2026-08-29: 水位を**割合ではなく絶対トークン**で決め直した。
#:
#: 商用ハーネスの数字（200K 窓に対する reserve 16,384 等）は移さない。ここは
#: **32K の実機**で、意味のある単位は「道具の結果 1 本」である:
#:   `tools/limits.out_cap()` = min(道具ごとの上限, n_ctx//4*2) は n_ctx=32,768 で
#:   **16,384 字**に収束する（Read/Bash/Grep すべて）。実トレースから回帰した
#:   0.34 トークン/バイトで **約 5,600 トークン ＝ 大きい結果 1 本**。
#: → 「大きい結果 1 本ぶんの余地を残して削り始め、2 本ぶん残るまで削る」。
#:
#: 32,768 窓・固定接頭辞 5,311・生成 4,096・slack 1,024 → 予算 22,337 のとき
#:   高 16,767（旧 16,753）／低 11,197（旧 11,168）＝**ほぼ同値**。
#:   ＝この直しは 32K の挙動を変えるためではなく、**根拠を割合から絶対量へ
#:     置き換える**ためのもの。効くのは窓が変わったとき:
#:       16,384 窓（予算 ~6,000）: 高 4,500→3,300・低 3,000→2,100（結果 1 本
#:         2,785 トークンが予算の半分近くを占めるので、早く深く削るのが正しい）
#:       131,072 窓（予算 ~60,000）: 高 45,000→54,000・低 30,000→48,500
#:         （割合だと窓が広いほど**理由なく捨てる**。絶対量なら捨てない）
#: 残す「結果の本数」。1 本残して削り始め、2 本ぶん残るまで削る。
RESERVE_HIGH_RESULTS = 1
#: ★2026-09-10 オーナー指示「文脈圧縮が頻繁すぎる」で 2→3。
#   **発火点（high）は上げない**（上げると窓が溢れて走行が死ぬ＝もっと悪い）。
#   代わりに**削るときに深く削る**（low を下げる）ので、再発火までの余白が広がり、
#   圧縮の回数が減る。下限 0.35*limit があるので削りすぎにはならない（前方書き換えは高コスト）。
RESERVE_LOW_RESULTS = 3
#: 実トレース 4 本（n=5,283 ペア・r=0.943）から回帰した バイト→トークン。
#: `approx_tokens` の素の上界（1.0）は 3〜5 倍過大で、予約の見積りには使えない。
TOKENS_PER_BYTE = 0.34


def max_result_tokens(n_ctx: int) -> int:
    """この窓で道具 1 本が返しうる最大量（トークン）。

    ★式は `tools/limits.out_cap()` と同じ（`max(4000, min(上限, n_ctx//4*2))` 字）。
      予約を定数で持つと、窓が変わったときに**必ずどちらかに外れる**——
      20K 窓では 11,500 の予約が予算の 7 割を食い、64K 窓では足りない。
    """
    chars = max(4000, min(16384, max(0, int(n_ctx)) // 4 * 2))
    return int(chars * TOKENS_PER_BYTE)


def water_marks(limit: int, n_ctx: int | None = None) -> tuple[int, int]:
    """予算 → (削り始める水位, 削り終える水位)。

    絶対トークン（結果 n 本ぶん）を、旧来の割合（0.75/0.50）の前後で挟む。
    箍が要るのは、窓が極端に狭いと予約が予算を食い切り、極端に広いと
    予約が誤差になるから。
    """
    limit = max(0, int(limit))
    one = max_result_tokens(n_ctx if n_ctx is not None else limit)
    high = max(int(0.55 * limit),
               min(int(0.90 * limit), limit - RESERVE_HIGH_RESULTS * one))
    low = max(int(0.35 * limit),
              min(high, limit - RESERVE_LOW_RESULTS * one))
    return high, low


#: 工程プロンプト（`phases.build_phase_prompt`）に許す字数。**窓に対する絶対量**。
#: 保護される物（帯6 でしか削られない・平常時は 1 文字も減らない）ほど、
#: 自分で蓋を持たなければならない。6,000 字は日本語主体だと約 6,100 トークン＝
#: 32K の予算 22,337 の **27%** を毎ターン占める。7.5% は 32,768 → 2,457 字
#: （≒2,500 トークン・予算の 11%）で、工程の指示＋検収列＋守ること＋課題の頭
#: （実測 ~1,750 字）が必ず入る大きさ。
PROMPT_CAP_FRACTION = 0.075
#: 窓が分からない/狭いときでも、必須の節が入る下限。
PROMPT_CAP_MIN_CHARS = 2000
#: 窓が広くても、依頼文が際限なく育たないための上限（従来値）。
PROMPT_CAP_MAX_CHARS = 6000


def prompt_cap_chars(n_ctx: int | None) -> int:
    """窓 → 工程プロンプトに許す字数。"""
    try:
        n = int(n_ctx or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = FALLBACK_N_CTX
    return max(PROMPT_CAP_MIN_CHARS,
               min(PROMPT_CAP_MAX_CHARS, int(n * PROMPT_CAP_FRACTION)))

#: 帯2 の段。上から順に締める。
_HEAD_TAIL = {2: (60, 20), 3: (40, 15), 4: (20, 8), 5: (10, 4), 6: (4, 2)}
#: 梯子の最下段。ここでも収まらなければ切り詰めずに名前のついた停止（＝仕切り直し）をする。
#:
#: ★2026-08-29 に「段9＝保護窓を 1 本に縮める最後の手」を**作って、測って、捨てた**。
#:   実トレース 4 本の再生では仕切り直しが 73 → 67 回（−8%）に減った。だが段9 は
#:   `test_直近2ターンのツール結果は全文で残る` が固定している不変条件を壊す。
#:   あれは「直前に読んだ物が消えるとモデルは埋め合わせに嘘をつく」という、
#:   この現場が最初に踏んだ形から立てた線で、6/99 回の仕切り直しと引き換えに
#:   緩めるものではない（緩めるなら、嘘が増えないことを別に測ってからにすること）。
FLOOR_LEVEL = 8

#: n_ctx が分からないときの既定。保守側（実際の窓が広いぶんには損をしない）。
FALLBACK_N_CTX = 16384

#: 会話に必ず残す席（トークン）。生成の余白がこれを割り込むなら、**生成側を削る**。
#: ★2026-08-19 実測: 強制ターンが 8192 を確保すると、窓 16,384 / 固定接頭辞 6,816 で
#:   会話に 352 トークンしか残らず、969 トークンの会話で「文脈切れ」として走行が
#:   打ち切られていた（95走行中13本＝14%）。生成余白のために会話を失うのは本末転倒。
#:   3,072 は実測の会話長（969〜2,000 前後）に余裕を見た値で、根拠のある上限ではない。
TRANSCRIPT_FLOOR = 3072


@dataclass
class Budget:
    """1ターンぶんの足し算。**そのターンの実 max_tokens で会計する**のが要点。

    `agent.py` の強制ターン（差し戻し後 tool_choice=required）だけ max_tokens が
    8192 で、他は 4096。ここを定数 4096 で会計すると、**最も重要なターンが最も
    予算不足になる**。足りないと finish_reason=length で無音に切られ、native
    モードでは tool_call が 0 本になり、「指示を無視した」と誤診される
    （agent.py:702-708 に同じ現象の記録がある）。
    """

    n_ctx: int
    gen: int
    fixed: int
    slack: int = 1024

    @property
    def prompt(self) -> int:
        """transcript に使ってよいトークン数。負なら 0。"""
        return max(0, int(self.n_ctx) - int(self.gen) - int(self.slack) - int(self.fixed))


@dataclass
class Plan:
    """送信するビューと、そのために何を落としたか。"""

    messages: list[dict]
    tokens_before: int
    tokens_after: int
    fired: bool
    counters: dict[str, int] = field(default_factory=dict)
    #: 中略・失敗省略・重複ポインタ化した evidence 番号。最終報告がこの番号を
    #: 引用していたら「見えていないものを引用した」＝静かな取りこぼしの兆候。
    stub_ns: list[int] = field(default_factory=list)
    #: 最下段でも予算を超えた。切り詰めずに止めるための旗。
    exhausted: bool = False
    #: 今回採用した段。呼び出し側が次ターンへ持ち越す（単調に深くなるだけ）。
    level: int = 0
    #: 固定接頭辞だけで n_ctx を食い潰していて、transcript を削っても無意味。
    #: この場合は何もせずに通す（会計の誤りで run を殺すほうが害が大きい）。
    unusable: bool = False


# --------------------------------------------------------------------------
# トークン計測
# --------------------------------------------------------------------------

def _server_root(base_url: str | None) -> str | None:
    """`.../v1` は OpenAI 互換層。/tokenize と /props は素の根にある。"""
    if not base_url:
        return None
    u = str(base_url).rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")]
    return u or None


def probe_window(base_url: str | None) -> int:
    """実際の n_ctx を取りに行く。推測しない。

    順に /props（llama.cpp が per-slot の n_ctx をそのまま返す）→ 環境変数 →
    保守側の既定。**「-c を 32768 で起動したはず」を根拠にしない**: 4 スロット
    構成だと 1 リクエストが使えるのはその 1/4 のこともあり、外した側の誤りが
    そのまま 400 になる。
    """
    return probe_window_ex(base_url)[0]


def probe_window_ex(base_url: str | None) -> tuple[int, str]:
    """probe_window と同じ値と、その出所（"props"＝推論機が答えた・"env"＝LAPS_N_CTX・"fallback"＝既定）。
    ★出所が要る（2026-10-16 の点検）: /props が一度失敗しただけで、銘柄表の既定（32,768）を窓として走行全体に使っていた。
      推論機が答えていない値は呼ぶ側（Agent._ctx_window）が後で探り直す。"""
    root = _server_root(base_url)
    if root:
        try:
            r = httpx.get(f"{root}/props", timeout=5.0, trust_env=_trust_env())
            if r.status_code == 200:
                d = r.json()
                n = ((d.get("default_generation_settings") or {}).get("n_ctx")
                     or d.get("n_ctx"))
                if isinstance(n, int) and n > 0:
                    return n, "props"
        except Exception:
            pass
    env = os.environ.get("LAPS_N_CTX")
    if env and env.isdigit() and int(env) > 0:
        return int(env), "env"
    return FALLBACK_N_CTX, "fallback"


def approx_tokens(text: str) -> int:
    """/tokenize が使えないときの近似 = **UTF-8 バイト数**。

    byte-level BPE では1トークンが最低1バイトを消費するので、これは
    **証明できる上界**。過小評価は 400 で即死するので、証明できない側には
    倒さない。

    ★これは「文字数を係数で割る」を1度やって捨てた結果である。実測で
    chars/token は 1.00〜4.49 に散り、係数 0.75（ASCII）は次の 1 行で破れた:

        "1234567890 " * 10  →  110 文字 / **実測 110 トークン**（近似 108）

    数字も、数字の直後の空白も 1 文字 1 トークンになる。つまり ASCII について
    安全な係数は 1.0 しかなく、それはバイト数そのものである。

    代償は正直に書いておく: 英文やソースコードでは実測の 3〜4.5 倍、改行だらけの
    入力では 24 倍に見積もる。**ゆるすぎて使い物にならない場面がある。**
    llama.cpp なら /tokenize が実測を返すのでこの近似は出番が無く、出番が
    あったターンは `ctx_budget_approx_used` に出る。窓が狭く見積もられて何も
    できないときは `LAPS_N_CTX` で実際の窓を教えること。
    """
    if not text:
        return 0
    return len(text.encode("utf-8", "replace"))


#: chat template が1メッセージあたり足す非内容トークン。llama-server の
#: /apply-template を実測して決めた（Qwen3: `<|im_start|>role\n…<|im_end|>\n`）。
#: tool は `<tool_response>` で包まれるぶん重い。ズレは SLACK が吸う。
_ROLE_OVERHEAD = {"tool": 12}
_ROLE_OVERHEAD_DEFAULT = 5
#: 末尾の `<|im_start|>assistant\n`。system 側と transcript 側で別々に数えたものを
#: 足すと二重に乗るので、呼び出し側が1回ぶん引けるように公開している。
TAIL_OVERHEAD = 3
#: 道具呼び出し1件ぶんの、名前と引数以外の飾り（`<tool_call>` の包み・区切り・id）。
#: テンプレートによって違うので保守側（多め）に取る——**足りないほうが危険**
#: （見積りが小さいと 400 で走行ごと死ぬ）。
_TOOLCALL_OVERHEAD = 12

#: /tokenize が連続して何回失敗したら近似に固定するか。
#: 1（＝初回のみ）だと、推論器の起動直後にたまたま落ちただけで
#: セッション全体が近似になる。実機でそれが起き、見積りが約12倍に膨れた。
_EXACT_FAIL_LATCH = 3

#: 概算（UTF-8 バイト数）を実測で較正するときの安全側の倍率。
#: ★2026-10-05（敵対レビュー F3-1）: `/tokenize` の無い推論機（Ollama・LM Studio・外部 API）では
#:   概算が実測の **4.33 倍**（前置き 28,019 対 6,472 tok）になり、32K 窓では固定費だけで窓を超えて
#:   会話に使える席がほぼ無かった。応答の `usage.prompt_tokens` は OpenAI 互換なら必ず返るので、
#:   それで概算を縮める。**下げすぎは 400 で即死**なので、実測の 1.15 倍を見積りの目標にする。
APPROX_CALIBRATION_MARGIN = 1.15
#: 1 ターンで縮めてよい割合の下限（一度に半分までしか縮めない）。外れ値 1 本で崖から落とさない。
APPROX_CALIBRATION_MIN_STEP = 0.5


class TokenCounter:
    """/tokenize をメッセージ単位でメモ化する。

    メモ化が効くのは、**段を変えないかぎり古いメッセージの投影がバイト単位で
    同じ**だから。定常状態では新しい 1〜2 本しか実測しない。段を締めた
    ターンだけ全件を数え直す（実測 /tokenize は 193KB で 104ms、20KB×20 で
    227ms なので、それでも数秒で済む）。
    """

    def __init__(self, base_url: str | None = None, *, timeout: float | None = None) -> None:
        self._root = _server_root(base_url)
        # ★30 → 10 秒（2026-09-02）。健全な llama-server は 193KB を 104ms で返す。
        #   読み込み中／詰まった推論機では本文より先に **/tokenize が 30 秒×3 回**
        #   黙り、利用者には理由の無い 90 秒の沈黙になっていた（再現で実測）。
        self._timeout = float(timeout if timeout is not None
                              else (os.environ.get("LAPS_TOKENIZE_TIMEOUT", "10") or 10))
        self._client: httpx.Client | None = None
        # None = 未判定。ターンごとに実測と近似が入れ替わると投影のバイトが
        # 揺れて接頭辞キャッシュが飛ぶので、**一度決めたら固定**する。
        #
        # ★ただし「1回失敗したら固定」は強すぎた（2026-08-19・実機で踏んだ）。
        #   推論器が起動直後だと最初の /tokenize が落ちることがあり、その1回で
        #   **セッション全体が近似に落ちる**。近似は UTF-8 バイト数（安全側の
        #   上界）なので、実機では見積りが**約12倍**に膨れた:
        #       実プロンプト 7,713 tok  ↔  見積り 97,898 tok
        #   その結果 `ctx_budget_unusable`＝**文脈の整理が働かず**、
        #   整理されないまま送って本物の 400 で溢れた。
        #   さらに画面には「固定分 24,151・使えるのは約0」という嘘の数字が出た。
        #   → 連続 _EXACT_FAIL_LATCH 回まで再挑戦してから固定する。
        self._exact: bool | None = None
        self._fail_streak = 0
        self._memo: dict[str, int] = {}
        self.approx_calls = 0
        self.exact_calls = 0
        #: 概算に掛ける倍率（1.0 ＝ 素のバイト数＝証明できる上界）。`calibrate()` だけが動かす。
        self.approx_scale = 1.0

    # -- 内部 --------------------------------------------------------------
    def _post_tokenize(self, text: str) -> int | None:
        if not self._root:
            return None
        try:
            if self._client is None:
                self._client = httpx.Client(base_url=self._root, trust_env=_trust_env(), timeout=self._timeout)
            r = self._client.post("/tokenize", json={"content": text})
            if r.status_code != 200:
                return None
            return len(r.json()["tokens"])
        except Exception:
            return None

    # -- 公開 --------------------------------------------------------------
    def count(self, text: str) -> int:
        text = text or ""
        if not text:
            return 0
        hit = self._memo.get(text)
        if hit is not None:
            return hit
        n: int | None = None
        if self._exact is not False:
            n = self._post_tokenize(text)
            if n is None:
                self._fail_streak += 1
                if self._fail_streak >= _EXACT_FAIL_LATCH:
                    # ここまで続けて駄目なら、以後は近似に固定する。
                    # （途中で実測へ戻ると投影のバイトが揺れて接頭辞キャッシュが飛ぶ）
                    self._exact = False
            else:
                self._fail_streak = 0
                self._exact = True
        if n is None:
            self.approx_calls += 1
            # ★概算はメモ化しない（倍率が後から変わるので、控えた値が古くなる）。
            #   素の概算は `len(encode)` だけなので数え直しても安い。
            raw = approx_tokens(text)
            return raw if self.approx_scale >= 1.0 else max(1, int(raw * self.approx_scale + 0.999))
        self.exact_calls += 1
        self._memo[text] = n
        return n

    def calibrate(self, predicted: int, actual: int) -> float | None:
        """概算で数えたターンの見積り（predicted）と、推論機が返した実測（actual）で倍率を直す。

        実測で数えている（`/tokenize` がある）ときは何もしない。返り値は新しい倍率（変えなければ None）。
        - 見積りを「実測 × APPROX_CALIBRATION_MARGIN」に寄せる（掛け算で更新する。
          見積りは既にいまの倍率で縮んでいるので、比を掛ければ素のバイト数に対する倍率になる）。
        - **過小に見積もった**（actual > predicted）ら、その場で必要なだけ戻す（上限 1.0）。
        - 縮めるのは 1 ターンに半分まで。
        """
        if self._exact is not False and self.approx_calls == 0:
            return None
        if not (isinstance(predicted, int) and isinstance(actual, int)) or predicted <= 0 or actual <= 0:
            return None
        target = self.approx_scale * (actual * APPROX_CALIBRATION_MARGIN) / predicted
        target = max(self.approx_scale * APPROX_CALIBRATION_MIN_STEP, target)
        target = min(1.0, max(0.05, target))
        if abs(target - self.approx_scale) < 1e-3:
            return None
        self.approx_scale = target
        return target

    def count_messages(self, messages: list[dict]) -> int:
        """chat template のぶんも足した、実際に送られる見込みのトークン数。"""
        total = 0
        if not messages:
            return 0
        for m in messages:
            total += self.count(str(m.get("content", "") or ""))
            # ★`tool_calls` を数える（2026-08-19 実測で発覚）。
            #   2026-08-16 に「native の tool_calls を会話に残す」直しを入れたが、
            #   **数える側を直していなかった**。assistant の content は空のことが多く、
            #   道具の名前と引数の JSON が**丸ごと勘定から漏れていた**。
            #   結果、長い走行ほど見積りが小さくなり、実測では
            #   **39/39 回すべてが過小予測**（平均 +1,131〜+2,108・最大 3,886）。
            #   固定接頭辞のほうは +83 tok（1%）で正確だったので、差はここ。
            #   ＝「窓に収まるのに文脈切れ」も「本当に 400」も、同じ原因から出ていた。
            for tc in (m.get("tool_calls") or []):
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                total += self.count(str(fn.get("name") or ""))
                total += self.count(str(fn.get("arguments") or ""))
                total += _TOOLCALL_OVERHEAD
            if m.get("tool_call_id"):
                total += self.count(str(m.get("tool_call_id")))
            total += _ROLE_OVERHEAD.get(str(m.get("role", "")), _ROLE_OVERHEAD_DEFAULT)
        return total + TAIL_OVERHEAD

    def exact(self) -> bool:
        """True = /tokenize 実測。False = 近似（必ず過大側）。"""
        return bool(self._exact)

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# evidence 項目の分解
# --------------------------------------------------------------------------

# _record_evidence（agent.py:436-454）の書式 f"[{n}] {name} → {result}" と、
# tool メッセージを作る側の "\n\n".join（agent.py:664, 910）が根拠。
# ★tool メッセージは1本＝1結果ではない。並列実行では6件が1本に連結される。
#   メッセージ単位で切ると、1件落とすつもりで6件消える。
_ITEM_RE = re.compile(r"(?m)^\[(\d+)\] (\S+) → ")
_SEP = "\n\n"


def split_items(block: str) -> list[tuple[int | None, str, str]]:
    """tool メッセージを [(n, tool_name, 本文), ...] へ。

    印が無いとき、あるいは**分解して結合し直したものが原文と一致しないとき**は
    `[(None, "", block)]` を返す＝丸ごと1件として扱う。決して黙って捨てない。
    （区切りが "\\n\\n" でない場所で印が現れると復元できない。そこで諦めるほうが、
    1バイトずれた transcript を送るより安い。）
    """
    block = block or ""
    ms = list(_ITEM_RE.finditer(block))
    if not ms or ms[0].start() != 0:
        return [(None, "", block)]
    items: list[tuple[int | None, str, str]] = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() - len(_SEP) if i + 1 < len(ms) else len(block)
        end = max(m.start(), end)
        items.append((int(m.group(1)), m.group(2), block[m.start():end]))
    if join_items(items) != block:
        return [(None, "", block)]
    return items


def join_items(items: list[tuple[int | None, str, str]]) -> str:
    """split_items の逆。join(split(x)) == x が試験で固定されている。"""
    return _SEP.join(t for _, _, t in items)


def _body_of(text: str) -> str:
    """`[3] Read → 本文` の本文だけ。印が無ければ全体。"""
    m = _ITEM_RE.match(text or "")
    return text[m.end():] if m else (text or "")


def _head_of(text: str) -> str:
    """`[3] Read → ` の部分。印が無ければ空。"""
    m = _ITEM_RE.match(text or "")
    return text[: m.end()] if m else ""


# --------------------------------------------------------------------------
# 削り方（決定論のみ）
# --------------------------------------------------------------------------

_DEDUP_MARK = "(省略: 出力が ["
_DEDUP_RE = re.compile(r"^\(省略: 出力が \[\d+\] と同一\)$")
#: これより短い本文は重複でも畳まない（ポインタのほうが長くなる）。
_DEDUP_MIN_CHARS = 200


def dedup_items(items: list[tuple[int | None, str, str]],
                seen: dict) -> tuple[list[tuple[int | None, str, str]], int]:
    """同じ出力の再取得をポインタ1行に。

    帯1 単独では**失うものがゼロ**（先に出た番号を指すだけで、その番号の本文は
    残っている）。⚠ ただし帯2 と併用して指し先の本文が中略されると、そこで初めて
    失われる。だから指し先も中略されたときは、そちらに取り直し方が書いてある。
    """
    out: list[tuple[int | None, str, str]] = []
    n = 0
    for num, tool, text in items:
        body = _body_of(text)
        if num is None or len(body) < _DEDUP_MIN_CHARS or _DEDUP_RE.match(body.strip()):
            out.append((num, tool, text))
            continue
        key = (tool, body)
        prev = seen.get(key)
        if prev is None:
            seen[key] = num
            out.append((num, tool, text))
        else:
            out.append((num, tool, f"[{num}] {tool} → {_DEDUP_MARK}{prev}] と同一)"))
            n += 1
    return out, n


def head_tail(text: str, head: int, tail: int, marker: str | None = None) -> tuple[str, int]:
    """先頭 head 行 + 中略の印 + 末尾 tail 行。落とした行数も返す。

    印は要約ではない。**受領書**（何行落としたか）と**取り戻し方**を書く。
    「変わっていません」の類は書かない: 安心させると取りに戻らなくなる恐れが
    あり（未検証）、取り直し方だけ書くほうに倒している。
    """
    lines = (text or "").split("\n")
    if len(lines) <= head + tail + 1:
        return text, 0
    drop = len(lines) - head - tail
    mark = marker or f"… (中略 {drop} 行を省略)"
    return "\n".join(lines[:head] + [mark] + lines[-tail:]), drop


def stub_marker(n: int | None, tool: str, inp: dict, dropped: int) -> str:
    """要約ではなく、受領書＋取り戻し方。"""
    base = f"… (中略 {dropped} 行を省略／要約ではありません"
    if not isinstance(inp, dict):
        inp = {}
    if tool == "Read" and inp.get("file_path"):
        # ★「Edit の前に読み直す」を足した（2026-10-05）。実測（09-01 以降の 99 走行）:
        #   読んでから編集するまでに圧縮を挟むと Edit の照合失敗は 36%（60/166）、
        #   挟まなければ 18%（34/190）。中身が外れた後、モデルは記憶で old_string を書く。
        #   Claude Code も圧縮の後は「圧縮前に読んだファイル」を明示して読み直させる。
        #   ★印は 1 走行で何十回も文脈に残るので、足すのは短い一言だけ。
        return base + (f"／Read(file_path=\"{inp['file_path']}\", offset=<開始行>, "
                       f"limit=<行数>) で取り直せます。Edit の前は必ず読み直す)")
    if tool == "Bash" and inp.get("command"):
        return base + f"／同じコマンドを実行し直せば取り直せます: {str(inp['command'])[:160]})"
    if tool:
        return base + f"／{tool} を同じ引数で呼び直せば取り直せます)"
    return base + ")"


# ---- 帯7: 最深段（FLOOR_LEVEL）だけで効く「受領書」 ------------------------
#
# ★なぜ最深段だけか（2026-09-04・run29/31/32/34 の再生で確かめてから入れた）:
#   32K の窓では level=8（最深段＝すでに head/tail(4,2) + 帯3 + 帯4 が効いている）
#   が全 5,709 手番の 38%（2,167 件）を占めていた——**すでにいちばん激しく
#   削っている段が、いちばん多く選ばれている**。段2〜7 の head/tail はそれでも
#   数百〜数千字の頭/尾を残す。この段でだけ、頭/尾をゼロにして
#   「文字数・先頭/末尾の短い抜粋・取り直し方」の1行受領書へ落とす
#   （goose/opencode の「本体でなく計算結果を返す」と同じ形。要約はしない）。
# ★誤爆止め: band3（失敗の本文の1行折り畳み）は先に判定されるので、この段は
#   band3 に当たらなかった body（＝成功した結果）だけを扱う。直近 keep_last 本の
#   ツール結果と user は `_protected()` がメッセージ単位で先に守るので、
#   ここへは来ない（＝「読んだはずの物が無い」を作らない）。
#   しきい値は `ht`（level>=8 では常に (4,2)）と揃える——head/tail で削られる
#   はずだった本文だけを対象にする。
_RECEIPT_SNIP = 80


def tool_receipt(num: int | None, tool: str, inp: dict, text: str) -> tuple[str, int]:
    """本文を丸ごと受領書に差し替える。戻り: (新しい text, 落とした行数)。

    要約ではない（内容を読んで言い換えない）——数えられる事実（文字数・行数）と、
    本文そのものの先頭/末尾の短い抜粋、`stub_marker` と同じ取り直し方だけを書く。
    """
    head, body = _head_of(text), _body_of(text)
    lines = body.split("\n")
    if len(lines) <= 1:
        return text, 0
    if not isinstance(inp, dict):
        inp = {}
    nchars = len(body)
    first = " ".join(lines[0].split())[:_RECEIPT_SNIP]
    last = " ".join(lines[-1].split())[:_RECEIPT_SNIP]
    dropped = len(lines)          # 本文を丸ごと落とす＝1行も残さない
    marker = stub_marker(num, tool, inp, dropped)
    receipt = (f"（受領書: {nchars:,} 文字・{len(lines):,} 行・"
              f"先頭 {first!r}・末尾 {last!r}）\n{marker}")
    return head + receipt, dropped


_ERR_RE = re.compile(r"^(Error\b|Error:|Error in |Traceback \(most recent call last\))")
_ERR_FOLDED = "… (失敗の詳細 "


def _fold_error(text: str) -> tuple[str, int]:
    """失敗した結果は1行目だけ残す。先へ進んだ後に要るのは「失敗した」であって
    traceback ではない。

    ★既に畳んだものを畳み直さない。これを忘れると毎ターン「詳細 1 行を省略」に
    書き換わり続けて、段を変えていないのに先頭が動く＝接頭辞キャッシュが毎ターン
    飛ぶ。冪等性はここでは速度の問題でもある。
    """
    head, body = _head_of(text), _body_of(text)
    lines = body.split("\n")
    if len(lines) <= 1 or lines[-1].startswith(_ERR_FOLDED):
        return text, 0
    drop = len(lines) - 1
    return head + lines[0] + f"\n{_ERR_FOLDED}{drop} 行を省略)", drop


# ---- 帯5: 道具呼び出しの引数 -----------------------------------------------
#
# native tool-calling では、モデルが書いた「何をしたか」は assistant の content では
# なく `tool_calls[].function.arguments` に乗る。実トレース 4 本で:
#   Edit  646 回 / 推定 506,596 トークン（引数の 64.3%・1 回平均 784）
#   Bash 1,767 回 / 110,924    Write 147 回 / 50,323
# これが**どの段でも縮まないまま**最後まで残るのが、仕切り直しが 12 回まで
# 増えた直接の理由。畳んでも失うのは「どう書いたか」だけで、
#   * その呼び出しの**結果**（成否・エラー本文）は tool メッセージに残る
#   * 書いた**中身**は作業場のファイルにある（Read で取り直せる）
# ＝帯1（損失ゼロ）の次に安い。だから梯子の浅い段（2）に置く。

#: これより短い引数は畳まない（受領書のほうが長くなる）。
_ARGS_MIN_CHARS = 300
#: 畳んだあと、各値に残す先頭の字数。呼び出しを**見分けられる**だけは残す。
_ARGS_KEEP_CHARS = 100
#: 畳み済みの印（冪等性のため。毎ターン書き換わると接頭辞キャッシュが飛ぶ）。
_ARGS_MARK = "…（引数 "
_ARGS_TAIL = " 字省略・実行済み。中身は作業場のファイルにあります）"


def fold_tool_args(arguments: str) -> tuple[str, int]:
    """道具呼び出しの引数を短くする。**JSON のまま**返す（キーと構造は保つ）。

    ★構造を壊さない理由: この文字列は chat template に流し込まれる。値だけを
      詰めれば、テンプレートが `| tojson` を掛けても掛けなくても壊れない。
      JSON として読めないもの（モデルが壊れた引数を書いた回）は頭だけ残す。
    ★冪等: 既に印がある値は触らない。
    """
    s = str(arguments or "")
    if len(s) < _ARGS_MIN_CHARS:
        return s, 0
    try:
        obj = json.loads(s)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        # JSON ではない/辞書でない。頭だけ残して受領書を付ける。
        if _ARGS_MARK in s:
            return s, 0
        drop = len(s) - _ARGS_KEEP_CHARS
        return s[:_ARGS_KEEP_CHARS] + f"{_ARGS_MARK}{drop}{_ARGS_TAIL}", drop
    dropped = 0
    new: dict = {}
    for k, v in obj.items():
        if isinstance(v, str) and len(v) > _ARGS_KEEP_CHARS and _ARGS_MARK not in v:
            drop = len(v) - _ARGS_KEEP_CHARS
            new[k] = v[:_ARGS_KEEP_CHARS] + f"{_ARGS_MARK}{drop}{_ARGS_TAIL}"
            dropped += drop
        else:
            new[k] = v
    if not dropped:
        return s, 0
    return json.dumps(new, ensure_ascii=False), dropped


# ---- 帯6: ハーネスが書いた依頼文 --------------------------------------------
#
# 人が書いた依頼は削らない。削ってよいのは**ハーネスが自分で組み立てた** user
# メッセージだけ。目印はその先頭にある（工程プロンプト＝`phases.build_phase_prompt`、
# 仕切り直しの覚え書き＝`agent.py` の "[ハーネスからの覚え書き"）。

#: 工程プロンプトの節見出し。**この定数が正本**で、`phases.py` はここから読む
#: （両側に同じ文字列を持つと、片方を直したときに黙って外れる）。
SEC_TASK_HEAD = "[元の課題"
SEC_STATE = "[ここまでの記録"
SEC_PHASE = "[この工程]"
SEC_ACCEPT = "[この工程の検収"
SEC_RULES = "[守ること]"
SEC_LEDGER = "[走行台帳"
SEC_FAILURE = "[前回の失敗の証拠"
#: `repomap.render_map` が工程プロンプトの**前**に置く地図（別の作業で 2026-08-29 に
#: 追加された）。同じ user メッセージに乗るので、ここでも節として扱う。
#: 実体は `repomap._HEADER`。ずれたら `test_context_budget` の
#: `test_地図の見出しが帯6の表とずれていない` が鳴る（落とさず警告だけ）。
SEC_REPOMAP = "[作業場の地図"

#: ★仕切り直し／工程の引き継ぎで**何を持ち越すか**の優先順位（正本）。
#:   数字が小さいほど残す。0 は決して落とさない。
#:   根拠（実トレース run29/31/32/34）:
#:     0 いまやること・合格の定義   … これが無いと工程そのものが成立しない
#:     1 前回の失敗の証拠           … 12 試行中 140 回が同じ不合格。ここを失うと同じ道を掘る
#:     2 元の課題の頭               … 目標。失うと脱線する（600 字と小さい）
#:     3 守ること                   … 静的な規律。system プロンプトと重なる
#:     4 走行台帳・作業場の地図     … ledger.json / Glob と Read で取り直せる
#:     5 ここまでの記録・成果物一覧 … state.json にあり、Glob でも取り直せる＝最初に落とす
HANDOFF_PRIORITY: tuple[tuple[str, int], ...] = (
    (SEC_PHASE, 0),
    (SEC_ACCEPT, 0),
    (SEC_FAILURE, 1),
    (SEC_TASK_HEAD, 2),
    (SEC_RULES, 3),
    (SEC_LEDGER, 4),
    (SEC_REPOMAP, 4),
    (SEC_STATE, 5),
)

#: ハーネスが書いた user メッセージの目印（先頭一致）。
#: ★仕切り直しの覚え書き（"[ハーネスからの覚え書き…"）は**入れていない**。
#:   節が 1 つしかないので `fold_harness_user` は何もせず、実トレースでも
#:   発火が 0 になる（＝発火しない機構を作らない、というこの現場の決まり）。
#:   そもそも仕切り直しの直後は段が 0 に戻るので、削る場面が構造的に来ない。
#: ★工程プロンプトの**前**に地図が付くことがあるので、両方を目印にする
#:   （`startswith` で見るのは、人が書いた依頼文を誤って畳まないため）。
HARNESS_USER_MARKS = (SEC_TASK_HEAD, SEC_REPOMAP)

#: 段 → この優先度**以上**の節を落とす。帯6 は浅い段では働かない（依頼文は
#: 既に `phases._cap_prompt` が窓に合わせて蓋をしている。ここで更に削るのは
#: 「蓋をしてもなお窓が詰まった」ときだけ）。
_USER_DROP_AT = {4: 5, 5: 5, 6: 4, 7: 3, 8: 3}

#: ★「行頭の `[` 何でも」で割ってはいけない（一度そう書いて危うかった）。
#:   前回の失敗の証拠の本文には `[NG] …` や `[工程の実行が例外で終わりました]` が
#:   入っていて、そこで割ると**証拠の途中から別の節**になり、いちばん残したい物が
#:   「知らない節」として落ちる。割るのは**表に載っている見出しだけ**。
_SEC_RE = re.compile("(?m)^(?:"
                     + "|".join(re.escape(m) for m, _ in HANDOFF_PRIORITY) + ")")


def _sections(text: str) -> list[str]:
    """依頼文を既知の `[見出し]` 単位に割る。見出しが無ければ 1 節。"""
    text = text or ""
    ms = list(_SEC_RE.finditer(text))
    if not ms:
        return [text]
    out: list[str] = []
    if ms[0].start() > 0:
        out.append(text[: ms[0].start()])
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append(text[m.start():end])
    return out


def section_priority(section: str) -> int:
    """節 → 優先度。**表に無い節は 0（落とさない）**——安全側に倒す。

    知らない物を落とすのは、知らない物を残すより悪い（何が消えたか誰も言えない）。
    """
    for mark, pri in HANDOFF_PRIORITY:
        if section.startswith(mark):
            return pri
    return 0


_USER_FOLD_MARK = "…（この節は文脈が詰まったため省略。"


def fold_harness_user(text: str, drop_at: int) -> tuple[str, int]:
    """ハーネスが書いた依頼文から、優先度 `drop_at` 以上の節を落とす。

    落とした節は**消さずに見出しと受領書だけ残す**。黙って消すと、モデルは
    消えた物を前提に喋り続ける（この現場で実際に起きた形）。
    """
    if drop_at is None or drop_at > 5:
        return text, 0
    secs = _sections(text)
    if len(secs) <= 1:
        return text, 0
    out: list[str] = []
    dropped = 0
    for s in secs:
        if _USER_FOLD_MARK in s or section_priority(s) < drop_at:
            out.append(s)
            continue
        head = s.split("\n", 1)[0]
        body_len = len(s) - len(head)
        if body_len <= 80:
            out.append(s)
            continue
        dropped += body_len
        out.append(head + f"\n{_USER_FOLD_MARK}{body_len} 字／"
                          ".blackwall-phases/ の state.json・ledger.json にあります）\n")
    if not dropped:
        return text, 0
    return "".join(out), dropped


_TOOLCALL_MARK = "<tool_call>"


#: 畳んだ跡に置く目印。★**モデルの声に見える文にしない**（2026-09-13・実機の記録で判明）。
#: 旧: `… (経過の記述 3 行を省略)` ——**散文そのものに見える**ので、モデルが自分の前の発言として
#: 読み、**次の答えに真似して書いた**。利用者には
#:   「外部サイトからの作品取得の実行を開始しました。… (経過の記述 15 行を省略)」
#: という**途中で切れた答え**が出ていた（オーナーが送った記録 2026-09-13T22:44 で確認）。
#: → 角括弧つきの**ハーネスの札**にして、真似しても最終応答から剥がせる形にする。
#: ★2026-09-13 夜（オーナーの記録 23:15・版 0.1.221 で確認）——**文言を変えても無駄だった。**
#:   角括弧の札にした 13 分後の走行で、モデルは**新しい形をそのまま真似て**書いていた
#:   （token 事象に 14 回）。相手は「自分の前の発言」として読むので、**そこに何を置いても真似る**。
#:   → **assistant の発言の中には何も置かない。** 省いた事実は `agent._progress_block()`
#:     ＝**ハーネスの声**で伝える（役割が違うので自分の台詞として真似ない）。
#:   ★併せて分かったこと: 畳み→言い直し→また畳み、で**同じ段落を 5 回繰り返していた**
#:     （seq 2120/2227/2334/2450/2601）。「同じ出力を返す」の一因はこれ。
FOLD_MARK = ""                       # 互換のため名前は残す（もう本文には入れない）
#: この走行で assistant の散文から省いた行数の累計（`agent._progress_block` が読む）。
PROSE_FOLDED = 0
#: 剥がすとき用（行数を問わず当てる）。`agent._clean_reply` が使う。★写しを作らない。
FOLD_MARK_HEAD = "[ハーネスが畳みました:"


def _fold_prose(text: str) -> tuple[str, int]:
    """中間の assistant 散文を1行に。描写元のツール結果は残っている。"""
    lines = (text or "").split("\n")
    if len(lines) <= 2:
        return text, 0
    first = next((ln for ln in lines if ln.strip()), "")
    drop = len(lines) - 1
    #: 省いた事実は**ハーネスの声**（agent._progress_block）で伝えるため、ここに数だけ残す。
    global PROSE_FOLDED
    PROSE_FOLDED += drop
    return first, drop            # ★札を置かない＝真似る対象を作らない


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------

HINT_MARK = "ハーネスが自動で引いた 知識サーバー 照会"   # 旧い中核の部品 / agent.hint_injected と同じ目印


#: 畳まずに残す直近の assistant の本数（＝モデル自身の「いまの筋書き」）。
#: 1 だと、2 手前に書いた「次はこれをする」が消える。
KEEP_LAST_PROSE = 2


def _protected(messages: list[dict], keep_last: int) -> set[int]:
    """どの段でも触らない添字。"""
    prot: set[int] = set()
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if keep_last > 0:
        prot.update(tool_idx[-keep_last:])
    asst = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    # ★2026-09-13: 直近 **KEEP_LAST_PROSE 本**の assistant を守る（以前は 1 本だけ）。
    #   オーナー指摘「細かい作業の途中で何をやっているか忘れる」の機構がここにあった:
    #   帯4 は「中間の assistant 散文」を 1 行に畳む＝**モデル自身が書いた
    #   『いま何をしていて次に何をするか』が真っ先に消える**。
    #   残るのはファイルの中身とコマンド出力だけ＝**素材は残って筋書きだけ消える**。
    #   直近の語りは畳まない（費用は数百トークン・失うものが大きすぎる）。
    if asst:
        prot.update(asst[-KEEP_LAST_PROSE:])
    for i, m in enumerate(messages):
        # 目標・差し戻し・reset。小さいので削る意味が無く、削ると
        # 「なぜ突き返されたか」が消えて同じ失敗を繰り返す。
        if m.get("role") == "user":
            prot.add(i)
        # ★2026-09-07（run57）: ハーネスが注入した 知識サーバー の助言（≤2.6KB）は、注入の
        #   直後に畳みが走ると keep_last の外へ落ちて消える（実走: 注入 17:33:38 →
        #   畳み 17:34:10 で 22.8K→11.9K）。答えそのものが消えては注入の意味が無い。
        elif m.get("role") == "tool" and HINT_MARK in str(m.get("content") or ""):
            prot.add(i)
    return prot


@dataclass(frozen=True)
class _Params:
    dedup: bool
    ht: tuple[int, int] | None
    band3: bool
    band4: bool
    band7: bool
    args: bool
    user_drop_at: int | None


def _params(level: int) -> _Params:
    """段 → どの帯を効かせるか。

    ★段の番号は変えていない（前ターンから持ち越す状態で、試験も外の記録も
      この数字を読む）。新しい帯は既存の段に**足した**:
        段2 = 帯2(60,20) ＋ **帯5（道具呼び出しの引数）**
        段4 以降 = ＋ **帯6（ハーネスの依頼文の低優先節）**
        段8（FLOOR_LEVEL＝最深段）= ＋ **帯7（本体を受領書へ）**
      帯5 を浅い段に置くのは、失うものが帯1 の次に少ないから（上の表）。
      帯7 を最深段だけに置くのは、そこがいちばん多く選ばれる段だから
      （実測: run29/31/32/34 の再生で全手番の 38%。`tool_receipt` の docstring）。
    """
    return _Params(
        dedup=level >= 1,
        ht=_HEAD_TAIL.get(min(level, 6)) if level >= 2 else None,
        band3=level >= 7,
        #: ★2026-09-17: **既定で切った**（`LAPS_FOLD_PROSE=1` で戻せる）。
        #:
        #: 実測（実機 8 セッション・中央値／範囲 90.9〜97.7%）— 窓の中身の内訳:
        #:     道具の結果   95.4%
        #:     ★発言        3.6%   ← この帯が畳んでいたもの
        #:     依頼          0.9%
        #: ⇒ **畳んでも浮くのは最大 3.6%**。しかもその 3.6% は
        #:   **意図（なぜ・次に何を）の唯一の写し**で、ハーネスはそれを観測できない
        #:   （事実＝書いたファイル名・走らせたコマンド・道具の結果は別に持っている）。
        #:
        #: 外の測定も同じ向き:
        #:   ・SWE-agent の ACI（arXiv:2405.15793 Table 3）: 畳むのは**観測**であって
        #:     モデルの発言ではない。Last-5-Obs 18.0 対 Full history 15.0。
        #:   ・ACE（arXiv:2510.04618）: 記憶を**書き直す**と context collapse が起き、
        #:     無記憶より悪くなる。追記のみなら回避。
        #:   ・Useful Memories Become Faulty（arXiv:2605.12978）: 生の記録を持つ側が
        #:     強制要約した側の **2 倍**の精度。
        #: 自ラボの実測: この帯が発火した走行で**同じ段落を 5 回**繰り返した
        #: （意図が消える→言い直す→また畳まれる）。跡に置いた目印も真似された。
        #:
        #: ⚠ **未測定**: 浮かなくなった 3.6% のぶん、最深段で収まらず
        #:   「仕切り直し」が増える可能性がある。既知24＋未知24 で測ってから確定する。
        band4=level >= 8 and os.environ.get("LAPS_FOLD_PROSE") == "1",
        band7=level >= FLOOR_LEVEL,
        args=level >= 2,
        user_drop_at=_USER_DROP_AT.get(min(level, 8)) if level >= 4 else None,
    )


def _project(messages: list[dict], level: int, *, keep_last: int, frozen_upto: int,
             inputs: dict[int, dict] | None) -> tuple[list[dict], dict[str, int], list[int]]:
    """記録を変えずに、送信用のビューを作る。新しいリストを返す。"""
    counters = _zero_counters()
    stub_ns: list[int] = []
    if level <= 0:
        return list(messages), counters, stub_ns

    P = _params(level)
    dedup_on, ht, band3, band4, band7 = P.dedup, P.ht, P.band3, P.band4, P.band7
    prot = _protected(messages, keep_last)
    #: 帯5 は**直近の呼び出しには触らない**。直近 keep_last 本のツール結果は全文で
    #: 守られているのに、その結果を生んだ呼び出しの引数だけ畳むと、いま見ている
    #: 結果が「何をした結果か」だけが消える（いちばん要る対応関係を壊す）。
    asst_calls = [i for i, m in enumerate(messages)
                  if m.get("role") == "assistant" and m.get("tool_calls")]
    args_prot = set(asst_calls[-keep_last:]) if keep_last > 0 else set()
    seen: dict[tuple[str, str], int | None] = {}
    out: list[dict] = []

    for i, m in enumerate(messages):
        role = m.get("role")

        # 帯6: ハーネスが自分で書いた依頼文だけ、優先度の低い節から落とす。
        # （人が書いた user は `prot` のまま＝どの段でも触らない）
        if role == "user" and i >= frozen_upto and P.user_drop_at is not None:
            body = str(m.get("content", "") or "")
            if body.startswith(HARNESS_USER_MARKS):
                new, drop = fold_harness_user(body, P.user_drop_at)
                if drop:
                    counters["ctx_budget_band6_user"] += 1
                    counters["ctx_budget_user_chars_dropped"] += drop
                    out.append({**m, "content": new})
                    continue

        if i < frozen_upto or role not in ("tool", "assistant"):
            out.append(m)
            continue

        if role == "assistant":
            body = str(m.get("content", "") or "")
            new_m = m
            # 帯5: 古い道具呼び出しの引数。**保護窓の外だけ**。
            if P.args and i not in args_prot and i not in prot and m.get("tool_calls"):
                tcs, drop_total = [], 0
                for tc in (m.get("tool_calls") or []):
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    folded, drop = fold_tool_args(str(fn.get("arguments") or ""))
                    if drop:
                        drop_total += drop
                        tcs.append({**tc, "function": {**fn, "arguments": folded}})
                    else:
                        tcs.append(tc)
                if drop_total:
                    counters["ctx_budget_band5_args"] += 1
                    counters["ctx_budget_args_chars_dropped"] += drop_total
                    new_m = {**m, "tool_calls": tcs}
            if i in prot:
                out.append(new_m)
                continue
            # ツール呼び出しを含む発話は畳まない（実行の記録そのもの）。
            if band4 and _TOOLCALL_MARK not in body:
                new, drop = _fold_prose(body)
                if drop:
                    counters["ctx_budget_band4_prose"] += 1
                    counters["ctx_budget_lines_dropped"] += drop
                    out.append({**new_m, "content": new})
                    continue
            out.append(new_m)
            continue

        if i in prot:
            out.append(m)
            continue

        items = split_items(str(m.get("content", "") or ""))
        if dedup_on:
            items, ndup = dedup_items(items, seen)
            if ndup:
                counters["ctx_budget_band1_dedup"] += ndup
                stub_ns.extend(n for n, _, t in items
                               if n is not None and _DEDUP_RE.match(_body_of(t).strip()))
        new_items: list[tuple[int | None, str, str]] = []
        for num, tool, text in items:
            body = _body_of(text)
            if band3 and _ERR_RE.match(body.lstrip()):
                folded, drop = _fold_error(text)
                if drop:
                    counters["ctx_budget_band3_errbody"] += 1
                    counters["ctx_budget_lines_dropped"] += drop
                    if num is not None:
                        stub_ns.append(num)
                new_items.append((num, tool, folded))
                continue
            if band7 and ht:
                head_n, tail_n = ht
                lines = text.split("\n")
                if len(lines) > head_n + tail_n + 1:
                    receipt_text, dropped = tool_receipt(
                        num, tool, (inputs or {}).get(num) or {}, text)
                    if dropped:
                        counters["ctx_budget_band7_receipt"] += 1
                        counters["ctx_budget_lines_dropped"] += dropped
                        if num is not None:
                            stub_ns.append(num)
                        new_items.append((num, tool, receipt_text))
                        continue
            if ht:
                head, tail = ht
                lines = text.split("\n")
                if len(lines) > head + tail + 1:
                    drop = len(lines) - head - tail
                    mark = stub_marker(num, tool, (inputs or {}).get(num) or {}, drop)
                    cut, dropped = head_tail(text, head, tail, marker=mark)
                    counters["ctx_budget_band2_stub"] += 1
                    counters["ctx_budget_lines_dropped"] += dropped
                    if num is not None:
                        stub_ns.append(num)
                    new_items.append((num, tool, cut))
                    continue
            new_items.append((num, tool, text))
        out.append({**m, "content": join_items(new_items)})

    _append_receipt(out, counters, stub_ns)
    return out, counters, sorted(set(stub_ns))


# --------------------------------------------------------------------------
# 何を削ったかをモデルに伝える（受領書）
# --------------------------------------------------------------------------
#
# ★この現場で確定していること: **説明文に書いても従わない回がある。結果の中／
#   行き詰まりの場で言うと効く。** だから受領書は system プロンプトではなく、
#   モデルが最後に読む**道具の結果の末尾**に足す。
# ★黙って消すのがいちばん悪い: 消えた物を前提に喋り続ける（この現場の実例）。
#   中略の跡（stub_marker）は「その場所」には残るが、**畳んだ引数と落とした節は
#   跡が小さく**、全体として何が起きたかは伝わらない。1 行で総括する。
# ★足す先は投影の**最後**のメッセージだけ。前方は 1 バイトも動かさない
#   （先頭書き換えは自測で 260 倍遅い）。

_RECEIPT_MARK = "（文脈整理:"
#: 受領書に並べる中略番号の上限。長いと本文を押しのける。
_RECEIPT_MAX_NS = 8


def _receipt_text(counters: dict[str, int], stub_ns: list[int]) -> str:
    bits: list[str] = []
    ns = sorted(set(n for n in stub_ns if n is not None))
    if ns:
        shown = " ".join(f"[{n}]" for n in ns[:_RECEIPT_MAX_NS])
        more = f"（他 {len(ns) - _RECEIPT_MAX_NS} 件）" if len(ns) > _RECEIPT_MAX_NS else ""
        bits.append(f"中略した結果 {shown}{more}")
    if counters.get("ctx_budget_band5_args"):
        bits.append(f"古い道具呼び出しの引数 {counters['ctx_budget_band5_args']} 件は"
                    "「実行済み」に畳みました")
    if counters.get("ctx_budget_band6_user"):
        bits.append("依頼文の記録・台帳の節を省きました"
                    "（.blackwall-phases/state.json・ledger.json にあります）")
    if counters.get("ctx_budget_band4_prose"):
        bits.append("経過の記述を省きました")
    if not bits:
        return ""
    return (f"\n\n{_RECEIPT_MARK} 窓に収めるため上の記録を短くしています。"
            + "／".join(bits)
            + "。要るなら同じ道具で取り直してください。**見えていない物を"
              "前提に書かないこと。**）")


def _append_receipt(out: list[dict], counters: dict[str, int],
                    stub_ns: list[int]) -> None:
    """投影の最後が道具の結果なら、そこに受領書を 1 行足す。

    最後が道具の結果でないターン（モデルが道具を呼ばずに喋った直後など）は
    黙って諦める。**新しいメッセージは足さない**——件数が変わると、呼び出し側の
    会計（`count_messages` と `_with_tail_plan`）と記録の対応が読めなくなる。
    """
    if not out:
        return
    last = out[-1]
    if last.get("role") != "tool":
        return
    body = str(last.get("content", "") or "")
    if _RECEIPT_MARK in body:
        return
    note = _receipt_text(counters, stub_ns)
    if not note:
        return
    counters["ctx_budget_receipt"] = counters.get("ctx_budget_receipt", 0) + 1
    out[-1] = {**last, "content": body + note}


def plan(messages: list[dict], budget: Budget, counter: TokenCounter,
         *, keep_last: int = 2, frozen_upto: int = 0,
         inputs: dict[int, dict] | None = None, min_level: int = 0) -> Plan:
    """予算に収まるまで決定論の梯子を降りる。`messages` は変更しない。

    予算内なら恒等写像（fired=False、counters は全 0、要素は元のオブジェクト）。

    `min_level` は前ターンで採用した段。**段は単調にしか深くならない**。これが
    「凍結」の実体で、緩めない理由は接頭辞キャッシュにある: 段が同じかぎり古い
    メッセージの投影はバイト単位で同じなので、追記だけで済む（自測 0.10s）。
    段を選び直して緩めると先頭から書き換わって全再 prefill（自測 26.00s）。

    `inputs` は evidence 番号 → ツール入力。中略の跡に「Read(file_path=…) で
    取り直せます」と具体的に書くために要る（transcript の本文には入力が無い）。
    """
    msgs = list(messages or [])
    before = counter.count_messages(msgs)
    limit = budget.prompt

    if limit <= 0:
        # 固定接頭辞だけで窓を食い潰している。transcript を削っても届かないので、
        # 何もせずに通してサーバに判定させる。会計の誤りで run を殺すほうが害が
        # 大きい（近似モードでは fixed が実測の 2〜3 倍に出ることがある）。
        return Plan(messages=msgs, tokens_before=before, tokens_after=before,
                    fired=False, counters=_zero_counters(), unusable=True,
                    level=max(0, min_level))

    level = max(0, int(min_level))
    proj, counters, stub_ns = _project(msgs, level, keep_last=keep_last,
                                       frozen_upto=frozen_upto, inputs=inputs)
    after = counter.count_messages(proj) if level > 0 else before

    high, low = water_marks(limit, budget.n_ctx)
    if after > high:
        # 発火。HIGH を割った時点では止めず LOW まで落とす＝次ターンにまた
        # 削り直して前方を書き換える羽目にならないようにする。
        #
        # ★採るのは「最小を達成した中で最も浅い段」。素朴に「LOW を割るまで
        # 深くする」と、保護窓（直近2ターンのツール結果は全文）だけで LOW を
        # 超える形では最下段まで落ちきってしまい、**何も削っていない帯まで
        # 適用済みとして記録される**。実測（推論機・14ファイル読取・policy.py が
        # 保護窓に入る形）で level=8 と記録されながら帯3/帯4 の計数は 0 だった。
        # 段は次ターンへ持ち越す状態なので、実態より深い数字を残してはいけない。
        probe = level
        while probe < FLOOR_LEVEL and after > low:
            probe += 1
            cand, cand_counters, cand_stub = _project(msgs, probe, keep_last=keep_last,
                                                      frozen_upto=frozen_upto, inputs=inputs)
            cand_after = counter.count_messages(cand)
            if cand_after < after:
                level, proj, counters, stub_ns, after = (
                    probe, cand, cand_counters, cand_stub, cand_after)

    return Plan(
        messages=proj,
        tokens_before=before,
        tokens_after=after,
        fired=level > 0,
        counters=counters,
        stub_ns=stub_ns,
        exhausted=after > limit,
        level=level,
    )


def _zero_counters() -> dict[str, int]:
    return {
        "ctx_budget_band1_dedup": 0,
        "ctx_budget_band2_stub": 0,
        "ctx_budget_band3_errbody": 0,
        "ctx_budget_band4_prose": 0,
        "ctx_budget_band5_args": 0,
        "ctx_budget_band6_user": 0,
        "ctx_budget_band7_receipt": 0,
        "ctx_budget_lines_dropped": 0,
        "ctx_budget_args_chars_dropped": 0,
        "ctx_budget_user_chars_dropped": 0,
        "ctx_budget_receipt": 0,
    }


def apply_to(messages: list[dict], p: Plan) -> list[dict]:
    """Plan を送信用リストにする。system の付加は呼び出し側。

    発火していないときは**元のリストをそのまま**返す。ここで新しい辞書を作ると
    「予算内なら何も変えない」が目視で確かめられなくなる。
    """
    if not p.fired:
        return list(messages)
    return list(p.messages)
