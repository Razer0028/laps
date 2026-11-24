"""gateway 経由（Web / Android）の走行を telemetry の record にする（2026-09-07）。

★なぜ: 実機の Windows 版は「Send log」で record（`記録の受け口`）を残し、
  それが今日の真因（git check-ignore の永久停止）を割る材料になった。Web/Android 経路は
  gateway が serve を動かすのに record を残しておらず、同じ可視性が無かった。
  gateway は serve の全事象（seq つき）を中継しているので、そこから Rust 側（journal.rs）と
  同じ形の record を組み、走行の終わり（done）に ingest へ 1 行 POST する。

形は実機の record に合わせる（client / engine / input / output / tools / cost / stop / counters）。
本文は先頭だけ（input 2000 字・output 4000 字・道具の結果 120 字）。合言葉・token は含めない。
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
import urllib.request
from typing import Any

#: 記録の受け口。既定は空＝送らない（LAPS_TELEMETRY で自分の受け口を渡す）。
DEFAULT_URL = ""


def telemetry_url() -> str | None:
    """`LAPS_GATEWAY_TELEMETRY_URL`（空文字 = 送らない・未設定 = 既定）。"""
    v = os.environ.get("LAPS_GATEWAY_TELEMETRY_URL")
    if v is None:
        return DEFAULT_URL or None
    v = v.strip()
    return v or None


class RunRecorder:
    """1 セッションぶんの事象を食べて、走行（依頼→done）ごとに record を出す。"""

    def __init__(self, *, sid: str, version: str, peer: str = "", workspace: str = "",
                 backend: str = "", model: str = "") -> None:
        self.sid = sid
        self.version = version
        self.peer = peer
        self.workspace = workspace
        self.backend = backend
        self.model = model
        self._reset()

    # ── 依頼側（クライアント → serve）────────────────────────
    def note_client(self, msg: dict) -> None:
        # ★"send" と "run" の両方を受ける。**私（0.1.209 の監査 C1）と外部レビューが独立に見つけた**同じ欠陥。
        #   serve.py は昔から両方を受理するのに、ここは "send" しか見ていなかった。実在する 2 クライアント
        #   （webui/app.js・Android ServeCommand.kt）はどちらも `type:"run"` を送るので、
        #   **本番の Web/Android 走行は record が 1 本も作られていなかった**
        #   （試験は手書きの send 型だけを通していたので緑のまま）。
        #   ★このモジュールは 2026-09-07 に「Web/Android 経路が record を残していない」を直すために
        #   作られたのに、webui についてその目的が達成されていなかった。しかも
        #   **2026-08-11 に serve 側で一度直した send/run の食い違いの再発**（serve.py の同名コメント）。
        #   ＝「片方で直して片方に残す」の型。次に受け口を足すときは 3 クライアントの送信型を先に数える。
        if msg.get("type") not in ("send", "run"):
            return
        self._reset()
        self.request_id = str(msg.get("id") or "")
        text = msg.get("prompt")
        if not text and isinstance(msg.get("messages"), list):
            for m in reversed(msg["messages"]):
                if isinstance(m, dict) and m.get("role") == "user":
                    text = m.get("content"); break
        self.input_text = str(text or "")
        self.t0 = time.time()
        self.active = True

    # ── 事象側（serve → クライアント）────────────────────────
    def feed(self, line: str) -> dict | None:
        """1 行食べる。走行が終わった（done）なら record を返す（それ以外は None）。"""
        try:
            ev = json.loads(line)
        except Exception:
            return None
        if not isinstance(ev, dict):
            return None
        t = ev.get("type")
        # ★2026-09-08 監査 C1: serve が線に出す走行開始は `{"type":"notice","kind":"run_start"}`
        #   であって `{"type":"run_start"}` という型は**存在しない**（旧試験の fixture が
        #   その架空の形を書き、本番では一度も活性化しなかった）。実物の形で判定する。
        run_start = (t == "notice" and ev.get("kind") == "run_start") or t == "run_start"
        if not self.active and not run_start:
            return None
        if run_start:
            self.active = True
            self.iters = 0
            if not self.request_id:
                self.request_id = str(ev.get("id") or "")
        elif t == "iter_start":
            self.iters += 1
        elif t == "token":
            self.output.append(str(ev.get("text") or ""))
            self.tokens += 1
        elif t == "think":
            self.think_chars += len(str(ev.get("text") or ""))
        elif t == "tool_call":
            cid = str(ev.get("call") or len(self.tools))
            self.pending[cid] = {"name": str(ev.get("name") or "?"),
                                 "args": json.dumps(ev.get("input") or {}, ensure_ascii=False)[:200],
                                 "t0": time.time()}
        elif t == "tool_result":
            cid = str(ev.get("call") or "")
            p = self.pending.pop(cid, None) or {"name": str(ev.get("name") or "?"), "args": "", "t0": time.time()}
            body = str(ev.get("text") or ev.get("result") or "")
            ok = bool(ev.get("ok", True))
            kind = str(ev.get("kind") or "")
            # ★成否の種類と失敗の分類を残す（2026-10-18）。それまで捨てていたので、失敗の理由を分けるには本文を読むしかなかった。
            try:
                from .oc.failclass import classify as _classify
                cls = _classify(p["name"], ok, kind, body)
            except Exception:
                cls = None
            self.tools.append({"name": p["name"], "ok": ok, "kind": kind, "cls": cls,
                               "secs": round(time.time() - p["t0"], 1), "args": p["args"],
                               "result_chars": len(body), "result_head": body[:120]})
        elif t == "notice":
            k = str(ev.get("kind") or "")
            if k:
                self.notices[k] = self.notices.get(k, 0) + 1
        elif t == "error":
            self.last_error = str(ev.get("message") or ev.get("kind") or "")[:300]
        elif t == "done":
            return self._finish(ev)
        return None

    # ── 内部 ──────────────────────────────────────────────
    def _reset(self) -> None:
        self.active = False
        self.request_id = ""
        self.input_text = ""
        self.t0 = time.time()
        self.iters = 0
        self.tokens = 0
        self.output: list[str] = []
        self.tools: list[dict] = []
        self.pending: dict[str, dict] = {}
        self.notices: dict[str, int] = {}
        self.last_error = ""
        self.think_chars = 0

    def _finish(self, done: dict) -> dict:
        secs = round(time.time() - self.t0, 1)
        out = "".join(self.output) or str(done.get("text") or "")
        outcome = str(done.get("outcome") or "done")
        err = done.get("error") or (self.last_error if outcome not in ("done", "accepted") else None)
        rec = {
            "rec": 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S") + time.strftime("%z")[:3] + ":" + time.strftime("%z")[3:],
            "id": f"{self.sid}-{self.request_id or 'r'}",
            "client": {"kind": "gateway", "app_version": self.version, "host_label": self.peer or "gateway",
                       "install_id": f"gw-{platform.node()}"},
            "engine": {"kind": "gateway", "model": self.model or None, "workspace": self.workspace,
                       "backend": self.backend, "backend_source": "gateway"},
            "run": {"request_id": self.request_id, "session": self.sid},
            "input": {"text": self.input_text[:2000], "chars": len(self.input_text),
                      "truncated": len(self.input_text) > 2000},
            "output": {"text": out[:4000], "chars": len(out)},
            "tools": self.tools,
            "cost": {"iters": self.iters, "secs": secs, "answer_tokens": self.tokens,
                     "tok_s": round(self.tokens / secs, 1) if secs > 0 else 0.0},
            "stop": {"reason": outcome, "error": err},
            "counters": {"notices": self.notices},
            # ★思考の字数（アプリの記録と同じ欄・2026-10-11: ゲートウェイ経由の記録は思考も数え値も空だった）。
            "think": {"chars": int(done.get("think_chars") or self.think_chars)},
        }
        # ★serve の done が持つ走行の数え値・道具ごとの回数・トークンを、アプリの記録と同じ欄へ写す
        #   （ウェブ版から送った走行はゲートウェイで走る＝これが無いと見回りで何も分からない）。
        dc = done.get("counters")
        if isinstance(dc, dict):
            rec["counters"].update({k: v for k, v in dc.items() if isinstance(k, str) and k != "notices"})
        tc = done.get("tool_calls")
        if isinstance(tc, dict):
            rec["counters"].update({f"tool_calls:{k}": v for k, v in tc.items()})
        for k in ("prompt_tokens", "answer_tokens", "think_tokens", "tok_s"):
            if isinstance(done.get(k), (int, float)):
                rec["cost"][k] = done[k]
        if isinstance(done.get("iters"), int):
            rec["cost"]["iters"] = done["iters"]
        self._reset()
        return rec


def post_record(rec: dict, url: str | None = None, *, timeout: float = 5.0, on_error=None) -> None:
    """record を 1 行で POST（背景スレッド・失敗は走行を止めないが **黙らない**＝on_error に 1 行）。"""
    url = url if url is not None else telemetry_url()
    if not url:
        return
    text = json.dumps(rec, ensure_ascii=False)
    try:   # ★保存した API キーの値は外へ送らない（laps/apikeys.py・2026-10-08）
        from . import apikeys
        if apikeys.has_keys():
            text = apikeys.redact(text)[0]
    except Exception:
        pass
    body = (text + "\n").encode("utf-8")

    def _go() -> None:
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/x-ndjson"})
            urllib.request.urlopen(req, timeout=timeout).read(64)
        except Exception as e:
            if on_error is not None:
                try:
                    on_error(f"record を送れない（{url}）: {e!r}")
                except Exception:
                    pass
    threading.Thread(target=_go, name="bw-gw-record", daemon=True).start()
