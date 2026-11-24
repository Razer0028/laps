"""画面との線。stdout に JSON を 1 行ずつ出す（アプリ・端末版・スマホの中継が読む形）。

    {"v":1,"seq":3,"ts":…,"type":"token","id":"r1","text":"…"}

stdout は線の専用。人が読む出力は stderr へ。

読み手（アプリ・中継）が止まっても走行を凍らせないよう、書き出しは専用のスレッドで行う。
走行の間は同じ行を会話の置き場の live.ndjson にも書く（スマホの「追う画面」がこれを読む）。"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Any, Callable

PROTOCOL = 1


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _real_stream(stream: Any) -> bool:
    try:
        stream.fileno()
        return True
    except (OSError, ValueError, AttributeError):
        return False


class Out:
    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()
        self._seq = 0
        #: 走行の間だけ付く写し先（events.jsonl）。
        self.tap: Callable[[str, dict], None] | None = None
        #: 最後に何かを出した時刻（心拍が見る）。
        self.last = time.monotonic()
        self.dropped = 0
        self._tee: Any = None
        self._tee_clean: Callable[[str], str] | None = None
        self._tee_rid: str | None = None
        self._q: "queue.Queue[str | None] | None" = None
        self._writer: threading.Thread | None = None
        self._closed = False
        # メモリ上の stream（試験）は同期のまま。LAPS_OUT_SYNC=1 でも同期。
        if _real_stream(self.stream) and os.environ.get("LAPS_OUT_SYNC") != "1":
            self._q = queue.Queue(maxsize=max(16, int(_env_num("LAPS_OUT_QUEUE", 5000))))
            self._writer = threading.Thread(target=self._pump, name="bw-out", daemon=True)
            self._writer.start()

    # ── 書き出し ──────────────────────────
    def _write(self, line: str) -> None:
        try:
            self.stream.write(line)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def _pump(self) -> None:
        while True:
            line = self._q.get()
            if line is None:
                self._q.task_done()
                return
            self._write(line)
            self._q.task_done()

    def _enqueue(self, line: str, type_: str) -> None:
        q = self._q
        if q is None:
            self._write(line)
            return
        try:
            q.put_nowait(line)
            return
        except queue.Full:
            pass
        if type_ in ("token", "think"):    # 読み手が遅い。流れの途中は捨ててよい（本文は done にも載る）
            self.dropped += 1
            if self.dropped in (1, 100, 1000):
                log(f"stdout の待ち行列が満杯（読み手が遅い）: {type_} を {self.dropped} 件捨てた")
            return
        wait = _env_num("LAPS_OUT_BLOCK_SECS", 2.0)
        terminal = type_ in ("done", "error")   # 走行を閉じる行は、書き手が生きている限り捨てない（画面が固まるため）
        while True:
            try:
                q.put(line, timeout=wait if terminal else wait * 5)
                return
            except queue.Full:
                if terminal and self._writer is not None and self._writer.is_alive():
                    continue
                self.dropped += 1
                log(f"stdout の待ち行列が満杯のまま: {type_} を捨てた（累計 {self.dropped}）")
                return

    def close(self, timeout: float = 5.0) -> None:
        """待ち行列を書き切って書き手を止める（プロセスの終わりに呼ぶ）。"""
        if self._closed:
            return
        self._closed = True
        if self._q is None or self._writer is None:
            return
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            return
        self._writer.join(timeout)

    # ── 走行の写し ────────────────────────
    def tee_open(self, path: Any, clean: Callable[[str], str] | None = None, rid: str | None = None) -> None:
        """この走行の事象を path にも書く（前の写しは閉じて作り直す）。done で閉じる。"""
        with self._lock:
            self._tee_close()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._tee = open(path, "w", encoding="utf-8")
                self._tee_clean = clean
                self._tee_rid = rid
            except OSError as e:
                log(f"走行の写しを開けない: {e!r}")

    def _tee_close(self) -> None:
        if self._tee is not None:
            try:
                self._tee.close()
            except OSError:
                pass
            self._tee = None

    def _tee_write(self, line: str, type_: str, rid: str | None = None) -> None:
        if self._tee is None:
            return
        if type_ == "done" and self._tee_rid and rid != self._tee_rid:
            return                         # 順番待ちの依頼を取り消した done。いま走っている写しには書かない
        try:                               # 追う側がすぐ読めるように 1 行ずつ flush
            self._tee.write(self._tee_clean(line) if self._tee_clean else line)
            self._tee.flush()
        except (OSError, ValueError):
            self._tee_close()
            return
        if type_ == "done":
            self._tee_close()

    # ── 出す ─────────────────────────────
    def emit(self, type_: str, rid: str | None = None, **fields: Any) -> None:
        with self._lock:
            self._seq += 1
            rec = {"v": PROTOCOL, "seq": self._seq, "ts": round(time.time(), 3), "type": type_, "id": rid, **fields}
            line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            self._tee_write(line, type_, rid)
            self._enqueue(line, type_)
            if type_ != "notice" or fields.get("kind") != "heartbeat":
                self.last = time.monotonic()
        tap = self.tap
        if tap is not None:
            try:
                tap(type_, {"id": rid, **fields})
            except Exception:
                pass                       # 記録の不具合で走行を落とさない

    def notice(self, kind: str, rid: str | None = None, **fields: Any) -> None:
        self.emit("notice", rid, kind=kind, **fields)


def log(msg: str) -> None:
    try:
        sys.stderr.write(f"[engine] {msg}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
