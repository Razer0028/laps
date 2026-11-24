"""作業場の地図（laps/repomap.py）の約束を固定する。

この機構は**既定 ON** で、工程プロンプトの先頭に自動で載る。壊れ方が静かに
なりやすい種類の物なので、次を音の出る形にしておく:

  1. **絶対予算を超えない**（超えたら窓が死ぬ。ここが一番大事）
  2. **見積りは実測を下回らない**（下回ると予算の意味が消える）
  3. **不完全だと必ず書く**（黙って一部だけ出さない）
  4. 壊れても**空を返す**（走行を止めない）
  5. 作業場が変われば**作り直す**／変わらなければ**控えを使う**

★実測値（2026-08-29・Qwen2/3 の実トークナイザを pure-Python で再実装して計測。
  `"1234567890 "*10 → 110 tok` という既知の実測値で校正済み）:
      VDIR run34 の作業場（64 ファイル・PHP+Python+sh）  1,577 tok
      VDIR run9 の成果物（26 ファイル・Python）            778 tok
      BLACKWALL src 自身（352 ファイル）                 1,679 tok
  この試験はトークナイザを持たないので、`est_tokens`（保守側）で検査する。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps import repomap as rm  # noqa: E402


# --------------------------------------------------------------------------
# 作業場を1つ組む（VDIR の課題と同じ形: PHP の手続きファイル＋Python＋sh）
# --------------------------------------------------------------------------

def make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "site" / "api").mkdir(parents=True)
    (ws / "bin").mkdir(parents=True)
    (ws / "site" / "config.php").write_text(
        "<?php\nfunction get_db_connection() { return new PDO('sqlite:../works.db'); }\n"
        "function get_videos_db_connection() { return null; }\n", encoding="utf-8")
    (ws / "site" / "api" / "works.php").write_text(
        "<?php\nrequire_once '../config.php';\n"
        "$db = get_db_connection();\n"
        "$db->query('SELECT * FROM works');\n"
        "http_response_code(500);\n", encoding="utf-8")
    (ws / "site" / "video.php").write_text(
        "<?php\nrequire_once 'config.php';\n"
        "$slug = $_GET['slug'];\n$r = $_SERVER['HTTP_RANGE'];\n"
        "$db->query('SELECT * FROM videos');\nhttp_response_code(206);\n",
        encoding="utf-8")
    (ws / "bin" / "build.py").write_text(
        "import sqlite3\nDB_PATH = 'works.db'\n\n"
        "def build(rows, *, dry=False) -> int:\n    return len(rows)\n\n"
        "class Idx:\n    def add(self, k, v): pass\n", encoding="utf-8")
    (ws / "start.sh").write_text("#!/bin/sh\nboot() {\n  php -S 0.0.0.0:18300\n}\nboot\n",
                                 encoding="utf-8")
    (ws / "notes.md").write_text("# メモ\n", encoding="utf-8")
    return ws


# --------------------------------------------------------------------------
# 1. 絶対予算
# --------------------------------------------------------------------------

def test_budget_is_never_exceeded(tmp_path):
    """既定 2,048 でも、下限 512 でも、見積りが予算を超えない。"""
    ws = make_ws(tmp_path)
    for budget in (rm.DEFAULT_BUDGET_TOKENS, 1024, rm.MIN_BUDGET_TOKENS):
        text = rm.repo_map_block(str(ws), seed_text="site/api/works.php",
                                 budget_tokens=budget)
        assert text, f"budget={budget} で空になった"
        assert rm.est_tokens(text) <= budget, (
            f"budget={budget} を {rm.est_tokens(text)} tok で超えた")


def test_budget_holds_on_a_large_workspace(tmp_path):
    """ファイル数が多くても予算に収める（収まらないなら中略して、そう書く）。"""
    ws = tmp_path / "big"
    (ws / "pkg").mkdir(parents=True)
    for i in range(300):
        (ws / "pkg" / f"m{i}.py").write_text(
            f"import os\n\ndef f{i}(a, b=1) -> int:\n    return a\n"
            f"\nclass C{i}:\n    def m(self): pass\n", encoding="utf-8")
    text = rm.repo_map_block(str(ws), budget_tokens=2048)
    assert rm.est_tokens(text) <= 2048
    assert "不完全" in text


def test_min_budget_is_enforced(tmp_path):
    """予算に 0 を渡しても下限まで戻す（見出しだけの地図を作らない）。"""
    ws = make_ws(tmp_path)
    text = rm.repo_map_block(str(ws), budget_tokens=0)
    assert rm.est_tokens(text) <= rm.MIN_BUDGET_TOKENS
    assert text.strip()


# --------------------------------------------------------------------------
# 2. 見積りは実測を下回らない
# --------------------------------------------------------------------------

def test_estimate_is_never_below_the_byte_floor_for_ascii():
    """ASCII では 1 トークンが 1 文字より小さくなることはない。

    ★逆側（見積りが小さすぎる）だけを検査する。過大は予算を無駄にするだけだが、
      過小は 400 で走行ごと死ぬ（`context_budget` が同じ理由で上界を採っている）。
    """
    for s in ("a" * 1000, "def f(x): return x\n" * 50, "- site/api/works.php\n" * 40):
        assert rm.est_tokens(s) >= len(s) / 4.0


def test_estimate_counts_cjk_close_to_one_per_char():
    """日本語の見出しを ASCII と同じ係数で割ると見積りが小さくなりすぎる。"""
    jp = "この地図は不完全です" * 20
    assert rm.est_tokens(jp) >= len(jp) * 0.9


# --------------------------------------------------------------------------
# 3. 中身 — 本体は載らない・シグネチャと依存は載る
# --------------------------------------------------------------------------

def test_signatures_not_bodies(tmp_path):
    ws = make_ws(tmp_path)
    text = rm.repo_map_block(str(ws), seed_text="site/api/works.php")
    assert "def build(rows, *, dry=False) -> int" in text
    assert "class Idx" in text
    assert "function get_db_connection()" in text
    assert "boot()" in text
    # 本体は 1 行も出ていない
    assert "return len(rows)" not in text
    assert "php -S 0.0.0.0:18300" not in text


def test_dependency_edge_is_resolved(tmp_path):
    """require_once が作業場内のパスに解決されて `→` に出る（2ホップの土台）。"""
    ws = make_ws(tmp_path)
    text = rm.repo_map_block(str(ws), seed_text="site/api/works.php")
    assert "→ site/config.php" in text


def test_clues_expose_request_keys_and_tables(tmp_path):
    """関数を1つも定義しない PHP でも、入力の鍵・表・状態コードは載る。

    ★これは飾りではない: run29〜34 で実際に探されていたのが
      `HTTP_RANGE` `slug` `videos` だった。
    """
    ws = make_ws(tmp_path)
    text = rm.repo_map_block(str(ws), seed_text="site/video.php")
    assert "HTTP_RANGE" in text and "slug" in text
    assert "videos" in text
    assert "206" in text


def test_routes_are_found_without_a_word_list(tmp_path):
    """経路は語の一覧（`api|video|…`）で拾わない。それは 1 つの課題への当て込み。"""
    ws = tmp_path / "r"
    ws.mkdir()
    (ws / "srv.php").write_text(
        "<?php\n$u = $_SERVER['REQUEST_URI'];\n"
        "if ($u === '/quux/frobnicate') { }\n"
        "if ($u === '/zzz/') { }\n"
        "require_once '/config.php';\n"
        "$log = '/var/log/app.log';\n", encoding="utf-8")
    text = rm.repo_map_block(str(ws))
    assert "/quux/frobnicate" in text and "/zzz/" in text
    assert "経路 " in text
    assert "/var/log/app.log" not in text          # OS のパスは経路ではない
    assert "経路 /config.php" not in text          # 1 段の取り込み先は `→` の仕事


def test_looks_like_route():
    assert rm._looks_like_route("/api/works")
    assert rm._looks_like_route("/videos/")
    assert rm._looks_like_route("/health")
    assert not rm._looks_like_route("/config.php")


def test_python_clues_do_not_mistake_import_for_sql(tmp_path):
    """`from aiohttp import ...` を SQL の `FROM <表>` と読まない（実測で出た誤り）。"""
    ws = tmp_path / "p"
    ws.mkdir()
    (ws / "s.py").write_text("from aiohttp import web\nimport pathlib\n\n"
                             "def go(): pass\n", encoding="utf-8")
    text = rm.repo_map_block(str(ws))
    assert "aiohttp" not in text.split("def go")[0].split("- s.py")[-1]


def test_files_without_a_known_language_are_still_named(tmp_path):
    """中を読めない物も**名前は出す**（無いことにしない）。"""
    ws = make_ws(tmp_path)
    text = rm.repo_map_block(str(ws), seed_text="site/api/works.php")
    assert "notes.md" in text


def test_syntax_error_is_reported_not_swallowed(tmp_path):
    ws = tmp_path / "s"
    ws.mkdir()
    (ws / "broken.py").write_text("def f(:\n", encoding="utf-8")
    (ws / "ok.py").write_text("def g(): pass\n", encoding="utf-8")
    text = rm.repo_map_block(str(ws))
    assert "構文誤り" in text
    assert "def g()" in text          # 1 本壊れても残りは出る


# --------------------------------------------------------------------------
# 4. 不完全さを黙らない
# --------------------------------------------------------------------------

def test_always_says_it_is_incomplete(tmp_path):
    ws = make_ws(tmp_path)
    text = rm.repo_map_block(str(ws))
    assert "不完全" in text
    assert "Glob" in text and "Read" in text     # 確かめ方が書いてある


def test_omitted_counts_are_stated(tmp_path):
    """字数で省いたら件数を書く（黙って一部だけ出さない）。"""
    ws = tmp_path / "m"
    (ws / "a").mkdir(parents=True)
    for i in range(120):
        (ws / "a" / f"f{i}.py").write_text(
            "".join(f"def f{j}(a, b, c, d, e, ff, g, h): pass\n" for j in range(30)),
            encoding="utf-8")
    text = rm.repo_map_block(str(ws), budget_tokens=1024)
    assert "省きました" in text or "中略" in text


# --------------------------------------------------------------------------
# 5. 壊れても走行を止めない・環境変数で切れる
# --------------------------------------------------------------------------

def test_missing_workspace_returns_empty(tmp_path):
    assert rm.repo_map_block(str(tmp_path / "nope")) == ""


def test_empty_workspace_returns_empty(tmp_path):
    ws = tmp_path / "e"
    ws.mkdir()
    assert rm.repo_map_block(str(ws)) == ""


def test_can_be_turned_off(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    monkeypatch.setenv("LAPS_REPOMAP", "0")
    assert rm.repo_map_block(str(ws)) == ""


def test_scan_failure_does_not_raise(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    monkeypatch.setattr(rm, "scan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert rm.repo_map_block(str(ws)) == ""


# --------------------------------------------------------------------------
# 6. 作業場が変われば、次の工程の地図はそれを映す
# --------------------------------------------------------------------------

def test_map_follows_the_workspace(tmp_path):
    """控えを持たないので、地図は**常にいまの作業場**を映す。

    ★控えは作りかけて捨てた: 実トレース 12 試行すべてで作業場は毎回書き換わり
      （Write/Edit 24〜115 回）、控えは一度も当たらないと実測できたため。
    """
    ws = make_ws(tmp_path)
    a = rm.repo_map_block(str(ws), seed_text="x")
    assert "new.php" not in a
    (ws / "site" / "new.php").write_text("<?php function fresh() {}\n", encoding="utf-8")
    b = rm.repo_map_block(str(ws), seed_text="x")
    assert "new.php" in b and "function fresh()" in b
    (ws / "site" / "new.php").unlink()
    c = rm.repo_map_block(str(ws), seed_text="x")
    assert "new.php" not in c, "消えた物が地図に残っている"


def test_no_stale_cache_module_state():
    """控えを持たないことを構造で固定する（後から静かに戻さない）。"""
    assert not hasattr(rm, "_CACHE")
    assert not hasattr(rm, "workspace_fingerprint")


# --------------------------------------------------------------------------
# 7. 繋がっている証拠 — 工程ランナーが実際に前置している
# --------------------------------------------------------------------------


def test_seeds_must_actually_exist(tmp_path):
    """存在しないパスを種にしない（地図の中心が課題の外へずれる）。"""
    ws = make_ws(tmp_path)
    files, _ = rm.scan(str(ws))
    assert rm._seed_paths(files, "site/api/works.php を直す") == ["site/api/works.php"]
    assert rm._seed_paths(files, "nowhere/absent.php を直す") == []


# --------------------------------------------------------------------------
# 資産の羅列が地図を食い潰さないこと（2026-09-02）
# --------------------------------------------------------------------------

def test_中を見ないファイルは置き場と拡張子でまとめる(tmp_path):
    """★実測: この課題の作業場は `images/<作者>/*.png` を数十枚作る。
    1 個ずつ名前を並べると、地図 1,940 字のうち **約 1,500 字（77%）**が
    画像の名前になり、コードの側は 450 字しか残らなかった。
    実走行の作業場にも同じ形の資産がある（run46 で画像 88・動画 258 の経路が
    出力に出る／run48 で画像 60）。
    ★これは `phases._false_green` で一度直したのと同じ族——
    「同じ物の変種が枠を使い切って、いちばん効く行が落ちる」。"""
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "api.php").write_text(
        "<?php function works(){ http_response_code(404); }", encoding="utf-8")
    imgs = tmp_path / "images" / "作者A"
    imgs.mkdir(parents=True)
    for i in range(60):
        (imgs / f"{i}.png").write_bytes(b"\x89PNG" + b"0" * 200)
    (tmp_path / "works.db").write_bytes(b"SQLite format 3\x00")

    out = rm.repo_map_block(str(tmp_path), seed_text="site/api.php")
    assert "images/作者A/*.png × 60 個" in out, out
    assert "images/作者A/0.png" not in out, "1 個ずつ並べている"
    assert "works.db" in out, "少数のファイルは名前のまま残す"
    assert "function works()" in out
    # 資産の羅列に消えていた予算が戻る（旧実装は同じ作業場で 1,900 字超）
    assert len(out) < 900, f"地図が資産の名前で膨らんでいる: {len(out)} 字"


def test_まとめても省いた件数は必ず言う(tmp_path):
    """★この地図の約束: 省いた事実と件数を本文に書く（黙って一部だけ出さない）。"""
    for d in range(rm._OTHER_MAX_LINES + 3):
        sub = tmp_path / f"assets{d:02d}"
        sub.mkdir()
        for i in range(rm._OTHER_FOLD_MIN + 1):
            (sub / f"{i}.bin").write_bytes(b"x" * 10)
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    files, dropped = rm.scan(str(tmp_path))
    lines, hidden = rm._fold_others([f for f in files if not f.lang])
    assert hidden > 0 and len(lines) <= rm._OTHER_MAX_LINES + 1
    out = rm.repo_map_block(str(tmp_path), seed_text="a.py")
    assert "字数で省きました" in out, out


# --------------------------------------------------------------------------
# ★2026-10-19: git の作業木では .gitignore を守る・組み立て物と .bak を飛ばす・コードを先に残す
# --------------------------------------------------------------------------

def _git(ws: Path, *args: str) -> None:
    import subprocess
    subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True)


def test_gitignored_copies_are_not_mapped(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "copy").mkdir()
    (ws / "src" / "core.py").write_text("def real_one():\n    pass\n", encoding="utf-8")
    (ws / "copy" / "core.py").write_text("def copied_one():\n    pass\n", encoding="utf-8")
    (ws / ".gitignore").write_text("copy/\n", encoding="utf-8")
    _git(ws, "init", "-q")
    files, _ = rm.scan(str(ws))
    paths = {f.path for f in files}
    assert "src/core.py" in paths
    assert not any(p.startswith("copy/") for p in paths), paths


def test_build_and_backup_dirs_skipped(tmp_path):
    ws = tmp_path / "ws"
    for d in ("src", "target/debug", "app.bak-20260902"):
        (ws / d).mkdir(parents=True)
        (ws / d / "m.py").write_text("def f():\n    pass\n", encoding="utf-8")
    for paths in ({f.path for f in rm.scan(str(ws))[0]},):
        assert paths == {"src/m.py"}, paths
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    assert {f.path for f in rm.scan(str(ws))[0]} == {"src/m.py"}


def test_code_is_kept_first_when_over_max(tmp_path):
    ws = tmp_path / "ws"
    (ws / "a_fonts").mkdir(parents=True)
    for i in range(10):
        (ws / "a_fonts" / f"f{i}.woff2").write_bytes(b"x")
    (ws / "z.py").write_text("def g():\n    pass\n", encoding="utf-8")
    files, dropped = rm.scan(str(ws), max_files=3)
    assert "z.py" in {f.path for f in files}
    assert dropped["over_max_files"] == 8
