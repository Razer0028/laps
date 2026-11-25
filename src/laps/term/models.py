"""送り先とモデルの選び（2026-10-19 オーナー「Windows や Linux 版のアプリから API のモデルを自由に切り替えられるようにしてほしい」）。

端末版の `/model`・`/backend claude|server`・全画面の F5 から、Claude API のモデル（開発機 の中継 scripts/claude_relay.py）と
推論機のモデル（推論機 の モデル切り替えの口）を**同じ一覧**で選ぶ。選ぶと送り先（backend）も合わせて変える
（Claude のモデルなら中継へ・推論機のモデルなら推論機へ）。

★切り替えそのものは向こうがする（中継は次の依頼から・推論機は裏で 1〜3 分の入れ替えと GPU の設定の突き合わせ）。
  ここは一覧を読むのと頼むだけ＝Windows のアプリの modelctl.rs・remotectl.server_models と同じ口（/modelctl/v1/models・
  /modelctl/v1/switch／:8743 の /v1/models・/v1/switch）。
★中継で選んだ Claude のモデルは中継に残る＝Windows・Android・端末版のどれから見ても同じ（推論機 の推論機と同じ考え）。
★本体（serve）に送るモデル名は変えない（blackwall-coder のまま）。中継はこの別名をいま選ばれているモデルに読み替える。

★推論機を空ける・起こす（2026-11-03 オーナー「Blackwallのアプリや CLI 版から推論機を空けたり（止めて GPU を返す）
  推論機を起動したりできるようにしたい」）。`/model stop` が モデル切り替えの口 の POST /v1/stop を頼み、止まっている推論機に
  `/model <id>` を選ぶと /v1/switch がそのまま起動する。止める口は モデル切り替えの口 だけにある（一覧に "stoppable": true）。
  Claude の中継（/modelctl/）には無い＝止める先は必ず推論機の :8743 で、中継へは頼まない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import httpx

__all__ = ["Choice", "Listing", "Result", "is_claude", "server_url", "fetch", "pick", "switch", "stop", "pretty",
           "STOPPED_LABEL", "LEASED_LABEL", "stopped_label"]

#: 推論機が止まっているときの表示（GPU は全セッション共用の GPU の貸し出し に空いている）。
STOPPED_LABEL = "止まっています（GPU は空き）"
#: 推論機を GPU の貸し出し の貸し出しが止めているときの表示（GPU は仕事が使っている・終われば向こうが戻す）。
LEASED_LABEL = "貸し出し中（終われば自動で戻ります）"


def stopped_label(lease_stopped: str) -> str:
    """推論機が載っていないときの一言。貸し出しが止めているなら（lease.llm_stopped）GPU は空いていない。"""
    return LEASED_LABEL if lease_stopped else STOPPED_LABEL

#: 中継の口（scripts/claude_relay.py の RELAY_PORT）。設定の claude_backend と住所が違っても、この口なら中継と見る
#: （Windows のアプリの isClaudeUrl と同じ考え）。
CLAUDE_PORT = 8745
#: 推論機の モデル切り替えの口（src/deploy/モデル切り替えの口）の口。
MODELCTL_PORT = 8743
#: 一覧は モデル切り替えの口 が systemctl を何度か呼ぶので長め（modelctl.rs と同じ 15 秒）。
TIMEOUT = httpx.Timeout(15.0, connect=3.0)


@dataclass
class Choice:
    kind: str                 # "claude"（中継）| "server"（推論機）
    id: str                   # 切り替えに渡す名前（中継はモデル名・推論機は llm-blackwall-<id> の id）
    label: str                # 表示の名前
    detail: str = ""          # 料金・文脈など
    active: bool = False      # その送り先でいま選ばれている
    ok: bool = True           # 選べる（推論機のファイルが無いと False）


@dataclass
class Listing:
    where: str                                  # いまの送り先: "claude" | "server"
    claude_url: str                             # 中継の /v1（設定の claude_backend）
    server_url: str                             # 推論機の /v1
    choices: list[Choice] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # 取れなかった理由（黙って一覧から消さない）
    spent: str = ""                             # 今月の Claude API の額（中継が答えたとき）
    switching: str = ""                         # 推論機の入れ替え中の行き先
    switch_note: str = ""                       # 推論機の最後の入れ替えの結果
    # ★推論機を止める・起こす（2026-11-03）。server_read は推論機の一覧が読めたとき True
    #   （読めないときに「止まっている」と言わない）。
    server_read: bool = False
    server_stopped: bool = False                # 推論機の一覧は読めたが、載っているモデルが無い（active が null）
    stoppable: bool = False                     # モデル切り替えの口 が POST /v1/stop を持つ（一覧の "stoppable": true）
    stopping: bool = False                      # 推論機を止めている途中（switch.action == "stop" で running）
    # ★GPU の貸し出し の貸し出しが止めている LLM（一覧の lease.llm_stopped）。空でなければ「止まっている」は貸し出しのせいで、
    #   GPU は仕事が使っている。仕事が終われば向こうが戻す＝利用者が止めたのとは違う（2026-11-03）。
    lease_stopped: str = ""

    def stopped_label(self) -> str:
        return stopped_label(self.lease_stopped)

    def current(self) -> Optional[Choice]:
        """いま実際に使っているもの（送り先が同じで、向こうで選ばれている物）。"""
        return next((c for c in self.choices if c.kind == self.where and c.active), None)


def _root(url: str) -> str:
    u = urlsplit(str(url or "").strip())
    if not u.scheme or not u.hostname:
        return ""
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme}://{host}{f':{u.port}' if u.port else ''}"


def is_claude(backend: str, claude_url: str = "") -> bool:
    """送り先が Claude の中継か（設定の claude_backend と同じ住所と口、または中継の口）。"""
    b = urlsplit(str(backend or "").strip())
    if not b.hostname:
        return False
    c = urlsplit(str(claude_url or "").strip())
    if c.hostname and (b.hostname, b.port) == (c.hostname, c.port):
        return True
    return b.port == CLAUDE_PORT


def server_url(cfg: dict, backend: str) -> str:
    """推論機の送り先。いまが推論機ならそのまま、中継なら覚えておいた推論機（server_backend）か既定の P40。"""
    claude = str((cfg or {}).get("claude_backend") or "")
    if backend and not is_claude(backend, claude):
        return backend
    from .config import DEFAULTS
    return str((cfg or {}).get("server_backend") or "").strip() or str(DEFAULTS["backend"])


def pretty(model_id: str) -> str:
    """claude-sonnet-5-5 → Claude Sonnet 5.5（Android の ChatModel と同じ読み方）。それ以外はそのまま。"""
    m = re.match(r"^claude-([a-z]+)-(\d+)-(\d+)$", str(model_id or "").strip().lower())
    if not m:
        return str(model_id or "")
    return f"Claude {m.group(1).capitalize()} {m.group(2)}.{m.group(3)}"


def _http(transport: Optional[httpx.BaseTransport]) -> httpx.Client:
    # trust_env=False: 相手は LAN の私設アドレス（端末版の他の HTTP と同じ）。
    return httpx.Client(transport=transport, timeout=TIMEOUT, trust_env=False)


def _bases(lst: Listing) -> dict[str, str]:
    out = {}
    r = _root(lst.claude_url)
    if r:
        out["claude"] = r + "/modelctl/"
    u = urlsplit(lst.server_url)
    if u.hostname:
        host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
        out["server"] = f"{u.scheme or 'http'}://{host}:{MODELCTL_PORT}/"
    return out


def _claude_choices(d: dict) -> tuple[list[Choice], str]:
    out = []
    for u in d.get("units") or []:
        mid = str(u.get("id") or "")
        if not mid:
            continue
        p = u.get("price") or {}
        detail = f"${p['in']}/${p['out']}・100 万トークン" if "in" in p and "out" in p else ""
        out.append(Choice("claude", mid, pretty(mid), detail, bool(u.get("active")), bool(u.get("file_ok", True))))
    spent = ""
    if isinstance(d.get("spent_usd"), (int, float)):
        spent = f"今月 ${float(d['spent_usd']):.2f}"
        lim = [f"普段使いは ${d['ordinary_usd']:g} まで" if isinstance(d.get("ordinary_usd"), (int, float)) else "",
               f"${d['stop_usd']:g} で止まる" if isinstance(d.get("stop_usd"), (int, float)) else ""]
        lim = [x for x in lim if x]
        if lim:
            spent += "（" + "・".join(lim) + "）"
    return out, spent


def _server_choices(d: dict) -> list[Choice]:
    out = []
    for u in d.get("units") or []:
        uid = str(u.get("id") or "")
        if not uid:
            continue
        name = str(u.get("model") or uid)
        feat = [f"コンテキスト {round(int(u['ctx']) / 1024)}K" if u.get("ctx") else "",
                "画像" if u.get("vision") else "", "投機的デコード" if u.get("mtp") else ""]
        detail = "・".join(x for x in feat if x)
        if name != uid:
            detail = f"{uid}" + (f"・{detail}" if detail else "")
        ok = bool(u.get("file_ok", True))
        out.append(Choice("server", uid, name, detail + ("" if ok else "（ファイルが無い）"), bool(u.get("active")), ok))
    return out


def fetch(cfg: dict, backend: str, transport: Optional[httpx.BaseTransport] = None) -> Listing:
    """両方の一覧を読む（Claude が先）。届かない方は notes に理由を書いて飛ばす。

    ★設定に claude_backend が無ければ Claude の一覧は読まない（試験の仮の設定・中継を使わない人）。"""
    claude = str((cfg or {}).get("claude_backend") or "").strip()
    lst = Listing(where="claude" if is_claude(backend, claude) else "server",
                  claude_url=claude or (backend if is_claude(backend) else ""),
                  server_url=server_url(cfg, backend))
    bases = _bases(lst)
    with _http(transport) as h:
        for kind in ("claude", "server"):
            base = bases.get(kind)
            if not base:
                continue
            name = "Claude の中継サーバー" if kind == "claude" else "推論サーバーの切り替え API"
            try:
                r = h.get(base + "v1/models")
            except Exception as e:  # noqa: BLE001 — 届かない理由を 1 行で
                lst.notes.append(f"{name}（{base}）に接続できません: {type(e).__name__}")
                continue
            try:
                d = r.json() if r.status_code == 200 else None
            except ValueError:
                d = None
            if not isinstance(d, dict) or not isinstance(d.get("units"), list):
                lst.notes.append(f"{name}（{base}）が一覧を返しません（HTTP {r.status_code}）")
                continue
            if kind == "claude":
                got, lst.spent = _claude_choices(d)
            else:
                got = _server_choices(d)
                lst.server_read = True
                lst.server_stopped = not str(d.get("active") or "")
                lst.stoppable = d.get("stoppable") is True
                lease = d.get("lease") if isinstance(d.get("lease"), dict) else {}
                lst.lease_stopped = str(lease.get("llm_stopped") or "")
                sw = d.get("switch") or {}
                if sw.get("state") == "running":
                    # ★止めている途中は target が null（action "stop"）。古い モデル切り替えの口 は action を持たない＝入れ替え。
                    if sw.get("action") == "stop":
                        lst.stopping = True
                    else:
                        lst.switching = str(sw.get("target") or "")
                elif sw.get("state") in ("done", "failed"):
                    lst.switch_note = str(sw.get("message") or "")
            lst.choices.extend(got)
    return lst


def pick(lst: Listing, arg: str) -> tuple[Optional[Choice], list[Choice]]:
    """`arg`（番号・名前・名前の一部）に当たる物。(当たった物, 候補が複数のときの候補)。

    番号は一覧の 1 から。名前は id と表示の名前の両方で、完全一致を先に、次に一部の一致（1 つに決まるときだけ）。"""
    a = str(arg or "").strip()
    if not a:
        return None, []
    if a.isdigit():
        i = int(a) - 1
        return (lst.choices[i], []) if 0 <= i < len(lst.choices) else (None, [])
    low = a.lower()
    for c in lst.choices:
        if low in (c.id.lower(), c.label.lower()):
            return c, []
    hits = [c for c in lst.choices if low in c.id.lower() or low in c.label.lower()]
    if len(hits) == 1:
        return hits[0], []
    return None, hits


@dataclass
class Result:
    ok: bool
    message: str
    backend: str = ""        # 新しい送り先（変えないなら空）
    pending: bool = False    # 推論機の入れ替えが裏で続いている


def switch(lst: Listing, c: Choice, backend: str,
           transport: Optional[httpx.BaseTransport] = None) -> Result:
    """`c` に切り替える。送り先が変わるなら Result.backend に新しい送り先を入れて返す（書き換えは呼び手）。

    ★向こうでもう選ばれている物は頼まない（送り先だけ変える）。推論機は 202 で受けて裏で入れ替える。"""
    if not c.ok:
        return Result(False, f"{c.label} は選べません{('（' + c.detail + '）') if c.detail else ''}")
    target = lst.claude_url if c.kind == "claude" else lst.server_url
    move = c.kind != lst.where or (target and _root(target) != _root(backend))
    msg = ""
    pending = False
    if not c.active:
        base = _bases(lst).get(c.kind)
        if not base:
            return Result(False, "切り替え API の住所が分かりません")
        if c.kind == "server" and lst.switching:
            return Result(False, f"推論サーバーは {lst.switching} へ入れ替えている途中です（終わってから）")
        if c.kind == "server" and lst.stopping:
            return Result(False, "推論サーバーを止めている途中です（終わってから）")
        try:
            with _http(transport) as h:
                r = h.post(base + "v1/switch", json={"id": c.id, "force": False})
        except Exception as e:  # noqa: BLE001
            return Result(False, f"切り替え API （{base}）に接続できません: {type(e).__name__}")
        try:
            d = r.json()
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        if r.status_code >= 400:
            return Result(False, str(d.get("error") or f"切り替え API が HTTP {r.status_code} を返しました")[:300])
        msg = str(d.get("message") or "切り替えを始めました")
        pending = c.kind == "server"
        if pending and lst.server_stopped:
            # ★止まっている推論機に選んだ＝入れ替えではなく起動（モデル切り替えの口 は前のモデル無しで載せる）。
            msg = f"{c.label} を起動します"
        if pending:
            msg += "（推論サーバーの読み込みに 1〜3 分ほど。進み具合は /model）"
    else:
        msg = f"{c.label} はもう選ばれています"
    if move and target:
        where = "Claude API（中継サーバー）" if c.kind == "claude" else "推論サーバー"
        msg += f"。送り先を {where} {target} にしました（次の依頼から）"
        if c.kind == "claude":
            msg += "。費用は共有の API の枠から出ます"
        return Result(True, msg, backend=target, pending=pending)
    return Result(True, msg, pending=pending)


def stop(lst: Listing, force: bool = False, transport: Optional[httpx.BaseTransport] = None) -> Result:
    """推論機を止めて GPU を空ける（モデル切り替えの口 の POST /v1/stop・2026-11-03）。

    ★頼む先は推論機の :8743 だけ（Claude の中継には止める口が無い）。一覧に "stoppable" が無い口
      （古い モデル切り替えの口・中継）には頼まない。生成中（409 busy）は force で押し切れる。
      GPU の仕事（GPU の貸し出し の借り）が GPU を持っている間（409 lease）は押し切らない＝借りと争わない。"""
    base = _bases(lst).get("server")
    if not base or not lst.server_read:
        why = "・".join(n for n in lst.notes if "推論サーバー" in n)
        return Result(False, "推論サーバーの切り替え API が読めません" + (f"（{why}）" if why else ""))
    if is_claude(lst.server_url):
        return Result(False, "Claude API の中継サーバーには止める口がありません（止めるのは推論サーバーだけ）")
    if not lst.stoppable:
        return Result(False, "この推論サーバーの切り替え API は止める口を持っていません（モデル切り替えの口 が古い）")
    if lst.stopping:
        return Result(True, "推論サーバーはもう止めている途中です（進み具合は /model）", pending=True)
    if lst.switching:
        return Result(False, f"推論サーバーは {lst.switching} へ入れ替えている途中です（終わってから）")
    # ★貸し出しが止めた LLM は仕事の後に戻されるので、止まって見えても手元で「もう止まっています」と答えない（2026-11-03）。
    #   向こうに頼んで「戻さない」と帳面から消してもらう（仕事が動いている間は 409 lease で断られる）。
    if lst.server_stopped and not lst.lease_stopped:
        return Result(True, f"推論サーバーはもう{STOPPED_LABEL}")
    try:
        with _http(transport) as h:
            r = h.post(base + "v1/stop", json={"force": bool(force)})
    except Exception as e:  # noqa: BLE001
        return Result(False, f"切り替え API （{base}）に接続できません: {type(e).__name__}")
    try:
        d = r.json()
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    if r.status_code == 404:
        return Result(False, "この推論サーバーの切り替え API は止める口を持っていません（モデル切り替えの口 が古い）")
    if r.status_code >= 400:
        msg = str(d.get("error") or f"切り替え API が HTTP {r.status_code} を返しました")[:300]
        if d.get("busy") and not force:
            msg += "（生成の途中です。それでも止めるなら /model stop --force）"
        elif d.get("lease"):
            msg += "（GPU の仕事が GPU を使っています。終われば空きます）"
        return Result(False, msg)
    if d.get("already"):
        return Result(True, str(d.get("message") or f"推論サーバーはもう{STOPPED_LABEL}"))
    if lst.server_stopped and r.status_code == 200:
        return Result(True, str(d.get("message") or f"推論サーバーはもう{STOPPED_LABEL}"))
    msg = str(d.get("message") or "推論サーバーを止めます")
    msg += "（GPU が空くまで数十秒。進み具合は /model。起こすときは /model <名前>）"
    return Result(True, msg, pending=True)
