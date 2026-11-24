"""中継の向こうの機械の本体（serve）を、標準入出力の子のように見せる橋（2026-10-30）。

オーナー「remote control の仕組み」。Windows のアプリから、ほかの機械（開発機・作業用のコンテナ など）の会話を開いて続けるため。
中継（relay.py）の `/d/<機械>/ws?session=<名札>` は、その機械のゲートウェイの serve と 1 行ずつ（NDJSON 1 行＝WebSocket の
文字のフレーム 1 つ）をそのまま運ぶ（Android と同じ線）。この橋は標準入力の 1 行を 1 フレームで送り、受けたフレームを
標準出力の 1 行で書く＝アプリ（Rust の sidecar.rs）は、この PC の serve の子と同じ扱いで走行を読める（run_loop をそのまま使う）。

使い方: python -m laps.relaybridge --relay ws://<中継の住所>:18400 --device <機械の名札> --session <この線の名札>
線が閉じたら終わる（終わり方は 1 行の JSON を標準エラーへ）。標準入力が閉じたら線を閉じて終わる。

追う（2026-10-31 オーナー「リモートコントロールを Claude と同じ感じに」）: `--follow <会話の名札> --as-id <この PC の依頼の id>`。
向こうで走っている走行の写しを、この PC の走行と同じ行として渡す＝画面は実況をそのまま描ける。依頼は送らない。
割り込み・止める・答えはゲートウェイの受け箱へ届く。線が切れたら繋ぎ直して続きから読み、走行の終わり（done）で終わる。
認証情報と TLS は laps/relayauth.py（2026-10-31）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
from urllib.parse import quote

from . import relayauth

FOLLOW_BEAT = 30.0                 # 追っている間の心拍（秒）
FOLLOW_GIVE_UP = 600.0             # 線が切れたまま、これだけ経ったらあきらめる（秒）


def _url(relay: str, device: str, session: str) -> tuple[str, dict]:
    """(住所, connect の引数)。認証情報は見出しで運ぶ（住所に書かない）。"""
    base, kw = relayauth.ws_connect_args(relay)
    return f"{base}/d/{quote(device)}/ws?session={quote(session)}", kw


async def run(relay: str, device: str, session: str) -> int:
    from websockets.asyncio.client import connect

    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()

    def read_stdin() -> None:                       # 標準入力は別の糸で読む（Windows の asyncio はパイプを直接待てない）
        for line in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, line.rstrip("\r\n"))
        loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()
    try:
        url, kw = _url(relay, device, session)
        ws = await connect(url, max_size=64 * 1024 * 1024, open_timeout=30, **kw)
    except Exception as e:  # noqa: BLE001 — 繋がらない理由をそのまま渡す
        print(json.dumps({"type": "error", "message": f"中継の向こうの機械 {device} に繋がりません: {e}"},
                         ensure_ascii=False), flush=True)
        return 2

    async def up() -> None:
        while True:
            line = await lines.get()
            if line is None:
                await ws.close()
                return
            if line.strip():
                await ws.send(line)

    async def down() -> None:
        async for msg in ws:
            text = msg if isinstance(msg, str) else msg.decode("utf-8", "replace")
            sys.stdout.write(text.rstrip("\n") + "\n")
            sys.stdout.flush()

    t_up, t_down = asyncio.create_task(up()), asyncio.create_task(down())
    await asyncio.wait({t_up, t_down}, return_when=asyncio.FIRST_COMPLETED)
    for t in (t_up, t_down):
        t.cancel()
    code = getattr(ws, "close_code", None)
    print(json.dumps({"bridge": "closed", "device": device, "code": code}, ensure_ascii=False), file=sys.stderr, flush=True)
    return 0


async def follow(relay: str, device: str, sid: str, as_id: str) -> int:
    from websockets.asyncio.client import connect

    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, line.rstrip("\r\n"))
        loop.call_soon_threadsafe(lines.put_nowait, None)

    def say(o: dict) -> None:
        sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    async def beat() -> None:              # 写しに心拍は載らない。黙っている道具の間に画面が「応答なし」にならないように
        while True:
            await asyncio.sleep(FOLLOW_BEAT)
            say({"v": 1, "seq": 0, "type": "notice", "id": as_id, "kind": "heartbeat", "stage": "向こうの機械"})

    threading.Thread(target=read_stdin, daemon=True).start()
    say({"type": "ready"})
    beater = asyncio.create_task(beat())
    seen, real_id, lost_at, delay = 0, "", 0.0, 2.0
    state = {"done": False, "closed": False}
    try:
        while True:
            why = ""
            try:
                base, kw = relayauth.ws_connect_args(relay)
                ws = await connect(f"{base}/d/{quote(device)}/ws?follow={quote(sid)}&since_seq={seen}",
                                   max_size=64 * 1024 * 1024, open_timeout=30, **kw)
            except Exception as e:  # noqa: BLE001 — 繋がらない理由は最後に渡す
                ws, why = None, str(e)
            if ws is not None:
                lost_at, delay = 0.0, 2.0

                async def up(ws=ws) -> None:
                    while True:
                        line = await lines.get()
                        if line is None:
                            state["closed"] = True
                            return
                        if not line.strip():
                            continue
                        try:
                            o = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(o, dict) and o.get("id") == as_id:
                            o["id"] = real_id
                        await ws.send(json.dumps(o, ensure_ascii=False))

                async def down(ws=ws) -> None:
                    nonlocal seen, real_id
                    async for msg in ws:
                        text = msg if isinstance(msg, str) else msg.decode("utf-8", "replace")
                        try:
                            o = json.loads(text)
                        except ValueError:
                            continue
                        if not isinstance(o, dict):
                            continue
                        seen = max(seen, int(o.get("seq") or 0))
                        if o.get("id"):
                            real_id = str(o["id"])
                            o["id"] = as_id
                        say(o)
                        if o.get("type") == "done":
                            state["done"] = True
                            return

                t_up, t_down = asyncio.create_task(up()), asyncio.create_task(down())
                await asyncio.wait({t_up, t_down}, return_when=asyncio.FIRST_COMPLETED)
                for t in (t_up, t_down):
                    t.cancel()
                why = str(t_down.exception()) if t_down.done() and not t_down.cancelled() and t_down.exception() else ""
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
                if state["done"] or state["closed"]:
                    return 0
            lost_at = lost_at or time.monotonic()
            if time.monotonic() - lost_at > FOLLOW_GIVE_UP:
                say({"v": 1, "seq": 0, "type": "error", "id": as_id, "kind": "fatal",
                     "message": f"{device} に繋がらなくなりました。向こうの走行は続いているかもしれません（{why[:200]}）"})
                return 2
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    finally:
        beater.cancel()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="laps.relaybridge")
    ap.add_argument("--relay", default=os.environ.get("LAPS_RELAY_URL") or "ws://127.0.0.1:18400")
    ap.add_argument("--device", required=True)
    ap.add_argument("--session", default="")
    ap.add_argument("--follow", default="", help="走っている会話の名札（依頼を送らずに追う）")
    ap.add_argument("--as-id", default="r1", help="--follow のとき、行に付ける依頼の id")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", a.device) or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,128}", a.follow or a.session):
        print(json.dumps({"type": "error", "message": "機械か線の名札の形が違います"}, ensure_ascii=False), flush=True)
        return 2
    try:
        if a.follow:
            return asyncio.run(follow(a.relay, a.device, a.follow, a.as_id))
        return asyncio.run(run(a.relay, a.device, a.session))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
