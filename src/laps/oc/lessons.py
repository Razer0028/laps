"""同じ失敗の知見を走行をまたいで貯める・Web で調べる一言（2026-10-20 オーナー「Web検索をして知見を貯める動作等入れたいです。
タスクのときに同じ失敗を繰り返してしまって詰まるときが結構多いので」）。

旧: 同じ型の失敗は 3 回ごとに「これまでの試み」を添えるだけ（evidence.repeat_note）で、Web 検索は 3 回目の一行
（_resource_reminder）に名前が出るだけだった。解決しても何も残らず、次の走行（別の依頼・別の作業フォルダ）でまた同じ失敗から始めた。
帳面（FINDINGS.md）は要約のときにしか書かれず、作業フォルダごと。

・2 回目の同じ失敗で、まだ Web を引いていなければ「この問いで WebSearch」を具体的な問い付きで言う（小さいモデルは
  「調べて」より「この語で調べて」に従う）。1 走行で同じ型に 1 回。
・同じ型で 2 回以上失敗した後、同じ道具が成功したら「効いた手」として知見の帳面に積む（その間に引いた Web の問い・読んだ URL も）。
  帳面は ~/.blackwall/lessons.jsonl（LAPS_HOME）＝作業フォルダをまたいで効く。値は redact を通してから書く。
・失敗したとき、帳面に同じ型（または十分近い型）の知見があれば、1 走行で 1 型 1 回だけ結果に添える。
推論機は呼ばない（時間を足さない）。LAPS_LESSONS=0 で全部止める。
"""
from __future__ import annotations

from .. import paths as _lp
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 帳面に残す件数の上限（超えたら古い物から外す）。
MAX_LESSONS = 400
#: 失敗のあと何手のうちに成功したら「効いた手」とみなすか。
FIX_WINDOW = 8
#: 1 走行で知見を添える回数の上限。
MAX_RECALL_PER_RUN = 4
#: 型どうしの近さ（語の重なり）。これ以上なら同じ失敗とみなす。
SIMILAR = 0.7

_lock = threading.Lock()
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.+\-]{1,}|[一-龯ぁ-んァ-ヶー]{2,}")
_NOISE = {"error", "the", "and", "for", "with", "from", "not", "this", "that", "line", "file", "exit", "code",
          "harness", "of", "in", "is", "to", "a"}


def enabled() -> bool:
    return (os.environ.get("LAPS_LESSONS") or "1").strip().lower() not in ("0", "off", "false", "no")


def store_path() -> Path:
    return Path(str(_lp.home_dir())) / "lessons.jsonl"


@dataclass
class RunState:
    """1 走行ぶんの覚え（OcAgent が 1 つ持つ）。"""
    seq: int = 0
    open: dict[str, dict] = field(default_factory=dict)       # 型 → {tool, pick, n, last, queries, urls}
    nudged: set[str] = field(default_factory=set)              # Web の一言を出した型
    recalled: set[str] = field(default_factory=set)            # 知見を添えた型（帳面の型）
    queries: list[tuple[int, str]] = field(default_factory=list)
    urls: list[tuple[int, str]] = field(default_factory=list)
    saved: int = 0


def _words(s: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(s or "") if w.lower() not in _NOISE}


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    ta, sa = a.split(":", 1)[0], a.split(":", 1)[-1]
    tb, sb = b.split(":", 1)[0], b.split(":", 1)[-1]
    if ta != tb:
        return False
    wa, wb = _words(sa), _words(sb)
    if len(wa) < 2 or len(wb) < 2:
        return False
    return len(wa & wb) / len(wa | wb) >= SIMILAR


def search_query(pick: str) -> str:
    """失敗の 1 行から、Web で引く問いを作る（パス・数字・長い 16 進などを外し、語だけを残す）。"""
    s = re.sub(r"[A-Za-z]:\\\S*|(?<![\w])/\S+|https?://\S+", " ", pick or "")
    s = re.sub(r"(?<![\w.])(?:0x[0-9a-fA-F]+|[0-9a-f]{8,}|\d+)(?![\w.])", " ", s)
    s = re.sub(r"^\s*(?:Error|error|ERROR)\s*[:：]\s*", "", s)
    s = re.sub(r"[\"'`«»<>()\[\]{}]", " ", s)
    s = " ".join(s.split())
    return s[:110].strip(" :,.-")


def _load() -> list[dict]:
    p = store_path()
    out: list[dict] = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("sig") and d.get("fix"):
                    out.append(d)
    except OSError:
        pass
    return out


def _redact(s: str) -> str:
    try:
        from ..redact import redact
        return redact(s).text
    except Exception:
        return s


def _save(entry: dict) -> None:
    p = store_path()
    with _lock:
        items = _load()
        for d in items:                                   # 同じ型・同じ手は数を足すだけ
            if d.get("sig") == entry["sig"] and d.get("fix") == entry["fix"]:
                d["count"] = int(d.get("count") or 1) + 1
                d["ts"] = entry["ts"]
                for k in ("queries", "urls"):
                    d[k] = list(dict.fromkeys(list(d.get(k) or []) + entry.get(k, [])))[-4:]
                break
        else:
            items.append(entry)
        items = items[-MAX_LESSONS:]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in items), encoding="utf-8")
        os.replace(tmp, p)


def lookup(sig: str) -> list[dict]:
    """帳面から同じ（近い）型の知見を、新しく・多く効いた順に返す。"""
    hits = [d for d in _load() if _similar(sig, str(d.get("sig")))]
    hits.sort(key=lambda d: (d.get("sig") == sig, int(d.get("count") or 1), float(d.get("ts") or 0)), reverse=True)
    return hits


def _recall_note(hits: list[dict]) -> str:
    rows = []
    for d in hits[:2]:
        r = f"- error «{d.get('error', '')[:160]}» → what worked: {d.get('fix', '')[:200]}"
        if d.get("count", 1) > 1:
            r += f" (worked {d['count']} times)"
        if d.get("queries"):
            r += "; searched: " + " | ".join(str(q)[:80] for q in d["queries"][:2])
        if d.get("urls"):
            r += "; read: " + " ".join(str(u)[:120] for u in d["urls"][:2])
        rows.append(r)
    return ("\n\n[Harness] An earlier run hit this same error and got past it (from the lessons file "
            f"{store_path()}):\n" + "\n".join(rows) + "\nTry that first if it fits here.")


#: 2 回目の同じ失敗で Web の検索を名指しする道具（外のプログラムの誤りを返す物）。★2026-10-23 オーナー「全ツールの導線とかも確認しておいて」:
#: 旧はどの道具の失敗にも出し、Blackwall の道具が自分で出す使い方の誤り（Check の「プロジェクトの中ではない。Bash で走らせて」・
#: Doc の拡張子）にまで「その文を Web で検索して」と添えていた＝ローカルのモデルを内部の文の検索へ送る。Tools 経由は call の名前で見る。
WEB_NUDGE_FROM = frozenset({"Bash", "BashOutput", "Lab", "Monitor", "Api"})


def observe(st: RunState, name: str, attempt: str, inp: dict, ok: bool, text: str, sig: str, pick: str,
            has_web: bool) -> str:
    """道具 1 回ぶん。結果に添える文（無ければ ""）を返す。sig・pick は evidence.failure_signature の値（成功なら ""）。"""
    if not enabled():
        return ""
    st.seq += 1
    if name == "WebSearch" and ok and isinstance(inp, dict) and (inp.get("query") or inp.get("q")):
        st.queries.append((st.seq, str(inp.get("query") or inp.get("q"))[:160]))
        return ""
    if name == "WebFetch" and ok and isinstance(inp, dict) and inp.get("url"):
        st.urls.append((st.seq, str(inp.get("url"))[:300]))
        return ""
    if ok:
        _maybe_learn(st, name, attempt, text, _head_of(name, inp, attempt))
        return ""
    o = st.open.setdefault(sig, {"tool": name, "pick": pick, "n": 0, "first": st.seq, "last": st.seq, "heads": set()})
    o["n"] += 1
    o["heads"].add(_head_of(name, inp, attempt))
    o["last"] = st.seq
    o["pick"] = pick or o["pick"]
    note = ""
    if len(st.recalled) < MAX_RECALL_PER_RUN:
        try:
            hits = [d for d in lookup(sig) if d.get("sig") not in st.recalled]
        except Exception:
            hits = []
        if hits:
            st.recalled.update(str(d.get("sig")) for d in hits[:2])
            st.recalled.add(sig)
            note += _recall_note(hits)
    eff = str(inp.get("call") or "") if name == "Tools" and isinstance(inp, dict) else name
    if o["n"] == 2 and has_web and eff in WEB_NUDGE_FROM and sig not in st.nudged \
            and not any(s >= o["first"] for s, _ in st.queries):
        q = search_query(pick)
        if len(_words(q)) >= 2:
            st.nudged.add(sig)
            note += (f"\n\n[Harness] Second failure of the same kind. Before trying another variation, search the web "
                     f"for the error: WebSearch(query=\"{q}\") (add the tool or library name and version if you know "
                     "them), then read the best result with WebFetch. What fixes it will be kept for later runs.")
    return note


def _head(attempt: str) -> str:
    """試みの「何をしたか」の頭。Bash は命令の最初の語（`ls` の成功を `pip install` の直し方として残さない）。"""
    s = attempt.strip("` ")
    if attempt.startswith("`"):
        for seg in reversed(re.split(r"&&|;|\|\|", s)):
            toks = [t for t in seg.split() if "=" not in t and t not in ("sudo", "env", "time")]
            if toks and toks[0] != "cd":
                return os.path.basename(toks[0]).lower()
        return ""
    return s.split(" ", 1)[0].lower()


def _still_failing(pick: str, text: str) -> bool:
    """成功で返ったのに、出力にまだ同じ誤りの 1 行が出ているか。★2026-10-29 Terminal-Bench の cron-broken-network（KAT）:
    `curl …; echo "EXIT:$?"` は最後の echo で 0 になり、出力に「DNS resolution error」が出たまま「効いた手」に積まれて、
    次の失敗に効かなかった手を勧めていた。"""
    p = " ".join((pick or "").split())[:80]
    return len(p) >= 6 and p in " ".join((text or "").split())


def _head_of(name: str, inp: dict, attempt: str) -> str:
    """Bash は命令の全文から頭を取る。★2026-10-29: 試みの文（evidence._attempt）は 120 文字で切ってあり、
    `curl …; echo "EXIT:$?"; curl …` の切れ目に残った curl を頭と読んで、echo で 0 になった試みを curl の直し方に積んでいた。"""
    if name == "Bash" and isinstance(inp, dict) and inp.get("command"):
        return _head("`" + " ".join(str(inp["command"]).split()) + "`")
    return _head(attempt)


def _maybe_learn(st: RunState, name: str, attempt: str, text: str = "", head: str | None = None) -> None:
    for sig, o in list(st.open.items()):
        if o["tool"] != name:
            continue
        if _still_failing(o.get("pick", ""), text):
            continue                                              # 同じ誤りが出ている＝直っていない（型も開いたまま）
        if (head if head is not None else _head(attempt)) not in o["heads"]:
            if st.seq - o["last"] > FIX_WINDOW:
                st.open.pop(sig, None)
            continue
        if o["n"] >= 2 and st.seq - o["last"] <= FIX_WINDOW:
            qs = [q for s, q in st.queries if s >= o["first"]][-3:]
            us = [u for s, u in st.urls if s >= o["first"]][-3:]
            entry = {"sig": sig, "tool": name, "error": _redact(o["pick"])[:240], "fix": _redact(attempt)[:300],
                     "queries": [_redact(q) for q in qs], "urls": us, "count": 1, "ts": time.time(),
                     "cwd": os.path.basename(os.getcwd())}
            try:
                _save(entry)
                st.saved += 1
            except Exception:
                pass
        st.open.pop(sig, None)
