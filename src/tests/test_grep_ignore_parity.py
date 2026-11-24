"""rg の無い機械は、**rg が探さない場所まで探していた**（線D の続き・2026-08-31 実測）。

E1 は「綴りの方言」を揃えた。揃っていなかったのは **どこを探すか** のほう。
同じ問い（`tool_failed`）・同じ木（/home/user/project）で測った:

    engine                 一致      表示できた行
    -------------------    ------    ------------
    rg                     167 件    31 行
    grep -P（rg 無し）      201 件    19 行
    Python の re（両方無し） 197 件    11 行

余分な 13 本はすべて git が無視している場所——`app/pyruntime/`（配布用の写し）と
`app/src-tauri/target/…`（ビルド出力）にある **`agent.py` `serve.py` の古い複製**だった。
害は 2 つあって、どちらも実測できる:
  ① 表示の枠はファイルごとに公平に配るので、複製が増えるほど本物が押し出される。
  ② モデルは出てきた道を読み書きする。**ビルド出力の写しを直す**のは、
     この現場が何度も踏んだ形（直したのに何も変わらない）。

直したあと、同じ問いで **3 エンジンとも 168 件**（ファイル単位・件数まで一致）。

★規則は自分で書かない。`.venv` や `node_modules` を名前で外す一覧は、
  その名前を正当に使っている木で**嘘（0 件）**を作る。git に聞けば、
  rg が見せるものをこちらが隠すことはない——それをこの試験で押さえる。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from laps.tools import grep as gr

#: 試験の入力は**実物のソース**を使う（作り物の "hello" では行の長さも中身も嘘になる）。
_REAL = Path(__file__).resolve().parents[1] / "laps" / "tools" / "fold.py"
_NEEDLE = "round_robin"          # 実際に _REAL の中にある語


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setattr(gr, "_GREP_FLAG", None)


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """実物のソースを、追跡する場所と **git が無視する場所** の両方に置いた木。

    ＝実機で見つけた形そのもの（`app/pyruntime/…` は `app/.gitignore` に載っている
    配布用の写しで、中身は本物のソースの複製だった）。
    """
    if not shutil.which("git"):
        pytest.skip("git が無い")
    _git("init", "-q", ".", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("build/\nvendor/\n", encoding="utf-8")
    body = _REAL.read_text(encoding="utf-8")
    for rel in ("pkg/fold.py", "build/pkg/fold.py", "vendor/copy/fold.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def _no_rg(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(gr.shutil, "which",
                        lambda n, *a, **k: None if n == "rg" else real(n, *a, **k))


def _no_rg_no_grep(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(gr.shutil, "which",
                        lambda n, *a, **k: None if n in ("rg", "grep") else real(n, *a, **k))


def _files_in(out: str, root: Path) -> set[str]:
    got = set()
    for ln in str(out).splitlines():
        head = ln.split(":", 1)[0]
        if head.startswith(str(root)):
            got.add(os.path.relpath(head, str(root)))
    return got


def _search(root, **kw):
    return gr._run({"pattern": _NEEDLE, "path": str(root), "max_results": 10000, **kw})


# ── 1. rg と同じ場所だけを探す ────────────────────────────────────────

@pytest.mark.skipif(not shutil.which("rg"), reason="比べる相手（rg）が無い")
@pytest.mark.parametrize("engine", ["grep", "python"])
def test_代替エンジンもrgと同じファイルだけを返す(repo, monkeypatch, engine):
    want = _files_in(_search(repo), repo)          # rg（基準）
    assert want == {"pkg/fold.py"}, want           # 基準そのものの確認
    (_no_rg if engine == "grep" else _no_rg_no_grep)(monkeypatch)
    got = _files_in(_search(repo), repo)
    assert got == want, f"{engine} が rg と違う場所を探している: {got ^ want}"


@pytest.mark.parametrize("engine", ["grep", "python"])
def test_gitが無視する複製は代替エンジンでも出てこない(repo, monkeypatch, engine):
    """rg が無い機械でも `build/` と `vendor/` の写しは出さない。"""
    (_no_rg if engine == "grep" else _no_rg_no_grep)(monkeypatch)
    got = _files_in(_search(repo), repo)
    assert "build/pkg/fold.py" not in got
    assert "vendor/copy/fold.py" not in got


# ── 2. 隠してはいけないもの（対の試験）─────────────────────────────

@pytest.mark.parametrize("engine", ["grep", "python"])
def test_追跡されているファイルは必ず出る(repo, monkeypatch, engine):
    """除外が効きすぎて**本物を隠す**のが、この道具でいちばん高くつく嘘。"""
    (_no_rg if engine == "grep" else _no_rg_no_grep)(monkeypatch)
    assert "pkg/fold.py" in _files_in(_search(repo), repo)


@pytest.mark.parametrize("engine", ["grep", "python"])
def test_名指しされた無視対象は探す(repo, monkeypatch, engine):
    """実測: `rg needle build` は `build/` が .gitignore にあっても一致を返す。

    ここで外すと「無視されている場所の中を見たい」という指定に 0 件を返す
    ——`_hidden_excludes` が隠しディレクトリについてしているのと同じ約束。
    """
    (_no_rg if engine == "grep" else _no_rg_no_grep)(monkeypatch)
    out = _search(repo / "build")
    assert "pkg/fold.py" in _files_in(out, repo / "build"), str(out)[:300]


@pytest.mark.parametrize("engine", ["grep", "python"])
def test_gitが無い機械では何も外さない(repo, monkeypatch, engine):
    """git に聞けないなら**除外しない**（見せすぎる側へ倒す）。"""
    (_no_rg if engine == "grep" else _no_rg_no_grep)(monkeypatch)
    inner = gr.shutil.which
    monkeypatch.setattr(gr.shutil, "which",
                        lambda n, *a, **k: None if n == "git" else inner(n, *a, **k))
    got = _files_in(_search(repo), repo)
    assert "build/pkg/fold.py" in got, "git が引けないのに除外している（隠す側へ倒れている）"


# ── 3. 根がリポジトリでなくても効く（実機で効かなかった形）───────────

@pytest.mark.parametrize("engine", ["grep", "python"])
def test_根がリポジトリでなくても下のリポジトリの規則が効く(tmp_path, monkeypatch, engine):
    """実測でここに落ちた。`/home/user/project` 自体はリポジトリではないが、
    その下に 5 つのリポジトリ（src・app・scripts・ops・scratch/v2check）があり、
    **rg は降りながらそれぞれの規則を適用する**。根に 1 回聞くだけの実装では
    201 件のまま 1 件も減らなかった。
    """
    if not shutil.which("git"):
        pytest.skip("git が無い")
    outer = tmp_path / "workspace"            # ← リポジトリではない
    inner = outer / "proj"
    inner.mkdir(parents=True)
    _git("init", "-q", ".", cwd=inner)
    (inner / ".gitignore").write_text("build/\n", encoding="utf-8")
    body = _REAL.read_text(encoding="utf-8")
    for rel in ("src/fold.py", "build/fold.py"):
        p = inner / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    (_no_rg if engine == "grep" else _no_rg_no_grep)(monkeypatch)
    got = _files_in(_search(outer), outer)
    assert "proj/src/fold.py" in got
    assert "proj/build/fold.py" not in got, "下のリポジトリの無視規則が効いていない"


# ── 4. Python で歩く経路（Windows の出荷物が必ず通る）─────────────────

def test_8MBより後ろの一致も見つかる(tmp_path, monkeypatch):
    """旧実装は `f.read(8*1024*1024)` で**その先を黙って捨てていた**。

    実測（2026-08-31）: 47MB の実ファイル `scratch/claude-app/strings.txt`
    （NUL の無い普通のテキスト）で、8.45MB の位置にある `tool_failed` 4 件が
    rg には見え、Python 経路には見えなかった。道具は「(no matches)」としか
    言わないので、**「その語は無い」という嘘**になる。
    """
    big = tmp_path / "strings.txt"
    filler = ("x" * 199 + "\n") * 45_000          # 約 9MB
    big.write_text(filler + "needle_beyond_the_cap\n" + filler, encoding="utf-8")
    assert big.stat().st_size > 8 * 1024 * 1024
    _no_rg_no_grep(monkeypatch)
    out = gr._run({"pattern": "needle_beyond_the_cap", "path": str(tmp_path)})
    assert "needle_beyond_the_cap" in str(out) and "no matches" not in str(out)


def test_行番号がgrepと一致する(tmp_path, monkeypatch):
    """`str.splitlines()` は `\\r` や改ページでも切るので、そういう字を含む
    ファイルでは**行番号が grep とずれる**（ログの進捗表示で普通に起きる）。"""
    if not shutil.which("grep"):
        pytest.skip("比べる相手（grep）が無い")
    log = tmp_path / "server.log"
    log.write_bytes(b"start\ndownload 10%\rdownload 90%\r\x0cpage break\nNEEDLE_HERE\n")
    _no_rg_no_grep(monkeypatch)
    out = str(gr._run({"pattern": "NEEDLE_HERE", "path": str(tmp_path)}))
    ours = [ln for ln in out.splitlines() if "NEEDLE_HERE" in ln][0]
    ref = subprocess.run(["grep", "-n", "NEEDLE_HERE", str(log)],
                         capture_output=True, text=True).stdout.strip()
    assert ours.split(":")[1] == ref.split(":")[0], f"{ours!r} vs grep {ref!r}"


def test_時間切れを0件と言わない(tmp_path, monkeypatch):
    """rg と grep には `timeout=30` があるのに、**Python 経路にだけ上限が無かった**。
    実測 0.06 秒（rg）に対して 11.1 秒、しかも木が大きくなるほど止まらない。
    ここは rg も grep も無い機械＝Windows の出荷物が必ず通る経路。

    打ち切ったときに "(no matches)" と言えば、**時間切れが「その語は無い」に化ける**。
    """
    body = _REAL.read_text(encoding="utf-8")
    for i in range(50):
        p = tmp_path / f"d{i}" / "fold.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _no_rg_no_grep(monkeypatch)
    monkeypatch.setenv("LAPS_GREP_TIMEOUT", "0.001")
    out = str(gr._run({"pattern": "no_such_token_zzz", "path": str(tmp_path)}))
    assert out.startswith("Error:") and "打ち切" in out, out[:200]
    assert "no matches" not in out
    # 対の試験: 時間があれば、同じ問いは正直に 0 件と答える。
    monkeypatch.setenv("LAPS_GREP_TIMEOUT", "30")
    ok = gr._run({"pattern": "no_such_token_zzz", "path": str(tmp_path)})
    assert str(ok).startswith("(no matches)") and getattr(ok, "kind", None) == "empty"
