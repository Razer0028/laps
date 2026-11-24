"""Construct — 手掛かりで思い出す記憶（2026-10-20 オーナー「導入してください。慎重に一つづつ正確に」「名前は Construct」）。

設計と根拠は /home/user/project/DESIGN-memory-20261020.md。要点:
  ・海馬は記憶の中身ではなく索引を持ち、手掛かりの一部から全体を思い出す（索引理論）。ここも本文は今の置き場
    （作業ログ・帳面・lessons・要約前の全文・会話）に残し、要旨 1 行と場所だけを持つ。
  ・選ぶのは「新しい順」ではなく、手掛かりの当たり × ACT-R の活性（新しさと使われた回数・Anderson & Schooler 1991）
    × 出どころ × フォルダ。弱い当たりは返さない（取りこぼす方に倒す＝文脈は長いほど精度が落ちる・LongMemEval）。
  ・出どころ（利用者・道具の出力・ハーネスの記録・モデルの推測・Web）と確かめの状態を必ず持つ（出どころの監視・自己汚染を防ぐ）。
推論機は呼ばない（時間を足さない）。置き場は ~/.blackwall/construct.db（LAPS_HOME）。LAPS_CONSTRUCT=0 で全部止める。

★2026-10-21 作り直し（オーナー「自動で添える機能自体がだめ」「モデル自身がまるで断片を記憶していて、そこから連想して必要な記憶を
  呼び出す」「呼び起こした記憶が本当に今必要なのか必要ないのかを現在の状況に結びつけて判断する」）: ハーネスは記憶を添えない。
  思い出すのはモデルが道具で行う。
  ・思い出す（query）: 断片だけを返す（何の・いつ・どこ・誰の記録か＋つながる語）。中身（確かめの命令・効いた手）は返さない。
  ・開く（open＋why）: 今の作業とどう関係するかを 1 文書かせてから中身を返す（思い出した物を今と照らす段＝前頭葉の確かめ）。
  ・つながる語と前後の出来事で、次の手掛かりをたどれる（連想）。
  ・重みはモデルの判断で変える: 断片で見せた（hits）・開いた（used）・開いた後に効いた／外れた（good・bad）。
  ハーネスに残すのは、利用者の訂正の止め（オーナー「1 は残して」）と、記録（取り込み・訂正・やり残し）と、呼ぶべき場面で
  呼んだかの数え（shadow_*: 当たる記憶があったかを数えるだけ・モデルには見せない）。

★全文検索の拡張（FTS5）は使わない: Windows の Python の SQLite の版に頼らないため。日本語の 2 文字の語（設定・検索）は
  3 文字ずつの全文検索では引けないので、候補は SQL の LIKE で集め、点数は Python で付ける（1 台の記憶は数万件まで）。
"""
from __future__ import annotations

from .. import paths as _lp
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 1 台に持つ記憶の上限（超えたら古い出来事から外す・利用者の訂正と未完の約束は残す）。
MAX_ITEMS = 20000
#: 候補を SQL で集める数（点数は Python で付ける）。
MAX_CANDIDATES = 400
#: 呼ぶべき場面の数え（shadow_start）で「当たる記憶があった」とみなす点数（0〜1）。
MIN_SCORE = 0.30
#: 断片 1 つの長さ（字）。中身ではなく「何の記憶か」が分かるだけ。
FRAGMENT_CHARS = 90
#: 開いた記憶の本文の上限（字）。
OPEN_CHARS = 1500

#: 出どころの重み（誰が言ったか・確かめたか）。モデルの推測は低く、外れた物はさらに低く。
SOURCE_WEIGHT = {"user": 1.0, "tool": 0.95, "harness": 0.9, "model": 0.8, "web": 0.5}
STATUS_WEIGHT = {"verified": 1.0, "": 0.9, "unverified": 0.7, "done": 0.6, "contradicted": 0.25}
#: 種類（kind）の既定の出どころ。
KINDS = ("episode", "fact", "dead_end", "lesson", "correction", "intention", "schema")

_lock = threading.Lock()


def enabled() -> bool:
    return (os.environ.get("LAPS_CONSTRUCT") or "1").strip().lower() not in ("0", "off", "false", "no")


def db_path() -> Path:
    return Path(str(_lp.home_dir())) / "construct.db"


def folder_key(path: str | os.PathLike | None) -> str:
    """作業フォルダの比べ方（大文字小文字と区切りを揃える）。全体の記憶は ""。"""
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except Exception:
        return str(path)


# ── 手掛かり ────────────────────────────────────────────────────────────────

_ASCII = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*(?:[/\\][A-Za-z0-9_.\-]+)*")
_VERSION = re.compile(r"(?<![\w.])v?\d+\.\d+(?:\.\d+)+(?![\w])")
_KATA = re.compile(r"[゠-ヿー]{2,}")
_KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿々]{2,}")
_STOP_ASCII = frozenset("""
a an the and or but for with from into onto this that these those there here what which who whom how why when where
is are was were be been being do does did done have has had can could should would will shall may might must
not no yes please thanks thank you your yours me my mine we our us it its they them their i he she him her his
to of in on at by as if then than so too very just also more most some any all each every other another such only
file files code run runs running make makes made use used using get got set put add new old one two three
error errors fail failed failure work works working task tasks done check checks test tests fix fixed
echo exit code line lines found matching cannot unable such directory command usr bin tmp none null true false
pip install uninstall npm npx yarn apt sudo python python3 node cargo build make cmake clone pull push cat grep
""".split())
_COMMON_ACRONYMS = frozenset("api url uri cpu gpu ram llm pdf csv json yaml xml http https html css sql ssh usb ui ux os id".split())
_STOP_JA = frozenset(["ください", "します", "しました", "している", "について", "それ", "これ", "あれ", "ここ", "そこ",
                      "とき", "ところ", "こと", "もの", "ため", "よう", "ファイル", "フォルダ", "作業", "確認", "対応",
                      "お願い", "以下", "以上", "場合", "必要", "可能", "今回", "前回", "全部", "一旦"])


@dataclass
class Cue:
    term: str          # 小文字（ASCII）か原文（日本語）
    weight: float      # 強さ（ファイル名・識別子が強い・2 文字の日本語は弱い）
    strong: bool       # 1 つだけでも当たりと見てよい手掛かりか


def cues(text: str, limit: int = 12) -> list[Cue]:
    """文から手掛かりの語を取り出す。ファイル名・識別子・カタカナ語・漢字の語。一般の語は外す。"""
    out: dict[str, Cue] = {}
    s = str(text or "")
    for m in _ASCII.finditer(s):
        tok = m.group(0).strip(".-_/\\")
        if len(tok) < 3:
            continue
        low = tok.lower()
        parts = [low]
        if "/" in low or "\\" in low:                         # パスは名前の部分も手掛かりに
            parts.append(re.split(r"[/\\]", low)[-1])
        for p in parts:
            if len(p) < 3 or p in _STOP_ASCII or p.isdigit():
                continue
            is_name = bool(re.search(r"[._/\\]", p)) or bool(re.search(r"[a-z][A-Z]|[0-9]", tok))
            # 大文字の略語（DNS・SNS など）は短くても手掛かりとして強い。どこにでも出る略語（API・GPU…）は除く。
            acronym = tok.isupper() and 3 <= len(tok) <= 6 and p not in _COMMON_ACRONYMS
            w = 1.0 if is_name else (0.8 if acronym else (0.7 if len(p) >= 5 else 0.5))
            if p not in out or out[p].weight < w:
                out[p] = Cue(p, w, is_name or acronym or len(p) >= 6)
    for m in _VERSION.finditer(s):                        # 版の番号（0.1.551・v2.3.8）は強い手掛かり
        v = m.group(0).lower().lstrip("v")
        if v not in out:
            out[v] = Cue(v, 1.0, True)
    for rx, base in ((_KATA, 0.8), (_KANJI, 0.7)):
        for m in rx.finditer(s):
            t = m.group(0)
            if t in _STOP_JA:
                continue
            w = base if len(t) >= 3 else base * 0.7
            if t not in out or out[t].weight < w:
                out[t] = Cue(t, w, len(t) >= 3)
            # 漢字が 5 文字以上続く語（複合語）は、そのままでは他の文に現れにくい＝3 文字ずつの弱い手掛かりも足す
            if rx is _KANJI and len(t) >= 5:
                for i in range(len(t) - 2):
                    piece = t[i:i + 3]
                    if piece not in out:
                        out[piece] = Cue(piece, 0.4, False)
    ranked = sorted(out.values(), key=lambda c: (-c.weight, -len(c.term)))
    return ranked[:limit]


# ── 置き場 ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE,
  kind TEXT NOT NULL,
  folder TEXT NOT NULL DEFAULT '',
  session TEXT NOT NULL DEFAULT '',
  run TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'harness',
  status TEXT NOT NULL DEFAULT '',
  gist TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  loc TEXT NOT NULL DEFAULT '',
  cues TEXT NOT NULL DEFAULT '',
  prev INTEGER,
  uses TEXT NOT NULL DEFAULT '[]',
  hits INTEGER NOT NULL DEFAULT 0,
  used INTEGER NOT NULL DEFAULT 0,
  good INTEGER NOT NULL DEFAULT 0,
  bad INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS items_folder_ts ON items(folder, ts);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def _connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=5)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    con.executescript(_SCHEMA)
    return con


def _redact(s: str) -> str:
    try:
        from ..redact import redact
        return redact(s).text
    except Exception:
        return s


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def item_key(kind: str, folder: str, gist: str) -> str:
    return hashlib.sha1(f"{kind}\x00{folder}\x00{_norm(gist)}".encode("utf-8")).hexdigest()


def _put(con: sqlite3.Connection, kind: str, gist: str, *, folder: str = "", body: str = "", loc: str = "",
         source: str = "harness", status: str = "", session: str = "", run: str = "", ts: float | None = None,
         prev: int | None = None, extra_cues: str = "", touch: bool = True) -> tuple[int | None, bool]:
    """1 件を置く（開いた接続の上で・commit はしない）。(id, 新しく足したか)。"""
    if kind not in KINDS or not str(gist or "").strip():
        return None, False
    gist = _clip(_redact(str(gist)), 240)
    body = _clip(_redact(str(body or "")), 2000)
    folder = folder_key(folder) if folder else ""
    now = float(ts if ts is not None else time.time())
    cue_text = " ".join(c.term for c in cues(f"{gist}\n{body}\n{extra_cues}", limit=20))
    text = _norm(f"{gist}\n{body}\n{cue_text}\n{loc}")
    key = item_key(kind, folder, gist)
    row = con.execute("SELECT id, uses FROM items WHERE key=?", (key,)).fetchone()
    if row and not touch:
        return int(row[0]), False
    if row:
        uses = _uses(row[1]) + [now]
        con.execute("UPDATE items SET ts=?, uses=?, body=CASE WHEN ?<>'' THEN ? ELSE body END, "
                    "loc=CASE WHEN ?<>'' THEN ? ELSE loc END, status=CASE WHEN ?<>'' THEN ? ELSE status END "
                    "WHERE id=?", (now, json.dumps(uses[-20:]), body, body, loc, loc, status, status, row[0]))
        return int(row[0]), False
    try:
        cur = con.execute(
            "INSERT INTO items(key,kind,folder,session,run,ts,source,status,gist,body,loc,cues,prev,uses,text) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, kind, folder, session, run, now, source, status, gist, body, loc, cue_text, prev,
             json.dumps([now]), text))
    except sqlite3.IntegrityError:                        # 別のプロセスが同じ物を先に置いた
        row = con.execute("SELECT id FROM items WHERE key=?", (key,)).fetchone()
        return (int(row[0]) if row else None), False
    return int(cur.lastrowid), True


def add(kind: str, gist: str, *, folder: str = "", body: str = "", loc: str = "", source: str = "harness",
        status: str = "", session: str = "", run: str = "", ts: float | None = None, prev: int | None = None,
        extra_cues: str = "", touch: bool = True) -> int | None:
    """1 件を置く。同じ種類・同じフォルダ・同じ要旨は 1 件にまとめる（時刻と使った時刻を足す）。置いた id（無効なら None）。
    要旨・本文は redact を通してから書く。touch=False（取り込み）は、既にあれば何も変えない（使った回数を増やさない）。"""
    if not enabled():
        return None
    with _lock:
        con = _connect()
        try:
            iid, inserted = _put(con, kind, gist, folder=folder, body=body, loc=loc, source=source, status=status,
                                 session=session, run=run, ts=ts, prev=prev, extra_cues=extra_cues, touch=touch)
            con.commit()
            if inserted:
                _prune(con)
            return iid
        finally:
            con.close()


def _prune(con: sqlite3.Connection) -> None:
    n = con.execute("SELECT count(*) FROM items").fetchone()[0]
    if n <= MAX_ITEMS:
        return
    # 価値の低い物から外す: 一度も使われていない → 外れが多い → 古い（利用者の訂正とやり残しは外さない）
    con.execute("DELETE FROM items WHERE id IN (SELECT id FROM items WHERE kind NOT IN ('correction','intention') "
                "ORDER BY (used > 0) ASC, (good - bad) ASC, ts ASC LIMIT ?)", (n - MAX_ITEMS,))
    con.commit()


def _uses(raw: str) -> list[float]:
    try:
        v = json.loads(raw or "[]")
        return [float(x) for x in v if isinstance(x, (int, float))]
    except (ValueError, TypeError):
        return []


# ── 点数 ────────────────────────────────────────────────────────────────────

def activation(uses: list[float], now: float | None = None, d: float = 0.5) -> float:
    """ACT-R の基底活性 B = ln Σ t_j^(−d)（t_j は j 回目に使ってからの時間・時間単位・d=0.5）を 0〜1 に写す。
    1 時間前に 1 回 ≈ 0.92、1 日前 ≈ 0.71、1 週間前 ≈ 0.48、30 日前 ≈ 0.31、1 年前 ≈ 0.12。"""
    now = time.time() if now is None else now
    total = sum(max((now - u) / 3600.0, 1.0 / 60) ** (-d) for u in uses if u <= now + 60)
    if total <= 0:
        return 0.0
    b = math.log(total)
    return 1.0 / (1.0 + math.exp(-(b + 2.5)))


@dataclass
class Hit:
    id: int
    kind: str
    folder: str
    session: str
    ts: float
    source: str
    status: str
    gist: str
    loc: str
    score: float
    matched: list[str] = field(default_factory=list)


def search(text: str = "", *, folder: str = "", kinds: tuple[str, ...] | None = None, limit: int = 3,
           exclude_sessions: tuple[str, ...] = (), exclude_ids: tuple[int, ...] = (), min_score: float = MIN_SCORE,
           cue_list: list[Cue] | None = None, now: float | None = None, other_folders: bool = True) -> list[Hit]:
    """手掛かりで引く。点数＝手掛かりの当たり × 活性 × 出どころ × フォルダ。弱い当たり（min_score 未満）は返さない。"""
    if not enabled():
        return []
    cl = cue_list if cue_list is not None else cues(text)
    if not cl:
        return []
    fk = folder_key(folder) if folder else ""
    now = time.time() if now is None else now
    where = ["(" + " OR ".join("text LIKE ?" for _ in cl) + ")", "NOT (kind='intention' AND status='done')"]
    args: list = [f"%{c.term.lower()}%" for c in cl]
    if kinds:
        where.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
        args += list(kinds)
    if exclude_sessions:
        where.append("session NOT IN (" + ",".join("?" for _ in exclude_sessions) + ")")
        args += list(exclude_sessions)
    if not other_folders:
        where.append("folder IN (?, '')")
        args.append(fk)
    sql = ("SELECT id,kind,folder,session,ts,source,status,gist,loc,uses,text,hits,used,good,bad FROM items WHERE "
           + " AND ".join(where) + " ORDER BY ts DESC LIMIT ?")
    args.append(MAX_CANDIDATES)
    with _lock:
        try:
            con = _connect()
        except sqlite3.Error:
            return []
        try:
            rows = con.execute(sql, args).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
    # 分母は重い順に 5 つの手掛かりの重さ（長い依頼で、関係のある記憶まで点数が薄まらないように）
    total_w = sum(sorted((c.weight for c in cl), reverse=True)[:5]) or 1.0
    hits: list[Hit] = []
    for (iid, kind, ifolder, session, ts, source, status, gist, loc, uses, itext, n_hits, n_used, n_good, n_bad) in rows:
        if iid in exclude_ids:
            continue
        matched = [c for c in cl if c.term.lower() in itext]
        if not matched:
            continue
        glow = _norm(gist)
        in_gist = [c for c in matched if c.term.lower() in glow]
        # 当たりの強さ: 1 つだけなら、強い手掛かりが要旨に当たったときだけ（長い本文にたまたま出た語で引かない）。
        if len(matched) == 1 and not (matched[0].strong and in_gist):
            continue
        # 要旨での当たりは満点、本文だけの当たりは半分（2026-10-20 実物で試して: 作業の記録の答えの中に出た「MCP」だけで
        #   関係の薄い作業が 0.77 で並んだ）。
        cov = sum(c.weight * (1.0 if c in in_gist else 0.4) for c in matched) / total_w
        cov = min(1.0, cov + 0.15 * max(0, len(in_gist) - 1))     # 複数の当たりのおまけは要旨での当たりだけ
        act = activation(_uses(uses), now)
        w_src = SOURCE_WEIGHT.get(source, 0.7) * STATUS_WEIGHT.get(status, 0.8)
        if kind == "correction":
            w_src = max(w_src, 1.0)
        w_fold = 1.0 if (ifolder == fk and fk) else (0.85 if not ifolder else 0.5)
        score = cov * (0.5 + 0.5 * act) * w_src * w_fold * utility(n_hits, n_used, n_good, n_bad)
        if score < min_score:
            continue
        hits.append(Hit(int(iid), kind, ifolder, session, float(ts), source, status, gist, loc, round(score, 3),
                        [c.term for c in matched]))
    hits.sort(key=lambda h: h.score, reverse=True)
    out: list[Hit] = []
    seen: set[str] = set()
    for h in hits:
        k = _norm(h.gist)[:60]                        # 頭がほぼ同じ物（要約のたびの言い換え）は 1 つだけ
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def note_hits(ids: list[int]) -> None:
    """断片として見せた回数を数える（段 2 の効き目の数えの元）。"""
    if not ids or not enabled():
        return
    with _lock:
        try:
            con = _connect()
            try:
                con.executemany("UPDATE items SET hits=hits+1 WHERE id=?", [(i,) for i in ids])
                con.commit()
            finally:
                con.close()
        except sqlite3.Error:
            pass


def utility(hits: int, used: int, good: int, bad: int) -> float:
    """思い出した結果から決める重み（段 2・再固定化と忘却）。効いた・外れたの比（ラプラスの補正つき）で 0.75〜1.25、
    断片で何度見せてもモデルが一度も開かない記憶は 0.7 倍（雑音として下げる・Richards & Frankland 2017 の忘却は機能）。"""
    u = (int(good or 0) + 1) / (int(good or 0) + int(bad or 0) + 2)          # 0〜1・何も無ければ 0.5
    w = 0.75 + 0.5 * u
    if int(hits or 0) >= 5 and int(used or 0) == 0:
        w *= 0.7
    return w


# ── 見せる形（断片）────────────────────────────────────────────────────────────

_KIND_LABEL = {"episode": "earlier work", "fact": "finding", "dead_end": "dead end", "lesson": "fix that worked",
               "correction": "user correction", "intention": "left to do", "schema": "project summary"}
_SOURCE_LABEL = {"user": "said by the user", "tool": "from tool output", "harness": "recorded by the harness",
                 "model": "your own guess, unverified", "web": "from the web, unverified"}


def _where(h: Hit, folder: str) -> str:
    fk = folder_key(folder) if folder else ""
    if not h.folder:
        return "any folder"
    if h.folder == fk:
        return "this folder"
    return f"another folder ({os.path.basename(h.folder) or h.folder})"


def _src(source: str, status: str) -> str:
    src = _SOURCE_LABEL.get(source, source)
    if source == "model" and status != "unverified":
        src = "your own note" + (", marked as checked" if status == "verified" else "")
    return src + (" · later found wrong" if status == "contradicted" else "")


def fragment(kind: str, gist: str) -> str:
    """断片（何の記憶か）。中身は入れない: 前の作業は依頼と結果だけ（書き換えたファイル・確かめの命令は外す）、効いた直し方は
    失敗の型だけ（効いた手は外す）。ほかは頭の FRAGMENT_CHARS 字。"""
    g = str(gist or "")
    if kind == "episode":
        g = re.split(r"・(?:書き換え|確かめ):", g)[0]
    elif kind == "lesson":
        g = re.split(r"\s*→\s*効いた手", g)[0]
    return _clip(g, FRAGMENT_CHARS)


def _link_terms(item_cues: str, shown: str, exclude: set[str], limit: int = 4) -> list[str]:
    """つながる語: その記憶の手掛かりのうち、引いた語にも見せた文にも無い物（次に引く語の候補）。短い弱い語は除く。"""
    low = shown.lower()
    out: list[str] = []
    for t in str(item_cues or "").split():
        t = t.lower()
        if t in exclude or t in low or t in out:
            continue
        if not (len(t) >= 4 or re.search(r"[._/\\0-9]", t)):
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


_REQ_IN_GIST = re.compile(r"^依頼「(.*?)」")


def _shown(gist: str, ctx_norm: str) -> bool:
    """その記憶の頭の文が、もう文脈に出ているか（作業ログ・帳面の塊）。呼ぶべき場面の数えで、もう見えている物を数えない。"""
    core = gist
    m = _REQ_IN_GIST.match(core)
    if m:
        core = m.group(1)
    core = _norm(core)[:40]
    return len(core) >= 8 and core in ctx_norm


# ── 呼ぶべき場面の数え（モデルには見せない）───────────────────────────────────────
# ★2026-10-21 オーナー了承（「2 はそれでいいです」）: まずはモデル任せにして、呼んだ回数と、呼ぶべき場面で呼んだかを数える。
#   旧（自動で添える形）が添えていた場面で、当たる記憶があったかだけを数える。見せた回数（hits）も数えない（見せていない）。

#: 利用者が前の作業を指す言い方（前回・この前・前と同じ…）。呼ぶべき場面の印。
_PAST_REF = re.compile(r"前回|この前|前に|以前|前と同じ|前みたいに|前のやり方|あの時|あのとき|前の会話|先週|昨日の|覚えて|思い出|"
                       r"(?<![a-z])(?:last time|earlier|previously|again|remember)(?![a-z])", re.I)


def refers_to_past(text: str) -> bool:
    return bool(_PAST_REF.search(str(text or "")))


#: 依頼の始めに自動で添える記憶の点数の下限（Construct の道具の 0.15・search の 0.30 より高い＝強く当たる物だけ）。
START_SCORE = 0.45


def auto_enabled() -> bool:
    """依頼の始めに当たる記憶を添えるか（2026-11-20 オーナー「任せます」・外部の評価で「記憶を引かない」が最優先の指摘）。
    ★モデルが自分から引いたのは 233 本の走行の 8%（25 回）だけだったので、強く当たる物だけを断片で見せる。"""
    return enabled() and os.environ.get("LAPS_CONSTRUCT_AUTO", "1").strip().lower() not in ("0", "off", "false", "no")


def start_block(request: str, folder: str, *, context: list[str] | tuple[str, ...] = (), session: str = "",
                limit: int = 2) -> tuple[str, list[int]]:
    """依頼の手掛かりで強く当たる記憶（まだ文脈に出ていない物）を、断片で最大 limit 件。中身は出さない（開くのはモデル）。
    返り値は（依頼に添える文、見せた id）。当たらなければ（"", []）。"""
    if not auto_enabled() or not str(request or "").strip():
        return "", []
    hits = search(request, folder=folder, limit=limit * 4, min_score=START_SCORE,
                  exclude_sessions=(session,) if session else ())
    ctx_norm = _norm("\n".join(str(x) for x in context))
    hits = [h for h in hits if not _shown(h.gist, ctx_norm)][:limit]
    if not hits:
        return "", []
    note_hits([h.id for h in hits])
    lines = ["[Memory] Possibly related from earlier work (fragments only; they may be out of date). If one matters "
             "for this request, read it with Construct(open=<id>, why=\"...\") before relying on it; otherwise ignore them."]
    for h in hits:
        day = time.strftime("%Y-%m-%d", time.localtime(h.ts))
        lines.append(f"#{h.id} [{day} · {_KIND_LABEL.get(h.kind, h.kind)} · {_where(h, folder)} · "
                     f"{_src(h.source, h.status)}] {fragment(h.kind, h.gist)}")
    return "\n".join(lines), [h.id for h in hits]


def shadow_start(request: str, folder: str, *, context: list[str] | tuple[str, ...] = (), session: str = "",
                 limit: int = 3) -> list[int]:
    """依頼の手掛かりで当たる記憶（まだ文脈に出ていない物）の id。見せない・数えない。"""
    if not enabled() or not str(request or "").strip():
        return []
    hits = search(request, folder=folder, limit=limit * 4, exclude_sessions=(session,) if session else ())
    ctx_norm = _norm("\n".join(str(x) for x in context))
    return [h.id for h in hits if not _shown(h.gist, ctx_norm)][:limit]


# ── 利用者の訂正（5/7）─────────────────────────────────────────────────────────

#: 訂正・止める指示の言い方（予想と違ったことほど強く覚える・Lisman & Grace 2005／同じ注意を何度も受けないように）。
_CORRECTION = re.compile(
    r"違う|ちがう|違います|そうじゃな|そうではな|間違|誤っ|誤り|やめて|止めて|しないで|しなくてい|しなくて良|しなくてよ|"
    r"するな|しては(?:いけ|だめ|ダメ)|ダメ|だめ|勝手に|余計な|要らない|いらない|ではなく|じゃなくて|でなく|"
    r"元に戻|戻して|取り消|やり直して|そっちじゃ|それじゃな|禁止|"
    # 「付けないで・使わないで・触らないで…（ください）」「〜ないように」（2026-10-21 実機の試しの準備で気づいた: 「しないで」しか
    #   拾っておらず、いちばんよくある頼み方を落としていた）。「わからないので」のような理由の「ないので」は拾わない。
    r"ないで(?:ください|下さい|くれ|ね|ほしい|欲しい|$|[。、！!．.])|ないように|"
    r"(?<![a-z])(?:not that|don'?t|do not|stop|wrong|instead of|undo|revert)(?![a-z])", re.I)


def is_correction(text: str) -> bool:
    t = str(text or "").strip()
    return len(t) >= 4 and bool(_CORRECTION.search(t))


def touch(iid: int, now: float | None = None) -> None:
    """使った時刻を足す（ACT-R の活性が上がる）。"""
    if not enabled():
        return
    now = time.time() if now is None else now
    with _lock:
        try:
            con = _connect()
            try:
                row = con.execute("SELECT uses FROM items WHERE id=?", (iid,)).fetchone()
                if row:
                    con.execute("UPDATE items SET uses=? WHERE id=?", (json.dumps((_uses(row[0]) + [now])[-20:]), iid))
                    con.commit()
            finally:
                con.close()
        except sqlite3.Error:
            pass


def note_correction(text: str, *, folder: str, request: str = "", action: str = "", session: str = "",
                    now: float | None = None) -> tuple[int | None, bool]:
    """利用者の訂正を覚える（出どころは利用者・いちばん強い重み）。何をしていた時の訂正か（元の依頼・直前の道具）も本文に残し、
    似た依頼や操作のときに手掛かりで出るようにする。(id, 前にも似た訂正を受けていたか)。前にもあれば、その記憶も強める。"""
    if not enabled() or not is_correction(text):
        return None, False
    gist = _clip(text, 240)
    prior = search(gist, folder=folder, kinds=("correction",), limit=1, min_score=0.55, now=now)
    body = (f"訂正: {_clip(text, 600)}\n元の依頼: {_clip(request, 300)}\n"
            f"直前にしていたこと: {_clip(action, 240) or '（なし）'}")
    iid = add("correction", gist, folder=folder, body=body, source="user", session=session, ts=now)
    if prior and prior[0].id != iid:
        touch(prior[0].id, now)
    return iid, bool(prior)


# ── 会話をまたぐやり残し（6/7・展望記憶）─────────────────────────────────────────

INTENTION_HEAD = "やり残し: "


def note_intentions(open_items: list[str], done_items: list[str], *, folder: str, request: str = "",
                    session: str = "") -> tuple[int, int]:
    """走行の終わりの ToDo を覚える。残った項目は「やり残し」（出どころはモデルの予定）として足し、済んだ項目は前の
    やり残しを「済み」にする（思い出しの候補から外れる）。一度済んだ項目がまた残っていたら「済み」を外す。(足した数, 済みにした数)。
    手掛かり（依頼の語・ファイル名）が当たったときにだけ出る＝毎回並べない（McDaniel & Einstein の手掛かりで思い出す道）。"""
    if not enabled():
        return 0, 0
    fk = folder_key(folder)
    added = closed = 0
    with _lock:
        con = _connect()
        try:
            for t in open_items:
                t = _clip(t, 200)
                if len(t) < 4:
                    continue
                gist = INTENTION_HEAD + t
                iid, inserted = _put(con, "intention", gist, folder=fk, body=f"元の依頼: {_clip(request, 300)}",
                                     source="model", session=session, touch=False)
                if inserted:
                    added += 1
                elif iid is not None:
                    con.execute("UPDATE items SET status='' WHERE id=? AND status='done'", (iid,))
            for t in done_items:
                t = _clip(t, 200)
                if len(t) < 4:
                    continue
                cur = con.execute("UPDATE items SET status='done' WHERE key=? AND status<>'done'",
                                  (item_key("intention", fk, INTENTION_HEAD + t),))
                closed += cur.rowcount or 0
            con.commit()
        finally:
            con.close()
    return added, closed


# ── 動いている途中（7/7）: 呼ぶべき場面の数えと、前に訂正された操作の止め ─────────────────────

_MARKUP = re.compile(r"<(\w+)>.*?</\1>", re.S)


def call_text(name: str, inp: dict) -> str:
    """道具の呼び出しから、手掛かりを取る文（命令・パス・問い）。"""
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return str(inp.get("command") or "")
    parts = [str(inp.get(k) or "") for k in ("file_path", "path", "notebook_path", "pattern", "url", "query", "q")]
    # 書き込みは何を書くかも手掛かり（ファイル名だけでは「何を書き換えるか」が分からない）。頭の 200 字だけ。
    for k in ("content", "new_string", "old_string"):
        if inp.get(k):
            parts.append(str(inp.get(k))[:200])
    for e in (inp.get("edits") or [])[:3] if isinstance(inp.get("edits"), list) else []:
        if isinstance(e, dict):
            parts.append(str(e.get("new_string") or "")[:200])
    return " ".join(p for p in parts if p)


def shadow_failure(name: str, inp: dict, error: str, *, folder: str, session: str = "") -> list[int]:
    """道具が失敗した時に当たる記憶（行き止まり・事実・前の作業・訂正）の id。呼ぶべき場面の数えだけ・見せない。"""
    # ハーネスが付けた印（<shell_metadata>…・[Host] の行）は手掛かりにしない。エラーは頭の 3 行だけ（2026-10-21 試験で発見:
    #   印の語が強い手掛かりになり、分母が膨らんで本当の手掛かり spconv-cu120 の点数が 0.175 に薄まった）。
    err = _MARKUP.sub(" ", str(error or ""))
    head = [ln.strip() for ln in err.splitlines() if ln.strip() and not ln.strip().startswith(("[Host]", "[Harness]"))][:3]
    text = f"{call_text(name, inp).splitlines()[0] if call_text(name, inp) else ''}\n" + "\n".join(head)[:400]
    hits = search(text, folder=folder, kinds=("dead_end", "fact", "episode", "correction"), limit=2, min_score=0.35,
                  exclude_sessions=(session,) if session else ())
    return [h.id for h in hits]


def shadow_file(path: str, *, folder: str, session: str = "") -> list[int]:
    """その走行で初めてファイルに触った時に、そのファイル名で当たるこのフォルダの記憶の id。数えだけ・見せない。"""
    try:
        rel = os.path.relpath(path, folder) if os.path.isabs(path) else path
    except ValueError:
        rel = path
    base = os.path.basename(rel)
    if not base or "." not in base:                        # 拡張子の無い名前は手掛かりとして弱い（README などは除く）
        return []
    cl = [Cue(base.lower(), 1.0, True)] + ([Cue(rel.lower().replace("\\", "/"), 1.0, True)] if rel != base else [])
    hits = search("", folder=folder, kinds=("dead_end", "fact", "correction", "intention", "episode"), limit=2,
                  min_score=0.45, other_folders=False, cue_list=cl, exclude_sessions=(session,) if session else ())
    return [h.id for h in hits]


def correction_for(name: str, inp: dict, *, folder: str, session: str = "") -> Hit | None:
    """これからする呼び出しが、前に利用者に訂正された操作に当たるか（訂正の手掛かりが 2 つ以上、呼び出しの中に在る）。
    当たれば、その訂正（いちばん強い物）。★いまの会話の訂正は除く（2026-10-21 の試し: 「型ヒントを付けないで」と言われた直後に、
    その決まりを作業ログのメモに書く Edit を止めた＝言われたことを守る操作まで止めていた。いまの会話の訂正は文脈に見えている）。"""
    text = call_text(name, inp)
    if not text.strip():
        return None
    hits = search(text, folder=folder, kinds=("correction",), limit=1, min_score=0.5,
                  exclude_sessions=(session,) if session else ())
    return hits[0] if hits and len(hits[0].matched) >= 2 else None


def correction_gate_message(h: Hit) -> str:
    day = time.strftime("%Y-%m-%d", time.localtime(h.ts))
    return (f"[Harness] Not run. The user corrected this kind of action before ({day}): \"{h.gist}\". If this call is "
            "still right for the task, write one line saying why it is different this time, then call it again. "
            "If not, do it another way.")


# ── 段 2: 思い出した結果で書き直す（再固定化）─────────────────────────────────────

#: 結果で書き直す種類（利用者の訂正とやり残しは、効いた・外れたで書き換えない）。
FEEDBACK_KINDS = ("episode", "fact", "dead_end", "lesson", "schema")


def cue_terms(text: str) -> set[str]:
    return {c.term.lower() for c in cues(text, limit=20)}


def novel_terms(item_cues: str, gist: str, trigger: set[str]) -> list[str]:
    """その記憶にしかない語（思い出させた手掛かりを除いた、記憶が運んだ新しい情報）。短い弱い語は偶然の一致が多いので除く。"""
    terms = {t for t in str(item_cues or "").lower().split() if t}
    terms |= {c.term.lower() for c in cues(gist, limit=20) if c.strong}
    return sorted(t for t in terms if t not in trigger and (len(t) >= 4 or re.search(r"[._/\\0-9]", t)))


def feedback(entries: list[tuple[int, set[str], str, str]], run_ok: bool, now: float | None = None) -> dict[str, int]:
    """走行の終わりに、モデルが開いた記憶ごとに「当てはめたか」と「その後どうなったか」を書き戻す（再固定化）。
    entries: (id, 思い出させた手掛かりの語, 開いた後の命令・書いた中身・答え, 開いた後に失敗した命令)。
    開いた回数（used）は開いた時に数えてある（open_memory）。当てはめた＝記憶にしかない語が after_text に現れた（開いて読んだが
    使わなかった記憶は、効いた・外れたのどちらにも数えない）。外れた＝その語が失敗した命令に在った（予測の外れ）。効いた＝当てはめて、
    その語で失敗せず、走行がうまく終わった。外れが 2 回以上で効いたより多ければ「外れた」の印（点数 ×0.25）。効いたが上回れば外す。
    数を返す（applied・good・bad・contradicted・restored）。"""
    out = {"applied": 0, "good": 0, "bad": 0, "contradicted": 0, "restored": 0}
    if not enabled() or not entries:
        return out
    with _lock:
        con = _connect()
        try:
            for iid, trigger, after_text, failed_text in entries:
                after = str(after_text or "").lower()
                failed = str(failed_text or "").lower()
                row = con.execute("SELECT kind, cues, gist, good, bad, status FROM items WHERE id=?", (iid,)).fetchone()
                if not row:
                    continue
                kind, icues, gist, good, bad, status = row
                novel = novel_terms(icues, gist, {t.lower() for t in trigger})
                if not novel or not any(t in after for t in novel):
                    continue
                out["applied"] += 1
                hit_fail = any(t in failed for t in novel)
                good, bad = int(good or 0), int(bad or 0)
                if kind in FEEDBACK_KINDS:
                    if hit_fail:
                        bad += 1
                        out["bad"] += 1
                    elif run_ok:
                        good += 1
                        out["good"] += 1
                new_status = status
                if kind in FEEDBACK_KINDS:
                    if bad >= 2 and bad > good and status != "contradicted":
                        new_status = "contradicted"
                        out["contradicted"] += 1
                    elif status == "contradicted" and good > bad:
                        new_status = ""
                        out["restored"] += 1
                con.execute("UPDATE items SET good=?, bad=?, status=? WHERE id=?", (good, bad, new_status, iid))
            con.commit()
        finally:
            con.close()
    return out


# ── 道具 Construct（エージェントが自分で引く）──────────────────────────────────

#: いまの走行の会話とフォルダ（道具が自分の会話を引かないように・loop が走行の始めに入れる）。
_current = {"session": "", "folder": ""}
CONV_SECONDS = 2.0
CONV_SESSIONS = 400


def set_current(session: str, folder: str) -> None:
    _current["session"] = str(session or "")
    _current["folder"] = str(folder or "")


def _sessions_root() -> Path:
    return Path(str(_lp.home_dir())) / "sessions"


def _conv_date(sid: str) -> str:
    m = re.match(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})", sid)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}" if m else sid


def search_conversations(query: str, *, limit: int = 4, exclude: str = "", seconds: float = CONV_SECONDS,
                         max_sessions: int = CONV_SESSIONS) -> list[dict]:
    """過去の会話（~/.blackwall/sessions/*/messages.jsonl）を新しい順に探す。1 会話 1 件（いちばん当たった発言）。
    時間と会話の数に上限がある（全部は探さない）。当たりの決め方は search と同じ（2 つ以上か、強い手掛かり 1 つ）。"""
    cl = cues(query)
    if not cl:
        return []
    root = _sessions_root()
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        return []
    try:
        from .base import _user_part
    except Exception:                                         # pragma: no cover
        def _user_part(x):
            return x
    t0 = time.monotonic()
    out: list[dict] = []
    for n, d in enumerate(dirs):
        if n >= max_sessions or time.monotonic() - t0 > seconds or len(out) >= limit:
            break
        if exclude and d.name == exclude:
            continue
        best = None
        try:
            with open(d / "messages.jsonl", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    role = m.get("role")
                    if role not in ("user", "assistant") or m.get("bw_note"):   # bw_note＝新しい中核の添え書き（作業ログなど）
                        continue
                    text = m.get("content")
                    if not isinstance(text, str) or not text.strip() or text.startswith("[Harness]"):
                        continue
                    if role == "user":
                        text = _user_part(text)
                    low = text.lower()
                    matched = [c for c in cl if c.term.lower() in low]
                    if not matched or (len(matched) == 1 and not matched[0].strong):
                        continue
                    sc = sum(c.weight for c in matched)
                    if best is None or sc > best[0]:
                        i = low.find(matched[0].term.lower())
                        snip = text[max(0, i - 70): i + 110]
                        best = (sc, role, snip, [c.term for c in matched])
        except OSError:
            continue
        if best:
            out.append({"session": d.name, "date": _conv_date(d.name), "role": best[1],
                        "snippet": _clip(_redact(best[2]), 200), "matched": best[3],
                        "file": str(d / "messages.jsonl")})
    return out


DESCRIPTION = (
    "Recall your own memory of earlier work on this machine: past tasks in this folder and others, findings, dead "
    "ends, fixes that worked, the user's corrections, things left to do, and past conversations. Memories do not come "
    "to you by themselves: recall when the user refers to earlier work, when a file, error or task looks like "
    "something you may have done before, before investigating something again, or when a fix fails. Give the "
    "distinctive words of the moment (file names, identifiers, version numbers, error text, names). A recall returns "
    "fragments only (what, when, where, who recorded it). Decide from the current task whether one is needed, and "
    "open it with open=<id> and why=<how it relates to what you are doing now> to read it. A fragment's links are "
    "words to recall with next. If nothing matches, say you do not remember instead of guessing.")
SCHEMA_PROPS = {
    "query": {"type": "string", "description": "Distinctive words to recall with (file names, identifiers, versions, "
              "error text, names)"},
    "open": {"type": "integer", "description": "The id (#N) of one fragment to read in full"},
    "why": {"type": "string", "description": "With open: one sentence on how this memory relates to what you are "
            "doing now"},
    "scope": {"type": "string", "enum": ["all", "folder"],
              "description": "all (default): every folder, this folder first; folder: only this folder"},
    "conversations": {"type": "boolean", "description": "Also search the text of past conversations (default true)"},
}
#: 前置きに入れる節（道具 Construct を渡すときだけ・oc/prompt）。思い出すのは自分・決めるのも自分、を先に言う。
PROMPT_NOTE = (
    "# Your memory\n"
    "You remember earlier work on this machine through the Construct tool: past tasks, findings, dead ends, fixes "
    "that worked, the user's corrections and things left to do. Memories do not come to you by themselves; you recall "
    "them when you need them.\n"
    "- Recall when the user refers to earlier work, when a file, error or task looks like something you may have done "
    "before, before investigating something again, and when a fix fails. Recall with the distinctive words of the "
    "moment: file names, identifiers, error text.\n"
    "- A recall gives fragments. Decide from the current situation whether one is needed, and open only that one with "
    "Construct(open=<id>, why=<how it relates to now>). A fragment's links are words you can recall with next.\n"
    "- A memory describes that time, not now. Check it against the present before relying on it, and set it aside "
    "when it does not fit; the current request, files and tool results come first.\n"
    "- If nothing comes back, you do not remember it: say so instead of guessing.")
#: 開く時の why の最短（「関係ある」だけの空の理由を通さない）。
WHY_MIN = 8


def _neighbors(con: sqlite3.Connection, ids: list[int]) -> dict[int, tuple[str, int | None, int | None]]:
    """id → (手掛かりの語, 前の出来事, 次の出来事)。"""
    if not ids:
        return {}
    qs = ",".join("?" for _ in ids)
    out: dict[int, tuple[str, int | None, int | None]] = {}
    for iid, icues, prev in con.execute(f"SELECT id, cues, prev FROM items WHERE id IN ({qs})", ids):
        out[int(iid)] = (str(icues or ""), int(prev) if prev is not None else None, None)
    for prev, iid in con.execute(f"SELECT prev, id FROM items WHERE prev IN ({qs}) ORDER BY ts", ids):
        k = int(prev)
        if k in out and out[k][2] is None:
            out[k] = (out[k][0], out[k][1], int(iid))
    return out


def _links_line(icues: str, shown: str, exclude: set[str], prev: int | None, nxt: int | None) -> str:
    bits = []
    lt = _link_terms(icues, shown, exclude)
    if lt:
        bits.append("links: " + ", ".join(lt))
    if prev is not None:
        bits.append(f"before #{prev}")
    if nxt is not None:
        bits.append(f"after #{nxt}")
    return (" · " + " · ".join(bits)) if bits else ""


def _as_id(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    m = re.search(r"\d+", str(v or ""))
    return int(m.group(0)) if m else None


def open_memory(iid: int, why: str, *, folder: str = "", now: float | None = None) -> str:
    """モデルが選んだ記憶を 1 つ開く。why（今の作業との関係）が無ければ開かない。開いた回数（used）と使った時刻を足す
    （思い出すほど定着する・テスト効果）。"""
    why = " ".join(str(why or "").split())
    if len(why) < WHY_MIN:
        return ("Error: say in why how this memory relates to what you are doing now (one sentence), then open it. "
                "If it does not relate, do not open it.")
    now = time.time() if now is None else now
    with _lock:
        con = _connect()
        try:
            row = con.execute("SELECT kind, folder, session, ts, source, status, gist, body, loc, uses FROM items "
                              "WHERE id=?", (iid,)).fetchone()
            if not row:
                return f"Error: there is no memory #{iid}. Use an id from a Construct result."
            kind, ifolder, session, ts, source, status, gist, body, loc, uses = row
            con.execute("UPDATE items SET used=used+1, uses=? WHERE id=?",
                        (json.dumps((_uses(uses) + [now])[-20:]), iid))
            nb = _neighbors(con, [iid]).get(iid, ("", None, None))
            con.commit()
        finally:
            con.close()
    h = Hit(int(iid), kind, ifolder, session, float(ts), source, status, gist, loc, 0.0)
    day = time.strftime("%Y-%m-%d", time.localtime(h.ts))
    text = [f"Memory #{iid} [{day} · {_KIND_LABEL.get(kind, kind)} · {_where(h, folder)} · {_src(source, status)}]",
            gist]
    b = str(body or "").strip()
    if b and _norm(b) != _norm(gist):
        text.append(_clip(b, OPEN_CHARS) if len(b) > OPEN_CHARS else b)
    if loc:
        text.append(f"Details: {loc}")
    tail = _links_line(nb[0], gist + "\n" + b, set(), nb[1], nb[2])
    if tail:
        text.append(tail.lstrip(" ·"))
    text.append("This is a record of that time, not of now. Check it against the current files and results before "
                "relying on it; if it does not fit what you are doing now, set it aside.")
    return "\n".join(text)


def tool(inp: dict) -> str:
    inp = inp or {}
    if not enabled():
        return "Error: Construct is off on this machine (LAPS_CONSTRUCT=0)."
    folder = _current.get("folder") or os.getcwd()
    if inp.get("open") not in (None, ""):
        iid = _as_id(inp.get("open"))
        if iid is None:
            return "Error: open takes the id of a fragment (the number after #)."
        return open_memory(iid, str(inp.get("why") or ""), folder=folder)
    q = str(inp.get("query") or "").strip()
    if not q:
        return "Error: give query (the distinctive words to recall with), or open=<id> with why to read one fragment."
    scope = str(inp.get("scope") or "all")
    cl = cues(q)
    if not cl:
        return ("No usable words in the query. Give distinctive words: file names, identifiers, version numbers, "
                "error text or names.")
    hits = search(q, folder=folder, limit=8, min_score=0.15, cue_list=cl, other_folders=(scope != "folder"),
                  exclude_sessions=(_current["session"],) if _current.get("session") else ())
    parts: list[str] = []
    if hits:
        note_hits([h.id for h in hits])
        with _lock:
            con = _connect()
            try:
                nbs = _neighbors(con, [h.id for h in hits])
            finally:
                con.close()
        qterms = {c.term.lower() for c in cl}
        parts.append(f"Construct: {len(hits)} fragments (best first). These are only fragments: decide from the current "
                     "task whether one is needed, then open it with Construct(open=<id>, why=\"<how it relates to what "
                     "you are doing now>\"). Links are words to recall with next. If none fits, go on without them.")
        for h in hits:
            day = time.strftime("%Y-%m-%d", time.localtime(h.ts))
            frag = fragment(h.kind, h.gist)
            icues, prev, nxt = nbs.get(h.id, ("", None, None))
            parts.append(f"#{h.id} [{day} · {_KIND_LABEL.get(h.kind, h.kind)} · {_where(h, folder)} · "
                         f"{_src(h.source, h.status)}] {frag}{_links_line(icues, frag, qterms, prev, nxt)}")
    conv: list[dict] = []
    if inp.get("conversations", True) is not False:
        conv = search_conversations(q, exclude=_current.get("session", ""))
        if conv:
            parts.append(f"Past conversations: {len(conv)} (newest first; the file holds the whole conversation, "
                         "search it with Grep if one is needed now)")
            for cv in conv:
                parts.append(f"- [{cv['date']} · {cv['role']}] …{_clip(cv['snippet'], 120)}… (file: {cv['file']})")
    if not parts:
        return ("No memory matches these words (Construct searched this machine's memory"
                + (" and recent conversations" if inp.get("conversations", True) is not False else "")
                + "). Say that you do not remember, or ask the user; do not guess.")
    return "\n".join(parts)


# ── 取り込み（いまの置き場が正本・Construct は索引だけ）─────────────────────────────

def _meta_get(con: sqlite3.Connection, k: str) -> str:
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return str(row[0]) if row else ""


def _changed(path: Path, tag: str) -> str:
    """ファイルが前に取り込んだ時から変わっていれば新しい印（mtime と大きさ）を、変わっていなければ "" を返す。"""
    try:
        st = path.stat()
    except OSError:
        return ""
    stamp = f"{st.st_mtime_ns}:{st.st_size}"
    with _lock:
        con = _connect()
        try:
            old = _meta_get(con, tag)
        finally:
            con.close()
    return "" if old == stamp else stamp


_WL_HEAD = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*・\s*([^・]+?)\s*・")
_WL_REQ = re.compile(r"^- 依頼:\s*「(.*)」\s*$", re.M)
_WL_FILES = re.compile(r"^- 書き換えたファイル:\s*(.+)$", re.M)
_WL_CHECK = re.compile(r"^- 確認:\s*(.+)$", re.M)
_WL_RUN = re.compile(r"<!-- run:(\S+) -->")
_FD_LINE = re.compile(r"^\[(?:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}))?\s*(?:#(\S+))?\]\s*(.+)$")


def _ts_of(stamp: str) -> float | None:
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M"))
    except (ValueError, OverflowError):
        return None


def ingest_worklog(cwd: str) -> int:
    """作業ログ（.blackwall/WORKLOG.md）の作業の記録を、出来事として索引に足す（無い物だけ）。足した件数。
    途中の記録（作業中）は足さない（終わりの記録が置き換える）。古い記録から足し、時間の隣（prev）を繋ぐ。"""
    from . import worklog as _wl
    p = _wl.path(cwd)
    fk = folder_key(cwd)
    stamp = _changed(p, f"wl:{fk}")
    if not stamp:
        return 0
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return 0
    rel = os.path.join(_lp.workspace_name(cwd), "WORKLOG.md")
    n = 0
    with _lock:
        con = _connect()
        try:
            row = con.execute("SELECT id FROM items WHERE kind='episode' AND folder=? ORDER BY ts DESC LIMIT 1",
                              (fk,)).fetchone()
            prev = int(row[0]) if row else None
            for ent in reversed(_wl._entries(_wl._find(_wl._sections(text), _wl.H_LOG))):
                m = _WL_HEAD.match(ent)
                if not m or "作業中" in m.group(2):
                    continue
                # ★ハーネスが書いた記録だけ（走行の印 <!-- run:… --> がある物）。2026-10-21 実機の試しで、モデルが作業ログに
                #   自分で「完了」の記録を書き足した＝印の無い記録を「ハーネスの記録」として取り込むと自己汚染になる
                #   （実物の作業ログ /home/user/project は 30 件すべてに印がある＝落とす物は無い）。
                if not _WL_RUN.search(ent):
                    continue
                when, label = m.group(1), m.group(2).strip()
                mreq, mfiles, mcheck, mrun = (_WL_REQ.search(ent), _WL_FILES.search(ent), _WL_CHECK.search(ent),
                                              _WL_RUN.search(ent))
                req = mreq.group(1) if mreq else ""
                run_id = mrun.group(1) if mrun else ""
                session = re.sub(r"-\d+$", "", run_id) if run_id else ""
                gist = f"依頼「{_clip(req, 120)}」→ {label}" + (f"・書き換え: {_clip(mfiles.group(1), 70)}" if mfiles else "")
                status = "verified" if (mcheck and "✓" in mcheck.group(1)) else ""
                # ★通った確かめの命令は、次に同じ種類の作業をするときにいちばん使える（どう試すか）。要旨に入れる
                #   （2026-10-21 実機の試しの準備で気づいた: 作業ログを開かないと命令が分からなかった）。断片（fragment）には
                #   出さず、モデルが開いた時に見える。
                mcmd = re.search(r"`([^`]{2,80})`", mcheck.group(1)) if (mcheck and status == "verified") else None
                if mcmd:
                    gist += f"・確かめ: {mcmd.group(1)}"
                iid, inserted = _put(con, "episode", gist, folder=fk, body=_WL_RUN.sub("", ent).strip(),
                                     loc=f"{rel} ({when})", source="harness", status=status, session=session,
                                     run=run_id, ts=_ts_of(when), prev=prev, touch=False)
                if iid is not None:
                    n += int(inserted)
                    prev = iid
            con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (f"wl:{fk}", stamp))
            con.commit()
            if n:
                _prune(con)
        finally:
            con.close()
    return n


def ingest_findings(cwd: str) -> int:
    """帳面（.blackwall/FINDINGS.md）の確かめた事実と行き止まりを足す（無い物だけ）。出どころはモデル（要約で書いた物）。"""
    from . import findings as _fd
    p = _fd.ledger_path(cwd)
    fk = folder_key(cwd)
    stamp = _changed(p, f"fd:{fk}")
    if not stamp:
        return 0
    secs = _fd._read_ledger(p)
    rel = os.path.join(_lp.workspace_name(cwd), "FINDINGS.md")
    n = 0
    with _lock:
        con = _connect()
        try:
            for head, kind in ((_fd.H_FACTS, "fact"), (_fd.H_DEAD, "dead_end")):
                for line in secs.get(head, []):
                    m = _FD_LINE.match(line.strip())
                    when, session, body = (m.group(1) or "", m.group(2) or "", m.group(3)) if m else ("", "", line)
                    body = body.strip()
                    if len(body) < 25 or body.endswith((":", "：")):     # 見出しだけの行・短すぎる行は手掛かりにならない
                        continue
                    _, inserted = _put(con, kind, body, folder=fk, loc=rel + (f" ({when})" if when else ""),
                                       source="model", status="verified" if kind == "fact" else "", session=session,
                                       ts=_ts_of(when) if when else None, touch=False)
                    n += int(inserted)
            con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (f"fd:{fk}", stamp))
            con.commit()
            if n:
                _prune(con)
        finally:
            con.close()
    return n


def ingest_lessons() -> int:
    """失敗と効いた直し方（~/.blackwall/lessons.jsonl・フォルダをまたぐ）を足す（無い物だけ）。出どころは道具の結果。"""
    from . import lessons as _ls
    p = _ls.store_path()
    stamp = _changed(p, "lessons")
    if not stamp:
        return 0
    n = 0
    with _lock:
        con = _connect()
        try:
            for d in _ls._load():
                sig, fix = str(d.get("sig") or ""), str(d.get("fix") or "")
                gist = f"失敗「{_clip(sig, 100)}」→ 効いた手「{_clip(fix, 110)}」"
                body = " ".join(str(x) for x in (d.get("queries") or []) + (d.get("urls") or []))
                _, inserted = _put(con, "lesson", gist, folder="", body=body, loc=f"~/{_lp.dot_dir().name}/lessons.jsonl",
                                   source="tool", status="verified", ts=float(d.get("ts") or time.time()), touch=False)
                n += int(inserted)
            con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("lessons", stamp))
            con.commit()
            if n:
                _prune(con)
        finally:
            con.close()
    return n


def ingest(cwd: str) -> dict[str, int]:
    """走行の始めに呼ぶ。変わったファイルだけを読み、無い物だけを足す。推論機は呼ばない。"""
    if not enabled():
        return {}
    out = {}
    for name, fn in (("worklog", lambda: ingest_worklog(cwd)), ("findings", lambda: ingest_findings(cwd)),
                     ("lessons", ingest_lessons)):
        try:
            out[name] = fn()
        except Exception:
            out[name] = -1
    return out


# ── 段 3: 夜の整理（プロジェクトの要点・sleep-time）────────────────────────────────

PROJECT_NAME = "PROJECT.md"


def project_rel(folder: str) -> str:
    """プロジェクトの要点の、作業フォルダからの相対の道（置き場の名前は paths の規則で選ぶ）。"""
    return os.path.join(_lp.workspace_name(folder), PROJECT_NAME)
CARD_SECTIONS = ("How to build and run", "How to check", "Rules and the user's preferences", "Pitfalls", "Main files")
CARD_INPUT_CHARS = 7000
CARD_PER_SECTION = 6
CARD_MIN_NOTES = 5
_CITE = re.compile(r"\[\s*n\d+(?:\s*,\s*n\d+)*\s*\]")
_CARD_PRIORITY = {"correction": 0, "dead_end": 1, "lesson": 2, "fact": 3, "intention": 4, "episode": 5}

CARD_SYSTEM = ("You summarize an engineering work log into a short project card. Use only the notes you are given and "
               "never add a fact that is not in them. End every bullet with the ids of the notes it comes from, like "
               "[n12] or [n12, n40].")


def replay_notes(folder: str, limit_chars: int = CARD_INPUT_CHARS) -> list[tuple[int, str, str]]:
    """整理に渡す記録を役に立つ順に（優先度つきの再生・Mattar & Daw 2018）: 利用者の訂正 → 行き止まり → 効いた直し方 →
    分かったこと → やり残し → 最近の作業。同じ種類の中では、効いた・使われた・新しい物から。(id, 種類, 1 行)。
    ★前の要点（schema）は入れない（自分の要約をまた要約する＝自己汚染）。「外れた」の印と済んだやり残しも入れない。"""
    fk = folder_key(folder)
    with _lock:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT id, kind, source, status, gist, used, good, bad, ts FROM items "
                "WHERE (folder=? OR (folder='' AND kind IN ('correction','lesson'))) AND kind<>'schema' "
                "AND status<>'contradicted' AND NOT (kind='intention' AND status='done')", (fk,)).fetchall()
        finally:
            con.close()
    rows.sort(key=lambda r: (_CARD_PRIORITY.get(r[1], 9), -(int(r[6] or 0) - int(r[7] or 0)), -int(r[5] or 0), -float(r[8])))
    out: list[tuple[int, str, str]] = []
    used = 0
    for iid, kind, source, status, gist, *_ in rows:
        tag = kind + (", said by the user" if source == "user" else "") + (", unverified" if status == "unverified" else "")
        line = f"[n{iid}] ({tag}) {gist}"
        if used + len(line) > limit_chars:
            break
        out.append((int(iid), kind, line))
        used += len(line) + 1
    return out


def card_prompt(folder: str, notes: list[tuple[int, str, str]]) -> list[dict]:
    heads = "\n".join(f"## {h}" for h in CARD_SECTIONS)
    body = "\n".join(line for _, _, line in notes)
    return [{"role": "system", "content": CARD_SYSTEM},
            {"role": "user", "content": (
                f"Folder: {folder}\nNotes, most important first (correction = said by the user, dead_end = tried and "
                "failed, lesson = a fix that worked after failures, fact = something noted, intention = left to do, "
                f"episode = an earlier task):\n{body}\n\nWrite the project card in the language of the notes, with "
                f"exactly these headings:\n{heads}\nUnder each heading write at most {CARD_PER_SECTION} bullets, one line "
                "each (under 140 characters), each ending with the note ids it comes from. Prefer what the user said "
                "and what was confirmed. Leave out what was only true at that moment (someone using a machine right "
                "now, a temporary state, a one-time request). Do not repeat the same fact. If nothing is known for a "
                "heading, write "
                "\"- (none)\". Write nothing else.")}]


def parse_card(text: str, valid_ids: set[int]) -> tuple[dict[str, list[str]], int]:
    """要点の文を見出しごとの箇条に分ける。実在する記録の番号を 1 つも引いていない行は捨てる（作り話を残さない）。
    (見出し → 箇条, 捨てた行の数)。"""
    secs: dict[str, list[str]] = {h: [] for h in CARD_SECTIONS}
    cur = None
    dropped = 0
    for raw in str(text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^#{1,3}\s*(.+?)\s*$", line)
        if m:
            name = m.group(1).strip().lower()
            cur = next((h for h in CARD_SECTIONS if h.lower() == name), None)
            continue
        if cur is None or not line.startswith(("- ", "* ")):
            continue
        item = line[2:].strip()
        if not item or item.strip("()（）").lower() in ("none", "なし"):
            continue
        ids = {int(x) for x in re.findall(r"n(\d+)", " ".join(_CITE.findall(item)))}
        if not ids or not (ids & valid_ids):
            dropped += 1
            continue
        if len(secs[cur]) < CARD_PER_SECTION:
            secs[cur].append(_clip(item, 200))
    return secs, dropped


def _write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def consolidate(folder: str, backend=None, *, now: float | None = None) -> dict:
    """1 つのフォルダの記録を、推論機に「プロジェクトの要点」（スキーマ・Tse 2007）としてまとめさせる（段 3・夜の整理）。
    .blackwall/PROJECT.md に書き、箇条を「project summary」として索引に入れ直す（前の要点の箇条は消す）。
    出どころはモデル（まとめた物）＝確かめた事実とは混ぜない。記録が少なければ何もしない。結果の数を返す。"""
    if not enabled():
        return {"skipped": "off"}
    now = time.time() if now is None else now
    fk = folder_key(folder)
    notes = replay_notes(fk)
    if len(notes) < CARD_MIN_NOTES:
        return {"skipped": "too few notes", "notes": len(notes)}
    if backend is None:
        from ..backend import Backend
        backend = Backend()
    text = backend.chat(card_prompt(fk, notes), temperature=0.2, max_tokens=1800,
                        chat_template_kwargs={"enable_thinking": False})
    try:
        from .base import strip_think
        text = strip_think(text or "")
    except Exception:
        text = str(text or "")
    secs, dropped = parse_card(text, {i for i, _, _ in notes})
    kept = sum(len(v) for v in secs.values())
    if not kept:
        return {"skipped": "no usable bullets", "notes": len(notes), "dropped": dropped}
    day = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    parts = [f"# プロジェクトの要点（Construct が {day} に {len(notes)} 件の記録からまとめた・未確認）",
             "（夜の整理で作り直す。人が書き足すことは作業ログのメモへ。各行の [nNN] は元にした記録の番号＝Construct の道具で引ける）", ""]
    for h in CARD_SECTIONS:
        parts.append(f"## {h}")
        if secs[h]:
            parts.extend(f"- {b}" for b in secs[h])
        else:
            parts.append("- (none)")
        parts.append("")
    prel = project_rel(fk)
    path = os.path.join(fk, prel)
    _write_atomic(path, _redact("\n".join(parts)))
    with _lock:
        con = _connect()
        try:
            con.execute("DELETE FROM items WHERE kind='schema' AND folder=?", (fk,))
            for h in CARD_SECTIONS:
                for b in secs[h]:
                    _put(con, "schema", _CITE.sub("", b).strip(), folder=fk, body=b, loc=f"{prel} ({h})",
                         source="model", ts=now)
            con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (f"card:{fk}", str(now)))
            con.commit()
        finally:
            con.close()
    return {"bullets": kept, "dropped": dropped, "notes": len(notes), "path": path}


def stats() -> dict:
    """記憶の数（種類ごと・外れた印・使われた・効いた・外れた）。"""
    with _lock:
        con = _connect()
        try:
            kinds = dict(con.execute("SELECT kind, count(*) FROM items GROUP BY kind").fetchall())
            row = con.execute("SELECT count(*), sum(hits), sum(used), sum(good), sum(bad), "
                              "sum(status='contradicted') FROM items").fetchone()
            folders = con.execute("SELECT count(DISTINCT folder) FROM items WHERE folder<>''").fetchone()[0]
        finally:
            con.close()
    total, hits, used, good, bad, contra = (int(x or 0) for x in row)
    return {"total": total, "kinds": kinds, "folders": int(folders or 0), "hits": hits, "used": used, "good": good,
            "bad": bad, "contradicted": contra, "path": str(db_path())}


# ── 段 3b: 夜に自動で整理する（serve が空いている時だけ）──────────────────────────

#: 夜の窓（この機械の時刻・時）。この間に 1 晩 1 回だけ。
NIGHT_HOURS = (1, 5)
NIGHT_EVERY = 20 * 3600
NIGHT_FOLDERS = 3
NIGHT_MIN_NEW = 3


def nightly_enabled() -> bool:
    """★既定は切る（2026-10-21 オーナー: 段 3 は要るかを聞かれて「1（既定で止める）・2 で試してみて」）。要点が作業ログ・帳面・
    段 1 の思い出しと重なり、効き目は未測定で、夜に推論機を使う。LAPS_CONSTRUCT_NIGHT=1 で入る。手で動かす
    `blackwall construct consolidate` はいつでも使える。"""
    return enabled() and (os.environ.get("LAPS_CONSTRUCT_NIGHT") or "0").strip().lower() in ("1", "on", "true", "yes")


def nightly_due(now: float | None = None) -> bool:
    """夜の窓の中で、前の夜の整理から 20 時間以上たっているか。"""
    if not nightly_enabled():
        return False
    now = time.time() if now is None else now
    if not (NIGHT_HOURS[0] <= time.localtime(now).tm_hour < NIGHT_HOURS[1]):
        return False
    with _lock:
        con = _connect()
        try:
            last = _meta_get(con, "night:last")
        finally:
            con.close()
    try:
        return now - float(last or 0) >= NIGHT_EVERY
    except ValueError:
        return True


def due_folders(max_folders: int = NIGHT_FOLDERS, min_new: int = NIGHT_MIN_NEW) -> list[str]:
    """前の要点から新しい記録が min_new 件以上増えたフォルダ（最近の動きの順・在るフォルダだけ）。"""
    with _lock:
        con = _connect()
        try:
            rows = con.execute("SELECT folder, max(ts) FROM items WHERE folder<>'' AND kind<>'schema' "
                               "GROUP BY folder ORDER BY max(ts) DESC").fetchall()
            out = []
            for folder, _last in rows:
                try:
                    card = float(_meta_get(con, f"card:{folder}") or 0)
                except ValueError:
                    card = 0.0
                n_new = con.execute("SELECT count(*) FROM items WHERE folder=? AND kind<>'schema' AND ts>?",
                                    (folder, card)).fetchone()[0]
                if n_new >= min_new and os.path.isdir(folder):
                    out.append(folder)
                if len(out) >= max_folders:
                    break
        finally:
            con.close()
    return out


def local_backend_ok(backend) -> bool:
    """夜の整理に使ってよい送り先か。★Claude の中継（API・費用がかかる）には向けない（2026-10-18 オーナー「API は
    ローカルに混ぜない」）。ローカルの推論機（llama-server）だけ。"""
    try:
        cfg = getattr(backend, "cfg", None)
        url = str(getattr(cfg, "base_url", "") or "")
        model = str(getattr(cfg, "model", "") or "").lower()
        from ..term.models import is_claude
        return bool(url) and not is_claude(url) and "anthropic" not in url.lower() and not model.startswith("claude")
    except Exception:
        return False


def nightly(should_stop, backend_factory=None, now: float | None = None) -> dict:
    """夜の整理を 1 回: 新しい記録が増えたフォルダを最大 3 つ、consolidate する。should_stop() が真になったら次のフォルダへ
    進まない（serve に依頼が来た）。始めに「今夜やった」と書く＝失敗しても同じ夜に何度も試さない。結果の数を返す。"""
    now = time.time() if now is None else now
    out = {"folders": 0, "bullets": 0, "skipped": 0, "errors": 0, "stopped": 0}
    if not nightly_enabled():
        return out
    with _lock:
        con = _connect()
        try:
            con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("night:last", str(now)))
            con.commit()
        finally:
            con.close()
    folders = due_folders()
    if not folders:
        return out
    if backend_factory is None:
        from ..backend import Backend
        backend_factory = Backend
    backend = backend_factory()
    if not local_backend_ok(backend):
        out["skipped"] = len(folders)
        return out
    for f in folders:
        if should_stop():
            out["stopped"] = 1
            break
        try:
            r = consolidate(f, backend, now=now)
            if r.get("bullets"):
                out["folders"] += 1
                out["bullets"] += int(r["bullets"])
            else:
                out["skipped"] += 1
        except Exception:
            out["errors"] += 1
            if should_stop():
                out["stopped"] = 1
                break
    return out
