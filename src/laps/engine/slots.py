"""推論機の席の控え（2026-11-16）。

推論機（Strata）の席は 1 つで、控え（読み込み済みの文脈）は最後に読んだ物しか残らない。
子のエージェントや別の会話が席を使うと、元の会話の控えが消え、次の手番で会話を頭から読み直す。
11-09〜16 の推論機の記録: この形の読み直しが 50 回・78 分（20 万トークンなら 1 回約 7 分）。

推論機は席をファイルへ保存して戻せる（/slots/0?action=save|restore・置き場は推論機の 推論機の slot_save_path）。
20.4 万トークン（3.35 GB）の保存は LAN から 4.3 秒だった。読み直すより 100 倍近く速い。

- 子のエージェント: 起こす前に親の席を保存し、子が終わったら親の次の手番の前に戻す（loop.Agent.spawn）。
- 会話の切り替え: 依頼の始めに、席に残っている前の会話を保存し、この会話の保存があれば戻す（server）。
  席に何が入っているかは、最後に送った長さ（n_prompt_tokens）で見分ける（推論機は中身を返さない）。
- 依頼の終わり（2026-11-25 オーナー「タスクを連続して頼んでもコンテキストの読み直しが発生する」）: 大きい会話（既定 4 万トークン
  以上・前の保存から 8 千トークン以上伸びた時）は、終わった後に裏で席を保存しておく。別の機械の L.A.P.S（別の PC の画面と
  アプリなど）が同じ推論機を使うと、相手はこちらの会話を知らないので保存しない。次の依頼で席が入れ替わっていれば、この控えから戻す。
  LAPS_SLOT_SAVE_AT_END=0 で止める・LAPS_SLOT_SAVE_MIN と LAPS_SLOT_SAVE_STEP で大きさを変える。
- 古いファイルの片付けは推論機の側（推論機 の strata-session-gc・新しい 8 本まで）。

LAPS_SLOT_CACHE=0 で止める。Strata の時だけ働く（llama.cpp にも同じ口はあるが確かめていない）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

#: これより短い文脈は保存しない（読み直しても 1 分かからない）。
MIN_TOKENS = 20000
#: 席の長さがこの差の内なら、同じ会話がそのまま入っているとみなす。
SAME_SLACK = 64
_lock = threading.Lock()
#: 終わりの保存（裏）が走っている間、次の依頼の begin と子の park は待つ（保存と戻しを重ねない）。
_save_lock = threading.Lock()


def _wait_saving(limit: float = 120.0) -> None:
    if _save_lock.acquire(timeout=limit):
        _save_lock.release()


def _env_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        return default


def enabled(llm: Any) -> bool:
    if os.environ.get("LAPS_SLOT_CACHE", "1").strip() == "0":
        return False
    try:
        return bool(llm.info().strata)
    except Exception:  # noqa: BLE001
        return False


def fingerprint(llm: Any) -> str:
    """控えを作った推論機の素性（モデルのファイル・推論エンジンの版・窓・会話の型）。違う素性の控えは戻さない（2026-11-20・
    外部の評価「控えにモデルと版を結び付け、食い違ったら戻さず読み直す」）。Strata は層の分け方の違いを自分で断る（400）。"""
    try:
        i = llm.info()
    except Exception:  # noqa: BLE001
        return ""
    raw = f"{i.model_path}|{i.build}|{i.n_ctx}|{hashlib.sha1(i.template.encode('utf-8')).hexdigest()[:12]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _usable(entry: dict, fp: str) -> bool:
    """控えの素性が今の推論機と同じか。素性の無い古い控え（11-20 より前）は今までどおり試す（推論機が断れば捨てる）。"""
    old = str(entry.get("fp") or "")
    return not old or not fp or old == fp


def file_name(conv_id: str) -> str:
    return "laps-" + hashlib.sha1(str(conv_id).encode("utf-8")).hexdigest()[:16] + ".bin"


def _registry_path() -> Path:
    from .. import paths as _lp
    return _lp.home_dir() / "slots.json"


def _load() -> dict:
    try:
        d = json.loads(_registry_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    p = _registry_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def park(llm: Any, conv_id: str, tokens_hint: int, count) -> str:
    """子を起こす前に、親の席を保存する。保存したファイルの名前（しなかったら ""）。"""
    if not enabled(llm) or tokens_hint < MIN_TOKENS:
        return ""
    _wait_saving()
    held = llm.slot_tokens()
    if not held or held < MIN_TOKENS:
        return ""
    name = file_name(conv_id)
    t0 = time.monotonic()
    ok, why = llm.slot_file("save", name)
    count("slot_saved" if ok else "slot_save_failed")
    if not ok:
        return ""
    count("slot_save_ms", int((time.monotonic() - t0) * 1000))
    with _lock:
        reg = _load()
        reg.setdefault("files", {})[conv_id] = {"name": name, "tokens": held, "t": time.time(), "fp": fingerprint(llm)}
        _save(reg)
    return name


def unpark(llm: Any, name: str, count) -> bool:
    """親の次の手番の前に、保存した席を戻す。"""
    t0 = time.monotonic()
    ok, why = llm.slot_file("restore", name)
    count("slot_restored" if ok else "slot_restore_failed")
    if ok:
        count("slot_restore_ms", int((time.monotonic() - t0) * 1000))
    return ok


def begin(llm: Any, conv_id: str, count) -> None:
    """依頼の始め: 席に別の会話（この機械の物）が残っていれば保存し、この会話の保存があれば戻す。"""
    if not enabled(llm):
        return
    _wait_saving()
    with _lock:
        reg = _load()
        held = llm.slot_tokens()
        if held is None:
            return
        owner, owner_tokens = reg.get("owner"), int(reg.get("owner_tokens") or 0)
        files = reg.setdefault("files", {})
        if owner == conv_id and abs(held - owner_tokens) <= SAME_SLACK:
            return                                     # 席にこの会話がそのまま入っている
        if owner and owner != conv_id and held >= MIN_TOKENS and abs(held - owner_tokens) <= SAME_SLACK:
            name = file_name(owner)
            ok, _ = llm.slot_file("save", name)
            count("slot_switch_saved" if ok else "slot_save_failed")
            if ok:
                files[owner] = {"name": name, "tokens": held, "t": time.time(), "fp": fingerprint(llm)}
        mine = files.get(conv_id)
        if mine and not _usable(mine, fingerprint(llm)):
            count("slot_restore_skipped_mismatch")     # モデル・版・窓が変わった＝戻さずに読み直す
            files.pop(conv_id, None)
            mine = None
        now_tokens = 0                                 # 席の中身はこれから入れ替わる（終わりに end が記す）
        if mine:
            t0 = time.monotonic()
            ok, why = llm.slot_file("restore", str(mine.get("name") or file_name(conv_id)))
            count("slot_switch_restored" if ok else "slot_restore_failed")
            if ok:
                count("slot_restore_ms", int((time.monotonic() - t0) * 1000))
                now_tokens = llm.slot_tokens() or 0
            else:
                files.pop(conv_id, None)               # 戻せない（推論機の設定が変わった・消えた）＝次から試さない
        reg["owner"], reg["owner_tokens"] = conv_id, now_tokens
        _save(reg)


def held_for(llm: Any, conv_id: str) -> int:
    """この会話が席に入っているなら、その長さ（推論機が数えた本物のトークン数）。分からなければ 0。
    begin の後に呼ぶ（席にこの会話がある・戻した時に記した長さ）。境目の要約のしきい値に使う（2026-11-25・文字からの見積もりより確か）。"""
    if not enabled(llm):
        return 0
    with _lock:
        reg = _load()
    return int(reg.get("owner_tokens") or 0) if reg.get("owner") == conv_id else 0


def end(llm: Any, conv_id: str) -> None:
    """依頼の終わり: 席にこの会話が入っていると記す（次の切り替えで保存するため）。大きい会話は裏で控えも保存する（2026-11-25）。"""
    if not enabled(llm):
        return
    held = llm.slot_tokens()
    if held is None:
        return
    with _lock:
        reg = _load()
        reg["owner"], reg["owner_tokens"] = conv_id, held
        prev = dict((reg.get("files") or {}).get(conv_id) or {})
        _save(reg)
    if (os.environ.get("LAPS_SLOT_SAVE_AT_END") or "1").strip().lower() in ("0", "off", "false", "no"):
        return
    if held < _env_int(os.environ.get("LAPS_SLOT_SAVE_MIN"), 40000):
        return
    fp = fingerprint(llm)
    if prev and str(prev.get("fp") or "") == fp and \
            abs(held - int(prev.get("tokens") or 0)) < _env_int(os.environ.get("LAPS_SLOT_SAVE_STEP"), 8000):
        return                                         # 前の保存からあまり伸びていない（毎回 GB 単位で書かない）
    threading.Thread(target=_save_bg, args=(llm, conv_id, held, fp), name="bw-slot-save", daemon=True).start()


def _save_bg(llm: Any, conv_id: str, held: int, fp: str) -> None:
    """終わった後に裏で席を保存する（答えを待たせない）。もう次の依頼が席を使っていたら保存しない。"""
    with _save_lock:
        try:
            if llm.slot_tokens() != held:
                return
            name = file_name(conv_id)
            ok, _why = llm.slot_file("save", name)
            if not ok:
                return
            with _lock:
                reg = _load()
                reg.setdefault("files", {})[conv_id] = {"name": name, "tokens": held, "t": time.time(), "fp": fp}
                _save(reg)
        except Exception:  # noqa: BLE001 — 控えの不具合で本体を止めない
            return
