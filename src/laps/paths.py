"""置き場の名前（`blackwall` → `laps`・2026-11-14）。

正式名称を L.A.P.S に変えたので、新しく作る置き場は `laps` の名前にする。
ただし **既にある旧い名前の置き場は、そのまま使い続ける**（移さない・写さない・消さない）。
利用者の会話・ログイン・記憶・作業ログが見えなくなる事故を起こさないため。

規則（Rust の `app/src-tauri/src/paths.rs` とまったく同じにする）:

    1. new が在れば new
    2. new が無く old が在れば old（旧い置き場を使い続ける。new は作らない）
    3. どちらも無ければ new（ここで初めて作る）

- 選ぶ単位は「置き場のいちばん上のフォルダ」（`~/.laps` と `~/.blackwall`・`~/.config/laps` と
  `~/.config/blackwall`・作業場の `.laps/` と `.blackwall/`）。中の 1 ファイルごとに選ばない。
- **作る所も必ずここを通す**。規則を通さずに new を作ると、ほかの部品（Rust のアプリを含む）が
  「new が在る」と読んで、旧い置き場のファイルを見失う。
- 環境変数で場所を明示している場合（`LAPS_HOME` など）は、その値がそのまま勝つ（規則は既定の場所にだけ当てる）。
- 結果を覚えない（毎回見る）。走行の途中で片方が作ったフォルダを、もう片方がすぐ見られるように。

★ここは標準ライブラリしか読まない（`laps` のどの部品からも import できるように）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Union

NEW = "laps"
OLD = "blackwall"

#: 作業場の中の置き場の名前（新しい名前, 旧い名前）。木を歩く部品が読み飛ばす名前の一覧にも使う。
WS_NEW = "." + NEW
WS_OLD = "." + OLD
WS_NAMES = (WS_NEW, WS_OLD)

_P = Union[str, "os.PathLike[str]"]


def pick(new: _P, old: _P) -> Path:
    """規則どおりに new か old を返す。何も作らない。"""
    n, o = Path(new), Path(old)
    try:
        if n.exists():
            return n
        if o.exists():
            return o
    except OSError:
        pass
    return n


def _home() -> Path:
    return Path.home()


def dot_dir() -> Path:
    """利用者の置き場の既定（`~/.laps`・旧い `~/.blackwall`）。環境変数は見ない。

    `LAPS_HOME` に従わない部品（道具の置き場・声・画面操作の補助など、それぞれ自分の環境変数を持つ物）の既定に使う。
    """
    h = _home()
    return pick(h / WS_NEW, h / WS_OLD)


def home_dir() -> Path:
    """会話・記憶・監査の記録の置き場。`LAPS_HOME` があればその値、無ければ `dot_dir()`。作らない。"""
    v = os.environ.get("LAPS_HOME")
    return Path(v) if v else dot_dir()


def config_base(xdg: bool = True) -> Path:
    """設定の親フォルダ（`~/.config`）。xdg=True なら `XDG_CONFIG_HOME`（絶対の道だけ）を先に見る。"""
    if xdg:
        base = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
        if base and os.path.isabs(base):
            return Path(base)
    return _home() / ".config"


def config_dir(xdg: bool = True) -> Path:
    """利用者の設定の置き場（`~/.config/laps`・旧い `~/.config/blackwall`）。作らない。

    xdg=False は認証情報（auth.json）用: Rust のアプリ（auth.rs）が `XDG_CONFIG_HOME` を見ないので、同じ場所になるように。
    """
    b = config_base(xdg)
    return pick(b / NEW, b / OLD)


def workspace_dir(ws: _P) -> Path:
    """作業場の中の置き場（`<作業場>/.laps`・旧い `<作業場>/.blackwall`）。作らない。"""
    w = Path(ws)
    return pick(w / WS_NEW, w / WS_OLD)


def workspace_name(ws: _P) -> str:
    """作業場の中の置き場の名前（`.laps` か `.blackwall`）。モデルや利用者への文に実際の道を書くために使う。"""
    return workspace_dir(ws).name


def gateway_home() -> Path:
    """スマホ用の入口（gateway.py）の記録の既定の置き場。"""
    return pick("/root/.laps-mobile", "/root/.blackwall-mobile")


def ws_text(text: str, ws: "_P | None" = None) -> str:
    """文の中の作業場の置き場の名前（`.laps/` と書いてある）を、実際に使っている名前に合わせる。

    前置きや道具の説明は `.laps/FINDINGS.md` と書いておき、渡す直前にここを通す。旧い置き場（`.blackwall/`）を
    使い続けている作業場では `.blackwall/FINDINGS.md` になる。会話の間は置き場の名前が変わらないので、前置きも変わらない。
    """
    try:
        name = workspace_name(ws if ws is not None else os.getcwd())
    except OSError:
        return text
    return text if name == WS_NEW else text.replace(WS_NEW + "/", name + "/")
