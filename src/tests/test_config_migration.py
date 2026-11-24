"""古い既定が設定ファイルに焼き付いたまま残る問題（2026-08-29 実機で発覚）。

0.1.134 で既定を `trusted/raw` にしたのに、**一度でも起動した機械には当時の既定が
書かれた config.toml が残り、そちらが勝っていた**。＝オーナーの
「全コマンド使用を許可・倫理的制限をすべて排除」が既存の導入先に届いていなかった。
画面は `workspace/standard` と**正しく**表示していたので、そのぶん気づきにくい。
"""
from __future__ import annotations

import tomllib

import pytest

from laps.term import config


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    """★conftest が全試験に `LAPS_POLICY=workspace` を差している。

    環境変数は設定ファイルより強いので、外さないと**移行そのものを測れない**
    （一度これで外し、「利用者の選択を上書きした」という嘘の失敗を見た）。
    """
    for v in ("LAPS_POLICY", "LAPS_ETHICS"):
        monkeypatch.delenv(v, raising=False)


def _write(p, **kv):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f'{k} = "{v}"' for k, v in kv.items()), encoding="utf-8")


def test_昔の既定のままなら今の既定に移す(tmp_path):
    p = tmp_path / "config.toml"
    _write(p, policy="workspace", ethics="standard")
    cfg = config.resolve(path=p)
    assert cfg["policy"] == config.DEFAULTS["policy"]      # 公開版の既定は workspace
    assert cfg["ethics"] == "raw"
    assert config.migrated, "移行したことを画面に出す材料が無い"
    assert tomllib.loads(p.read_text(encoding="utf-8"))["config_version"] == config.CONFIG_VERSION


def test_利用者が自分で選んだ値は動かさない(tmp_path):
    """★ここが肝。黙って権限を広げてよいのは「昔の既定のまま」のときだけ。"""
    p = tmp_path / "config.toml"
    _write(p, policy="readonly", ethics="strict")
    cfg = config.resolve(path=p)
    assert cfg["policy"] == "readonly", "利用者の選択を上書きした"
    assert cfg["ethics"] == "strict"
    assert not config.migrated


def test_片方だけ昔の既定でも動かさない(tmp_path):
    """組で一致したときだけ＝「触っていない」と言える。片方でも違えば触っている。"""
    p = tmp_path / "config.toml"
    _write(p, policy="workspace", ethics="raw")
    cfg = config.resolve(path=p)
    assert cfg["policy"] == "workspace"
    assert not config.migrated


def test_二度目は移さない(tmp_path):
    p = tmp_path / "config.toml"
    _write(p, policy="workspace", ethics="standard")
    config.resolve(path=p)
    # 移行後に利用者が戻したなら、それは利用者の選択なので二度と動かさない
    cfg = config.load(path=p)
    cfg["policy"] = "workspace"
    config.save(cfg, path=p)
    out = config.resolve(path=p)
    assert out["policy"] == "workspace", "二度目の移行で利用者の選択を上書きした"
    assert not config.migrated


def test_移行したら画面に出る(tmp_path, monkeypatch):
    """★「書いたが繋いでいない」を作らない: 移行の事実が**実際に描かれる**こと。"""
    from laps.term import app as app_mod
    p = tmp_path / "config.toml"
    _write(p, policy="workspace", ethics="standard")
    config.resolve(path=p)
    assert config.migrated
    from rich.console import Console
    con = Console(record=True, width=100, force_terminal=False)

    class _Ctx:
        console = con
        cfg = {"model": "m", "backend": "http://x/v1", "policy": "trusted",
               "ethics": "raw", "tools": "full"}
        phases = False
    monkeypatch.setattr(app_mod, "header_line", lambda c: "BLACKWALL")
    monkeypatch.setattr(app_mod, "is_first_run", lambda c: False)
    app_mod._header(_Ctx())
    out = con.export_text()
    assert "設定を今の既定に合わせました" in out, "移行したのに画面に出ていない"
    assert "policy" in out and "ethics" in out
