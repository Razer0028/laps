"""設定（laps/term/config.py）の試験 — 設計 §5。

優先順 引数 > 環境変数 > ファイル > 既定、壊れた TOML で落ちない、
ensure_file が作る、XDG_CONFIG_HOME を尊重する。
"""
from __future__ import annotations

import os
import sys
import tomllib
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps.term import config as C  # noqa: E402
from laps import paths as _lp  # noqa: E402  置き場の名前は規則で選ぶ（~/.config/laps・旧い ~/.config/blackwall）


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """利用者の ~/.config を見ない・汚さない。env の LAPS_* も持ち込まない。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in C.ENV_KEYS:
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# 既定・場所
# --------------------------------------------------------------------------

def test_既定は_P40_で上限なし():
    assert C.DEFAULTS["backend"] == "http://127.0.0.1:8080/v1"
    assert C.DEFAULTS["model"] == "default"
    assert C.DEFAULTS["policy"] == "workspace"      # 公開版の既定
    # ★2026-08-29 オーナー指示で既定を変更（全コマンド許可・倫理制限の排除）
    assert C.DEFAULTS["ethics"] == "raw"
    assert C.DEFAULTS["tools"] == "full"
    assert C.DEFAULTS["max_iters"] == 0
    assert C.DEFAULTS["telemetry"] == ""
    # ★SPEC 項目 21（動き）: 波は既定で出す。calm は遅く小さく、off は出さない。
    assert C.DEFAULTS["wave"] == "on"
    assert C.WAVE_MODES == ("on", "calm", "off")
    assert C.CHOICES["wave"] == C.WAVE_MODES


def test_config_path_は_XDG_CONFIG_HOME_を尊重する(tmp_path, monkeypatch):
    assert C.config_path() == tmp_path / "xdg" / "laps" / "config.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert C.config_path() == _lp.config_dir(xdg=False) / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", "")   # 空は未設定と同じ（XDG の規則）
    assert C.config_path() == _lp.config_dir(xdg=False) / "config.toml"


def test_CONFIG_PATH_は_Path():
    assert isinstance(C.CONFIG_PATH, Path)
    assert C.CONFIG_PATH.name == "config.toml" and C.CONFIG_PATH.parent.name in ("laps", "blackwall")


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------

def test_無ければ_DEFAULTS_のコピー():
    cfg = C.load()
    assert cfg == C.DEFAULTS
    assert cfg is not C.DEFAULTS
    cfg["model"] = "x"
    assert C.DEFAULTS["model"] == "default", "既定が書き換わった"


def test_ファイルの値で上書きし_無い鍵は既定で埋める(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('model = "other"\nmax_iters = 12\n', encoding="utf-8")
    cfg = C.load(p)
    assert cfg["model"] == "other" and cfg["max_iters"] == 12
    assert cfg["backend"] == C.DEFAULTS["backend"]
    assert cfg["policy"] == C.DEFAULTS["policy"]


def test_壊れた_TOML_は警告つきで既定(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('model = "unterminated\nbackend = \n', encoding="utf-8")
    with pytest.warns(UserWarning, match="読めない"):
        cfg = C.load(p)
    assert cfg == C.DEFAULTS


def test_型の合わない鍵だけ捨てる(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('model = "keep"\nmax_iters = "abc"\n[nested]\nx = 1\n', encoding="utf-8")
    with pytest.warns(UserWarning):
        cfg = C.load(p)
    assert cfg["model"] == "keep"
    assert cfg["max_iters"] == 0
    assert "nested" not in cfg


def test_既定の場所から読む(tmp_path):
    p = C.config_path()
    p.parent.mkdir(parents=True)
    p.write_text('backend = "http://x/v1"\n', encoding="utf-8")
    assert C.load()["backend"] == "http://x/v1"


# --------------------------------------------------------------------------
# save / ensure_file
# --------------------------------------------------------------------------

def test_save_は_tomllib_で読み戻せる(tmp_path):
    p = tmp_path / "deep" / "c.toml"
    cfg = dict(C.DEFAULTS, model='na"me\\with "quotes" と日本語', max_iters=3, flag=True)
    C.save(cfg, p)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data == cfg
    assert C.load(p)["model"] == cfg["model"]


def test_save_は文字列_整数_真偽しか書かない(tmp_path):
    p = tmp_path / "c.toml"
    with pytest.raises(TypeError):
        C.save({"model": "m", "weird": [1, 2]}, p)
    assert not p.exists(), "型エラーでも半端なファイルが残った"
    with pytest.raises(TypeError):
        C.save({"x": 1.5}, p)


def test_save_は既定の場所に書く():
    C.save(dict(C.DEFAULTS, model="zz"))
    assert C.config_path().is_file()
    assert C.load()["model"] == "zz"


def test_ensure_file_が無ければ作り_あれば触らない(tmp_path):
    p = C.ensure_file()
    assert p == C.config_path() and p.is_file()
    text = p.read_text(encoding="utf-8")
    assert tomllib.loads(text) == C.DEFAULTS
    for k in C.DEFAULTS:
        assert f"# {C.KEY_HELP[k]}" in text, f"{k} の説明コメントが無い"
    assert "127.0.0.1" in text
    p.write_text('model = "mine"\n', encoding="utf-8")
    assert C.ensure_file() == p
    assert p.read_text(encoding="utf-8") == 'model = "mine"\n'


def test_ensure_file_は_XDG_を尊重する(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other"))
    p = C.ensure_file()
    assert p == tmp_path / "other" / "laps" / "config.toml"
    assert p.is_file()


# --------------------------------------------------------------------------
# resolve — 優先順
# --------------------------------------------------------------------------

def test_resolve_既定だけ():
    assert C.resolve(env={}) == C.DEFAULTS


def test_resolve_の優先順は_引数_環境_ファイル_既定(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('backend = "http://file/v1"\nmodel = "file-model"\n'
                 'policy = "readonly"\nmax_iters = 5\n', encoding="utf-8")
    env = {"LAPS_BACKEND": "http://env/v1", "LAPS_MODEL": "env-model",
           "LAPS_MAX_ITERS": "9"}
    args = SimpleNamespace(backend="http://arg/v1", model=None, policy=None,
                           ethics=None, tools=None, max_iters=None, telemetry=None)
    cfg = C.resolve(args, env=env, path=p)
    assert cfg["backend"] == "http://arg/v1"        # 引数 > 環境
    assert cfg["model"] == "env-model"              # 環境 > ファイル
    assert cfg["max_iters"] == 9                    # 環境の文字列は整数になる
    assert cfg["policy"] == "readonly"              # ファイル > 既定
    assert cfg["ethics"] == "raw"                   # 既定（2026-08-29 に変更）
    assert cfg["telemetry"] == C.DEFAULTS["telemetry"]


def test_resolve_は_dict_の引数も受ける(tmp_path):
    cfg = C.resolve({"model": "d", "max_iters": "3"}, env={}, path=tmp_path / "none.toml")
    assert cfg["model"] == "d" and cfg["max_iters"] == 3


def test_resolve_の環境変数名は設計どおり():
    assert C.ENV_KEYS == {
        "LAPS_BACKEND": "backend", "LAPS_MODEL": "model",
        "LAPS_POLICY": "policy", "LAPS_ETHICS": "ethics",
        "LAPS_TOOLSET": "tools", "LAPS_MAX_ITERS": "max_iters",
        "LAPS_TELEMETRY": "telemetry", "LAPS_WAVE": "wave",
        # ★2026-09-01 追加。本文の読み幅（実機の端末が 200 桁で、設計が効く幅に
        #   文字が来ていなかった）。env と config の名前が食い違うと
        #   「設定したのに効かない」になるので、この表で固定する。
        "LAPS_PROSE_WIDTH": "prose_width",
    }
    cfg = C.resolve(env={"LAPS_TOOLSET": "focused", "LAPS_ETHICS": "direct",
                         "LAPS_POLICY": "trusted", "LAPS_TELEMETRY": "http://t/"},
                    path="/nonexistent/c.toml")
    assert cfg["tools"] == "focused" and cfg["ethics"] == "direct"
    assert cfg["policy"] == "trusted" and cfg["telemetry"] == "http://t/"


def test_resolve_は_os_environ_を既定で読む(monkeypatch):
    monkeypatch.setenv("LAPS_MODEL", "from-os-env")
    assert C.resolve()["model"] == "from-os-env"


def test_resolve_は空や壊れた環境変数を下の層に落とす(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('max_iters = 4\nmodel = "f"\n', encoding="utf-8")
    with pytest.warns(UserWarning, match="LAPS_MAX_ITERS"):
        cfg = C.resolve(env={"LAPS_MAX_ITERS": "lots", "LAPS_MODEL": "  "}, path=p)
    assert cfg["max_iters"] == 4
    assert cfg["model"] == "f"


def test_resolve_は壊れたファイルでも落ちない(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("= = =\n", encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = C.resolve({"model": "a"}, env={"LAPS_BACKEND": "http://e/v1"}, path=p)
    assert cfg["model"] == "a" and cfg["backend"] == "http://e/v1"
    assert cfg["policy"] == C.DEFAULTS["policy"]


def test_resolve_は引数の知らない属性を無視する():
    args = SimpleNamespace(model="m", verbose=True, prompt="hi")
    cfg = C.resolve(args, env={}, path="/nonexistent/c.toml")
    assert cfg["model"] == "m"
    assert "verbose" not in cfg and "prompt" not in cfg
    assert os.environ.get("LAPS_MODEL") is None


# --------------------------------------------------------------------------
# 検証で見つけた穴（回帰）
# --------------------------------------------------------------------------

def test_save_は_U007F_を_TOML_が読める形で書く(tmp_path):
    """json.dumps は U+007F を素通しにするが TOML の basic string では禁止。"""
    p = tmp_path / "c.toml"
    C.save({"model": "a\x7fb"}, p)
    assert tomllib.loads(p.read_text(encoding="utf-8"))["model"] == "a\x7fb"


def test_文字列の鍵に文字列以外が来たら捨てる(tmp_path):
    """`policy = true` を "True" にして通すと、設定したのに効かない。"""
    p = tmp_path / "c.toml"
    p.write_text('model = 5\npolicy = true\nbackend = 1979-05-27\nethics = "direct"\n',
                 encoding="utf-8")
    with pytest.warns(UserWarning) as rec:
        cfg = C.load(p)
    assert cfg["model"] == C.DEFAULTS["model"]
    assert cfg["policy"] == C.DEFAULTS["policy"]
    assert cfg["backend"] == C.DEFAULTS["backend"]
    assert cfg["ethics"] == "direct"
    assert len(rec) == 3


def test_load_したものは必ず_save_できる(tmp_path):
    """知らない鍵の float/日時を持ち回ると save(load()) が TypeError で落ちる。"""
    p = tmp_path / "c.toml"
    p.write_text('model = "m"\nweird = 1.5\nwhen = 1979-05-27T07:32:00Z\nkeep = "yes"\nn = 3\n',
                 encoding="utf-8")
    with pytest.warns(UserWarning):
        cfg = C.load(p)
    assert "weird" not in cfg and "when" not in cfg
    assert cfg["keep"] == "yes" and cfg["n"] == 3
    C.save(cfg, p)   # 落ちない
    assert C.load(p)["keep"] == "yes"


def test_resolve_は空文字の引数を未指定と扱う(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('model = "f"\n', encoding="utf-8")
    cfg = C.resolve({"model": "", "backend": "  "}, env={"LAPS_BACKEND": "http://e/v1"}, path=p)
    assert cfg["model"] == "f"
    assert cfg["backend"] == "http://e/v1"


def test_config_path_は相対の_XDG_を無視する(monkeypatch):
    """XDG の規則。相対だと cwd ごとに別の設定を読んでしまう。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel/dir")
    assert C.config_path() == _lp.config_dir(xdg=False) / "config.toml"


def test_版はソースのpyprojectと一致する():
    """★画面と `--version` が**いま走っているコードの版**を言うこと。

    2026-08-29 に、ソースが 0.1.135 なのに画面は 0.1.106 と出していた
    （`importlib.metadata` が古い dist-info を読むため）。出荷物では一致するので
    実害は出なかったが、**手元で `/update` を試すと「いま入っている版」を取り違える**。
    """
    import pathlib
    import tomllib

    from laps.term import __version__
    pp = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    want = tomllib.loads(pp.read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == want, (
        f"画面が言う版 {__version__} と pyproject の版 {want} が違います。"
        "`importlib.metadata` の dist-info が古い可能性があります")


# --------------------------------------------------------------------------
# 語彙の決まった鍵（wave）— SPEC 項目 21
# --------------------------------------------------------------------------

def test_語彙の外の値は捨てて既定で動く(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('wave = "kirakira"\nmodel = "m"\n', encoding="utf-8")
    with pytest.warns(UserWarning, match="wave"):
        cfg = C.load(p)
    assert cfg["wave"] == "on"        # 既定に戻る
    assert cfg["model"] == "m"        # 他の鍵は生きる（1 つの誤字で全部戻さない）


def test_語彙は大文字と空白を許す(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('wave = "  CALM "\n', encoding="utf-8")
    assert C.load(p)["wave"] == "calm"
    assert C.resolve(env={"LAPS_WAVE": "OFF"}, path=p)["wave"] == "off"


def test_wave_mode_は書かれていなければ_None(tmp_path):
    """★「書かれていない」と「既定と同じ値が書いてある」を区別する
    （呼び手＝wave.resolve_mode が --calm / NO_COLOR に落とすかを決めるため）。"""
    p = tmp_path / "c.toml"
    assert C.wave_mode(env={}, path=p) is None          # ファイルが無い
    p.write_text('model = "m"\n', encoding="utf-8")
    assert C.wave_mode(env={}, path=p) is None          # 鍵が無い
    p.write_text('wave = "on"\n', encoding="utf-8")
    assert C.wave_mode(env={}, path=p) == "on"          # 明示された "on" は None ではない
    p.write_text('wave = "off"\n', encoding="utf-8")
    assert C.wave_mode(env={}, path=p) == "off"
    assert C.wave_mode(env={"LAPS_WAVE": "calm"}, path=p) == "calm"   # 環境変数が勝つ
    with pytest.warns(UserWarning, match="LAPS_WAVE"):
        assert C.wave_mode(env={"LAPS_WAVE": "zzz"}, path=p) == "off"  # 語彙外は下の層へ


def test_wave_mode_は壊れたファイルでも落ちない(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("wave = = broken\n", encoding="utf-8")
    with pytest.warns(UserWarning):
        assert C.wave_mode(env={}, path=p) is None


def test__read_はファイルにある鍵だけ返す(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('model = "m"\n', encoding="utf-8")
    assert C._read(p) == {"model": "m"}
    assert C.load(p) == dict(C.DEFAULTS, model="m")
    assert C._read(tmp_path / "none.toml") == {}
