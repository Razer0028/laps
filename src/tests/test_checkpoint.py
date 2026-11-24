"""A5「やり直せる」の試験。

**固定したいこと**（ACCEPTANCE.md A5 の完了基準そのまま）:
1. 3ファイル編集して2つ目で失敗 → 戻すと3つとも元通り
2. 戻さなければ変更は残る（★既定の挙動を変えていない、の証拠）
3. 上限超過が黙って消えず、記録されて報告に出る
4. Windows 風のパス（大文字小文字・`\\` 区切り・非ASCII 名）で壊れない
5. 書いたものが繋がっている（agent の choke point を実際に通る）

★2 が一番大事。「戻せる」機構は、**呼ばれていないのに戻す**ようになった瞬間に
利用者の意図した変更を消す道具に変わる。だから「呼ばなければ何も起きない」を
機械で固定しておく。
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path

import pytest

from laps import checkpoint as cp_mod
from laps.checkpoint import Checkpoint, Limits, normalize_key


# ---- 道具立て ---------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """控えの置き場所。**作業ディレクトリの外**（本番と同じ関係）。"""
    d = tmp_path / "store"
    return d


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


def _edit(path: Path, old: str, new: str) -> str:
    """本物の Edit ツールを通す（自前の書き込みで代用すると試験が嘘になる）。"""
    from laps.tools.edit import _run
    return _run({"file_path": str(path), "old_string": old, "new_string": new})


# ---- 1. 途中失敗からの復帰（A5 の本命）--------------------------------

def test_三ファイル編集の二つ目で失敗しても戻せば三つとも元通り(store, work):
    a, b, c = work / "a.py", work / "b.py", work / "c.py"
    a.write_text("x = 1\n")
    b.write_text("y = 2\n")
    c.write_text("z = 3\n")
    before = {p: p.read_text() for p in (a, b, c)}

    cp = Checkpoint(store, task_id="t1")

    # 1つ目: 成功
    cp.record(str(a))
    assert _edit(a, "x = 1", "x = 11").startswith("OK")
    # 2つ目: 成功したあと…
    cp.record(str(b))
    assert _edit(b, "y = 2", "y = 22").startswith("OK")
    # …3つ目で失敗する（old_string が無い＝現実に一番多い失敗の形）
    cp.record(str(c))
    fail = _edit(c, "存在しない行", "何か")
    assert fail.startswith("Error:"), fail

    # 半分だけ新しい木ができている＝これが戻したい状態
    assert a.read_text() == "x = 11\n"
    assert b.read_text() == "y = 22\n"

    rep = cp.rollback()
    assert rep.ok, rep.text()
    assert rep.complete, rep.text()
    for p in (a, b, c):
        assert p.read_text() == before[p], f"{p.name} が元に戻っていない"
    assert sorted(Path(x).name for x in rep.restored) == ["a.py", "b.py"]
    # c は書き換わっていないので「元から同じ」に入る（戻す必要が無かった）
    assert [Path(x).name for x in rep.unchanged] == ["c.py"]


def test_この依頼で作られたファイルは戻すと消える(store, work):
    made = work / "新規" / "できたファイル.txt"
    cp = Checkpoint(store, task_id="t2")
    cp.record(str(made))          # まだ無い状態を控える
    made.parent.mkdir(parents=True)
    made.write_text("できた\n")

    rep = cp.rollback()
    assert rep.ok, rep.text()
    assert not made.exists(), "新規作成が消えていない"
    assert not made.parent.exists(), "自分が作ったディレクトリが残っている"
    assert [Path(x).name for x in rep.deleted] == ["できたファイル.txt"]


def test_自分が作っていないディレクトリは消さない(store, work):
    d = work / "元からある"
    d.mkdir()
    keep = d / "他人のファイル"
    keep.write_text("触るな\n")
    made = d / "新しい.txt"

    cp = Checkpoint(store, task_id="t3")
    cp.record(str(made))
    made.write_text("x\n")
    cp.rollback()

    assert not made.exists()
    assert d.exists() and keep.read_text() == "触るな\n", "無関係なものを巻き込んだ"


def test_二回戻しても壊れない(store, work):
    f = work / "a.txt"
    f.write_text("元\n")
    cp = Checkpoint(store, task_id="t4")
    cp.record(str(f))
    f.write_text("新\n")

    assert cp.rollback().ok
    rep2 = cp.rollback()
    assert rep2.ok and rep2.restored == [] and len(rep2.unchanged) == 1
    assert f.read_text() == "元\n"


def test_控えるのは最初の一回だけ_依頼開始時点が保たれる(store, work):
    f = work / "a.txt"
    f.write_text("最初\n")
    cp = Checkpoint(store, task_id="t5")
    assert cp.record(str(f)) == "recorded"
    f.write_text("途中\n")
    assert cp.record(str(f)) == "already", "2回目で控えを上書きしている"
    f.write_text("最後\n")
    cp.rollback()
    assert f.read_text() == "最初\n", "『元』が途中の姿にすり替わっている"


# ---- 2. 呼ばなければ何も起きない（既定の挙動を変えない）---------------

def test_戻さなければ変更は残る(store, work):
    f = work / "a.txt"
    f.write_text("元\n")
    cp = Checkpoint(store, task_id="t6")
    cp.record(str(f))
    f.write_text("利用者が意図した変更\n")
    # rollback を呼ばない
    assert f.read_text() == "利用者が意図した変更\n"
    assert cp.touched() == [str(f)]


def test_モデルにrollbackツールを渡していない():
    """★モデルが自分の失敗を隠すために巻き戻せてはいけない。"""
    from laps import tools as tools_mod
    names = {t.name for t in tools_mod.all_specs()}
    for bad in ("Rollback", "Checkpoint", "Undo", "Revert"):
        assert bad not in names, f"{bad} がモデルから呼べる位置にある"


# ---- 3. 上限（黙って諦めない）-----------------------------------------

def test_大きすぎるファイルは控えず理由を記録する(store, work):
    big = work / "big.bin"
    big.write_bytes(b"A" * 5000)
    cp = Checkpoint(store, task_id="t7", limits=Limits(max_file_bytes=1000))
    assert cp.record(str(big)) == "skipped:too_large"
    assert len(cp.skipped) == 1 and cp.skipped[0].reason == "too_large"

    big.write_bytes(b"B" * 5000)
    rep = cp.rollback()
    assert rep.ok, "控えていないものを『失敗』に混ぜている"
    assert not rep.complete, "控え漏れがあるのに『完全に戻した』と言っている"
    assert "big.bin" in rep.text() and "上限" in rep.text()
    assert big.read_bytes() == b"B" * 5000, "控えていないのに戻したことにしている"


def test_控えの総量の上限で打ち切り理由が残る(store, work):
    files = []
    for i in range(4):
        p = work / f"f{i}.txt"
        p.write_bytes(b"x" * 400)
        files.append(p)
    cp = Checkpoint(store, task_id="t8", limits=Limits(max_total_bytes=1000))
    got = [cp.record(str(p)) for p in files]
    assert got[:2] == ["recorded", "recorded"]
    assert got[2] == "skipped:store_full", got
    assert any(s.reason == "store_full" for s in cp.skipped)
    assert "控えの総量" in "".join(s.text() for s in cp.skipped)


def test_ファイル数の上限で打ち切り理由が残る(store, work):
    cp = Checkpoint(store, task_id="t9", limits=Limits(max_files=2))
    for i in range(3):
        p = work / f"g{i}.txt"
        p.write_text("x")
        cp.record(str(p))
    assert [s.reason for s in cp.skipped] == ["too_many_files"]


def test_Bashは控えの対象外だと報告に出る(store, work):
    f = work / "a.txt"
    f.write_text("元\n")
    cp = Checkpoint(store, task_id="t10")
    cp.record(str(f))
    cp.note_untracked("Bash", "sed -i s/a/b/ 別のファイル")
    f.write_text("新\n")
    rep = cp.rollback()
    assert rep.ok and not rep.complete, "Bash が走ったのに『完全に戻した』と言っている"
    assert "対象外" in rep.text() and "sed -i" in rep.text()


def test_上限は環境変数で変えられる(monkeypatch):
    monkeypatch.setenv("LAPS_CHECKPOINT_MAX_FILE_BYTES", "123")
    monkeypatch.setenv("LAPS_CHECKPOINT_MAX_FILES", "7")
    lim = Limits.from_env()
    assert lim.max_file_bytes == 123 and lim.max_files == 7
    # 壊れた値で落ちない（この層の不具合で本体を止めない）
    monkeypatch.setenv("LAPS_CHECKPOINT_MAX_FILES", "たくさん")
    assert Limits.from_env().max_files == cp_mod.DEFAULT_MAX_FILES


# ---- 4. Windows で壊れない --------------------------------------------

def test_windows風のパスは大文字小文字と区切りを吸収して同じ鍵になる():
    """★これを外すと、同じファイルを2回控えて1回目（＝本物の元）を失う。"""
    k1 = normalize_key(r"C:\Proj\App\Main.PY", _pathmod=ntpath)
    k2 = normalize_key("c:/proj/app/main.py", _pathmod=ntpath)
    assert k1 == k2 == r"c:\proj\app\main.py"


def test_posixでは大文字小文字を潰さない():
    """Linux では Main.py と main.py は別のファイル。潰したら別物を壊す。"""
    import posixpath
    a = normalize_key("/tmp/x/Main.py", _pathmod=posixpath)
    b = normalize_key("/tmp/x/main.py", _pathmod=posixpath)
    assert a != b


def test_バックスラッシュを含む名前でも壊れない(store, work):
    """POSIX では `\\` はただの文字。区切りとして雑に split すると別物を触る。"""
    odd = work / "a\\b.txt"
    odd.write_text("元\n")
    cp = Checkpoint(store, task_id="t11")
    assert cp.record(str(odd)) == "recorded"
    odd.write_text("新\n")
    assert cp.rollback().ok
    assert odd.read_text() == "元\n"


def test_非ASCIIの名前と内容がバイト単位で戻る(store, work):
    f = work / "設定ファイル・本番.yaml"
    original = "名前: 本番\r\n値: 　全角スペース\r\n".encode("utf-8")
    f.write_bytes(original)
    cp = Checkpoint(store, task_id="t12")
    cp.record(str(f))
    f.write_bytes(b"broken")
    assert cp.rollback().ok
    assert f.read_bytes() == original, "CRLF か非ASCII が変質した"


def test_控えの名前はASCIIで短い(store, work):
    """Windows の 260 文字制限と文字化けを避けるため、控えはハッシュ名にする。"""
    f = work / "とても長い名前の設定ファイル・本番・第2版.yaml"
    f.write_text("x")
    cp = Checkpoint(store, task_id="t13")
    cp.record(str(f))
    blobs = [p.name for p in Path(cp.root).iterdir()]
    assert len(blobs) == 1
    assert blobs[0].isascii() and len(blobs[0]) <= 48, blobs


@pytest.mark.skipif(os.name == "nt", reason="Windows に実行ビットは無い")
def test_実行ビットも戻る(store, work):
    """戻した shell スクリプトが実行できない、は静かな壊れ方。"""
    f = work / "deploy.sh"
    f.write_text("#!/bin/sh\necho 元\n")
    os.chmod(f, 0o755)
    cp = Checkpoint(store, task_id="t21")
    cp.record(str(f))
    f.write_text("#!/bin/sh\necho 新\n")
    os.chmod(f, 0o600)
    assert cp.rollback().ok
    assert f.read_text().endswith("元\n")
    assert oct(f.stat().st_mode & 0o777) == "0o755", "実行ビットが落ちた"


def test_バイナリも壊さずに戻る(store, work):
    f = work / "data.bin"
    original = bytes(range(256)) * 4
    f.write_bytes(original)
    cp = Checkpoint(store, task_id="t14")
    cp.record(str(f))
    f.write_bytes(b"\x00\x01")
    assert cp.rollback().ok
    assert f.read_bytes() == original


def test_戻せなかったら失敗として報告する(store, work, monkeypatch):
    """★「戻しました」と言って戻っていない、が一番危ない嘘。"""
    f = work / "a.txt"
    f.write_text("元\n")
    cp = Checkpoint(store, task_id="t15")
    cp.record(str(f))
    f.write_text("新\n")

    def boom(*a, **k):
        raise OSError("ロックされています")  # Windows のファイルロックを模す
    monkeypatch.setattr(cp_mod.os, "replace", boom)

    rep = cp.rollback()
    assert not rep.ok and len(rep.failed) == 1
    assert "戻せなかった" in rep.text()
    assert f.read_text() == "新\n"


# ---- 5. 差分（git を起こさない）---------------------------------------

def test_差分はハーネスが計算して利用者のgitを触らない(store, work):
    f = work / "a.py"
    f.write_text("x = 1\n")
    cp = Checkpoint(store, task_id="t16")
    cp.record(str(f))
    f.write_text("x = 2\n")
    d = cp.diff()
    assert "-x = 1" in d and "+x = 2" in d
    assert not (work / ".git").exists(), "利用者の作業場所に git を作った"
    assert not any(Path(cp.root).glob("**/.git")), "控えの側に git を作った"


def test_変更が無ければ差分は空だと言う(store, work):
    f = work / "a.py"
    f.write_text("x = 1\n")
    cp = Checkpoint(store, task_id="t17")
    cp.record(str(f))
    assert "変更はありません" in cp.diff()


# ---- 6. 書いたものが繋がっている（★5回踏んだ失敗）---------------------

class _FakeBackend:
    def chat(self, *a, **k):
        return "テスト応答"

    def chat_with_tools(self, *a, **k):
        return "テスト応答", None

    def close(self):
        pass


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path / "sess"))
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_CHECKPOINT_DIR", str(tmp_path / "cpstore"))
    monkeypatch.setenv("LAPS_GROUNDING", "0")
    from laps.oc.base import Agent
    a = Agent()
    a.backend = _FakeBackend()
    return a


def test_何も触っていないなら戻したとは言わない(store):
    cp = Checkpoint(store, task_id="t20")
    rep = cp.rollback()
    assert rep.restored == [] and rep.deleted == []
    assert "0 ファイル" in rep.text(), rep.text()


def test_ツールを実行する場所は一箇所しかない():
    """★控えは `_run_one`（oc/loop.py）の1点にしか置いていない。二つ目の実行経路ができた
    瞬間、そこを通った編集は**黙って戻せなくなる**（控えが無いのに rollback は
    成功と言う）。だから経路が増えたらここで落ちるようにしておく。

    増やすときは `_checkpoint_before_tool` をその経路にも通してから、この試験の
    期待値を直すこと。
    """
    import ast
    sites = []
    guarded = set()
    for p in (Path(__file__).resolve().parents[1] / "laps").rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        # 行番号ではなく「どの関数の中か」で押さえる。この木は今も別の作業で
        # 動いているので、行番号で固定すると無関係な編集で赤くなる。
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "handler":
                        sites.append(f"{p.name}::{fn.name}")
                    elif node.func.attr == "_checkpoint_before_tool":
                        guarded.add(f"{p.name}::{fn.name}")
    # ★2026-10-18 更新: 実行の中核が oc（loop.py の _run_one）に移ってから、この試験は経路が 4 つで赤のままだった。
    #   _run_one も呼ぶ直前に控えを取っている（_checkpoint_before_tool）。tools.py の 2 つは、oc の Read・WebFetch が
    #   旧い道具の handler に読みを任せているだけ（書き換えない）。経路が増えたら落ち、控えの無い経路でも落ちる。
    READ_ONLY_DELEGATES = {"tools.py::read_tool", "tools.py::webfetch_tool"}
    #   ★2026-10-21 旧中核（agent.py の _exec_tool）を外した。
    assert sorted(sites) == sorted(["loop.py::_run_one", *READ_ONLY_DELEGATES]), (
        f"ツールの実行経路が {sites} にある。控えは _run_one の直前にしか"
        "置いていないので、他の経路を通った編集は戻せない"
        "（rollback は「全部戻した」と言うのに戻っていない、が起きる）。")
    unguarded = [s for s in sites if s not in READ_ONLY_DELEGATES and s not in guarded]
    assert not unguarded, f"控え（_checkpoint_before_tool）を通らずに道具を実行する経路: {unguarded}"


def test_依頼ごとに控えが張り替わる(agent, monkeypatch):
    agent._begin_checkpoint()
    first = agent.checkpoint
    first_root = Path(first.root)
    first_root.mkdir(parents=True, exist_ok=True)
    agent._begin_checkpoint()
    assert agent.checkpoint is not first, "前の依頼の控えを使い回している"
    assert not first_root.exists(), "前の依頼の控えが残ってディスクが育つ"


# ---- 7. どのツールがどのパスを触るかの表 -------------------------------

def test_全ツールが分類済みで漏れがない():
    """★新しいツールを足したとき、分類し忘れると**そのツールの編集だけ静かに
    戻せなくなる**（rollback は「全部戻した」と言う）。ここで落として、
    足した人に「控えるのか・戻せないと言うのか・関係ないのか」を決めさせる。
    """
    from laps import tools as tools_mod
    known = (cp_mod.TRACKED_TOOLS | cp_mod.UNTRACKABLE_TOOLS
             | cp_mod.HARNESS_ONLY_TOOLS)
    readonly = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "Umbra",
                "RemoteRead", "SystemState", "AskUserQuestion", "BashOutput"}
    names = {t.name for t in tools_mod.all_specs()}
    unclassified = names - known - readonly
    assert not unclassified, (
        f"分類されていないツール: {sorted(unclassified)}。"
        "checkpoint.py の TRACKED_TOOLS / UNTRACKABLE_TOOLS / "
        "HARNESS_ONLY_TOOLS のどれかに入れること。")
    # 控える側は、実際にパスを取り出せること（表に名前だけ足して実装を忘れない）
    for n in cp_mod.TRACKED_TOOLS:
        assert n in ("Write", "Edit", "NotebookEdit", "ApplyDiff")


def test_書き込み系ツールのパス抽出():
    P = cp_mod.paths_for_tool
    assert P("Write", {"file_path": "/tmp/a.txt"}) == ["/tmp/a.txt"]
    assert P("Edit", {"path": "/tmp/b.txt"}) == ["/tmp/b.txt"]
    assert P("NotebookEdit", {"notebook_path": "/tmp/n.ipynb"}) == ["/tmp/n.ipynb"]
    assert P("Read", {"file_path": "/tmp/a.txt"}) == []
    assert P("Bash", {"command": "rm -rf /"}) == []
    assert P("Write", {}) == [] and P("Write", None) == []


def test_WriteのSFTパス書き換えと同じ場所を控える(monkeypatch, tmp_path):
    """Write は偽パスを cwd へ書き換えてから書く。控えが元のパスを見ていると、
    **書かれるファイルと控えたファイルが別物**になって戻せない。"""
    monkeypatch.chdir(tmp_path)
    got = cp_mod.paths_for_tool(
        "Write", {"file_path": "/opt/blackwall-sandbox/workspace/x.txt"})
    assert got == [os.path.join(str(tmp_path), "x.txt")], got


def test_ApplyDiffは全ファイルぶんのパスを返す():
    diff = ("a.py\n<<<<<<< SEARCH\n1\n=======\n2\n>>>>>>> REPLACE\n\n"
            "b.py\n<<<<<<< SEARCH\n3\n=======\n4\n>>>>>>> REPLACE\n")
    assert cp_mod.paths_for_tool("ApplyDiff", {"diff": diff}) == ["a.py", "b.py"]


def test_控え置き場は作業ディレクトリの外(monkeypatch, tmp_path):
    """中に置くと、エージェント自身の Glob/Grep が控えを拾って作業に混ざる。"""
    monkeypatch.delenv("LAPS_CHECKPOINT_DIR", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("LAPS_HOME", str(home))
    elsewhere = tmp_path / "どこか別の場所"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    root = cp_mod.store_root()
    assert root.name == "checkpoints" and "home" in str(root)


def test_LAPS_HOMEが作業場所の中を指したら一時ディレクトリへ逃がす(monkeypatch, tmp_path):
    """★ベンチは LAPS_HOME を測定対象のディレクトリの中へ向ける（実在する）。
    そのまま置くと、控えがベンチの成果物に混ざって数字が汚れる。"""
    work = tmp_path / "測定対象"
    work.mkdir()
    monkeypatch.delenv("LAPS_CHECKPOINT_DIR", raising=False)
    monkeypatch.setenv("LAPS_HOME", str(work / ".blackwall"))
    monkeypatch.chdir(work)
    root = cp_mod.store_root()
    assert not cp_mod._inside(root, work), f"作業場所の中に控えを置いている: {root}"


def test_明示指定された置き場所は尊重する(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_CHECKPOINT_DIR", str(tmp_path / "ここ"))
    assert cp_mod.store_root() == tmp_path / "ここ"


def test_殺された実行の控えは古くなったら片付く(tmp_path):
    """close() は正常終了の道しか通らない。SIGKILL のぶんが永久に積むのを止める。"""
    base = tmp_path / "cpstore"
    old = base / "殺された実行"
    old.mkdir(parents=True)
    (old / "aaa.blob").write_bytes(b"x" * 10)
    os.utime(old, (0, 0))  # 十分に古くする
    fresh = base / "動いている実行"
    fresh.mkdir()
    (fresh / "bbb.blob").write_bytes(b"y")

    assert cp_mod.prune_store(base, max_age_hours=1) == 1
    assert not old.exists(), "古い控えが残った"
    assert fresh.exists(), "動いている実行の控えを消した"


def test_見知らぬ中身のディレクトリは片付けない(tmp_path):
    """★置き場所を取り違えて指定されたときに、他人のファイルを消さない。"""
    base = tmp_path / "cpstore"
    d = base / "利用者の書類"
    d.mkdir(parents=True)
    (d / "大事な資料.docx").write_text("消すな")
    os.utime(d, (0, 0))
    assert cp_mod.prune_store(base, max_age_hours=1) == 0
    assert (d / "大事な資料.docx").read_text() == "消すな"


def test_closeは知らないファイルを巻き込まない(store, work):
    f = work / "a.txt"
    f.write_text("元\n")
    cp = Checkpoint(store, task_id="t19")
    cp.record(str(f))
    intruder = Path(cp.root) / "誰かが置いた.txt"
    intruder.write_text("消すな")
    cp.close()
    assert intruder.read_text() == "消すな", "掃除のつもりで他人のものを消した"
    assert not any(Path(cp.root).glob("*.blob")), "自分の控えは消していない"


def test_closeで控えが消える(store, work):
    f = work / "a.txt"
    f.write_text("元\n")
    cp = Checkpoint(store, task_id="t18")
    cp.record(str(f))
    assert Path(cp.root).exists()
    cp.close()
    assert not Path(cp.root).exists()
    assert cp.record(str(f)) == "closed", "閉じたあとも控えている"


# ---- 7. ウォーターマーク（部分合格の最高到達点）— core-hold 2026-09-05 ------
#
# ★固定したいこと:
#   a. 戻る先は**控えた時点**であって依頼開始時点ではない（rollback と別の層）
#   b. 2 回目の控えが 1 回目を置き換える（最高到達点は更新される）
#   c. 控えた後に作ったファイルは restore で消え、控えた後に消したファイルは戻る
#   d. 控えが無ければ restore_watermark は None で**何もしない**（呼ばれない限り戻さない）
#   e. 報告の untracked は控え**以降**の Bash だけ（合格した時点より前の操作は合格の一部）
#   f. close で控えの blob も消える

def test_ウォーターマークは控えた時点へ戻し依頼開始時点へは戻らない(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="wm_a")
    cp.record(str(a))
    assert _edit(a, "v0", "v1").startswith("OK")       # 合格した姿
    assert cp.snapshot_watermark() == 1
    assert cp.has_watermark
    assert _edit(a, "v1", "v2").startswith("OK")       # 劣化編集

    rep = cp.restore_watermark()
    assert rep is not None and rep.ok and rep.complete, rep and rep.text()
    assert [Path(x).name for x in rep.restored] == ["a.py"]
    assert a.read_text() == "v1\n", "依頼開始時点(v0)ではなく控えた時点(v1)へ戻る"
    assert "最後に合格数を更新した時点" in rep.text()
    # rollback（依頼開始時点）は別の層のまま生きている
    assert cp.rollback().ok and a.read_text() == "v0\n"


def test_二回目のウォーターマークが一回目を置き換える(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="wm_b")
    cp.record(str(a))
    _edit(a, "v0", "v1")
    cp.snapshot_watermark()
    _edit(a, "v1", "v2")
    cp.snapshot_watermark()                              # 最高到達点の更新
    _edit(a, "v2", "v3")
    rep = cp.restore_watermark()
    assert rep is not None and a.read_text() == "v2\n"
    assert cp.watermark_count == 2
    # blob は依頼開始時点ぶんと控えぶんの 2 つだけ（古い控えが積まれていない）
    assert len(list(store.glob("*.blob"))) == 2


def test_控えた後に作ったファイルは消え_消したファイルは戻る(store, work):
    a, b = work / "a.py", work / "b.py"
    a.write_text("keep\n")
    cp = Checkpoint(store, task_id="wm_c")
    cp.record(str(a))
    cp.snapshot_watermark()                              # この時点: a あり・b なし
    cp.record(str(b))                                    # 控えた**後**に初めて触る
    b.write_text("junk\n")
    a.unlink()                                           # 控えた後に消す
    rep = cp.restore_watermark()
    assert rep is not None, "控えがあるのに None"
    assert a.read_text() == "keep\n"
    assert not b.exists(), "控えた時点に無かった物は残さない（run34 の残骸の形）"
    assert [Path(x).name for x in rep.deleted] == ["b.py"]


def test_控えが無ければrestore_watermarkはNoneで何もしない(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="wm_d")
    cp.record(str(a))
    _edit(a, "v0", "v1")
    assert not cp.has_watermark
    assert cp.restore_watermark() is None
    assert a.read_text() == "v1\n", "呼ばれていないのに戻している"


def test_報告のuntrackedは控え以降の分だけ(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="wm_e")
    cp.record(str(a))
    cp.note_untracked("Bash", "php -S …（合格の一部）")
    cp.snapshot_watermark()
    cp.note_untracked("Bash", "rm data/rx.db")
    rep = cp.restore_watermark()
    assert rep is not None
    assert rep.untracked == ["Bash: rm data/rx.db"], rep.untracked
    assert "控えの対象外の操作が 1 件" in rep.text()


def test_closeでウォーターマークのblobも消える(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="wm_f")
    cp.record(str(a))
    cp.snapshot_watermark()
    assert len(list(store.glob("*.blob"))) == 2
    cp.close()
    assert not store.exists() or not list(store.glob("*.blob"))
    assert cp.snapshot_watermark() == -1                 # closed のあとは控えない
    assert cp.restore_watermark() is None


# ---- 8. 第4回b（2026-09-05）: 復元前の控え（stash）・容量の合算・木ごとの控え・Bash の分類 ----
#
# ★固定したいこと:
#   g. stash→restore_watermark→unstash で**復元前の状態**に戻る（opencode revert.ts:70 の形）
#   h. stats()["bytes"] は依頼開始時点＋ウォーターマーク＋stash の合計で、record の上限も合算で見る
#   i. 木ごとの控え: ドットのディレクトリ・pid/log・シンボリックリンクは控えない／上限超えは木を諦める
#   j. tree_scope_allowed は `/`・ホーム・浅い場所・控え置き場を含む場所を断る
#   k. classify_bash は本番と再生（bench/replay_hold.py）が同じ 1 本
#   l. restore_watermark(keep_created=True) は控え以降に作った物を消さない

def test_stashで復元を取り消せる(store, work):
    a, b = work / "a.py", work / "b.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="st_a")
    cp.record(str(a))
    _edit(a, "v0", "v1")
    cp.snapshot_watermark()                              # 控え: a=v1
    _edit(a, "v1", "v2")
    cp.record(str(b))
    b.write_text("new\n")                                # 控え以降に作った物
    assert cp.stash() and cp.has_stash
    rep = cp.restore_watermark()
    assert rep is not None and a.read_text() == "v1\n" and not b.exists()
    urep = cp.unstash()
    assert urep is not None and urep.ok, urep.text()
    assert a.read_text() == "v2\n" and b.read_text() == "new\n", "復元前の状態に戻っていない"
    assert not cp.has_stash and cp.stash_bytes == 0
    assert cp.unstash() is None                          # 使った控えは無い


def test_容量は控え三層の合算で見る(store, work):
    a = work / "a.py"
    a.write_bytes(b"x" * 400)
    cp = Checkpoint(store, task_id="st_b", limits=Limits(max_total_bytes=1000))
    cp.record(str(a))                                    # base 400
    assert cp.snapshot_watermark() == 1                  # wm 400 → 800
    assert cp.stats()["bytes"] == 800 and cp.stats()["watermark_bytes"] == 400
    assert cp.stash() is False, "合算 1200 > 1000 なのに控えた"
    b = work / "b.py"
    b.write_bytes(b"y" * 300)
    assert cp.record(str(b)) == "skipped:store_full", "合算を見ずに控えた（上限の 2 倍まで育つ穴）"
    cp.close()
    assert cp.stats()["bytes"] == 0


def test_木ごとの控えは実行時状態と隠しディレクトリを避ける(store, work):
    (work / "site").mkdir()
    (work / "site" / "index.php").write_text("<?php\n")
    (work / ".git").mkdir()
    (work / ".git" / "HEAD").write_text("ref\n")
    (work / ".blackwall-phases").mkdir()
    (work / ".blackwall-phases" / "state.json").write_text("{}")
    (work / "server.pid").write_text("123\n")
    (work / "server.log").write_text("log\n")
    (work / "data").mkdir()
    (work / "data" / "x.db").write_bytes(b"db")
    (work / "data" / "x.db-wal").write_bytes(b"wal")
    os.symlink(str(work / "site"), str(work / "link"))
    paths, complete = cp_mod.iter_tree_files(str(work), Limits())
    names = sorted(os.path.relpath(p, work) for p in paths)
    assert names == ["data/x.db", "site/index.php"], names
    assert complete
    paths, complete = cp_mod.iter_tree_files(str(work), Limits(max_tree_files=1))
    assert not complete


def test_木の上限を超えたら追跡済みだけに落ちる(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    for i in range(3):
        (work / f"f{i}.txt").write_text("x")
    cp = Checkpoint(store, task_id="st_c", limits=Limits(max_tree_files=2))
    cp.record(str(a))
    assert cp.snapshot_watermark(tree_root=str(work)) == 1
    assert cp.watermark_scope == "tracked"
    assert any(s.reason == "tree_too_big" for s in cp._wm_skipped)
    cp2 = Checkpoint(store / "2", task_id="st_c2")
    cp2.record(str(a))
    assert cp2.snapshot_watermark(tree_root=str(work)) == 4
    assert cp2.watermark_scope == "tree"


def test_tree_scope_allowedは爆発半径の大きい場所を断る(tmp_path, monkeypatch):
    home = os.path.realpath(os.path.expanduser("~"))
    assert cp_mod.tree_scope_allowed("/") == ""
    assert cp_mod.tree_scope_allowed(home) == ""
    assert cp_mod.tree_scope_allowed("/tmp") == "", "浅すぎる場所"
    ws = tmp_path / "ws"
    ws.mkdir()
    assert cp_mod.tree_scope_allowed(str(ws)) == os.path.realpath(str(ws))
    assert cp_mod.tree_scope_allowed(str(ws), store_root=str(ws / "cp")) == "", "控え置き場を含む"
    monkeypatch.setenv("LAPS_HOME", str(ws / ".bw"))
    assert cp_mod.tree_scope_allowed(str(ws)) == "", "LAPS_HOME を含む"
    assert cp_mod.tree_scope_allowed(str(ws / "none")) == ""


def test_木ごとの控えはBashの書き換えを戻し増えた物は消さない(store, work):
    a = work / "a.txt"
    a.write_text("good\n")
    g = work / "g"
    g.write_text("")                                     # Bash が作った物（追跡外）
    cp = Checkpoint(store, task_id="st_d")
    cp.record(str(a))
    assert cp.snapshot_watermark(tree_root=str(work)) == 2
    assert cp.watermark_scope == "tree"
    a.write_text("bad\n")                                # sed -i 相当
    g.unlink()                                           # rm 相当
    (work / "extra.txt").write_text("later\n")           # 増えた物
    rep = cp.restore_watermark()
    assert rep is not None
    assert a.read_text() == "good\n" and g.exists()
    assert (work / "extra.txt").exists() and [os.path.basename(p) for p in rep.extras] == ["extra.txt"]


def test_keep_createdは控え以降に作った物を消さない(store, work):
    a, b = work / "a.py", work / "b.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="st_e")
    cp.record(str(a))
    cp.snapshot_watermark()
    cp.record(str(b))
    b.write_text("new\n")
    _edit(a, "v0", "v1")
    rep = cp.restore_watermark(keep_created=True)
    assert rep is not None and a.read_text() == "v0\n"
    assert b.exists() and rep.extras == [str(b)] and rep.deleted == []


@pytest.mark.parametrize("cmd,expect", [
    ("ls -la data/", set()),
    ("ps aux | grep php", set()),
    ("curl -s http://127.0.0.1:18300/api/works", set()),
    ("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18300/api/works", set()),
    ("curl -s http://127.0.0.1:18300/api/works | python3 -m json.tool > /dev/null", set()),
    ("sqlite3 data/works.db '.tables'", {"write"}),
    ("sed -i 's/a/b/' site/api.php", {"write"}),
    ("cp data/genre.db.sim data/genre.db", {"write"}),
    ("rm -f data/demo_genre.db", {"write"}),
    ("php -S 0.0.0.0:18300 -t site/ > server.log 2>&1 &", {"write", "env"}),
    ("bash start.sh", {"write", "env"}),
    ("bash bin/refresh_search.sh", {"write"}),
    ("python3 bin/pipeline_health.py", {"write"}),
    ("pkill -f 'php -S 0.0.0.0:18300'", {"env"}),
    ("dpkg -i /srv/packages/sqlite3_3.45.1-1ubuntu2.7_amd64.deb", {"env"}),
    ("chmod +x start.sh stop.sh", {"write"}),
    ("php -v", set()),
    ("which ffmpeg", set()),
    ("test -s data/scale.json", set()),
    ("echo hi > out.txt", {"write"}),
])
def test_classify_bashは実記録の形を分ける(cmd, expect):
    assert set(cp_mod.classify_bash(cmd)) == expect, cmd


def test_控え以降のBashの効果を数える(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="st_f")
    cp.record(str(a))
    cp.note_untracked("Bash", "bash start.sh")
    cp.snapshot_watermark()
    cp.note_untracked("Bash", "ls -la")
    cp.note_untracked("Bash", "sed -i s/a/b/ x.php")
    cp.note_untracked("Task", "委譲")
    fx = cp.bash_effects_since_watermark()
    assert fx == {"write": 2, "env": 1, "read": 1}, fx     # Task は write+env 扱い（何をしたか分からない）
    assert cp.untracked_since_watermark() == ["Bash: ls -la", "Bash: sed -i s/a/b/ x.php", "Task: 委譲"]


# ---- 第4回c（2026-09-05・反証 must_fix 3/4）----------------------------------------

def test_WALのSQLiteは本体だけ戻さず戻していませんと言う(store, work):
    """反証の実測: WAL で 300 行→控え→210 行→restore→**210 行のまま「復元 0 / 元から同じ 1」**。
    -wal は控えの対象外（TREE_EXCLUDE_SUFFIXES）なので本体だけ戻すと中身は戻らない。
    嘘の「戻した」を出さず、`failed` に理由つきで出す。"""
    import sqlite3
    db = work / "x.db"
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("create table t(i)")
    con.executemany("insert into t values(?)", [(i,) for i in range(300)])
    con.commit()
    cp = Checkpoint(store, task_id="st_wal")
    cp.record(str(db))
    assert cp.snapshot_watermark() == 1
    con.execute("delete from t where i >= 210")
    con.commit()
    assert (work / "x.db-wal").stat().st_size > 32          # frame が WAL に残っている形
    rep = cp.restore_watermark()
    assert rep is not None and rep.restored == [] and rep.unchanged == []
    assert [p for p, _ in rep.failed] == [str(db)]
    assert "x.db-wal" in rep.failed[0][1] and "戻していません" in rep.failed[0][1]
    assert not rep.ok and "戻せなかったファイル 1 件" in rep.text()
    assert con.execute("select count(*) from t").fetchone()[0] == 210
    con.close()


def test_ジャーナルの無いSQLiteは戻る(store, work):
    """対照: 既定（DELETE）モードで閉じた DB には副ファイルが無い＝普通に戻る。"""
    import sqlite3
    db = work / "y.db"
    con = sqlite3.connect(str(db))
    con.execute("create table t(i)")
    con.executemany("insert into t values(?)", [(i,) for i in range(300)])
    con.commit()
    con.close()
    cp = Checkpoint(store, task_id="st_del")
    cp.record(str(db))
    assert cp.snapshot_watermark() == 1
    con = sqlite3.connect(str(db))
    con.execute("delete from t where i >= 210")
    con.commit()
    con.close()
    rep = cp.restore_watermark()
    assert rep is not None and rep.restored == [str(db)] and rep.failed == []
    con = sqlite3.connect(str(db))
    assert con.execute("select count(*) from t").fetchone()[0] == 300
    con.close()


def test_冷えたjournalは戻すのを妨げない(store, work):
    """PERSIST モードの journal は先頭が 0 埋め＝ホットではない。これで断ると誤った拒否になる。"""
    db = work / "z.db"
    db.write_bytes(b"v0")
    (work / "z.db-journal").write_bytes(b"\x00" * 64)
    cp = Checkpoint(store, task_id="st_cold")
    cp.record(str(db))
    assert cp.snapshot_watermark() == 1
    db.write_bytes(b"v1")
    rep = cp.restore_watermark()
    assert rep is not None and rep.restored == [str(db)] and db.read_bytes() == b"v0"
    # ホットな journal（魔法数つき）なら断る
    (work / "z.db-journal").write_bytes(cp_mod._SQLITE_HOT_JOURNAL_MAGIC + b"\x00" * 56)
    db.write_bytes(b"v2")
    rep = cp.restore_watermark()
    assert rep is not None and rep.restored == [] and "z.db-journal" in rep.failed[0][1]
    assert db.read_bytes() == b"v2"


def test_store_fullの表示は実際の判定基準で出す(store, work):
    """反証: `_take_snapshot` の store_full 文言が `stored_bytes + total` で、置き換える層の旧値を
    二重に数えていた（判定は正しく表示だけ違う）。"""
    a = work / "a.py"
    a.write_bytes(b"x" * 400)
    cp = Checkpoint(store, task_id="st_msg", limits=Limits(max_total_bytes=1000))
    cp.record(str(a))                                    # base 400
    assert cp.snapshot_watermark() == 1                  # wm 400（stored 800）
    a.write_bytes(b"x" * 700)
    assert cp.snapshot_watermark() == 0                  # 予算 1000-(800-400)=600 < 700 → 控えない
    sk = [s for s in cp._wm_skipped if s.reason == "store_full"]
    assert sk and sk[0].detail.startswith("控え合計 400+700 > 上限 1000"), sk[0].detail


# ---- 9. 第4回c-2（2026-09-05・反証 must_fix 1）: 印（mark）＝副作用のある操作の前に置き、後で戻す ----
#
# ★固定したいこと:
#   m. mark→（変更・追加・削除）→revert_to_mark で、変えた物は戻り・消えた物は作り直され・
#      **増えた物は消える**（木ごとの印）。印は使ったら捨てる。
#   n. 追跡済みだけの印は増えた物を知らない＝消さない、と `skipped` に出す（黙って「戻した」と言わない）。
#   o. 印の容量は三層＋印の合算で見る・close() で消える。

def test_印へ戻すと変えた物は戻り消えた物は作り直され増えた物は消える(store, work):
    a, keep, new = work / "a.py", work / "keep.txt", work / "new.txt"
    a.write_text("v0\n")
    keep.write_text("k\n")
    cp = Checkpoint(store, task_id="mk_a")
    cp.record(str(a))
    assert cp.snapshot_watermark(tree_root=str(work)) >= 2 and cp.watermark_scope == "tree"
    assert cp.mark() and cp.has_mark and cp.mark_scope == "tree"
    a.write_text("v1\n")                                 # 変えた
    new.write_text("n\n")                                # 増えた（Bash 由来を模す・record しない）
    keep.unlink()                                        # 消えた
    rep = cp.revert_to_mark()
    assert rep is not None and rep.ok, rep.text()
    assert a.read_text() == "v0\n" and keep.read_text() == "k\n" and not new.exists()
    assert rep.deleted == [str(new)] and str(a) in rep.restored and str(keep) in rep.restored
    assert rep.label == "検収を走らせる直前"
    assert not cp.has_mark and cp.mark_bytes == 0 and cp.mark_scope == ""
    assert cp.revert_to_mark() is None                   # 使った印は無い


def test_追跡済みだけの印は増えた物を消さないと言う(store, work):
    a, new = work / "a.py", work / "new.txt"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="mk_b")
    cp.record(str(a))
    assert cp.snapshot_watermark() == 1 and cp.watermark_scope == "tracked"
    assert cp.mark() and cp.mark_scope == "tracked"
    a.write_text("v1\n")
    new.write_text("n\n")
    rep = cp.revert_to_mark()
    assert rep is not None and a.read_text() == "v0\n"
    assert new.exists(), "追跡済みだけの印で、知らない物を消した"
    # ★第4回c-5（再反証 must_fix 5）: 「木ごとの印ではない」は `not_tree`。旧形は `tree_too_big`
    #   （木が上限を超えた）を再利用しており、読む側が原因を取り違えられた。
    assert any(s.reason == "not_tree" and "増えた物は消していない" in s.detail for s in rep.skipped), \
        [s.text() for s in rep.skipped]
    assert not any(s.reason == "tree_too_big" for s in rep.skipped), [s.text() for s in rep.skipped]


def test_印は容量の合算に入りcloseで消える(store, work):
    a = work / "a.py"
    a.write_bytes(b"x" * 400)
    cp = Checkpoint(store, task_id="mk_c", limits=Limits(max_total_bytes=1000))
    cp.record(str(a))                                    # base 400
    assert cp.snapshot_watermark() == 1                  # wm 400 → 800
    assert cp.mark() is False and not cp.has_mark, "合算 1200 > 1000 なのに印を置いた"
    cp.close()
    cp2 = Checkpoint(store, task_id="mk_d", limits=Limits(max_total_bytes=2000))
    cp2.record(str(a))
    assert cp2.snapshot_watermark() == 1
    assert cp2.mark() and cp2.stats()["mark_bytes"] == 400 and cp2.stats()["bytes"] == 1200
    cp2.close()
    assert cp2.stats()["bytes"] == 0 and cp2.stats()["mark_bytes"] == 0 and not cp2.has_mark


# ---- 第4回c-3（反証 must_fix 1）: 除外規則の場所に残った物は戻せないが**数えて言う** ------------
#
# ★実物の検収（bench/acceptance/vdir-phases.txt:107,116,117）は `T="${TMPDIR:-$PWD}/.acc-tmp"` に
#   acc_*.json/log/txt を書く。`.acc-tmp` はドットのディレクトリ＝`iter_tree_files` が降りない＝
#   印へ戻す prune が見ない＝「ファイルに残した効果は戻した」が偽になる盲点そのもの。

def test_木の列挙と除外の列挙は補集合で全ファイルを尽くす(work):
    """規則が片側だけ変わると盲点が復活するので、2 つの列挙の和が全ファイルに等しいことを固定する。"""
    (work / "site").mkdir()
    (work / "site" / "index.php").write_text("<?php\n")
    (work / "site" / "acc.lock").write_text("l\n")               # 除外サフィックス
    (work / ".acc-tmp").mkdir()
    (work / ".acc-tmp" / "acc_a.json").write_text("{}")          # ドットのディレクトリ
    (work / ".git" / "objects" / "ab").mkdir(parents=True)
    (work / ".git" / "objects" / "ab" / "cdef").write_bytes(b"o")  # ドットの中の深い所
    (work / "node_modules" / "x").mkdir(parents=True)
    (work / "node_modules" / "x" / "index.js").write_text("js")  # TREE_EXCLUDE_DIRS の中
    (work / "data").mkdir()
    (work / "data" / "x.db").write_bytes(b"db")
    (work / "data" / "x.db-wal").write_bytes(b"wal")
    (work / ".blackwall-phases").mkdir()
    (work / ".blackwall-phases" / "ledger.json").write_text("{}")  # ハーネス自身の台帳＝両方から外れる
    # ★第4回c-4（再反証 M3）: **深い所**の同名ディレクトリは台帳ではない＝除外の列挙に入る。
    #   旧形は名前一致で深さ無制限に外し、ここが「木にも除外にも入らない」盲点だった。
    (work / "site" / ".blackwall-phases").mkdir()
    (work / "site" / ".blackwall-phases" / "acc_sentinel").write_text("x")
    os.symlink(str(work / "site"), str(work / "link"))            # リンク＝辿らず 1 件
    tree, ok1 = cp_mod.iter_tree_files(str(work), Limits())
    excl, ok2 = cp_mod.iter_excluded_files(str(work), Limits())
    assert ok1 and ok2
    every = set()
    for dp, dns, fns in os.walk(work, followlinks=False):
        for d in list(dns):
            if os.path.islink(os.path.join(dp, d)):
                every.add(os.path.join(dp, d))
        for fn in fns:
            every.add(os.path.join(dp, fn))
    # 不変条件から外すのは**作業場直下**の台帳だけ（深さ問わず外すと不変条件を検査したことにならない）。
    ledger_dir = str(work / ".blackwall-phases") + os.sep
    every = {p for p in every if not p.startswith(ledger_dir)}
    assert set(tree).isdisjoint(excl)
    assert set(tree) | set(excl) == every, sorted(every - set(tree) - set(excl))
    rel = sorted(os.path.relpath(p, work) for p in excl)
    assert rel == [".acc-tmp/acc_a.json", ".git/objects/ab/cdef", "data/x.db-wal", "link",
                   "node_modules/x/index.js", "site/.blackwall-phases/acc_sentinel", "site/acc.lock"], rel
    _, ok3 = cp_mod.iter_excluded_files(str(work), Limits(max_excluded_files=2))
    assert not ok3, "上限を超えたのに complete=True（黙って 0 件になる形）"


@pytest.mark.parametrize("where", [".acc-tmp/acc_sentinel", "site/acc.lock"])
def test_印へ戻すとき除外規則の場所に増えた物は消えないが数えて言う(store, work, where):
    (work / "site").mkdir()
    a = work / "site" / "a.txt"
    a.write_text("v0\n")
    (work / ".git").mkdir()
    (work / ".git" / "HEAD").write_text("ref\n")
    cp = Checkpoint(store, task_id="mk_x")
    cp.record(str(a))
    assert cp.snapshot_watermark(tree_root=str(work)) >= 1 and cp.watermark_scope == "tree"
    assert cp.mark() and cp.mark_scope == "tree"
    sentinel = work / where
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("mark\n")                                   # 検収#2 が残す印（除外規則の場所）
    (work / ".git" / "HEAD").write_text("ref2\n")                   # 除外規則の場所で**変わった**物
    a.write_text("v1\n")                                            # 木の中で変わった物（戻る）
    rep = cp.revert_to_mark()
    assert rep is not None and rep.ok and a.read_text() == "v0\n"
    assert sentinel.exists(), "除外規則の場所の物を消した（数えて言う、が約束）"
    assert rep.unknown_counted is True
    assert rep.unknown == [str(sentinel), str(work / ".git" / "HEAD")], rep.unknown
    assert "除外規則の場所" in rep.text() and where.rsplit("/", 1)[-1] in rep.text()
    assert not cp.has_mark and cp._mark_excluded is None


def test_除外規則の場所が上限を超えたら数えられなかったと言い0と言わない(store, work):
    a = work / "a.txt"
    a.write_text("v0\n")
    (work / ".big").mkdir()
    for i in range(5):
        (work / ".big" / f"f{i}").write_text("x")
    cp = Checkpoint(store, task_id="mk_y", limits=Limits(max_excluded_files=3))
    cp.record(str(a))
    assert cp.snapshot_watermark(tree_root=str(work)) >= 1
    assert cp.mark() and cp.mark_scope == "tree" and cp._mark_excluded_complete is False
    (work / ".big" / "new").write_text("n")
    rep = cp.revert_to_mark()
    assert rep is not None and rep.unknown == [] and rep.unknown_counted is False
    assert any(s.reason == "excluded_too_big" for s in rep.skipped), [s.text() for s in rep.skipped]
    assert "上限超過で数えられませんでした" in rep.text() and "木ごとの印でない" not in rep.text()


def test_追跡済みだけの印では除外規則の場所を数えられないと言う(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="mk_z")
    cp.record(str(a))
    assert cp.snapshot_watermark() == 1 and cp.mark() and cp.mark_scope == "tracked"
    rep = cp.revert_to_mark()
    assert rep is not None and rep.unknown_counted is False and rep.unknown == []
    assert "数えていません（木ごとの印でない）" in rep.text() and "上限超過で数えられませんでした" not in rep.text()


# ---- 第4回c-4（再反証 M2・M3）: 印の戻り値と状態の一致／台帳の skip は直下だけ ----------------

def test_木に普通のファイルが0件でも印は置け戻すと増えた物は消え印は捨てられる(store, work):
    """再反証 M2（p4_emptymark）: 旧形は `mark()` が True なのに `revert_to_mark()` が None＝
    2 回目の検収が木に増やした物が消えず・注記は「印が置けなかった」と偽り・drop されず
    `_mark_excluded` が残った。空の木でも「置いた」事実で一貫させる。"""
    (work / ".acc-tmp").mkdir()                                     # 除外領域だけがある木
    cp = Checkpoint(store, task_id="mk_empty")
    assert cp.mark(tree_root=str(work)) is True
    assert cp.has_mark and cp.mark_scope == "tree" and cp._mark == {}
    (work / "acc_sentinel").write_text("x")                         # 検収#2 が木に増やした物
    (work / ".acc-tmp" / "acc.json").write_text("y")                # 除外領域に増やした物
    rep = cp.revert_to_mark(prune_new=True)
    assert rep is not None, "mark() が True なのに revert_to_mark() が None（旧形）"
    assert not (work / "acc_sentinel").exists() and rep.deleted == [str(work / "acc_sentinel")]
    assert rep.unknown_counted is True and rep.unknown == [str(work / ".acc-tmp" / "acc.json")]
    assert not cp.has_mark and cp.mark_scope == "" and cp._mark_excluded is None
    assert cp.revert_to_mark() is None                              # 使った印は無い


def test_印が置けなかったら前の印ごと捨てて返り値と状態が一致する(store, work):
    a = work / "a.py"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="mk_drop", limits=Limits(max_tree_files=1))
    assert cp.mark(tree_root=str(work)) and cp.has_mark              # 1 件＝上限内
    (work / "b.py").write_text("b")
    assert cp.mark(tree_root=str(work)) is False                     # 2 > 1＝木の範囲が揃わない
    assert not cp.has_mark and cp.mark_scope == "" and cp._mark_excluded is None and cp.mark_bytes == 0
    assert cp.revert_to_mark() is None
    cp2 = Checkpoint(store, task_id="mk_drop2", limits=Limits(max_total_bytes=1))
    a.write_bytes(b"x" * 8)
    assert cp2.mark(tree_root=str(work)) is False and not cp2.has_mark   # store_full でも同じ
    assert cp2.revert_to_mark() is None


def test_印に入らなかった大きすぎる物を増えた物として消さない(store, work):
    """★第4回c-5（再反証 must_fix 1・致命）: `_take_snapshot` が `too_large` で落としたファイルは
    `_mark` に無い。旧形の prune は `known=set(self._mark)` だけを見て「印より後に増えた物」と
    見なし **os.unlink した**＝印より前から在った 9 MiB の mp4 が消えた（本番 run_phases で実測）。
    消してよいのは印より後に増えた物だけ。控えられなかった物は触らない・戻したとも言わない。"""
    big = work / "movie.mp4"
    big.write_bytes(b"\0" * 4096)
    (work / "a.py").write_text("v0\n")
    cp = Checkpoint(store, task_id="mk_big", limits=Limits(max_file_bytes=1024))
    assert cp.mark(tree_root=str(work)) and cp.mark_scope == "tree"
    assert cp.mark_incomplete == ("too_large",), cp.mark_incomplete
    (work / "new.txt").write_text("n")                              # 印より後に増えた物
    (work / "a.py").write_text("v1\n")
    rep = cp.revert_to_mark(prune_new=True)
    assert rep is not None
    assert big.exists() and big.stat().st_size == 4096, "印より前から在った大きなファイルを消した"
    assert not (work / "new.txt").exists(), "印より後に増えた物は消す（この機能自体は生きている）"
    assert (work / "a.py").read_text() == "v0\n"
    assert str(big) not in rep.deleted, rep.deleted
    assert any(s.reason == "mark_incomplete" and "印に入らなかった" in s.detail
               for s in rep.skipped), [x.text() for x in rep.skipped]
    assert not cp.has_mark


def test_印は置けても覆えていないことがある(store, work):
    """`mark()` の True は「置けた」であって「覆えた」ではない。両者を別に読む口がある。"""
    (work / "ok.py").write_text("x")
    cp = Checkpoint(store, task_id="mk_cov")
    assert cp.mark(tree_root=str(work)) and cp.mark_incomplete == ()      # 覆えた
    cp.drop_mark()
    (work / "big.bin").write_bytes(b"\0" * 4096)
    cp2 = Checkpoint(store, task_id="mk_cov2", limits=Limits(max_file_bytes=1024))
    assert cp2.mark(tree_root=str(work)) is True and cp2.has_mark          # 置けた
    assert cp2.mark_incomplete == ("too_large",)                          # が覆えていない
    rep = cp2.revert_to_mark(prune_new=True)
    assert (work / "big.bin").exists() and rep is not None


def test_印の後に木が上限を超えたら増えた物は消さずskippedに言う(store, work):
    """再反証 M1 の 3 つ目の原因（tree_grew）: 印は木ごとに置けたが、検収#2 が増やした物で木が上限を
    超えると prune は見送る＝増えた物は残る。`skipped` に tree_too_big で出る（phases はこれを見て
    「戻せていません」と言う）。"""
    a = work / "a.py"
    a.write_text("v0\n")
    (work / "b.py").write_text("b")
    cp = Checkpoint(store, task_id="mk_grew", limits=Limits(max_tree_files=2))
    assert cp.mark(tree_root=str(work)) and cp.mark_scope == "tree"
    a.write_text("v1\n")
    (work / "new1.py").write_text("n")                              # 3 > 2
    rep = cp.revert_to_mark(prune_new=True)
    assert rep is not None and a.read_text() == "v0\n"
    assert (work / "new1.py").exists(), "上限超過なのに増えた物を消した（部分的な prune）"
    assert any(s.reason == "tree_too_big" for s in rep.skipped), [x.text() for x in rep.skipped]
    assert rep.unknown_counted is True and rep.unknown == []
    assert not cp.has_mark


def test_深い所の台帳名のディレクトリは除外の列挙に入り印へ戻すとき数えられる(store, work):
    """再反証 M3（p12_skipname）: `site/.blackwall-phases/acc_sentinel` は台帳ではない。旧形は
    名前一致で深さ問わず外し「増えた/変わった物は 0 件」と積極的に偽れた。"""
    (work / "seed.txt").write_text("s")
    cp = Checkpoint(store, task_id="mk_skip")
    assert cp.mark(tree_root=str(work))
    (work / "site" / ".blackwall-phases").mkdir(parents=True)
    s = work / "site" / ".blackwall-phases" / "acc_sentinel"
    s.write_text("x")
    (work / ".blackwall-phases").mkdir()                             # 直下＝台帳＝数えない
    (work / ".blackwall-phases" / "ledger.json").write_text("{}")
    rep = cp.revert_to_mark()
    assert rep is not None and rep.unknown_counted is True
    assert rep.unknown == [str(s)], rep.unknown
    assert s.exists()                                                # 消さない・数えて言う
    tf, _ = cp_mod.iter_tree_files(str(work), Limits())
    ex, _ = cp_mod.iter_excluded_files(str(work), Limits())
    assert str(s) in ex and str(s) not in tf
    assert str(work / ".blackwall-phases" / "ledger.json") not in ex


# ---- ⑧ 第4回c-6（再々反証 must_fix 4）: 読めなかったのに「書けなかった」と言わない --------

def _unreadable() -> str:
    """**本物の読み取り失敗**を起こすファイル（模擬しない）。

    `/proc/self/mem` は `is_file()` が真・`st_size` が 0 で控えの上限も通るのに、
    `read_bytes()` が `OSError [Errno 5]` を返す＝控えの経路の「読み」だけが落ちる形。
    root でも起こせるので、権限を落とす仕掛け（setpriv・別ユーザ）が要らない。
    """
    p = "/proc/self/mem"
    if not Path(p).is_file():
        pytest.skip("この環境に /proc/self/mem が無い")
    return p


def test_読めなかった物を控えを書けなかったと言わない(store, work):
    """★第4回c-6（再々反証 must_fix 4）: `record` は `p.read_bytes()` と `write_bytes()` を
    **同じ try** に入れていたので、読めなかった物を `store_error`＝「控えを書けなかった」と
    報告した（実測 err-perm000b: 「sec.txt: 控えを書けなかった（[Errno 13] Permission denied）」）。
    読めなかったのか書けなかったのかで直し方が違う（前者は所有者・権限、後者は控えの置き場所）。"""
    src = _unreadable()
    cp = Checkpoint(store, task_id="rderr")
    assert cp.record(src) == "skipped:read_error", cp.skipped[-1].text() if cp.skipped else "(空)"
    sk = [s for s in cp.skipped if s.path == src]
    assert sk and sk[-1].reason == "read_error", [s.text() for s in cp.skipped]
    assert "読めなかった" in sk[-1].text() and "控えを書けなかった" not in sk[-1].text()
    # 控えの置き場所は無事＝「書けなかった」ではないことの裏取り。
    ok = work / "ok.txt"
    ok.write_text("v0\n")
    assert cp.record(str(ok)) == "recorded"


def test_控えの側も読めなかったと書けなかったを分ける(store, work):
    """同じ穴が `_take_snapshot`（watermark）にもあった。追跡済みの物が控えの時点で
    読めなくなっていると、旧形は `store_error` と言った。"""
    src = _unreadable()
    a = work / "a.txt"
    a.write_text("v0\n")
    cp = Checkpoint(store, task_id="rderr2")
    assert cp.record(str(a)) == "recorded"
    a.unlink()
    a.symlink_to(src)                       # 追跡済みの物が「読めない物」に化けた
    assert cp.snapshot_watermark() >= 0
    sk = [s for s in cp._wm_skipped if Path(s.path).name == "a.txt"]
    assert sk and sk[0].reason == "read_error", [s.text() for s in cp._wm_skipped]
    assert "読めなかった" in sk[0].text()


def test_木の符号も人の言葉で出る(store, work):
    """★参考（再々反証）: `REASON_TEXT` に `tree_too_big`/`tree_store_full` が無く、
    `Skipped.text()` が**生の符号**をそのまま人に見せていた。"""
    (work / "a.py").write_text("a")
    (work / "b.py").write_text("b")
    (work / "c.py").write_text("c")
    cp = Checkpoint(store, task_id="treewords", limits=Limits(max_tree_files=2))
    assert cp.snapshot_watermark(tree_root=str(work)) >= 0
    sk = [s for s in cp._wm_skipped if s.reason == "tree_too_big"]
    assert sk, [s.text() for s in cp._wm_skipped]
    assert "tree_too_big" not in sk[0].text() and "木のファイル数が上限" in sk[0].text()
    assert cp_mod.REASON_TEXT["tree_store_full"] and "tree_store_full" not in cp_mod.REASON_TEXT["tree_store_full"]


def test_控えられなかった物が消えたら変わったと言い見られないなら濁す(tmp_path):
    """★第4回c-8（再々反証 N1 deleted）: `Skipped.changed_now()` は `_lstat_sig` が None を
    返したら一律 None だった。`_lstat_sig` は**消えた**ときも見られないときも None を返すので、
    「もう無い」と分かる物まで「変わったかは確かめていません」と濁した。

    併せて `False` の意味を固定する: 比べているのは `(size, mtime_ns)` だけで、**中身の同一は
    言えない**（`os.utime` で更新時刻を戻して同じ大きさの別内容を書けば指紋は一致する）。
    """
    from laps.checkpoint import Skipped, _lstat_sig
    import os

    p = tmp_path / "big.bin"
    p.write_bytes(b"a" * 32)
    sk = Skipped(path=str(p), reason="too_large", detail="", stat=_lstat_sig(str(p)))
    assert sk.changed_now() is False                       # 触っていない

    st = os.stat(p)
    p.write_bytes(b"b" * 32)                               # 同じ大きさ・中身は違う
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))       # 更新時刻を戻す
    assert sk.changed_now() is False, "指紋が同じなら False（＝中身まで見た主張ではない）"
    assert p.read_bytes() == b"b" * 32                     # 中身は確かに違う

    p.unlink()
    assert sk.changed_now() is True, "消えたのは確実に変わった（旧形は None と言った）"

    # 見られない（親が普通のファイル＝ENOTDIR）は None のまま＝「消えた」と言わない。
    #   root でも必ず起きる形にしてある（chmod 000 は CAP_DAC_OVERRIDE で素通りする）。
    notdir = tmp_path / "plain"
    notdir.write_text("x")
    sk3 = Skipped(path=str(notdir / "inner.bin"), reason="too_large", detail="", stat=(8, 1))
    assert sk3.changed_now() is None


def test_辿れないディレクトリの中は消えたと言わない(tmp_path):
    """親ディレクトリが辿れない形でも None（＝「消えた」と言わない）。root は DAC を無視
    できてしまうので、この 1 本だけは環境によって skip する（本体は上の試験が ENOTDIR で見る）。"""
    from laps.checkpoint import Skipped, _lstat_sig
    import os

    sub = tmp_path / "sub"
    sub.mkdir()
    q = sub / "x.bin"
    q.write_bytes(b"z" * 8)
    sk2 = Skipped(path=str(q), reason="too_large", detail="", stat=_lstat_sig(str(q)))
    os.chmod(sub, 0)
    try:
        if os.access(str(q), os.F_OK):                     # root（DAC 無視）では作れない形
            pytest.skip("この環境は mode 000 のディレクトリを辿れる（CAP_DAC_OVERRIDE）")
        assert sk2.changed_now() is None
    finally:
        os.chmod(sub, 0o755)
