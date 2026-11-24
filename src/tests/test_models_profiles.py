"""銘柄表 — 複数のモデルを名前で切り替える（2026-09-17・オーナー指示）。

★「マルチモデル互換にして欲しい。kimi とか使ってみたい」。

ここで守るのは 3 つ:
  ① **外に出る銘柄は fail-closed**。エージェントは作業場のファイルを読んで文脈に載せるので、
     外部 API を選ぶことは**その中身を外へ送ること**と同じ。知らずに選べてはいけない。
  ② **方言**。llama.cpp は未知のキーを黙って捨てるが、厳しい API は 400 を返す。
  ③ **窓は銘柄が持つ**。32K のつもりで 256K を使う（逆も）を構造で消す。
     窓の見積りは既に最大 9,168 tok ずれているので、ここを推測に任せない。
"""
from __future__ import annotations

import pytest

from laps import models as M


def test_外に出る銘柄は許可が無ければ断る(monkeypatch):
    monkeypatch.delenv("LAPS_ALLOW_EXTERNAL", raising=False)
    monkeypatch.setenv("MOONSHOT_API_KEY", "dummy")
    with pytest.raises(M.NotAllowed) as e:
        M.get("kimi-k2")
    # ★断り文は「何が起きるか」を言うこと（禁止の宣言ではなく、判断の材料）
    assert "外へ" in str(e.value) or "外部" in str(e.value)


def test_許可があっても鍵が無ければ断る(monkeypatch):
    monkeypatch.setenv("LAPS_ALLOW_EXTERNAL", "1")
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    with pytest.raises(M.NotAllowed):
        M.get("kimi-k2")


def test_ラボ内は許可なしで使える(monkeypatch):
    monkeypatch.delenv("LAPS_ALLOW_EXTERNAL", raising=False)
    p = M.get("local-coder")
    assert p.n_ctx == 32_768 and p.dialect == "llamacpp"


def test_方言に合わないキーは送らない():
    """★llama.cpp 用の独自キーを OpenAI 互換 API に送ると 400 になる。"""
    ラボ = M.sampling_for(M.PROFILES["local-coder"])
    外 = M.sampling_for(M.PROFILES["kimi-k2"])
    assert "repeat_penalty" in ラボ and "top_k" in ラボ
    for k in ("repeat_penalty", "repetition_penalty", "repeat_last_n", "top_k"):
        assert k not in 外, f"openai 方言に独自キー {k} を送ろうとしている"


def test_窓は銘柄が持つ():
    """★`LAPS_N_CTX` の渡し忘れで取り違えない。"""
    assert M.PROFILES["local-coder"].n_ctx != M.PROFILES["kimi-k2"].n_ctx
    for p in M.PROFILES.values():
        assert p.n_ctx > 0


def test_鍵の値を持たない():
    """★銘柄表は**鍵の名前**だけを持つ。値をソースに置かない。"""
    import inspect
    src = inspect.getsource(M)
    for 疑い in ("sk-", "api_key=\"", "MOONSHOT_API_KEY=\""):
        assert 疑い not in src.replace('"sk-no-key-required"', ""), f"鍵らしきものがある: {疑い}"


def test_一覧に鍵の値を出さない(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "秘密の値")
    out = M.describe()
    assert "秘密の値" not in out
    assert "鍵あり" in out          # 有無だけを言う


def test_backendが銘柄から窓を入れる(monkeypatch):
    """★渡し忘れの取り違えを構造で消していること。"""
    for k in ("LAPS_N_CTX", "LAPS_PROFILE", "LAPS_BACKEND", "LAPS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    from laps.backend import _from_profile
    cfg = _from_profile()
    import os
    assert os.environ.get("LAPS_N_CTX") == "32768"
    assert cfg["model"] == "default"
