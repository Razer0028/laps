"""会話の記録。置き場と形は読む側（端末版の /resume・スマホの読み返し）に合わせる:
    <home>/sessions/<id>/messages.jsonl   1 行 1 発言（OpenAI の形・思考と道具の呼び出しも入れる）
    <home>/sessions/<id>/events.jsonl     走行の事象（{"ts","seq","type","payload"}）
    <home>/sessions/<id>/meta.json

messages.jsonl は追記だけ（opencode と同じく、要約しても元の発言は消さない）。
要約と道具の出力の片付けは role=system の印の行で表し、モデルへ送る写し（view）で効かせる。"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from ..redact import redact
from ..session import is_valid_session_id, sessions_dir

CLEARED = "[Old tool result content cleared]"
_SEQ = re.compile(r'"seq":\s*(\d+)')


def _clean(text: str) -> str:
    try:
        return redact(text).text
    except Exception:
        return text


def _clean_value(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _clean_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean_value(x) for x in v]
    if not isinstance(v, str):
        return v
    if v.lstrip()[:1] in ("{", "["):       # 道具の引数のように JSON を文字列で持つ物
        try:
            json.loads(v)
        except ValueError:
            return _clean(v)
        return clean_json(v)
    return _clean(v)


def clean_json(text: str) -> str:
    """JSON の文字列を伏せる。★全体へ掛けると、値が引用符の手前の \\ まで飲み込んで JSON が壊れることがある
    （2026-10-31: `echo "login --password=abc"` で走行が落ち、記録の行は読み直しで消えた）。壊れたら値ごとに伏せ直す。"""
    out = _clean(text)
    if out == text:
        return text
    try:
        json.loads(out)
        return out
    except ValueError:
        pass
    return json.dumps(_clean_value(json.loads(text)), ensure_ascii=False, default=str) + ("\n" if text.endswith("\n") else "")


def new_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + f"_{random.randrange(16**6):06x}"


class EventLog:
    """stdout の事象を events.jsonl の形（読む側が知っている名前）で残す。"""
    NAMES = {"tool_call": "tool_start", "tool_result": "tool_end", "done": "result"}

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seq = 0
        #: 長い走行で記録が膨らまないよう、本文の流れは N 文字ごとにまとめて書く（画面にはチャンクのまま流れる）。
        try:
            self.coalesce = int(os.environ.get("LAPS_TOKEN_COALESCE_CHARS") or 400)
        except ValueError:
            self.coalesce = 400
        self._pending: dict | None = None
        self._pending_type = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = _SEQ.search(line)
                    if m:
                        self.seq = max(self.seq, int(m.group(1)))
        except OSError:
            pass
        self._fh = path.open("a", encoding="utf-8")

    def write(self, type_: str, payload: dict) -> None:
        if payload.get("kind") == "heartbeat":
            return
        if type_ in ("token", "think") and self.coalesce > 0:
            p = self._pending
            if p is not None and self._pending_type == type_ and p.get("id") == payload.get("id"):
                p["text"] = str(p.get("text") or "") + str(payload.get("text") or "")
            else:
                self._flush_pending()
                self._pending, self._pending_type = dict(payload), type_
            if len(str(self._pending.get("text") or "")) >= self.coalesce:
                self._flush_pending()
            return
        self._flush_pending()
        self._line(type_, payload)
        self._fh.flush()

    def _flush_pending(self) -> None:
        if self._pending is not None:
            p, t = self._pending, self._pending_type
            self._pending = None
            self._line(t, p)

    def _line(self, type_: str, payload: dict) -> None:
        self.seq += 1
        line = json.dumps({"ts": time.time(), "seq": self.seq, "type": self.NAMES.get(type_, type_), "payload": payload},
                          ensure_ascii=False, default=str)
        self._fh.write(clean_json(line) + "\n")

    def close(self) -> None:
        try:
            self._flush_pending()
            self._fh.close()
        except OSError:
            pass


#: 伏せて記録した発言の、伏せる前の中身（記録のパス → {記録の 1 行: 発言}）。この本体が動いている間だけ持ち、ディスクには書かない。
#: ★記録は伏せて残すが、走行中のモデルは伏せる前を読んでいる。次の依頼で記録から読むと伏字に変わり、そこから先のプロンプトが
#:   変わって推論機が会話を読み直す（2026-11-14 実機: 7 万トークンで 175 秒・Ask と Agent の切り替えで起きるように見えていた）。
#:   同じ本体の中では、伏せる前を使い続ける。本体を起動し直した後は記録（伏字）のとおりに送る。
_LIVE: dict[str, dict[str, dict]] = {}
_LIVE_SESSIONS = 8


class Transcript:
    def __init__(self, sid: str | None = None) -> None:
        self.id = sid if sid and is_valid_session_id(sid) else new_id()
        self.root = sessions_dir() / self.id
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "messages.jsonl"
        self.raw: list[dict] = []
        if self.path.exists():
            live = _LIVE.get(str(self.path)) or {}
            with self.path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    seen = live.get(line.rstrip("\n")) if live else None
                    if seen is not None:           # この本体が伏せて書いた行: モデルが読んだ中身のまま
                        self.raw.append(seen)
                        continue
                    try:
                        self.raw.append(json.loads(line))
                    except ValueError:
                        continue
        self._fh = self.path.open("a", encoding="utf-8")

    def _write_line(self, msg: dict) -> str:
        text = json.dumps(msg, ensure_ascii=False, default=str)
        line = clean_json(text).rstrip("\n")
        if line != text:                           # 伏せた: 伏せる前をこの本体の中にだけ覚える
            key = str(self.path)
            kept = _LIVE.pop(key, {})
            kept[line] = msg
            _LIVE[key] = kept                      # 使った会話を後ろへ
            while len(_LIVE) > _LIVE_SESSIONS:
                _LIVE.pop(next(iter(_LIVE)))
        return line + "\n"

    # ── 書く ─────────────────────────────
    def append(self, msg: dict) -> None:
        self.raw.append(msg)
        self._fh.write(self._write_line(msg))
        self._fh.flush()

    def rewrite(self, fn: Any) -> bool:
        """全部の発言を fn で置き換えてファイルも書き直す（会話で渡された鍵を保存した後に値を消すときだけ使う）。"""
        new = [fn(m) for m in self.raw]
        if new == self.raw:
            return False
        self.raw = new
        _LIVE.pop(str(self.path), None)            # 値を消すための書き直し: 覚えていた伏せる前も捨てる
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for m in new:
                fh.write(clean_json(json.dumps(m, ensure_ascii=False, default=str)) + "\n")
        self._fh.close()
        os.replace(tmp, self.path)
        self._fh = self.path.open("a", encoding="utf-8")
        return True

    def mark_compacted(self, summary: str, keep_from: int) -> None:
        self.append({"role": "system", "content": summary, "bw_compaction": {"keep_from": keep_from}})

    def mark_pruned(self, indexes: list[int]) -> None:
        self.append({"role": "system", "content": "", "bw_prune": indexes})

    def mark_images(self, text: list[int], keep: list[int]) -> None:
        """古い画像の行き先（text＝文字の 1 行に替える／keep＝画像のまま残す）。一度決めたら変えない（2026-11-16）。"""
        self.append({"role": "system", "content": "", "bw_images": {"text": text, "keep": keep}})

    def image_marks(self) -> tuple[set[int], set[int]]:
        """(文字に替えた画像, 画像のまま残すと決めた画像)。後の text は前の keep に勝つ（要約の後にまとめて替えるため）。"""
        text: set[int] = set()
        keep: set[int] = set()
        for m in self.raw:
            mark = m.get("bw_images")
            if isinstance(mark, dict):
                text.update(int(x) for x in mark.get("text") or [])
                keep.update(int(x) for x in mark.get("keep") or [])
        return text, keep - text

    def write_meta(self, **meta: Any) -> None:
        p = self.root / "meta.json"
        old: dict = {}
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        old.update(meta)
        try:
            p.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

    # ── 読む ─────────────────────────────
    def has_history(self) -> bool:
        return any(m.get("role") == "user" for m in self.raw)

    def last_compaction(self) -> tuple[int, dict] | None:
        for i in range(len(self.raw) - 1, -1, -1):
            if "bw_compaction" in self.raw[i]:
                return i, self.raw[i]
        return None

    def view(self) -> list[dict]:
        """モデルへ送る写し。最後の要約より前は送らない。片付けた道具の出力は短い印に替える。文字に替えた画像は画像の印を外す。"""
        pruned: set[int] = set()
        for m in self.raw:
            if "bw_prune" in m:
                pruned.update(int(x) for x in m["bw_prune"])
        texted, _ = self.image_marks()
        head: list[dict] = []
        start = 0
        last = self.last_compaction()
        if last is not None:
            i, mark = last
            start = int(mark["bw_compaction"].get("keep_from") or i)
            head = [{"role": "user", "content": mark.get("content") or ""}]
        out = head
        for i in range(start, len(self.raw)):
            m = self.raw[i]
            if m.get("role") == "system":
                continue
            if i in pruned and m.get("role") == "tool":
                m = {**m, "content": CLEARED}
            if i in texted and m.get("bw_image"):
                m = {k: v for k, v in m.items() if k != "bw_image"}
            out.append(m)
        return out
