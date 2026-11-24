"""Windows のプロキシで「推論機に 1 件も届かないまま heartbeat だけ」になる型の回帰。

事故（2026-09-06・owner 実機 0.1.194）:
  「推論機の応答を待っています（N 秒）」だけが出続け、推論機（LAN の llama-server）の
  ss / ログ / GPU には **要求が 1 件も届いていなかった**。Linux（同じソース）では正常。

機構（この試験が固定する事実）:
  1. httpx の `trust_env=True`（既定）は `urllib.request.getproxies()` を読む。
  2. CPython の `getproxies()` は **`os.name == "nt"` の枝だけ**
     `getproxies_environment() or getproxies_registry()` ——環境変数が空なら
     **レジストリ**（Internet 設定 ProxyEnable/ProxyServer）を読む。
  3. その辞書に除外リスト（レジストリの ProxyOverride）は**入らない**ので、
     httpx から見た `no_proxy` は空＝「ローカル アドレスにはプロキシを使わない」の
     チェックを入れていても **192.0.2.160 も 127.0.0.1 もプロキシ送り**になる。
  ⇒ Linux では同じコードが素通しになるので、**Linux でいくら走らせても再現しない**。

守っているもの:
  ・`backend` / `context_budget` の httpx は既定でプロキシを尊重（`_trust_env()` True）・`LAPS_HTTP_TRUST_ENV=0` で直結。
  ・見る設定に戻したときは、宛先がプロキシへ回ることを**画面と記録に必ず出す**
    （`netdiag` → serve の `backend_proxy` notice・`run_start` の `net`・失敗本文の 1 行）。
"""
from __future__ import annotations

import io
import json
import os

import pytest

from laps import netdiag


# --- 1. httpx が見る辞書の読み方（Windows の枝を Linux 上で写して確かめる） ------

def test_proxy_applies_when_the_environment_has_one():
    gp = lambda: {"http": "http://px.example:8080", "https": "http://px.example:8080"}
    assert netdiag.proxy_applies("http://192.0.2.160:8741/v1", getproxies=gp) is True


def test_no_proxy_entry_bypasses_the_target():
    gp = lambda: {"http": "http://px.example:8080", "no": "192.0.2.160,localhost"}
    assert netdiag.proxy_applies("http://192.0.2.160:8741/v1", getproxies=gp) is False


def test_star_in_no_proxy_disables_everything():
    gp = lambda: {"http": "http://px.example:8080", "no": "*"}
    assert netdiag.proxy_applies("http://192.0.2.160:8741/v1", getproxies=gp) is False


def test_registry_style_dict_has_no_bypass_list_so_even_loopback_is_proxied():
    """★これが Windows 固有の壊れ方の核。`getproxies_registry()` は `no` を返さない
    ので、除外の意図（ProxyOverride）は httpx に**一切伝わらない**。"""
    registry_like = lambda: {"http": "http://px.corp:8080", "https": "http://px.corp:8080"}
    assert netdiag.proxy_applies("http://127.0.0.1:8760/v1", getproxies=registry_like) is True


def test_cpython_reads_the_registry_on_windows_only():
    """前提の固定: `getproxies` が OS 依存であること自体を、CPython の原文で確かめる
    （この前提が崩れたら、この試験群の意味が変わる）。"""
    import inspect
    import urllib.request
    src = inspect.getsource(urllib.request)
    assert "getproxies_environment() or getproxies_registry()" in src
    # レジストリ側は 'no' キーを組み立てない＝httpx から見た除外は常に空。
    reg = src.split("def getproxies_registry(")[1].split("def getproxies(")[0]
    assert "'no'" not in reg and '"no"' not in reg


# --- 2. 値を伏せる（プロキシ URL は資格情報を埋められる形） ---------------------

def test_userinfo_is_never_printed():
    line = netdiag.describe(
        "http://192.0.2.160:8741/v1",
        getproxies=lambda: {"http": "http://" + "bob" + ":" + "hunter2" + "@px:8080"},
        environ={},
    )
    assert "hunter2" not in line and "bob" not in line
    assert "***@px:8080" in line


# --- 3. Windows だと分かる形で名乗る -------------------------------------------

def test_summary_names_the_registry_when_env_is_empty_on_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    s = netdiag.summary("http://192.0.2.160:8741/v1",
                        getproxies=lambda: {"http": "http://px.corp:8080"},
                        environ={})
    assert s["proxy_applies"] is True
    assert s["proxy_from_registry"] is True
    assert "レジストリ" in netdiag.describe(
        "http://192.0.2.160:8741/v1",
        getproxies=lambda: {"http": "http://px.corp:8080"}, environ={})


def test_env_vars_are_named_when_they_are_the_source():
    s = netdiag.summary("http://192.0.2.160:8741/v1",
                        getproxies=lambda: {"http": "http://px.corp:8080"},
                        environ={"HTTP_PROXY": "http://px.corp:8080"})
    assert s["proxy_env"] == {"HTTP_PROXY": "http://px.corp:8080"}
    assert s["proxy_from_registry"] is False


# --- 4. 既定でプロキシを見ない（本番の関数そのものを呼ぶ） ----------------------

def test_backend_and_context_budget_trust_proxy_env_by_default_but_can_disable(monkeypatch):
    from laps import backend as backend_mod
    from laps import context_budget as cb_mod
    monkeypatch.delenv("LAPS_HTTP_TRUST_ENV", raising=False)
    # ★2026-09-06 訂正: 既定は True（proxy を尊重）。proxy 経由でしか推論機に届かない機械
    #   （owner 実機 利用者の PC）を壊さないため。0 で明示的に直結へ落とせる。
    assert backend_mod._trust_env() is True
    assert cb_mod._trust_env() is True
    monkeypatch.setenv("LAPS_HTTP_TRUST_ENV", "0")
    assert backend_mod._trust_env() is False
    assert cb_mod._trust_env() is False


def test_the_real_client_is_built_trusting_the_environment_by_default(monkeypatch):
    """★「書いたが繋いでいない」を止める: `_trust_env()` を読むだけでなく、
    実際に作られる `httpx.Client` に渡っていることを見る。"""
    import httpx
    from laps import backend as backend_mod

    seen: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            seen.update(kw)
        is_closed = False

    monkeypatch.delenv("LAPS_HTTP_TRUST_ENV", raising=False)
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    b = backend_mod.Backend(backend_mod.BackendConfig(base_url="http://192.0.2.160:8741/v1"))
    assert seen.get("trust_env") is True, f"httpx.Client に渡っていない: {seen!r}"
    del b


# --- 5. 黙って待たせない（serve の口と記録に必ず出る） --------------------------







def test_backend_failure_text_says_where_it_was_waiting(monkeypatch):
    """「推論機が応答しません」だけでは、宛先が違うのか相手が死んでいるのかが
    利用者にも開発側にも分からない（実機はまさにこれで丸一日溶けた）。"""
    monkeypatch.setattr(netdiag, "_getproxies",
                        lambda getproxies=None: {"http": "http://px.corp:8080"})
    out = netdiag.append_target("[backend error] 推論機が応答しません",
                                "http://192.0.2.160:8741/v1")
    assert "192.0.2.160:8741" in out
    assert "プロキシ" in out
    # 2 回足さない（差し戻しの往復で同じ行が積み上がらない）。
    assert netdiag.append_target(out, "http://192.0.2.160:8741/v1") == out
