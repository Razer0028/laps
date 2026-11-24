"""この機械の会話の目録（2026-10-30・全端末で 1 つの会話の一覧）。

オーナー（2026-10-30）「Claude のアプリのような使いかた」→ 選択「一覧は中継・中身は元の機械」。
名乗り役（remotehost）がこれで自分の会話を 1 件 1 行に縮め、中継（relay.py）へ送る。中継は全機械ぶんを覚えて、
スマホとアプリに「どの機械のどの会話が・いまどうなっているか」を配る。中身（記録）は元の機械に置いたまま。

1 件の形（中継・スマホ・アプリで同じ）:
    {"sid", "title", "first", "last", "status", "outcome", "updated", "source", "via", "ask", "secs", "quiet", "group"}
    status … running（走っている）／asking（答えを待っている）／done／failed／stopped／idle（記録だけ）

★読むのは各会話のフォルダの頭と尻だけ（記録を全部は読まない）。変わっていないフォルダは前の結果を使う。
★blackwall の重いモジュールを読み込まない（名乗り役は Windows のアプリの子としても動く）。

★知らせるかはここで 1 か所で決める（quiet・各端末は読むだけ＝同じ判定を 2 つの言語で書かない）。手本は Claude の Remote Control:
  知らせるのは判断が要るときと、長い仕事が終わったときだけ。機械の前に居る間は知らせない
  （2026-10-31 オーナー「ベンチを回すと通知がめっちゃ来ます」）。自動で流した走行（--json・source=batch）は一覧にも載せない。
"""
from __future__ import annotations

from . import paths as _lp
import json
import os
import time
from pathlib import Path

#: 目録に載せる会話の数（新しい順）・古さの上限
MAX_ITEMS = 300
MAX_AGE_SECS = 90 * 86400
#: 写し（live.ndjson）がこれより古いまま終わりの印が無ければ、走っているとは見ない（gateway の /live と同じ）
LIVE_STALE_SECS = 300
_TEXT_MAX = 160
#: これより短い走行の終わりは知らせない（秒）
NOTIFY_MIN_SECS = 60.0
#: アプリの窓が前にある間、アプリがこのファイルを新しくし続ける＝機械の前に人が居る
PRESENCE_FILE = ("remote", "presence")
PRESENCE_SECS = 20.0
#: 人が見ない走行（--json の自動の走行）の出どころ
BATCH_SOURCE = "batch"

_DONE_OUTCOMES = {"accepted", "accepted_after_pushback", "done", "ok", "answered"}
_STOP_OUTCOMES = {"cancelled", "canceled", "stopped", "interrupted", "halted"}


def _user_head(content: str, n: int) -> str:
    """利用者の文の頭（serve は依頼を `[アプリからの指示]…[利用者]\\n<本文>` で包む・gateway._user_head と同じ）。"""
    marker = "[利用者]\n"
    i = content.rfind(marker)
    body = content[i + len(marker):] if i >= 0 else content
    return " ".join(body.split())[:n]


def _clip(s: str, n: int = _TEXT_MAX) -> str:
    return " ".join(str(s or "").split())[:n]


def _read_head_tail(f: Path, head: int, tail: int) -> tuple[list[bytes], list[bytes], int]:
    st = f.stat()
    with open(f, "rb") as fh:
        h = fh.read(head)
        fh.seek(max(0, st.st_size - tail))
        t = fh.read()
    return [x for x in h.split(b"\n") if x.strip()], [x for x in t.split(b"\n") if x.strip()], st.st_size


def _first_user(d: Path) -> str:
    try:
        with open(d / "messages.jsonl", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("role") == "user":
                    return _user_head(str(m.get("content") or ""), 120)
    except OSError:
        pass
    return ""


def _from_events(ev: Path) -> dict:
    """記録（events.jsonl）の頭の run_spec と、尻の最後の result（どう終わったか・最後の答え）。"""
    out: dict = {}
    try:
        heads, tails, _ = _read_head_tail(ev, 65536, 262144)
    except OSError:
        return out
    t0 = 0.0
    for raw in heads:
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if not t0:
            t0 = float(o.get("ts") or 0)
        if o.get("type") == "run_spec":
            p = o.get("payload") or {}
            out["prompt"] = _user_head(str(p.get("prompt") or ""), 120)
            out["source"] = str(p.get("source") or "")
            out["via"] = str(p.get("via") or "")
            break
    for raw in reversed(tails):
        if b'"result"' not in raw:
            continue
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if o.get("type") != "result":
            continue
        p = o.get("payload") or {}
        out["outcome"] = str(p.get("outcome") or "")
        out["last"] = _clip(p.get("text") or "")
        t1 = float(o.get("ts") or 0)
        # ★その走行の長さ（結果の secs）。会話の最初からの長さにすると、長い会話の短い続きまで「長い仕事」になる。
        if isinstance(p.get("secs"), (int, float)):
            out["secs"] = round(float(p["secs"]), 1)
        elif t0 and t1 >= t0:
            out["secs"] = round(t1 - t0, 1)
        break
    return out


def _from_live(lv: Path, now: float) -> dict:
    """写し（live.ndjson）から、いま走っているか・答えを待っているか・どこから始めたか。"""
    out: dict = {}
    try:
        heads, tails, _ = _read_head_tail(lv, 16384, 65536)
        age = now - lv.stat().st_mtime
    except OSError:
        return out
    for raw in heads:
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if o.get("type") == "notice" and o.get("kind") == "run_start":
            out["source"] = str(o.get("source") or "")
            out["via"] = str(o.get("via") or "")
            if o.get("prompt"):
                out["prompt"] = _user_head(str(o.get("prompt")), 120)
            break
    ended, ask, text = False, None, []
    for raw in tails:
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        t = o.get("type")
        if t == "done":
            ended = True
            ask = None
            if o.get("text"):
                out["last"] = _clip(o.get("text"))
            if o.get("outcome"):
                out["outcome"] = str(o.get("outcome"))
            if isinstance(o.get("secs"), (int, float)):
                out["secs"] = round(float(o["secs"]), 1)
        elif t == "ask":
            ask = _clip(o.get("question") or "", 200)
        elif t == "notice" and o.get("kind") in ("ask_closed", "ask_expired"):
            ask = None
        elif t == "text" and o.get("text"):
            text.append(str(o.get("text")))
        elif t == "token" and isinstance(o.get("text"), str):
            text.append(o["text"])
    if not ended and age < LIVE_STALE_SECS:
        out["status"] = "asking" if ask else "running"
        if ask:
            out["ask"] = ask
        if text and "last" not in out:
            out["last"] = _clip("".join(text)[-400:])
    out["live_mtime"] = lv.stat().st_mtime if lv.exists() else 0
    return out


def _status_of(outcome: str) -> str:
    o = (outcome or "").lower()
    if not o:
        return "idle"
    if o in _DONE_OUTCOMES:
        return "done"
    if o in _STOP_OUTCOMES:
        return "stopped"
    return "failed"


def app_meta(home: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Windows のアプリの会話の題とグループ（app_root/conversations.json の session → title・group）。無ければ空。
    アプリは LAPS_HOME＝app_root/agent にしているので、その 1 つ上を見る。グループは 2026-11-19（0.1.707 の会話のグループを
    ほかの機械とスマホにも見せる）。"""
    f = home.parent / "conversations.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    titles, groups = {}, {}
    for c in data if isinstance(data, list) else []:
        if not (isinstance(c, dict) and c.get("session")):
            continue
        sid = str(c["session"])
        if c.get("title"):
            titles[sid] = _clip(c["title"], 120)
        if isinstance(c.get("group"), str) and c["group"].strip():
            groups[sid] = _clip(c["group"].strip(), 60)
    return titles, groups


def app_titles(home: Path) -> dict[str, str]:
    return app_meta(home)[0]


class Index:
    """会話の目録。`scan()` は変わった物だけを返す（中継へ送る差分）。"""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.items: dict[str, dict] = {}
        self._keys: dict[str, tuple] = {}
        self._hidden: set[str] = set()
        self._titles_key: tuple = ()
        self._titles: dict[str, str] = {}
        self._groups: dict[str, str] = {}

    def _titles_now(self) -> dict[str, str]:
        f = self.home.parent / "conversations.json"
        try:
            st = f.stat()
            key = (st.st_mtime, st.st_size)
        except OSError:
            key = ()
        if key != self._titles_key:
            self._titles_key = key
            self._titles, self._groups = app_meta(self.home) if key else ({}, {})
        return self._titles

    def _one(self, d: Path, titles: dict[str, str], now: float) -> dict:
        ev, lv = d / "events.jsonl", d / "live.ndjson"
        info = _from_events(ev) if ev.exists() else {}
        live = _from_live(lv, now) if lv.exists() else {}
        first = _first_user(d) or live.get("prompt") or info.get("prompt") or ""
        status = live.get("status") or _status_of(live.get("outcome") or info.get("outcome") or "")
        mt = max((p.stat().st_mtime for p in (d, ev, lv) if p.exists()), default=0.0)
        item = {
            "sid": d.name,
            "title": titles.get(d.name) or _clip(first, 80),
            "first": first,
            "last": live.get("last") or info.get("last") or "",
            "status": status,
            "outcome": live.get("outcome") or info.get("outcome") or "",
            "updated": int(mt),
            "source": live.get("source") or info.get("source") or "",
        }
        # どこの本体で走ったか（"app"・"gateway"）。同じ機械のアプリが、自分の走行でない物を実況で追う（2026-11-01）。
        via = live.get("via") or info.get("via") or ""
        if via:
            item["via"] = via
        if live.get("ask"):
            item["ask"] = live["ask"]
        secs = live.get("secs") if live.get("secs") is not None else info.get("secs")
        if secs is not None and status not in ("running", "asking"):
            item["secs"] = secs
        if self._quiet(status, secs, now):
            item["quiet"] = True
        if self._groups.get(d.name):
            item["group"] = self._groups[d.name]
        return item

    def _quiet(self, status: str, secs: float | None, now: float) -> bool:
        """この変わり目を、ほかの端末へ知らせないか。★判断が要るとき（asking）は機械の前に居なければ必ず知らせる。"""
        if status not in ("asking", "done", "failed"):
            return False
        try:
            if now - self.home.joinpath(*PRESENCE_FILE).stat().st_mtime < PRESENCE_SECS:
                return True
        except OSError:
            pass
        return status != "asking" and secs is not None and secs < NOTIFY_MIN_SECS

    def scan(self, now: float | None = None) -> tuple[list[dict], list[str]]:
        """（変わった・増えた会話, 消えた会話の sid）。最初の呼び出しは全部を返す。"""
        now = time.time() if now is None else now
        root = self.home / "sessions"
        try:
            dirs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            dirs = []
        keyed = []
        for d in dirs:
            try:
                mt = d.stat().st_mtime
                parts = []
                for n in ("events.jsonl", "live.ndjson", "messages.jsonl"):
                    try:
                        s = (d / n).stat()
                        parts.append((s.st_mtime, s.st_size))
                    except OSError:
                        parts.append(None)
            except OSError:
                continue
            last = max([mt] + [p[0] for p in parts if p])
            if now - last > MAX_AGE_SECS:
                continue
            keyed.append((last, d, (mt, *parts)))
        keyed.sort(key=lambda x: x[0], reverse=True)
        keyed = keyed[:MAX_ITEMS]
        titles = self._titles_now()
        changed, seen = [], set()
        for _last, d, key in keyed:
            sid = d.name
            seen.add(sid)
            title = titles.get(sid)
            k = (key, title, self._groups.get(sid))
            old = self.items.get(sid)
            # 走っている物は時間で状態が変わる（写しが古くなれば running でなくなる）ので毎回作り直す
            if old is not None and self._keys.get(sid) == k and old.get("status") not in ("running", "asking"):
                continue
            if sid in self._hidden and self._keys.get(sid) == k:
                continue
            item = self._one(d, titles, now)
            self._keys[sid] = k
            if item.get("source") == BATCH_SOURCE:      # 自動の走行は一覧に載せない
                self._hidden.add(sid)
                self.items.pop(sid, None)
                continue
            self._hidden.discard(sid)
            if item != old:
                self.items[sid] = item
                changed.append(item)
        removed = [sid for sid in list(self.items) if sid not in seen]
        for sid in removed:
            self.items.pop(sid, None)
            self._keys.pop(sid, None)
        self._hidden &= seen
        return changed, removed

    def all(self) -> list[dict]:
        return sorted(self.items.values(), key=lambda x: x.get("updated", 0), reverse=True)


def main(argv: list[str] | None = None) -> int:
    """`python -m laps.convindex [home]` — 目録を JSON で出す（確かめ用）。"""
    import sys
    a = argv if argv is not None else sys.argv[1:]
    home = Path(a[0]) if a else Path(_lp.home_dir())
    idx = Index(home)
    idx.scan()
    print(json.dumps(idx.all(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
