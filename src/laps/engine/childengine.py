"""子のエージェントだけを別の推論機で動かす（2026-11-25 オーナー「Gyro-M が子に命令を送って作業をさせて結果を受け取れるようにする
って感じです。Claude の仕組みに近い感じにしたい」）。

親（Gyro-M）と子が同じ推論機の席 1 つを分け合うと、子を起こすたびに親の席を保存して戻す（3〜15 秒）必要があり、その食い違いで親が
会話を頭から読み直したこともある（11-16・11-18）。子を別の GPU の推論機で動かせば、親の席は触られず、親は子の答えだけを受け取る。

子の推論機は LAPS_CHILD_BACKEND（OpenAI 互換の口・例 http://<機械>:8750/v1）。止まっている（GPU を貸し出し中・起動中）なら、
今までどおり親と同じ推論機で動かす。生きているかは /health を短く聞き、結果をしばらく覚える（毎回聞かない）。"""
from __future__ import annotations

import os
import re
import threading
import time

import httpx

from .llm import LLM

OK_SEC = 30          # 生きていると分かったら、この間は聞き直さない
DOWN_SEC = 60        # 止まっていると分かったら、この間は聞き直さない
_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()


def configured() -> str:
    url = (os.environ.get("LAPS_CHILD_BACKEND") or "").strip()
    return "" if url.lower() in ("", "0", "off", "none", "false", "no") else url.rstrip("/")


def alive(url: str, timeout: float = 1.5) -> bool:
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < (OK_SEC if hit[1] else DOWN_SEC):
            return hit[1]
    ok = False
    try:
        r = httpx.get(re.sub(r"/v1/?$", "", url) + "/health", timeout=timeout)
        ok = r.status_code == 200          # llama-server は読み込み中 503
    except Exception:  # noqa: BLE001
        ok = False
    with _lock:
        _cache[url] = (time.time(), ok)
    return ok


def forget(url: str) -> None:
    """子の推論機が途中で応えなくなった（GPU の貸し出しで止まった など）。しばらく使わない。"""
    with _lock:
        _cache[url.rstrip("/")] = (time.time(), False)


def open_for(parent: LLM) -> LLM | None:
    """子に使う推論機。無い・親と同じ・止まっているなら None（親と同じ推論機で動かす）。"""
    url = configured()
    if not url or url == parent.base_url.rstrip("/"):
        return None
    try:
        if not getattr(parent.info(), "llama", False):   # 親が Claude などの時は子も親と同じ（手元の小さいモデルに落とさない）
            return None
    except Exception:  # noqa: BLE001
        return None
    if not alive(url):
        return None
    llm = LLM(url, os.environ.get("LAPS_CHILD_MODEL") or "laps-child", api_key=os.environ.get("LAPS_CHILD_KEY") or "")
    llm.patience = 0                       # 応えなければ待たずに親の推論機へ（GPU の貸し出しで止まった時に 90 秒待たない）
    return llm
