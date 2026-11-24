"""同梱の REA（rea-agents の MCP サーバー）を load_config が自動で足す（★2026-11-03 オーナー「同梱してください」）。

本物の node は起動しない。偽の実行環境（node/node と rea.mjs の空ファイル）を tmp_path に作って確かめる。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.oc import mcp  # noqa: E402

NODE = "node.exe" if sys.platform == "win32" else "node"


def _runtime(root: Path) -> Path:
    (root / "node").mkdir(parents=True)
    (root / "node" / NODE).write_text("#!/bin/sh\nexit 0\n")
    entry = root / "pkg" / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"
    entry.parent.mkdir(parents=True)
    entry.write_text("// fake\n")
    return entry


def _env(monkeypatch, tmp_path, rea_dir: Path | None):
    home = tmp_path / "home"
    monkeypatch.setenv("LAPS_HOME", str(home))
    for k in ("LAPS_REA", "LAPS_REA_GHIDRA", "GHIDRA_INSTALL_DIR", "LAPS_MCP_PROJECT", "LAPS_MCP",
              "LAPS_INSTALL_PYRUNTIME", "LAPS_MCP_MAX_DIRECT"):
        monkeypatch.delenv(k, raising=False)
    if rea_dir is not None:
        monkeypatch.setenv("LAPS_REA_DIR", str(rea_dir))
    else:
        monkeypatch.delenv("LAPS_REA_DIR", raising=False)
    return home


def test_present_is_included(monkeypatch, tmp_path):
    entry = _runtime(tmp_path / "rea")
    _env(monkeypatch, tmp_path, tmp_path / "rea")
    cfg = mcp.load_config()["rea"]
    assert cfg["command"] == str(tmp_path / "rea" / "node" / NODE)
    assert cfg["args"] == [str(entry), "mcp"]
    assert cfg["env"] == {"REA_MCP_INPUT_SCHEMA_PROFILE": "compact", "REA_MCP_OUTPUT_SCHEMA_PROFILE": "off"}
    assert cfg["_source"] == "built-in" and cfg["_lazy"] is True
    assert cfg["_path_prepend"] == str(tmp_path / "rea" / "node")
    assert mcp._transport(cfg) == "stdio"
    # 控えの鍵は内部の印（_ で始まる）を含まない＝PATH や _lazy で控えが外れない
    k = mcp._key(cfg)
    assert k == mcp._key({k2: v for k2, v in cfg.items() if not k2.startswith("_")})


def test_default_dir_is_next_to_python(monkeypatch, tmp_path):
    _runtime(tmp_path / "pyruntime" / "rea")
    _env(monkeypatch, tmp_path, None)
    (tmp_path / "pyruntime" / "python.exe").write_text("")
    monkeypatch.setattr(mcp.sys, "executable", str(tmp_path / "pyruntime" / "python.exe"))
    assert "rea" in mcp.load_config()


def test_deb_layout_next_to_venv(monkeypatch, tmp_path):
    """Linux の .deb: /opt/blackwall/venv の python（系の python への symlink）でも /opt/blackwall/rea を見つける。"""
    if sys.platform == "win32":
        return
    base = tmp_path / "opt-blackwall"
    _runtime(base / "rea")
    (base / "venv" / "bin").mkdir(parents=True)
    _env(monkeypatch, tmp_path, None)
    monkeypatch.setattr(mcp.sys, "prefix", str(base / "venv"))
    monkeypatch.setattr(mcp.sys, "executable", "/usr/bin/python3")
    assert mcp._rea_dir() == base / "rea"
    assert "rea" in mcp.load_config()


def test_absent_adds_nothing(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, tmp_path / "nothing-here")
    assert mcp.load_config() == {}
    # rea.mjs だけ欠けている
    (tmp_path / "half" / "node").mkdir(parents=True)
    (tmp_path / "half" / "node" / NODE).write_text("")
    monkeypatch.setenv("LAPS_REA_DIR", str(tmp_path / "half"))
    assert mcp.load_config() == {}


def test_user_config_wins(monkeypatch, tmp_path):
    _runtime(tmp_path / "rea")
    home = _env(monkeypatch, tmp_path, tmp_path / "rea")
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"rea": {"command": "my-rea", "args": ["x"]}}}))
    cfg = mcp.load_config()["rea"]
    assert cfg["command"] == "my-rea" and cfg["_source"] == str(home / "mcp.json")
    # disabled にした "rea" も利用者の意思として勝つ（同梱で黙って生き返らせない）
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"rea": {"command": "my-rea", "disabled": True}}}))
    assert "rea" not in mcp.load_config()


def test_switch_off(monkeypatch, tmp_path):
    _runtime(tmp_path / "rea")
    _env(monkeypatch, tmp_path, tmp_path / "rea")
    monkeypatch.setenv("LAPS_REA", "0")
    assert "rea" not in mcp.load_config()


def test_ghidra_passthrough(monkeypatch, tmp_path):
    _runtime(tmp_path / "rea")
    _env(monkeypatch, tmp_path, tmp_path / "rea")
    monkeypatch.setenv("GHIDRA_INSTALL_DIR", "C:/ghidra_11")
    env = mcp.load_config()["rea"]["env"]
    assert env["GHIDRA_INSTALL_DIR"] == "C:/ghidra_11" and env["REA_ANALYSIS_PROVIDER"] == "ghidra"
    monkeypatch.setenv("LAPS_REA_GHIDRA", "D:/g")
    assert mcp.load_config()["rea"]["env"]["GHIDRA_INSTALL_DIR"] == "D:/g"


def test_path_prepended_at_start(monkeypatch, tmp_path):
    """_Stdio.start が node のフォルダを PATH の先頭に足す（Popen は差し替えて起動しない）。"""
    _runtime(tmp_path / "rea")
    _env(monkeypatch, tmp_path, tmp_path / "rea")
    cfg = mcp.load_config()["rea"]
    seen = {}

    class _P:
        def __init__(self, argv, **kw):
            seen["argv"], seen["env"] = argv, kw["env"]
            self.stdout = self.stderr = self.stdin = None

    monkeypatch.setattr(mcp.subprocess, "Popen", _P)
    monkeypatch.setattr(mcp.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    c = mcp._Stdio("rea", cfg)
    c.start()
    assert seen["argv"][1:] == cfg["args"]
    assert seen["env"]["PATH"].split(mcp.os.pathsep)[0] == cfg["_path_prepend"]
    assert seen["env"]["REA_MCP_INPUT_SCHEMA_PROFILE"] == "compact"


def test_lazy_no_spawn_at_run_start(monkeypatch, tmp_path):
    """走行の開始（specs）では起動しない。Mcp(list, server=rea) で初めてつなぐ。控えがあっても裏で温めない。"""
    _runtime(tmp_path / "rea")
    _env(monkeypatch, tmp_path, tmp_path / "rea")
    calls = []

    class _C:
        alive, stale, tools, server_info = True, False, [{"name": "rea_inspect", "description": "d"}], {}

    def fake_connect(name, cfg, timeout=None):
        calls.append(name)
        c = _C()
        c.key = mcp._key(cfg)
        mcp._pool[name] = c
        mcp._save_cache(name, c)
        return c

    monkeypatch.setattr(mcp, "connect", fake_connect)
    monkeypatch.setattr(mcp, "_pool", {})
    monkeypatch.setattr(mcp, "_failed", {})
    specs = []
    mcp.specs(specs.append)
    assert calls == []
    assert [s.name for s in specs] == ["Mcp"]        # 道具の名前が分からないので直接は載せない
    assert "starts on first use" in specs[0].description
    r = specs[0].handler({"action": "list", "server": "rea"})
    assert calls == ["rea"] and "rea_inspect" in str(r) and "built into L.A.P.S" in str(r)
    # 次の走行: 控えから載せる。_warm（裏の起動）は呼ばない
    monkeypatch.setattr(mcp, "_pool", {})
    warmed = []
    monkeypatch.setattr(mcp, "_warm", lambda n, c: warmed.append(n))
    specs2 = []
    mcp.specs(specs2.append)
    assert warmed == [] and calls == ["rea"]
    # ★同梱の REA は道具が分かっても直接には載せず、いつも Mcp を通す（2026-11-03 レビュー）
    assert [s.name for s in specs2] == ["Mcp"]
    assert "rea_inspect" in specs2[0].description


def test_install_dir_preferred(monkeypatch, tmp_path):
    """LAPS_INSTALL_PYRUNTIME（インストール先）に rea/ があれば、複製した python.exe の隣より先に使う。"""
    _runtime(tmp_path / "install" / "rea")
    _runtime(tmp_path / "staged" / "rea")
    _env(monkeypatch, tmp_path, None)
    monkeypatch.setattr(mcp.sys, "executable", str(tmp_path / "staged" / "python.exe"))
    monkeypatch.setenv("LAPS_INSTALL_PYRUNTIME", str(tmp_path / "install"))
    assert mcp.load_config()["rea"]["command"] == str(tmp_path / "install" / "rea" / "node" / NODE)
    # インストール先に rea/ が無ければ従来どおり python.exe の隣
    monkeypatch.setenv("LAPS_INSTALL_PYRUNTIME", str(tmp_path / "nowhere"))
    assert mcp.load_config()["rea"]["command"] == str(tmp_path / "staged" / "rea" / "node" / NODE)


def test_user_small_server_stays_direct(monkeypatch, tmp_path):
    """REA（229 本）が控えにあっても、利用者の小さなサーバーは mcp__<server>__<tool> のまま。REA は Mcp の側に足す。"""
    _runtime(tmp_path / "rea")
    home = _env(monkeypatch, tmp_path, tmp_path / "rea")
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"mine": {"command": "my-srv"}}}))
    cfgs = mcp.load_config()
    mine = [{"name": f"t{i}", "description": "d"} for i in range(3)]
    rea = [{"name": f"r{i}", "description": "d"} for i in range(229)]
    monkeypatch.setattr(mcp, "_tool_index", lambda: (cfgs, {"mine": mine, "rea": rea}, {}))
    out = []
    mcp.specs(out.append)
    names = [s.name for s in out]
    assert names == ["mcp__mine__t0", "mcp__mine__t1", "mcp__mine__t2", "Mcp"]
    assert "- rea:" in out[-1].description and "- mine:" not in out[-1].description
    # REA がまだ起動していない（控えも無い）ときも、利用者のサーバーは直接
    monkeypatch.setattr(mcp, "_tool_index", lambda: (cfgs, {"mine": mine}, {}))
    out = []
    mcp.specs(out.append)
    assert [s.name for s in out] == ["mcp__mine__t0", "mcp__mine__t1", "mcp__mine__t2", "Mcp"]
    assert "starts on first use" in out[-1].description
    # REA を切れば従来どおり Mcp は載らない
    monkeypatch.setattr(mcp, "_tool_index", lambda: ({"mine": cfgs["mine"]}, {"mine": mine}, {}))
    out = []
    mcp.specs(out.append)
    assert [s.name for s in out] == ["mcp__mine__t0", "mcp__mine__t1", "mcp__mine__t2"]


def test_no_respawn_right_after_failure(monkeypatch, tmp_path):
    """起動に失敗した直後（60 秒以内）は、list / describe のたびに node を立て直さない。"""
    _runtime(tmp_path / "rea")
    _env(monkeypatch, tmp_path, tmp_path / "rea")
    cfg = mcp.load_config()["rea"]
    calls = []
    monkeypatch.setattr(mcp, "connect", lambda n, c, timeout=None: calls.append(n))
    monkeypatch.setattr(mcp, "_pool", {})
    monkeypatch.setattr(mcp, "_failed", {"rea": (mcp._key(cfg), mcp.time.time() - 5)})
    mcp._ensure_started("rea")
    assert calls == []
    monkeypatch.setattr(mcp, "_failed", {"rea": (mcp._key(cfg), mcp.time.time() - 61)})
    mcp._ensure_started("rea")
    assert calls == ["rea"]
    # 設定が変わった（鍵が違う）なら待たずに起動する
    monkeypatch.setattr(mcp, "_failed", {"rea": ("other-key", mcp.time.time() - 5)})
    mcp._ensure_started("rea")
    assert calls == ["rea", "rea"]


def test_tools_listing_names_bundled_rea(tmp_path, monkeypatch):
    """Tools の一覧の説明に同梱の REA が名前と中身つきで載る（2026-11-03: 名前だけではモデルが REA を知らなかった）。"""
    rea = tmp_path / "rea"
    (rea / "node").mkdir(parents=True)
    (rea / "node" / "node").write_text("")
    (rea / "pkg/node_modules/rea-agents/scripts").mkdir(parents=True)
    (rea / "pkg/node_modules/rea-agents/scripts/rea.mjs").write_text("")
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_REA_DIR", str(rea))
    from laps.engine.tools import ext
    monkeypatch.delenv("LAPS_REA_GHIDRA", raising=False)
    monkeypatch.delenv("GHIDRA_INSTALL_DIR", raising=False)
    # ★2026-11-04: いつ使うか・最初に何を呼ぶかを、その機械で動く物だけ書く（Ghidra が無いと逆コンパイルの道具は動かない）。
    h = ext._mcp_hint()
    assert "rea (reverse-engineering server REA, static readers only on this machine" in h and "open_binary" not in h
    monkeypatch.setenv("LAPS_REA_GHIDRA", str(tmp_path / "ghidra"))
    h = ext._mcp_hint()
    assert "Use it when you need to know what a program does inside" in h and "open_binary(path=" in h
    monkeypatch.setenv("LAPS_REA", "0")
    assert ext._mcp_hint() == ""


def test_aids_rea_notes(monkeypatch):
    """REA の得意なファイルを Read したら 1 回だけ使い方を添える。provider_unavailable なら空回りを止める一言（2026-11-04）。"""
    import types
    from laps.engine import aids as A
    from laps.oc import mcp

    class R:
        def __init__(self, out, ok=True):
            self.output, self.ok, self.kind = out, ok, "text"

    def mk():
        ag = types.SimpleNamespace(counts={})
        ag._count = lambda k: ag.counts.__setitem__(k, ag.counts.get(k, 0) + 1)
        x = A.Aids.__new__(A.Aids)
        x.a, x.rea_noted, x.rea_down_noted = ag, False, False
        return x

    def note(x, path):
        t: list[str] = []
        x._rea("Read", {"file_path": path}, R("ok"), "ok", t)
        return t[0] if t else ""

    monkeypatch.setattr(mcp, "rea_info", lambda: {"built_in": True, "native": True})
    assert 'tool="open_binary"' in note(mk(), "C:\\g\\Game.exe")
    assert 'tool="inspect_sqlite_database"' in note(mk(), "/d/app.sqlite")
    assert note(mk(), "/d/readme.txt") == ""
    x = mk()
    assert note(x, "/a.exe") and note(x, "/b.exe") == ""                      # 1 走行に 1 回
    x = mk()
    x._rea("Mcp", {"server": "rea", "tool": "open_binary"}, R("ok"), "ok", [])
    assert note(x, "/a.exe") == ""                                           # もう使っているなら添えない

    monkeypatch.setattr(mcp, "rea_info", lambda: {"built_in": True, "native": False})
    assert note(mk(), "/x/lib.so") == ""                                     # この機械では読めない形式には添えない
    assert 'tool="inspect_managed_artifact"' in note(mk(), "/x/App.dll")
    t: list[str] = []
    mk()._rea("Mcp", {"server": "rea", "tool": "list_procedures"}, R("x", False), '{"error":{"code":"provider_unavailable"}}', t)
    assert t and "cannot run on this machine" in t[0]

    monkeypatch.setattr(mcp, "rea_info", lambda: None)
    assert note(mk(), "/a.exe") == ""                                        # REA が無ければ何も言わない


def test_builtin_rea_finds_ghidra_and_jdk_in_tools(tmp_path, monkeypatch):
    """<LAPS_HOME>/tools に置いた Ghidra と JDK を見つけ、REA に GHIDRA_INSTALL_DIR・JAVA_HOME を渡す（2026-11-05）。"""
    import os
    import sys as _sys
    from laps.oc import mcp
    rea = tmp_path / "rea"
    (rea / "node").mkdir(parents=True)
    (rea / "node" / ("node.exe" if _sys.platform == "win32" else "node")).write_text("")
    (rea / "pkg/node_modules/rea-agents/scripts").mkdir(parents=True)
    (rea / "pkg/node_modules/rea-agents/scripts/rea.mjs").write_text("")
    home = tmp_path / "home"
    monkeypatch.setenv("LAPS_HOME", str(home))
    monkeypatch.setenv("LAPS_REA_DIR", str(rea))
    for k in ("LAPS_REA_GHIDRA", "GHIDRA_INSTALL_DIR", "LAPS_REA_JAVA", "LAPS_REA"):
        monkeypatch.delenv(k, raising=False)
    assert "REA_ANALYSIS_PROVIDER" not in mcp._builtin_rea()["env"]          # 何も無ければ Ghidra を使わない
    g = home / "tools/ghidra/ghidra_12.1.4_PUBLIC"
    (g / "Ghidra").mkdir(parents=True)
    (g / "_half").mkdir()                                                    # 展開の途中の物は数えない
    (g / "Ghidra/application.properties").write_text("")
    j = home / "tools/jdk/jdk-21.0.13+9/bin"
    j.mkdir(parents=True)
    (j / ("java.exe" if _sys.platform == "win32" else "java")).write_text("")
    cfg = mcp._builtin_rea()
    assert cfg["env"]["GHIDRA_INSTALL_DIR"] == str(g) and cfg["env"]["REA_ANALYSIS_PROVIDER"] == "ghidra"
    assert cfg["env"]["JAVA_HOME"] == str(j.parent)
    assert cfg["_path_prepend"].split(os.pathsep)[0] == str(j)
