"""版を決める**唯一の**実装（2026-09-09）。

★なぜ切り出したか（実測で見つけた欠陥）:
  `laps/__init__.py` は `__version__ = "0.1.0"` を**直書き**していた。
  pyproject は 0.1.208。つまり `blackwall-legacy --version`（cli.py:163 の
  `click.version_option`）は **208 版ぶん古い値を名乗り続けていた**。
  一方 `laps/term/__init__.py` は 2026-08-29 に同じ問題を踏んで
  正しい実装（pyproject を読む→出荷物では dist-info）を持っていた。
  ＝**正しい実装と腐った実装が同じパッケージに並んでいた。**

  この現場は同じ族の事故を既に踏んでいる——`scripts/check_version_declarations.py`
  の docstring: 「`Cargo.toml` が 0.1.132 のまま 21 版ぶん止まり、0.1.133〜0.1.153 の
  走行が**1つの版に畳まれて集計された**＝『分母を間違える』が測定の土台で起きた」。
  走行記録に版が入らないと、変更の前後が同じ山に混ざって効果が読めない。

★実体は1本。`laps/__init__.py` も `laps/term/__init__.py` もここを呼ぶ。
  写しを2つ置くと、片方だけが古びて**気づけない**（このファイルが生まれた理由そのもの）。

★重い依存を引かない。`laps/__init__.py` は全経路で import されるので、
  ここで prompt_toolkit や httpx を触ってはいけない。
"""
from __future__ import annotations

import pathlib as _pathlib

__all__ = ["source_version", "resolve"]


def source_version() -> str | None:
    """開発ツリーで走っているなら pyproject の版を返す。

    ★2026-08-29 に踏んだ（term/__init__.py から移設した本文）:
      `importlib.metadata` は**インストール時の dist-info** を読むので、
      pyproject を上げても再インストールするまで古い版を返し続ける。
      実際に **ソースが 0.1.135 なのに画面と `--version` は 0.1.106** と表示していた。
      出荷物（.deb 同梱の wheel）では両者が必ず一致するので実害は出ていないが、
      **手元で `/update` を試すと「いま入っている版」を取り違える**＝
      この現場が更新経路で2度壊した「版の比較」と同じ族。**コードのある場所を正とする。**
    """
    here = _pathlib.Path(__file__).resolve()
    for parent in here.parents:
        pp = parent / "pyproject.toml"
        if not pp.is_file():
            continue
        # このパッケージ自身の pyproject か（他所の物を拾わない）
        if not (parent / "laps" / "__init__.py").is_file():
            return None
        try:
            try:
                import tomllib as _toml
            except ModuleNotFoundError:      # 3.10
                import tomli as _toml        # type: ignore[no-redef]
            with pp.open("rb") as fh:
                v = _toml.load(fh).get("project", {}).get("version")
            return str(v) if v else None
        except Exception:
            return None
    return None


def resolve() -> str:
    """開発ツリーなら pyproject、出荷物なら dist-info。どちらも駄目なら目印を返す。

    ★`"0.0.0+dev"` を返すのは黙って `"0.1.0"` を名乗るより良い。
      **知らないことを知っている値**なので、記録を読む人が気づける。
    """
    v = source_version()
    if v:
        return v
    try:  # 出荷物ではこちら（dist-info が正しい）
        from importlib.metadata import version as _v
        return str(_v("blackwall"))
    except Exception:
        return "0.0.0+dev"
