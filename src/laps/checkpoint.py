"""A5「やり直せる」— 依頼1件のあいだだけ有効な、影の控え（チェックポイント）。

**何のためか**: 複数ファイルにまたがる編集は途中で失敗する。ApplyDiff は
`Applied 1/3 file(s)` を平然と返すし、Edit を3回呼ぶ流れは2回目で止まりうる。
そのとき残るのは「半分だけ新しい」木で、これが一番たちが悪い（動かないうえ、
どこまで進んだか分からない）。**触ったファイルだけを、依頼の開始時点に戻せる**
ようにするのがこの層。

---

## なぜ git を呼ばず、自前の控えにしたか

監査 backlog 11 の原案は「task-scoped shadow-git」。実装では **git を起動せず、
書き込み直前に元のバイト列を控える方式（copy-on-first-write）** を採った。理由:

1. **利用者の git を汚さない**が最優先の要件。git を使うと `GIT_DIR` を別に
   持っても index/lock/hooks/`core.autocrlf` が利用者の設定に引きずられる。
   Windows の `core.autocrlf=true` は控えた時点で CRLF を書き換える＝
   **控えそのものが原本と違う**（既存の CRLF 保持の作り込みが台無しになる）。
2. **git の外でも動く必要がある**。エージェントはリポジトリでない場所（/etc、
   設定ファイル、~/scratch）も編集する。git 前提だとそこで無効になる。
3. **バイナリ・非ASCII 名・長いパス**をバイト列のまま扱える。控えの名前は
   パスの sha256 にするので、Windows の 260 文字制限にも文字化けにも当たらない。
4. 外部プロセスを起こさない＝**決定論的で速い**（P40 律速のループで git を
   毎編集起こさない、は既存の規律）。

失うのは「git の履歴として後から漁れる」点だけ。diff は `difflib` で
**ハーネス側が計算する**ので、原案の目的（rollback＋diff）は満たす。

## 既定の挙動を変えない（★これが一番大事）

- **控えるのは既定 ON**（控えていなければ戻せない）が、**戻すのは明示的に
  呼ばれたときだけ**。黙って戻すと、利用者が意図した変更まで消える。
- **モデルには rollback を渡さない**。ツールとして生やせば、モデルが自分の
  失敗を隠すために利用者の変更ごと巻き戻せてしまう。呼べるのは人間だけ
  （CLI の `/rollback`）。
- 控えの成否はツールの結果に影響しない。控えに失敗しても編集は通す
  （＝この層の不具合でエージェントが止まらない）。

## 正直さの線

Bash が何を変えたかは**分からない**。だから Bash は控えの対象外で、
その事実を `untracked` に積み、rollback の報告に必ず出す。
「戻しました」とだけ言って Bash の副作用が残っている、が一番危ない嘘。
上限超過も同じで、**黙って諦めず** `skipped` に理由つきで残す。
"""
from __future__ import annotations

from . import paths as _lp
import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# 既定の上限。大きなファイル・多数のファイルで重くならないように置く。
# 8MiB: ソースコード・設定ファイルなら十分（このリポジトリ最大の .py は 65KB）。
DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
# 64MiB: 依頼1件ぶんの控えの総量。超えたら控えるのをやめて記録する。
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
# 500: 1件の依頼で 500 ファイルも触るなら、それはもう rollback の話ではない。
DEFAULT_MAX_FILES = 500
#: 作業場を**木ごと**控えるときの上限ファイル数（第4回b・core-hold-fix）。
#: 超えたら木の控えは諦め、追跡済みファイルだけの控えに落ちる（部分的な木の控えは
#: 「戻した」の意味が壊れるので作らない）。run53 の作業場は 2,200 ファイル・27MB。
DEFAULT_MAX_TREE_FILES = 5000
#: 木を控えるとき素通りする物。実行時状態（pid/log/sock/lock）・SQLite の一時ファイル・
#: 生成物・他の仕組みの台帳（`.blackwall-phases` は run_phases 自身が書く）。
#: ★これらを控えて戻すと**動いているサーバの pid を古い値に戻す**等の実害が出る。
#: ★`dist`/`build` は入れない: 検収が見る成果物（サイトの出力等）でありうる。`target`（Rust）は
#:   大きすぎて上限で木ごと諦める原因になるだけなので外す。
TREE_EXCLUDE_DIRS = frozenset({
    "node_modules", "__pycache__", "venv", "vendor", "target",
})
TREE_EXCLUDE_SUFFIXES = (".pid", ".log", ".sock", ".lock", "-wal", "-shm", "-journal",
                         ".pyc", ".pyo", ".swp", ".tmp", ".part")
#: 除外規則の場所（ドットのディレクトリ・TREE_EXCLUDE_DIRS の中・TREE_EXCLUDE_SUFFIXES・
#: シンボリックリンク・通常ファイル以外）を**数える**ときの上限（第4回c-3・反証 must_fix 1）。
#: ★印（mark）へ戻すとき、除外規則の場所は列挙されないので戻せない。だが「ファイルに残した効果は
#:   戻した」と言うなら、**そこに増えた/変わった物が何件あるか**は数えて出す。実物の検収は
#:   `T="${TMPDIR:-$PWD}/.acc-tmp"` に acc_*.json/log/txt を書く（bench/acceptance/vdir-phases.txt:
#:   107,116,117）＝ドットのディレクトリ＝盲点そのもの。作業ゼロの次の試行がそれで合格していた。
#: `.git`/`.venv` は数千〜数万ファイルなので木の上限（5,000）とは別に持つ。超えたら「数えられなかった」
#: と正直に出す（黙って 0 にしない）。
DEFAULT_MAX_EXCLUDED_FILES = 20000
#: 除外規則の場所を数えるときも見ない物: ハーネス自身の台帳（run_phases の state.json と ledger.json）。
#: 検収の最中に `ledger.note_acceptance` が ledger.json を書くので、数えると検収のたびに 1 件出る。
#: ★第4回c-4（再反証 M3）: 見ないのは**作業場直下の 1 つだけ**（`dirname(p) == root`）。名前一致で
#:   深さ無制限に外すと `site/.blackwall-phases/…` が木の列挙にも除外の列挙にも入らず「0 件」と偽れる。
EXCLUDED_SCAN_SKIP = frozenset({".blackwall-phases"})

# 控えを書けなかった理由。報告にそのまま出るので、意味の分かる語にしてある。
REASON_TEXT = {
    "too_large": "ファイルが大きすぎる（上限超過）",
    "too_many_files": "控えたファイル数が上限に達した",
    "store_full": "控えの総量が上限に達した",
    "read_error": "読めなかった",
    "not_a_file": "通常ファイルではない",
    "store_error": "控えを書けなかった",
    "excluded_too_big": "除外規則の場所を数えられなかった（上限超過）",
    # ★第4回c-5（再反証 must_fix 5）: 旧形は「木ごとの印ではない」を `tree_too_big`
    #   （＝木が上限を超えた）で表していた。**別の事実に同じ符号**を使うと、読む側
    #   （phases.py の `why`）が原因を取り違える。別コードにする。
    "not_tree": "木ごとの印ではない（増えた物は消していない）",
    # ★第4回c-5（再反証 must_fix 1）: 印に**入らなかった**ファイルがある（大きすぎ・読めない・
    #   置けない）＝その物は戻せないし、prune の「増えた物」でもない（触らない）。
    "mark_incomplete": "印に入らなかった物がある（そこは戻していない・消していない）",
    # ★第4回c-6（再反証 M3・致命）: 印（mark）と同じことが**控え（watermark）**の側にもある。
    #   控えに入らなかった物は「最後に合格した時点」を持っていない＝戻せないし、
    #   「控え以降に増えた物」でもない（印より前から在る）。
    "watermark_incomplete": "控えに入らなかった物がある（そこは戻していない）",
    # ★第4回c-6（再々反証・参考）: 木の側の 2 符号が表に無く、`Skipped.text()` が
    #   生の符号（"tree_too_big"）をそのまま人に見せていた。
    "tree_too_big": "木のファイル数が上限を超えた（木ごとにできなかった）",
    "tree_store_full": "木の控えが容量上限に入らなかった（木ごとにできなかった）",
}


def _env_int(env: dict, name: str, default: int) -> int:
    """環境変数を整数で読む。壊れた値で落ちない（この層の不具合で本体を止めない）。"""
    try:
        v = int(str(env.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_files: int = DEFAULT_MAX_FILES
    max_tree_files: int = DEFAULT_MAX_TREE_FILES
    max_excluded_files: int = DEFAULT_MAX_EXCLUDED_FILES

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Limits":
        e = os.environ if env is None else env
        return cls(
            max_file_bytes=_env_int(e, "LAPS_CHECKPOINT_MAX_FILE_BYTES",
                                    DEFAULT_MAX_FILE_BYTES),
            max_total_bytes=_env_int(e, "LAPS_CHECKPOINT_MAX_TOTAL_BYTES",
                                     DEFAULT_MAX_TOTAL_BYTES),
            max_files=_env_int(e, "LAPS_CHECKPOINT_MAX_FILES",
                               DEFAULT_MAX_FILES),
            max_tree_files=_env_int(e, "LAPS_CHECKPOINT_MAX_TREE_FILES",
                                    DEFAULT_MAX_TREE_FILES),
            max_excluded_files=_env_int(e, "LAPS_CHECKPOINT_MAX_EXCLUDED_FILES",
                                        DEFAULT_MAX_EXCLUDED_FILES),
        )


# ---- Bash の効果の分類（第4回b・§2-7）------------------------------------------
#
# ★なぜ要るか: 控えの対象外（Bash）の変更は、実記録で退行 20 件のうち 13 件に挟まっていた
#   （bench/replay_hold.py）。「Bash があった」だけで一律に諦めると安全網が実質死ぬ
#   （run52 の発火 2 例はどちらも Bash 60 回／12 回）ので、**何を変えうる形か**で分ける。
#   ここが本番と再生（bench/replay_hold.py）の**同じ 1 本**。試験が写しを持たない。
#     write … 作業場の**ファイル**を書き換えうる形（木ごとの控えで戻せる）
#     env   … ファイル以外の状態（プロセス・パッケージ・ポート）を変える形（戻せない）
#   保守的＝疑わしきは write（読取り専用と断定できる形だけ空集合）。
_BASH_NOISE_RE = re.compile(r"\s*[12]?>\s*/dev/null|\s*2>&1|\s*>&2|\s-[a-zA-Z]*[oO]\s*/dev/null")
_BASH_WRITE_RE = re.compile(
    r"sed\s+(-[a-zA-Z]*i|--in-place)|\btee\b|(?<![<>|&])>(?!>?&)"
    r"|\b(mv|cp|rm|rmdir|touch|mkdir|ln|truncate|dd|patch|install|rsync|wget|unzip|gunzip)\b"
    r"|\bcurl\b[^|;&]*\s-[a-zA-Z]*[oO]\b|\btar\s+[a-zA-Z-]*x"
    r"|\bsqlite3\b|\bchmod\b|\bchown\b"
    r"|\bgit\s+(checkout|reset|stash|mv|rm|clean|pull|merge|rebase|apply|commit|add|restore)\b"
    r"|\bmake\b|\bnpm\s+(run|install|ci)\b|\bcomposer\b"
    # スクリプトの実行（何を書くか分からない＝疑わしきは write）
    r"|\b(python[0-9.]*|php|bash|sh|zsh|node|perl|ruby)\s+(?!-m\s+(pytest|json\.tool|py_compile)\b|-[lvV]\b|--version\b)[^\s|;&]*\.(py|php|sh|js|pl|rb)\b"
    r"|(?:^|[;&|]\s*)\./[^\s|;&]+"
)
_BASH_ENV_RE = re.compile(
    r"\b(kill|pkill|killall|systemctl|service|docker|nohup|dpkg|apt|apt-get|pip|pip3|npm|snap"
    r"|iptables|nft|mount|umount|ssh|scp|useradd|nginx|php-fpm)\b"
    r"|\bphp\s+-S\b|\bpython[0-9.]*\s+-m\s+http\.server\b"
    # start.sh/stop.sh は**実行された**形だけ（`chmod +x start.sh` は環境操作ではない）
    r"|(?:^|[;&|(]\s*|(?<![\w.\-/])(?:bash|sh|nohup|exec)\s+|\./)(?:[^\s|;&]*/)?(?:start|stop|restart)\.sh\b"
    r"|&\s*$"
)


def classify_bash(cmd: str) -> frozenset:
    """Bash コマンドの効果を {"write","env"} の部分集合で返す（空＝読取り専用と見なす）。"""
    c = _BASH_NOISE_RE.sub("", str(cmd or ""))
    out = set()
    for line in c.split("\n"):
        if _BASH_WRITE_RE.search(line):
            out.add("write")
        if _BASH_ENV_RE.search(line):
            out.add("env")
    return frozenset(out)


def tree_scope_allowed(root: str | Path | None, *, store_root: str | Path | None = None) -> str:
    """作業場を木ごと控えてよいか。よければ実体のパス、駄目なら空文字。

    ★断る側に倒す: `/`・ホーム・浅すぎる場所（`/srv` 等）・控え置き場や LAPS_HOME を
      含む場所は、戻したときの爆発半径が「依頼の成果物」を超える。
    """
    if not root:
        return ""
    try:
        r = os.path.realpath(str(root))
        if not os.path.isdir(r):
            return ""
        home = os.path.realpath(os.path.expanduser("~"))
        if r in ("/", home) or r.count(os.sep) < 2:
            return ""
        for inner in (store_root, str(_lp.home_dir())):
            if inner:
                ip = os.path.realpath(str(inner))
                if ip == r or ip.startswith(r.rstrip(os.sep) + os.sep):
                    return ""
    except OSError:
        return ""
    return r


def iter_tree_files(root: str, limits: Limits) -> "tuple[list[str], bool]":
    """木の中の通常ファイルを列挙する。(paths, complete)。上限を超えたら (途中, False)。

    ドットで始まるディレクトリ（`.git`/`.blackwall-phases`/`.venv` …）と TREE_EXCLUDE_DIRS は
    降りない。シンボリックリンクは辿らない（外へ出ないため）。
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not _is_excluded_dir(dirpath, d))
        for fn in sorted(filenames):
            if fn.endswith(TREE_EXCLUDE_SUFFIXES):
                continue
            p = os.path.join(dirpath, fn)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            out.append(p)
            if len(out) > limits.max_tree_files:
                return out, False
    return out, True


def iter_tree_links(root: str, limits: Limits) -> "dict[str, str]":
    """木の中の**記号リンク**を `{path: リンク先の文字列}` で返す。辿らない・先は読まない。

    ★なぜ要るか（2026-09-20・敵対レビュー）: `iter_tree_files` はリンクを**丸ごと飛ばす**
      （外へ出ないため）。その結果、シェルで `sites-enabled/default` を張り替えても、リンクを普通の
      ファイルに置き換えても、控えにも報告にも一切出ず、`restore_watermark` は
      **「戻しました・complete=True」**と言っていた（実測）。インフラの変更はリンクの張り替えが本体なので、
      いちばん戻したい物が盲点だった。中身ではなく**向き先の文字列**だけを控えれば、外へは出ない。
    OSS: opencode は控えの git に `core.symlinks=true` を明示している（`snapshot/index.ts:25`）。
    """
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames) + sorted(filenames):
            pth = os.path.join(dirpath, name)
            if os.path.islink(pth):
                try:
                    out[pth] = os.readlink(pth)
                except OSError:
                    continue
                if len(out) > limits.max_tree_files:
                    return out
        dirnames[:] = sorted(d for d in dirnames if not _is_excluded_dir(dirpath, d))
    return out


def _is_excluded_dir(dirpath: str, name: str) -> bool:
    """`iter_tree_files` が降りないディレクトリか（ドット・TREE_EXCLUDE_DIRS・リンク）。"""
    return (name.startswith(".") or name in TREE_EXCLUDE_DIRS
            or os.path.islink(os.path.join(dirpath, name)))


def iter_excluded_files(root: str, limits: Limits) -> "tuple[dict[str, tuple[int, int]], bool]":
    """木の中だが**除外規則で `iter_tree_files` が列挙しない物**を数える。
    戻り値 ({path: (size, mtime_ns)}, complete)。上限を超えたら (途中, False)。

    ★何のためか（第4回c-3・反証 must_fix 1）: 印へ戻す `revert_to_mark` の prune は
      `iter_tree_files` の列挙しか見ない。除外規則の場所（ドットのディレクトリ・`.lock` 等）に
      検収が残した物は戻らないし消えない。ここは**戻すためではなく、戻せなかった物を数えて
      言うため**の列挙＝`iter_tree_files` の**補集合**を、同じ規則（同じ関数 `_is_excluded_dir`・
      同じ `TREE_EXCLUDE_SUFFIXES`）で作る。規則が片側だけ変わると盲点が復活するので、
      試験（tests/test_checkpoint.py）が「木の列挙 ∪ 除外の列挙 ＝ 全ファイル」を固定する。
    含む物: 除外ディレクトリの中の全ファイル（深さ問わず）／除外サフィックスのファイル／
      シンボリックリンク（辿らない・リンク自体を 1 件と数える）／通常ファイル以外。
    含まない物: `EXCLUDED_SCAN_SKIP`（ハーネス自身の台帳）——**作業場直下の 1 つだけ**。
    ★第4回c-4（再反証 M3）: 旧形は名前一致で深さ無制限に外していたので、`site/.blackwall-phases/
      acc_sentinel` が木の列挙にも除外の列挙にも入らず「増えた物は 0 件」と積極的に偽れた（不変条件
      「木 ∪ 除外 ＝ 全ファイル」が崩れる）。run_phases の台帳は作業場直下にしか無い。
    """
    out: dict[str, tuple[int, int]] = {}
    cap = max(0, int(limits.max_excluded_files))
    root = os.fspath(root)

    def _stat(p: str) -> "tuple[int, int] | None":
        try:
            st = os.lstat(p)
        except OSError:
            return None
        return (int(st.st_size), int(getattr(st, "st_mtime_ns", 0) or 0))

    def _add(p: str) -> bool:
        v = _stat(p)
        if v is not None:
            out[p] = v
        return len(out) <= cap

    def _walk_all(top: str) -> bool:
        """除外ディレクトリの中身を全部数える（リンクは辿らず 1 件に数える）。"""
        for dp, dns, fns in os.walk(top, followlinks=False):
            keep: list[str] = []
            for d in sorted(dns):
                # ★ここは作業場より深い所＝EXCLUDED_SCAN_SKIP は外さない（直下の 1 つだけが対象）。
                if os.path.islink(os.path.join(dp, d)):
                    if not _add(os.path.join(dp, d)):
                        return False
                else:
                    keep.append(d)
            dns[:] = keep
            for fn in sorted(fns):
                if not _add(os.path.join(dp, fn)):
                    return False
        return True

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep: list[str] = []
        at_root = (dirpath == root)          # os.walk の最初の 1 回だけ top をそのまま返す
        for d in sorted(dirnames):
            if at_root and d in EXCLUDED_SCAN_SKIP:
                continue
            if _is_excluded_dir(dirpath, d):
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    if not _add(full):
                        return out, False
                elif not _walk_all(full):
                    return out, False
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            if fn.endswith(TREE_EXCLUDE_SUFFIXES) or os.path.islink(p) or not os.path.isfile(p):
                if not _add(p):
                    return out, False
    return out, True


def normalize_key(path: str, *, _pathmod=None) -> str:
    """同じファイルを指す別表記を、1つの鍵にまとめる。

    Windows で効く: `C:\\A\\b.TXT` と `c:/a/B.txt` は同じファイルなので、
    別々に控えると **1回目の控えが2回目に上書きされて原本が失われる**。
    `normcase` が大文字小文字と `/`→`\\` を吸収し、`abspath` が相対を潰す。

    `_pathmod` は試験用（`ntpath` を渡すと Linux 上でも Windows の規則で
    確かめられる）。本番は常に `os.path`。
    """
    pm = _pathmod or os.path
    p = str(path)
    try:
        # realpath: シンボリックリンク越しの編集は実体を書き換えるので、
        # 控えるのも戻すのも実体側でなければ辻褄が合わない。
        p = pm.realpath(pm.abspath(p))
    except (OSError, ValueError):
        # 壊れたリンクや長すぎるパスでも鍵は作れる（控えの可否は record が決める）
        try:
            p = pm.abspath(p)
        except (OSError, ValueError):
            pass
    return pm.normcase(p)


def _blob_name(key: str) -> str:
    """控えファイルの名前。非ASCII 名・長いパス・パス区切りを完全に回避する。"""
    h = hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()
    return h[:40] + ".blob"


def _missing_ancestors(path: Path) -> list[str]:
    """まだ存在しない祖先ディレクトリを、浅い順に返す。

    Write は親ディレクトリを自動で作る。戻すときにファイルだけ消すと、
    空のディレクトリが残って「元に戻った」と言えない。ここで控えておいて、
    **自分が作った分だけ・空のときだけ** 消す。
    """
    missing: list[str] = []
    cur = path.parent
    seen = 0
    while seen < 64:  # 病的に深いパスで無限に登らない
        try:
            if cur.exists():
                break
        except OSError:
            break
        missing.append(str(cur))
        nxt = cur.parent
        if nxt == cur:
            break
        cur = nxt
        seen += 1
    missing.reverse()  # 浅い順
    return missing


@dataclass
class Entry:
    """1ファイルぶんの控え。"""
    key: str
    path: str                    # 受け取った表記（報告用）
    existed: bool
    blob: str | None = None      # 控えの実体（existed=False なら None）
    size: int = 0
    mode: int | None = None
    created_dirs: list[str] = field(default_factory=list)


#: ★第4回c-7（再反証 must_fix 1）: `Skipped` のうち **1 ファイルを指さない要約行**の符号。
#:   path は木の根（または "(控え)"）で、ファイルの数に混ぜると 1 本が 2 件・3 件に見える
#:   （実測 Q3b button: 同じ videos/big.txt が 2 行＋要約行 1 行で「控えを取れていなかった
#:   ファイル 3 件」＝実体は 1 本）。件数を出す側はこれを除く。
SUMMARY_REASONS = ("watermark_incomplete", "mark_incomplete", "not_tree",
                   "tree_too_big", "tree_store_full", "excluded_too_big")


def _lstat_sig(path) -> "tuple[int, int] | None":
    """ファイルの指紋 `(size, mtime_ns)`。読めなければ None（＝不明・0 と言わない）。

    ★`iter_excluded_files` の `_stat` と同じ形（辿らない `lstat`）。控えられなかった物が
      あとで「変わったか」を言うために使う。
    """
    try:
        st = os.lstat(os.fspath(path))
    except (OSError, TypeError, ValueError):
        return None
    return (int(st.st_size), int(getattr(st, "st_mtime_ns", 0) or 0))


@dataclass
class Skipped:
    path: str
    reason: str
    detail: str = ""
    #: ★第4回c-7（再反証 must_fix 2）: 控えられ**なかった**その時点の指紋 `(size, mtime_ns)`。
    #:   `stash()` の skip なら「手番の終わり」の姿＝あとで今と比べれば「変わった／変わって
    #:   いない／不明」を言い分けられる（旧形は誰も触っていない物を「戻せていない」と断定した）。
    #:   読めなければ None。
    stat: "tuple[int, int] | None" = None

    def text(self) -> str:
        r = REASON_TEXT.get(self.reason, self.reason)
        return f"{self.path}: {r}" + (f"（{self.detail}）" if self.detail else "")

    def changed_now(self) -> "bool | None":
        """控えられなかった時点から**いま**までに変わったか。True=変わった／False=**大きさと
        更新時刻が同じ**（中身までは見ていない）／None=確かめられない（指紋が無い・いま見られない）。
        **推測しない。**

        ★第4回c-8（再々反証 N1 deleted・過小申告）: 旧形は `_lstat_sig` が None を返したら
          いつでも None（＝「変わったかは確かめていません」）と言った。`_lstat_sig` は
          **消えた場合も**見られない場合も None を返すので、手番の終わりには在った 9 MiB を
          余分な検収が `rm` したとき、消えたことが分かるのに「確かめていません」と濁した
          （実測 base_n1_deleted: turn_end=(9437184, …) → いま absent なのに undo_unverified=1）。
          消えた（ENOENT）は**確実に変わった**。それ以外の理由で見られないときだけ None。
        ★False は「中身まで同じ」ではない（呼び元の文面はそこを言い分ける）。実測
          base_n1_samesig: `touch -r` + `cp -p` で (size, mtime_ns) が完全一致のまま
          sha256 が d2ee4703cd96 → be1c8c26faae に変わる＝この指紋では中身の同一は言えない。
        """
        if self.stat is None:
            return None
        now = _lstat_sig(self.path)
        if now is None:
            try:
                os.lstat(os.fspath(self.path))
            except FileNotFoundError:
                return True              # 手番の終わりには在った物がいまは無い＝確実に変わった
            except (OSError, TypeError, ValueError):
                return None              # 見られない＝分からない（消えたとは言わない）
            return None                  # lstat は通ったのに指紋が取れない（競合）＝濁す
        return now != tuple(self.stat)


@dataclass
class RollbackReport:
    """戻した結果。**戻せなかったものを必ず持つ**のがこの型の役目。"""
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    #: 木ごとの控えで、控え**以降**に Bash 等で増えたファイル（消さずに残した物）。
    extras: list[str] = field(default_factory=list)
    #: ★第4回c-3: 印（mark）へ戻すとき、**除外規則の場所**（ドットのディレクトリ・`.lock` 等・
    #:   リンク）に増えた/変わった/消えた物。戻していない・消していない（列挙の外＝盲点だった所）。
    #:   `unknown_counted` が False なら数えられなかった（上限超過・木ごとの印でない）＝ 0 ではない。
    unknown: list[str] = field(default_factory=list)
    unknown_counted: bool = True
    #: どの時点へ戻したか（報告の 1 行目に出る）。rollback は依頼開始時点、
    #: restore_watermark は「最後に合格数を更新した時点」。
    label: str = "依頼開始時点"

    @property
    def ok(self) -> bool:
        """戻せなかったファイルが1つも無いか。

        ★`skipped`（上限超過）と `untracked`（Bash）は ok に含めない：
        これらは「戻せない」ではなく「そもそも控えていない」。混ぜると
        「戻したのに失敗と出る」か「戻していないのに成功と出る」のどちらかになる。
        判定に使うときは `ok and not skipped and not untracked` と書くこと。
        """
        return not self.failed

    @property
    def complete(self) -> bool:
        """控え漏れも無く、完全に元通りと言えるか。"""
        return self.ok and not self.skipped and not self.untracked

    def text(self) -> str:
        lines: list[str] = []
        n = len(self.restored) + len(self.deleted) + len(self.unchanged)
        lines.append(f"やり直し: {n} ファイルを{self.label}に戻しました"
                     f"（復元 {len(self.restored)} / 新規作成を削除 {len(self.deleted)}"
                     f" / 元から同じ {len(self.unchanged)}）")
        for p in self.restored:
            lines.append(f"  ← {p}")
        for p in self.deleted:
            lines.append(f"  ✗ {p}（この依頼で作られたので消しました）")
        if self.failed:
            lines.append(f"★戻せなかったファイル {len(self.failed)} 件:")
            for p, err in self.failed:
                lines.append(f"  ! {p}: {err}")
        # ★第4回c-7（再反証 must_fix 1）: 件数は**実ファイルの行だけ**で数える。要約行
        #   （`watermark_incomplete` 等・path は木の根）を混ぜると 1 本が 2 件・3 件に見えた
        #   （実測 Q3b button: 実体 1 本なのに「控えを取れていなかったファイル 3 件」）。
        _files = [s for s in self.skipped if s.reason not in SUMMARY_REASONS]
        _notes = [s for s in self.skipped if s.reason in SUMMARY_REASONS]
        if _files:
            lines.append(f"★控えを取れていなかったファイル {len(_files)} 件"
                         f"（上限超過など・**変更は残っています**）:")
            for s in _files:
                lines.append(f"  ? {s.text()}")
        if _notes:
            lines.append("★控えの**範囲**について（ファイルの件数ではありません）:")
            for s in _notes:
                lines.append(f"  ? {s.text()}")
        if self.untracked:
            lines.append(f"★控えの対象外の操作が {len(self.untracked)} 件あります"
                         f"（Bash など。何を変えたか分からないので戻せません）:")
            for u in self.untracked[:10]:
                lines.append(f"  ~ {u}")
            if len(self.untracked) > 10:
                lines.append(f"  ~ …ほか {len(self.untracked) - 10} 件")
        if self.unknown:
            lines.append(f"★除外規則の場所（ドットのディレクトリ・.lock 等）に増えた/変わった物 "
                         f"{len(self.unknown)} 件（**戻していません**）:")
            for u in self.unknown[:10]:
                lines.append(f"  ? {u}")
            if len(self.unknown) > 10:
                lines.append(f"  ? …ほか {len(self.unknown) - 10} 件")
        elif not self.unknown_counted:
            # ★第4回c-4（再反証 M1）: 2 原因を 1 文に潰さない（上限超過／木ごとの印でない）。
            if any(s.reason == "excluded_too_big" for s in self.skipped):
                lines.append("★除外規則の場所（ドットのディレクトリ・.lock 等）は上限超過で数えられませんでした"
                             "＝そこに残った物は不明です")
            else:
                lines.append("★除外規則の場所（ドットのディレクトリ・.lock 等）は数えていません"
                             "（木ごとの印でない）＝そこに残った物は不明です")
        return "\n".join(lines)


class Checkpoint:
    """依頼1件ぶんの控え。書き込み直前に `record()`、戻すときだけ `rollback()`。"""

    def __init__(self, root: str | Path, *, task_id: str = "",
                 limits: Limits | None = None) -> None:
        self.task_id = task_id or (time.strftime("%Y%m%d_%H%M%S") + "_"
                                   + hashlib.sha256(str(time.time_ns())
                                                    .encode()).hexdigest()[:6])
        self.root = Path(root)
        self.limits = limits or Limits()
        self._entries: dict[str, Entry] = {}
        self.skipped: list[Skipped] = []
        self.untracked: list[str] = []
        self.total_bytes = 0
        self.closed = False
        self._root_made = False
        # ---- ウォーターマーク（部分合格の最高到達点）-----------------------
        # ★2026-09-05・第4回 core-hold-fix。検収で「合格数が過去最高を更新した瞬間」の
        #   追跡済みファイルの現在値。rollback（依頼開始時点）とは別の層。
        #   phases.py の PhaseAcceptance ハンドラが snapshot_watermark() を呼び、
        #   RestoreLastPassing 道具と工程末の安全網が restore_watermark() を呼ぶ。
        self._watermark: dict[str, Entry] = {}
        self._wm_links: dict[str, str] = {}       # 木ごとの控えのときの記号リンク（path → 向き先）
        self._wm_skipped: list[Skipped] = []
        #: 控えた時点での untracked の長さ。restore の報告に出すのは**それ以降**の分だけ
        #: （合格した時点より前の Bash は、合格状態の一部なので「戻せない」の対象ではない）。
        self._wm_untracked_at = 0
        #: 何回控え直したか（0 発火と無効果を区別するため）。
        self.watermark_count = 0
        #: 控えの範囲: "" / "tracked"（追跡済みファイルだけ）/ "tree"（作業場を木ごと）。
        self.watermark_scope = ""
        self._wm_tree_root = ""
        self.watermark_bytes = 0
        # ---- 復元前の控え（stash）------------------------------------------------
        # ★第4回b（反証: 戻して悪化した例を本番 run_phases で再現された）。opencode
        #   `session/revert.ts:70` 「戻す前に今を控える」＝取り消せる形。
        #   工程末の安全網が stash()→restore_watermark()→再検収→増えなければ unstash()。
        self._stash: dict[str, Entry] = {}
        self._stash_skipped: list[Skipped] = []
        self.stash_bytes = 0
        #: ★第4回c-6（再反証 M4）: 直前の stash() が「取り消せない」と断った実ファイル。
        self._stash_refused: tuple = ()
        # ---- 印（mark）＝副作用のある操作の**前**に置き、後で戻す -------------------
        # ★第4回c-2（反証 must_fix 1・atk_b）: 工程末の安全網は復元後に検収を**もう 1 回**走らせる。
        #   live の検収は冪等でない（`bash start.sh`・印を touch・DB を書く）ので、その 2 回目の
        #   副作用が作業場に残ったまま次の試行へ渡り、次の試行が**作業ゼロで合格**する形があった。
        #   印は「検収を走らせる直前の木」を控え、走らせた後に **変えた物は戻し・増えた物は消す**
        #   （木の範囲内・除外規則 TREE_EXCLUDE_* の外は触らない）。木の外・プロセス・環境は戻らない
        #   ＝呼び元が注記で明示する。
        self._mark: dict[str, Entry] = {}
        self._mark_skipped: list[Skipped] = []
        self.mark_bytes = 0
        self.mark_scope = ""
        self._mark_tree_root = ""
        #: ★第4回c-3: 印を置いた時点の**除外規則の場所**の一覧 {path: (size, mtime_ns)}。
        #:   戻すときに突き合わせ、増えた/変わった/消えた物を `RollbackReport.unknown` に出す。
        self._mark_excluded: "dict[str, tuple[int, int]] | None" = None
        self._mark_excluded_complete = True
        #: ★第4回c-4（再反証 M2）: 印を**置いた**事実。`_mark` の中身（控えた項目）とは別のビット。
        #:   木に普通のファイルが 0 件なら `_mark == {}` だが印は置けている（増えた物を消すために要る）。
        #:   旧形は `has_mark`/`revert_to_mark` が `bool(self._mark)` で判断していたので、mark() が True
        #:   を返したのに revert_to_mark() が None・drop されず `_mark_excluded` が残る、という不整合があった。
        self._mark_placed = False
        #: untracked の原文（note_untracked は 200 字で切るので、分類用に別に持つ）。
        self._untracked_cmds: list[tuple[str, str]] = []

    # ---- 控える -------------------------------------------------------

    def _ensure_root(self) -> None:
        if not self._root_made:
            self.root.mkdir(parents=True, exist_ok=True)
            self._root_made = True

    def record(self, path: str) -> str:
        """書き込み直前に、いまのバイト列を控える。

        戻り値は状態語（"recorded" / "created" / "already" / "skipped:<理由>"）。
        **例外は投げない**。控えられなくても編集自体は通すのがこの層の約束。
        """
        if self.closed or not path:
            return "closed" if self.closed else "skipped:not_a_file"
        key = normalize_key(path)
        if key in self._entries:
            # 最初の1回だけ控える＝「依頼の開始時点」が保たれる。
            # 2回目で上書きすると、1回目の編集後の姿が“元”になってしまう。
            return "already"
        p = Path(path)

        # 上限（ファイル数）。存在しないファイルの記録も枠を使う——枠を使わないと
        # 「新規作成 1000 件」で戻す対象が無制限に増える。
        if len(self._entries) >= self.limits.max_files:
            self._note_skip(path, "too_many_files",
                            f"上限 {self.limits.max_files} ファイル")
            return "skipped:too_many_files"

        try:
            exists = p.exists()
        except OSError as e:
            self._note_skip(path, "read_error", str(e))
            return "skipped:read_error"

        if not exists:
            # まだ無いファイル＝この依頼で作られる。控えるバイト列は無いが、
            # 「元は無かった」ことを覚えておかないと戻せない（消せない）。
            self._entries[key] = Entry(key=key, path=str(path), existed=False,
                                       created_dirs=_missing_ancestors(p))
            return "created"

        if not p.is_file():
            self._note_skip(path, "not_a_file", "ディレクトリ等")
            return "skipped:not_a_file"

        try:
            size = p.stat().st_size
        except OSError as e:
            self._note_skip(path, "read_error", str(e))
            return "skipped:read_error"

        if size > self.limits.max_file_bytes:
            self._note_skip(path, "too_large",
                            f"{size} bytes > 上限 {self.limits.max_file_bytes}")
            return "skipped:too_large"
        if self.stored_bytes + size > self.limits.max_total_bytes:
            self._note_skip(path, "store_full",
                            f"控え合計 {self.stored_bytes}+{size} > "
                            f"上限 {self.limits.max_total_bytes}")
            return "skipped:store_full"

        blob = _blob_name(key)
        # ★第4回c-6（再々反証 must_fix 4）: **読みと書きを別の try に分ける。**
        #   旧形は 1 つの `except OSError` に押し込んでいたので、`p.read_bytes()` が
        #   落ちた物（mode 000 の read しか奪われていないファイル等）を
        #   `store_error`＝「控えを書けなかった」と報告した＝**読めなかったのに
        #   書けなかったと言う**（実測 err-perm000b: 「sec.txt: 控えを書けなかった
        #   （[Errno 13] Permission denied）」）。原因が違えば直し方も違う。
        try:
            # バイト列のまま複製する。decode すると CRLF・BOM・不正 UTF-8 が
            # 変質して「戻したのに diff が出る」になる。
            data = p.read_bytes()
        except OSError as e:
            self._note_skip(path, "read_error", str(e))
            return "skipped:read_error"
        try:
            self._ensure_root()
            (self.root / blob).write_bytes(data)
            mode = None
            try:
                mode = p.stat().st_mode & 0o777
            except OSError:
                pass
        except OSError as e:
            self._note_skip(path, "store_error", str(e))
            return "skipped:store_error"

        self._entries[key] = Entry(key=key, path=str(path), existed=True,
                                   blob=blob, size=len(data), mode=mode)
        self.total_bytes += len(data)
        return "recorded"

    def record_many(self, paths) -> list[str]:
        return [self.record(p) for p in paths if p]

    def note_untracked(self, tool: str, detail: str = "") -> None:
        """控えられない操作（Bash 等）を、忘れずに残す。"""
        if self.closed:
            return
        d = (detail or "").replace("\n", " ")
        if len(d) > 200:
            d = d[:200] + "…"
        self.untracked.append(f"{tool}: {d}" if d else tool)
        self._untracked_cmds.append((tool, str(detail or "")))

    def untracked_since_watermark(self) -> list[str]:
        """控え（ウォーターマーク）以降の、控えの対象外の操作。控えが無ければ全部。"""
        return list(self.untracked[self._wm_untracked_at:])

    def bash_effects_since_watermark(self) -> dict:
        """控え以降の Bash 等を効果で数える: {"write": n, "env": n, "read": n}。"""
        out = {"write": 0, "env": 0, "read": 0}
        for tool, cmd in self._untracked_cmds[self._wm_untracked_at:]:
            fx = classify_bash(cmd) if tool == "Bash" else frozenset({"write", "env"})
            if not fx:
                out["read"] += 1
            for k in fx:
                out[k] += 1
        return out

    def _note_skip(self, path: str, reason: str, detail: str = "") -> None:
        self.skipped.append(Skipped(path=str(path), reason=reason, detail=detail,
                                    stat=_lstat_sig(path)))

    # ---- 見る ---------------------------------------------------------

    @property
    def entries(self) -> list[Entry]:
        return list(self._entries.values())

    def touched(self) -> list[str]:
        """この依頼で触った（控えた）ファイルの一覧。"""
        return [e.path for e in self._entries.values()]

    @property
    def stored_bytes(self) -> int:
        """置き場に実際にある控えの合計（依頼開始時点＋ウォーターマーク＋復元前の控え＋印）。
        ★第4回b: 以前は依頼開始時点ぶんだけ数えていたので、上限の約 2 倍まで育ちえた。"""
        return self.total_bytes + self.watermark_bytes + self.stash_bytes + self.mark_bytes

    def stats(self) -> dict:
        return {
            "task_id": self.task_id,
            "files": len(self._entries),
            "bytes": self.stored_bytes,
            "base_bytes": self.total_bytes,
            "watermark_bytes": self.watermark_bytes,
            "stash_bytes": self.stash_bytes,
            "mark_bytes": self.mark_bytes,
            "skipped": len(self.skipped),
            "untracked": len(self.untracked),
            "watermark_files": len(self._watermark),
            "watermark_scope": self.watermark_scope,
            "watermarks_taken": self.watermark_count,
        }

    def summary(self) -> str:
        s = self.stats()
        out = [f"チェックポイント {s['task_id']}: 控え {s['files']} ファイル / "
               f"{s['bytes']} bytes"]
        if self.skipped:
            out.append(f"  控えられなかったもの {len(self.skipped)} 件:")
            out.extend("    " + x.text() for x in self.skipped)
        if self.untracked:
            out.append(f"  控えの対象外の操作 {len(self.untracked)} 件（Bash 等）")
        return "\n".join(out)

    def diff(self, *, max_bytes: int = 200_000) -> str:
        """控えと現在の差分。**ハーネス側で計算する**（git を起こさない）。"""
        import difflib
        chunks: list[str] = []
        size = 0
        for e in sorted(self._entries.values(), key=lambda x: x.path):
            before = b""
            if e.existed and e.blob:
                try:
                    before = (self.root / e.blob).read_bytes()
                except OSError:
                    chunks.append(f"--- {e.path}\n（控えが読めません）")
                    continue
            p = Path(e.path)
            after = b""
            now_exists = False
            try:
                now_exists = p.is_file()
                if now_exists:
                    after = p.read_bytes()
            except OSError as ex:
                chunks.append(f"--- {e.path}\n（現在の内容が読めません: {ex}）")
                continue
            if before == after and e.existed == now_exists:
                continue
            if not e.existed and now_exists:
                head = f"+++ {e.path}（新規作成 {len(after)} bytes）"
            elif e.existed and not now_exists:
                head = f"--- {e.path}（削除された。元は {len(before)} bytes）"
            else:
                head = f"~~~ {e.path}"
            if b"\x00" in before or b"\x00" in after:
                chunks.append(f"{head}\n（バイナリ: {len(before)} → {len(after)} bytes）")
            else:
                a = before.decode("utf-8", "replace").splitlines(keepends=True)
                b = after.decode("utf-8", "replace").splitlines(keepends=True)
                body = "".join(difflib.unified_diff(a, b, "before", "after", n=2))
                chunks.append(head + "\n" + body)
            size += len(chunks[-1])
            if size > max_bytes:
                chunks.append(f"…（差分が {max_bytes} bytes を超えたので打ち切り）")
                break
        return "\n".join(chunks) if chunks else "（変更はありません）"

    # ---- 戻す ---------------------------------------------------------

    def rollback(self, paths=None) -> RollbackReport:
        """控えた状態へ戻す。**明示的に呼ばれたときだけ実行される。**

        `paths` を渡すとその分だけ。渡さなければ触ったファイル全部。
        控えを消さないので、同じ rollback を2回呼んでも壊れない（2回目は
        すべて `unchanged` になる）。
        """
        rep = RollbackReport(skipped=list(self.skipped), untracked=list(self.untracked))
        if paths is None:
            targets = list(self._entries.values())
        else:
            want = {normalize_key(p) for p in paths}
            targets = [e for e in self._entries.values() if e.key in want]
            missing = want - {e.key for e in targets}
            for m in missing:
                rep.failed.append((m, "このチェックポイントに控えがありません"))
        for e in targets:
            self._restore_one(e, rep)
        return rep

    def _restore_one(self, e: Entry, rep: RollbackReport) -> None:
        p = Path(e.path)
        try:
            now_exists = p.is_file()
        except OSError as ex:
            rep.failed.append((e.path, f"状態を確認できません: {ex}"))
            return
        # ★第4回c（反証 must_fix 3）: SQLite の WAL／ホットジャーナルが**中身つきで**併存する
        #   DB は戻さない。`TREE_EXCLUDE_SUFFIXES` が -wal/-shm/-journal を控えないので、本体だけ
        #   戻すと (a) WAL の frame が次の open で戻した本体に再適用され復元が黙って消える、
        #   (b) ホットジャーナルが戻した本体へロールバックされて壊れる。反証の実測: WAL で
        #   300 行→控え→210 行→restore→**210 行のまま「復元 0 / 元から同じ 1」**＝戻っていない
        #   のに失敗が出なかった。戻せない物は `failed` に理由つきで出す（aider cmd_undo の作法）。
        side = _live_sqlite_sidecar(p)
        if side:
            rep.failed.append((e.path, f"SQLite の {side} が併存しているので戻していません"
                                       "（本体だけ戻すと中身が戻らないか壊れる。DB を閉じて"
                                       " `PRAGMA wal_checkpoint(TRUNCATE)` してから）"))
            return

        if not e.existed:
            # 元は無かった＝この依頼で作られたので消す。
            if not now_exists:
                rep.unchanged.append(e.path)
            else:
                err = _retry_os(lambda: os.unlink(str(p)))
                if err:
                    rep.failed.append((e.path, f"削除できません: {err}"))
                    return
                rep.deleted.append(e.path)
            # 自分が作ったディレクトリだけ・空のときだけ片付ける（深い順）。
            for d in reversed(e.created_dirs):
                try:
                    os.rmdir(d)
                except OSError:
                    break  # 空でない＝他の物が入った。触らない。
            return

        # 元があった＝控えたバイト列に戻す。
        blob_path = self.root / (e.blob or "")
        try:
            data = blob_path.read_bytes()
        except OSError as ex:
            rep.failed.append((e.path, f"控えが読めません: {ex}"))
            return
        if now_exists:
            try:
                if p.read_bytes() == data:
                    rep.unchanged.append(e.path)
                    return
            except OSError:
                pass  # 読めなくても書き戻しは試す
        err = _atomic_write(p, data, e.mode)
        if err:
            rep.failed.append((e.path, err))
            return
        rep.restored.append(e.path)

    # ---- 合格の最高到達点を控える・戻す（ウォーターマーク）-------------------
    #
    # ★何のためか（2026-09-05・run50/52 の実測）: 検収で一度合格した行が、そのあとの
    #   編集で落ちる「合格→不合格の反転」が run50 で 6 件・run52 で 40 件あった。
    #   phases.py の `_regression_note` は「あとから壊れました」と**言う**が、戻す手は
    #   人間専用の `/rollback`（依頼開始時点＝合格より前まで戻ってしまう）しか無かった。
    #   ＝検知はある・是正が無い。ここが是正の側。
    #
    # ★形は opencode の snapshot（refs/opencode/.../snapshot/index.ts:318-346 `track` が
    #   毎回 `write-tree` で**木ごと**控え、:382-406 `restore` が `read-tree`＋
    #   `checkout-index -a -f` で戻す）と同じ「差分でなく丸ごと」。対象が record 済みの
    #   ファイルに限られるので件数は小さい（run50 実測: 7 試行で recorded 14＋created 15）。
    #   git を起こさない理由は本ファイル冒頭のとおり。
    # ★戻す対象は**2 層**: ウォーターマーク時点で追跡済みだった物はその時点の値へ、
    #   それ以降に初めて触った物は依頼開始時点の控え（＝ウォーターマーク時点でも同じ姿）へ。
    #   後者を忘れると「合格後に作った余計なファイル」が残る（run34 の残骸と同じ形）。

    @property
    def has_watermark(self) -> bool:
        return bool(self._watermark)

    # ---- 控えの共通部（ウォーターマーク／復元前の控え）----------------------------

    def _collect_targets(self, tree_root: str | Path | None, *, extra_keys=()) -> tuple[dict, str, list]:
        """控える対象を集める。戻り値 (key→path, scope, skipped)。

        scope="tree" は追跡済み ∪ 作業場の木（tree_scope_allowed が許し、上限内のとき）。
        木が上限を超えたら**部分的に控えない**（戻した意味が壊れる）＝追跡済みだけに落とす。
        `extra_keys` は「今は無いかもしれないが状態を覚えたい鍵」（復元前の控えで、
        ウォーターマークにある鍵＝restore が作り直しうる物）。
        """
        targets: dict[str, str] = {key: base.path for key, base in self._entries.items()}
        for k in extra_keys:
            targets.setdefault(k, self._key_path(k))
        skipped: list[Skipped] = []
        scope = "tracked"
        root = tree_scope_allowed(tree_root, store_root=self.root) if tree_root else ""
        if root:
            paths, complete = iter_tree_files(root, self.limits)
            if not complete:
                skipped.append(Skipped(path=root, reason="tree_too_big",
                                       detail=f"木のファイル数が上限 {self.limits.max_tree_files} を超えた"
                                              "ので木ごとの控えは諦め、追跡済みだけ控えた"))
            else:
                scope = "tree"
                for p in paths:
                    targets.setdefault(normalize_key(p), p)
        return targets, scope, skipped

    def _key_path(self, key: str) -> str:
        for d in (self._watermark, self._stash, self._entries):
            e = d.get(key)
            if e is not None:
                return e.path
        return key

    def _take_snapshot(self, prefix: str, targets: dict, previous: dict, *,
                       budget: int) -> tuple[dict, list, int]:
        """targets の**現在値**を blob に控える。戻り値 (entries, skipped, bytes)。

        `budget` は置き場に残っている容量。超えた物は store_full で控えない
        （呼び元が木の控えを諦めるかを決める）。**例外は投げない。**
        """
        new: dict[str, Entry] = {}
        skipped: list[Skipped] = []
        total = 0
        for key, path in targets.items():
            p = Path(path)
            try:
                exists = p.is_file()
            except OSError as ex:
                skipped.append(Skipped(path=path, reason="read_error", detail=str(ex),
                                       stat=_lstat_sig(path)))
                continue
            if not exists:
                new[key] = Entry(key=key, path=path, existed=False,
                                 created_dirs=_missing_ancestors(p))
                continue
            try:
                size = p.stat().st_size
            except OSError as ex:
                skipped.append(Skipped(path=path, reason="read_error", detail=str(ex),
                                       stat=_lstat_sig(path)))
                continue
            if size > self.limits.max_file_bytes:
                skipped.append(Skipped(path=path, reason="too_large",
                                       detail=f"{size} bytes > 上限 {self.limits.max_file_bytes}",
                                       stat=_lstat_sig(path)))
                continue
            if total + size > budget:
                # ★第4回c: 表示も実際の判定基準（他の層の占有 used ＋ この控え total）で出す。
                #   旧形は `stored_bytes + total` で、置き換える層の旧値を二重に数えていた。
                used = max(0, self.limits.max_total_bytes - budget)
                skipped.append(Skipped(path=path, reason="store_full",
                                       detail=f"控え合計 {used + total}+{size} > 上限 "
                                              f"{self.limits.max_total_bytes}",
                                       stat=_lstat_sig(path)))
                continue
            blob = _blob_name(prefix + key)
            # ★第4回c-6（再々反証 must_fix 4）: 読みと書きを分ける（`record` と同じ理由）。
            #   控え（watermark）の側も同じ嘘をついていた。
            try:
                data = p.read_bytes()
            except OSError as ex:
                skipped.append(Skipped(path=path, reason="read_error", detail=str(ex),
                                       stat=_lstat_sig(path)))
                continue
            try:
                self._ensure_root()
                (self.root / blob).write_bytes(data)
                mode = None
                try:
                    mode = p.stat().st_mode & 0o777
                except OSError:
                    pass
            except OSError as ex:
                skipped.append(Skipped(path=path, reason="store_error", detail=str(ex),
                                       stat=_lstat_sig(path)))
                continue
            new[key] = Entry(key=key, path=path, existed=True, blob=blob,
                             size=len(data), mode=mode)
            total += len(data)
        # 前回の控えのうち、今回 blob を持たなくなった物だけ消す（同名は上書き済み）。
        for old in previous.values():
            if old.blob and getattr(new.get(old.key), "blob", None) != old.blob:
                try:
                    (self.root / old.blob).unlink()
                except OSError:
                    pass
        return new, skipped, total

    def _drop_blobs(self, entries: dict) -> None:
        for e in entries.values():
            if e.blob:
                try:
                    (self.root / e.blob).unlink()
                except OSError:
                    pass

    # ---- ウォーターマーク -----------------------------------------------------

    def snapshot_watermark(self, tree_root: str | Path | None = None) -> int:
        """追跡している全ファイル（＋許されれば作業場の木）の**現在値**を控え直す。
        戻り値は控えた件数（closed は -1）。

        **例外は投げない**。上限は record と同じ `limits`。木が上限を超えたら木は諦めて
        追跡済みだけ控える（`watermark_scope` が "tracked" になる）。
        """
        if self.closed:
            return -1
        targets, scope, skipped = self._collect_targets(tree_root)
        budget = self.limits.max_total_bytes - (self.stored_bytes - self.watermark_bytes)
        new, sk, total = self._take_snapshot("wm:", targets, self._watermark, budget=max(0, budget))
        if scope == "tree" and any(s.reason == "store_full" for s in sk):
            # 木が容量に入らない＝部分的な木は作らない。追跡済みだけで取り直す。
            self._drop_blobs(new)
            skipped.append(Skipped(path=str(tree_root), reason="tree_store_full",
                                   detail="木の控えが容量上限に入らないので追跡済みだけ控えた"))
            targets, scope, _ = self._collect_targets(None)
            new, sk, total = self._take_snapshot("wm:", targets, {}, budget=max(0, budget))
        self._watermark = new
        # ★木ごとの控えなら、記号リンクの向き先も控える（上の iter_tree_links の注記）。
        _troot = tree_scope_allowed(tree_root, store_root=self.root) if scope == "tree" else ""
        self._wm_links = iter_tree_links(_troot, self.limits) if _troot else {}
        self._wm_skipped = skipped + sk
        self._wm_untracked_at = len(self.untracked)
        self.watermark_scope = scope
        self._wm_tree_root = tree_scope_allowed(tree_root, store_root=self.root) if scope == "tree" else ""
        self.watermark_bytes = total
        self.watermark_count += 1
        return len(new)

    @property
    def watermark_incomplete(self) -> tuple[str, ...]:
        """控え（watermark）に入らなかったファイルの理由（出た順・重複なし）。空なら覆えている。

        ★第4回c-6（再反証 M3・致命）: `mark_incomplete` の**控え側の対**。`record_watermark` は
          `too_large` 等で控えられなかった物があっても件数を返す（False は返さない）ので、
          呼び元が「控えられた＝木を覆っている」と読むと、`restore_watermark` が
          **印より前から在った物を「控え以降に増えた物」として extras に並べる**。
        """
        out: list[str] = []
        for sk in self._wm_skipped:
            if sk.reason in self.MARK_INCOMPLETE_REASONS and sk.reason not in out:
                out.append(sk.reason)
        return tuple(out)

    def _watermark_skipped_rows(self) -> "list[Skipped]":
        """控え（watermark）の報告に載せる「**控えに入っていない**実ファイル」の行。

        ★第4回c-7（再反証 must_fix 1・致命の偽り）: 旧形は `self.skipped`（＝**依頼開始時点**の
          `record()` の skip）を丸ごと足していた。record の skip は「そのとき控えられなかった」
          であって「いまの控え（watermark）に入っていない」ではない。実測 Q3b restored:
          9 MiB の `videos/big.txt` を作業者が Write で小さくした（record は too_large で飛ばす）
          → 次の `PhaseAcceptance` で**控えに入る**（`in watermark: True`）→ Bash で潰す →
          安全網が**実際に戻した**（`rep.restored` に載り、中身も控えと同じ）のに、同じ注記が
          「★控えに入らなかった物 1 件は**戻していません**: videos/big.txt」と言った。
          さらに record と控えの両方で同じ 1 本が飛ぶと 2 件に数えた（実測 Q3b dup:
          「控えに入らず戻していない 2」・一覧も `videos/big.txt, videos/big.txt`）。

        規則: **控えに入っている鍵は除く**（入っているなら戻せる）・**パスで重複を除く**・
        控えの側の行（`_wm_skipped`）を先に置いて record の古い行より優先する（指紋が新しい）。
        """
        seen: set[str] = set()
        out: list[Skipped] = []
        for sk in list(self._wm_skipped) + list(self.skipped):
            try:
                key = normalize_key(sk.path)
            except Exception:
                key = str(sk.path)
            if key in self._watermark or key in seen:
                continue
            seen.add(key)
            out.append(sk)
        return out

    def restore_watermark(self, *, keep_created: bool = False) -> "RollbackReport | None":
        """最後に控えたウォーターマークへ戻す。控えが無ければ None（何もしない）。

        `keep_created=True` は、控え以降に Write/Edit で**新しく作った**ファイルも消さない
        （工程末の安全網が使う: 反証で「正しい b.txt を消して 1/5 に落とした」を再現された。
        新しい成果物は残し、それでも合格が増えなければ呼び元が復元ごと取り消す）。
        ★第4回c: 作業者のボタン（RestoreLastPassing）も True で呼ぶ（説明文「控え以降に増えた
        ファイルは消さない」と挙動を一致させる・Write 由来だけ消して Bash 由来を残す非対称をやめる）。
        既定 False は unit と `rollback` 系の互換のため。本番の呼び元 2 つはどちらも True。

        報告の `untracked` は**ウォーターマーク以降**の Bash 等だけ。合格した時点より
        前の操作は合格状態の一部であって「戻せなかった物」ではない。
        ★木ごとの控えでも、控え以降に Bash 等で**増えた**ファイルは消さない（`extras` に
          列挙するだけ）。pid/生成物/取り込んだ資料など、出所の分からない物を消すと
          動いているサーバを止められなくなる（stop.sh が pid を失う）等の実害が出るため。
          追跡済み（Write/Edit/ApplyDiff で作った）物は従来どおり消す（run34 の残骸の形）。
        """
        if not self._watermark:
            return None
        rep = RollbackReport(skipped=self._watermark_skipped_rows(),
                             untracked=list(self.untracked[self._wm_untracked_at:]),
                             label="最後に合格数を更新した時点")
        for e in list(self._watermark.values()):
            self._restore_one(e, rep)
        # ウォーターマーク以降に初めて触った物＝その時点では依頼開始時の姿だった。
        for key, base in list(self._entries.items()):
            if key not in self._watermark:
                if keep_created and not base.existed:
                    if Path(base.path).is_file():
                        rep.extras.append(base.path)
                    continue
                self._restore_one(base, rep)
        # ★記号リンクを戻す（控えた向き先と違う／リンクでなくなった物だけ）。ディレクトリに化けた物は触らず、戻せなかったと言う。
        for lp, target in sorted(getattr(self, "_wm_links", {}).items()):
            try:
                if os.path.islink(lp) and os.readlink(lp) == target:
                    continue
                if os.path.isdir(lp) and not os.path.islink(lp):
                    rep.failed.append((lp, "記号リンクだった場所がディレクトリになっている（消さずに残した）"))
                    continue
                if os.path.lexists(lp):
                    os.remove(lp)
                os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
                os.symlink(target, lp)
                rep.restored.append(f"{lp} → {target}（記号リンク）")
            except OSError as e:
                rep.failed.append((lp, f"記号リンクを戻せなかった: {e}"))
        if self.watermark_scope == "tree" and self._wm_tree_root:
            paths, complete = iter_tree_files(self._wm_tree_root, self.limits)
            if complete:
                # ★第4回c-6（再反証 M3・致命）: 控えに**入らなかった**物（`too_large` 既定 8 MiB 超・
                #   `read_error` 等）は `_watermark` にも `_entries` にも無い。旧形はそれを
                #   「控え以降に増えたファイル」として `extras` に入れたので、**印より前から在る
                #   9 MiB の mp4** を「控え以降に増えた 1 件」と偽った（実測 p8_wm: 作業者が Bash で
                #   100 B に潰した mp4 を、安全網が「木ごと・復元 1 / 削除 0」と言って戻さないまま
                #   「増えたファイル」に数えた）。印の側（`revert_to_mark` の `known`）と同じ形にする。
                known = (set(self._watermark) | set(self._entries)
                         | {normalize_key(sk.path) for sk in self._wm_skipped})
                rep.extras += [p for p in paths if normalize_key(p) not in known]
        # ★第4回c-6（再反証 M3）: 控えに入らなかった物がある＝そこは戻していない。
        #   `mark_incomplete` と同じ形の符号を出し、読む側（注記）が「戻しました」と言い切らない。
        _winc = self.watermark_incomplete
        if _winc:
            rep.skipped.append(Skipped(
                path=self._wm_tree_root or "(控え)", reason="watermark_incomplete",
                detail="控えに入らなかった物がある（" + "・".join(
                    REASON_TEXT.get(r, r) for r in _winc) + "）ので、そこは戻していない"))
        return rep

    # ---- 復元前の控え（stash）---------------------------------------------------

    @property
    def has_stash(self) -> bool:
        return bool(self._stash)

    def stash(self, tree_root: str | Path | None = None) -> bool:
        """**戻す前に今を控える**（opencode session/revert.ts:70 の形）。成功なら True。

        対象＝追跡済み ∪ ウォーターマークの鍵（restore が作り直しうる物は「今は無い」も
        覚える）∪ 作業場の木（ウォーターマークが木なら）。容量に入らなければ False
        （＝取り消せない復元はしない、と呼び元が判断できる）。**例外は投げない。**

        ★第4回c-6（再反証 M4・致命・データ消失）: 旧形が False を返すのは `store_full`
          （置き場ごと足りない）のときだけで、**1 ファイルの `too_large` 等では True** を返した。
          その物を `restore_watermark` が書き換えると `unstash` は戻せない＝docstring の
          「取り消せない復元はしない」が偽。実測 p8c: 控え時点 5 B の a.txt を作業者が 9.5 MB に
          したところ、控えは a.txt を `too_large` で飛ばしたまま True を返し、復元が a.txt を 5 B へ
          戻し、増えなかったので「復元は取り消し、手番の終わりの状態に戻しました」と言って
          **作業者の 9.5 MB が黙って消えた**（undo_failed=0）。
          いまは「控えられなかった物のうち、**復元が触る物**（＝ウォーターマークか追跡済みの鍵）」
          が 1 つでもあれば False。木の中にあるだけで復元が触らない物（例: 追跡していない
          9 MiB の mp4）は従来どおり控えの対象外のまま進める（安全網を無用に止めない）。
          断った事実は `stash_refused` に残す（黙って何もしないのが一番危ない）。
        """
        self._stash_refused = ()
        if self.closed:
            return False
        try:
            root = self._wm_tree_root if self.watermark_scope == "tree" else None
            targets, scope, skipped = self._collect_targets(root, extra_keys=list(self._watermark))
            if root and scope != "tree":
                return False                        # 木の範囲が揃わない＝取り消せない
            budget = self.limits.max_total_bytes - (self.stored_bytes - self.stash_bytes)
            new, sk, total = self._take_snapshot("st:", targets, self._stash, budget=max(0, budget))
            if any(s.reason == "store_full" for s in sk):
                self._drop_blobs(new)
                self._stash = {}
                self._stash_skipped = []
                self.stash_bytes = 0
                return False
            # ★復元（restore_watermark）が触る鍵＝ウォーターマーク ∪ 追跡済み。
            touched = set(self._watermark) | set(self._entries)
            refused = [s for s in (skipped + sk)
                       if s.reason in self.MARK_INCOMPLETE_REASONS
                       and normalize_key(s.path) in touched]
            if refused:
                self._drop_blobs(new)
                self._stash = {}
                self._stash_skipped = []
                self.stash_bytes = 0
                self._stash_refused = tuple(refused)
                return False
            self._stash = new
            self._stash_skipped = skipped + sk
            self.stash_bytes = total
            return True
        except Exception:
            return False

    @property
    def stash_refused(self) -> tuple:
        """直前の `stash()` が False を返した理由のうち、**控えられなかった実ファイル**の一覧。

        空タプルなら「容量ごと足りない／木が揃わない／控えを試していない」。呼び元は
        これを見て「安全網を見送った」を数え、黙って何もしなかったことに気づけるようにする。
        """
        return getattr(self, "_stash_refused", ())

    def unstash(self) -> "RollbackReport | None":
        """復元前の控えへ戻す（復元の取り消し）。控えが無ければ None。控えは使ったら捨てる。"""
        if not self._stash:
            return None
        rep = RollbackReport(skipped=list(self._stash_skipped), label="復元前の時点")
        for e in list(self._stash.values()):
            self._restore_one(e, rep)
        self.drop_stash()
        return rep

    def drop_stash(self) -> None:
        self._drop_blobs(self._stash)
        self._stash = {}
        self._stash_skipped = []
        self.stash_bytes = 0

    # ---- 印（mark）: 副作用のある操作の前に置き、後で「変えた物は戻し・増えた物は消す」 ----

    @property
    def has_mark(self) -> bool:
        return self._mark_placed

    #: 印（mark）に**入らなかった**ファイルの理由（`_take_snapshot` の 1 ファイル単位の skip）。
    #: これがあると印は対象を覆っていない＝その物は「戻せない」し「増えた物」でもない。
    MARK_INCOMPLETE_REASONS = ("too_large", "read_error", "store_error", "store_full", "not_a_file")

    @property
    def mark_incomplete(self) -> tuple[str, ...]:
        """印に入らなかったファイルの理由（出た順・重複なし）。空なら印は対象を覆っている。

        ★第4回c-5（再反証 must_fix 1・致命）: `mark()` は `too_large` 等で控えられなかった物が
          あっても **True** を返す（False は `store_full` で印ごと捨てるときだけ）。呼び元が
          「印を置けた＝木を覆っている」と読むと、`revert_to_mark` が**印より前から在った物を
          増えた物として消す**。置けた事実（`has_mark`）と覆えた事実（ここ）は別に読む。
        """
        out: list[str] = []
        for sk in self._mark_skipped:
            if sk.reason in self.MARK_INCOMPLETE_REASONS and sk.reason not in out:
                out.append(sk.reason)
        return tuple(out)

    def mark(self, tree_root: str | Path | None = None) -> bool:
        """**副作用のある操作（検収）を走らせる直前の木**を控える。成功なら True。

        対象＝追跡済み ∪ ウォーターマークの鍵 ∪ 復元前の控えの鍵 ∪ 木（`tree_root`、無ければ
        ウォーターマークの木）。木が揃わない・容量に入らないなら False（＝増えた物を消せない、
        と呼び元が判断できる）。stash と別の枠を持つのは、stash が「復元前」を守っている最中に
        置くから（同じ枠だと取り消しの控えを潰す）。**例外は投げない。**
        ★第4回c-3: 木ごとの印は**除外規則の場所**（ドットのディレクトリ・.lock 等）の一覧も控える
        （`iter_excluded_files`）。戻すためではなく、`revert_to_mark` で「そこに増えた/変わった物」を
        数えて言うため（戻せない物を戻したと言わない）。
        ★第4回c-4（再反証 M2）: **戻り値と状態を一致させる**。True ⇔ `has_mark`（印を置いた）。
          木に普通のファイルが 0 件でも True（`_mark == {}` だが、増えた物を消す印としては要る）。
          False を返すときは前の印ごと捨てる（`drop_mark`）＝「置けなかったのに残っている」を作らない。
        ★第4回c-5（再反証 must_fix 1）: **True は「木を覆えた」を意味しない。** `too_large`
          （既定 8 MiB 超）・`read_error`・`store_error` で控えられなかった物があっても True。
          覆えたかは `mark_incomplete`（空なら覆えた）で読む。`revert_to_mark` は入らなかった物を
          **消さない**（増えた物ではないので）が、戻すこともできない＝報告で「戻した」と言わない。
        """
        if self.closed:
            return False
        try:
            root = tree_root if tree_root else (
                self._wm_tree_root if self.watermark_scope == "tree" else None)
            extra = list(self._watermark) + list(self._stash)
            targets, scope, skipped = self._collect_targets(root, extra_keys=extra)
            if root and scope != "tree":
                self.drop_mark()
                return False                        # 木の範囲が揃わない＝増えた物を消せない
            budget = self.limits.max_total_bytes - (self.stored_bytes - self.mark_bytes)
            new, sk, total = self._take_snapshot("mk:", targets, self._mark, budget=max(0, budget))
            if any(s.reason == "store_full" for s in sk):
                self._drop_blobs(new)
                self.drop_mark()
                return False
            self._mark = new
            self._mark_skipped = skipped + sk
            self.mark_bytes = total
            self.mark_scope = scope
            self._mark_tree_root = (tree_scope_allowed(root, store_root=self.root)
                                    if (root and scope == "tree") else "")
            # ★第4回c-3: 除外規則の場所も**数える**（戻すためではない・戻せなかった物を言うため）。
            self._mark_excluded, self._mark_excluded_complete = (
                iter_excluded_files(self._mark_tree_root, self.limits)
                if self._mark_tree_root else (None, False))
            self._mark_placed = True
            return True
        except Exception:
            self.drop_mark()
            return False

    def revert_to_mark(self, *, prune_new: bool = True) -> "RollbackReport | None":
        """印の時点へ戻す: **変えた物は戻し、消えた物は作り直し、増えた物は消す**（木の範囲内）。
        印が無ければ None。印は使ったら捨てる。

        `prune_new` は木ごとの印のときだけ効く（追跡済みだけの印では「増えた物」を知る術が無い
        ＝消さない・`skipped` に理由を出す）。除外規則（pid/log/-wal 等・ドットのディレクトリ）の
        外にある物は列挙されないので触らない——**が、そこに増えた/変わった/消えた物は数えて
        `unknown` に出す**（第4回c-3・反証 must_fix 1: 実物の検収が `.acc-tmp/` に書く物が
        この盲点に落ち、「ファイルに残した効果は戻した」が偽になっていた）。数えられなければ
        `unknown_counted=False`（0 と言わない）。SQLite の WAL/ホットジャーナルが併存する DB は
        `_restore_one` が `failed` に出す（戻したふりをしない）。
        """
        if not self._mark_placed:                   # ★`bool(self._mark)` ではない（空の木でも印は置ける）
            return None
        rep = RollbackReport(skipped=list(self._mark_skipped), label="検収を走らせる直前")
        for e in list(self._mark.values()):
            self._restore_one(e, rep)
        if prune_new and self.mark_scope == "tree" and self._mark_tree_root:
            paths, complete = iter_tree_files(self._mark_tree_root, self.limits)
            if not complete:
                rep.skipped.append(Skipped(path=self._mark_tree_root, reason="tree_too_big",
                                           detail="木のファイル数が上限を超えたので、増えた物は消していない"))
            else:
                # ★第4回c-5（再反証 must_fix 1・致命）: 印に**入らなかった**物（too_large 等）は
                #   `_mark` に無い。旧形はそれを「印より後に増えた物」と見なして os.unlink した
                #   ＝**印より前から在った 9 MiB の mp4 を消した**（本番 run_phases で実測）。
                #   消してよいのは印より後に増えた物だけ。控えられなかった物は**触らない**。
                known = set(self._mark) | {normalize_key(sk.path) for sk in self._mark_skipped}
                for p in paths:
                    if normalize_key(p) in known:
                        continue
                    err = _retry_os(lambda p=p: os.unlink(p))
                    if err:
                        rep.failed.append((p, f"削除できません: {err}"))
                    else:
                        rep.deleted.append(p)
        elif prune_new and self.mark_scope != "tree":
            # ★第4回c-5（再反証 must_fix 5）: 旧形はここで `tree_too_big`（木が上限を超えた）を
            #   「木ごとの印ではない」の意味に**再利用**していた＝別の事実に同じ符号。別コードにする。
            rep.skipped.append(Skipped(path=self._mark_tree_root or "(追跡済みだけ)", reason="not_tree",
                                       detail="木ごとの印ではないので、増えた物は消していない"))
        # ★第4回c-5（再反証 must_fix 1）: 印に入らなかった物がある＝その物は戻していないし
        #   （増えた物ではないので）消してもいない。読む側が「戻した」と言わないための符号。
        _inc = self.mark_incomplete
        if _inc:
            rep.skipped.append(Skipped(
                path=self._mark_tree_root or "(印)", reason="mark_incomplete",
                detail="印に入らなかった物がある（" + "・".join(
                    REASON_TEXT.get(r, r) for r in _inc) + "）ので、そこは戻していない"))
        # ★第4回c-3: 除外規則の場所の突き合わせ（増えた・変わった・消えた）。戻さない・消さない。
        rep.unknown_counted = False
        if self.mark_scope == "tree" and self._mark_tree_root and self._mark_excluded is not None:
            before, ok_before = self._mark_excluded, self._mark_excluded_complete
            try:
                now, ok_now = iter_excluded_files(self._mark_tree_root, self.limits)
            except Exception:
                now, ok_now = {}, False
            if ok_before and ok_now:
                rep.unknown_counted = True
                added = [p for p in now if p not in before]
                changed = [p for p in now if p in before and before[p] != now[p]]
                gone = [p for p in before if p not in now]
                rep.unknown = sorted(added) + sorted(changed) + [f"(消えた) {p}" for p in sorted(gone)]
            else:
                rep.skipped.append(Skipped(path=self._mark_tree_root, reason="excluded_too_big",
                                           detail="除外規則の場所（ドットのディレクトリ等）のファイル数が"
                                                  f"上限 {self.limits.max_excluded_files} を超えたので、"
                                                  "そこに残った物は数えていない"))
        self.drop_mark()
        return rep

    def drop_mark(self) -> None:
        self._drop_blobs(self._mark)
        self._mark = {}
        self._mark_skipped = []
        self.mark_bytes = 0
        self.mark_scope = ""
        self._mark_tree_root = ""
        self._mark_excluded = None
        self._mark_excluded_complete = True
        self._mark_placed = False

    # ---- 片付け -------------------------------------------------------

    def close(self) -> None:
        """控えを捨てる。以後 record も rollback もしない。

        ★`rmtree` は使わない。自分が書いた `.blob` だけを消し、**空になったとき
        だけ**ディレクトリを外す。置き場所を間違えて指定された場合（利用者が
        `LAPS_CHECKPOINT_DIR=~/Documents` と書いた等）に、掃除のつもりで
        他人のファイルを消さないため。
        """
        self.closed = True
        entries = (list(self._entries.values()) + list(self._watermark.values())
                   + list(self._stash.values()) + list(self._mark.values()))
        self._entries.clear()
        self._watermark.clear()
        self._stash.clear()
        self._mark.clear()
        self._mark_placed = False
        self.mark_scope = ""
        self._mark_tree_root = ""
        self._mark_excluded = None
        self._mark_excluded_complete = True
        self.total_bytes = self.watermark_bytes = self.stash_bytes = self.mark_bytes = 0
        for e in entries:
            if e.blob:
                try:
                    (self.root / e.blob).unlink()
                except OSError:
                    pass
        try:
            self.root.rmdir()
        except OSError:
            pass  # 空でない＝自分以外の物がある。触らない。
        self._root_made = False


def _retry_os(fn, tries: int = 3):
    """Windows のロック（ウイルス対策・エディタの掴み）で一瞬失敗するのに備える。

    戻り値は None（成功）か、最後の例外の文字列。
    """
    last = None
    for i in range(tries):
        try:
            fn()
            return None
        except OSError as e:
            last = e
            if i + 1 < tries:
                time.sleep(0.05 * (i + 1))
    return str(last)


#: SQLite のホットジャーナルの魔法数（先頭 8 バイト）。PERSIST モードで残る「冷えた」journal は
#: 先頭が 0 で埋まるので、これで生きている journal だけを見分ける。
_SQLITE_HOT_JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
#: WAL ヘッダの長さ。これ以下なら frame が無い＝再適用される物が無い。
_SQLITE_WAL_HEADER = 32


def _live_sqlite_sidecar(p: Path) -> str:
    """`p` に SQLite の WAL／ホットジャーナルが**中身つきで**併存していればその名前を返す。

    -wal: ヘッダ（32 バイト）より大きければ frame があり、次の open で本体へ再適用されうる。
    -journal: 先頭 8 バイトが魔法数ならホット（次の open でロールバックされる）。
    -shm は索引だけ（wal が無ければ作り直される）なので見ない。読めなければ「無い」扱い
    （この判定で restore を落とさない）。
    """
    try:
        wal = p.with_name(p.name + "-wal")
        if wal.is_file() and wal.stat().st_size > _SQLITE_WAL_HEADER:
            return wal.name
        jnl = p.with_name(p.name + "-journal")
        if jnl.is_file() and jnl.stat().st_size >= len(_SQLITE_HOT_JOURNAL_MAGIC):
            with open(jnl, "rb") as fh:
                if fh.read(len(_SQLITE_HOT_JOURNAL_MAGIC)) == _SQLITE_HOT_JOURNAL_MAGIC:
                    return jnl.name
    except OSError:
        pass
    return ""


def _atomic_write(p: Path, data: bytes, mode: int | None) -> str | None:
    """同じディレクトリに書いてから差し替える。戻り値は None（成功）かエラー文。

    途中で落ちても半端なファイルを残さない、は Edit 側と同じ作法。
    """
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"親ディレクトリを作れません: {e}"
    try:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".bw_rb_")
    except OSError as e:
        return f"一時ファイルを作れません: {e}"
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass  # Windows では意味が薄い。落とすほどのことではない。
        err = _retry_os(lambda: os.replace(tmp, str(p)))
        if err:
            return f"差し替えられません: {err}"
        return None
    except OSError as e:
        return f"書けません: {e}"
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


# ---- 置き場所と入り口 --------------------------------------------------

def _inside(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.realpath(child)).relative_to(Path(os.path.realpath(parent)))
        return True
    except (ValueError, OSError):
        return False


def store_root() -> Path:
    """控えの置き場所。**作業ディレクトリの外**に置く。

    中に置くと、エージェント自身の Glob/Grep が控えを拾って読みに行き、
    「作業結果」に控えが混ざる。この罠はこのプロジェクトで前にも踏んでいる
    （deterministic_prefetch がベンチの期待ファイルを先に書いていた）。

    既定は `~/.blackwall/checkpoints`。ただし **LAPS_HOME を作業場所の
    中に向けるベンチが実在する**（session.py の注記どおり）ので、そうなった
    ときは黙って作業場所を汚さず、一時ディレクトリへ逃がす。
    """
    override = os.environ.get("LAPS_CHECKPOINT_DIR")
    if override:
        return Path(override)  # 明示指定は利用者の判断。尊重する。
    from .session import state_dir
    root = state_dir() / "checkpoints"
    try:
        cwd = Path.cwd()
    except OSError:
        return root
    if _inside(root, cwd):
        # 作業場所の中に来てしまった。ここに置くと測定と作業が汚れる。
        safe = Path(tempfile.gettempdir()) / "blackwall_checkpoints"
        try:
            safe.mkdir(parents=True, exist_ok=True)
            return safe
        except OSError:
            return root  # 逃がせないなら元の場所（動かないよりはまし）
    return root


def enabled(env: dict | None = None) -> bool:
    """控えを取るか。既定 ON（控えていなければ戻せない）。0 で止められる。"""
    e = os.environ if env is None else env
    return str(e.get("LAPS_CHECKPOINT", "1")) != "0"


def prune_store(base: Path, *, max_age_hours: float = 48.0) -> int:
    """殺された実行が置いていった控えを片付ける。消した数を返す。

    close() は正常終了の道だけを通る。SIGKILL / 電源断では控えが残り、
    1回あたり最大 64MiB が永久に積む。ここで古いものだけ落とす。
    ★安全側: **`.blob` しか入っていないディレクトリ**だけが対象。利用者が
    置き場所を間違えて指定していても、他人のファイルには触らない。
    """
    removed = 0
    cutoff = time.time() - max_age_hours * 3600
    try:
        dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return 0
    for d in dirs:
        try:
            if d.stat().st_mtime > cutoff:
                continue
            kids = list(d.iterdir())
            if any(not k.is_file() or k.suffix != ".blob" for k in kids):
                continue  # 見知らぬ中身。触らない。
            for k in kids:
                k.unlink()
            d.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def begin(task_id: str = "", *, root: str | Path | None = None) -> Checkpoint:
    """依頼1件ぶんのチェックポイントを開く。"""
    base = Path(root) if root is not None else store_root()
    try:
        if base.exists():
            prune_store(base, max_age_hours=_env_int(
                os.environ, "LAPS_CHECKPOINT_KEEP_HOURS", 48))
    except Exception:  # noqa: BLE001 — 掃除の失敗で依頼を止めない
        pass
    cp = Checkpoint(base / (task_id or "cp"), task_id=task_id,
                    limits=Limits.from_env())
    return cp


# ---- どのツールが、どのファイルを触るか ---------------------------------
#
# ★ここが「書いたが繋いでいない」を防ぐ要。ツールを足したらこの表にも足す。
# 表に無いツール＝控えの対象外＝戻せない、を `note_untracked` で必ず記録する。

def paths_for_tool(name: str, inp: dict) -> list[str]:
    """そのツール呼び出しが書き換えるファイルのパス。読み取り専用なら空。

    ツール側の実装をそのまま使う（パスの解釈を2箇所に持つと必ずずれる）:
    - Write は SFT 由来の偽パスを書き換えてから書く → 同じ関数を通す
    - ApplyDiff は diff 本文から複数のパスを取り出す → 同じ parser を使う
    """
    if not isinstance(inp, dict):
        return []
    if name == "Write":
        raw = inp.get("file_path") or inp.get("path")
        if not raw:
            return []
        try:
            from .tools.write import _sanitize_path
            eff, _ = _sanitize_path(str(raw))
            return [eff]
        except Exception:
            return [str(raw)]
    if name in ("Edit",):
        raw = inp.get("file_path") or inp.get("path")
        return [str(raw)] if raw else []
    if name == "Doc":     # 資料作成（oc/doctool.py）。読むだけのときは何も変えない
        if str(inp.get("action") or "create").lower() == "read":
            return []
        raw = inp.get("path") or inp.get("file_path")
        return [str(raw)] if raw else []
    if name == "NotebookEdit":
        raw = inp.get("notebook_path") or inp.get("file_path")
        return [str(raw)] if raw else []
    if name == "ApplyDiff":
        diff = inp.get("diff", "")
        if not diff:
            return []
        try:
            from .tools.apply_diff import _parse_blocks
            return [b["path"] for b in _parse_blocks(str(diff)) if b.get("path")]
        except Exception:
            return []
    return []


# 控えの対象外だが、ファイルを変えうるツール。報告に「戻せない」と出すため。
#
# Bash: 何を触るか事前に分からない（`sed -i` も `cat >` も通る）。
# Task: 子エージェントは**自分の Agent と自分の控え**を持つ。親の控えには
#   子の編集が入らないので、親が「全部戻した」と言うと嘘になる。
#   ★本来は親子で控えを共有すべき（そうすれば実際に戻せる）。いまは
#   ツールの handler から親の Agent に届く経路が無く、共有するには
#   モジュール大域か引数の追加が要る。**嘘をつくより、戻せないと言う**方を採った。
#: ★Verify は Bash と同じ経路でコマンドを走らせる（2026-09-21 追加）。
#:   任意のコマンドなので、何が変わるかを事前に知る術が無い＝控えを取れない。
#: ★Desktop（2026-10-05）は画面のアプリを操作する。保存ボタンを押せばファイルは変わるが、
#:   どれが変わるかは事前に分からない。
UNTRACKABLE_TOOLS = frozenset({"Bash", "Task", "Verify", "Desktop"})

# ファイルは書くが、書き先が `~/.blackwall/sessions/<id>/` の中だけ＝
# 利用者の作業物ではない（進捗メモ・タスク一覧）。戻す対象にしない。
HARNESS_ONLY_TOOLS = frozenset({
    "TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskStop",
})

# 利用者のファイルを書き換えるツール＝控えの対象（`paths_for_tool` が実装）。
TRACKED_TOOLS = frozenset({"Write", "Edit", "NotebookEdit", "ApplyDiff"})
