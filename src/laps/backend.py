"""OpenAI-compatible chat backend.

Works with:
- local llama-server (default: http://127.0.0.1:8080/v1)
- Ollama (http://127.0.0.1:11434/v1)
- vLLM
- external OpenAI-compatible APIs

Strict streaming-disabled JSON contract for tool calling.
Transient errors (network blip, 5xx) are retried with exponential backoff.
"""
from __future__ import annotations
from laps import _envcompat
import json
import re
import os
import threading
import time
from dataclasses import dataclass
from . import loopcut as _loopcut

#: 反復の検査は**貯まった字がこれだけ増えるたび**に 1 回（毎トークンだと費用が乗る）。
_LOOPCUT_EVERY = 400
from typing import Any, Callable

import httpx


class DegenerateOutput(Exception):
    """生成が崩れた（短い周期の繰り返し・語の切れ端の寄せ集め）ので切った（loopcut.degenerate_tail・2026-10-10）。
    呼び出し側（oc の手番）が、読み込みの控えを使わずに 1 回だけやり直す。"""


class ContextOverflow(Exception):
    """プロンプトが n_ctx を超えたとサーバが言っている。

    ★`RuntimeError` を継承させない。下の retry 節が `RuntimeError` を「クライアント
    が閉じられた」の回復用に捕まえているので、継承すると**リトライされてしまう**。
    実測（2026-08-11・推論機 / qwen3-30b-a3b / n_ctx=32768 に 60,009 tok を投げた）:

        経過 11.8s  例外型=HTTPStatusError   ← 1回なら 0.3s。11.8s = 1.5+3+6 の backoff
        status=400  {"error":{...,"type":"exceed_context_size_error",
                              "n_prompt_tokens":60009,"n_ctx":32768}}

    つまり直上のコメント「bail on other 4xx」はコードと一致していなかった。決定的な
    拒否を4回投げ直しても答えは変わらない。加えてこの例外が `str(e)` に closed/EOF/
    502/503 を含まないため、agent.py の「集めたツール結果を返す」フォールバックにも
    掛からず、**それまでの全ツール結果を捨てて `[backend error]` が返っていた**。
    """

    def __init__(self, message: str, *, n_prompt_tokens: int | None = None,
                 n_ctx: int | None = None) -> None:
        super().__init__(message)
        self.n_prompt_tokens = n_prompt_tokens
        self.n_ctx = n_ctx


class BackendRejected(Exception):
    """400 のうち、文脈溢れ以外。こちらもリトライしない。"""


def _reject_from_400(r: "httpx.Response") -> Exception:
    """400 の本文を読んで、溢れなら機械可読な形にして返す。

    サーバは `n_prompt_tokens` と `n_ctx` を数字で返してくる。予算の当て推量を
    答え合わせできる唯一の一次情報なので、握り潰さずに上へ渡す。
    """
    body: dict = {}
    try:
        body = r.json() or {}
    except Exception:
        body = {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    msg = str(err.get("message") or body.get("message") or (r.text or "")[:500])
    kind = str(err.get("type") or "")
    npt, nctx = err.get("n_prompt_tokens"), err.get("n_ctx")
    looks_overflow = (
        kind == "exceed_context_size_error"
        or "context size" in msg.lower()
        or "exceed" in kind.lower()
        or isinstance(npt, int)
    )
    if looks_overflow:
        return ContextOverflow(
            f"context overflow: n_prompt_tokens={npt} n_ctx={nctx} :: {msg}",
            n_prompt_tokens=npt if isinstance(npt, int) else None,
            n_ctx=nctx if isinstance(nctx, int) else None,
        )
    return BackendRejected(f"HTTP 400: {msg}")


#: ★手番ごとの思考の上限（2026-10-06 オーナー「作業高速化の仕組みも欲しい」）。oc の主ループが、道具が成功した
#:   直後の手番（次の一手を決めるだけ）でだけ小さい上限をかける。`with reasoning_budget_limit(n):` の内側の要求に効く。
#:   ★環境変数 LAPS_REASONING_BUDGET（利用者の明示の絶対値）が優先。上限は「小さいほう」を採る（増やさない）。
import contextvars as _contextvars
_BUDGET_LIMIT: "_contextvars.ContextVar[int | None]" = _contextvars.ContextVar("bw_reasoning_budget_limit", default=None)


class reasoning_budget_limit:
    def __init__(self, n: int | None) -> None:
        # ★0 は「思考させない」（llama.cpp は予算 0 で思考を即座に閉じる・2026-10-07 実測）。None が「制限なし」。
        self.n = n if (n is not None and n >= 0) else None
        self._tok = None

    def __enter__(self) -> "reasoning_budget_limit":
        self._tok = _BUDGET_LIMIT.set(self.n)
        return self

    def __exit__(self, *exc) -> None:
        _BUDGET_LIMIT.reset(self._tok)


def reasoning_budget(max_tokens: int) -> int | None:
    """思考に許すトークン数。`max_tokens` とは**別枠**で渡す。

    ★2026-09-21。OSS 突き合わせ（cline は `thinkingBudgetTokens` と
      `maxTokensPerTurn` を**独立した設定**として持つ）→ 推論機 の llama.cpp が
      ★リクエスト本文で `reasoning_budget_tokens` を受けると実コードで確認した
      （`tools/server/server-common.cpp:1128` / `common/sampling.cpp:311-317`
        ＝予算に達したら思考を閉じさせる）。

    ★これが要る理由（実測・2026-09-21）:
        max_tokens=2000 → 思考 1617 字 / 本文 226 字
        max_tokens= 400 → 思考  400 字 / **本文 0 字**
      思考と本文が**同じ予算を奪い合う**ので、考えるだけで終わる回が出る。
      その回は `gen_all_thinking` として数えている（＝仮説ではなく実際に起きている）。

    既定は `max_tokens` の **60%**（★2026-10-06 から天井 REASONING_BUDGET_CAP つき）。★実機で測った（推論機・2026-09-21）:

        予算 400  分離なし → 思考 1423 / 本文 **0**     (length で切れる)
        予算 400  思考 240 → 思考  769 / 本文 **645**   (length)
        予算 1200 分離なし → 思考 3891 / 本文 **0**     (length)
        予算 1200 思考 720 → 思考 2412 / 本文 **101**   (★stop＝切れずに終わった)

    ⇒ ★**本文 0 字が解消し、予算も余った**（下段は 770/1200 しか使っていない）。
    ⚠ ★60% という比率そのものは**まだ最適化していない**。4 点しか測っていない。
    `LAPS_REASONING_BUDGET` に絶対値（トークン）を入れれば上書きできる。
    `0` で機能を切る（＝サーバ既定に任せる／従来どおり）。
    """
    raw = (os.environ.get("LAPS_REASONING_BUDGET") or "").strip()
    if raw:
        try:
            v = int(raw)
        except ValueError:
            return None
        return None if v <= 0 else v
    if max_tokens <= 0:
        return None
    # ★Gemma は思考を打ち切らない（2026-10-10 オーナー「思考を打ち切らない方針でもいいです」）。
    #   オーナーの走行（0.1.355）を同じ文脈で生成し直すと、打ち切らない 3 回は 3 回とも「思考→そのまま道具を呼ぶ」、
    #   打ち切った 6 回は 5 回が道具を呼ばずに本文を書き始めた。本文を書いた後の Gemma は、道具を呼ばずに手番を終えるか
    #   （34 回中 18 回）`<br>` を挟んで繰り返しに入る（34 回中 6 回）＝崩れた手番そのもの。Gemma の生成は 48〜67 tok/s で
    #   思考は 300〜700 語ほど＝上限で抑える理由（Qwen3.8 の 12 tok/s）が無い。暴走は崩れの判定とループの切りが止める。
    # ★GLM-4.7-Flash も同じ扱い（2026-10-12）: 打ち切らず、繰り返しは backend の思考の見張りで切る。
    if thinking_unbounded():
        # ★明示の「思考させない」（上限 0）だけは通す（2026-10-10）: 要約の要求は前置きを変えないよう思考ありの設定のまま
        #   上限 0 で思考を止める（oc._compact）。ここで None にすると要約の間も考えていた。ふだんの手番は打ち切らない。
        return 0 if _BUDGET_LIMIT.get() == 0 else None
    base = max(256, min(int(max_tokens * 0.6), budget_cap()))
    lim = _BUDGET_LIMIT.get()
    return min(base, lim) if lim is not None else base


#: ★2026-10-06（オーナー「暴走しないようにして」）。比率（max_tokens の 60%）だけでは上限になっていなかった:
#:   oc の max_tokens は窓 64K の 1/4＝16,384 なので予算は **9,830 トークン**。ベンチ中の推論機の記録で 1 回の応答が
#:   **6,575〜8,687 トークン**（6〜9 tok/s で 12〜20 分）に達し、それだけで課題の上限 900 秒を超えていた（予算の内側）。
#:   ふだんの思考は短い（終わった課題の中央値 178 文字）ので、天井は長い尾だけを切る。Qwen3.8 の P40 で約 2〜3.5 分。
REASONING_BUDGET_CAP = 1536
#: ★Think の「深く」（2026-10-11 オーナー「深さを選べるように」）のときの天井。Qwen3.8 の P40（12〜15 tok/s）で 1 手番 5 分ほどまで。
#:   浅く（既定）は REASONING_BUDGET_CAP のまま。
REASONING_BUDGET_CAP_DEEP = 4096


def _think_ceiling_chars() -> int:
    """思考の長さの天井（字・Gemma・GLM・Qwen の MoE＝thinking_unbounded・0 で切る）。既定 12,000＝661 手番の上位 1%（10,312 字）より上。"""
    try:
        return max(0, int(os.environ.get("LAPS_THINK_CEILING_CHARS") or 12000))
    except ValueError:
        return 12000


def reasoning_effort() -> str:
    """いまの依頼の思考の深さ（書式の reasoning_effort の値: low / medium / xhigh）。serve が依頼ごとに入れる。"""
    eff = (os.environ.get("LAPS_REASONING_EFFORT") or "low").strip().lower()
    if eff == "high":
        eff = "xhigh"
    return eff if eff in ("xhigh", "medium", "low") else "low"


def budget_cap() -> int:
    """手番ごとの思考の天井（深く＝REASONING_BUDGET_CAP_DEEP・それ以外＝REASONING_BUDGET_CAP）。"""
    return REASONING_BUDGET_CAP_DEEP if reasoning_effort() == "xhigh" else REASONING_BUDGET_CAP
#: 予算に達したとき思考の終わりの直前に差し込む一言（llama.cpp `reasoning_budget_message`）。
#:   無いと思考が文の途中で閉じられ、続きの行動に移りにくい。
REASONING_BUDGET_MESSAGE = ("\n\n(Thinking budget reached. Stop deliberating and act now: make the next tool call, "
                            "or give the answer with what you already know.)\n")


def progress_fields() -> dict:
    """流しの要求に、読み込みの進み具合を送らせる印（llama.cpp `return_progress`・知らないサーバは無視する）。
    ★2026-10-07: 実機の停滞の記録 16 本はすべて推論機の読み込み待ち（無音 91 秒〜・最大 277 秒）。読み込みの間は
      1 byte も来ないので、画面は止まって見え、20 分超の走行 9 本中 8 本が利用者の手で止められていた。
    LAPS_PROMPT_PROGRESS=0 で送らない。"""
    if os.environ.get("LAPS_PROMPT_PROGRESS", "1").strip() == "0":
        return {}
    return {"return_progress": True}


def reasoning_fields(max_tokens: int) -> dict:
    """要求の本文に足す思考の予算（予算が無ければ空）。4 か所の要求で同じ物を使う。"""
    rb = reasoning_budget(max_tokens)
    if rb is None:          # ★0 は「思考させない」なので送る（旧: `not rb` で 0 を送らず、無制限になっていた）
        return {}
    if rb == 0:             # 考えさせない手番に「上限に達した」の一言は要らない（思考の欄に同じ英文が並んだ・2026-10-07）
        return {"reasoning_budget_tokens": 0}
    return {"reasoning_budget_tokens": rb, "reasoning_budget_message": REASONING_BUDGET_MESSAGE}


def transient_reason(r: "httpx.Response") -> str:
    """5xx/429 の**本文に書いてある理由**を取り出して人が読む一行にする。

    ★2026-09-21。旧実装は `transient HTTP 500` としか言わず、**サーバが本文に
      書いていた理由を捨てていた**。投げ直して全部失敗したとき、利用者に届くのは
      数字だけ＝原因の分からない沈黙になる（`理由を隠す失敗は直せない` と同じ型）。

    ★いちばん多い 500 の中身は「共有 KV の枯渇」だった。実測（推論機 の journal・
      2026-09-20 に 76 件）:

        llama-server は n_slots=4 / `-c 32768` / kv_unified=true で動いており、
        **32,768 は 4 スロットの合計**。ところが 1 要求が 16,000〜24,000 使う
        （実測 16531 / 18256 / 18562 / 18819 / 18825 / 24061）。
        ⇒ **2 本同時で溢れる。**

      さらに上流の実装（`tools/server/server-context.cpp:3671`）は溢れたとき

        for (auto & slot : slots) if (slot.is_processing()) {
            send_error(slot, err); slot.release(); slot.prompt_clear(); }

      ＝ ★**処理中の全スロットを巻き添えで殺す。** 重い走行を 1 本重ねるだけで、
      無関係な利用者の要求まで落ちる。⇒ **他人が使っている間に走行を重ねない。**

    ⚠ ★これを `ContextOverflow` にしてはいけない。あちらは「この会話が長すぎる」の
      合図で、受けた側は**文脈を畳んで捨てる**。原因が**別の要求との同居**なのに
      畳むと、利用者の文脈を理由なく失う。だから**一時的な失敗のまま**にして、
      理由だけを言葉で運ぶ。
    """
    # ★誤りを報告する経路で例外を出さない。ここが落ちると、投げ直せば通るはずの
    #   500 が AttributeError に化けて走行ごと死ぬ（報告係が事故を起こす形）。
    try:
        body = r.json() or {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    try:
        raw = getattr(r, "text", "") or ""
    except Exception:
        raw = ""
    msg = str(err.get("message") or body.get("message") or raw[:200]).strip()
    head = f"HTTP {getattr(r, 'status_code', '?')}"
    if not msg:
        return f"transient {head}"
    if "context size" in msg.lower():
        n_ctx = err.get("n_ctx")
        extra = f"（この推論機の窓は {n_ctx}）" if isinstance(n_ctx, int) else ""
        return (f"transient {head}: 推論機の共有 KV が埋まっています{extra}"
                "——同時に走っている要求があると、1 本あたり 16K〜24K を使うこの"
                "構成では 2 本で溢れます。少し待って投げ直します。")
    return f"transient {head}: {msg}"


def read_to_desc() -> str:
    """read timeout を人が読む形で。"""
    v = os.environ.get("LAPS_READ_TIMEOUT", str(READ_TIMEOUT_DEFAULT)) or str(READ_TIMEOUT_DEFAULT)
    return f"{v} 秒"


# ★2026-09-06 P0（実機ハング）: 逐次入口の「最初の 1 byte」だけに掛ける番犬。
#   道具付き逐次配信（llama.cpp の --jinja + tools）は、prefill が終わるまで
#   **1 byte も返さない**（実測: 38k tok の接頭辞で first_line が 21.6s。しかも
#   tool_call は最後に一気に届く＝逐次の利点が無い）。first byte が来るまでの間、
#   read timeout(90s) は「1 read の無音」でしか切れないので、prefill が 90s を
#   超える状況（モデル入替・巨大プロンプト・P40 混雑）では **90s×4 の再試行 +
#   一括 90s ＝ 最悪 450s 沈黙**になる（実測で確認）。番犬は「最初の 1 byte が
#   来るまで」だけを別枠で縛り、超えたら stream を切って一括へ落とす。
#   0 以下で無効（read timeout の従来挙動に戻る）。
def _trust_env() -> bool:
    """httpx に OS の HTTP(S)_PROXY / NO_PROXY を読ませるか。

    ★既定 True（OS のプロキシ設定を尊重）。2026-09-06 の顛末: いったん既定 False にしたが、
      owner 実機（利用者の PC）は **proxy 経由でしか 推論機 に届かない**（/health は 200・直結は即失敗）ため
      backend_error(secs 0.0) を招いた＝逆だった。ProxyOverride を無視して LAN を proxy 送りに
      する Windows の罠（getproxies がレジストリを読む）は残るが、それは proxy 側の除外設定で
      対処すべきで、既定で proxy を切ると「proxy 経由でしか届かない機械」を壊す。
      `LAPS_HTTP_TRUST_ENV=0` で proxy を無視（直結）に落とせる。"""
    # ★2026-09-06 訂正: 既定は **True（OS のプロキシ設定を尊重）**。
    #   owner 実機（利用者の PC）は **proxy 経由でしか 推論機 に届かない**（/health は WinINET 経由で 200・
    #   直結は即失敗）。0.1.195 で既定 False（proxy を使わない）にしたら backend_error(secs 0.0)＝
    #   私の「修正」が逆で、直結を試みて届かなくなっていた。0 で明示的に無効化できる。
    v = (os.environ.get("LAPS_HTTP_TRUST_ENV", "") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


#: ★2026-10-06（オーナー「タイムアウトまでの時間を延長して」）。推論機が Qwen3.8-27B（密）になり、読み込み（prefill）が
#:   約 220 tok/s・生成が 7〜12 tok/s に落ちた。窓 64K を丸ごと読み直すと約 300 秒、思考の予算 1,536 ＋本文を一括で
#:   受け取ると数分かかる。旧値（最初の 1 byte 45 秒・応答待ち 90 秒）は前のモデル（MoE・38k tok の読み込みが 21 秒）の
#:   実測で決めた値で、キャッシュが外れた手番では逐次を切って一括へ落ち、その一括も 90 秒で切れうる。
#:   番犬の順（最初の 1 byte ＜ 応答待ち）は保つ。
FIRST_BYTE_TIMEOUT_DEFAULT = 360.0
READ_TIMEOUT_DEFAULT = 600


def _first_byte_timeout() -> float:
    """最初の 1 byte を待つ上限（秒）。LAPS_STREAM_FIRST_BYTE で変えられる。

    既定 360 秒（旧 45 秒・上の注記）: 窓 64K の読み直し（~300s）に余裕を持たせつつ、
    read timeout(600s) より内側で番犬が先に鳴る。0 以下で無効。"""
    raw = os.environ.get("LAPS_STREAM_FIRST_BYTE", "").strip()
    if not raw:
        return FIRST_BYTE_TIMEOUT_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return FIRST_BYTE_TIMEOUT_DEFAULT


class _FirstByteStall(Exception):
    """逐次配信が「最初の 1 byte」の番犬に切られた（prefill が長すぎる/詰まり）。
    呼び出し側はこれを見て、相手の生存を確かめてから一括へ落とす。"""


def thinking_enabled() -> bool:
    """この依頼で思考させるか。serve がアプリの Think から依頼ごとに LAPS_THINK を入れる（既定 on）。"""
    return (os.environ.get("LAPS_THINK") or "1").strip() != "0"


def template_kwargs() -> dict[str, Any]:
    """chat template に渡す思考の設定（Qwen3.8 の書式が読む `enable_thinking` と `reasoning_effort`）。

    ★2026-10-05: oc 中核はこれを**1度も送っていなかった**＝アプリで Think を切っても思考し、深さは書式の
      既定（xhigh＝最も深い）だった。P40 では Qwen3.8-27B の生成が 12〜15 tok/s なので、xhigh の思考は 1 手に
      何分もかかる。既定は low（LAPS_REASONING_EFFORT で xhigh/medium に替えられる）。
    ★2026-10-05 夜: 既定を medium → low。書式は medium のとき**何も足さない**（＝モデル素のまま）ので抑えに
      なっていなかった。オーナー機の実測（0.1.280・n=1）: 85 手番で思考 60,617 tok・102 分（Qwen3.6 の 1 手番
      164 tok に対し 713 tok）。low だけが system の先頭に「思考は短く」を足す。
    ★知らない書式（Qwen3.6 等）は `reasoning_effort` を黙って無視する＝送っても壊れない。"""
    if not thinking_enabled():
        return {"enable_thinking": False}
    return {"enable_thinking": True, "reasoning_effort": reasoning_effort()}


#: やり直しの 1 回だけ、推論機の読み込みの控え（KV キャッシュ）を使わずに頭から読ませる（2026-10-10）。
#: 崩れが控えの使い回しのせいでも、たまたまの脱線でも、同じ道を通らないようにする。
_FRESH_PROMPT = _contextvars.ContextVar("blackwall_fresh_prompt", default=False)


class fresh_prompt:
    """この中で出す要求には `cache_prompt: false` を付ける。"""

    def __enter__(self) -> "fresh_prompt":
        self._tok = _FRESH_PROMPT.set(True)
        return self

    def __exit__(self, *exc: Any) -> None:
        _FRESH_PROMPT.reset(self._tok)


#: 崩れを切ったとき画面に流す一言（やり直す前・中身は捨てる）。
DEGENERATE_NOTE = "\n\n★出力が崩れたので、ここで切って、この手番をやり直します。\n"

_FAMILY_CACHE: dict[str, tuple[float, str]] = {}
_FAMILY_TTL = 60.0
#: ★届かなかったときの控えは短く（2026-10-15・1-E の E4）。1 分控えると、推論機の再起動の間に聞いた 1 回の失敗で
#:   その後 1 分の要求が全部「系統なし」（Qwen3.6 の温度・思考の扱い・促しが外れる）になる。
_FAMILY_FAIL_TTL = 5.0
#: model_family が `/props` から読んだモデルのファイル名（小文字）。同じ 1 分の控え（2026-10-14・Qwen3.6 の扱いを分けるため）。
_MODEL_NAME: dict[str, str] = {}


def model_name(base_url: str | None = None) -> str:
    """推論機に載っているモデルのファイル名（小文字・分からなければ ""）。model_family と同じ `/props` の読みを使う。"""
    model_family(base_url)
    base = (base_url or os.environ.get("LAPS_BACKEND") or "").strip().rstrip("/")
    return _MODEL_NAME.get(base, "")


def _qwen_moe() -> bool:
    """Qwen の MoE（Qwen3.6-35B-A3B など・P40 で 60〜80 tok/s）。密な Qwen3.8-27B（12〜15 tok/s）とは思考の扱いを分ける。"""
    return model_family() == "qwen" and "a3b" in model_name()


def thinking_unbounded() -> bool:
    """手番ごとの思考を推論機の予算で打ち切らない系統（Gemma・GLM・Qwen の MoE）。暴走は思考の長さの天井とループの切りが止める。
    ★2026-10-14 Qwen3.6-35B-A3B を足した（オーナー「QWEN 用に…最適化」・10-13「モデルには自由に考えさせます」）。予算の天井
      （REASONING_BUDGET_CAP 1,536）は Qwen3.8-27B の遅さ（12〜15 tok/s）のための物で、A3B は 5 倍ほど速い。作者の推奨は
      出力 32,768 トークン（モデルカード）。打ち切ると推論機が一言を差し込んで思考を閉じさせる＝難しい手番ほど途中で切れる。"""
    # ★North Mini Code（cohere・2026-10-30）は入れない: 予算なしでは短い関数 1 つに 6,000 トークン考えて本文 0 字（156 秒）、
    #   予算 1,536 なら思考を閉じて 52 秒で答えた（推論機・P40＋2080 SUPER・生成 約 41 tok/s）。
    fam = model_family()
    return fam in ("gemma", "glm") or (fam == "qwen" and _qwen_moe())


def model_family(base_url: str | None = None) -> str:
    """推論機に載っているモデルの系統（"gemma"・"qwen"・""）。2026-10-10 Gemma 4 対応。

    ★出荷済みのクライアントは別名（blackwall-coder）しか知らないので、llama-server の `/props` の
      `model_path`（と書式の印）から決める。1 分だけ控える＝推論機を差し替えたら次の要求から追う。
    ★届かない・llama-server でない（`/props` が無い）ときは ""＝従来どおり（Qwen3.8 の値）。
    `LAPS_MODEL_FAMILY`（gemma・qwen・glm・cohere）で決め打ちできる。"""
    forced = (os.environ.get("LAPS_MODEL_FAMILY") or "").strip().lower()
    if forced:
        return forced
    base = (base_url or os.environ.get("LAPS_BACKEND") or "").strip().rstrip("/")
    if not base:
        return ""
    now = time.monotonic()
    hit = _FAMILY_CACHE.get(base)
    if hit is not None and now - hit[0] < (_FAMILY_TTL if hit[1] or len(hit) < 3 or hit[2] else _FAMILY_FAIL_TTL):
        return hit[1]
    fam = ""
    answered = False
    try:
        root = base[:-3] if base.endswith("/v1") else base
        # ★proxy の設定に従う（2026-10-15・E4）。ここだけ直結にしていたので、proxy 経由でしか届かない機械（記録ではオーナーの 利用者の PC）
        #   では系統が "" になり、Qwen3.6 の設定が全部外れていた。要求の本体（_new_client）と /health は前から _trust_env()。
        r = httpx.get(root + "/props", timeout=3.0, trust_env=_trust_env())
        props = r.json() if r.status_code == 200 else {}
        name = str((props or {}).get("model_path") or "").lower()
        _MODEL_NAME[base] = name.replace("\\", "/").rsplit("/", 1)[-1]
        tmpl = str((props or {}).get("chat_template") or "")
        answered = r.status_code == 200
        if "gemma" in name or "<|turn>" in tmpl:
            fam = "gemma"
        elif "glm" in name or "[gMASK]" in tmpl:
            fam = "glm"                    # GLM-4.7-Flash（2026-10-12 オーナー「これ持ってきて試してみて」「GLM 用に」）
        elif "north" in name or "cohere" in name or "<|START_TEXT|>" in tmpl:
            fam = "cohere"                 # North Mini Code 1.0（Cohere・cohere2moe・2026-10-30 オーナー「とりあえずこれいれます？」）
        elif "qwen" in name:
            fam = "qwen"
    except Exception:                      # noqa: BLE001 — 調整値の話で走行を殺さない
        fam = ""
    _FAMILY_CACHE[base] = (now, fam, answered)       # 答えて系統が無いのは 1 分・届かなかったのは 5 秒
    return fam


def qwen36_sampling() -> str:
    """Qwen3.6 の思考ありの設定の組（"coding" か "general"）。★2026-10-18 オーナー「長期タスクでの内部思考の繰り返しなども
    非常に多く散見されているため、ローカルモデルに合わせて対応してください」。

    作者のモデルカード（huggingface.co/Qwen/Qwen3.6-35B-A3B の Best Practices・2026-10-18 に読んだ）:
      一般の思考      temperature 1.0・top_p 0.95・top_k 20・min_p 0・presence_penalty 1.5
      精密なコーディング temperature 0.6・同・presence_penalty 0（例: WebDev）
      「presence_penalty を 0〜2 で上げると終わらない繰り返しが減る。高いと言語が混ざり、性能が少し落ちることがある」。
    ★2026-10-14 から全部の作業を「精密なコーディング」で走らせていたが、オーナーの長い作業（ゲームの解析・調べ物）は
      カードの言う「一般」で、繰り返しの切り（engine:loop_cut）は 10-14〜18 の 143 走行で 63 回・15 走行に出ていた。
    ⇒ 作業のモードで選ぶ: 開発モード（LAPS_MODE=development）だけ coding、ほかは general。
      LAPS_QWEN_SAMPLING=coding／general で決め打ち（LAPS_TEMPERATURE・LAPS_PRESENCE_PENALTY はさらに優先）。"""
    v = (os.environ.get("LAPS_QWEN_SAMPLING") or "").strip().lower()
    if v in ("coding", "general"):
        return v
    try:
        from .oc import modes as _modes
        return "coding" if _modes.current() == "development" else "general"
    except Exception:
        return "coding"


def _qwen36_general(thinking: bool) -> bool:
    return bool(thinking) and model_family() == "qwen" and "qwen3.6" in model_name() and qwen36_sampling() == "general"


def sampling_params() -> dict[str, Any]:
    """生成の設定を**1箇所で決める**（2026-08-19・web 調査の指摘で判明）。

    ★それまで三重帳簿だった:
        ① Rust の `sampling.rs` が 0.6 / 0.95 / 20（**Qwen3 の thinking 版の値**）
        ② Python はここで **temperature しか送っていなかった**
        ③ その結果 **サーバ既定が黙って効いていた**（実測 `/props`:
           top_p 0.95 / top_k 40 / min_p 0.05 / repeat_penalty 1.0）
      ＝実際に効いていた③は**コードのどこにも書かれていない**ので、
        読んでも気づけない。誰も top_k 40 を選んでいない。

    ★採る値は**モデルの作者が公開している推奨**（Qwen3-Coder-30B-A3B-Instruct の
      `generation_config.json`）: temperature 0.7 / top_p 0.8 / top_k 20 /
      repetition_penalty 1.05。
      ★Coder-Next(80B) の 1.0/0.95/40 と混同しない（別モデルの値）。

    ★**効くかどうかは別の話。** ここで直したのは「選んでいない値が効いていた」
      ことであって、推奨値が手元で良いという測定はまだ無い。
      env で上書きできるようにして A/B できる形にしてある。
      測るなら合否ではなく **`loop_detected` の発火数と反復数**で見る
      （サンプリングは「詰まりやすさ」に効く）。
    """
    def _f(name: str, default: float) -> float:
        try:
            v = os.environ.get(name)
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    # ★2026-10-05: 推論機を Qwen3.8-27B へ替えたので、その作者の推奨（モデルカードの Best Practices）に
    #   合わせ、思考の有無で分ける（旧 0.8 / 20 / 1.05 は Qwen3-Coder-30B の値だった）:
    #     思考 on   temperature 1.0 / top_p 0.95 / top_k 20 / min_p 0 / presence 0   / repetition 1.0
    #     思考 off  temperature 0.7 / top_p 0.80 / top_k 20 / min_p 0 / presence 1.5 / repetition 1.0
    #   思考の有無は serve が依頼ごとに入れる LAPS_THINK（アプリの Think）。env の上書きはそのまま効く。
    thinking = thinking_enabled()
    # ★2026-10-10 Gemma 4（オーナー「Gemma に対応させますか」）: 作者の推奨は用途・思考の有無を問わず
    #   temperature 1.0 / top_p 0.95 / top_k 64（モデルカードの Best Practices・generation_config.json）。
    gemma = model_family() == "gemma"
    # ★2026-10-12 GLM-4.7-Flash（オーナー「GLM 用に」）: Z.ai の推奨（unsloth の案内・道具を使う用途）
    #   temperature 0.7 / top_p 1.0 / min_p 0.01 / repeat_penalty 1.0（llama.cpp の既定 min_p 0.05 は使わない）。top_k は切る（0）。
    glm = model_family() == "glm"
    # ★2026-10-30 North Mini Code 1.0（Cohere）: 作者の推奨は temperature 1.0 / top_p 0.95 だけ（評価も vLLM の既定＝top_k・min_p なし）。
    #   思考は書式（テンプレート）の既定で常に入るので、思考の有無で分けない。
    cohere = model_family() == "cohere"
    _rep = _f("LAPS_REPEAT_PENALTY", 1.0)
    out: dict[str, Any] = {
        "top_p": _f("LAPS_TOP_P", 1.0 if glm else 0.95 if (thinking or gemma or cohere) else 0.8),
        "top_k": int(_f("LAPS_TOP_K", 0 if (glm or cohere) else 64 if gemma else 20)),
        # ★Gemma だけ 0.05（2026-10-10・llama.cpp の既定と同じ）。崩れた 2 回はどちらも 1 語目から文脈の切れ端を拾い始めた。
        #   温度 1.0・min_p 0 では、一番ありそうな語の 1/20 にも届かない語まで引ける。その裾だけを切る（上位の語の順は変えない）。
        "min_p": _f("LAPS_MIN_P", 0.01 if glm else 0.05 if gemma else 0.0),
        "presence_penalty": _f("LAPS_PRESENCE_PENALTY",
                               1.5 if _qwen36_general(thinking) else 0.0 if (thinking or gemma or glm or cohere) else 1.5),
        # ★キー名を**両方**載せる（2026-09-09）。実測でこう確かめた:
        #     {"repetition_penalty":1.77} を投げて /slots → repeat_penalty=1.0（黙って捨てられた）
        #     {"repeat_penalty":1.66}    を投げて /slots → repeat_penalty=1.66（効いた）
        #   `repetition_penalty` は HF transformers / vLLM の綴りで、llama.cpp は
        #   `repeat_penalty` を使う。**未知のキーは黙って捨てられるので両方載せて害は無い**。
        #   このプロジェクトは llama-server / llama-swap / Ollama / vLLM を等しく相手に
        #   すると README が宣言しているので、どちらか一方に賭けないほうが正しい。
        #
        #   ★これは退行の修理ではなく、**意図した 1.05 が初めて効く変更**である。
        #     2026-08-19 に「三重帳簿を1箇所に畳んだ」commit で綴りだけが取り残されていた。
        #     以後の全記録は実効 1.0（llama.cpp 既定）で走っている＝**前後を直接比べられない**。
        #   ★症状「同じ出力が繰り返される」の対策として読まないこと。
        #     サーバ実測の `repeat_last_n` は 64＝直近64トークンの窓なので、
        #     **ターンをまたぐ「同じ返答が15回」には原理的に効かない**。
        #     その繰り返しは REPORTS/devbench-20260823.md:46 が事象列から
        #     「Reset に上限が無かった」と特定し、ハーネス側の修正だけで
        #     loop 0／未完 0（p=0.009・n=15/腕）になっている。
        #   ★効き始めたら `tool_call` 率を見張ること（旧記録「高 T や強い penalty は
        #     tool_call 率を下げる」）。落ちれば早期終了と精度が悪化する。
        #   ★`repeat_last_n` はここでは触らない。窓を変えるのは新しい調整判断であって、
        #     測定を出してから入れる（REQUIREMENTS R4）。
        "repetition_penalty": _rep,
        "repeat_penalty": _rep,
    }
    if _FRESH_PROMPT.get():
        out["cache_prompt"] = False
    # ★2026-09-17（複数モデル対応）: **方言に合わないキーを落とす。**
    #   llama.cpp は未知のキーを黙って捨てるのでここまで害が無かったが、
    #   厳しい API（Moonshot 等）は **400 を返す**。銘柄が "openai" 方言なら
    #   `repeat_penalty` / `top_k` などの独自キーは送らない。
    #   ★握りつぶさない: 銘柄が引けないときは**既定（ラボ内）の方言のまま**にする。
    try:
        from . import models as _models
        _p = _models.PROFILES.get(
            (os.environ.get("LAPS_PROFILE") or _models.DEFAULT_PROFILE).strip())
        if _p is not None and _p.dialect != "llamacpp":
            for _k in ("repeat_penalty", "repetition_penalty", "repeat_last_n",
                       "top_k", "min_p", "typical_p", "tfs_z", "mirostat", "cache_prompt"):
                out.pop(_k, None)
    except Exception:                      # noqa: BLE001 — 調整値の話で走行を殺さない
        pass
    # ★種は**設定されたときだけ**載せる（2026-08-19 追加）。
    #   常に載せると出荷時の振る舞いが決定論になり、利用者が同じ質問を
    #   し直しても同じ答えしか返らない——不具合の回避手段を1つ奪う。
    #
    #   要る理由: 腕を比べる測定で「ブロックごとに同じ種・腕ごとに違う条件」に
    #   すると対応のある比較になり、分散が大きく減る。刻みの効果より
    #   対照群自身の揺れのほうが大きかった前例があるので、これは飾りではない
    #   （同一設定の対照3回で 2/30・5/30・6/30）。
    #
    # ⚠ **2026-08-19 実測: 手元の llama-server は種を尊重しなかった。**
    #   同じ種で2回叩いて別の出力が返る（`--cont-batching` ＋ n_slots=4 が原因と
    #   推測するが未検証）。**受け付けることと効くことは別。**
    #   対応のあるブロック設計をやるなら、`--parallel 1` などで決定論になることを
    #   先に確かめること。確かめずに「種で揃えた」と書いてはいけない。
    seed = (os.environ.get("LAPS_SEED") or "").strip()
    if seed:
        try:
            out["seed"] = int(seed)
        except ValueError:
            pass
    return out


def temperature() -> float:
    """温度も**1箇所で決める**（2026-08-19 追加）。

    ★上の三重帳簿を畳んだとき、**温度だけ畳み忘れていた**——agent.py の
      3 箇所に `temperature=0.7` が直書きされたまま残っていた。同じ形の
      バグが同じファイルに残るのは、畳んだ範囲を「サンプリング設定」と
      呼んで温度をその外側だと思ったため。

    ★既定 0.7 は Qwen3 の `generation_config.json` の推奨。
      ただし**モデルによって動作点が違う**: 旧 BLACKWALL の
      チューニング済みモデル（Ω-J' = Qwen3-32B + omega_j_prime LoRA）は
      **本番でクライアントが 0.05 を送る前提**で調整されていた
      （記録: .5/CLAUDE_INFERENCE_TUNE.md「高 T や強い penalty は
      tool_call 率を下げる」）。0.7 のまま測ると、調整不良の状態を
      測って「このモデルは弱い」と誤結論する。
    """
    # ★2026-10-05: 既定は思考の有無で分ける（Qwen3.8 の推奨 1.0 / 0.7・上の sampling_params の注記）。
    #   Gemma 4 は思考の有無を問わず 1.0（2026-10-10・model_family の注記）。
    #   GLM-4.7-Flash は道具を使う用途の推奨 0.7（2026-10-12・sampling_params の注記）。
    # ★2026-10-14 Qwen3.6（オーナー「QWEN 用に…最適化」）: 作者の推奨（モデルカード）は思考ありの一般 1.0（presence 1.5）と
    #   「正確なコーディング（WebDev など）」0.6（presence 0）。Blackwall は開発とインフラの作業なので後者。presence は元から 0。
    # ★2026-10-18 作業のモードで分ける（qwen36_sampling の注記）: 開発モードは「精密なコーディング」0.6、ほかは「一般」1.0。
    fam = model_family()
    default = 0.7 if fam == "glm" else 1.0 if (thinking_enabled() or fam in ("gemma", "cohere")) else 0.7
    if fam == "qwen" and thinking_enabled() and "qwen3.6" in model_name():
        default = 0.6 if qwen36_sampling() == "coding" else 1.0
    try:
        v = os.environ.get("LAPS_TEMPERATURE")
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


# --------------------------------------------------------------------------
# 推論機が別枠に出す思考を本文へ戻す
# --------------------------------------------------------------------------
#
# ★2026-09-21 実測（友人の実機 KOSEI の記録から辿った）。
#   llama.cpp を `--jinja` で動かすと、思考する模型の `<think>…</think>` は
#   **サーバ側で抜かれて `message.reasoning_content` に移る**。BLACKWALL は
#   `content` しか読んでいなかったので、思考は「隠れていた」のではなく
#   **丸ごと捨てられていた**。
#
#   実測（blackwall-coder = Qwen3.6-35B-A3B abliterated, 2026-09-21）:
#       max_tokens=2000 → reasoning 1617 字 / content 226 字  ＝ 生成の 88% を破棄
#       max_tokens= 400 → reasoning  400 字 / content   0 字  ＝ **本文が空で返る**
#   後者が「開発で使うと問題が起きる」の中身のひとつ: 空の本文を受け取った
#   ハーネスが「実質空」と判定して押し戻し、予算だけが溶ける。
#
#   ここで `<think>` に戻しておくと、既にある経路がそのまま全部生きる:
#   serve.ThinkSplitter が think イベントに割り、TUI と WebUI が表示し、
#   `_clean_reply` が最終本文からは外し、記録の think.chars が数え始める。
#
# ⚠ 戻した思考には**下書きの `<tool_call>` が混じりうる**。実行してよいのは
#   確定した呼び出しだけなので、抽出側は think を落としてから走査すること
#   （`agent._extract_tool_calls_guarded`）。
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

#: 思考が入ってくるフィールド名。提供元で違う（opencode が設定で 3 種を受けており、
#: 実際に出回っている綴りがこれだけある）。最初に見つかった非空を使う。
REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")


def _reasoning_of(msg: dict) -> str:
    for k in REASONING_FIELDS:
        v = msg.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def inline_reasoning(msg: dict) -> str:
    """別枠の思考を `<think>…</think>` として本文の先頭へ戻す。

    ★本文に既に `<think>` があっても**捨てずに前へ足す**（OpenHands と同じ。
      あちらは `reasoning_content` と本文中の think を**連結**している）。
      閉じた塊を前に置くだけなので入れ子にはならず、ThinkSplitter は
      2 つの思考区間として正しく割る。
    """
    content = msg.get("content") or ""
    reasoning = _reasoning_of(msg)
    if not reasoning:
        return content
    return f"{THINK_OPEN}{reasoning}{THINK_CLOSE}{content}"


#: 思考の後の本文が英語だけで続いたら思考の続きとみなす字数（空白を除く）。書き出し／途中。
LEAK_START_CHARS = 160
LEAK_RUN_CHARS = 600
_WS_RE = re.compile(r"\s+")
_LEAK_JA = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_LEAK_LINE_HEAD = re.compile(r"^[\s#>*\-+0-9.、。・「『（(]*")
#: 本文に流れた思考を振り分け直す言語（oc が Gemma で返答の言語が分かっているときだけ入れる・leak_guard）。
_LEAK_LANG: "_contextvars.ContextVar[str]" = _contextvars.ContextVar("blackwall_leak_lang", default="")


class leak_guard:
    """この中の逐次受信では、思考の後に英語だけで書き出した本文を思考の続きとして扱う（ReasoningInliner）。"""

    def __init__(self, lang: str) -> None:
        self.lang = lang or ""

    def __enter__(self) -> "leak_guard":
        self._tok = _LEAK_LANG.set(self.lang)
        return self

    def __exit__(self, *exc: Any) -> None:
        _LEAK_LANG.reset(self._tok)


class ReasoningInliner:
    """逐次配信版。`delta.reasoning_content` を `<think>` で包んで流す。

    思考は本文より先に来る。最初の思考の断片で開き、本文が始まった時点
    （または終了時）で閉じる。開いていなければ閉じない。

    ★leak_lang（2026-10-10・Gemma だけ・oc が leak_guard で入れる）: Gemma 4 は思考を閉じた後も本文の側で英語のまま考え続ける
      （オーナー「内部思考が本文に出てきてしまっています」）。午後の走行の 4 手番目は、本文を日本語で 13 行書いた後に英語の分析が
      約 415 行続き（英字 11,314 対かな漢字 237）、その中で同じ段落を繰り返して切られた。本文は行ごとに見る:
        ・日本語を含む行・コードの塊の中の行は、すぐ本文として流す（日本語の答えの見え方は変えない）
        ・英語だけの行は溜め、書き出しなら LEAK_START_CHARS、途中なら LEAK_RUN_CHARS（空白を除く）続いたら、そこから思考の続きとして流す
        ・思考の続きの途中で日本語で始まる行が来たら、そこから本文に戻す
      振り分けた文は `leaked` に残す（oc が答えの空いた手番の扱いに使う）。
    """

    def __init__(self, leak_lang: str = "") -> None:
        self._open = False
        self._lang = leak_lang or ""
        self._mode = "pass"          # "pass" 本文 / "leak" 思考の続き
        self._buf = ""               # まだ流していない行の途中
        self._eng: list[str] = []    # 溜めている英語だけの行
        self._fence = False
        self._started = False        # この思考の後に本文を 1 字でも流したか
        self.leaked = ""
        self.reclassified = 0

    def feed(self, delta: dict) -> str:
        """この delta が本文として流すべき文字列。無ければ空。"""
        out = ""
        r = _reasoning_of(delta)
        if r:
            out += self._settle(thought_follows=True)
            if not self._open:
                out += THINK_OPEN
                self._open = True
            out += r
            if self._lang:
                self._mode, self._started = "pass", False
        c = delta.get("content") or ""
        if c:
            out += self._content(c) if self._lang else (self._close_think() + c)
        return out

    # ── 本文の振り分け（leak_lang のときだけ）──
    def _close_think(self) -> str:
        if not self._open:
            return ""
        self._open = False
        return THINK_CLOSE

    def _body(self, s: str) -> str:
        if not s:
            return ""
        self._started = True
        return self._close_think() + s

    def _held_chars(self) -> int:
        return len(_WS_RE.sub("", "".join(self._eng) + self._buf))

    def _flush_eng(self) -> str:
        held, self._eng = "".join(self._eng), []
        return self._body(held)

    def _to_leak(self, with_buf: bool = True) -> str:
        text = "".join(self._eng) + (self._buf if with_buf else "")
        self._eng = []
        if with_buf:
            self._buf = ""
        self._mode = "leak"
        self.reclassified += 1
        self.leaked += text
        out = "" if self._open else THINK_OPEN
        self._open = True
        return out + text

    def _line(self, line: str) -> str:
        if line.lstrip().startswith("```"):
            self._fence = not self._fence
            return self._flush_eng() + self._body(line)
        if self._fence or _LEAK_JA.search(line):
            return self._flush_eng() + self._body(line)
        self._eng.append(line)
        return ""

    def _content(self, c: str) -> str:
        if self._mode == "leak":
            return self._leak_feed(c)
        out = ""
        self._buf += c
        while "\n" in self._buf and self._mode == "pass":
            line, self._buf = self._buf.split("\n", 1)
            out += self._line(line + "\n")
            if self._eng and len(_WS_RE.sub("", "".join(self._eng))) >= (LEAK_RUN_CHARS if self._started else LEAK_START_CHARS):
                out += self._to_leak(with_buf=False)
        if self._mode == "leak":
            rest, self._buf = self._buf, ""
            return out + (self._leak_feed(rest) if rest else "")
        if self._buf and (self._fence or _LEAK_JA.search(self._buf)):
            out += self._flush_eng() + self._body(self._buf)
            self._buf = ""
        elif self._held_chars() >= (LEAK_RUN_CHARS if self._started else LEAK_START_CHARS) and \
                len(re.findall(r"[A-Za-z]", "".join(self._eng) + self._buf)) >= 40:
            out += self._to_leak()
        return out

    def _settle(self, thought_follows: bool = False) -> str:
        """溜めた英語の行を片付ける（思考が続く・終わり）。書き出しから英語だけなら思考の続き、それ以外は本文。"""
        if not self._lang or self._mode != "pass" or not (self._eng or self._buf):
            return ""
        held = "".join(self._eng) + self._buf
        if (not self._started or thought_follows) and not self._fence and not _LEAK_JA.search(held) \
                and len(re.findall(r"[A-Za-z]", held)) >= 40:
            return self._to_leak()
        self._eng, self._buf = [], ""
        return self._body(held)

    def _leak_feed(self, c: str) -> str:
        m = _LEAK_JA.search(c)
        if m:
            start = c.rfind("\n", 0, m.start()) + 1
            line = c[start:]
            if _LEAK_JA.match(_LEAK_LINE_HEAD.sub("", line, count=1) or " "):
                self._mode = "pass"                 # 日本語で始まる行から本文に戻す
                self.leaked += c[:start]
                return c[:start] + self._body(line)
        self.leaked += c
        out = "" if self._open else THINK_OPEN
        self._open = True
        return out + c

    def discard_held(self) -> None:
        """溜めている本文を捨てる（繰り返しを切ったとき）。"""
        self._eng, self._buf = [], ""

    def close(self) -> str:
        """開いていれば閉じタグを返す（溜めている本文があれば先に片付ける）。"""
        out = self._settle()
        return out + self._close_think()


def merge_tool_call_deltas(
    deltas: list[dict[str, Any]],
    acc: dict[int, dict[str, Any]],
    done: list[dict[str, Any]],
) -> None:
    """逐次配信で届く道具呼び出しの断片を併合する（状態は呼び手が持つ）。

    ★なぜ index だけで束ねないか（2026-08-18 の調査／llama.cpp #18591）:
      ある版は**複数の呼び出しを全部 `index: 0`** で送ってくる。index を鍵にすると

          名前 : 上書きされて最後のものだけ残る
          引数 : `{"a":1}{"b":2}` と連結されて JSON として壊れる
               → `__unparsed_arguments__` のまま実行へ渡る

      結果は「道具が空振りした」ように見え、記録上は成功と区別がつかない。
      実機の「道具を呼ばず記事を書いて終わる」に化ける経路の1本。

    ★名前は呼び出しごとに1度しか来ないので、区切りに使える。
      index が正しく振られているサーバでは名前は別 index へ届くので、この規則は
      発火しない＝**正しい側は壊さない**（試験に負の対照あり）。

    ★この関数は `_chat_with_tools_stream` から呼ばれる**唯一の併合規則**。
      試験も直接これを呼ぶ（規則を試験側に写すと、写しだけが古びて気づけない）。
    """
    for tc in deltas:
        i = tc.get("index", 0)
        fn = tc.get("function") or {}
        name = fn.get("name")
        slot = acc.get(i)
        if slot is not None and name and slot["name"]:
            done.append(slot)          # 2つ目の名前＝ここが次の呼び出しの始まり
            slot = None
        if slot is None:
            slot = {"name": "", "args": []}
            acc[i] = slot
        if name:
            slot["name"] = name
        piece = fn.get("arguments")
        if piece:
            slot["args"].append(piece)


def _degenerate_call(acc: dict[int, dict[str, Any]]) -> str | None:
    """併合中の道具呼び出しのどれかの引数の末尾が崩れていれば、その単位を返す（loopcut.degenerate_args）。
    末尾の数千字だけを見る（4 万字を毎回つなげない）。"""
    want = max(_loopcut.ARGS_MIN_CONTENT, _loopcut.LONG_MAX_PERIOD * _loopcut.LONG_REPS) * 2
    for slot in acc.values():
        parts, n = [], 0
        for piece in reversed(slot["args"]):
            parts.append(piece)
            n += len(piece)
            if n >= want:
                break
        unit = _loopcut.degenerate_args(slot["name"], "".join(reversed(parts)))
        if unit is not None:
            return unit
    return None


def finish_tool_calls(
    acc: dict[int, dict[str, Any]],
    done: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """併合した枠を呼び出しに確定する。届いた順（確定済み → 残り index 順）。"""
    def _one(slot: dict[str, Any]) -> dict[str, Any]:
        raw = "".join(slot["args"]) or "{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError as _e:
            # 一括経路と同じ扱い。黙って直さない＝壊れていることが見えるように。
            # 受け側 `agent._exec_tool` がここで止めて、モデルへ差し戻す。
            # ★2026-09-22: **解けなかった理由**も一緒に運ぶ。
            #   実測 21 件を後から分類しようとしたら、記録は先頭 300 字だけで
            #   **理由も全長も残っておらず、何が壊れているか数えられなかった**。
            #   ★直す前に「測れるようにする」。理由・位置・全長は 1 行で足りる。
            args = {"__unparsed_arguments__": raw,
                    "__parse_error__": f"{_e.msg} (pos {_e.pos} / 全長 {len(raw)})"}
        return {"name": slot["name"], "input": args}

    return [_one(s) for s in done] + [_one(acc[i]) for i in sorted(acc)]


#: 既定のモデル名。★端末側（term/config.py の DEFAULTS）と**同じ値**にする。
#:   2026-08-30 のレビューまで、ここだけ `blackwall-think-omega-j` で、端末側は
#:   `blackwall-coder` だった。端末は毎回 LAPS_MODEL を渡すので端末経由では
#:   気づけず、**環境変数を渡さない経路だけ**（blackwall-legacy・ライブラリ直呼び・
#:   spec.model を持たない serve 要求）が、推論機に無い名前を要求して落ちていた。
#:   ＝「手元では動くのに、その経路だけ動かない」の形。値は1か所に寄せる。
DEFAULT_MODEL = "default"
DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"


@dataclass
class BackendConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = "sk-no-key-required"
    model: str = DEFAULT_MODEL
    timeout: float = 600.0
    #: 接続を張るまでの上限。★生成待ち(timeout)と分ける。
    #:   両方を 600 秒にしていたため、推論機が落ちているとき**接続待ちだけで**
    #:   最長 600 秒×再試行4回、画面には理由が1行も出なかった（2026-08-16 敵対レビュー）。
    #:   相手が居ないことは数秒で分かる。分からないふりをして待たない。
    connect_timeout: float = 5.0
    max_retries: int = 3
    retry_base: float = 1.5  # seconds



def _reachable(url: str) -> str:
    """届く住所を選ぶ（公開版では与えられた住所をそのまま使う）。"""
    return url

#: _from_profile が銘柄表から LAPS_N_CTX に入れた値（利用者の指定ではない＝推論機が答えた窓で上書きしてよい）。
PROFILE_SEEDED_N_CTX: str | None = None


def _from_profile() -> dict:
    """銘柄表（`laps.models`）から接続の既定を組む。

    ★2026-09-17、複数モデル対応で入れた。**個別の環境変数のほうが強い**——
      いままでどおり `LAPS_BACKEND` だけ差し替える使い方を壊さないため。
    ★銘柄が外部（ラボの外）なら `models.get()` が fail-closed で断る。
      ここで握りつぶさない: 断られた事実は**利用者に見える**べきもの。
    ★窓は銘柄が持つ。`LAPS_N_CTX` を忘れて 32K のつもりで 256K を使う、
      という取り違えを構造で消す（窓の見積りは既に最大 9,168 tok ずれている）。
    """
    from . import models as _models

    try:
        p = _models.get()
    except (KeyError, _models.NotAllowed):
        if os.environ.get("LAPS_PROFILE"):
            raise                       # 明示で選んだなら黙らない
        p = _models.PROFILES[_models.DEFAULT_PROFILE]
    # ★入れた値を覚える（2026-10-16 の点検）: 推論機の /props が答えた窓は、ここで入れた銘柄表の値より強い
    #   （Agent._ctx_window が上書きする）。利用者が自分で指定した LAPS_N_CTX は上書きしない。
    global PROFILE_SEEDED_N_CTX
    if not os.environ.get("LAPS_N_CTX"):
        _envcompat.setenv("LAPS_N_CTX", str(p.n_ctx))
        PROFILE_SEEDED_N_CTX = str(p.n_ctx)
    if not p.native_tools:
        _envcompat.setdefault("LAPS_NATIVE_TOOLS", "0")
    return {
        "base_url": _reachable(os.environ.get("LAPS_BACKEND", p.base_url)),
        "api_key": os.environ.get("LAPS_API_KEY") or p.api_key(),
        "model": os.environ.get("LAPS_MODEL", p.model),
    }


class Backend:
    def __init__(self, cfg: BackendConfig | None = None) -> None:
        self.cfg = cfg or BackendConfig(**_from_profile())
        #: ★read timeout で送り直すとき呼ぶ `(attempt, waited_secs)`。agent が
        #:   `notice kind=engine_retry` にして流す。無ければ黙って送り直す（従来）。
        self.on_wait: "Callable[[int, float], None] | None" = None
        self._read_to = 90.0
        self._client = self._new_client()
        self._inflight: set = set()     # 飛行中の Response（abort_inflight が閉じる）
        # Ω-O: when True, _ensure_open is a no-op so cancel actually cancels.
        self._hard_closed = False
        #: この Backend が**一度でも応答を受けたか**。接続できないときの扱いを分ける（下の `_await_peer_back`）。
        self._ever_ok = False
        # ★直近の中断（abort_inflight）が実際に何を切ったか（2026-09-03・item B）。
        #   「止めたのに 73 秒止まらなかった」を後から追うには、abort が
        #   **socket を1本でも掴めたか**（掴めなければ黙って何もしていない）と
        #   所要が要る。掴めなかった＝要求送信中で pool を辿れなかった、の疑い。
        #   serve が cancel を受けた直後に読み、`notice kind=cancel_applied` に載せる。
        self.last_abort: dict[str, Any] = {}
        # 推論器は毎回 usage を返しているのに読み捨てていた。UI に「消費」を出す
        # 側が自分で数え直すと、必ず本物とずれた数字が並ぶ（char/4 の見積りと
        # 実測を混ぜたまま比べられなくなる）。ここで足しておけば、出す側は
        # 「本物か見積りか」を reported_calls で見分けられる。
        self.usage: dict[str, int] = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "calls": 0, "reported_calls": 0,
        }
        #: ★繰り返しで切った直後の要求だけ、presence_penalty を上げて送る（_sampling・2026-10-15 オーナー「進めてください」）。
        self._loop_escape_next = False
        # 直近1回の prompt_tokens。累積だけだと「今回のターンの予測が当たったか」
        # が確かめられない。予算の会計にバグがあるのか、モデルの出来が悪いのか、
        # この1つの数字だけが両者を分ける（context_budget の較正に使う）。
        self.last_prompt_tokens: int | None = None
        # ★直近1回の finish_reason。2026-08-16 まで**一度も読んでいなかった**。
        #   そのせいで「上限で切られた」と「モデルが自分で切った」を区別できず、
        #   記録の stop.reason には length が**構造上いつまでも出ない**状態だった。
        #   実機で「字数制限のため切りました」と書く回が 6/40 あり、それが本当に
        #   切られたのかを議論する材料が無かった（結局この値が要る、という結論）。
        #   None は「推論器が返さなかった」。空文字にしない＝欠測と値を混ぜない。
        self.last_finish_reason: str | None = None

    def _begin_request(self) -> None:
        """要求の頭で、直近 1 回の値を消す（2026-10-15・E7）。

        ★繰り返しで切った・usage を返さない推論機・失敗した要求では `_record_usage` が呼ばれず、前の要求の
          last_prompt_tokens がそのまま残って、要約を始めるかの判定と画面の目盛りが古い値で動いていた。
          None なら oc は送った文字から見積もる（loop._used_tokens）。"""
        self.last_prompt_tokens = None
        self.last_finish_reason = None

    def _record_usage(self, data: dict | None, *, count_call: bool = True) -> None:
        """1回分の usage を積む。無ければ calls だけ増やす。決して raise しない。

        ★逐次の経路は要求の頭で calls を数えるので、usage の塊では数えない（count_call=False・2026-10-15・E3）。
          finish_reason も塊が別（finish_reason の塊の後に choices が空の usage の塊）なので、ここでは上書きしない。"""
        self._ever_ok = True          # 応答を 1 回受けた＝以後の接続失敗は「途中で相手が落ちた」
        if count_call:
            self.usage["calls"] += 1
            self.last_prompt_tokens = None
            self.last_finish_reason = None
        try:
            ch = ((data or {}).get("choices") or [{}])[0]
            fr = ch.get("finish_reason")
            if isinstance(fr, str) and fr:
                self.last_finish_reason = fr
        except Exception:
            pass
        try:
            # ★2026-10-13: llama-server は最後の塊に timings（生成だけの数と時間）を付ける。合計の時間で割ると読み込みの
            #   時間まで入り、Gemma 4 の 1 手番目が「5.1 tok/s」と出た（生成は 30〜50）。生成の速さはこちらで数える。
            tm = (data or {}).get("timings") or {}
            pn, pms = tm.get("predicted_n"), tm.get("predicted_ms")
            if isinstance(pn, (int, float)) and isinstance(pms, (int, float)) and pn > 0 and pms > 0:
                self.usage["gen_tokens"] = self.usage.get("gen_tokens", 0) + int(pn)
                self.usage["gen_ms"] = self.usage.get("gen_ms", 0) + int(pms)
            # ★読み直した数と時間・控え（KV キャッシュ）から使った数も拾う（2026-10-18・エージェント全体の点検）。
            #   要約が 1 走行の中央値で 163 秒（最長 33 分）かかっていたが、読み直しのせいか生成のせいかを記録から分けられなかった。
            #   llama-server の timings は prompt_n（読んだ数）・prompt_ms（その時間）・cache_n（控えから使った数）を返す。
            for _src, _dst in (("prompt_n", "read_tokens"), ("prompt_ms", "read_ms"), ("cache_n", "cached_tokens")):
                _v = tm.get(_src)
                if isinstance(_v, (int, float)) and _v >= 0:
                    self.usage[_dst] = self.usage.get(_dst, 0) + int(_v)
        except Exception:
            pass
        try:
            u = (data or {}).get("usage") or {}
            if not u:
                return
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                v = u.get(k)
                if isinstance(v, (int, float)):
                    self.usage[k] += int(v)
            if isinstance(u.get("prompt_tokens"), (int, float)):
                self.last_prompt_tokens = int(u["prompt_tokens"])
            self.usage["reported_calls"] += 1
        except Exception:
            pass

    def _new_client(self) -> httpx.Client:
        # ★read の待ちを短くする（2026-08-23・実機が 600 秒ハングした）。
        #   旧: Timeout(600, connect=5) ＝ 繋がったあと**1バイトも来なくても 600 秒**待つ。
        #   実機は手元の推論器（127.0.0.1:8760）へ送り、それが応答せず
        #   「600 秒のあいだ Python から1バイトも届きませんでした」で落ちた。
        #   その間ずっと本体が塞がり、利用者は「まだ最初の応答が来ていません（455秒）」を見た。
        #
        #   逐次配信なので、**最初の塊が来るまで**の待ちと、**塊と塊の間**の待ちは
        #   同じ read timeout で縛れる。90 秒来なければ推論器側が詰まっている
        #   （健全な P40 は 11 秒で最初の塊を返す。6,800 tok の接頭辞でも 5.5 秒）。
        #   長い生成そのものは塊が続く限り切れない（read は「次の塊まで」の待ち）。
        #   ★止まるより失敗するほうがよい。失敗は読めるが、沈黙は読めない。
        read_to = float(os.environ.get("LAPS_READ_TIMEOUT", str(READ_TIMEOUT_DEFAULT)) or READ_TIMEOUT_DEFAULT)
        self._read_to = read_to
        return httpx.Client(
            trust_env=_trust_env(),
            base_url=self.cfg.base_url,
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            timeout=httpx.Timeout(connect=self.cfg.connect_timeout,
                                  read=read_to, write=30.0, pool=10.0),
        )

    def _stream_once_retry(self, fn, *, can_retry: "Callable[[], bool] | None" = None):
        """逐次呼び出しを 1 回だけ、**相手が生きていると確かめられたときだけ**やり直す。

        ★2026-08-24 VDIR run7: 一過性の断で 240 秒待って落ちたが、P40 自身は 22K トークンを
          4 秒で処理していた（要求がサーバに届いていない）。/health が 2 秒で応えるなら 1 回
          やり直す。死んでいれば従来どおり即諦める（600 秒ハングの教訓は保つ）。
        ★`can_retry` が False を返す（もう文字を流した）ならやり直さない（2026-10-15・E2）。やり直すと、流した文がもう一度頭から流れ、
          画面に同じ文が二重に並んでいた。"""
        last: Exception | None = None
        for _try in range(4):                     # 1 回 + 生存確認つき再試行 3 回
            try:
                return fn()
            except httpx.ReadTimeout as e:
                last = e
                if self._hard_closed or (can_retry is not None and not can_retry()) or not self._peer_alive():
                    break
                # ★2026-08-24: 推論機 の NIC RX drop で大きな POST が間欠的に落ちる実測。
                #   相手が生きていると確かめられる限り、走行を失敗させるより粘る
                #   （長時間タスクでは「生き延びる」が「早く諦める」より価値が高い）。
                self.usage["timeout_retry"] = self.usage.get("timeout_retry", 0) + 1
                if self.on_wait is not None:
                    try:
                        self.on_wait(_try + 1, self._read_to)
                    except Exception:
                        pass
        raise RuntimeError(
            f"推論機が応答しません: {self.cfg.base_url}"
            "（read timeout まで待っても最初の応答が来ませんでした）。"
            "宛先が正しいか、その推論機が動いているかを Connection で確かめてください。"
        ) from last

    def _await_peer_back(self) -> bool:
        """**走行の途中で**推論機に繋がらなくなったとき、戻ってくるのを待つ。戻れば True。

        ★2026-09-20・敵対レビュー F2-1: 旧形は `ConnectError` を**即座に**失敗にしていた。
          1 通目には正しい（居ない相手を待たない・`test_backend_unreachable.py` の約束）。
          だが 2 時間走った後の接続拒否は別の事実で、たいていは**推論機の再起動**（モデルの切替・置き場の移動）。
          llama-server が 21GB を読み直すのに 15〜26 秒かかり（実測）、その間の 1 回の拒否で走行が丸ごと終わっていた。
          OSS: aider は接続エラーを再試行の対象にし、待ちが 60 秒を超えるまで続ける（`aider/models.py:26,1045-1079`）。
        ★待つのは**一度でも応答を受けた後だけ**。中断（`_hard_closed`）は即座に抜ける。待っている間は `on_wait` で知らせる（沈黙にしない）。
        """
        if not self._ever_ok or self._hard_closed:
            return False
        try:
            limit = float(os.environ.get("LAPS_RECONNECT_SECS", "") or 90)
        except ValueError:
            limit = 90.0
        if limit <= 0:
            return False
        t0 = time.time()
        n = 0
        while time.time() - t0 < limit and not self._hard_closed:
            if self._peer_alive():
                self.usage["reconnected"] = self.usage.get("reconnected", 0) + 1
                return True
            n += 1
            if self.on_wait is not None:
                try:
                    self.on_wait(n, time.time() - t0)
                except Exception:
                    pass
            time.sleep(2.0)
        return False

    def _peer_alive(self) -> bool:
        """/health が 2 秒で応えるか。ReadTimeout の後に「相手は生きているか」を見る。"""
        try:
            base = self.cfg.base_url.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            # 健全な llama-server の /health は 9ms（実測）。1 秒で十分・沈黙する相手への
            # 総待ち時間（read + probe + 一括の read）を「すぐ諦める」の枠に収める。
            r = httpx.get(base.rstrip("/") + "/health", timeout=1.0, trust_env=_trust_env())
            return r.status_code == 200
        except Exception:
            return False

    def _ensure_open(self) -> None:
        """Ω-O: if the client was closed by a cancel and we're back in a
        fresh chat() call (e.g. retry after fallback), reopen it. Detection
        is best-effort: httpx 0.27+ exposes is_closed.
        Skip when _hard_closed (caller explicitly cancelled)."""
        if self._hard_closed:
            return
        if getattr(self._client, "is_closed", False):
            self._client = self._new_client()

    def _sampling(self, *, escape: bool = False) -> dict[str, Any]:
        """この要求で送る生成の設定（sampling_params）。★繰り返しで切った直後の 1 回だけ presence_penalty を上げる。

        ★2026-10-15 オーナー「モデルが内部思考をしすぎて迷走していないか確かめて」→「進めてください」。
          14:48 の走行（Qwen3.6・18 分）は、思考が同じ文の繰り返しに入って切られた要求が 7 本・464 秒（約 4 割）で、
          切って促しても、やり直しの要求がまた同じ繰り返しに入った（3 本・4 本と続けて切られた）。10:50 の走行も最後に 4 本続いた。
          いまの設定は Qwen3.6 のカードの「精密なコーディング向け」（temperature 0.6・presence_penalty 0）。同じカードの
          「一般の思考向け」は presence_penalty 1.5 で、Qwen3 のカードは「0〜2 で終わらない繰り返しが減る・高すぎると言語が混ざる」。
          ⇒ 普段の手番は今のまま、切った直後の 1 回だけ 1.5 で送って、その場の繰り返しから抜けさせる（思考は止めない）。
          続けて切られたら、その次もまた 1 回だけ上げる。LAPS_LOOP_ESCAPE_PRESENCE（0 で切る）。
        ★旗は**次の手番の要求**（道具付き・要約でない）だけが使い、**成功してから**下ろす（2026-10-15・E6）。旧形はここで
          下ろしていたので、点検の要求（chat()）や、逐次が失敗して一括へ落ちたときの逐次の側に先に使われ、手番の本体に届かなかった。
          下ろすのは `_escape_done`。"""
        out = sampling_params()
        if escape and self._loop_escape_next and getattr(self, "compacting", None) is None:
            try:
                v = float(os.environ.get("LAPS_LOOP_ESCAPE_PRESENCE") or 1.5)
            except ValueError:
                v = 1.5
            if v > 0 and "presence_penalty" in out and float(out.get("presence_penalty") or 0) < v:
                out["presence_penalty"] = v
        return out

    def _escape_done(self, payload: dict[str, Any], *, cut_again: bool = False) -> None:
        """道具の要求（要約でない）が成功した後に呼ぶ。旗を下ろす（この要求でまた切ったなら残す）。"""
        if not self._loop_escape_next or cut_again or getattr(self, "compacting", None) is not None:
            return
        self._loop_escape_next = False
        if payload.get("presence_penalty") != sampling_params().get("presence_penalty"):
            self.usage["loop_escape"] = self.usage.get("loop_escape", 0) + 1

    def _reject_4xx(self, r: "httpx.Response") -> None:
        """429 以外の 4xx を、投げ直さない BackendRejected にする（chat() と道具の経路で同じ・2026-10-15・E5）。"""
        if 400 <= r.status_code < 500 and r.status_code != 429:
            _hint = {401: "鍵か認証の設定", 403: "権限", 404: "宛先の綴り（/v1 の有無など）",
                     405: "メソッド", 422: "送っている本文の形"}.get(r.status_code, "要求の内容")
            raise BackendRejected(
                f"推論機が HTTP {r.status_code} を返しました: {self.cfg.base_url}"
                f"（{_hint}を確かめてください）。投げ直しても同じ答えになるので待ちません。")

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        on_token: Callable[[str], None] | None = None,
        chat_template_kwargs: dict[str, Any] | None = None,
    ) -> str:
        """Single non-streaming call; if on_token is provided, use SSE streaming
        under the hood and accumulate, so callers see live progress without
        changing the return contract (full text on completion).
        chat_template_kwargs flows through to llama-server's --jinja template
        (e.g. {"enable_thinking": False} for Qwen3)."""
        self._ensure_open()
        if on_token is not None:
            # ★逐次の経路も一括と同じ扱いにする（2026-10-15・E2）。旧形は ReadTimeout しか見ておらず、
            #   400 の溢れは HTTPStatusError のまま上がって要約に入らず、500・接続の失敗は投げ直さずに走行ごと落ちていた。
            #   400 は _chat_stream が ContextOverflow / BackendRejected にする。5xx・429・接続の失敗は、**1 文字も流していなければ**
            #   下の一括の再試行へ落とし、受けた本文を on_token へ 1 度に渡す（流した後なら二重に出さないため、そのまま上げる）。
            _streamed = {"n": 0}

            def _tok(s: str) -> None:
                _streamed["n"] += len(s)
                on_token(s)
            try:
                return self._stream_once_retry(lambda: self._chat_stream(
                    messages, temperature=temperature, max_tokens=max_tokens,
                    stop=stop, on_token=_tok,
                    chat_template_kwargs=chat_template_kwargs,
                ), can_retry=lambda: _streamed["n"] == 0)
            except (httpx.HTTPStatusError, httpx.RequestError):
                if self._hard_closed or _streamed["n"]:
                    raise
                self.usage["stream_fallback_nonstream"] = self.usage.get("stream_fallback_nonstream", 0) + 1
            text = self.chat(messages, temperature=temperature, max_tokens=max_tokens, stop=stop,
                             chat_template_kwargs=chat_template_kwargs)
            if text:
                try:
                    on_token(text)
                except Exception:
                    pass
            return text

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temperature,
            **self._sampling(),
            "max_tokens": max_tokens,
            # ★思考の予算を本文と分ける（2026-09-21・上の reasoning_budget を参照）。
            #   知らないサーバは**このキーを黙って無視する**ので、付けても壊れない。
            **reasoning_fields(max_tokens),
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs

        last_exc: Exception | None = None
        _waited_back = False
        for attempt in range(self.cfg.max_retries + 1):
            try:
                self._ensure_open()
                self._begin_request()
                r = self._client.post("/chat/completions", json=payload)
                # 400 は決定的な拒否。投げ直しても同じ答えが返るだけなので、下の
                # except に捕まらない例外型で即座に抜ける（ContextOverflow /
                # BackendRejected は RuntimeError ではない）。
                if r.status_code == 400:
                    raise _reject_from_400(r)
                # Retry on 5xx / 429
                if r.status_code in (429,) or 500 <= r.status_code < 600:
                    raise httpx.HTTPStatusError(
                        transient_reason(r), request=r.request, response=r,
                    )
                # ★一時的でない 4xx は**投げ直さない**（2026-08-30・第三者レビュー D7）。
                #   `raise_for_status()` の `HTTPStatusError` を下の retry 節が拾うため、
                #   401（鍵違い）や 404（宛先違い）でも 1.5+3+6 秒の backoff で 3 回投げ直す。
                #   待っても答えは変わらず、**利用者の沈黙が 11 秒延びて理由が遅れて届く**だけ。
                #   接続できないときと同じ扱いにする（宛先と状態を文面に入れる）。
                self._reject_4xx(r)
                r.raise_for_status()
                data = r.json()
                self._record_usage(data)
                # ★思考は別枠(reasoning_content)で来る。捨てずに本文へ戻す。
                return inline_reasoning(data["choices"][0]["message"] or {})
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # ★走行の途中なら、推論機が戻るのを待って送り直す（1 回の呼び出しにつき 1 度だけ・F2-1）。
                if not _waited_back and self._await_peer_back():
                    _waited_back = True
                    last_exc = e
                    continue
                # ★接続が張れない＝相手が居ない/経路が無い。同じ理由で4回待たない
                #   （待っても結果は変わらず、利用者の沈黙が4倍になるだけ）。
                #   宛先を文面に入れる: 「どこへ繋ごうとして失敗したか」が
                #   分からないのが、この種の障害でいちばん時間を溶かす。
                raise RuntimeError(
                    f"推論機に接続できません: {self.cfg.base_url}"
                    f"（{type(e).__name__}）。設定の宛先と、その機械が動いているかを確かめてください。"
                ) from e
            except httpx.ReadTimeout as e:
                # ★沈黙している相手にリトライしない（2026-08-23）。一括経路は生存確認つき
                #   再試行の対象外（逐次入口 _stream_once_retry が担当・2026-08-24）。
                raise RuntimeError(
                    f"推論機が応答しません: {self.cfg.base_url}"
                    f"（{read_to_desc()} 待っても最初の応答が来ませんでした）。"
                    "宛先が正しいか、その推論機が動いているかを Connection で確かめてください。"
                ) from e
            except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
                # RuntimeError catches "Cannot send a request, as the client
                # has been closed" — reopen and retry.
                last_exc = e
                # ★利用者が止めた（close() 済み）なら再試行しない（2026-08-23 レビュー）:
                #   閉じた client に 1.5+3+6 秒の後退で 3 回投げ直し、中断が 10 秒遅れていた。
                if self._hard_closed or attempt == self.cfg.max_retries:
                    break
                sleep = self.cfg.retry_base * (2 ** attempt)
                time.sleep(sleep)
        raise last_exc if last_exc else RuntimeError("backend.chat failed without exception")

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        chat_template_kwargs: dict[str, Any] | None = None,
        tool_choice: str = "auto",
        on_token: Callable[[str], None] | None = None,
        first_byte_timeout: float | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Native OpenAI tool-calling: hand the server the tool schemas and let
        it return structured `tool_calls` instead of us regex-scraping them out
        of free text.

        llama-server does this under `--jinja` (enabled by default on this
        build): with `tools` in the request it constrains and then parses the
        model's output, so a syntactically malformed call cannot come back.
        Returns (content, calls) where each call is {"name", "input"} — the
        shape the agent's dispatcher already speaks.

        This does NOT make the model choose the *right* tool or the right
        arguments; it only removes the format failure mode. See DESIGN.md §2.
        """
        self._ensure_open()

        # ★2026-08-11: ここが非ストリーミングだったため、道具を使う会話では
        #   **生成中に1文字も流れず**、答えが最後に一気に出ていた。思考も見えず、
        #   画面の心電図も動かない（トークンで駆動しているため）。
        #   llama.cpp は tools 付きでも逐次配信でき、tool_calls は delta で届く。
        #   ★失敗したら黙って一括へ落ちる＝いまの正確さを壊さない。
        if on_token is not None:
            # ★2026-09-06 P0（実機ハング）: ここは以前 `_stream_once_retry` で
            #   ReadTimeout を **3 回まで再試行** していた。prefill が read timeout
            #   (90s) を超えると 90s×4 ＝ 最悪 360s 逐次で沈黙し、さらに下の一括で
            #   もう 90s ＝ 450s、利用者は「推論中」のまま待ち続けた（実測）。
            #   いまは①番犬（最初の 1 byte を別枠で縛る）＋②相手の生存を見てから
            #   **一括に1回だけ**落ちる、に置き換えた。再試行の乗算をやめ、
            #   「二重待ちしない」教訓（2026-08-23）は /health ゲートで保つ。
            try:
                return self._chat_with_tools_stream(
                    messages, tools,
                    temperature=temperature, max_tokens=max_tokens,
                    chat_template_kwargs=chat_template_kwargs,
                    tool_choice=tool_choice, on_token=on_token,
                    # ★呼ぶ側が長めに待てる（2026-10-05・oc の要約）。既定は `_first_byte_timeout()`。
                    first_byte_to=(_first_byte_timeout() if first_byte_timeout is None
                                   else first_byte_timeout),
                )
            except _FirstByteStall as e:
                # 最初の 1 byte が番犬の枠内に来なかった。中断なら透過。
                if self._hard_closed:
                    raise
                # 相手が生きていない（/health も無反応）なら二重待ちせず即あきらめる
                #   （2026-08-23 の 600 秒二重待ちの教訓）。
                if not self._peer_alive():
                    raise RuntimeError(
                        f"推論機が応答しません: {self.cfg.base_url}"
                        "（最初の応答が来ず、/health も無反応）。"
                        "宛先が正しいか、その推論機が動いているかを Connection で確かめてください。"
                    ) from e
                # 相手は生きている＝prefill が長い/詰まっている。番犬が stream を
                #   切って KV は温まっているので、**一括で1回だけ**取りに行く（下へ）。
                self.usage["stream_fallback_nonstream"] = (
                    self.usage.get("stream_fallback_nonstream", 0) + 1
                )
                self._client = self._new_client()   # 番犬が transport を閉じた＝張り直す
            except httpx.ReadTimeout as e:
                # 最初の 1 byte は来たが途中で無音になった（NIC drop 等）。相手が
                #   生きていれば一括で1回だけ取り直す。死んでいれば二重待ちしない。
                if self._hard_closed:
                    raise
                if not self._peer_alive():
                    raise RuntimeError(
                        f"推論機が応答しません: {self.cfg.base_url}"
                        f"（{read_to_desc()} 待っても続きが来ませんでした）。"
                        "宛先が正しいか、その推論機が動いているかを Connection で確かめてください。"
                    ) from e
                self.usage["stream_fallback_nonstream"] = (
                    self.usage.get("stream_fallback_nonstream", 0) + 1
                )
                self._client = self._new_client()
            except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError, ValueError):
                # ★中断で socket を落としたとき（_hard_closed）は**一括へ落ちない**
                #   （2026-09-03 実測）。逐次の道具経路は失敗すると非逐次へ落ちる設計だが、
                #   中断で切った stream の失敗までここへ来る。落ちると閉じた client へ
                #   もう一度 POST する——相手がまだ黙っていれば（P40 の slot 待ち）
                #   read timeout(90s) まで塞がる。実測: 中断そのものは close() が
                #   _hard_closed を立てるので一括ループが即 break していたが、それは
                #   偶然の保険で、**再送を試みること自体が誤り**（切った相手へ送り直す
                #   意味は無い）。中断なら掴んだ例外をそのまま上げる。
                if self._hard_closed:
                    raise

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temperature,
            **self._sampling(escape=True),
            "max_tokens": max_tokens,
            # ★思考の予算を本文と分ける（2026-09-21・上の reasoning_budget を参照）。
            #   知らないサーバは**このキーを黙って無視する**ので、付けても壊れない。
            **reasoning_fields(max_tokens),
            "stream": False,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs

        last_exc: Exception | None = None
        _waited_back = False
        for attempt in range(self.cfg.max_retries + 1):
            try:
                self._ensure_open()
                self._begin_request()
                r = self._client.post("/chat/completions", json=payload)
                # chat() と同じ理由で 400 は即座に抜ける（native 経路のほうが
                # tool schema ぶん固定費が重く、溢れるのは実際こちらが先）。
                if r.status_code == 400:
                    raise _reject_from_400(r)
                if r.status_code in (429,) or 500 <= r.status_code < 600:
                    raise httpx.HTTPStatusError(
                        transient_reason(r), request=r.request, response=r,
                    )
                # ★chat() と同じく、429 以外の 4xx は投げ直さない（2026-10-15・E5。ここだけ 4 回投げ直して 10 秒待っていた）。
                self._reject_4xx(r)
                r.raise_for_status()
                data = r.json()
                self._record_usage(data)
                self._escape_done(payload)
                msg = data["choices"][0]["message"]
                calls: list[dict[str, Any]] = []
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    raw = fn.get("arguments", "{}")
                    # ★逐次の経路と同じく、引数の崩れはこの手番のやり直しにする（loopcut.degenerate_args・2026-10-13）。
                    _unit = (_loopcut.degenerate_args(fn.get("name", ""), raw[-8000:])
                             if isinstance(raw, str) else None)
                    if _unit is not None:
                        self.usage["degenerate_cut"] = self.usage.get("degenerate_cut", 0) + 1
                        self.usage["degenerate_args_cut"] = self.usage.get("degenerate_args_cut", 0) + 1
                        raise DegenerateOutput(f"道具の引数が崩れました（{_unit[:24]!r} の繰り返し）")
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except json.JSONDecodeError as _e:
                        # Server-side constraint should prevent this; if it
                        # happens we want it visible, not silently patched.
                        # ★2026-09-22: 理由も運ぶ（上と同じ。片方だけ直さない）。
                        args = {"__unparsed_arguments__": raw,
                                "__parse_error__": f"{_e.msg} (pos {_e.pos} / 全長 {len(str(raw))})"}
                    calls.append({"name": fn.get("name", ""), "input": args})
                return (inline_reasoning(msg or {}), calls)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # ★走行の途中なら、推論機が戻るのを待って送り直す（1 回の呼び出しにつき 1 度だけ・F2-1）。
                if not _waited_back and self._await_peer_back():
                    _waited_back = True
                    last_exc = e
                    continue
                # ★接続が張れない＝相手が居ない/経路が無い。同じ理由で4回待たない
                #   （待っても結果は変わらず、利用者の沈黙が4倍になるだけ）。
                #   宛先を文面に入れる: 「どこへ繋ごうとして失敗したか」が
                #   分からないのが、この種の障害でいちばん時間を溶かす。
                raise RuntimeError(
                    f"推論機に接続できません: {self.cfg.base_url}"
                    f"（{type(e).__name__}）。設定の宛先と、その機械が動いているかを確かめてください。"
                ) from e
            except httpx.ReadTimeout as e:
                # ★沈黙している相手にリトライしない（一括経路も同じ扱い・2026-08-23）。
                raise RuntimeError(
                    f"推論機が応答しません: {self.cfg.base_url}"
                    f"（{read_to_desc()} 待っても最初の応答が来ませんでした）。"
                    "宛先が正しいか、その推論機が動いているかを Connection で確かめてください。"
                ) from e
            except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
                last_exc = e
                if self._hard_closed or attempt == self.cfg.max_retries:   # 中断なら再試行しない
                    break
                time.sleep(self.cfg.retry_base * (2 ** attempt))
        raise last_exc if last_exc else RuntimeError("backend.chat_with_tools failed")

    def _chat_with_tools_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        chat_template_kwargs: dict[str, Any] | None,
        tool_choice: str,
        on_token: Callable[[str], None],
        first_byte_to: float = 0.0,
    ) -> tuple[str, list[dict[str, Any]]]:
        """道具付きの逐次配信。本文は届いた順に `on_token` へ、
        `tool_calls` は delta を index ごとに繋いで組み立てる。

        ★引数の JSON は**断片で届く**（`{"pa` → `th": "a` → `.py"}`）。
        index で束ねてから一度に解釈する。途中で解釈しようとしてはいけない。
        リトライはしない（呼び出し側が一括へ落ちる）。

        `first_byte_to` > 0 なら「最初の 1 byte」だけを別枠の番犬で縛る
        （2026-09-06 P0）。prefill が長引いて何も返ってこないまま超えたら、
        別スレッドから `abort_inflight()` で stream を切り、`_FirstByteStall`
        を上げる（呼び出し側が相手の生存を見て一括へ落とす）。最初の 1 byte が
        届いた瞬間に番犬は解除され、以後は通常の read timeout が生成を守る
        （＝長い生成そのものは切らない）。
        """
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temperature,
            **self._sampling(escape=True),
            "max_tokens": max_tokens,
            # ★思考の予算を本文と分ける（2026-09-21・上の reasoning_budget を参照）。
            #   知らないサーバは**このキーを黙って無視する**ので、付けても壊れない。
            **reasoning_fields(max_tokens),
            "stream": True,
            **progress_fields(),
            "stream_options": {"include_usage": True},
            "tools": tools,
            "tool_choice": tool_choice,
        }
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs

        full: list[str] = []
        _inliner = ReasoningInliner(leak_lang=_LEAK_LANG.get())
        self.last_leaked_text = ""
        self.last_loop_cut = False
        self.last_loop_cut_in_think = False
        self.last_loop_cut_kind = ""
        # index -> {"name": str, "args": [断片...]}
        # 併合の状態。規則は `merge_tool_call_deltas` が持つ（試験もそれを直接呼ぶ）。
        acc: dict[int, dict[str, Any]] = {}
        done: list[dict[str, Any]] = []
        self.usage["calls"] += 1
        self._begin_request()
        _finish: str | None = None

        # ★最初の 1 byte だけの番犬（2026-09-06 P0）。first_byte_to<=0 なら無効。
        first_byte = threading.Event()
        tripped = {"v": False}
        watchdog: threading.Timer | None = None
        if first_byte_to and first_byte_to > 0:
            def _bite() -> None:
                # 既に 1 byte 来ていれば何もしない／中断中も触らない。
                if first_byte.is_set() or self._hard_closed:
                    return
                tripped["v"] = True
                self.usage["stream_first_byte_stall"] = (
                    self.usage.get("stream_first_byte_stall", 0) + 1
                )
                try:
                    self.abort_inflight()
                except Exception:
                    pass
            watchdog = threading.Timer(first_byte_to, _bite)
            watchdog.daemon = True
            watchdog.start()

        _cut_busy: int | None = None
        try:
            with self._client.stream("POST", "/chat/completions", json=payload) as r:
                self._track(r)
                if r.status_code == 400:
                    r.read()
                    raise _reject_from_400(r)
                r.raise_for_status()
                _seen_chars = 0
                _tail = ""
                _args_seen = 0
                for line in r.iter_lines():
                    # ★最初の 1 byte（＝最初に届いた行）で番犬を解除する。ヘッダは
                    #   prefill 前に返ることがある（実測）ので「stream() が返った」では
                    #   早すぎる——本文が届いて初めて相手が生成を始めたと分かる。
                    if not first_byte.is_set():
                        first_byte.set()
                        if watchdog is not None:
                            watchdog.cancel()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    if isinstance(obj.get("prompt_progress"), dict):
                        self._note_progress(obj["prompt_progress"])
                    if isinstance(obj.get("usage"), dict):
                        self._record_usage(obj, count_call=False)      # ★E3: calls は頭で数えた
                    try:
                        _ch0 = obj["choices"][0]
                        delta = _ch0.get("delta") or {}
                    except Exception:
                        continue
                    if isinstance(_ch0.get("finish_reason"), str) and _ch0["finish_reason"]:
                        _finish = _ch0["finish_reason"]               # ★E3: usage の塊（choices が空）で消さない

                    # ★思考(reasoning_content)も本文として流す。<think> で包むので
                    #   受け手（serve.ThinkSplitter）が思考と本文に割れる。
                    chunk = _inliner.feed(delta)
                    if chunk:
                        full.append(chunk)
                        try:
                            on_token(chunk)
                        except Exception:
                            pass
                        # ★同じ塊を繰り返し始めたら、その場で切る（2026-09-21）。
                        #   `repeat_penalty` の窓は 64 トークンで段落には届かず、
                        #   道具の反復検知は**引数**を見るのでここは通り抜ける。
                        #   費用を抑えるため、貯まった字が一定量増えたときだけ見る。
                        # ★`<br>` だけの行が続いたら、待たずにすぐ切る（2026-10-10・Gemma が道具を呼ぶ代わりに挟む癖）。
                        _tail = (_tail + chunk)[-160:]
                        if "<br" in chunk and _loopcut.br_run(_tail):
                            self.usage["degenerate_cut"] = self.usage.get("degenerate_cut", 0) + 1
                            try:
                                on_token(DEGENERATE_NOTE)
                            except Exception:
                                pass
                            _cut_busy = self._slots_busy()
                            raise DegenerateOutput("出力が崩れました（'<br>' の繰り返し）")
                        _seen_chars += len(chunk)
                        if _seen_chars >= _LOOPCUT_EVERY:
                            _seen_chars = 0
                            _txt = "".join(full)
                            # ★短い周期の崩れ（2026-10-10・loopcut.degenerate_tail）。切って呼び出し側にやり直させる。
                            _unit = _loopcut.degenerate_tail(_txt)
                            if _unit is not None:
                                self.usage["degenerate_cut"] = self.usage.get("degenerate_cut", 0) + 1
                                try:
                                    on_token(DEGENERATE_NOTE)
                                except Exception:
                                    pass
                                _cut_busy = self._slots_busy()
                                raise DegenerateOutput(f"出力が崩れました（{_unit[:24]!r}）")
                            _cut, _note = _loopcut.cut_loop(_txt)
                            if not _note and _inliner._open:
                                # ★思考の中の堂々巡り（loopcut.think_circle・2026-10-11）。思考はそのまま残し、ここで切る。
                                _th = _txt[_txt.rfind(THINK_OPEN) + len(THINK_OPEN):] if THINK_OPEN in _txt else _txt
                                _line = _loopcut.think_circle(_th)
                                self.last_loop_cut_kind = "circle"
                                # ★同じ思考の 2 回目・前の手番と同じ考え（loopcut.think_dup / same_as_before・2026-10-12）を先に見る。
                                _dup = _loopcut.think_dup(_th)
                                if _dup is not None:
                                    self.usage["think_dup_cut"] = self.usage.get("think_dup_cut", 0) + 1
                                    self.last_loop_cut_kind = "dup"
                                    _cut, _note = _txt, ("\n\n★直前とまったく同じ考えが続いたので、ここで切りました"
                                                         f"（{_dup[:60]!r}）。\n")
                                    _line = None
                                elif _loopcut.same_as_before(_th, getattr(self, "prev_think", "") or ""):
                                    self.usage["think_prev_cut"] = self.usage.get("think_prev_cut", 0) + 1
                                    self.last_loop_cut_kind = "prev"
                                    _cut, _note = _txt, "\n\n★前の手番の思考をそのまま繰り返したので、ここで切りました。\n"
                                    _line = None
                                # ★思考の長さの天井（2026-10-13 オーナー「任せます」・長い作業向け）。Gemma と GLM には推論機の思考の予算が
                                #   効かないので、暴走した長い尾だけをここで止める。普段の思考には触らない（661 手番: 中央値 742 字・
                                #   上位 1% 10,312 字・1.2 万字超は 4 手番＝0.6%・最長 34,982 字）。LAPS_THINK_CEILING_CHARS（0 で切る）。
                                if not _note and _line is None and thinking_unbounded():
                                    _ceil = _think_ceiling_chars()
                                    if _ceil and len(_th) >= _ceil:
                                        self.usage["think_long_cut"] = self.usage.get("think_long_cut", 0) + 1
                                        self.last_loop_cut_kind = "long"
                                        _cut, _note = _txt, f"\n\n★思考が長くなりすぎたので（{len(_th):,} 字）、ここで区切りました。\n"
                                if _note:
                                    pass
                                elif _line is not None:
                                    self.usage["think_circle_cut"] = self.usage.get("think_circle_cut", 0) + 1
                                    _cut, _note = _txt, ("\n\n★考えが同じ所を回り始めたので、ここで切りました"
                                                         f"（同じ行が {_loopcut.THINK_TIMES} 回: {_line[:60]!r}）。\n")
                            if _note:
                                self.usage["loop_cut"] = self.usage.get("loop_cut", 0) + 1
                                self.last_loop_cut = True
                                if self.last_loop_cut_kind != "long":
                                    self._loop_escape_next = True      # 次の要求だけ presence_penalty を上げる（_sampling）
                                self.last_loop_cut_in_think = bool(_inliner._open)    # 思考の中で繰り返した
                                if _inliner._lang:
                                    # ★Gemma（leak_guard の中）: 切った一文は思考の側に流す（答えの欄に出さない・2026-10-10
                                    #   オーナー「こうなってしまうのも防いでほしい」）。続きは oc が「日本語で答えて」と差し戻す。
                                    _inliner.discard_held()
                                    if not _inliner._open:
                                        _note = THINK_OPEN + _note + THINK_CLOSE
                                full[:] = [_cut, _note]
                                try:
                                    on_token(_note)
                                except Exception:
                                    pass
                                _cut_busy = self._slots_busy()
                                break

                    _tcd = delta.get("tool_calls") or []
                    merge_tool_call_deltas(_tcd, acc, done)
                    # ★道具の引数の崩れ（loopcut.degenerate_args・2026-10-13）。引数は上の本文の検査を通らないので、ここで見る。
                    #   崩れた呼び出しを最後まで受けると、引数が JSON として閉じず、履歴に残ったそれを推論機が次の要求から
                    #   毎回読めずに 500 を返した（オーナーの走行・4 万字）。
                    if _tcd:
                        _args_seen += sum(len((t.get("function") or {}).get("arguments") or "") for t in _tcd)
                        if _args_seen >= _LOOPCUT_EVERY:
                            _args_seen = 0
                            _unit = _degenerate_call(acc)
                            if _unit is not None:
                                self.usage["degenerate_cut"] = self.usage.get("degenerate_cut", 0) + 1
                                self.usage["degenerate_args_cut"] = self.usage.get("degenerate_args_cut", 0) + 1
                                try:
                                    on_token(DEGENERATE_NOTE)
                                except Exception:
                                    pass
                                _cut_busy = self._slots_busy()
                                raise DegenerateOutput(f"道具の引数が崩れました（{_unit[:24]!r} の繰り返し）")
        except Exception as e:
            # ★思考の途中で切った（崩れ・時間切れ・中断）なら、閉じタグを流してから上げる（2026-10-15・E1）。流さないと
            #   画面の分割器（serve.ThinkSplitter）は思考が開いたままだと見なし、次の手番の本文を思考の欄に出していた。
            if _inliner._open:
                _inliner.discard_held()
                try:
                    on_token(THINK_CLOSE)
                except Exception:
                    pass
            # 番犬が切ったのなら、read timeout ではなく「最初の 1 byte が来なかった」
            # として上げる（呼び出し側が相手の生存を見て一括へ落とす）。中断
            # （_hard_closed）は番犬ではないのでそのまま透過させる。
            if tripped["v"] and not self._hard_closed:
                raise _FirstByteStall(
                    f"最初の応答が {first_byte_to:.0f} 秒以内に来ませんでした"
                ) from e
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
            # ★途中で切った（崩れ・`<br>`・繰り返し・利用者の割り込み）なら、推論機がその枠を手放すまで少し待つ（2026-10-10）。
            #   待たずに次を送ると、推論機はまだ前の生成を処理中と見て空いた別の枠に回し、読み込みの控えを使えずに頭から読み直す
            #   （オーナーの走行: 繰り返しを切った直後の答え直しで 6.8 万語を毎秒 150 語で読み直し、7 分待てずに止められた）。
            _abort_busy = getattr(self, "_busy_at_abort", None)
            self._busy_at_abort = None
            if _cut_busy is not None or _abort_busy is not None:
                self._await_release(_cut_busy if _cut_busy is not None else _abort_busy)

        # ★思考の途中で生成が切れても閉じる。閉じ忘れると ThinkSplitter が
        #   以降すべてを思考と見なし、本文が一文字も出ない。
        # ★閉じタグは**逐次配信にも流す**（2026-10-05）。思考だけ返して本文なしで道具を呼ぶ応答では
        #   戻り値にしか閉じタグが入らず、画面の分割器は思考が開いたままだと見なして
        #   次の手番の `<think>` を文字としてそのまま出していた（test2 の実走で確認）。
        _tail = _inliner.close()
        if _inliner.reclassified:
            self.usage["think_leak_reclassified"] = self.usage.get("think_leak_reclassified", 0) + _inliner.reclassified
            self.last_leaked_text = _inliner.leaked
        full.append(_tail)
        if _tail:
            try:
                on_token(_tail)
            except Exception:
                pass
        if _finish:
            self.last_finish_reason = _finish
        self._escape_done(payload, cut_again=self.last_loop_cut and self.last_loop_cut_kind != "long")
        return ("".join(full), finish_tool_calls(acc, done))

    def _chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        stop: list[str] | None,
        on_token: Callable[[str], None],
        chat_template_kwargs: dict[str, Any] | None = None,
    ) -> str:
        """SSE stream from llama-server; aggregates tokens and invokes on_token
        for each delta. Returns full text on completion. No retries — the upper
        layer will fall back to non-streaming chat() on error."""
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # ★思考の予算を本文と分ける（2026-09-21・上の reasoning_budget を参照）。
            #   知らないサーバは**このキーを黙って無視する**ので、付けても壊れない。
            **reasoning_fields(max_tokens),
            "stream": True,
            **progress_fields(),
            # ★sampling_params を載せる（2026-09-09）。ここだけ抜けていた。
            #   出荷経路は LAPS_NATIVE_TOOLS=1（gateway.py:301・serve.py:3087・
            #   term/client.py:142）なので**この関数は本番では到達しない**——
            #   直しても利用者の生成は1文字も変わらない。
            #   直す理由は**測定の汚染**のほう:
            #     ・tests/test_pushback_text_reaches_model.py:77 が対照腕を
            #       NATIVE_TOOLS=0 で切っているので、その腕だけ top_p/top_k が
            #       サーバ既定(0.95/40)になり、**腕の差にサンプリング差が交絡していた**。
            #     ・同じ非ネイティブ走行の中でも、逐次(ここ)と例外時の一括
            #       フォールバック(chat())でサンプリングが違い、
            #       **1走行の途中で腕が入れ替わっていた**。
            #   ＝incidents/2026-08-09-切り替えたはずの部品が配線されていなかった.md と同じ族。
            **self._sampling(),
        }
        if stop:
            payload["stop"] = stop
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs

        full: list[str] = []
        _inliner2 = ReasoningInliner()
        # ストリームでは usage が来ない（stream_options を付けていないため）。
        # 回数だけ数えておく＝「本物の数字が無い」ことが後から分かる。
        self.usage["calls"] += 1
        self._begin_request()
        try:
            with self._client.stream("POST", "/chat/completions", json=payload) as r:
                self._track(r)
                # ★400 は一括の経路と同じく ContextOverflow / BackendRejected にする（2026-10-15・E2）。旧形は raise_for_status の
                #   HTTPStatusError のまま上がり、溢れても要約に入らなかった。
                if r.status_code == 400:
                    r.read()
                    raise _reject_from_400(r)
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    if isinstance(obj.get("prompt_progress"), dict):
                        self._note_progress(obj["prompt_progress"])
                    try:
                        delta = obj["choices"][0].get("delta") or {}
                        # ★思考も流す（<think> で包む）。捨てると「推論しているのか
                        #   どうか分からない」画面になる。
                        chunk = _inliner2.feed(delta)
                    except Exception:
                        chunk = ""
                    if not chunk:
                        continue
                    full.append(chunk)
                    try:
                        on_token(chunk)
                    except Exception:
                        pass
        except Exception:
            if _inliner2._open:                      # ★E1: 思考の途中で落ちたら閉じタグを流してから上げる
                try:
                    on_token(THINK_CLOSE)
                except Exception:
                    pass
            raise
        _tail2 = _inliner2.close()
        full.append(_tail2)
        if _tail2:
            try:
                on_token(_tail2)
            except Exception:
                pass
        return "".join(full)

    # ★飛行中の Response（2026-08-23 レビュー high）。`httpx.Client.close()` は**別スレッドで
    #   読み途中の stream を中断しない**ので、Ctrl-C しても最初の生成が終わるまで（実測 15〜100 秒）
    #   done が来なかった。Response を控えておき、close() でそれ自身を閉じる＝iter_lines が
    #   その場で例外になり、走行が即座に cancelled へ落ちる。
    def _track(self, r: Any) -> None:
        try:
            self._inflight.add(r)
        except Exception:
            pass

    def _slots_busy(self) -> int | None:
        """推論機で処理中の枠の数（llama-server の /slots・取れなければ None）。"""
        try:
            base = self.cfg.base_url.rstrip("/")
            root = base[:-3] if base.endswith("/v1") else base
            r = httpx.get(root + "/slots", timeout=1.5, trust_env=_trust_env())     # ★E4: /props と同じく proxy の設定に従う
            if r.status_code != 200:
                return None
            return sum(1 for s in (r.json() or []) if isinstance(s, dict) and s.get("is_processing"))
        except Exception:
            return None

    def _await_release(self, busy_before: int | None, limit: float = 3.0) -> None:
        """生成を途中で切った後、処理中の枠の数が切る前より減るまで待つ（最大 limit 秒・取れなければ 0.3 秒だけ）。"""
        if busy_before is None:
            time.sleep(0.3)
            return
        t_end = time.monotonic() + limit
        while time.monotonic() < t_end:
            n = self._slots_busy()
            if n is None or n < busy_before or n == 0:
                return
            time.sleep(0.1)
        self.usage["slot_release_wait_timeout"] = self.usage.get("slot_release_wait_timeout", 0) + 1

    def abort_inflight(self) -> int:
        """飛行中の要求を**今**切る。戻り値は切った数。

        ★close() だけでは足りない（反証役の実測 2026-08-23）: 相手が**黙っている**（P40 の slot 待ち）
          あいだは、別スレッドの socket を close しても blocked な recv は起きない＝相手が何か
          送ってくるか read timeout（90 秒）まで待つ。`shutdown(SHUT_RDWR)` は recv を EOF で
          起こす。httpcore の私有属性を辿るので、取れなければ close() に落ちる（害は無い）。
        """
        import socket as _socket
        _t0 = time.monotonic()
        try:
            self._busy_at_abort = self._slots_busy() if getattr(self, "_inflight", None) else None
        except Exception:
            self._busy_at_abort = None
        n = 0
        conns_seen = 0
        sock_reachable = 0
        inflight_before = len(getattr(self, "_inflight", ()) or ())
        try:
            pool = self._client._transport._pool          # httpcore.ConnectionPool
            for conn in list(getattr(pool, "connections", []) or []):
                conns_seen += 1
                inner = getattr(conn, "_connection", None)     # HTTP11Connection
                stream = getattr(inner, "_network_stream", None)
                sock = getattr(stream, "_sock", None)
                if sock is not None:
                    sock_reachable += 1
                    try:
                        sock.shutdown(_socket.SHUT_RDWR)
                        n += 1
                    except OSError:
                        pass
        except Exception:
            pass
        for r in list(getattr(self, "_inflight", ()) or ()):
            try:
                r.close()
            except Exception:
                pass
        try:
            self._inflight.clear()
        except Exception:
            pass
        # 念のため transport 全体も閉じる（pool に載っていない接続の保険）。
        try:
            self._client._transport.close()
        except Exception:
            pass
        # ★何を切ったかを残す（2026-09-03）。sockets=0 かつ inflight=0 なら、
        #   abort は**何も掴めていない**（要求送信中で pool を辿れなかった疑い）＝
        #   相手が黙っていれば読みは read timeout まで起きない。この 3 数字が
        #   「止めたのに止まらない」の原因を分ける。
        self.last_abort = {
            "sockets": n,
            "inflight": inflight_before,
            "connections": conns_seen,
            "sock_reachable": sock_reachable,
            "elapsed_ms": round((time.monotonic() - _t0) * 1000, 1),
        }
        return n

    def close(self) -> None:
        self._hard_closed = True
        self.abort_inflight()
        self._client.close()

    def _note_progress(self, pp: dict) -> None:
        """推論機の読み込みの進み具合（llama.cpp の prompt_progress）を控える。serve の心拍が読む。"""
        try:
            self.progress = {"total": int(pp.get("total") or 0), "cache": int(pp.get("cache") or 0),
                             "processed": int(pp.get("processed") or 0), "time_ms": int(pp.get("time_ms") or 0),
                             "at": time.monotonic()}
        except Exception:
            pass

    def compact_text(self) -> str:
        """会話の要約（oc._compact）の進み具合。読み込み中ならその割合、書いている間は字数。要約中でなければ ""。
        ★2026-10-10 オーナー「会話圧縮中の進行度も見れるようにしたいですね」。"""
        c = getattr(self, "compacting", None)
        if not c:
            return ""
        reading = self.progress_text()
        if reading:
            return reading
        n = int(c.get("chars") or 0)
        secs = int(time.time() - float(c.get("t0") or time.time()))
        return f"Writing summary ({n:,} chars, {secs}s)" if n else f"Preparing summary ({secs}s)"

    def progress_text(self, fresh_secs: float = 30.0) -> str:
        """読み込みの途中なら「Reading 34% (9,210 / 27,310 tokens, ~2 min left)」。終わっている・古いなら ""。
        ★2026-10-13 オーナー「文言全部長すぎるので英語にして短く」で英語に（アプリ・ウェブ版・Android は新旧どちらも読む）。"""
        p = getattr(self, "progress", None)
        if not p or time.monotonic() - p.get("at", 0) > fresh_secs:
            return ""
        total, cache, done, ms = p["total"], p["cache"], p["processed"], p["time_ms"]
        if total <= 0 or done >= total:
            return ""
        pct = int(100 * done / total)
        eta = ""
        work = done - cache
        if work > 0 and ms > 0:
            left = (total - done) / (work / (ms / 1000.0))
            eta = f", ~{int(left)}s left" if left < 90 else f", ~{int(left / 60 + 0.5)} min left"
        return f"Reading {pct}% ({done:,} / {total:,} tokens{eta})"

    def soft_reset(self) -> None:
        """Ω-O: clear hard_closed and ensure a fresh client. Used at the
        start of a new run() to recover from a prior cancel."""
        self._hard_closed = False
        self._ensure_open()
