"""道具の結果が「成功／失敗／0 件」をきちんと名乗る（第三者レビュー E5・E7・E10・E12）。

2026-08-31 に、全 22 道具の失敗経路を実際に呼んで測った: **22/22 が素の str** を返し、
成否も種類も `tool_failed` の語彙表からの推測だった。推測が外れている実例が同じ日に出た:

  ・ApplyDiff `Applied 0/1 file(s)`（1 本も当たっていない）→ **ok=True**
  ・`(no tasks)` / `(no results for …)` → kind="ok"
    （grounding は EMPTY_SENTINELS で「0 件」と読むのに、こちらは「ok」＝
      **同じ文字列を 2 つの層が別のものとして扱っていた**）
  ・TaskCreate はセッション未設定でも `OK: task #1 created` と答え、
    直後の TaskList が `(no tasks)`（作った直後に消える課題）
  ・WebSearch は「本当に 0 件」と「頁を読めなかった」を同じ文字列にしていた
"""
from __future__ import annotations

import json
import pathlib

import pytest

from laps.tools import all_specs, as_result, EMPTY_SENTINELS
from laps.tools import apply_diff as ad
from laps.tools import task_mgmt as tm
from laps.tools import websearch as ws


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


# ── E10: 0 件の判定は 1 か所で決める ─────────────────────────────

@pytest.mark.parametrize("sentinel", EMPTY_SENTINELS)
def test_0件の綴りはすべてemptyと読まれる(sentinel):
    r = as_result(sentinel + ("'q')" if sentinel.endswith("for") else ""))
    assert r.ok is True and r.kind == "empty", f"{sentinel!r} が {r.kind}"


def test_失敗と0件と成功が取り違えられない():
    assert as_result("Error: なにか").kind == "error"
    assert as_result("ふつうの出力").kind == "ok"
    assert as_result("(no matches)  ※注記つき").kind == "empty"
    # 0 件の綴りで**始まらない**行が混ざれば、それは 0 件ではない
    assert as_result("(no matches)\n1 件見つかりました").kind == "ok"


def test_必須引数を落とした呼び出しは失敗と読まれる():
    """★「まだ推測している箇所」を数え続けるための土台。ここが崩れると、
    失敗がモデルの読む文字列にも記録にも出なくなる（2026-08-28 の監査と同じ穴）。

    ⚠ 対象は **schema が `required` と言っている道具だけ**。TodoWrite / TaskList は
      引数なしが正しい呼び方（今の一覧を返す）で、これを失敗と決めつけた最初の版は
      **他の試験がセッションを設定したかどうかで結果が変わる**、順序に依存する試験だった。
    """
    bad = []
    for s in all_specs():
        if not s.schema.get("required"):
            continue
        r = as_result(s.handler({}))
        if r.ok:
            bad.append((s.name, sorted(s.schema["required"]), str(r)[:60]))
    assert not bad, f"必須引数が無いのに成功を返す道具: {bad}"


# ── E5: 1 本も当たらなかった diff は失敗 ─────────────────────────

def _block(path, old, new):
    return f"{path}\n<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE\n"


def test_全滅した差分は失敗として返る(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n", encoding="utf-8")
    r = as_result(ad._run({"diff": _block(p, "NOT PRESENT", "z")}))
    assert r.ok is False and r.kind == "error", str(r)[:200]
    assert p.read_text(encoding="utf-8") == "x = 1\n"


def test_一部だけ当たった差分は半端であることを言う(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 2\n", encoding="utf-8")
    r = as_result(ad._run({"diff": _block(a, "x = 1", "x = 99") + "\n" + _block(b, "NOPE", "z")}))
    assert r.ok is False
    assert "1/2" in str(r) and "一部" in str(r), str(r)[:300]
    assert a.read_text(encoding="utf-8") == "x = 99\n"      # 当たった分は残る
    assert b.read_text(encoding="utf-8") == "y = 2\n"


def test_途中の拒否でも書き換えた分を報告する(tmp_path, monkeypatch):
    """★旧実装は policy 拒否で即 return し、**既に書き換えたファイルの話が消えていた**。
    モデルは「何も起きなかった」と読み、同じ差分を出し直す。"""
    inside = tmp_path / "in"
    inside.mkdir()
    outside = tmp_path / "out"
    outside.mkdir()
    a, b = inside / "a.py", outside / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 2\n", encoding="utf-8")
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", str(inside))
    out = str(ad._run({"diff": _block(a, "x = 1", "x = 99") + "\n" + _block(b, "y = 2", "y = 99")}))
    assert a.read_text(encoding="utf-8") == "x = 99\n"
    assert str(a) in out, f"書き換えたファイルが報告に出ていない: {out}"
    assert "policy denied" in out
    assert b.read_text(encoding="utf-8") == "y = 2\n"


def test_全部当たった差分は成功のまま(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n", encoding="utf-8")
    r = as_result(ad._run({"diff": _block(p, "x = 1", "x = 99")}))
    assert r.ok is True and r.kind == "ok"
    assert (r.data or {}).get("patches"), "画面向けの差分が消えている"


# ── E7: 保存できないのに「作成しました」と言わない ───────────────

def _no_session(monkeypatch, tmp_path):
    monkeypatch.delenv("LAPS_SESSION_ID", raising=False)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))


@pytest.mark.parametrize("fn", [tm._create, tm._list_tasks, tm._get, tm._update, tm._stop])
def test_セッションが無いときは断る(monkeypatch, tmp_path, fn):
    _no_session(monkeypatch, tmp_path)
    r = as_result(fn({"subject": "s", "task_id": "1"}))
    assert r.ok is False, f"保存できないのに成功と答えた: {str(r)[:80]}"
    assert "LAPS_SESSION_ID" in str(r)


def test_セッションがあれば今まで通り(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_SESSION_ID", "s1")
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    assert "OK: task #1 created" in str(tm._create({"subject": "工程1"}))
    assert "工程1" in str(tm._list_tasks({}))
    assert "updated" in str(tm._update({"task_id": "1", "status": "completed"}))
    saved = json.loads((tmp_path / "sessions" / "s1" / "tasks.json").read_text())
    assert saved[0]["status"] == "completed"


def test_読めない課題ファイルを黙って上書きしない(monkeypatch, tmp_path):
    """★空リストを返すと、次の TaskCreate が #1 から採番して**読めなくなっただけの
    課題表を確実に消す**。"""
    monkeypatch.setenv("LAPS_SESSION_ID", "s1")
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    p = tmp_path / "sessions" / "s1" / "tasks.json"
    p.parent.mkdir(parents=True)
    p.write_text("{ broken", encoding="utf-8")
    r = as_result(tm._create({"subject": "新"}))
    assert r.ok is False and "読めません" in str(r)
    assert p.read_text(encoding="utf-8") == "{ broken", "壊れた記録を上書きした"


# ── E12: 0 件と「読めなかった」を分ける ──────────────────────────

class _Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200


class _Client:
    def __init__(self, text):
        self._t = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *_a, **_k):
        return _Resp(self._t)


_ZERO_PAGE = '<html><div class="no-results">No results.</div></html>'
_UNREADABLE = "<html>" + "<div class='x'>結果らしきものが山ほど</div>" * 200 + "</html>"


def _search(monkeypatch, page):
    monkeypatch.setattr(ws.httpx, "Client", lambda *a, **k: _Client(page))
    return as_result(ws._run({"query": "q"}))


def test_本当に0件は0件と言う(monkeypatch):
    r = _search(monkeypatch, _ZERO_PAGE)
    assert r.ok is True and r.kind == "empty"
    assert str(r).startswith("(no results for")


def test_読めなかった頁を0件と言わない(monkeypatch):
    """★実測: lite 版の頁（22KB・結果はぎっしり）を解析 0 件で受けると、
    旧実装は本当の 0 件と**同じ文字列**を返していた。"""
    r = _search(monkeypatch, _UNREADABLE)
    assert r.ok is False and r.kind == "error"
    assert "0 件" in str(r)


def test_正常な結果は今まで通り(monkeypatch):
    page = ('<div class="result"><a class="result__a" href="https://e.example/">題</a>'
            '<div class="result__snippet">抜粋</div></div>')
    r = _search(monkeypatch, page)
    assert r.ok is True and r.kind == "ok"
    assert "https://e.example/" in str(r)
