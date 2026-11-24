"""逐次配信で届く道具呼び出しの併合。

★2026-08-18 の調査で判明（llama.cpp #18591 の直撃）:
  ある版は**複数の呼び出しを全部 `index: 0`** で送ってくる。index だけを鍵に併合すると

    名前  : 上書きされて最後のものだけ残る
    引数  : `{"a":1}{"b":2}` と連結されて JSON として壊れる
          → `__unparsed_arguments__` のまま実行へ渡る

  結果は「道具が空振りした」ように見え、記録上は成功と区別がつかない。

★名前は呼び出しごとに1度しか来ないので、区切りに使える。
  index が正しく振られているサーバでは名前は別 index へ届くので、この規則は発火しない
  ＝**正しい側を壊さない**。その確認もここで行う（負の対照）。
"""
from __future__ import annotations

import json

import laps.backend as backend_mod


def _merge(deltas: list[dict]) -> list[dict]:
    """★実装そのものを呼ぶ。規則を試験側に写すと、写しだけが古びて気づけない。

    HTTP は張らない。ここで測りたいのは**併合規則**であって通信ではない。
    """
    acc: dict = {}
    done: list = []
    for delta in deltas:
        backend_mod.merge_tool_call_deltas(delta.get("tool_calls") or [], acc, done)
    return backend_mod.finish_tool_calls(acc, done)


def _d(index, name=None, args=None):
    fn = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    return {"tool_calls": [{"index": index, "function": fn}]}


BUGGY = [  # 2呼び出しが**両方 index 0** で来る（#18591）
    _d(0, "Read", '{"file_path":'),
    _d(0, None, ' "a.txt"}'),
    _d(0, "Read", '{"file_path":'),
    _d(0, None, ' "b.txt"}'),
]

CORRECT = [  # index が正しく振られている
    _d(0, "Read", '{"file_path":'),
    _d(0, None, ' "a.txt"}'),
    _d(1, "Bash", '{"command":'),
    _d(1, None, ' "ls"}'),
]


def test_同じindexで来た2呼び出しを分けられる() -> None:
    calls = _merge(BUGGY)
    assert len(calls) == 2, f"連結されて1本に潰れている: {calls}"
    assert [c["input"]["file_path"] for c in calls] == ["a.txt", "b.txt"]


def test_壊れた引数が生まれない() -> None:
    """★これが直前の症状。連結された `{…}{…}` は JSON として読めない。"""
    for c in _merge(BUGGY):
        assert "__unparsed_arguments__" not in c["input"], c


def test_旧規則なら壊れることの確認_負の対照() -> None:
    """★直った側だけ見ても「元から壊れていなかった」のか区別できない。"""
    acc: dict = {}
    for delta in BUGGY:
        for tc in delta["tool_calls"]:
            slot = acc.setdefault(tc.get("index", 0), {"name": "", "args": []})
            fn = tc["function"]
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["args"].append(fn["arguments"])
    raw = "".join(acc[0]["args"])
    assert len(acc) == 1, "旧規則で分かれている＝この対照が成立していない"
    try:
        json.loads(raw)
        raise AssertionError(f"旧規則でも読めてしまう＝症状が再現していない: {raw}")
    except json.JSONDecodeError:
        pass  # 期待どおり壊れる


def test_正しいサーバは壊さない() -> None:
    """★修理が正しい側を巻き込まないこと。ここが落ちるなら入れてはいけない。"""
    calls = _merge(CORRECT)
    assert [c["name"] for c in calls] == ["Read", "Bash"], calls
    assert calls[0]["input"] == {"file_path": "a.txt"}
    assert calls[1]["input"] == {"command": "ls"}


def test_単一呼び出しは分裂しない() -> None:
    calls = _merge([_d(0, "Bash", '{"command": "l'), _d(0, None, 's -la"}')])
    assert len(calls) == 1 and calls[0]["input"] == {"command": "ls -la"}


def test_引数の無い呼び出しは空辞書になる() -> None:
    calls = _merge([_d(0, "SystemState")])
    assert calls == [{"name": "SystemState", "input": {}}]


def test_配信経路が同じ関数を使っている() -> None:
    """★「書いたが繋いでいない」を防ぐ。規則を直しても呼ばれていなければ意味がない。"""
    import inspect

    src = inspect.getsource(backend_mod.Backend._chat_with_tools_stream)
    assert "merge_tool_call_deltas(" in src, "配信経路が併合関数を呼んでいない"
    assert "finish_tool_calls(" in src, "配信経路が確定関数を呼んでいない"
