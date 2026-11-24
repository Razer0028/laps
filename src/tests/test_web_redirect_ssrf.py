"""WebFetch: 転送先も必ず検査すること（2026-09-02）。

★見つけ方: `_is_private_url` の docstring が
  "Resolves DNS to catch dynamic redirects to internal IPs" と書いていたのに、
  呼び出し側は `follow_redirects=True` で httpx に転送を任せており、
  **検査は最初の URL 1 本にしか掛かっていなかった**。
  外向きの URL が 302 で `169.254.169.254`（クラウドのメタデータ＝一時資格情報）や
  社内の管理画面へ飛ぶ経路は素通りする。
  ★事実に反する註釈は、無い註釈より悪い——読んだ人が守られていると信じて、
    そこを検査しなくなる。

★未検証と正直に書いておくこと: 実機の記録に WebFetch の呼び出しは 0 件で、
  この穴が**実際に踏まれた記録は無い**。直した理由は
  「起きたら資格情報が出ていく」種類だから（起きてから測る類ではない）。
"""
from __future__ import annotations

import httpx
import pytest

from laps.tools import webfetch as W


def _client(handler):
    """httpx.Client を差し替え、転送の連鎖を自前で組み立てる。"""
    class _FakeClient:
        def __init__(self, **kw):
            # ★転送を httpx に任せていないこと自体を主張する（任せると
            #   ここが True になり、行き先を 1 段ずつ検査できない）。
            assert kw.get("follow_redirects") is False, "転送を httpx に任せている"

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url): return handler(url)
    return _FakeClient


class _Resp:
    def __init__(self, status, headers, body=b""):
        self.status_code = status
        self.headers = headers
        self._body = body
        self.encoding = "utf-8"

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def iter_bytes(self):
        yield self._body

    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://127.0.0.1:8900/query",
    "http://192.0.2.5/admin",
])
def test_内部アドレスへの転送を辿らない(monkeypatch, target) -> None:
    def handler(url):
        if url == "https://example.invalid.test/go":
            return _Resp(302, {"location": target})
        raise AssertionError(f"転送先を取りに行った: {url}")

    monkeypatch.setattr(W.httpx, "Client", _client(handler))
    monkeypatch.setattr(W, "_is_private_url",
                        lambda u: (False, "") if "example.invalid.test" in u
                        else (True, "private (RFC1918): x"))
    out = W._run({"url": "https://example.invalid.test/go"})
    assert out.startswith("Error:")
    # ★どこから飛ばされたかを言うこと（原因が消えると「読めない URL」で終わる）
    assert "転送されました" in out


def test_外向きの転送は普通に辿る(monkeypatch) -> None:
    seen = []

    def handler(url):
        seen.append(url)
        if url.endswith("/go"):
            return _Resp(302, {"location": "/final"})
        return _Resp(200, {"content-type": "text/plain"}, "ぶじ".encode("utf-8"))

    monkeypatch.setattr(W.httpx, "Client", _client(handler))
    monkeypatch.setattr(W, "_is_private_url", lambda u: (False, ""))
    out = W._run({"url": "https://example.invalid.test/go"})
    assert "ぶじ" in out
    assert seen == ["https://example.invalid.test/go",
                    "https://example.invalid.test/final"]
    # 見出しは**実際に読んだ**URL を出す（最初の URL のままだと出典が嘘になる）
    assert "URL: https://example.invalid.test/final" in out


def test_転送が終わらないときは打ち切る(monkeypatch) -> None:
    monkeypatch.setattr(W.httpx, "Client",
                        _client(lambda url: _Resp(302, {"location": "/loop"})))
    monkeypatch.setattr(W, "_is_private_url", lambda u: (False, ""))
    out = W._run({"url": "https://example.invalid.test/loop"})
    assert "転送が" in out and out.startswith("Error:")


def test_名前が引けないのを内部アドレス扱いで説明しない(monkeypatch) -> None:
    """原因が消える要約をしない。DNS の失敗を「内部だから塞いだ」と言わない。"""
    monkeypatch.setattr(W, "_is_private_url",
                        lambda u: (True, "DNS lookup failed: [Errno -2]"))
    out = W._run({"url": "https://nonexistent.invalid.test/"})
    assert "名前を解決できません" in out
    assert "blocked private" not in out


def test_読めない数値の扱いは2つの道具で同じ規則(monkeypatch) -> None:
    """★同じ問いに別々の答えを持たない（危うく片方は Error・片方は既定に落とす、
    という食い違いを自分で書きかけた）。規則は「仕事は続ける・黙って変えない」。"""
    from laps.tools import websearch as S

    assert W.num_or_default("30s", 30.0, "timeout")[0] == 30.0
    assert "読めない" in W.num_or_default("30s", 30.0, "timeout")[1]
    assert W.num_or_default(None, 30.0, "timeout") == (30.0, "")
    assert W.num_or_default("45", 30.0, "timeout")[0] == 45.0
    # websearch も**同じ関数**を使っていること（写しを持たない）
    assert S.num_or_default is W.num_or_default


def test_読めないtimeoutでも取得を諦めない(monkeypatch) -> None:
    def handler(url):
        return _Resp(200, {"content-type": "text/plain"}, "ほんぶん".encode("utf-8"))

    monkeypatch.setattr(W.httpx, "Client", _client(handler))
    monkeypatch.setattr(W, "_is_private_url", lambda u: (False, ""))
    out = W._run({"url": "https://example.invalid.test/", "timeout": "30s"})
    assert "ほんぶん" in out
    assert "既定" in out, "黙って別の値で走っている"
