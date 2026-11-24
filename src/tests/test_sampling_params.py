"""生成の設定を1箇所で決める（web 調査の指摘・2026-08-19）。

★それまで**三重帳簿**だった:
    ① Rust の sampling.rs が 0.6 / 0.95 / 20（Qwen3 の **thinking 版**の値）
    ② Python は **temperature しか送っていなかった**
    ③ 結果として **サーバ既定が黙って効いていた**（実測 /props:
       top_p 0.95 / top_k 40 / min_p 0.05）

  実際に効いていた③は**コードのどこにも書かれていない**ので、読んでも気づけない。
  誰も top_k 40 を選んでいない。

★採る値はモデル作者の公開推奨（Qwen3-Coder-30B-A3B-Instruct）。
★**推奨値が手元で良いという測定はまだ無い**。直したのは「選んでいない値が
  効いていた」ことだけ。env で上書きして A/B できる形にしてある。
"""
from __future__ import annotations

import inspect

import laps.backend as backend_mod


def test_公式の推奨値が既定(monkeypatch) -> None:
    for k in ("LAPS_TOP_P", "LAPS_TOP_K", "LAPS_REPEAT_PENALTY"):
        monkeypatch.delenv(k, raising=False)
    p = backend_mod.sampling_params()
    assert p["top_p"] == 0.8, "Qwen3-Coder の推奨は 0.8（0.95 はサーバ既定／別モデルの値）"
    assert p["top_k"] == 20, "推奨は 20（40 はサーバ既定）"
    assert p["repetition_penalty"] == 1.05
    # ★llama.cpp 側の綴りも必ず載せる（2026-09-09）。
    #   `repetition_penalty` は HF / vLLM の綴りで、llama.cpp は `repeat_penalty`。
    #   推論機 で実測: {"repetition_penalty":1.77} → /slots は repeat_penalty=1.0（捨てられた）／
    #                {"repeat_penalty":1.66}    → /slots は 1.66（効いた）。
    #   ＝この綴りが無いあいだ、意図した 1.05 は**一度も効いていなかった**。
    assert p["repeat_penalty"] == 1.05, "llama.cpp は repeat_penalty を見る（この綴りが無いと黙って捨てられる）"
    assert p["repeat_penalty"] == p["repetition_penalty"], "2つの綴りが食い違うと、相手によって効き方が変わる"


def test_envで上書きできる(monkeypatch) -> None:
    """★A/B できない変更は入れない。"""
    monkeypatch.setenv("LAPS_TOP_P", "0.95")
    monkeypatch.setenv("LAPS_TOP_K", "40")
    p = backend_mod.sampling_params()
    assert p["top_p"] == 0.95 and p["top_k"] == 40


def test_壊れた値は既定に戻す(monkeypatch) -> None:
    """★設定の誤字で走行が死なない（生成設定は毎回通る経路）。"""
    monkeypatch.setenv("LAPS_TOP_P", "たかめ")
    assert backend_mod.sampling_params()["top_p"] == 0.8


def test_全ての送信経路に載っている() -> None:
    """★「書いたが繋いでいない」を防ぐ。どれかで落ちると、
    その経路だけサーバ既定に戻り、**経路によって挙動が変わる**（いちばん追いにくい形）。

    ★3 → 4 に増やした（2026-09-09）。`_chat_stream` だけが抜けていた。
      出荷経路は NATIVE_TOOLS=1 なのでそこは本番に届かないが、
      **測定を汚していた**: tests/test_pushback_text_reaches_model.py:77 が対照腕を
      NATIVE_TOOLS=0 で切っているため、その腕だけ top_p/top_k がサーバ既定に戻り、
      腕の差にサンプリング差が交絡していた。さらに同じ走行の中でも逐次(_chat_stream)と
      例外時の一括フォールバック(chat)でサンプリングが違い、**途中で腕が入れ替わっていた**。
    """
    src = inspect.getsource(backend_mod)
    n = src.count("**sampling_params(),")
    assert n == 4, f"送信経路 4 箇所のうち {n} 箇所にしか載っていない"


def test_temperatureは各経路の引数のまま() -> None:
    """★temperature はターンごとに変わりうる（呼び出し側が決める）ので、
    ここに畳み込まない。畳むと呼び出し側の指定が黙って無視される。"""
    p = backend_mod.sampling_params()
    assert "temperature" not in p


def test_temperature_is_single_source_and_env_overridable(monkeypatch):
    """★温度も1箇所。三重帳簿を畳んだとき**温度だけ畳み忘れていた**。

    畳んだ範囲を「サンプリング設定」と呼んで温度をその外側だと思ったのが原因。
    agent.py に `temperature=0.7` が直書きで残らないことも併せて見る。
    """
    from pathlib import Path
    from laps import backend
    monkeypatch.delenv("LAPS_TEMPERATURE", raising=False)
    assert backend.temperature() == 0.7
    monkeypatch.setenv("LAPS_TEMPERATURE", "0.05")
    assert backend.temperature() == 0.05
    monkeypatch.setenv("LAPS_TEMPERATURE", "ごみ")
    assert backend.temperature() == 0.7          # 壊れた値は既定へ倒す

    src = (Path(backend.__file__).resolve().parent / "agent.py").read_text()
    assert "temperature=0.7" not in src, "agent.py に温度が直書きで残っている"


def test_seed_is_absent_unless_set(monkeypatch):
    """★常に種を載せない。載せると出荷時の振る舞いが決定論になり、
    利用者が同じ質問をし直しても同じ答えしか返らなくなる。"""
    from laps import backend
    monkeypatch.delenv("LAPS_SEED", raising=False)
    assert "seed" not in backend.sampling_params()


def test_seed_is_used_when_set(monkeypatch):
    from laps import backend
    monkeypatch.setenv("LAPS_SEED", "4242")
    assert backend.sampling_params()["seed"] == 4242
    monkeypatch.setenv("LAPS_SEED", "ごみ")
    assert "seed" not in backend.sampling_params()   # 壊れた値は載せない
