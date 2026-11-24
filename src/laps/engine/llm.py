"""推論機との線。OpenAI 互換のストリームと、llama.cpp の拡張（読み込みの進み具合・思考の上限）。

手本: opencode の session/llm.ts と session/retry.ts（文脈あふれと一時的な失敗を分けて扱う）。"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .repeat import RepeatWatch

#: 思考の上限（トークン）。North は上限なしだと短い課題で 6,000 トークン考えることがある（2026-10-30 実測）。
#: 暴走は repeat.py が先に止めるので、ここは普段の考えを切らない大きさにする。
THINK_BUDGET = 8192
THINK_BUDGET_DEEP = 16384
#: Think Low（画面の既定）。Claude の effort と同じく深さを選ばせる（2026-10-31・旧: low と無指定が同じ 8192）。
#: 手元の思考は中央値 428 字・90% 点 2,694 字＝普段の考えはほとんど切らない。
THINK_BUDGET_LOW = 2048
THINK_BUDGET_OFF = 16            # Think が Off の依頼の思考の上限（Strata の会話・書式を変えずに切る）
#: 迷いの書き出しがこの数に達したら思考を切る（repeat.py）。記録 81 手番で 11 手番に掛かり、見た限りすべて堂々巡り（思考の 35% が減る）。
THINK_HEDGE_HEADS = 15
#: 上限に達したときに思考の末尾へ入る一言。「または答えを書け」とは言わない
#: （旧い文言の「give the answer」で、手順を書いただけの答えが完了扱いになっていた・2026-10-30）。
THINK_LIMIT_NOTE = "\n\n(Thinking limit reached. Stop deliberating and act now: make the next tool call.)\n"
MAX_OUTPUT = 32000
RETRY_DELAYS = (2, 4, 8, 16, 30)
#: サンプリングの値を環境変数で上書きする（手元で試すとき）。
_SAMPLING_ENV = {"LAPS_TEMPERATURE": ("temperature", float), "LAPS_TOP_P": ("top_p", float), "LAPS_TOP_K": ("top_k", int),
                 "LAPS_MIN_P": ("min_p", float), "LAPS_PRESENCE_PENALTY": ("presence_penalty", float),
                 "LAPS_REPEAT_PENALTY": ("repeat_penalty", float), "LAPS_SEED": ("seed", int)}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _trust_env() -> bool:
    """OS のプロキシ設定を読むか。既定は読む（オーナー機はプロキシ経由でしか推論機に届かない・2026-09-06 に切って繋がらなくなった）。"""
    return (os.environ.get("LAPS_HTTP_TRUST_ENV") or "").strip().lower() not in ("0", "false", "no", "off")


class ContextOverflow(Exception):
    """推論機が「窓に収まらない」と答えた。"""


class BackendError(Exception):
    """再試行しても推論機に届かない・答えが返らない。"""


class Cancelled(Exception):
    pass


@dataclass
class Info:
    """推論機の素性（llama-server の /props）。取れなければ model_path は空＝ふつうの OpenAI 互換。"""
    model_path: str = ""
    template: str = ""
    n_ctx: int = 0
    vision: bool = False
    llama: bool = False
    strata: bool = False         # Strata（/props の口は llama.cpp と同じ・build_info で見分ける）
    build: str = ""              # build_info（席の控えの素性に使う・2026-11-20）


@dataclass
class Turn:
    text: str = ""
    thinking: str = ""
    calls: list[dict] = field(default_factory=list)
    finish: str = ""
    usage: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    cut: str = ""                # "repeat"＝繰り返しで切った
    cut_unit: str = ""
    preempted: bool = False
    degenerate: bool = False     # 崩れた（短い単位の繰り返し）。控えを使わずにやり直す


def family(model: str, info: Info) -> str:
    forced = (os.environ.get("LAPS_MODEL_FAMILY") or "").strip().lower()
    if forced:
        return forced
    name = (info.model_path or model or "").lower()
    tmpl = info.template or ""
    if "north" in name or "cohere" in name or "command-" in name or "<|START_TEXT|>" in tmpl:
        return "cohere"
    if "gemma" in name:
        return "gemma"
    if "glm" in name:
        return "glm"
    if "qwen" in name or "kat-coder" in name:
        return "qwen"
    if "claude" in name:
        return "claude"
    return ""


def sampling(fam: str, model: str, info: Info, mode: str = "general") -> dict:
    """作者の推奨（モデルカード）。llama.cpp でないとき（Claude の中継など）は何も送らない。"""
    if not info.llama:
        return {}
    name = (info.model_path or model or "").lower()
    if fam == "cohere":
        return {"temperature": 1.0, "top_p": 0.95, "top_k": 0, "min_p": 0.0}
    if fam == "gemma":
        return {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "min_p": 0.05}
    if fam == "glm":
        return {"temperature": 0.7, "top_p": 1.0, "top_k": 0, "min_p": 0.01}
    if fam == "qwen":
        if "qwen3.6" in name or "kat-coder" in name:
            if mode == "development":
                return {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
            return {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}
        return {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
    return {"temperature": 0.7, "top_p": 0.95}


def estimate_tokens(text: str) -> int:
    """おおよそのトークン数（opencode の Token.estimate と同じ考え方に、かな漢字の分を足した）。"""
    if not text:
        return 0
    wide = sum(1 for c in text if ord(c) > 0x2E7F)
    return (len(text) - wide) // 4 + wide


_CALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_CALL_FUNC = re.compile(r"<function=([\w.-]+)>(.*?)</function>", re.S)
_CALL_PARAM = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.S)


def salvage_calls(thinking: str, known: set[str]) -> list[dict]:
    """思考の中に書かれてしまった道具の呼び出しを拾う（Qwen 系で起きる・llama.cpp discussion 23351）。"""
    out: list[dict] = []
    for m in _CALL_TAG.finditer(thinking or ""):
        try:
            o = json.loads(m.group(1))
        except ValueError:
            continue
        if o.get("name") in known:
            args = o.get("arguments")
            out.append({"name": o["name"], "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)})
    for m in _CALL_FUNC.finditer(thinking or ""):
        if m.group(1) in known:
            out.append({"name": m.group(1), "arguments": json.dumps(dict(_CALL_PARAM.findall(m.group(2))), ensure_ascii=False)})
    return out


class LLM:
    def __init__(self, base_url: str, model: str = "", api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or "default"
        self.api_key = api_key or os.environ.get("LAPS_API_KEY") or "sk-no-key-required"
        self._client = httpx.Client(trust_env=_trust_env(), timeout=httpx.Timeout(
            connect=15, read=_env_float("LAPS_READ_TIMEOUT", 600.0), write=60, pool=15))
        self._info: Info | None = None
        self._info_at = 0.0
        self._lock = threading.Lock()
        self._resp: Any = None
        self._aborted = False
        self.progress = ""
        # Strata へ渡す思考の強さ（reasoning_effort）を会話の間固定する値。None は固定しない（子・単発の呼び出し）。
        # ★2026-11-14 実測: Qwen3.8 の書式は reasoning_effort を system に描く。会話の途中で low に変えると先頭から変わり、
        #   推論機が会話を頭から読み直す（見直しの手番で 33,358 トークン・控え 0・92 秒）。浅くするのは思考の上限だけにする。
        self.effort_pin: str | None = None
        self.totals = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "gen_ms": 0.0, "read_ms": 0.0,
                       "read_tokens": 0, "cached_tokens": 0, "gen_tokens": 0}

    # ── 素性 ──────────────────────────────
    def info(self, *, fresh: bool = False) -> Info:
        now = time.monotonic()
        if self._info is not None and not fresh and now - self._info_at < 60:
            return self._info
        root = re.sub(r"/v1/?$", "", self.base_url)
        got: Info | None = None
        try:
            r = self._client.get(root + "/props", timeout=5, headers=self._headers())
            if r.status_code == 200:
                p = r.json()
                if isinstance(p, dict) and ("default_generation_settings" in p or "model_path" in p):
                    gen = p.get("default_generation_settings") or {}
                    got = Info(model_path=str(p.get("model_path") or ""), template=str(p.get("chat_template") or ""),
                               n_ctx=int(gen.get("n_ctx") or p.get("n_ctx") or 0),
                               vision=bool((p.get("modalities") or {}).get("vision")), llama=True,
                               strata=str(p.get("build_info") or "").startswith("Strata"),
                               build=str(p.get("build_info") or ""))
                else:
                    got = Info()
            elif r.status_code == 404:     # 口が無い＝llama.cpp ではない
                got = Info()
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        if got is None:                    # 一時的に読めないだけ。前の値を空に置き換えない（サンプリングや思考の上限が 60 秒外れる）
            got = self._info or Info()
        forced = int(_env_float("LAPS_N_CTX", 0))
        if forced > 0:
            got.n_ctx = forced
        self._info, self._info_at = got, now
        return got

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    # ── 席の控え（engine/slots.py）────────────
    def slot_tokens(self) -> int | None:
        """推論機の席 0 に入っている文脈の長さ（読めなければ None）。"""
        root = re.sub(r"/v1/?$", "", self.base_url)
        try:
            r = self._client.get(root + "/slots", timeout=5, headers=self._headers())
            if r.status_code != 200:
                return None
            d = r.json()
            s = d[0] if isinstance(d, list) and d else d
            if not isinstance(s, dict) or s.get("is_processing"):
                return None
            return int(s.get("n_prompt_tokens") or 0)
        except Exception:  # noqa: BLE001
            return None

    def slot_file(self, action: str, filename: str, timeout: float = 180.0) -> tuple[bool, str]:
        """席 0 を推論機の置き場のファイルへ保存（save）・ファイルから戻す（restore）。(できたか, 推論機の答えか理由)。"""
        root = re.sub(r"/v1/?$", "", self.base_url)
        try:
            r = self._client.post(f"{root}/slots/0?action={action}", json={"filename": filename}, timeout=timeout,
                                  headers=self._headers())
            body = r.text[:400]
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {body}"
            d = r.json()
            ok = bool(d.get("n_saved") if action == "save" else d.get("n_restored"))
            return ok, body
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {str(e)[:200]}"

    # ── 生成の中断 ─────────────────────────
    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def close(self) -> None:
        self.abort()
        try:
            self._client.close()
        except Exception:
            pass

    # ── 要求 ──────────────────────────────
    def payload(self, messages: list[dict], tools: list[dict] | None, *, mode: str = "general", think: bool = True,
                effort: str = "", tool_choice: str = "", max_tokens: int = 0, budget: int | None = None,
                presence: float | None = None) -> dict:
        info = self.info()
        fam = family(self.model, info)
        limit = max_tokens or int(_env_float("LAPS_MAX_OUTPUT_TOKENS", MAX_OUTPUT))
        if os.environ.get("LAPS_THINK") == "0":
            think = False
        if info.n_ctx:
            limit = min(limit, max(2048, info.n_ctx // 4))
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True, "max_tokens": limit,
                                "stream_options": {"include_usage": True}, **sampling(fam, self.model, info, mode)}
        for env, (key, conv) in _SAMPLING_ENV.items():
            raw = os.environ.get(env)
            if raw:
                try:
                    body[key] = conv(raw)
                except ValueError:
                    pass
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
            body["parallel_tool_calls"] = True
        if effort and not info.llama and str(self.model).startswith("claude-"):
            body["reasoning_effort"] = effort    # Claude の中継が output_config.effort に写す
        if info.llama:
            body["cache_prompt"] = True
            body["return_progress"] = True
            if presence is not None:
                body["presence_penalty"] = presence
            if not think and budget != 0 and info.strata and self.effort_pin is not None:
                # ★会話の途中で思考を切らない（2026-11-14）: Qwen3.8 の書式は enable_thinking と reasoning_effort を先頭（system）に描く。
                #   Think を Off にすると先頭から変わり、会話を頭から読み直す。書式はそのままにして、思考の上限だけを小さくする
                #   （Strata は上限に達すると、モデルの書き方で思考を閉じる・0 は「上限なし」なので使わない）。
                body["reasoning_budget_tokens"] = THINK_BUDGET_OFF
                if self.effort_pin in ("low", "medium", "high"):
                    body["reasoning_effort"] = self.effort_pin
            elif not think or budget == 0:
                body["reasoning_budget_tokens"] = 0
                body["chat_template_kwargs"] = {"enable_thinking": False, "reasoning": False}
            else:
                by_effort = {"high": THINK_BUDGET_DEEP, "low": THINK_BUDGET_LOW}
                base = int(_env_float("LAPS_REASONING_BUDGET", by_effort.get(effort, THINK_BUDGET)))
                if budget or base > 0:     # LAPS_REASONING_BUDGET=0 は上限なし
                    body["reasoning_budget_tokens"] = budget or base
                    body["reasoning_budget_message"] = THINK_LIMIT_NOTE
                # Strata は思考の強さをモデルが訓練された指示として渡せる（上限とは別・無指定はモデルの既定 high）。
                said = effort if self.effort_pin is None else self.effort_pin
                if info.strata and said in ("low", "medium", "high"):
                    body["reasoning_effort"] = said
        return body

    def stream(self, body: dict, *, on_text: Callable[[str], None] | None = None,
               on_think: Callable[[str], None] | None = None, watch: bool = True,
               on_retry: Callable[[int, str, float], None] | None = None, leak_ja: bool = False) -> Turn:
        """1 回の生成。一時的な失敗は間を置いて再試行する（推論機の再起動を待てるよう LAPS_RECONNECT_SECS 秒まで）。"""
        attempt = 0
        patience = _env_float("LAPS_RECONNECT_SECS", 90.0)
        t_first = 0.0
        while True:
            try:
                return self._once(body, on_text, on_think, watch, leak_ja)
            except (ContextOverflow, BackendError, Cancelled):
                raise
            except Exception as e:
                # 別のスレッドから接続を閉じたときの例外は環境で形が違う（ReadError・StreamClosed・RuntimeError など）。
                # 止めた結果なら理由を問わず「止めた」として扱う。
                if self._aborted:
                    raise Cancelled() from e
                if not isinstance(e, (httpx.TransportError, _Transient)):
                    raise
                t_first = t_first or time.monotonic()
                if patience <= 0 or (attempt >= len(RETRY_DELAYS) and time.monotonic() - t_first >= patience):
                    raise BackendError(f"The inference server did not answer: {e}") from e
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                attempt += 1
                if on_retry:
                    on_retry(attempt, str(e)[:200], delay)
                t0 = time.monotonic()
                while time.monotonic() - t0 < delay:
                    if self._aborted:
                        raise Cancelled() from e
                    time.sleep(0.2)

    def _open(self, url: str, body: dict) -> Any:
        """要求を送って見出しを待つ。★推論機の順番待ちの間は接続を閉じても起こせない（読み取りの時間切れ 600 秒まで止まる）ので、
        別の糸で待ち、止められたら置いていく（見出しが来たらその糸が閉じる）。"""
        box: dict = {}
        done = threading.Event()

        def work() -> None:
            try:
                resp = self._client.send(self._client.build_request("POST", url, json=body, headers=self._headers()), stream=True)
            except Exception as e:
                box["err"] = e
                done.set()
                return
            with self._lock:
                box["resp"] = resp
                left = box.get("left")
            if left:
                resp.close()
            done.set()
        threading.Thread(target=work, daemon=True, name="bw-llm-open").start()
        while not done.wait(0.2):
            if self._aborted:
                with self._lock:
                    if "resp" not in box:
                        box["left"] = True
                        raise Cancelled()
                break
        if "err" in box:
            raise box["err"]
        return box["resp"]

    def _once(self, body: dict, on_text, on_think, watch: bool, leak_ja: bool = False) -> Turn:
        with self._lock:
            self._aborted = False
        turn = Turn()
        calls: dict[int, dict] = {}
        # 迷いの書き出しで切るのは手元の推論機だけ（Claude の思考は「But」「Actually」で考えを進める形が普通で、切ると代金が無駄になる）
        hedge = int(_env_float("LAPS_THINK_HEDGE", THINK_HEDGE_HEADS)) if self.info().llama else 0
        think_watch = RepeatWatch(hedge=hedge) if watch else None
        text_watch = RepeatWatch(exact=6, near=9, min_chars=1500) if watch else None
        inline = _InlineThink()
        leak = _LeakedAnalysis() if leak_ja else None
        check_at = {"think": 400, "text": 400}
        self.progress = ""
        url = self.base_url + "/chat/completions"
        with contextlib.closing(self._open(url, body)) as resp:
            with self._lock:
                self._resp = resp
                if self._aborted:
                    raise Cancelled()
            try:
                if resp.status_code != 200:
                    self._raise_for(resp)
                for line in resp.iter_lines():
                    if self._aborted:
                        raise Cancelled()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        o = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(o.get("error"), dict) or isinstance(o.get("error"), str):
                        self._raise_message(str(o["error"]), 500)
                    if isinstance(o.get("prompt_progress"), dict):
                        self.progress = _progress_line(o["prompt_progress"])
                    if isinstance(o.get("usage"), dict):
                        turn.usage = o["usage"]
                    if isinstance(o.get("timings"), dict):
                        turn.timings = o["timings"]
                    for ch in o.get("choices") or []:
                        if ch.get("finish_reason"):
                            turn.finish = str(ch["finish_reason"])
                        delta = ch.get("delta") or {}
                        th = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        tx = delta.get("content") or ""
                        if tx:
                            more_th, tx = inline.split(tx)
                            th = (th or "") + more_th
                            if leak is not None and tx:
                                more_th, tx = leak.split(tx)
                                th = (th or "") + more_th
                        if th:
                            self.progress = ""
                            turn.thinking += th
                            if on_think:
                                on_think(th)
                            if think_watch is not None:
                                unit = think_watch.feed(th)
                                if unit:
                                    turn.cut, turn.cut_unit = think_watch.kind, unit
                                    return turn
                                if _broken(turn.thinking, check_at, "think", turn):
                                    return turn
                        if tx:
                            self.progress = ""
                            turn.text += tx
                            if on_text:
                                on_text(tx)
                            if text_watch is not None:
                                unit = text_watch.feed(tx)
                                if unit:
                                    turn.cut, turn.cut_unit = "repeat", unit
                                    return turn
                                if _broken(turn.text, check_at, "text", turn):
                                    return turn
                        for tc in delta.get("tool_calls") or []:
                            self.progress = ""
                            i = int(tc.get("index") or 0)
                            c = calls.setdefault(i, {"name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                c["name"] = fn["name"]
                            if fn.get("arguments"):
                                c["arguments"] += fn["arguments"]
            finally:
                with self._lock:
                    self._resp = None
        if leak is not None:
            rest = leak.close()
            if rest:
                turn.text += rest
                if on_text:
                    on_text(rest)
        turn.calls = [calls[i] for i in sorted(calls)]
        self._tally(turn)
        return turn

    def _tally(self, t: Turn) -> None:
        u, g = t.usage or {}, t.timings or {}
        s = self.totals
        s["calls"] += 1
        s["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        s["completion_tokens"] += int(u.get("completion_tokens") or 0)
        s["gen_ms"] += float(g.get("predicted_ms") or 0)
        s["gen_tokens"] += int(g.get("predicted_n") or 0)
        s["read_ms"] += float(g.get("prompt_ms") or 0)
        s["read_tokens"] += int(g.get("prompt_n") or 0)
        s["cached_tokens"] += int(g.get("cache_n") or 0)

    def _raise_for(self, resp) -> None:
        try:
            resp.read()
            text = resp.text
        except Exception:
            text = ""
        self._raise_message(text[:600], resp.status_code)

    def _raise_message(self, text: str, status: int) -> None:
        low = text.lower()
        if "exceed_context_size" in low or "exceeds the available context" in low or "context length" in low \
                or "exceeds the context window" in low \
                or "too many tokens" in low or "context_length_exceeded" in low:
            raise ContextOverflow(text)
        if status in (408, 429, 500, 502, 503, 504, 529) or "loading model" in low:
            raise _Transient(f"HTTP {status}: {text[:200]}")
        try:
            said = json.loads(text)["error"]["message"]    # 中継の文（今月の枠で止まった、など）をそのまま見せる
        except (ValueError, KeyError, TypeError):
            said = text[:400]
        raise BackendError(f"HTTP {status}: {said}")


class _Transient(Exception):
    pass


def _broken(text: str, at: dict, key: str, turn: Turn) -> bool:
    """生成が崩れたか（短い単位の繰り返し・語の切れ端の寄せ集め）。400 字ごとに見る。"""
    if len(text) < at[key]:
        return False
    at[key] = len(text) + 400
    try:
        from ..loopcut import degenerate_tail
        unit = degenerate_tail(text[-20000:])
    except Exception:
        return False
    if not unit:
        return False
    turn.cut, turn.cut_unit, turn.degenerate = "repeat", unit, True
    return True


class _LeakedAnalysis:
    """Gemma 4 は思考を閉じた後に本文で英語の分析を続けることがある（2026-10-10 オーナー「内部思考が本文に出てきてしまっています」）。
    日本語の依頼では、日本語で始まる行が来るまで本文を溜め、それより前は思考へ回す。日本語が 1 度も来なければ全部本文のまま返す。"""
    _JA = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")

    def __init__(self) -> None:
        self.passing = False
        self.buf = ""

    def split(self, text: str) -> tuple[str, str]:
        if self.passing:
            return "", text
        self.buf += text
        m = self._JA.search(self.buf)
        if not m:
            return "", ""
        start = self.buf.rfind("\n", 0, m.start()) + 1
        head, rest = self.buf[:start], self.buf[start:]
        self.passing, self.buf = True, ""
        return head, rest

    def close(self) -> str:
        rest, self.buf = self.buf, ""
        return rest


class _InlineThink:
    """推論機が思考を分けずに本文へ <think> のまま流す形（--jinja なしの llama-server など）を分ける。"""
    def __init__(self) -> None:
        self.open = False
        self.started = False

    def split(self, text: str) -> tuple[str, str]:
        if not self.started:
            if not text.strip():
                return "", text
            self.started = True
            if text.lstrip().startswith("<think>"):
                self.open = True
                text = text.lstrip()[len("<think>"):]
        if self.open:
            if "</think>" in text:
                a, b = text.split("</think>", 1)
                self.open = False
                return a, b.lstrip("\n")
            return text, ""
        return "", text


def _progress_line(p: dict) -> str:
    total = int(p.get("total") or 0)
    done = int(p.get("processed") or 0)
    if total <= 0 or done >= total:
        return ""
    cache = int(p.get("cache") or 0)
    ms = float(p.get("time_ms") or 0)
    pct = int(done * 100 / total)
    eta = ""
    fresh = done - cache
    if ms > 0 and fresh > 0:
        left = (total - done) * ms / fresh / 1000
        eta = f", ~{int(left)}s left" if left < 120 else f", ~{int(left // 60)} min left"
    return f"Reading {pct}% ({done:,} / {total:,} tokens{eta})"
