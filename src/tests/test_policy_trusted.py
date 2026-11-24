"""`trusted` モード — 利用者が自分の機械で「全部やらせる」と決めた状態。

★2026-08-18 オーナー指示:
「フルに自由度を与えたい。ユーザー環境を自由に操れるようにしてください」
「コマンドもちゃんと使えるようにして」「Windows コマンドや Linux コマンド」

それまでの既定（workspace）では、外部到達・昇格・パッケージ管理・別プログラム起動が
**全モードで禁止**だった。実機のオーナーは自分の Windows でエージェントに作業させたいので、
この禁止は「安全」ではなく「使えない」になっていた。

★正直に書いておく: これは境界を外す選択で、モデルが誤ればその機械が壊れる。
  止めない代わりに**すべての呼び出しを audit に残す**のが対価。
  readonly / workspace はそのまま残してあり、config.toml でいつでも戻せる。

★唯一残す枷は自己保護（権限定義・プロンプト・監査ログ・実行される置き場＝`_self_protected_path`）。
  エージェントが自分の枷と記録を書き換えられると、「後から辿れる」という対価まで失われる。
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import laps.policy as policy


def _reload(monkeypatch, mode: str):
    """★`importlib.reload` を**しない**（2026-08-21・6本の試験を巻き添えにした）。

    policy.py の `_mode()` は毎回 `os.environ` を読むので、リロードは元々不要。
    にもかかわらずリロードすると **`PolicyDenied` が別のクラスになる**。
    先に import 済みの道具（write / edit / apply_diff / notebook_edit）は
    古いクラスを握っているので `except PolicyDenied` が一致せず、
    **例外が道具の外へ抜ける**。

    症状は分かりにくい: その試験自体は通り、**あとから走る無関係な試験が
    6本落ちる**。単独で走らせると通るので、順序に依存する形になる。
    ここを直せば、道具側を触らずに根が消える。
    """
    monkeypatch.setenv("LAPS_POLICY", mode)
    return policy


@pytest.fixture
def trusted(monkeypatch):
    yield _reload(monkeypatch, "trusted")


@pytest.fixture
def workspace(monkeypatch):
    yield _reload(monkeypatch, "workspace")


#: 実機で実際に要る形。Windows と Linux の両方。
REAL_WORK = [
    "python app.py",
    "pip install requests",
    "npm run build",
    "git push origin main",
    "powershell -Command Get-ChildItem",
    "cmd /c dir",
    "docker ps",
    "curl -s https://example.com/api",
]


@pytest.mark.parametrize("cmd", REAL_WORK)
def test_実機で要るコマンドが通る(trusted, cmd: str) -> None:
    """★`check_command` は Verdict を返す（`authorize_command` は例外を投げる側）。
    ここでは判定だけを見る——試験が本当に走らせてしまわないように。"""
    v = trusted.check_command(cmd)
    assert v.allowed, f"trusted なのに止められた: {cmd} — {v.reason}"


@pytest.mark.parametrize("cmd", ["pip install requests", "curl -s https://example.com/api"])
def test_workspaceでは止まる_比較(workspace, cmd: str) -> None:
    """★既定を緩めたのではなく、**新しいモードを足した**ことの確認。"""
    assert not workspace.check_command(cmd).allowed


def test_書き込み先の封じ込めも外れる(trusted, tmp_path) -> None:
    """作業フォルダの外にも書ける（利用者の環境を操るとはそういうこと）。"""
    v = trusted.check_path(str(tmp_path / "outside.txt"), write=True)
    assert v.allowed, v.reason


@pytest.fixture
def selfpaths(monkeypatch, tmp_path):
    """このプロセスが**実際に使っている**自己保護の実体パス。

    ★2026-09-02: 試験の側も名前ではなく実体を指すようにした。以前は
      `tmp_path/"policy.py"` という**別物**を渡して緑になっており、
      固定できていたのは「名前が policy.py なら拒否」という実装だけだった。
      その名前一致は逆向きの誤りも生んでいた（利用者の `.vscode/settings.json`
      や利用者が書いた `permission.py` まで書けない・`cat …/policy.py` が
      readonly で拒否される）。
    """
    home = tmp_path / "home"; home.mkdir()
    prompts = tmp_path / "PROMPTS"; prompts.mkdir()
    (home / "audit.jsonl").write_text("x")
    (home / "settings.json").write_text("{}")
    (prompts / "guardrails.md").write_text("x")
    monkeypatch.setenv("LAPS_HOME", str(home))
    monkeypatch.setenv("LAPS_PROMPT_DIR", str(prompts))
    here = Path(policy.__file__).resolve().parent
    return {
        "policy": here / "policy.py",
        "permission": here / "permission.py",
        "settings": home / "settings.json",
        "audit": home / "audit.jsonl",
        "home": home,
        "prompt": prompts / "guardrails.md",
    }


def test_自己保護だけは残る(trusted, selfpaths) -> None:
    """★ここは trusted でも渡さない。エージェントが自分の枷と記録を書き換えられると、
    『止めない代わりに後から辿れる』という対価まで失われる。"""
    for key in ("policy", "permission", "audit", "settings", "prompt"):
        v = trusted.check_path(str(selfpaths[key]), write=True)
        assert not v.allowed, f"自己保護が trusted で外れている: {key}"


def test_同じ名前の別ファイルは利用者のもの(trusted, tmp_path) -> None:
    """★逆向きの誤り（2026-09-02 に測って分かった）。

    判定が名前の部分一致だったので、利用者の `.vscode/settings.json`、
    利用者自身が書いた `permission.py`、他のプロジェクトの `policy.py` まで
    「エージェントの権限定義」として書けなくなっていた。この機械の記録には
    現れない形（ここにそんな利用者が居ないので）＝**商用で現れる**。
    """
    for name in ("policy.py", "permission.py", ".vscode/settings.json"):
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        v = trusted.check_path(str(f), write=True)
        assert v.allowed, f"利用者のファイルを巻き添えにしている: {name}"


def test_知らないモード名は安全側へ落ちる(monkeypatch) -> None:
    # ★ここにも reload が残っていた（2026-08-21）。冒頭の3箇所だけ直して
    #   「これで全部」と思ったのが誤り。1つ残っただけで症状は同じ
    #   （PolicyDenied が別クラスになり、道具の except が一致しなくなる）。
    p = _reload(monkeypatch, "全部ゆるして")
    assert p._mode() == "readonly"


#: 自己保護に触る形。**すべて trusted/off で「書く」側**（読むのは止めない）。
#:
#: ★2026-09-02（第2版）: 最初の版は 5 形しか並べておらず、しかも
#:   `tmp_path/policy.py` という**別物**を指していた。14 形を実際に
#:   `check_command` へ通したところ **10 形が許可**された——判定が
#:   `_PATH_WRITERS`（11 個）の引数しか見ておらず、`sed -i` / `dd of=` /
#:   `tee` / `truncate` / `install` / `ln` は**書き手として存在しない**扱い、
#:   glob（`polic?.py`・`audit.js*`）は名前が一致せず、`rm -rf ~/.blackwall`
#:   は親を名指しするので誰も見ていなかった。
#:   ＝「自己保護は trusted でも残す」は、直した直後もまだ嘘だった。
def _self_write_shapes(sp) -> list[str]:
    pol, aud, home = sp["policy"], sp["audit"], sp["home"]
    return [
        f"echo x > {pol}",
        f"cp evil.py {pol}",
        f"rm -f {aud}",
        f"mv {sp['settings']} /tmp/x",
        f"touch {sp['prompt']}",
        f"sed -i s/a/b/ {pol}",                    # ← ここから 10 形が通っていた
        f"truncate -s 0 {aud}",
        f"dd if=/dev/null of={pol}",
        f"tee {pol}",
        f"install evil.py {pol}",
        f"cp evil.py {pol.parent}/polic?.py",      # glob で名前を隠す
        f"rm -f {home}/audit.js*",                 # glob
        f"rm -rf {home}",                          # 親ごと＝audit の約束も消える
        f"ln -sf /tmp/evil.py {pol}",
        f"python3 -c \"open('{pol}','w')\"",
    ]


@pytest.mark.parametrize("i", range(15))
@pytest.mark.parametrize("mode", ["trusted", "off"])
def test_自己保護は_Bash_経路でも残る(monkeypatch, tmp_path, selfpaths, i: int, mode: str) -> None:
    """★書いてあったのに、書いていなかった（2026-09-02）。

    このファイルの冒頭も policy.py の docstring も「唯一残す枷は自己保護」と
    言っているのに、それを確かめていたのは上の `test_自己保護だけは残る`＝
    **`check_path` だけ**だった。`check_command`（Bash 経路）は
    `if mode in ("off","trusted"): continue` で自己保護を含む全部を飛ばしており、

        echo x > policy.py / cp evil.py …/policy.py / rm ~/.blackwall/audit.jsonl

    は**出荷既定の trusted で通っていた**（CVE-2025-53773 と同じ形＝
    エージェントが自分の枷と記録を書き換える）。片側で直して片側に残した形。
    """
    p = _reload(monkeypatch, mode)
    cmd = _self_write_shapes(selfpaths)[i]
    v = p.check_command(cmd, cwd=str(tmp_path))
    assert not v.allowed, f"{mode} で自己保護が外れている: {cmd}"


@pytest.mark.parametrize("mode", ["trusted", "off", "workspace", "readonly"])
def test_インタプリタ経由は閉じない_ことを固定する(monkeypatch, tmp_path, selfpaths, mode) -> None:
    """★閉じない穴を**閉じたことにしない**ための試験。

    `python3 evil.py` は、書き換え先がゲートの見ないファイルの中に書いてある。
    許可リストも引数の罠も機微パスも、検査できるのはシェルの文字列だけなので、
    **文字列の検査では閉じない**（policy.py の docstring も最初からそう言っている）。
    本当の直し方は別 uid / bwrap / コンテナで、それはこのファイルの外。
    ここで固定するのは「trusted では通る」という事実そのもの——将来
    誰かが『自己保護は完全』と書こうとしたとき、この試験が止める。
    """
    p = _reload(monkeypatch, mode)
    v = p.check_command("python3 evil.py", cwd=str(tmp_path))
    if mode in ("trusted", "off", "workspace"):
        assert v.allowed, "ここが閉じたなら、報告と docstring を書き換えること"


@pytest.mark.parametrize("cmd", [
    "rm -rf build",
    "echo hi > out.txt",
    "cp a.py b.py",
    "cat settings.json",          # ★**読む**のは止めない（止めるのは書き換え）
    "sudo systemctl restart nginx",
])
def test_自己保護を足しても_trusted_は何も止めない(trusted, tmp_path, cmd: str) -> None:
    """★上の枷を足すときに一番怖いのは**巻き添え**。trusted の約束
    （何も止めない）が生きていることを同じ場所で固定する。"""
    v = trusted.check_command(cmd, cwd=str(tmp_path))
    assert v.allowed, f"trusted なのに止められた: {cmd} — {v.reason}"
