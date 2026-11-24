"""空白入りパスの引用漏れを言い当てる（2026-09-21・実機の記録から）。

実機で出た 2 例をそのまま入力にする。★どちらの標準エラーにも「引用」の語は無い。
"""
import laps.tools.bash as B


def _win(monkeypatch, exists):
    monkeypatch.setattr(B.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(B.os.path, "exists", lambda p: p in exists)


REAL = {r"D:\Blackwall\Deep Rock Galactic"}


def test_powershell_の位置指定パラメーター(monkeypatch):
    _win(monkeypatch, REAL)
    out = "Get-ChildItem : 引数 'Galactic' を受け入れる位置指定パラメーターが見つかりません。"
    h = B._win_quote_hint(r"Get-ChildItem -Name D:\Blackwall\Deep Rock Galactic", out, 1)
    assert "引用されていません" in h
    assert r'"D:\Blackwall\Deep Rock Galactic"' in h


def test_cmd_の見つかりません(monkeypatch):
    _win(monkeypatch, REAL)
    h = B._win_quote_hint(r"dir /a /b D:\Blackwall\Deep Rock Galactic\*",
                          "指定されたファイルが見つかりません。", 1)
    assert r'"D:\Blackwall\Deep Rock Galactic\*"' in h


def test_実在しないなら黙る(monkeypatch):
    """★当てずっぽうを書かない。別の原因（本当にパス違い）を潰さないため。"""
    _win(monkeypatch, set())
    h = B._win_quote_hint(r"dir D:\No Such Place", "指定されたファイルが見つかりません。", 1)
    assert h == ""


def test_空白が無いなら黙る(monkeypatch):
    _win(monkeypatch, {r"D:\Blackwall"})
    assert B._win_quote_hint(r"dir D:\Blackwall", "指定されたファイルが見つかりません。", 1) == ""


def test_成功した実行には付けない(monkeypatch):
    _win(monkeypatch, REAL)
    assert B._win_quote_hint(r"dir D:\Blackwall\Deep Rock Galactic", "位置指定パラメーター", 0) == ""


def test_linux_では付けない(monkeypatch):
    monkeypatch.setattr(B.sys, "platform", "linux", raising=False)
    assert B._win_quote_hint(r"dir D:\A B", "位置指定パラメーター", 1) == ""


# ── 実行の前に直す（オーナー「似たような事象が出ないように」2026-09-21）──────

def test_実行前に引用を足す(monkeypatch):
    _win(monkeypatch, REAL)
    new, note = B._win_autoquote(r"Get-ChildItem -Name D:\Blackwall\Deep Rock Galactic")
    assert new == r'Get-ChildItem -Name "D:\Blackwall\Deep Rock Galactic"'
    assert "引用を足しました" in note


def test_後ろの引数を巻き込まない(monkeypatch):
    """★`-Recurse` をパスの一部にしない。実在する最長の並びで止める。"""
    _win(monkeypatch, REAL)
    new, _ = B._win_autoquote(r"Get-ChildItem D:\Blackwall\Deep Rock Galactic -Recurse")
    assert new == r'Get-ChildItem "D:\Blackwall\Deep Rock Galactic" -Recurse'


def test_まだ無いファイルも親が空白なら救う(monkeypatch):
    _win(monkeypatch, REAL)
    monkeypatch.setattr(B.os.path, "isdir", lambda p: p in REAL)
    new, _ = B._win_autoquote(r"type D:\Blackwall\Deep Rock Galactic\save.txt")
    assert new == r'type "D:\Blackwall\Deep Rock Galactic\save.txt"'


def test_既に引用してあるものは触らない(monkeypatch):
    _win(monkeypatch, REAL)
    cmd = r'Get-ChildItem "D:\Blackwall\Deep Rock Galactic"'
    assert B._win_autoquote(cmd) == (cmd, "")


def test_実在しないなら書き換えない(monkeypatch):
    """★推測で人の命令を書き換えない。"""
    _win(monkeypatch, set())
    monkeypatch.setattr(B.os.path, "isdir", lambda p: False)
    cmd = r"echo hello D:\No Such Thing"
    assert B._win_autoquote(cmd) == (cmd, "")


def test_空白が無ければ何もしない(monkeypatch):
    _win(monkeypatch, {r"D:\Blackwall"})
    cmd = r"dir D:\Blackwall"
    assert B._win_autoquote(cmd) == (cmd, "")


def test_linux_では書き換えない(monkeypatch):
    monkeypatch.setattr(B.sys, "platform", "linux", raising=False)
    cmd = r"ls D:\A B"
    assert B._win_autoquote(cmd) == (cmd, "")


def test_2箇所あれば両方(monkeypatch):
    two = {r"D:\Deep Rock", r"E:\My Games"}
    _win(monkeypatch, two)
    new, note = B._win_autoquote(r"copy D:\Deep Rock E:\My Games")
    assert new == r'copy "D:\Deep Rock" "E:\My Games"'
    assert note.count('"D:\\Deep Rock"') >= 1


def test_相対パスとUNCも対象(monkeypatch):
    here = {r".\Deep Rock", r"\\nas\share\My Docs"}
    _win(monkeypatch, here)
    new, _ = B._win_autoquote(r"dir .\Deep Rock")
    assert new == r'dir ".\Deep Rock"'
    new2, _ = B._win_autoquote(r"dir \\nas\share\My Docs")
    assert new2 == r'dir "\\nas\share\My Docs"'


def test_裸の相対パスは触らない(monkeypatch):
    """★普通の語と見分けがつかない。`echo Program Files` を壊さない。"""
    _win(monkeypatch, {"Program Files"})
    cmd = "echo Program Files"
    assert B._win_autoquote(cmd) == (cmd, "")
