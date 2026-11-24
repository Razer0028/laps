"""壊す形の Bash の前に状態を控え、後で何が変わったかを言う（2026-11-20・外部の評価「Bash の変更を戻せない」への対応）。

- ファイル: rm・mv・sed -i・truncate・`>` の上書き・cp の上書き先（PowerShell の Remove-Item・Move-Item・Set-Content・Out-File も）
  が触る既存のファイルを、依頼の控え（engine/undo.py の RunChanges.record）へ入れる＝/undo と Rewind で戻せる。
  フォルダを消す命令は中のファイルも控える（MAX_FILES 個・MAX_BYTES まで。超えたら「控えきれなかった」と言う）。
- サービス: systemctl の stop/start/restart/disable/enable/mask/unmask（PowerShell の Stop/Start/Restart/Set-Service）は、
  前と後の状態（active・enabled）を取り、変わったら戻し方の命令を添える。
- 網: ip route/addr/link・iptables・nft・ufw・firewall-cmd は、前の状態を控えのフォルダへ書き出して場所を言う（Linux だけ）。
判定は語の形だけ（シェルを解釈しない）。取りこぼしはありうる＝控えたと言えた物だけを言う。失敗しても命令は止めない。
`LAPS_BASH_SNAPSHOT=0` で止める。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

MAX_FILES = 2000
MAX_BYTES = 256 * 1024 * 1024

_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|(?!\|)|\n)\s*")
_REDIR = re.compile(r"(?<![0-9&>])>(?!>|&)\s*([^\s;&|<>]+)")
_SVC = re.compile(r"\bsystemctl\s+(?:--\S+\s+)*(stop|start|restart|reload|disable|enable|mask|unmask|kill)\s+([^;&|\n]+)")
_PS_SVC = re.compile(r"\b(Stop|Start|Restart|Set)-Service\b[^;|\n]*?(?:-Name\s+)?['\"]?([A-Za-z0-9_.\-]+)['\"]?", re.I)
_NET = re.compile(r"\b(ip\s+(?:-\S+\s+)*(?:route|r|addr|a|address|link|rule)\s+(?:add|del|delete|replace|change|flush|set)"
                  r"|iptables|ip6tables|nft\s|ufw\s|firewall-cmd|nmcli\s+(?:con|connection)\s+(?:mod|modify|down|delete))")


def enabled() -> bool:
    return os.environ.get("LAPS_BASH_SNAPSHOT", "1").strip().lower() not in ("0", "off", "false", "no")


def _words(seg: str) -> list[str]:
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


def _abs(p: str, cwd: str) -> str:
    p = os.path.expanduser(p.strip("\"'"))
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def file_targets(command: str, cwd: str) -> list[str]:
    """命令が書き換え・消す既存のパス（ファイルかフォルダ）。ワイルドカードは展開しない（展開はシェルの仕事・取りこぼしてよい）。"""
    out: list[str] = []
    for m in _REDIR.finditer(command):
        out.append(m.group(1))
    for seg in _SPLIT.split(command):
        w = _words(seg)
        while w and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]) or w[0] in ("sudo", "doas", "env", "nohup", "time")):
            w = w[1:]
        if not w:
            continue
        cmd = os.path.basename(w[0]).lower()
        args = [a for a in w[1:] if not a.startswith("-")]
        if cmd in ("rm", "unlink", "shred", "truncate", "remove-item", "ri", "del", "erase", "rmdir"):
            out += args
        elif cmd in ("mv", "move-item", "move"):
            out += args                                  # 元も行き先（上書き）も
        elif cmd in ("cp", "copy-item", "install"):
            out += args[-1:]                             # 上書き先
        elif cmd == "sed" and any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in w[1:]):
            out += [a for a in args[1:]]                 # 最初の非オプション引数は式
        elif cmd in ("set-content", "out-file", "add-content", "clear-content", "sc"):
            m = re.search(r"-(?:Path|FilePath|LiteralPath)\s+(['\"]?)([^'\"\s]+)\1", seg, re.I)
            if m:
                out.append(m.group(2))
            elif args:
                out.append(args[0])
        elif cmd in ("git",) and len(w) > 1 and w[1] in ("checkout", "restore") and "--" in w:
            out += w[w.index("--") + 1:]
    seen: list[str] = []
    for p in out:
        if not p or any(ch in p for ch in "*?$`") or p in ("/", "~", "."):
            continue
        ap = _abs(p, cwd)
        if os.path.exists(ap) and ap not in seen:
            seen.append(ap)
    return seen


def _record(changes, paths: list[str]) -> tuple[int, list[str]]:
    """控える。（控えたファイルの数、控えきれなかったパス）。"""
    n, missed, total = 0, [], 0
    for ap in paths:
        if os.path.isfile(ap):
            items = [ap]
        else:
            items = []
            for root, _dirs, files in os.walk(ap):
                for f in files:
                    items.append(os.path.join(root, f))
                    if len(items) > MAX_FILES:
                        break
                if len(items) > MAX_FILES:
                    break
            if len(items) > MAX_FILES:
                missed.append(ap)
                continue
        for f in items:
            try:
                sz = os.path.getsize(f)
            except OSError:
                continue
            if total + sz > MAX_BYTES:
                missed.append(ap)
                break
            before = len(changes.files)
            changes.record(f)
            if len(changes.files) > before:
                n += 1
                total += sz
    return n, missed


def _run(args: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def services(command: str, kind: str) -> list[tuple[str, str]]:
    """[(管理の種類, サービス名)]。"""
    out: list[tuple[str, str]] = []
    for m in _SVC.finditer(command):
        for name in m.group(2).split():
            if not name.startswith("-"):
                out.append(("systemd", name.strip("\"'")))
    if kind == "powershell" or re.search(r"-Service\b", command):
        for m in _PS_SVC.finditer(command):
            out.append(("windows", m.group(2)))
    return out[:8]


def service_state(mgr: str, name: str) -> str:
    if mgr == "systemd":
        a = _run(["systemctl", "is-active", name]) or "?"
        e = _run(["systemctl", "is-enabled", name]) or "?"
        return f"{a}/{e}"
    out = _run(["powershell", "-NoProfile", "-Command",
                f"$s=Get-Service -Name '{name}' -ErrorAction SilentlyContinue; if($s){{\"$($s.Status)/$($s.StartType)\"}}"])
    return out or "?"


def _revert_hint(mgr: str, name: str, before: str, after: str) -> str:
    if mgr == "systemd":
        a0, e0 = (before.split("/") + ["?"])[:2]
        a1, e1 = (after.split("/") + ["?"])[:2]
        cmds = []
        if e0 != e1 and e0 in ("enabled", "disabled", "masked"):
            cmds.append(("systemctl unmask " + name) if e1 == "masked" else "")
            cmds.append(f"systemctl {'enable' if e0 == 'enabled' else 'disable'} {name}" if e0 != "masked" else f"systemctl mask {name}")
        if a0 != a1 and a0 in ("active", "inactive"):
            cmds.append(f"systemctl {'start' if a0 == 'active' else 'stop'} {name}")
        return " && ".join(c for c in cmds if c)
    st0 = before.split("/")[0]
    return f"{'Start' if st0 == 'Running' else 'Stop'}-Service {name}" if st0 in ("Running", "Stopped") else ""


class Snapshot:
    """命令 1 回ぶん。before() を命令の前、after() を終わった後に呼ぶ。"""

    def __init__(self, command: str, cwd: str, kind: str, changes) -> None:
        self.command, self.cwd, self.kind, self.changes = command, cwd, kind, changes
        self.svc: list[tuple[str, str, str]] = []
        self.kept = 0
        self.missed: list[str] = []
        self.net_file = ""

    def before(self) -> None:
        if self.changes is not None:
            try:
                self.kept, self.missed = _record(self.changes, file_targets(self.command, self.cwd))
            except Exception:
                pass
        for mgr, name in services(self.command, self.kind):
            self.svc.append((mgr, name, service_state(mgr, name)))
        if os.name != "nt" and _NET.search(self.command) and self.changes is not None:
            parts = []
            for args in (["ip", "-o", "addr"], ["ip", "route", "show", "table", "all"], ["ip", "rule"],
                         ["iptables-save"], ["nft", "list", "ruleset"], ["ufw", "status", "verbose"]):
                out = _run(args)
                if out:
                    parts.append("$ " + " ".join(args) + "\n" + out)
            if parts:
                try:
                    d = Path(self.changes.dir)
                    d.mkdir(parents=True, exist_ok=True)
                    f = d / f"net-before-{int(time.time() * 1000)}.txt"
                    f.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
                    self.net_file = str(f)
                except OSError:
                    self.net_file = ""

    def after(self) -> list[str]:
        notes: list[str] = []
        if self.kept:
            notes.append(f"Before this command, {self.kept} file(s) it changes or deletes were saved; /undo (or Rewind) "
                         "can put them back.")
        if self.missed:
            notes.append("Not saved before this command (too many or too large): " + ", ".join(self.missed[:3]))
        for mgr, name, before in self.svc:
            after = service_state(mgr, name)
            if after != before:
                hint = _revert_hint(mgr, name, before, after)
                notes.append(f"Service {name}: {before} → {after}" + (f" (to undo: {hint})" if hint else "")
                             + ". Check that this is what was wanted.")
        if self.net_file:
            notes.append(f"The network and firewall state before this command was saved to {self.net_file} "
                         "(compare it, or restore from it, if something stops working).")
        return notes
