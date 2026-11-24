"""作業ログ — 作業フォルダの .blackwall/WORKLOG.md（2026-10-06 オーナー「ワークスペース内に専用の作業ログ保管の
MD ファイルをエージェント側で作成させて、次回起動時に前回の作業内容と環境情報をすぐ理解できるような導線に」
「作業ログは Blackwall の仕組みの話です」）。

★会話は会話ごとに独立していて、新しい会話は前の会話を知らない。実機の走行記録では、新しい会話の最初の依頼は
  ほぼ毎回「このフォルダには何がある？」から始まり、2〜6 手番を探索に使っていた。作業ログはその代わりになる。

書く（中核が走行の終わりに・推論機は呼ばない＝時間を足さない）:
  ・環境 … 作業フォルダ・OS とシェル・動くインタプリタ・フォルダの主な中身（毎回書き直す）
  ・メモ … モデルと利用者の覚え書き（道具の置き場・動かし方など）。★中核は書き換えない（そのまま残す）
  ・作業の記録 … 依頼・結果・書き換えたファイル・確認の結果・割り込み・答えの冒頭（新しい順・最大 30 件）
  道具を 1 回も使わなかった会話（ただの質問）は書かない（記録が雑談で埋まる）。

読む（新しい会話の最初の依頼にだけ・依頼の発言の写しに付ける＝前置きは変えない）:
  メモと直近 3 件を <worklog> として添え、全文が要るときはファイルを読むよう書く。

★2026-10-12 オーナー「ワークスペース内のディレクトリ構造やどんなファイルがあるかなどを作業時に毎回更新して、作業開始時に
  読み込むようにした方が」「ツール実行時の一番最後に毎回差分記録させるとか」:
  ・作業フォルダの変化（snapshot / diff）… 道具を使うたびに作った・変えた・消したファイルを数え（推論機は呼ばない）、
    その道具の結果の末尾に 1 行添え、作業ログの記録（「作業フォルダの変化」）を書き直す。Bash で動かしたスクリプトが
    作ったファイルもここで分かる（それまでは Write・Edit で書いた物しか記録に無かった）。
  ・フォルダの構造（structure）… ファイルのあるフォルダごとの本数と多い拡張子を「環境」に書く（上 1 段の名前だけだった）。
  ・読む時機 … 新しい会話の最初だけでなく、走行の始めと要約の後にも添える。前置きは変えない（変えると P40 が会話を頭から
    読み直す）＝依頼の発言・要約の後の続きの発言の写しに付ける。全ファイルの一覧は渡さない。
LAPS_WORKLOG=0 で書くのも読むのも止める。
"""
from __future__ import annotations

from .. import paths as _lp
import os
import re
import time
from pathlib import Path

NAME = "WORKLOG.md"
MAX_ENTRIES = 30
_CTX_ENTRIES = 3
_CTX_NOTES_CHARS = 1500
_CTX_ENTRIES_CHARS = 1800
_TOP_MAX = 30
_SKIP = {*_lp.WS_NAMES, ".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vs"}
SNAP_MAX = 50_000                   # これより多い作業フォルダは変化を数えない（その走行は道具ごとの差分を止める）
SNAP_SECS = 2.0                     # 1 回の数えにかけてよい時間
_STRUCT_DIRS = 25                   # 構造に載せるフォルダの数
_STRUCT_CHARS = 1800

H_ENV = "## 環境"
H_NOTES = "## メモ"
H_LOG = "## 作業の記録"
NOTES_HINT = "（Blackwall と利用者の覚え書き。道具の置き場・動かし方・決めたことなど、次の会話でも要ることを書く）"

_OUTCOME = {"accepted": "完了", "cancelled": "中断", "max_iters": "手番の上限で終了",
            "budget_exhausted": "時間の上限で終了", "context_exhausted": "文脈切れで終了",
            "backend_error": "推論機の誤りで終了", "declined": "モデルが断って終了"}


def enabled() -> bool:
    if os.environ.get("LAPS_WORKLOG", "1").strip() == "0":
        return False
    return os.environ.get("LAPS_TOOLSET", "").strip().lower() != "chat"


def path(cwd: str) -> Path:
    return _lp.workspace_dir(cwd) / NAME


def _one_line(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def _sections(text: str) -> dict[str, str]:
    """`## ` 見出しごとの本文（見出しの行を含まない）。"""
    out: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur is not None:
                out[cur] = "\n".join(buf).strip("\n")
            cur = line.strip()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip("\n")
    return out


def _find(secs: dict[str, str], head: str) -> str:
    for k, v in secs.items():
        if k.startswith(head):
            return v
    return ""


def _entries(body: str) -> list[str]:
    parts = re.split(r"(?m)^(?=### )", body or "")
    return [p.strip("\n") for p in parts if p.strip().startswith("### ")]


def _top_level(cwd: str) -> str:
    try:
        items = sorted(os.scandir(cwd), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    except OSError:
        return ""
    names = []
    for e in items:
        if e.name in _SKIP or e.name.startswith("."):
            continue
        names.append(e.name + ("/" if e.is_dir(follow_symlinks=False) else ""))
    more = f" ほか {len(names) - _TOP_MAX} 件" if len(names) > _TOP_MAX else ""
    return ", ".join(names[:_TOP_MAX]) + more


def snapshot(cwd: str) -> dict[str, tuple[int, int]] | None:
    """作業フォルダのファイルごとの (大きさ, 更新時刻 ns)。多すぎる・遅すぎるときは None。"""
    out: dict[str, tuple[int, int]] = {}
    t0 = time.monotonic()
    stack = [""]
    while stack:
        rel = stack.pop()
        try:
            it = os.scandir(os.path.join(cwd, rel) if rel else cwd)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name in _SKIP:
                    continue
                r = os.path.join(rel, e.name) if rel else e.name
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(r)
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                out[r] = (st.st_size, st.st_mtime_ns)
                if len(out) > SNAP_MAX or time.monotonic() - t0 > SNAP_SECS:
                    return None
    return out


def diff(before: dict, after: dict) -> tuple[list[str], list[str], list[str]]:
    """(作った, 変えた, 消した)。"""
    created = sorted(k for k in after if k not in before)
    deleted = sorted(k for k in before if k not in after)
    modified = sorted(k for k, v in after.items() if k in before and before[k] != v)
    return created, modified, deleted


def _describe(paths: list[str], n: int = 6) -> str:
    """少なければ名前を並べ、多ければフォルダごとの本数と多い拡張子にまとめる。"""
    if len(paths) <= n:
        return ", ".join(paths)
    groups: dict[str, list[str]] = {}
    for p in paths:
        groups.setdefault(os.path.dirname(p) or ".", []).append(p)
    parts = []
    for d, ps in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:4]:
        exts: dict[str, int] = {}
        for x in ps:
            k = os.path.splitext(x)[1].lower() or "(no ext)"
            exts[k] = exts.get(k, 0) + 1
        top = " ".join(f"{k}×{v}" for k, v in sorted(exts.items(), key=lambda kv: -kv[1])[:3])
        parts.append(f"{d}{os.sep} {len(ps)} ({top})")
    if len(groups) > 4:
        parts.append(f"+{len(groups) - 4} more folders")
    return "; ".join(parts)


def change_note(created: list[str], modified: list[str], deleted: list[str]) -> str:
    """道具の結果の末尾に添える 1 行（変化が無ければ ""）。"""
    bits = []
    if created:
        bits.append(f"created {len(created)}: {_describe(created)}")
    if modified:
        bits.append(f"modified {len(modified)}: {_describe(modified)}")
    if deleted:
        bits.append(f"deleted {len(deleted)}: {_describe(deleted)}")
    return ("[Work folder changes from this step — " + " | ".join(bits) + "]") if bits else ""


def change_line(changes: dict | None) -> str:
    """作業ログの記録に書く「作業フォルダの変化」（走行の始めからの累計）。"""
    if not changes:
        return ""
    bits = []
    for key, label in (("created", "作成"), ("modified", "変更"), ("deleted", "削除")):
        ps = sorted(changes.get(key) or ())
        if ps:
            bits.append(f"{label} {len(ps)}（{_describe(ps, 4)}）")
    return "・".join(bits)


def structure(cwd: str, snap: dict | None = None) -> str:
    """ファイルのあるフォルダごとの本数と多い拡張子（本数の多い順・_STRUCT_DIRS まで）。数えられなければ ""。"""
    if snap is None:
        snap = snapshot(cwd)
    if not snap:
        return ""
    dirs: dict[str, dict[str, int]] = {}
    for rel in snap:
        d = os.path.dirname(rel) or "."
        ext = os.path.splitext(rel)[1].lower() or "(no ext)"
        e = dirs.setdefault(d, {})
        e[ext] = e.get(ext, 0) + 1
    rows = sorted(dirs.items(), key=lambda kv: -sum(kv[1].values()))
    lines = [f"  （ファイル {len(snap):,} 本・ファイルのあるフォルダ {len(dirs):,}）"]
    for d, exts in rows[:_STRUCT_DIRS]:
        top = "・".join(f"{k} {v:,}" for k, v in sorted(exts.items(), key=lambda kv: -kv[1])[:3])
        name = "（直下）" if d == "." else d + os.sep
        lines.append(f"  - {name} {sum(exts.values()):,} 本（{top}）")
    if len(rows) > _STRUCT_DIRS:
        lines.append(f"  - ほか {len(rows) - _STRUCT_DIRS:,} フォルダ")
    out = "\n".join(lines)
    return out if len(out) <= _STRUCT_CHARS else out[:_STRUCT_CHARS] + "…"


def _env_block(cwd: str, struct: str | None = None) -> str:
    lines = [f"- 作業フォルダ: {cwd}"]
    try:
        from . import hostfacts
        ok, broken = hostfacts.interpreters()
        lines.append(f"- OS: {hostfacts.os_version()} ／ シェル: {hostfacts.shell_name()}")
        lines.append(f"- 動くインタプリタ・道具: {', '.join(ok) or '（見つからない）'}")
        if broken:
            lines.append(f"- PATH にあるが動かない: {', '.join(broken)}")
        tb = hostfacts.toolbox()
        if tb:
            lines.append(f"- その他のコマンド: {', '.join(tb)}")
    except Exception:
        pass
    top = _top_level(cwd)
    if top:
        lines.append(f"- フォルダの主な中身: {top}")
    st = structure(cwd) if struct is None else struct
    if st:
        lines.append("- ファイルのあるフォルダ（本数の多い順）:\n" + st)
    return "\n".join(lines)


def entry(*, request: str, outcome: str | None, steps: int, secs: float, files: list[str],
          verdict: dict | None, interjections: list[str], answer: str, tools: dict[str, int],
          run_id: str = "", in_progress: bool = False, changes: dict | None = None) -> str:
    try:
        from .base import _user_part          # 念のため包み（[アプリからの指示]…[利用者]）を外す
        request = _user_part(request)
    except Exception:
        pass
    when = time.strftime("%Y-%m-%d %H:%M")
    mins = f"{secs / 60:.0f} 分" if secs >= 90 else f"{secs:.0f} 秒"
    label = "作業中（途中の記録・止められた可能性もある）" if in_progress else _OUTCOME.get(outcome or "", outcome or "不明")
    head = f"### {when} ・ {label} ・ {steps} 手番 ・ {mins}"
    out = [head, f"- 依頼: 「{_one_line(request, 200)}」"]
    if interjections:
        out.append("- 割り込み: " + " / ".join(f"「{_one_line(x, 80)}」" for x in interjections[-5:]))
    if files:
        names = []
        for f in files:
            try:
                names.append(os.path.relpath(f))
            except ValueError:
                names.append(f)
        more = f" ほか {len(names) - 8} 件" if len(names) > 8 else ""
        out.append(f"- 書き換えたファイル: {', '.join(names[:8])}{more}")
    ch = change_line(changes)
    if ch:
        out.append(f"- 作業フォルダの変化（道具ごとに数えた累計）: {ch}")
    st = (verdict or {}).get("status")
    if st == "verified":
        out.append(f"- 確認: ✓ 通過（`{verdict.get('cmd')}`）")
    elif st == "failed":
        code = f" → exit {verdict['exit']}" if verdict.get("exit") is not None else ""
        out.append(f"- 確認: ✗ 失敗（`{verdict.get('cmd')}`{code}）")
    elif st == "unverified":
        out.append("- 確認: ⚠ 書き換えのあと未確認")
    if tools:
        out.append("- 使った道具: " + ", ".join(f"{k}×{v}" for k, v in sorted(tools.items(), key=lambda kv: -kv[1])[:8]))
    if answer.strip():
        out.append(f"- {'直近の発言' if in_progress else '答えの冒頭'}: {_one_line(answer, 300)}")
    if run_id:
        out.append(f"<!-- run:{run_id} -->")       # 同じ走行の途中の記録を、次の書き込みで置き換えるための印
    return "\n".join(out)


def record(cwd: str, new_entry: str, run_id: str = "", struct: str | None = None) -> bool:
    """作業ログを書き直す（環境は最新に・メモはそのまま・記録は新しい順に最大 30 件）。原子的に置き換える。"""
    p = path(cwd)
    try:
        old = p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        old = ""
    secs = _sections(old)
    notes = _find(secs, H_NOTES).strip()
    if not notes or notes == NOTES_HINT:
        notes = NOTES_HINT
    old_entries = _entries(_find(secs, H_LOG))
    if run_id:                                    # 同じ走行の途中の記録は置き換える（積み上げない）
        old_entries = [e for e in old_entries if f"<!-- run:{run_id} -->" not in e]
    entries = [new_entry] + old_entries
    body = "\n".join([
        "# Blackwall 作業ログ",
        "",
        "<!-- Blackwall が作業の終わりに書き足し、次の会話の最初に読みます。「メモ」は書き換えません"
        "（手で書き足してかまいません）。不要になったらこのファイルを消してください。 -->",
        "",
        f"{H_ENV}（{time.strftime('%Y-%m-%d %H:%M')} 時点）",
        _env_block(cwd, struct),
        "",
        H_NOTES,
        notes,
        "",
        f"{H_LOG}（新しい順・最大 {MAX_ENTRIES} 件）",
        "",
        "\n\n".join(entries[:MAX_ENTRIES]),
        "",
    ])
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        gi = p.parent / ".gitignore"
        if not gi.exists():
            gi.write_text("# Blackwall の作業ログなど。リポジトリには入れない。\n*\n", encoding="utf-8")
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def context_block(cwd: str) -> str:
    """新しい会話の最初の依頼に添える塊。作業ログが無ければ ""。"""
    p = path(cwd)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    secs = _sections(text)
    notes = _find(secs, H_NOTES).strip()
    ents = _entries(_find(secs, H_LOG))
    if not ents and (not notes or notes == NOTES_HINT):
        return ""
    parts = []
    env = _find(secs, H_ENV).strip()
    if env:
        parts.append("Environment when last saved:\n" + env[:2600])
    if notes and notes != NOTES_HINT:
        parts.append("Notes:\n" + (notes if len(notes) <= _CTX_NOTES_CHARS else notes[:_CTX_NOTES_CHARS] + "…"))
    if ents:
        recent = "\n\n".join(ents[:_CTX_ENTRIES])
        if len(recent) > _CTX_ENTRIES_CHARS:
            recent = recent[:_CTX_ENTRIES_CHARS] + "…"
        parts.append(f"Most recent work ({min(len(ents), _CTX_ENTRIES)} of {len(ents)}):\n{recent}")
    try:
        updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
    except OSError:
        updated = "?"
    return (f'<worklog path="{p}" updated="{updated}">\n' + "\n\n".join(parts) + "\n</worklog>\n"
            "This is your own work log for this folder (earlier conversations and earlier work in this one, "
            "including which files each step created or changed). Use it to pick up the work quickly instead of "
            "exploring or re-checking from scratch; read the "
            "whole file only if you need more. When you learn something the next conversation will need "
            "(where a tool is, how to run or build things, a decision), add a short line under 「## メモ」 "
            "in that file.")
