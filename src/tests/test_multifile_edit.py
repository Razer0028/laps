"""W3（第3回・2026-09-04）: 多数のファイルを白紙から作り、繰り返し編集する長い開発で、
編集が**確実に当たり・壊さない**ことを固める（担当: Write/Edit/ApplyDiff）。

Edit/ApplyDiff/syntax gate 個々の単体試験（`_apply_one` / `_loose_match` / `_syntax_error`
を直接呼ぶもの）は既に厚い（test_patches.py・test_edit_recovery_cascade.py・
test_edit_syntax_gate.py・test_syntax_gate_php_sh.py・test_applydiff_recovery.py）。
ここは**その1段上**——実開発の1本道を通しで再生する:

  10 ファイル（python/php/sh/json、実際に `_run` を通す）を Write で作り、
  各ファイルに複数回 Edit をかける。1回目で当たる編集・old_string が
  ファイルに無い編集（候補提示で救えるか）・old_string が複数箇所に
  一致する編集（安全に断るか／occurrence で1箇所だけ直せるか）・
  構文を壊す編集（gate が差し戻すか）を、言語ごとに実機の関数で確かめる。

  加えて、Edit 単体では埋められない穴（新規ファイルを作れない）を
  ApplyDiff が翻訳器として埋められるか（実運用で言われている「Edit の
  失敗経路の翻訳器」の具体的な中身）を1本立てる。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

#: 外部の構文検査器の有無（test_syntax_gate_php_sh.py と同じ条件）。
#:   無い環境では edit.py の gate 自体が「何もしない」で通す（_external_syntax_error
#:   の best-effort 仕様）ので、その環境では「gate が壊れた編集を止める」ではなく
#:   「gate が無いので素通りする」が正しい期待値になる。決め打ちで失敗させない。
_HAVE_PHP = bool(shutil.which("php"))
_HAVE_BASH = bool(shutil.which("bash"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.tools import write as W          # noqa: E402
from laps.tools import edit as E           # noqa: E402
from laps.tools import apply_diff as AD    # noqa: E402
from laps import checkpoint as CK          # noqa: E402


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


def _out(x) -> str:
    return str(x)


# ─────────────────────────────────────────────────────────────────────────
# 10 ファイル・複数編集サイクルの1本道
# ─────────────────────────────────────────────────────────────────────────
#
# 各エントリ: (ファイル名, 初期本文, 検査の種類)
#   "ambiguous"  … old_string が2箇所に一致する形。occurrence を使わない編集は
#                  断られ、replace_all も「行まるごと一致・前後が異なる」ため
#                  拒否され（edit.py の P5 ガード）、occurrence=2 で1箇所だけ直る。
#   "syntax"     … 構文を壊す編集を gate が差し戻し、ファイルは変わらない。
#                  正しい直し方は通る。
#   "mismatch"   … old_string がどこにも無い（書き写しの記憶違い）。近い場所を
#                  返す（候補提示）。ファイルは変わらない。
#   "idempotent" … 1回目の編集が当たったあと、**同じ old_string で再度**
#                  呼ぶと「もう無い」で断られる（二重適用の暴走を防ぐ）。

_CALC_PY = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n"
_UTIL_PY = "def helper(x):\n    return x + 1\n"
_HELPERS_PY = "def compute_total(a, b):\n    return a + b\n"

_CONFIG_PHP = (
    "<?php\nclass Config {\n    public $debug = false;\n}\n\n"
    "class Logger {\n    public $debug = false;\n}\n"
)
_API_PHP = '<?php\nfunction greet($name) {\n    return "hi " . $name;\n}\n'
_LOGGER_PHP = (
    "<?php\nfunction log_event($msg) {\n"
    "    file_put_contents('/var/log/app.log', $msg, FILE_APPEND);\n}\n"
)

_DEPLOY_SH = '#!/bin/bash\nset -e\nif [ -f /tmp/flag ]; then\n    echo "found"\nfi\n'
_BACKUP_SH = "#!/bin/bash\nset -e\ntar czf backup.tar.gz /data\n"

_SETTINGS_JSON = '{\n    "debug": true,\n    "port": 8080\n}\n'
_DATA_JSON = '{\n    "name": "widget",\n    "version": "1.0.0"\n}\n'


FILES = [
    ("calc.py", _CALC_PY, "ambiguous"),
    ("util.py", _UTIL_PY, "syntax"),
    ("helpers.py", _HELPERS_PY, "mismatch"),
    ("config.php", _CONFIG_PHP, "ambiguous"),
    ("api.php", _API_PHP, "syntax"),
    ("logger.php", _LOGGER_PHP, "mismatch"),
    ("deploy.sh", _DEPLOY_SH, "syntax"),
    ("backup.sh", _BACKUP_SH, "idempotent"),
    ("settings.json", _SETTINGS_JSON, "syntax"),
    ("data.json", _DATA_JSON, "idempotent"),
]


def test_ten_files_written_then_edited_through_the_real_functions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    # ── 1. 10 ファイルを Write で作る ────────────────────────────────
    for name, body, _kind in FILES:
        out = _out(W._run({"file_path": name, "content": body}))
        assert out.startswith("OK: wrote"), f"{name}: {out}"
        assert (tmp_path / name).read_text() == body

    # ── 2. 各ファイルを種類に応じて複数回 Edit する ──────────────────
    for name, body, kind in FILES:
        p = tmp_path / name

        if kind == "ambiguous":
            # ★置き換え後も構文が壊れない値にする（言語ごとに正しい文にする）。
            #   "CHANGED" のような裸の識別子は PHP の文としては不正で、
            #   構文ゲートに止められてしまい occurrence 自体の検査にならない。
            if name.endswith(".php"):
                old, new = "public $debug = false;", "public $debug = true;"
            else:
                old, new = "return a + b", "return a * b"

            # occurrence 無しでは断る（安全側）
            out = _out(E._run({"file_path": name, "old_string": old, "new_string": new}))
            assert out.startswith("Error"), out
            assert "箇所に一致" in out, out
            assert p.read_text() == body, f"{name}: 断ったのに書き換わっている"

            # replace_all は「行まるごと一致・前後が異なる」ため拒否される
            # （edit.py の P5 ガード: add を壊して mul を直した実例の再発防止）
            out = _out(E._run({"file_path": name, "old_string": old,
                               "new_string": new, "replace_all": True}))
            assert out.startswith("Error"), out
            assert "replace_all は拒否しました" in out, out
            assert p.read_text() == body, f"{name}: 拒否されたのに書き換わっている"

            # occurrence=2 で2箇所目だけ直る
            out = _out(E._run({"file_path": name, "old_string": old,
                               "new_string": new, "occurrence": 2}))
            assert out.startswith("OK"), out
            new_body = p.read_text()
            assert new_body.count(old) == 1, f"{name}: 1箇所目まで変わってしまった"
            assert new_body.count(new) == 1

        elif kind == "syntax":
            checker_present = True
            if name.endswith(".py"):
                bad_old, bad_new, good_new = "return x + 1", "return x +", "return x + 2"
            elif name.endswith(".php"):
                bad_old = 'return "hi " . $name;'
                bad_new = 'return "hi " . $name'      # セミコロン抜き
                good_new = 'return "hi there " . $name;'
                checker_present = _HAVE_PHP
            elif name.endswith(".sh"):
                bad_old, bad_new, good_new = "fi\n", "", "fi\n# deployed\n"
                checker_present = _HAVE_BASH
            else:  # .json
                bad_old = '"port": 8080'
                bad_new = '"port": 8080,'              # 末尾カンマで壊れる
                good_new = '"port": 9090'

            out = _out(E._run({"file_path": name, "old_string": bad_old, "new_string": bad_new}))
            if checker_present:
                assert out.startswith("Error"), out
                assert "構文" in out, out
                assert p.read_text() == body, f"{name}: gate が止めたのに書き換わっている"
            else:
                # 検査器が無い環境（php/bash 未導入）: gate は best-effort で
                # 何もしない＝壊れた編集でも素通りする、が正しい挙動。
                assert out.startswith("OK"), out
                p.write_text(body)  # 次の断言のため書き戻す

            out = _out(E._run({"file_path": name, "old_string": bad_old, "new_string": good_new}))
            assert out.startswith("OK"), out
            assert good_new in p.read_text()

        elif kind == "mismatch":
            if name.endswith(".py"):
                bad_old = "def compute_total(a, c):\n    return a + b"
            elif name.endswith(".php"):
                bad_old = "function log_evnt($msg) {"     # 綴り間違い
            out = _out(E._run({"file_path": name, "old_string": bad_old, "new_string": "x"}))
            assert out.startswith("Error"), out
            assert "old_string not found" in out
            # 候補提示: 近い場所か、現在の内容そのものが返っている
            assert ("いちばん近いのは" in out) or ("ファイルの現在の内容" in out), out
            assert p.read_text() == body, f"{name}: 見つからないのに書き換わっている"

        elif kind == "idempotent":
            if name.endswith(".sh"):
                old_line = "tar czf backup.tar.gz /data"
                new_line = "tar czf backup2.tar.gz /data2"
            else:  # .json
                old_line = '"version": "1.0.0"'
                new_line = '"version": "1.0.1"'
            out = _out(E._run({"file_path": name, "old_string": old_line, "new_string": new_line}))
            assert out.startswith("OK"), out
            assert new_line in p.read_text()

            # ★同じ old_string で**もう一度**呼ぶ（モデルが古い記憶のまま繰り返す形）。
            #   もう本文に無いので、黙って別の場所に当てたりせず断る。
            out2 = _out(E._run({"file_path": name, "old_string": old_line, "new_string": new_line}))
            assert out2.startswith("Error"), out2
            assert "old_string not found" in out2
            assert p.read_text().count(new_line) == 1, f"{name}: 二重に適用された"


# ─────────────────────────────────────────────────────────────────────────
# ApplyDiff は Edit の失敗経路の翻訳器——Edit に無い能力（新規ファイル作成）を
# 実際に埋められるか。
# ─────────────────────────────────────────────────────────────────────────

def test_edit_cannot_create_a_missing_file_but_applydiff_can(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = "brand_new_module.py"

    out = _out(E._run({"file_path": name, "old_string": "x", "new_string": "y"}))
    assert out.startswith("Error: file not found"), out
    assert not (tmp_path / name).exists()

    # ★同じ意図（このファイルを作りたい）を、ApplyDiff の空 SEARCH で出し直す。
    out2 = _out(AD._run({"diff": f"{name}\n<<<<<<< SEARCH\n=======\n"
                         "def entrypoint():\n    return 42\n"
                         ">>>>>>> REPLACE\n"}))
    assert "Applied 1/1" in out2, out2
    # ★空 SEARCH の追記は、渡した replace 行をそのまま連結する（末尾に改行を
    #   足したりしない）。SEARCH/REPLACE の書式そのままの挙動を確かめる。
    assert (tmp_path / name).read_text() == "def entrypoint():\n    return 42"


def test_applydiff_refuses_ambiguous_search_like_edit_does(tmp_path, monkeypatch):
    """ApplyDiff にも occurrence が無い——曖昧な SEARCH は、Edit と同じく
    「黙って1つ選ぶ」のではなく断る（P5 破損を別経路で再現させない）。"""
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "calc.py"
    p.write_text(_CALC_PY)
    diff = (f"calc.py\n<<<<<<< SEARCH\nreturn a + b\n=======\nreturn a * b\n>>>>>>> REPLACE\n")
    out = _out(AD._run({"diff": diff}))
    assert out.startswith("Error"), out
    assert "ambiguous" in out
    assert p.read_text() == _CALC_PY, "曖昧な SEARCH なのに書き換わっている"


# ─────────────────────────────────────────────────────────────────────────
# Write の安全: 上書き・checkpoint 連携・親ディレクトリ自動作成・
# シンボリックリンク/ディレクトリへの拒否
# ─────────────────────────────────────────────────────────────────────────

def test_write_creates_nested_parent_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = _out(W._run({"file_path": "deep/nested/dir/tree/f.txt", "content": "hi"}))
    assert out.startswith("OK: wrote"), out
    assert (tmp_path / "deep/nested/dir/tree/f.txt").read_text() == "hi"


def test_write_overwrite_is_atomic_and_replaces_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"version": 1}))
    out = _out(W._run({"file_path": "config.json", "content": json.dumps({"version": 2})}))
    assert out.startswith("OK: wrote"), out
    assert json.loads(p.read_text()) == {"version": 2}
    # 同じ inode ではない（tmp+os.replace の不可分置換であること）はここでは問わない
    # （atomic.py 側で試験済み）。ここは Write 経由でも同じ道を通ることだけを見る。


def test_write_path_matches_what_checkpoint_would_snapshot(tmp_path, monkeypatch):
    """checkpoint.paths_for_tool が Write の**書き先の解釈**を勝手に別実装
    しておらず、write.py の実装（_sanitize_path）をそのまま使っていること。
    ここがずれると「控えたつもりが別のパスだった」という戻せない事故になる。
    """
    monkeypatch.chdir(tmp_path)
    inp = {"file_path": "notes/todo.txt", "content": "buy milk"}
    out = _out(W._run(inp))
    assert out.startswith("OK: wrote"), out
    assert CK.paths_for_tool("Write", inp) == [inp["file_path"]]

    # SFT 由来の偽パス（Ω-N）でも、実際に書いた先と checkpoint の控え先が一致する
    fake = "/tmp/blackwall_bench_abc123/report.txt"
    inp2 = {"file_path": fake, "content": "x"}
    out2 = _out(W._run(inp2))
    assert out2.startswith("OK: wrote") or "Redirected" in out2, out2
    written_path = out2.splitlines()[-1].rsplit(" to ", 1)[-1]
    assert CK.paths_for_tool("Write", inp2) == [written_path]
    assert Path(written_path).exists()


def test_write_to_an_existing_directory_is_refused_and_directory_survives(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "somedir"
    d.mkdir()
    (d / "keepme.txt").write_text("still here")
    out = _out(W._run({"file_path": "somedir", "content": "oops"}))
    assert out.startswith("Error"), out
    assert d.is_dir(), "ディレクトリがファイルに置き換わった"
    assert (d / "keepme.txt").read_text() == "still here"


def test_write_through_symlink_that_escapes_the_workspace_is_denied(tmp_path, monkeypatch):
    """trusted モードでは記号リンクは辿って実体を書く（atomic.py の意図した
    挙動・test_atomic_writes.py 済み）。**workspace モードでは、辿った先が
    作業ディレクトリの外なら拒否される**こと（policy.check_path が
    resolve() 後のパスで判定するため）。symlink 経由で作業ディレクトリ外へ
    書ける抜け道になっていないかを、実際にリンクを辿らせて確かめる。
    """
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    monkeypatch.chdir(ws)

    outside = tmp_path / "outside_secret.txt"
    outside.write_text("do not touch")
    link = ws / "escape_link.txt"
    link.symlink_to(outside)

    out = _out(W._run({"file_path": str(link), "content": "PWNED"}))
    assert out.startswith("Error"), out
    assert "policy denied" in out
    assert outside.read_text() == "do not touch", "workspace 外の実ファイルが書き換わった"


def test_edit_through_symlink_that_escapes_the_workspace_is_denied(tmp_path, monkeypatch):
    """Edit にも同じ抜け道が無いこと（Write だけ塞いでいて Edit が空いている、
    という「片方で直して片方に残した」形を作らない）。"""
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    monkeypatch.chdir(ws)

    outside = tmp_path / "outside_conf.txt"
    outside.write_text("listen 80;\n")
    link = ws / "escape_link.conf"
    link.symlink_to(outside)

    out = _out(E._run({"file_path": str(link), "old_string": "80", "new_string": "8080"}))
    assert out.startswith("Error"), out
    assert "policy denied" in out
    assert outside.read_text() == "listen 80;\n"
