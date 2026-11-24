"""環境変数の名前の読み替え（`BLACKWALL_*` ⇄ `LAPS_*`・laps/_envcompat.py・2026-11-14）。

本体は `LAPS_*` しか読まない。旧い `BLACKWALL_*` だけを設定している環境（systemd の unit・利用者の機械）でも
動くことは、子プロセスを起こして確かめる（写しは `laps` の import のとき 1 回だけなので、この試験の
プロセスの中で setenv しても確かめられない）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from laps import _envcompat

SRC = str(Path(__file__).resolve().parents[1])


def _child(extra: dict[str, str], code: str) -> dict:
    """新旧どちらの名前も持たない環境に extra だけを足して、子で code を走らせる。code は JSON を 1 行出す。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith((_envcompat.NEW, _envcompat.OLD))}
    env["PYTHONPATH"] = SRC
    env.update(extra)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


_DUMP = ("import os, json, {mod}; "
         "print(json.dumps({{k: v for k, v in os.environ.items() if k.endswith('_ZZ_PROBE')}}))")


def test_旧い名前だけを渡すと新しい名前で読める():
    got = _child({"BLACKWALL_ZZ_PROBE": "1"}, _DUMP.format(mod="laps"))
    assert got == {"BLACKWALL_ZZ_PROBE": "1", "LAPS_ZZ_PROBE": "1"}


def test_新しい名前だけを渡すと旧い名前にも写る():
    got = _child({"LAPS_ZZ_PROBE": "new"}, _DUMP.format(mod="laps"))
    assert got == {"BLACKWALL_ZZ_PROBE": "new", "LAPS_ZZ_PROBE": "new"}


def test_両方を渡すとLAPSが勝つ():
    got = _child({"BLACKWALL_ZZ_PROBE": "old", "LAPS_ZZ_PROBE": "new"}, _DUMP.format(mod="laps"))
    assert got == {"BLACKWALL_ZZ_PROBE": "new", "LAPS_ZZ_PROBE": "new"}


def test_空文字もそのまま写る():
    got = _child({"BLACKWALL_ZZ_PROBE": ""}, _DUMP.format(mod="laps"))
    assert got == {"BLACKWALL_ZZ_PROBE": "", "LAPS_ZZ_PROBE": ""}
    # 空の LAPS も「在る」＝旧い名前の値に負けない
    got = _child({"BLACKWALL_ZZ_PROBE": "old", "LAPS_ZZ_PROBE": ""}, _DUMP.format(mod="laps"))
    assert got == {"BLACKWALL_ZZ_PROBE": "", "LAPS_ZZ_PROBE": ""}


def test_旧い包みblackwallから入っても写る():
    got = _child({"BLACKWALL_ZZ_PROBE": "1"}, _DUMP.format(mod="blackwall"))
    assert got.get("LAPS_ZZ_PROBE") == "1"


def test_importのときに環境変数を読む部品にも効く():
    """写しは `laps/__init__.py` のいちばん最初。部品（ここでは serve の作業場の解決）が読む前に済んでいる。"""
    code = ("import os, json; from laps import procutil; "
            "print(json.dumps({'ws': os.environ.get('LAPS_WORKSPACE')}))")
    assert _child({"BLACKWALL_WORKSPACE": "/tmp/zz-old"}, code) == {"ws": "/tmp/zz-old"}
    assert _child({"BLACKWALL_WORKSPACE": "/tmp/zz-old", "LAPS_WORKSPACE": "/tmp/zz-new"}, code) == {"ws": "/tmp/zz-new"}


def test_applyは渡した辞書だけを触る():
    env = {"BLACKWALL_A": "1", "LAPS_B": "2", "BLACKWALL_C": "old", "LAPS_C": "new", "PATH": "/bin",
           "BLACKWALL_": "x", "LAPS_": "y"}
    _envcompat.apply(env)
    assert env == {"BLACKWALL_A": "1", "LAPS_A": "1", "LAPS_B": "2", "BLACKWALL_B": "2",
                   "BLACKWALL_C": "new", "LAPS_C": "new", "PATH": "/bin", "BLACKWALL_": "x", "LAPS_": "y"}
    again = dict(env)
    _envcompat.apply(again)
    assert again == env                                  # 何度呼んでも同じ


def test_書く所と消す所は両方の名前(monkeypatch):
    env: dict[str, str] = {}
    _envcompat.setenv("LAPS_X", "1", env)
    _envcompat.setenv("BLACKWALL_Y", "2", env)
    _envcompat.setenv("Z", "3", env)
    assert env == {"LAPS_X": "1", "BLACKWALL_X": "1", "LAPS_Y": "2", "BLACKWALL_Y": "2", "LAPS_Z": "3", "BLACKWALL_Z": "3"}
    assert _envcompat.setdefault("LAPS_X", "9", env) == "1"        # 在ればそのまま
    assert _envcompat.setdefault("LAPS_W", "4", env) == "4"
    assert env["BLACKWALL_W"] == "4"
    # 新しい名前だけが在る（試験の monkeypatch.setenv など）→ setdefault が旧い名前にも写す
    env2 = {"LAPS_V": "5"}
    assert _envcompat.setdefault("LAPS_V", "0", env2) == "5" and env2["BLACKWALL_V"] == "5"
    assert _envcompat.unsetenv("LAPS_X", env) == "1"
    assert "LAPS_X" not in env and "BLACKWALL_X" not in env
    assert _envcompat.unsetenv("LAPS_X", env) is None
    # 既定は os.environ
    monkeypatch.delenv("LAPS_ZZ_PROBE", raising=False)
    monkeypatch.delenv("BLACKWALL_ZZ_PROBE", raising=False)
    _envcompat.setenv("LAPS_ZZ_PROBE", "on")
    assert os.environ["BLACKWALL_ZZ_PROBE"] == "on"
    _envcompat.unsetenv("LAPS_ZZ_PROBE")
    assert "LAPS_ZZ_PROBE" not in os.environ and "BLACKWALL_ZZ_PROBE" not in os.environ


def test_消した名前は子で戻らない():
    """片方だけ消すと、子の import のときに旧い名前から値が戻る。両方消してあれば戻らない。"""
    env = {"LAPS_ZZ_PROBE": "1", "BLACKWALL_ZZ_PROBE": "1"}
    _envcompat.unsetenv("LAPS_ZZ_PROBE", env)
    assert _child(env, _DUMP.format(mod="laps")) == {}
