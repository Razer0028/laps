"""道具の引数と、辿れない道の**言い方**（2026-09-02）。

★どちらも「利用者に理由が届かない」型。
  ・素の `int(...)` は例外になり、届くのは agent.py が包んだ
    `Error in Read: invalid literal for int() with base 10: 'all'` だけ。
    どの引数の話かも、何を渡せばよいかも書かれていない。
    同じ形は `notebook_edit.py` と `bash_output.py` で**既に直されていた**
    ＝片方で直して片方に残した。
  ・負の値は例外にならず `lines[-5:]` が**末尾から読む**。
    「読めたのに中身が違う」＝いちばん気づけない形。
  ・壊れた記号リンクは `exists()` が False なので「file not found」になるが、
    リンクは在るのだから**嘘**で、直し方にも辿り着けない。

★実測での出現は 0 件（走行 8 本・実機セッション 471 本）。
  頻度ではなく「起きたときに何も分からない」ことを直している。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.tools.read import _run as read_run  # noqa: E402
from laps.tools.bash import _run as bash_run  # noqa: E402
from laps.tools.grep import _run as grep_run  # noqa: E402


def _t(o):
    return o if isinstance(o, str) else o.text


@pytest.fixture()
def sample(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\nl3\n")
    return f


@pytest.mark.parametrize("bad", ["all", "2k", "", None, 1.5, True])
def test_read_limit_that_is_not_a_number(sample, bad):
    out = _t(read_run({"file_path": str(sample), "limit": bad}))
    if bad in ("", None):
        assert out.startswith("# ")        # 既定に落ちる（従来どおり読める）
    else:
        assert out.startswith("Error:") and "limit" in out, out


def test_read_negative_offset_is_refused_not_silently_wrong(sample):
    """`lines[-5:]` は例外を出さずに**末尾から**読む。黙って別の答えを返さない。"""
    out = _t(read_run({"file_path": str(sample), "offset": -5}))
    assert out.startswith("Error:") and "負の値" in out


def test_read_still_works_with_normal_args(sample):
    out = _t(read_run({"file_path": str(sample), "offset": 1, "limit": 1}))
    assert "l2" in out and "l1" not in out.split("\n", 1)[1]


def test_broken_symlink_is_named_as_such(tmp_path):
    link = tmp_path / "link.conf"
    os.symlink(str(tmp_path / "gone.conf"), str(link))
    out = _t(read_run({"file_path": str(link)}))
    assert "記号リンク" in out, out
    assert "file not found" not in out          # リンクは在る＝嘘を言わない
    assert "ln -sfn" in out                     # 次の一手


def test_bash_timeout_that_is_not_a_number():
    out = _t(bash_run({"command": "echo x", "timeout": "5m"}))
    assert out.startswith("Error:") and "timeout" in out


def test_bash_timeout_zero_is_not_unlimited():
    """0 を「上限なし」のつもりで渡すと、実際には**即座に打ち切られる**。"""
    out = _t(bash_run({"command": "echo x", "timeout": 0}))
    assert out.startswith("Error:") and "上限なし" in out


def test_grep_max_results_that_is_not_a_number(tmp_path):
    out = _t(grep_run({"pattern": "x", "path": str(tmp_path), "max_results": "all"}))
    assert out.startswith("Error:") and "max_results" in out
