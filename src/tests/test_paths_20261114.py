"""置き場の名前の規則（laps/paths.py・2026-11-14）。

    1. new が在れば new
    2. new が無く old が在れば old（旧い置き場を使い続ける。new は作らない）
    3. どちらも無ければ new

置き場ごとに 3 通り＋両方ある場合（new が勝つ）。old だけのとき new が作られないこと。
環境変数で場所を明示したときはその値が勝つこと。Rust（app/src-tauri/src/paths.rs）と同じ規則。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from laps import paths


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """利用者のフォルダを一時フォルダへ（実際の ~/.blackwall・~/.config/blackwall に触らない）。"""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for k in ("LAPS_HOME", "BLACKWALL_HOME", "XDG_CONFIG_HOME", "LAPS_AUTH_FILE", "BLACKWALL_AUTH_FILE",
              "LAPS_SESSIONS", "BLACKWALL_SESSIONS", "LAPS_TOOLS_DIR", "BLACKWALL_TOOLS_DIR"):
        monkeypatch.delenv(k, raising=False)
    assert Path.home() == h
    return h


# ── pick そのもの ─────────────────────────────────────────────────────────

def test_pick_どちらも無ければ_new(tmp_path):
    assert paths.pick(tmp_path / "n", tmp_path / "o") == tmp_path / "n"
    assert not (tmp_path / "n").exists() and not (tmp_path / "o").exists()      # 何も作らない


def test_pick_old_だけなら_old_で_new_は作らない(tmp_path):
    (tmp_path / "o").mkdir()
    assert paths.pick(tmp_path / "n", tmp_path / "o") == tmp_path / "o"
    assert not (tmp_path / "n").exists()


def test_pick_new_だけなら_new(tmp_path):
    (tmp_path / "n").mkdir()
    assert paths.pick(tmp_path / "n", tmp_path / "o") == tmp_path / "n"


def test_pick_両方あれば_new(tmp_path):
    (tmp_path / "n").mkdir()
    (tmp_path / "o").mkdir()
    assert paths.pick(tmp_path / "n", tmp_path / "o") == tmp_path / "n"


def test_pick_は覚えない(tmp_path):
    """片方が走行の途中で作ったフォルダを、すぐ見られる（Rust が置き、Python が読む）。"""
    assert paths.pick(tmp_path / "n", tmp_path / "o") == tmp_path / "n"
    (tmp_path / "o").mkdir()
    assert paths.pick(tmp_path / "n", tmp_path / "o") == tmp_path / "o"
    (tmp_path / "n").mkdir()
    assert paths.pick(tmp_path / "n", tmp_path / "o") == tmp_path / "n"


# ── 置き場ごと（4 通り）────────────────────────────────────────────────────

def _cases(home: Path, ws: Path):
    """（名前, 関数, new, old）。"""
    return [
        ("dot_dir", paths.dot_dir, home / ".laps", home / ".blackwall"),
        ("home_dir", paths.home_dir, home / ".laps", home / ".blackwall"),
        ("config_dir", paths.config_dir, home / ".config" / "laps", home / ".config" / "blackwall"),
        ("config_dir(xdg=False)", lambda: paths.config_dir(xdg=False), home / ".config" / "laps", home / ".config" / "blackwall"),
        ("workspace_dir", lambda: paths.workspace_dir(ws), ws / ".laps", ws / ".blackwall"),
    ]


@pytest.mark.parametrize("i", range(5))
def test_置き場ごとの規則(home, tmp_path, i):
    ws = tmp_path / "ws"
    ws.mkdir()
    name, fn, new, old = _cases(home, ws)[i]
    # どちらも無い → new（作らない）
    assert fn() == new, name
    assert not new.exists() and not old.exists(), name
    # old だけ → old（new は作らない）
    old.mkdir(parents=True)
    assert fn() == old, name
    assert not new.exists(), name
    # 両方 → new
    new.mkdir(parents=True)
    assert fn() == new, name
    # new だけ → new
    old.rmdir()
    assert fn() == new, name


def test_workspace_name_と文の合わせ(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert paths.workspace_name(ws) == ".laps"
    assert paths.ws_text("go in .laps/FINDINGS.md", ws) == "go in .laps/FINDINGS.md"
    (ws / ".blackwall").mkdir()
    assert paths.workspace_name(ws) == ".blackwall"
    assert paths.ws_text("go in .laps/FINDINGS.md", ws) == "go in .blackwall/FINDINGS.md"
    assert not (ws / ".laps").exists()


# ── 環境変数で明示したときはその値が勝つ ────────────────────────────────────

def test_LAPS_HOME_が勝つ(home, tmp_path, monkeypatch):
    (home / ".blackwall").mkdir()
    (home / ".laps").mkdir()
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "elsewhere"))
    assert paths.home_dir() == tmp_path / "elsewhere"
    assert paths.dot_dir() == home / ".laps"                  # dot_dir は環境変数を見ない
    from laps import session
    assert session.state_dir() == tmp_path / "elsewhere"


def test_XDG_CONFIG_HOME(home, tmp_path, monkeypatch):
    x = tmp_path / "xdg"
    (x / "blackwall").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(x))
    assert paths.config_dir() == x / "blackwall"              # 旧い置き場を使い続ける
    assert not (x / "laps").exists()
    assert paths.config_dir(xdg=False) == home / ".config" / "laps"   # 認証情報は XDG を見ない（Rust の auth.rs と同じ）
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")    # 相対は「未設定」
    assert paths.config_dir() == home / ".config" / "laps"


def test_LAPS_AUTH_FILE_が勝つ(home, tmp_path, monkeypatch):
    from laps import relayauth
    (home / ".config" / "blackwall").mkdir(parents=True)
    assert relayauth.auth_file() == home / ".config" / "blackwall" / "auth.json"
    monkeypatch.setenv("LAPS_AUTH_FILE", str(tmp_path / "a.json"))
    assert relayauth.auth_file() == tmp_path / "a.json"


# ── 使う部品が規則を通っていること（作る所も）────────────────────────────────

def test_旧い利用者の置き場を使い続ける(home):
    """~/.blackwall と ~/.config/blackwall が在る機械: 会話・ログイン・設定はそこから読み、新しい名前は作られない。"""
    old = home / ".blackwall"
    (old / "sessions").mkdir(parents=True)
    cfg = home / ".config" / "blackwall"
    cfg.mkdir(parents=True)
    (cfg / "auth.json").write_text('{"token": "t", "email": "a@b"}', encoding="utf-8")
    from laps import relayauth, session, settings
    from laps.oc import construct, lessons, remember, toolenv
    from laps.term import config as tconfig
    assert session.state_dir() == old                          # ここは mkdir する所
    assert relayauth.auth_file() == cfg / "auth.json"
    assert settings._settings_path() == old / "settings.json"
    assert construct.db_path() == old / "construct.db"
    assert lessons.store_path() == old / "lessons.jsonl"
    assert remember._cfg() == cfg
    assert toolenv.tools_dir() == old / "tools"
    assert tconfig.config_path() == cfg / "config.toml"
    assert not (home / ".laps").exists()
    assert not (home / ".config" / "laps").exists()


def test_新しい利用者は_laps(home):
    from laps import relayauth, session
    from laps.term import config as tconfig
    d = session.state_dir()                                    # 作る
    assert d == home / ".laps" and d.is_dir()
    assert relayauth.auth_file() == home / ".config" / "laps" / "auth.json"
    assert tconfig.config_path() == home / ".config" / "laps" / "config.toml"
    assert not (home / ".blackwall").exists()
    assert not (home / ".config" / "blackwall").exists()


def _write_ws(ws: Path):
    """作業場に書く部品を一通り動かす（作業ログ・帳面・全文の控え・解析の帳面）。"""
    from laps.oc import findings, notebook, worklog
    out = {
        "worklog": worklog.path(str(ws)),
        "ledger": findings.ledger_path(str(ws)),
        "hist": findings.hist_dir(str(ws)),
        "ledger_rel": findings.ledger_rel(str(ws)),
        "nb_rel": notebook.rel_dir(str(ws)),
    }
    (ws / "a.bin").write_bytes(b"x" * 10)
    out["nb_saved"] = notebook.save(str(ws), str(ws / "a.bin"), "hex", "", "00000000  78 78\n")
    return out


def test_新しい作業場は_dot_laps(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_NOTEBOOK", raising=False)
    monkeypatch.delenv("LAPS_TOOLSET", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    o = _write_ws(ws)
    assert o["worklog"] == ws / ".laps" / "WORKLOG.md"
    assert o["ledger"] == ws / ".laps" / "FINDINGS.md"
    assert o["hist"] == ws / ".laps" / "history"
    assert o["ledger_rel"] == ".laps/FINDINGS.md"
    assert o["nb_rel"] == ".laps/analysis"
    assert o["nb_saved"].startswith(".laps/analysis/")
    assert (ws / ".laps").is_dir() and not (ws / ".blackwall").exists()


def test_旧い作業場は_dot_blackwall_のまま(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_NOTEBOOK", raising=False)
    monkeypatch.delenv("LAPS_TOOLSET", raising=False)
    ws = tmp_path / "ws"
    (ws / ".blackwall").mkdir(parents=True)
    (ws / ".blackwall" / "WORKLOG.md").write_text("# 作業ログ\n", encoding="utf-8")
    o = _write_ws(ws)
    assert o["worklog"] == ws / ".blackwall" / "WORKLOG.md"
    assert o["ledger"] == ws / ".blackwall" / "FINDINGS.md"
    assert o["hist"] == ws / ".blackwall" / "history"
    assert o["ledger_rel"] == ".blackwall/FINDINGS.md"
    assert o["nb_saved"].startswith(".blackwall/analysis/")
    assert not (ws / ".laps").exists()                         # new は作られない


def test_添付の置き場_Rust_が先に作った物を読む(tmp_path):
    """Rust（attach.rs）が `.blackwall/attachments` に置く作業場で、Python の側も同じフォルダを選ぶ。"""
    ws = tmp_path / "ws"
    (ws / ".blackwall" / "attachments").mkdir(parents=True)
    assert paths.workspace_dir(ws) / "attachments" == ws / ".blackwall" / "attachments"
    ws2 = tmp_path / "ws2"
    (ws2 / ".laps" / "attachments").mkdir(parents=True)
    assert paths.workspace_dir(ws2) / "attachments" == ws2 / ".laps" / "attachments"




def test_前置きの中の置き場の名前(tmp_path, monkeypatch):
    """旧い中核の前置き: 新しい作業場では .laps/FINDINGS.md、旧い置き場が在れば .blackwall/FINDINGS.md。"""
    monkeypatch.delenv("LAPS_TOOLSET", raising=False)
    from laps.oc import prompt
    new_ws = tmp_path / "new"
    new_ws.mkdir()
    monkeypatch.chdir(new_ws)
    t = prompt.base_prompt()
    if "FINDINGS.md" in t:
        assert ".laps/FINDINGS.md" in t and ".blackwall/FINDINGS.md" not in t
    old_ws = tmp_path / "old"
    (old_ws / ".blackwall").mkdir(parents=True)
    monkeypatch.chdir(old_ws)
    t = prompt.base_prompt()
    if "FINDINGS.md" in t:
        assert ".blackwall/FINDINGS.md" in t and ".laps/FINDINGS.md" not in t
    assert not (old_ws / ".laps").exists()


def test_Rust_と同じ名前():
    """Rust の paths.rs と名前が食い違わないこと（文字で突き合わせる）。"""
    rs = Path(__file__).resolve().parents[2] / "app" / "src-tauri" / "src" / "paths.rs"
    if not rs.is_file():
        pytest.skip("app/ が無い")
    t = rs.read_text(encoding="utf-8")
    assert f'WS_NEW: &str = "{paths.WS_NEW}"' in t
    assert f'WS_OLD: &str = "{paths.WS_OLD}"' in t
    assert 'pick_in(&PathBuf::from(home).join(".config"), "laps", "blackwall")' in t
