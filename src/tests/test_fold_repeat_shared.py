"""同一結果の畳みを **1 本**にまとめ、Glob / SystemState にも掛ける。

★なぜ直したか（2026-09-02・8 走行 10,904 件の実測）:
  「道具＋引数＋本文がバイト単位で一致した反復」が 2,397 件 = 22.0%（2.3 MB）
  あったのに、畳みは **Bash にしか無かった**（`tools/bash.py` の中に private
  実装として置かれていた）。内訳は Read 1,438/3,402・SystemState 132/192・
  Glob 129/369・Bash 473/3,614。この現場の持病「片方で直して片方に残す」が
  そのままの形で残っていた。

  本番のこの関数を 8 走行に掛け直すと Glob 57 件・331,453 B、
  SystemState 70 件・21,146 B が畳めた（走行ごとに記憶を消して測った）。

★Read には掛けない。畳めた Read と直前の同一 Read の間隔は中央値 24 呼び出し
  （連続は 1,179 件中 3 件）で、常時圧縮の窓では元の全文が既に落ちている
  ＝再読は「文脈から消えた中身の取り戻し」。畳むと中身に戻れなくなる。
  この試験は **read.py が畳みを呼んでいないこと**も門にする。
"""
from __future__ import annotations

import inspect
import subprocess

import pytest

from laps.tools import bash as bash_mod
from laps.tools import fold
from laps.tools import glob as glob_mod
from laps.tools import read as read_mod
from laps.tools import system_state as ss_mod


@pytest.fixture(autouse=True)
def _clean() -> None:
    fold.reset_repeat_memory()
    yield
    fold.reset_repeat_memory()


def _body(n: int = 200) -> str:
    return "\n".join(f"line {i}" for i in range(n))


# --------------------------------------------------------------------------
# 共通の 1 本であること（同じ物を 2 つ持たない）
# --------------------------------------------------------------------------

def test_畳みの実装は一箇所だけ() -> None:
    """★bash.py に実装が残っていたら、また片方だけ直すことになる。"""
    src = inspect.getsource(bash_mod)
    assert "_OUT_SEEN: dict" not in src, "bash.py に記憶の実体が残っている"
    assert "hashlib.sha1" not in src, "bash.py に同一判定の実体が残っている"
    # 3 つの道具が同じ 1 本を呼んでいる（定義だけして繋いでいない、を弾く）
    for mod in (bash_mod, glob_mod, ss_mod):
        assert "fold.fold_repeat" in inspect.getsource(mod), \
            f"{mod.__name__} が共通の畳みを呼んでいない"
    # 呼び出し側（_run）から辿れること
    assert "fold.fold_repeat" in inspect.getsource(glob_mod._run)
    assert "fold.fold_repeat" in inspect.getsource(ss_mod._run)


def test_readには掛けない() -> None:
    """★測って**やめた**ほう。無エラーの再読 605 件が危険母数（本文の冒頭参照）。"""
    assert "fold_repeat" not in inspect.getsource(read_mod), \
        "Read に畳みが入っている（文脈認識なしでは中身に戻れなくなる）"


def test_bashの互換名は同じ記憶を指す() -> None:
    """再生スクリプトと既存試験が `bash._OUT_SEEN` 等を参照している。"""
    assert bash_mod._OUT_SEEN is fold._OUT_SEEN
    assert bash_mod._ERRISH is fold.ERRISH
    assert bash_mod._REPEAT_MIN_BYTES == fold.REPEAT_MIN_BYTES
    body = _body()
    bash_mod._fold_repeat("ls", "/tmp", body, body)
    assert bash_mod._repeat_key("ls", "/tmp") in fold._OUT_SEEN
    bash_mod._reset_repeat_memory()
    assert not fold._OUT_SEEN


def test_道具が違えば混ざらない() -> None:
    """★鍵に道具名が無いと、Bash の `ls` と Glob の `.` が同じ鍵でぶつかる。"""
    body = _body()
    assert fold.fold_repeat("Bash", "same", None, body, body) == body
    assert fold.fold_repeat("Glob", "same", None, body, body) == body, \
        "別の道具の初回なのに畳まれた（鍵が混ざっている）"
    assert fold.fold_repeat("Glob", "same", None, body, body).startswith("(前回と同じ出力")


def test_三回目の文面は道具ごとに自然になる(monkeypatch) -> None:
    """★「このコマンド」で固定すると、Glob / SystemState で日本語が壊れる。

    本番の経路を通す（道具側が `what=` を渡し忘れていたら落ちる）。
    """
    class _P:
        returncode = 0
        stdout = ("/dev/sda1  100G  50G  50G  50% /\n" * 12).encode()
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    for _ in range(2):
        ss_mod._run({"verb": "disk"})
    third = str(ss_mod._run({"verb": "disk"}))
    assert "この点検は同じ結果を 3 回返しています" in third

    body = _body()
    for _ in range(2):
        bash_mod._fold_repeat("ls", "/tmp", body, body)
    assert "このコマンドは同じ結果を 3 回返しています" in \
        bash_mod._fold_repeat("ls", "/tmp", body, body)


def test_失敗の理由は畳んでも残る() -> None:
    """★run44 の実害（畳みが理由を隠し、同じコマンドを 197 回打った）の再発防止。

    Glob / SystemState でも同じ約束であることを、共通の 1 本で確かめる。
    """
    body = ("Error: policy denied: /srv/notes/secret\n"
            + "\n".join(f"row {i}" for i in range(80)))
    for tool, args in (("Glob", ".|**/*.md"), ("SystemState", "failed_units")):
        fold.reset_repeat_memory()
        first = fold.fold_repeat(tool, args, None, body, body)
        assert first == body, "初回は全文"
        second = fold.fold_repeat(tool, args, None, body, body)
        assert "前回と同じ出力" in second
        assert "policy denied" in second, f"{tool}: 失敗の理由が消えている"


# --------------------------------------------------------------------------
# 道具を実際に 2 回呼ぶ（本番と同じ 1 本の経路を通す。殻にしない）
# --------------------------------------------------------------------------

def test_globは二回目を畳む(tmp_path) -> None:
    for i in range(60):
        (tmp_path / f"file_{i:03d}.txt").write_text("x")
    inp = {"path": str(tmp_path), "pattern": "*.txt"}
    first = glob_mod._run(inp)
    assert "file_000.txt" in str(first), "初回は一覧が出る"
    second = str(glob_mod._run(inp))
    assert second.startswith("(前回と同じ出力"), "2 回目が畳まれていない"
    assert fold.nbytes(second) < fold.nbytes(str(first))


def test_globは結果が変われば畳まない(tmp_path) -> None:
    for i in range(60):
        (tmp_path / f"file_{i:03d}.txt").write_text("x")
    inp = {"path": str(tmp_path), "pattern": "*.txt"}
    glob_mod._run(inp)
    (tmp_path / "new.txt").write_text("x")
    second = str(glob_mod._run(inp))
    # ★「new.txt が本文に出ること」を条件にしない。予算しだいで一覧側の畳みが
    #   働き、末尾のファイルは正当に省かれる（別の機構）。ここで見たいのは
    #   **反復の畳みが働かないこと**の一点。
    assert not second.startswith("(前回と同じ出力"), "内容が変わったのに畳んだ"


def test_systemstateは二回目を畳む(monkeypatch) -> None:
    class _P:
        returncode = 0
        stdout = ("/dev/sda1  100G  50G  50G  50% /\n" * 12).encode()
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    first = str(ss_mod._run({"verb": "disk"}))
    assert "/dev/sda1" in first
    second = str(ss_mod._run({"verb": "disk"}))
    assert second.startswith("(前回と同じ出力"), "2 回目が畳まれていない"
    assert "[SystemState disk] exit=0" in second, "何の結果かは残す"


def test_systemstateは項目が違えば畳まない(monkeypatch) -> None:
    class _P:
        returncode = 0
        stdout = ("same body line\n" * 30).encode()
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    ss_mod._run({"verb": "disk"})
    out = str(ss_mod._run({"verb": "memory"}))
    assert "same body line" in out, "別の項目なのに畳んだ"


def test_回数は1から数える() -> None:
    """★移設中に見つけた既存の欠陥（fold.py の該当箇所を参照）。

    旧実装は初回に「畳み関数が呼ばれた**通算回数**」を回数として保存していた。
    同じコマンドしか打たない試験では通算＝回数なので気づけない。本番では
    他の呼び出しが 5 回あった後に初めて反復すると、2 回目なのに
    「同じ結果を 7 回返しています。別の手を打ってください」と出る。
    行き詰まっていない場で行き詰まりの合図を出す＝間違った助言。
    """
    body = _body()
    for i in range(5):
        fold.fold_repeat("Bash", f"cmd{i}", None, body, body)   # 別の鍵で通算を進める
    assert fold.fold_repeat("Bash", "target", None, body, body) == body
    second = fold.fold_repeat("Bash", "target", None, body, body)
    assert "返しています" not in second, "2 回目なのに行き詰まりの合図が出ている"
    assert "同じ結果を 3 回返しています" in \
        fold.fold_repeat("Bash", "target", None, body, body)


def test_空出力には畳みを掛けない() -> None:
    """★空出力の担当は `_seen_empty`（初回は全文・2 回目からは 1 行＋回数）。

    両方掛けると、どちらが勝つかが**文の長さ差**で決まる。実際に全体試験が
    捕まえた形: 畳みの文言を 1 行縮めただけで勝者が入れ替わり、初回の定型文
    （command / cwd / shell / 次の一手）が「(前回と同じ出力…)」に置き換わった。
    空振りに対して次の一手を出すのは、実測で再試行を止めた仕組みなので消せない。
    """
    import os

    os.environ.pop("LAPS_BASH_TERSE", None)
    bash_mod._EMPTY_SEEN.clear()
    for _ in range(3):
        # 反復の記憶を汚しても（本番では別の走行の残りに相当）初回は全文のまま
        first = str(bash_mod.SPEC.handler({"command": "true"}))
        assert "cwd:" in first or "回目です" in first
        assert not first.startswith("(前回と同じ出力"), "空出力に畳みが掛かっている"
        bash_mod._EMPTY_SEEN.clear()
