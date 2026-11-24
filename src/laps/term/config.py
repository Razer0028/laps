"""設定 — `~/.config/blackwall/config.toml`（設計 §5）。既定は同じ機械の OpenAI 互換の推論機。

優先順は **引数 > 環境変数 > ファイル > 既定**。ここは「どこから値が来たか」を
一箇所で決める場所で、画面側（app/slash）は resolve() の結果だけを見る。

★ tomllib は読み専用なので、書く側は平らな dict を自分で TOML にする
  （文字列・整数・真偽だけ。入れ子は扱わない＝設計の設定は全部平ら）。
★ 壊れたファイルで起動を止めない。警告を出して既定で動く（設定の誤字で
  端末が開かなくなると直す手段が無い）。
"""
from __future__ import annotations

from .. import paths as _lp
import json
import re
import os
import warnings
from pathlib import Path
from typing import Any, Mapping

try:  # 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # 3.10 は tomli があれば使う
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:  # どちらも無ければ読めない＝既定で動く
        _toml = None  # type: ignore[assignment]

__all__ = [
    "CONFIG_PATH", "DEFAULTS", "ENV_KEYS", "KEY_HELP", "CHOICES", "WAVE_MODES",
    "config_path", "load", "save", "update_file", "resolve", "ensure_file", "wave_mode",
]

# 既定の送り先は同じ機械の OpenAI 互換の推論機（llama-server など）。LAPS_BACKEND で変える。
DEFAULTS: dict[str, Any] = {
    "backend": "http://127.0.0.1:8080/v1",
    "model": "default",
    # 公開版の既定は workspace（作業フォルダーの中だけ書ける）。trusted = 何も止めない（allowlist も
    #   書き込み先の封じ込めも外れる）は /policy trusted か LAPS_POLICY=trusted で選ぶ。
    #   raw     = 慎重さの塊を載せない（正直さの固定と**現場の安全**だけ残る）。
    #   ★「現場の安全」（破壊操作の前に確認・バックアップ不明なら止まる）は倫理ではなく
    #     ラボを壊さないための取り決めなので raw でも残る。外したい場合は別途言うこと。
    "policy": "workspace",
    "ethics": "raw",
    "tools": "full",
    "max_iters": 0,
    "telemetry": "",   # 空＝送らない（/sendlog の送り先を自分で立てたときだけ書く）
    # ★2026-08-29 SPEC 項目 21（動き）。波は「待っていること」を示すためだけの動き。
    #   on = 45ms 刻み／calm = 120ms 刻み・振れ幅半分／off = 出さない。
    "wave": "on",
    # ★本文の読み幅（表示セル）。0 = 端末幅に従う。（2026-09-01）
    #   ★なぜ要るか: 設計仕様は幅 20/40/80/100 でしか検証しておらず、実機の端末は
    #     **200 桁**だった。本文が最長 282 セルの板で流れ、折り返し・段・畳みといった
    #     24 件の設計が**効く場所に文字が来ていなかった**。
    #   本文だけに効かせる（道具の出力・コード・差分・表は幅が要るので触らない）。
    "prose_width": 96,
    # ★画面（2026-10-12 オーナー「TUI作りこんでほしいです。Btopみたいに」）。tui = btop 風の全画面（対話のときだけ）／
    #   classic = 従来の行の形。-p・パイプ・TERM=dumb・--card/--record/--demo は常に行の形。
    "ui": "tui",
    # ★Claude API の中継（2026-10-19 オーナー「Windows や Linux 版のアプリから API のモデルを自由に切り替えられるように」）。
    #   /model・/backend claude・全画面の F5 で Claude のモデルを選ぶと、送り先がここになる（scripts/claude_relay.py・開発機）。
    "claude_backend": "",   # Claude API を中継する口（自分で立てたときだけ書く）
    # ★Claude へ移る前の推論機の送り先（戻るときに使う）。空なら既定の送り先。切り替えたときに端末版が書く。
    "server_backend": "",
}

# 値が決まっている鍵（語彙の外は捨てて警告する）。★語彙を増やすときはここと KEY_HELP を両方。
WAVE_MODES = ("on", "calm", "off")
UI_MODES = ("tui", "classic")
CHOICES: dict[str, tuple[str, ...]] = {"wave": WAVE_MODES, "ui": UI_MODES}

# 環境変数 → 鍵。名前は serve / 本体が読むものと同じにしてある
# （ENV と config の名前が食い違うと「設定したのに効かない」が起きる）。
ENV_KEYS: dict[str, str] = {
    "LAPS_BACKEND": "backend",
    "LAPS_MODEL": "model",
    "LAPS_POLICY": "policy",
    "LAPS_ETHICS": "ethics",
    "LAPS_TOOLSET": "tools",
    "LAPS_MAX_ITERS": "max_iters",
    "LAPS_TELEMETRY": "telemetry",
    "LAPS_WAVE": "wave",
    "LAPS_PROSE_WIDTH": "prose_width",
    "LAPS_UI": "ui",
}

# ensure_file が書くコメント（各鍵の意味）。
KEY_HELP: dict[str, str] = {
    "backend": "推論の送り先（OpenAI 互換 /v1）。既定は同じ機械の http://127.0.0.1:8080/v1",
    "model": "モデル名（llama-swap の名前）",
    "policy": "実行ゲート: readonly | workspace | trusted（trusted は /policy で上げる）",
    "ethics": "倫理の刻み: strict | standard | direct | raw",
    # ★focused は案内しない（2026-10-16 監査: 既定の oc 中核は focused で道具を絞らない＝受け付けて黙って無視していた。
    #   値としては受け付ける。旧中核は 2026-10-21 に外した）。
    "tools": "道具の束: full | chat（chat はウェブ検索だけの会話）",
    "max_iters": "1回の送信あたりの反復上限。0 = 上限なし（オーナー指示）",
    "telemetry": "/sendlog の送り先（記録の受け口）",
    "wave": "推論中の波: on | calm（遅く・小さく） | off（出さない）",
    "prose_width": "本文の読み幅（表示セル）。0 = 端末幅いっぱい。広い端末で本文が板になるのを防ぐ",
    "ui": "画面: tui（btop 風の全画面・対話のとき） | classic（従来の行の形）",
    "claude_backend": "Claude API の中継（自分で立てた物）。/model で Claude のモデルを選ぶと送り先がここになる",
    "server_backend": "Claude へ切り替える前の推論機の送り先（戻るときに使う・端末版が書く）。空なら既定の P40",
}


def config_path() -> Path:
    """いまの環境での設定ファイルの場所（XDG_CONFIG_HOME を尊重）。

    import 時に固めると、後から環境変数を変えても追随しない（試験で困る）ので
    毎回計算する。CONFIG_PATH はその import 時点の値＝表示用。
    """
    # XDG の規則: 空と相対パスは「未設定」（相対だと cwd ごとに別の設定を読んでしまう）→ paths.config_base
    # 置き場の名前は規則で選ぶ（~/.config/laps・旧い ~/.config/blackwall が在ればそのまま）
    return _lp.config_dir() / "config.toml"


CONFIG_PATH: Path = config_path()


def _path(path: str | os.PathLike | None) -> Path:
    return Path(path) if path is not None else config_path()


# --------------------------------------------------------------------------
# 値の型
# --------------------------------------------------------------------------

def _coerce(key: str, value: Any) -> Any:
    """既定と同じ型に揃える。揃わなければ ValueError（呼び手が落とすか警告する）。"""
    want = DEFAULTS.get(key)
    if isinstance(want, bool):
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key} は真偽値で: {value!r}")
    if isinstance(want, int):
        if isinstance(value, bool):
            raise ValueError(f"{key} は整数で: {value!r}")
        return int(str(value).strip()) if not isinstance(value, int) else value
    if isinstance(want, str):
        if isinstance(value, str) and key in CHOICES:
            v = value.strip().lower()
            if v not in CHOICES[key]:
                # ★語彙の外を黙って通すと「設定したのに効かない」になる（policy/ethics で
                #   実際に踏んだ形）。捨てて警告し、既定で動く。
                raise ValueError(f"{key} は {' | '.join(CHOICES[key])} のどれかで: {value!r}")
            return v
        if not isinstance(value, str):
            # TOML の `policy = true` や `backend = 1979-05-27` を文字列にして
            # 通すと「設定したのに効かない」になる。型が違う鍵は捨てて警告する。
            raise ValueError(f"{key} は文字列で: {value!r}")
        return value
    if not isinstance(value, (str, int, bool)):
        # 既定に無い鍵も、save が書ける型（文字列・整数・真偽）だけ持ち回る。
        # float や日時を持ち回ると save(load()) が TypeError で落ちる。
        raise ValueError(f"{key} は文字列・整数・真偽のどれかで: {value!r}")
    return value  # 既定に無い鍵はそのまま（消さない）


# --------------------------------------------------------------------------
# 読む
# --------------------------------------------------------------------------

def _read(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """**ファイルに書かれている鍵だけ**を返す（既定で埋めない）。

    ★`load` と分けてあるのは「書かれていない」と「既定と同じ値が書いてある」を
      区別したい呼び手（`wave_mode`）がいるため。埋めてしまうと区別できない。
    """
    p = _path(path)
    got: dict[str, Any] = {}
    if not p.is_file():
        return got
    if _toml is None:
        warnings.warn(f"TOML を読む部品が無い（tomllib/tomli）。既定で動く: {p}")
        return got
    try:
        raw = p.read_bytes()
        data = _toml.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:  # TOMLDecodeError は ValueError
        warnings.warn(f"設定ファイルが読めない（既定で動く）: {p}: {e}")
        return got
    if not isinstance(data, dict):
        warnings.warn(f"設定ファイルの形が違う（既定で動く）: {p}")
        return got
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            warnings.warn(f"設定 {k} は平らな値で（入れ子は無視）: {p}")
            continue
        try:
            got[k] = _coerce(k, v)
        except ValueError as e:
            warnings.warn(f"設定 {k} を無視した: {e}")
    return got


def load(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """ファイルを読む。無ければ DEFAULTS のコピー。壊れていれば警告つきで DEFAULTS。

    ファイルに無い鍵は既定で埋める（古い設定ファイルに新しい鍵が無くても動く）。
    型の合わない値は、その鍵だけ捨てて警告する（1 つの誤字で全部が既定に
    戻るより、他の設定は生かすほうが直しやすい）。
    """
    cfg: dict[str, Any] = dict(DEFAULTS)
    cfg.update(_read(path))
    return cfg


def wave_mode(env: Mapping[str, str] | None = None,
              path: str | os.PathLike | None = None) -> str | None:
    """波の出し方（SPEC 項目 21）。環境変数 `LAPS_WAVE` > 設定ファイル `wave`。

    **どちらにも書かれていなければ None**（＝呼び手が `--calm` / NO_COLOR で決める）。
    語彙の外（`wave = "kirakira"`）は書かれていない扱い＋警告。
    使う側 → `laps.term.wave.resolve_mode`（そこから `Wave.__init__`）。
    """
    e = os.environ if env is None else env
    v = str(e.get("LAPS_WAVE", "") or "").strip().lower()
    if v in WAVE_MODES:
        return v
    if v:
        warnings.warn(f"環境変数 LAPS_WAVE を無視した: {' | '.join(WAVE_MODES)} のどれかで: {v!r}")
    got = _read(path).get("wave")
    return got if got in WAVE_MODES else None


# --------------------------------------------------------------------------
# 書く
# --------------------------------------------------------------------------

def _toml_scalar(key: str, value: Any) -> str:
    if isinstance(value, bool):        # bool は int の subclass なので先に見る
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # TOML の basic string のエスケープは JSON と同じ形（\" \\ \n \t \uXXXX）。
        # ensure_ascii=False で日本語はそのまま書く。U+007F だけは JSON が素通し
        # にするが TOML では禁止（tomllib が Illegal character で落ちる）ので自分で。
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    raise TypeError(f"設定 {key} は文字列・整数・真偽のどれかで: {type(value).__name__}")


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _render(cfg: Mapping[str, Any], *, with_help: bool) -> str:
    lines = ["# L.A.P.S 端末クライアントの設定（設計 §5）。",
             "# 優先順: 引数 > 環境変数（LAPS_BACKEND 等） > このファイル > 既定。",
             ""]
    # 既定の鍵を先に、既定の順で。知らない鍵は後ろに（消さない）。
    order = [k for k in DEFAULTS if k in cfg] + [k for k in cfg if k not in DEFAULTS]
    for k in order:
        if with_help and k in KEY_HELP:
            lines.append(f"# {KEY_HELP[k]}")
        # ★鍵も TOML として有効な形で書く（2026-08-23 レビュー）: 裸の鍵に使えない文字を含む
        #   鍵をそのまま書くと壊れた TOML になり、次回起動で設定全体が黙って既定に戻る。
        key = k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)
        lines.append(f"{key} = {_toml_scalar(k, cfg[k])}")
    return "\n".join(lines) + "\n"


def _write_atomic(p: Path, text: str) -> None:
    """途中で落ちても半端なファイルを残さない（次回 load が壊れた TOML を見る）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def save(cfg: Mapping[str, Any], path: str | os.PathLike | None = None) -> None:
    """平らな dict を TOML で書く（文字列・整数・真偽だけ）。他の型は TypeError。"""
    body = _render(cfg, with_help=True)   # 先に全部整形してから書く（途中で型エラー→何も書かない）
    _write_atomic(_path(path), body)


def update_file(updates: Mapping[str, Any], path: str | os.PathLike | None = None) -> None:
    """ファイルに書かれている鍵はそのまま、`updates` の鍵だけ書き換える（2026-10-19・/model の送り先を残す）。

    ★`save(load())` にしない: 既定で埋めた値まで書くと、今の既定がファイルに焼き付く（_migrate の注記の問題）。"""
    p = _path(path)
    raw = _read(p)
    raw.update(updates)
    save(raw, p)


def ensure_file(path: str | os.PathLike | None = None) -> Path:
    """無ければ DEFAULTS（各鍵の意味をコメントで）で作る。あれば触らない。"""
    p = _path(path)
    if not p.exists():
        _write_atomic(p, _render(DEFAULTS, with_help=True))
    return p


# --------------------------------------------------------------------------
# 決める
# --------------------------------------------------------------------------

def _arg_get(args: Any, key: str) -> Any:
    if args is None:
        return None
    if isinstance(args, Mapping):
        return args.get(key)
    return getattr(args, key, None)



# ★古い既定が設定ファイルに焼き付いたまま残る問題（2026-08-29 に実機で発覚）。
#   0.1.134 で既定を policy=trusted / ethics=raw に変えたが、**一度でも起動した機械には
#   その時の既定が書き込まれた config.toml が残り、そちらが勝つ**。
#   ＝オーナーの「全コマンド使用を許可・倫理的制限をすべて排除」という指示が、
#   新規導入にしか届いていなかった（画面は `workspace/standard` と正しく表示していたので、
#   **画面は嘘をついておらず、そのぶん気づきにくい**）。
#
#   ★**利用者が自分で選んだ値は動かさない。** 動かすのは「昔の既定のまま一度も触っていない」
#     ときだけ（policy=workspace かつ ethics=standard の組）。`readonly` や `direct` を
#     選んでいる人は対象外。移行したことは**必ず画面に出す**（黙って権限を広げない）。
_OLD_DEFAULTS = {"policy": "workspace", "ethics": "standard"}
CONFIG_VERSION = 2
migrated: list[str] = []


def _migrate(cfg: dict[str, Any], path: Path) -> None:
    """昔の既定のまま残っている policy/ethics を、今の既定に合わせる（1 度だけ）。

    ★印（config_version）は**ファイルにだけ**置く。`DEFAULTS` に入れてはいけない:
      `load` が既定で埋めてしまい、印の無い**古いファイルが「移行済み」に見える**＝
      移行が二度と起きなくなる（実際に一度そう書いて試験が教えてくれた）。
    ★戻り値にも混ぜない。`resolve` の結果は「設定」であって、ファイルの版ではない。
    """
    migrated.clear()
    raw = _read(path)                       # 既定で埋めない、ファイルの生の中身
    if not raw:
        return                              # ファイルが無い＝新規。既定が既に新しい
    if int(raw.get("config_version") or 0) >= CONFIG_VERSION:
        return
    if all(raw.get(k) == v for k, v in _OLD_DEFAULTS.items()):
        for k in _OLD_DEFAULTS:
            cfg[k] = DEFAULTS[k]
            migrated.append(f"{k}: {_OLD_DEFAULTS[k]} → {DEFAULTS[k]}")
    try:
        save({**cfg, "config_version": CONFIG_VERSION}, path)
    except OSError:
        pass                                # 書けなくても動く（次回また試す）


def resolve(args: Any = None,
            env: Mapping[str, str] | None = None,
            path: str | os.PathLike | None = None) -> dict[str, Any]:
    """最終的な設定を返す。優先: args（None でない属性） > env > file > DEFAULTS。

    args は argparse の Namespace でも dict でもよい（鍵は DEFAULTS と同じ名前）。
    env は省略時 os.environ。値の型が合わない層は捨てて下の層を使う（警告）。
    """
    cfg = load(path)
    _migrate(cfg, Path(path) if path else config_path())
    e = os.environ if env is None else env
    for var, key in ENV_KEYS.items():
        v = e.get(var)
        if v is None or str(v).strip() == "":
            continue
        try:
            cfg[key] = _coerce(key, v)
        except ValueError as err:
            warnings.warn(f"環境変数 {var} を無視した: {err}")
    for key in DEFAULTS:
        v = _arg_get(args, key)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            continue   # 空文字は環境変数と同じく「未指定」（`--model ""` で空の名前を送らない）
        try:
            cfg[key] = _coerce(key, v)
        except ValueError as err:
            warnings.warn(f"引数 {key} を無視した: {err}")
    return cfg
