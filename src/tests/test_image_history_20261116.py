"""古い画像を文字に替えるとき、会話の初めの方の画像は替えない（2026-11-16 オーナー「Desktop を使った後の手番で急に最初から読み直す」）。

推論機 の記録: 写真を撮った後の手番で会話が 300 トークン縮み、17.2 万トークンをほぼ頭から約 6 分読み直した。古い 3 枚を文字に替えたため。
"""
from __future__ import annotations

from laps.engine.llm import Info
from laps.engine.session import Transcript
from tests.test_engine_20261030 import env  # noqa: F401


def _setup(env, monkeypatch, tmp_path):  # noqa: F811
    a, *_ = env([])
    monkeypatch.setattr(a.llm, "info", lambda **k: Info(template="", llama=True))
    a.t.append({"role": "user", "content": "look at the screen"})
    shots = []
    for i in range(12):
        p = tmp_path / f"shot{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]) * 64)
        shots.append(str(p))
    return a, shots


def _step(a, k: int, out: str = "ok", shot: str = "") -> None:
    a.t.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{k}", "type": "function", "function": {"name": "Tools", "arguments": "{}"}}]})
    a.t.append({"role": "tool", "tool_call_id": f"c{k}", "content": out})
    if shot:
        a.t.append({"role": "user", "content": f"[image] {shot}", "bw_image": shot, "bw_note": True})


def _sent_images(sent: list[dict]) -> list[str]:
    out = []
    for m in sent:
        if isinstance(m.get("content"), list):
            out.append(next(p["text"] for p in m["content"] if p.get("type") == "text"))
    return out


def _first_diff(x: list[dict], y: list[dict]) -> int:
    return next((i for i, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)))


def test_far_picture_stays_a_picture_and_the_start_is_not_read_again(env, monkeypatch, tmp_path):  # noqa: F811
    a, shots = _setup(env, monkeypatch, tmp_path)
    _step(a, 0, shot=shots[0])
    for k in range(1, 6):                                  # 写真の後に長い仕事（約 3 万トークン）
        _step(a, k, out="x" * 24000)
    for k in range(1, 5):
        _step(a, 10 + k, shot=shots[k])
    before = a._send_messages()
    assert len(_sent_images(before)) == 5
    _step(a, 20, shot=shots[5])                            # 6 枚目: 古い 3 枚の行き先を決める
    after = a._send_messages()
    sent = _sent_images(after)
    assert sent == [f"[image] {s}" for s in (shots[0], shots[3], shots[4], shots[5])]
    pos0 = next(i for i, m in enumerate(before) if isinstance(m.get("content"), list))
    assert _first_diff(before, after) > pos0 + 5            # 変わるのは近い 2 枚の所から（遠い 1 枚の前後は同じ）
    assert a.counters.get("images_to_text") == 2 and a.counters.get("images_kept_far") == 1
    assert a._send_messages() == after                     # 決めた事は変えない


def test_near_pictures_still_go_to_text_in_steps(env, monkeypatch, tmp_path):  # noqa: F811
    a, shots = _setup(env, monkeypatch, tmp_path)
    counts = []
    for k in range(9):
        _step(a, k, shot=shots[k])
        counts.append(len(_sent_images(a._send_messages())))
    assert counts == [1, 2, 3, 4, 5, 3, 4, 5, 3]


def test_decisions_survive_a_restart(env, monkeypatch, tmp_path):  # noqa: F811
    a, shots = _setup(env, monkeypatch, tmp_path)
    _step(a, 0, shot=shots[0])
    _step(a, 1, out="y" * 60000)
    for k in range(1, 6):
        _step(a, 1 + k, shot=shots[k])
    a._send_messages()
    again = Transcript(a.t.id)
    assert [m.get("bw_image") for m in again.view()] == [m.get("bw_image") for m in a.t.view()]
    assert sum(1 for m in again.view() if m.get("bw_image")) == 4


def test_conversation_from_the_older_version_keeps_its_shape(env, monkeypatch, tmp_path):  # noqa: F811
    """前の版は印を残さず、送るたびに枚数だけで決めていた。前の要求で文字だった画像を画像に戻すと、そこから読み直しになる。"""
    a, shots = _setup(env, monkeypatch, tmp_path)
    _step(a, 0, shot=shots[0])
    _step(a, 1, out="z" * 60000)
    for k in range(1, 7):
        _step(a, 1 + k, shot=shots[k])                     # 前の要求の時点で 7 枚＝前の版は 0〜2 を文字にしていた
    _step(a, 9, shot=shots[7])
    sent = _sent_images(a._send_messages())
    assert f"[image] {shots[0]}" not in sent and f"[image] {shots[3]}" in sent


def test_summary_turns_the_old_pictures_into_text(env, monkeypatch, tmp_path):  # noqa: F811
    a, shots = _setup(env, monkeypatch, tmp_path)
    _step(a, 0, shot=shots[0])
    _step(a, 1, out="w" * 60000)
    for k in range(1, 6):
        _step(a, 1 + k, shot=shots[k])
    assert len(_sent_images(a._send_messages())) == 4
    a._settle_images(all_old=True)
    assert _sent_images(a._send_messages()) == [f"[image] {s}" for s in shots[3:6]]
