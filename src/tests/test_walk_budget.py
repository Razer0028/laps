"""歩く道具（Glob / Read の推測・双子探し）が**必ず戻る**こと。

★出どころ（2026-09-06・owner 実機 P0）: Windows の作業場 `D:\\Blackwall` で
  「現在の作業フォルダ内にあるゲームを調べてください」と頼んだところ、画面の
  道具欄が `(running)` のまま止まり、推論機（推論機）は idle・GPU 0 だった
  ＝**止まっていたのは推論ではなく道具の実行**。

★この試験が守るのは 1 点だけ:「走査が遅い機械でも、道具は時間で戻る」。
  速さは測らない（速さを測る試験は機械の busy 具合で落ちるので嘘の赤を出す）。
  代わりに**走査そのものを遅くして**、本番の関数がそれでも返ることを見る。

★なぜ件数の上限では足りなかったか（この試験が在る理由そのもの）:
  `Path.rglob` が返すのは**一致したものだけ**。`**/*.exe` のように一致が
  0 件のまま何万ものディレクトリを降りる形では、ループの中身が一度も
  実行されない＝件数を数える行にも、時計を見る行にも**到達しない**。
  「上限は書いてあるが、止めたい経路には繋がっていない」——この現場の常習。
"""
from __future__ import annotations

import os
import pathlib
import time

import pytest

from laps.tools import limits
from laps.tools.glob import _run as glob_run
from laps.tools import read as read_mod


# ---------------------------------------------------------------------------
# 予算そのもの
# ---------------------------------------------------------------------------

def test_予算は切れた瞬間を覚える() -> None:
    w = limits.Walk(0.01)
    assert w.expired() is False or w.hit is True     # 直後は普通まだ切れていない
    time.sleep(0.05)
    assert w.expired() is True
    assert w.hit is True


def test_ゼロ以下は無制限(monkeypatch) -> None:
    for v in ("0", "-1"):
        monkeypatch.setenv("LAPS_WALK_SECS", v)
        w = limits.Walk()
        time.sleep(0.01)
        assert w.expired() is False
        assert w.hit is False


def test_環境変数で変えられる_壊れた値は既定へ(monkeypatch) -> None:
    monkeypatch.setenv("LAPS_WALK_SECS", "2.5")
    assert limits.walk_secs() == 2.5
    monkeypatch.setenv("LAPS_WALK_SECS", "いいかげんな値")
    assert limits.walk_secs() == limits.WALK_SECS
    monkeypatch.delenv("LAPS_WALK_SECS", raising=False)
    assert limits.walk_secs() == limits.WALK_SECS


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("y", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("z", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAPS_POLICY", "off")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    return tmp_path


def _slow_rglob(seconds: float, yields=()):
    """`Path.rglob` の差し替え。**一致を返さないまま**時間を食う形を作る。

    これが owner 実機で起きた形（`**/*.exe` が木を歩ききるまで返らない）。
    """
    def rglob(self, pattern, *a, **kw):
        for item in yields:
            yield item
        time.sleep(seconds)
    return rglob


def test_一致が0件でも時間で必ず戻る(ws, monkeypatch) -> None:
    # ★件数の上限（_ENUM_MAX）は一致した数しか数えないので、この形では
    #   永久に発火しない。時間だけが唯一の出口。
    monkeypatch.setattr(pathlib.Path, "rglob", _slow_rglob(30.0))
    monkeypatch.setenv("LAPS_WALK_SECS", "0.3")
    t0 = time.monotonic()
    out = glob_run({"pattern": "**/*.exe", "path": str(ws)})
    took = time.monotonic() - t0
    assert took < 10.0, f"30 秒の走査に付き合ってしまった（{took:.1f} 秒）"
    assert "打ち切り" in str(out), out


def test_時間切れの0件を_no_matches_と言わない(ws, monkeypatch) -> None:
    """★いちばん高くつく壊れ方。「0 件」はモデルにとって『無い』の証拠になる。

    この道具は 2026-08-27 に一度この形（探索の失敗を 0 件として返す）を踏んでおり、
    モデルは「/srv/packages は空だ」という**誤った確信**を持った。
    時間切れは 0 件ではない。
    """
    monkeypatch.setattr(pathlib.Path, "rglob", _slow_rglob(30.0))
    monkeypatch.setenv("LAPS_WALK_SECS", "0.2")
    out = str(glob_run({"pattern": "**/*.exe", "path": str(ws)}))
    assert "(no matches)" not in out
    assert "0 件ではありません" in out


def test_途中まで集まった分は返す(ws, monkeypatch) -> None:
    got = [ws / "a.py", ws / "sub" / "c.py"]
    monkeypatch.setattr(pathlib.Path, "rglob", _slow_rglob(30.0, yields=got))
    monkeypatch.setenv("LAPS_WALK_SECS", "0.3")
    out = str(glob_run({"pattern": "**/*.py", "path": str(ws)}))
    assert "a.py" in out and "c.py" in out
    assert "打ち切り" in out          # 全部見たとは言わない


def test_普通の走査は今までどおり(ws, monkeypatch) -> None:
    """★境界を足したせいで**普通の場合が変わっていない**ことを見る
    （速くするための変更ではないので、結果は 1 文字も変わってはいけない）。"""
    monkeypatch.setenv("LAPS_WALK_SECS", "30")
    # ★走査の対象は専用の木にする（作業場の直下は、監査ログ置き場 `bwhome/` が
    #   最初の道具呼び出しで**作られる**ので、呼ぶ前と後で中身が変わる）。
    tree = ws / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.py").write_text("x", encoding="utf-8")
    (tree / "b.txt").write_text("y", encoding="utf-8")
    (tree / "sub" / "c.py").write_text("z", encoding="utf-8")
    ws = tree
    # ★期待値は**素の rglob**から作る（試験が本番の綴りを写すと、本番だけ
    #   壊れていても緑になる——この現場が 2026-08-30 に踏んだ形）。
    #   ここで比べているのは「境界を足す前と後で、返るものが同じか」。
    for pattern in ("*", "**/*.py", "**/*"):
        want = sorted(str(p) for p in ws.rglob(pattern)
                      if not any(part in (".git", "node_modules", "__pycache__",
                                          ".venv", ".mypy_cache", ".pytest_cache")
                                 for part in p.parts))
        got = str(glob_run({"pattern": pattern, "path": str(ws)}))
        assert "打ち切り" not in got, (pattern, got)
        assert got.splitlines() == want, (pattern, got, want)


def test_走査が例外で落ちても道具は返る(ws, monkeypatch) -> None:
    def boom(self, pattern, *a, **kw):
        raise OSError("ネットワークドライブが切れました")
        yield                                   # pragma: no cover
    monkeypatch.setattr(pathlib.Path, "rglob", boom)
    out = str(glob_run({"pattern": "**/*", "path": str(ws)}))
    assert "走査に失敗" in out and "ネットワークドライブ" in out


# ---------------------------------------------------------------------------
# Read（失敗したときの「近いもの」と、成功したときの「同名の別ファイル」）
# ---------------------------------------------------------------------------

def _slow_walk(per_dir: float = 0.02, dirs: int = 5000):
    """**ディレクトリばかりでファイルが少ない**深い木を模す。

    ★実機の形はこれ（1 回の `scandir` が遅いのではなく、速い `scandir` が
      何万回も続く）。`len(names) > 4000` / `seen > _TWIN_SCAN_MAX` という
      **ファイル数**の上限は、この木では一度も届かない。
    """
    def walk(top, *a, **kw):
        for i in range(dirs):
            time.sleep(per_dir)
            yield (os.path.join(str(top), f"d{i}"), [], [])
    return walk


def test_近いものの探索は時間で戻る(ws, monkeypatch) -> None:
    """★Read が失敗する**たび**に通る道。実機の記録では「想像したパス」で
    2 回続けて失敗している＝この道を 2 回通っている。"""
    import os as _os
    monkeypatch.setattr(_os, "walk", _slow_walk())
    monkeypatch.setenv("LAPS_WALK_SECS", "0.3")
    t0 = time.monotonic()
    read_mod._did_you_mean(str(ws / "存在しない.py"))
    took = time.monotonic() - t0
    assert took < 10.0, f"探索が返らなかった（{took:.1f} 秒）"


def test_同名の別ファイル探しは時間で戻る(ws, monkeypatch) -> None:
    """★こちらは読み取りが**成功する**たびに通る道。固まると
    「読めたのに返ってこない」になる。"""
    monkeypatch.setattr(os, "walk", _slow_walk())
    monkeypatch.setenv("LAPS_WALK_SECS", "0.3")
    t0 = time.monotonic()
    read_mod._same_name_elsewhere(ws / "a.py")
    took = time.monotonic() - t0
    assert took < 10.0, f"探索が返らなかった（{took:.1f} 秒）"
