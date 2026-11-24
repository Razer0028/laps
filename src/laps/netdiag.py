"""netdiag — 「どこへ繋ごうとしているか」を、待たせる前に言うための小さな道具。

なぜ要るか（2026-09-06・owner 実機 P0）:
  Windows 実機で「推論機の応答を待っています（N 秒）」だけが延々と出て、
  推論機（192.0.2.160:8741）には **要求が 1 件も届いていなかった**。
  原因は httpx が **プロキシを自分で拾う**こと。`httpx.Client(...)` は既定
  `trust_env=True` で `urllib.request.getproxies()` を読む。そして CPython の

      # urllib/request.py（os.name == 'nt' の枝）
      def getproxies():
          return getproxies_environment() or getproxies_registry()

  ——**Windows では環境変数が無ければレジストリ**（Internet 設定の
  ProxyEnable / ProxyServer）を読む。しかも httpx が見る除外リストは
  環境変数 `NO_PROXY` だけで、レジストリ側の `ProxyOverride`
  （＝「ローカル アドレスにはプロキシを使わない」のチェック）は
  **一切読まれない**。つまり Windows で系のプロキシが有効だと、
  `192.0.2.160` どころか `127.0.0.1` 宛てまでプロキシへ送られる。
  Linux では `getproxies()` は環境変数しか見ないので、**同じコードが
  Linux では正しく動き Windows でだけ黙って別の宛先へ行く**。

  ★これはコードを変えなくても**ある日突然**起きる（VPN・保護ソフト・
    企業ポリシーがレジストリを立てる）。だから「版を上げたら壊れた」と
    誤診しやすい。診断は推測ではなく**その場の値**で出す。

方針:
  ・ここは**観測だけ**。プロキシを無効化するのは呼び出し側
    （`httpx.Client(trust_env=False)`）の仕事で、この module は
    「いま何が見えているか」を人が読める 1 行にするだけ。
  ・**値は伏せる**。プロキシ URL には利用者名とパスワードを埋められるので、
    利用者情報は必ず落としてから出す（画面にも記録にも生では出さない）。
"""

from __future__ import annotations

import os
import sys
from typing import Any
from urllib.parse import urlsplit

#: 見るべき環境変数（大文字・小文字の両方が使われる）。
PROXY_ENV_VARS: tuple[str, ...] = (
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "NO_PROXY", "no_proxy",
)


def redact(value: str | None) -> str:
    """プロキシ URL から**利用者情報を落とす**（`@` の手前を伏せる）。

    ★出す前に必ず通す。プロキシの URL は利用者名とパスワードを埋められる形なので、
      そのまま画面や events.jsonl に出すと資格情報が記録に残る。
      （この docstring に実例を書かないのは、伏字検査 `tests/test_redact.py` が
        ソースそのものを走査して「例」と「本物」を区別できないため。）
    """
    v = str(value or "")
    if not v:
        return ""
    if "@" not in v:
        return v
    head, _, tail = v.rpartition("@")
    scheme, sep, _rest = head.partition("://")
    return f"{scheme}{sep}***@{tail}" if sep else f"***@{tail}"


def proxy_env(environ: dict | None = None) -> dict[str, str]:
    """設定されている proxy 系の環境変数（**値は伏せて**返す）。無ければ空。"""
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for k in PROXY_ENV_VARS:
        v = env.get(k)
        if v:
            out[k] = redact(v)
    return out


def _getproxies(getproxies: Any = None) -> dict[str, str]:
    """httpx が見るのと**同じ**辞書（`urllib.request.getproxies()`）。

    ★Windows ではここにレジストリの値が入る（環境変数が空でも）。
      試験から差し替えられるように引数で受ける。
    """
    if getproxies is None:
        from urllib.request import getproxies as _gp
        getproxies = _gp
    try:
        return {str(k): str(v) for k, v in (getproxies() or {}).items()}
    except Exception:
        return {}


def _no_proxy_hosts(proxy_info: dict[str, str]) -> list[str]:
    """httpx と同じ読み方（`no` キーのカンマ区切り）。★レジストリの
    ProxyOverride はここに**入らない**——それが Windows の落とし穴の本体。"""
    return [h.strip() for h in (proxy_info.get("no") or "").split(",") if h.strip()]


def proxy_applies(url: str, *, getproxies: Any = None) -> bool:
    """この URL への要求が**プロキシ経由になるか**。httpx の判定を写す。

    httpx `_utils.get_environment_proxies()` と同じ規則:
      ・`http` / `https` / `all` のいずれかに値があればその scheme はプロキシへ。
      ・`no` に `*` があれば全部素通し。
      ・`no` のホスト名に一致すれば素通し（`NO_PROXY=.example.com` は
        サブドメインのみ、`NO_PROXY=example.com` は自身とサブドメイン）。
    """
    try:
        parts = urlsplit(url or "")
        scheme = (parts.scheme or "http").lower()
        host = (parts.hostname or "").lower()
    except Exception:
        return False
    info = _getproxies(getproxies)
    if not info:
        return False
    hosts = _no_proxy_hosts(info)
    if "*" in hosts:
        return False
    for h in hosts:
        h = h.lower().lstrip("*")
        if not h:
            continue
        if h.startswith("."):
            if host.endswith(h):
                return False
        elif host == h or host.endswith("." + h):
            return False
    return bool(info.get(scheme) or info.get("all"))


def proxy_for(url: str, *, getproxies: Any = None) -> str:
    """この URL に**実際に使われるプロキシ**（値は伏せる）。使わないなら ""。"""
    if not proxy_applies(url, getproxies=getproxies):
        return ""
    parts = urlsplit(url or "")
    scheme = (parts.scheme or "http").lower()
    info = _getproxies(getproxies)
    return redact(info.get(scheme) or info.get("all") or "")


def summary(url: str, *, getproxies: Any = None, environ: dict | None = None) -> dict:
    """記録・`status()`・`ready` に載せる形（機械が読む）。**値は伏せ済み**。"""
    env = proxy_env(environ)
    applies = proxy_applies(url, getproxies=getproxies)
    return {
        "target": str(url or ""),
        "platform": sys.platform,
        "proxy_env": env,                      # 環境変数（伏せ済み・無ければ {}）
        "proxy_applies": applies,              # この宛先がプロキシ経由になるか
        "proxy": proxy_for(url, getproxies=getproxies) if applies else "",
        # ★Windows でだけ起こる「環境変数は空なのにレジストリで有効」。
        #   誤診（「版を上げたら壊れた」）を止めるために、そうと分かる形で出す。
        "proxy_from_registry": bool(applies and not env and os.name == "nt"),
    }


def describe(url: str, *, getproxies: Any = None, environ: dict | None = None) -> str:
    """人が読む 1 行。**黙って待たせない**ための文字列。"""
    s = summary(url, getproxies=getproxies, environ=environ)
    if not s["proxy_applies"]:
        return f"宛先 {s['target']}（プロキシは使いません）"
    if s["proxy_from_registry"]:
        where = "Windows の系のプロキシ設定（レジストリ Internet Settings）"
    elif s["proxy_env"]:
        where = "環境変数 " + "/".join(sorted(s["proxy_env"]))
    else:
        where = "この環境のプロキシ設定"
    return (f"宛先 {s['target']} への要求は **プロキシ {s['proxy']} 経由**になります"
            f"（出どころ: {where}）。LAN の推論機はプロキシを通すと届きません——"
            "推論機側には要求が 1 件も届かないまま待ち続けます。"
            "環境変数 NO_PROXY にその宛先を入れるか、"
            "アプリ側でプロキシを使わない設定にしてください。")


def append_target(text: str, url: str, *, getproxies: Any = None,
                  environ: dict | None = None) -> str:
    """推論機の失敗文に「**待っていた先**」を 1 行足す。

    ★「推論機が応答しません」だけでは、宛先が正しいのか、宛先へ着いていないのかが
      利用者にも開発側にも分からない。実機（2026-09-06）はまさにこれで、推論機の
      ログは空のまま「応答しません」だけが出ていた。
    """
    try:
        line = describe(url, getproxies=getproxies, environ=environ)
    except Exception:
        return text
    body = str(text or "")
    if line in body:
        return body
    return (body + ("\n" if body and not body.endswith("\n") else "") + "▸ " + line)
