"""Bash の前後に当てる、実際に踏んだ穴の塞ぎ。

中身は既存の部品（laps/oc/shellfix.py・tools/bash.py・oc/tools.py・oc/hollow.py）で、ここは順番だけを決める。
部品が壊れても命令は走らせる（どの段も失敗したら何もしない）。"""
from __future__ import annotations

import os
import re
import sys

WIN = os.name == "nt"
_CD_FIRST = re.compile(r"\s*(cd|pushd|set-location|sl)\s", re.I)


def fix_text(command: str, cwd: str, kind: str) -> tuple[str, list[str]]:
    """審査の前に直す（審査した文字列と走る文字列を一致させる）。引用漏れと、見た目が同じで文字だけ違うパス（全角空白など）。"""
    notes: list[str] = []
    try:
        from ...tools import bash as b
        fixes = [lambda c: b._fix_confusable_paths(c, cwd)]
        if WIN and kind != "bash":
            fixes.insert(0, b._win_autoquote)
        for fix in fixes:
            try:
                command, n = fix(command)
            except Exception:
                n = ""
            if n and n.strip():
                notes.append(n.strip())
    except Exception:
        pass
    return command, notes


def guard(command: str, cwd: str, kind: str, workspace: str, strict: bool) -> tuple[str, list[str], str]:
    """(走らせる命令, 一言, 断る理由)。
    利用者の deny_patterns・trusted 以外での壊す命令・本体ごと止める命令・作業場の外のプロセスを撃つ命令。"""
    notes: list[str] = []
    # ラボの取り返しのつかない操作（Windows の VM の再起動・.6 の保守・鍵の export など・2026-11-09）。前置きに書くだけでは守られない。
    try:
        from ... import permission as perm
        from ...settings import Settings
        try:
            deny = Settings.load().deny_patterns or []
        except Exception:
            deny = []
        if deny and perm._match_any(deny, command):
            return command, notes, ("This command matches deny_patterns in the user's settings.json and was not run. "
                                    "Do it another way, or ask the user.")
        if strict and perm.is_destructive(command) and not perm.confirm(command, kind="Bash"):
            return command, notes, ("This destructive command was not run: the permission gate refused it (no one could "
                                    "confirm it under the current policy). Ask the user, or use a non-destructive way.")
    except Exception:
        pass
    # `taskkill /F /IM python.exe` が本体ごと止めて、走行が「Python の出力が閉じました」で終わった（2026-10-10）。
    try:
        from ...oc import shellfix
        guarded, note = shellfix.guard_self_kill(command, windows=sys.platform == "win32")
        if guarded is None:
            return command, notes, note
        if note:
            command = guarded
            notes.append(note)
    except Exception:
        pass
    # 名前・PID・入れ子の kill を作業場の中のプロセスだけに絞る（旧の実害: 別のサービスを 7 分で 6 回撃った）。外れた弾だけ落として書く。
    if sys.platform != "win32":
        try:
            from ... import procutil
            from ...tools import bash as b
            said = ""
            for fn in (lambda c: b._scope_nested_kill(c, workspace, base=cwd), lambda c: b._scope_name_kill(c, workspace),
                       lambda c: b._scope_pid_kill(c, workspace, base=cwd)):
                try:
                    r = fn(command)
                except Exception:
                    r = None
                if r:
                    command, n = r
                    said += n
            if said:
                try:
                    said += procutil.workspace_note()
                except Exception:
                    pass
                notes.append(said.strip())
        except Exception:
            pass
    if kind == "cmd":                  # cmd.exe の癖だけで落ちる形を直す（複数行の python -c・timeout /t）
        try:
            from ...oc import shellfix
            command, more = shellfix.prepare_windows(command)
            notes.extend(more)
        except Exception:
            pass
    return command, notes, ""


def is_server(command: str) -> bool:
    """はっきりしたサーバーの命令（前面で走らせると時間切れまで返らない・2026-10-10 に 1 走行で 6 分失った）。"""
    try:
        from ...oc import shellfix
        return shellfix.looks_like_server(command)
    except Exception:
        return False


def no_match(command: str, code: int | None, text: str) -> str:
    """終了コード 1 が「絞り込みが何も拾わなかった」だけなら、その絞り込みの名前（失敗にしない）。"""
    try:
        from ...oc import shellfix
        return shellfix.is_no_match(command, code, text, "", windows=sys.platform == "win32") or ""
    except Exception:
        return ""


def failure_notes(command: str, code: int | None, text: str, cwd: str, workdir_given: bool) -> list[str]:
    """失敗の結果に添える一言（番号の意味・直し方の手掛かり・cd が続かないこと・出力の行き先）。"""
    notes: list[str] = []
    try:
        from ...oc import tools as old
        notes.append(f"exit code: {code}" + old._exit_code_note(code))
        tip = old._failure_tip(text)
        if tip:
            notes.append(tip)
        low = text.lower()
        if (not workdir_given and not _CD_FIRST.match(command or "")
                and (any(k in low for k in old._PATH_MISSING) or "no rule to make target" in low)):
            notes.append(f"This command ran in {cwd}. Every Bash call starts there: a `cd` in an earlier call does not "
                         "carry over. Pass workdir=\"<folder>\" (or start the same command with `cd <folder> &&`).")
        if WIN:
            try:
                from ...tools import bash as b
                hint = (b._win_ps_param_hint(text) + b._win_prn_hint(command, text)).strip()
                if hint:
                    notes.append(hint)
            except Exception:
                pass
        empty = old._empty_fail_note(command, text)
        if empty:
            notes.append(empty)
    except Exception:
        if not notes:
            notes.append(f"exit code: {code}")
    return notes


def hollow_note(command: str, text: str, ok: bool) -> str:
    """成功しても何も確かめていない命令（試験 0 本・生のバイトを音や映像として読む）を名乗る。"""
    try:
        from ...oc import hollow
        return hollow.note(command or "", text, ok=ok) or ""
    except Exception:
        return ""


def with_metadata(body: str, notes: list[str]) -> str:
    return body + "\n\n<shell_metadata>\n" + "\n".join(notes) + "\n</shell_metadata>" if notes else body
