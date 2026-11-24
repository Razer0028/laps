"""Persistent session state at ~/.blackwall/sessions/<id>/.

- .progress.md  : scratchpad (Pi-4-Q)
- messages.jsonl: full conversation history
- meta.json     : start/end timestamps, model, prompt
"""
from __future__ import annotations
from . import paths as _lp
from laps import _envcompat
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


# ★`$` ではなく `\Z` を使う（2026-09-05・r4c・第4回c 反証 #2）。Python の `$` は
#   **末尾の改行 1 個の手前**にも一致するので、`^...$` は `"abc\n"` を通していた
#   （実測: `is_valid_session_id("abc\n") is True`／`sessions/abc\n/events.jsonl` が
#   実際に作られた）。書く側（`serve._owner_sink`）はこの門を通してから
#   `session_log_path()` へ渡すので、改行つきの dir が生まれる。一方
#   `Gateway.session_events` は**旧実装が `re.fullmatch` で弾いていた**——つまり
#   この関数へ一本化した時点で「書けるのに読めない」退行になっていた。
#   `\Z` は文字列の絶対末尾にしか一致しない。
_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}\Z")


def is_valid_session_id(sid: object) -> bool:
    """裸の 1 要素か（区切り・`..`・空・先頭の `.`・末尾の改行を通さない）。"""
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        return False
    return ".." not in sid and "/" not in sid and "\\" not in sid


def state_dir() -> Path:
    d = Path(str(_lp.home_dir()))
    d.mkdir(parents=True, exist_ok=True)
    (d / "sessions").mkdir(exist_ok=True)
    (d / "plugins").mkdir(exist_ok=True)
    return d


def sessions_dir() -> Path:
    """Where transcripts live — separable from the workspace on purpose.

    Benchmarks point LAPS_HOME inside the directory under test and then
    delete that directory, which takes the transcripts with it. 45 of 192
    recorded runs have no events.jsonl for this reason, and the scorer can only
    mark them unscoreable: not "the agent failed", just nothing left to read.
    The run that most needed explaining — a model inventing
    `python3: command not found` after a policy refusal — was one of them.

    LAPS_SESSIONS puts the record somewhere the run cannot delete.
    """
    override = os.environ.get("LAPS_SESSIONS")
    d = Path(override) if override else state_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_session_id() -> str:
    """走行の識別子。**測定する側が先に決められる**ようにしてある。

    ★なぜ env の口が要るか（2026-08-19）: ベンチは run.log を
      `grep 'session: ...'` して識別子を拾っていた。走行が時間切れで
      落ちるとその行が出ず、**results.tsv の sid が空**になる。
      空の行は「記録が読めない走行」になり、後から何が起きたか追えない。
      45/192 走行が別の理由で同じ状態になった前例もある（上の docstring）。
      測定する側が先に決めて渡せば、落ち方に関係なく記録に辿り着ける。

    ★衝突したら**黙って混ぜずに落とす**。2つの走行が同じディレクトリに
      追記すると、後から見て分離できない記録ができる（同日、同じ形で
      ベンチの出力先を共有して実際に壊した）。

    ★★変数名は `LAPS_NEW_SESSION_ID`。**`LAPS_SESSION_ID` ではない。**
      あちらは既存の別用途——`Agent.run` が「いまどのセッションか」を
      道具（TodoWrite / TaskCreate）へ伝えるために設定している。
      最初この関数が `LAPS_SESSION_ID` を読む実装にしたところ、
      **同じプロセスで2回目を走らせた瞬間に実機が壊れた**:
      1回目の走行がその変数を自分の id に設定し、2回目の生成が
      それを「外部からの指定」と読んで「既に存在します」で落ちる。
      実機は 0 秒・道具0本・本文0字で failed になった（v0.1.74）。
      ★1つの変数を「いま何か」と「これから何を作るか」の両方に使わない。
    """
    forced = (os.environ.get("LAPS_NEW_SESSION_ID") or "").strip()
    if not forced:
        return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    if any(c in forced for c in "/\\.\0"):
        raise ValueError(f"LAPS_NEW_SESSION_ID に使えない文字があります: {forced!r}")
    if (sessions_dir() / forced).exists():
        raise ValueError(
            f"LAPS_NEW_SESSION_ID={forced} は既に存在します。"
            "2つの走行が同じ記録に追記すると、後から分離できません。")
    return forced


@dataclass
class Session:
    id: str = field(default_factory=_new_session_id)
    root: Path = field(init=False)
    messages: list[dict] = field(default_factory=list)
    scratchpad: str = ""

    def __post_init__(self) -> None:
        self.root = sessions_dir() / self.id
        self.root.mkdir(parents=True, exist_ok=True)
        self._messages_path = self.root / "messages.jsonl"
        # ★2026-09-13（オーナー指摘「ハーネスで畳まれている内容をエージェント側で出せないのか」）:
        #   会話の**全文**はここに在る。畳みはモデルに送る**ビュー**を削るだけで、
        #   記録は 1 文字も落としていない。**場所を env に出しておけば、モデルは
        #   Bash で自分の記録を読み返せる**（道具を増やさずに済む＝この現場の方針）。
        try:
            _envcompat.setenv("LAPS_SESSION_FILE", str(self._messages_path))
        except Exception:                      # noqa: BLE001 — 記録の場所が出せなくても走る
            pass
        self._scratchpad_path = self.root / ".progress.md"
        self._meta_path = self.root / "meta.json"

    @classmethod
    def load(cls, sid: str) -> "Session":
        # ★id は**裸の 1 要素**だけ（2026-08-23 レビュー high）。`/resume ../../etc` や絶対パスを通すと、
        #   pathlib が根を差し替えて任意の既存ディレクトリに messages.jsonl を書く（実測で再現）。
        #   線の向こう（serve の send.session）からも来る値なので、ここで止める。
        if not is_valid_session_id(sid):
            raise ValueError(f"session id が不正です: {sid!r}（英数字・'_'・'-'・'.' の 1 要素のみ）")
        s = cls.__new__(cls)
        s.id = sid
        s.root = sessions_dir() / sid
        if not s.root.exists():
            raise FileNotFoundError(f"session {sid} not found at {s.root}")
        s._messages_path = s.root / "messages.jsonl"
        s._scratchpad_path = s.root / ".progress.md"
        s._meta_path = s.root / "meta.json"
        s.messages = []
        if s._messages_path.exists():
            with s._messages_path.open() as f:
                for ln in f:
                    try: s.messages.append(json.loads(ln))
                    except: continue
        s.scratchpad = s._scratchpad_path.read_text() if s._scratchpad_path.exists() else ""
        # ★読み直した会話の記録の場所も env に出す（2026-10-16 の点検: 新しく作ったときだけ立てていたので、serve が会話を
        #   読み直して走らせると、前に作った別の会話の記録を指したままだった）。
        try:
            _envcompat.setenv("LAPS_SESSION_FILE", str(s._messages_path))
        except Exception:                      # noqa: BLE001
            pass
        return s

    @classmethod
    def ephemeral(cls, prefix: str = "bw-child-") -> "Session":
        """使い捨ての会話（調べ物の子・oc/explore）。置き場は一時フォルダ（会話の一覧に出ない）で、
        LAPS_SESSION_FILE も書き換えない。使い終えたら discard() で消す。
        ★2026-10-16 の点検: Explore を呼ぶたびに会話の一覧へ消えない会話が 1 つ増え、親の記録の場所の env も子の物に
          差し替わっていた。"""
        import tempfile
        s = cls.__new__(cls)
        s.root = Path(tempfile.mkdtemp(prefix=prefix))
        s.id = "ephemeral_" + s.root.name.replace(prefix, "", 1)
        s._messages_path = s.root / "messages.jsonl"
        s._scratchpad_path = s.root / ".progress.md"
        s._meta_path = s.root / "meta.json"
        s.messages = []
        s.scratchpad = ""
        s.ephemeral_root = s.root
        return s

    def discard(self) -> None:
        """ephemeral() で作った会話の置き場を消す（それ以外の会話には何もしない）。"""
        root = getattr(self, "ephemeral_root", None)
        if root is None:
            return
        import shutil
        shutil.rmtree(root, ignore_errors=True)

    def append_message(self, msg: dict) -> None:
        self.messages.append(msg)
        line = json.dumps(msg, ensure_ascii=False)
        try:   # ★保存した API キーの値は記録に書かない（laps/apikeys.py・2026-10-08）
            from . import apikeys
            if apikeys.has_keys():
                line = apikeys.redact(line)[0]
        except Exception:
            pass
        with self._messages_path.open("a") as f:
            f.write(line + "\n")

    def rewrite_messages(self) -> None:
        """記録を self.messages で書き直す（会話で渡された API キーを保存した後に、前の発言から値を消すため）。"""
        tmp = self._messages_path.with_suffix(".jsonl.tmp")
        with tmp.open("w") as f:
            for m in self.messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        os.replace(tmp, self._messages_path)

    def write_scratchpad(self, text: str) -> None:
        self.scratchpad = text
        self._scratchpad_path.write_text(text)

    def append_scratchpad(self, text: str) -> None:
        self.scratchpad += text
        with self._scratchpad_path.open("a") as f:
            f.write(text)

    def write_meta(self, meta: dict) -> None:
        self._meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    @staticmethod
    def list_all() -> list[str]:
        return sorted([p.name for p in sessions_dir().iterdir() if p.is_dir()])
