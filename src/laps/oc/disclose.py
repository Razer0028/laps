"""利用者に伝えるべきことを、伝えたかを機械で確かめる（2026-10-20 オーナー「進めていいです」）。

★きっかけ（SNS の撮影の走行・10-20 01:22・test2・KAT）: 依頼は「最新の AI のニュースを調べて、要点を 3 つ report.md に」。
  外へ出られない機械で、エージェントは本文を空のまま DNS の設定を書き換え（/etc/systemd/resolved.conf.d に足して
  systemd-resolved を再起動・resolvectl dns）、最後の答えでもそれに触れず、戻しもしなかった。設定はその後の撮り直し 5 回の
  あいだ残っていた。前置きには「壊す操作の前に、何に影響するかを 1 行言う」と書いてあった＝**書いてあっても守られない**。
  さらに report.md はモデルの記憶だけで書き（中身は古い）、末尾に「2026年10月時点のAI動向をベースに作成」とあった。
  「調べられなかった」はチャットの答えにだけ書き、ファイルを読む人には伝わらなかった。

ここで決めること（モデルに頼まず、ハーネスが決まって行う）:
  ・system_changes(): Bash の命令と Write・Edit の行き先から、作業フォルダの外のシステムの設定を変える物を拾う。
    loop が ①本文の無い手番では走らせずに 1 行言わせ（_execute の関門）②最後の答えに一覧を付ける（footer）。
  ・caveat_missing(): 答えが「調べられなかった・取得できなかった」と言うのに、この走行で書いた文書にはそれが書いていない。
"""
from __future__ import annotations

import os
import re
import shlex

#: システムの設定が置かれる場所（ここへの書き込みは作業フォルダの外の設定を変える）。
SYSTEM_DIRS = ("/etc/", "/usr/lib/systemd/", "/lib/systemd/", "/boot/", "/var/spool/cron/", "/usr/local/etc/")

#: 状態を変える下位命令（これ以外は読むだけとみなす）。
_VERBS = {
    "systemctl": {"start", "stop", "restart", "reload", "try-restart", "reload-or-restart", "enable", "disable",
                  "mask", "unmask", "daemon-reload", "set-property", "edit", "isolate", "set-default", "kill"},
    "service": None,          # service <名前> <動詞> は下で見る
    "resolvectl": {"dns", "domain", "default-route", "llmnr", "mdns", "dnssec", "dnsovertls", "revert",
                   "nta", "set-link"},
    "hostnamectl": {"set-hostname", "hostname", "set-icon-name", "set-chassis", "set-deployment", "set-location"},
    "timedatectl": {"set-time", "set-timezone", "set-ntp", "set-local-rtc"},
    "ufw": {"allow", "deny", "reject", "limit", "delete", "insert", "enable", "disable", "reset", "route", "default"},
    "nft": {"add", "delete", "flush", "insert", "replace", "create", "destroy"},
    "apt": {"install", "remove", "purge", "autoremove", "upgrade", "full-upgrade", "dist-upgrade", "reinstall"},
    "apt-get": {"install", "remove", "purge", "autoremove", "upgrade", "dist-upgrade", "reinstall"},
    "snap": {"install", "remove", "refresh", "disable", "enable"},
    "netplan": {"apply", "set"},
    "nmcli": {"modify", "mod", "up", "down", "add", "delete", "del", "reload", "set", "on", "off"},
}
#: 呼んだだけで設定を変える命令。
_ALWAYS = frozenset({"useradd", "usermod", "userdel", "groupadd", "groupmod", "groupdel", "chpasswd",
                     "modprobe", "rmmod", "insmod", "update-grub", "update-initramfs", "locale-gen"})
_IP_OBJECTS = {"addr", "address", "a", "route", "r", "link", "l", "rule", "neigh", "n", "tuntap"}
_IP_VERBS = {"add", "del", "delete", "change", "replace", "set", "flush", "append"}
_IPTABLES_FLAGS = {"-A", "-I", "-D", "-R", "-F", "-P", "-N", "-X", "-Z", "-E", "--append", "--insert", "--delete",
                   "--replace", "--flush", "--policy", "--new-chain", "--delete-chain"}
#: 引数に書いたパスへ書く命令（行き先がシステムの場所なら変更）。
_PATH_WRITERS = frozenset({"cp", "mv", "ln", "install", "rm", "rmdir", "mkdir", "touch", "chmod", "chown",
                           "chgrp", "truncate", "tee", "rsync", "unlink", "shred"})
#: 前に付くだけの命令（読み飛ばして中身を見る）。値を取る選択肢つき。
_WRAPPERS = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U"}, "doas": {"-u", "-C"},
             "env": {"-u", "-C", "-S"}, "nohup": set(), "nice": {"-n"}, "ionice": {"-c", "-n", "-p"},
             "timeout": {"-s", "-k"}, "stdbuf": {"-i", "-o", "-e"}, "time": set(), "command": set(), "exec": set()}
_SSH_VALUE_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")
_SHELLS = frozenset({"bash", "sh", "dash", "zsh"})


def _split(command: str) -> list[list[str]]:
    try:
        from ..policy import _split_commands
        return _split_commands(command, heredocs=False)       # 本文は呼ぶ側（_strip_heredocs）が先に外している
    except Exception:                                  # 引用の崩れた命令でも、空白で割って見る（数え落とさない）
        out: list[list[str]] = []
        for part in re.split(r"\|\||&&|[;|&\n]", command or ""):
            if part.split():
                out.append(part.split())
        return out


def system_path(path: str) -> bool:
    """作業フォルダの外のシステムの設定の場所か（絶対パスだけを見る）。"""
    p = str(path or "").strip().strip("'\"")
    if not p.startswith("/"):
        return False
    p = os.path.normpath(p) + ("/" if p.endswith("/") else "")
    return any(p.startswith(d) or p + "/" == d for d in SYSTEM_DIRS)


def _unwrap(argv: list[str]) -> list[str]:
    """sudo・env・nohup などを読み飛ばし、VAR=値 の前置も外す。"""
    i = 0
    while i < len(argv):
        tok = argv[i]
        base = os.path.basename(tok)
        if "=" in tok and not tok.startswith(("-", "/")) and base == tok:
            i += 1
            continue
        if base in _WRAPPERS:
            vals = _WRAPPERS[base]
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] in vals else 1
            if base == "timeout" and i < len(argv) and re.fullmatch(r"[\d.]+[smhd]?", argv[i]):
                i += 1
            if base == "nice" and i < len(argv) and re.fullmatch(r"-?\d+", argv[i]):
                i += 1
            continue
        break
    return argv[i:]


def _stage_change(argv: list[str]) -> bool:
    """1 つの命令（パイプの 1 段）がシステムの設定を変えるか。"""
    if not argv:
        return False
    prog = os.path.basename(argv[0])
    args = argv[1:]
    words = [a for a in args if not a.startswith("-")]
    if prog in _ALWAYS:
        return True
    if prog in ("iptables", "ip6tables", "iptables-legacy", "iptables-nft"):
        return any(a in _IPTABLES_FLAGS for a in args)
    if prog == "ip":
        return len(words) >= 2 and words[0] in _IP_OBJECTS and words[1] in _IP_VERBS
    if prog == "sysctl":
        return any(a in ("-w", "--write", "-p", "--load", "--system") for a in args) or any("=" in w for w in words)
    if prog == "crontab":
        return not any(a in ("-l", "--list") for a in args)
    if prog == "service":
        return len(words) >= 2 and words[1] in ("start", "stop", "restart", "reload", "force-reload")
    if prog == "dpkg":
        return any(a in ("-i", "-r", "-P", "--install", "--remove", "--purge", "--configure") for a in args)
    if prog == "update-alternatives":
        return any(a in ("--set", "--install", "--remove", "--remove-all", "--auto", "--config") for a in args)
    if prog == "sed" and any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args):
        return any(system_path(a) for a in words)
    if prog in _PATH_WRITERS:
        return any(system_path(a) for a in words)
    verbs = _VERBS.get(prog)
    if verbs:
        return bool(words) and words[0] in verbs
    return False


def _redirect_targets(argv: list[str]) -> list[str]:
    out = []
    for k, tok in enumerate(argv):
        if tok in (">", ">>", "1>", "&>", ">|", "1>>") and k + 1 < len(argv):
            out.append(argv[k + 1])
        elif re.fullmatch(r"(?:1|&)?>>?\S+", tok):
            out.append(tok.lstrip("1&>"))
    return out


def _ssh_remote(argv: list[str]) -> tuple[str, str] | None:
    """ssh [選択肢] 宛先 命令… → (宛先, 命令)。命令が無ければ None。"""
    k = 1
    while k < len(argv) and argv[k].startswith("-"):
        opt = argv[k]
        k += 2 if (len(opt) == 2 and opt[1] in _SSH_VALUE_OPTS) else 1
    if k >= len(argv) - 1:
        return None
    return argv[k], " ".join(argv[k + 1:])


_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def _strip_heredocs(command: str) -> str:
    """ヒアドキュメントの中身（ファイルへ書く文章や台本）を命令として読まない。"""
    out, tag = [], ""
    for line in (command or "").replace("\r\n", "\n").split("\n"):
        if tag:
            if line.strip() == tag:
                tag = ""
            continue
        out.append(line)
        m = _HEREDOC.search(line)
        if m:
            tag = m.group(1)
    return "\n".join(out)


def command_changes(command: str, *, _depth: int = 0) -> list[str]:
    """Bash の命令のうち、システムの設定を変える部分（命令の文字列）。他の機械へ ssh で送る物は「(宛先) 命令」。"""
    found: list[str] = []
    if _depth > 3:
        return found
    for argv in _split(_strip_heredocs(command)):
        stage = _unwrap(argv)
        if not stage:
            continue
        prog = os.path.basename(stage[0])
        if prog in _SHELLS and "-c" in stage[1:]:
            k = stage.index("-c")
            if k + 1 < len(stage):
                found += command_changes(stage[k + 1], _depth=_depth + 1)
            continue
        if prog == "ssh":
            rem = _ssh_remote(stage)
            if rem:
                found += [f"({rem[0]}) {c}" for c in command_changes(rem[1], _depth=_depth + 1)]
            continue
        hit = _stage_change([t for t in stage if not re.fullmatch(r"(?:\d|&)?>>?\S*", t)])
        if not hit:
            hit = any(system_path(t) for t in _redirect_targets(stage))
        if hit:
            text = " ".join(shlex.quote(t) if re.search(r"\s", t) else t for t in stage)
            found.append(text if len(text) <= 160 else text[:157] + "...")
    return found


def call_changes(name: str, inp: dict) -> list[str]:
    """道具の呼び出し 1 つが変えるシステムの設定（無ければ空）。"""
    if not isinstance(inp, dict):
        return []
    if name == "Bash":
        return command_changes(str(inp.get("command") or ""))
    if name in ("Write", "Edit", "NotebookEdit"):
        fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
        return [f"{name} {fp}"] if system_path(fp) else []
    return []


def footer(changes: list[tuple[str, bool]], japanese: bool) -> str:
    """最後の答えに付ける一覧。changes は (変更, 成功したか) の並び（同じ物は 1 つに）。"""
    seen: dict[str, bool] = {}
    for what, ok in changes:
        seen[what] = seen.get(what, False) or ok
    if not seen:
        return ""
    items = list(seen.items())
    shown, rest = items[:12], len(items) - 12
    if japanese:
        lines = [f"- `{w}`" + ("" if ok else "（失敗した）") for w, ok in shown]
        if rest > 0:
            lines.append(f"- ほか {rest} 件")
        return ("\n\n---\nこの作業で変えたシステムの設定（作業フォルダの外）:\n" + "\n".join(lines)
                + "\n（走らせた命令からハーネスが拾った一覧です。元に戻したかどうかは見ていません。）")
    lines = [f"- `{w}`" + ("" if ok else " (failed)") for w, ok in shown]
    if rest > 0:
        lines.append(f"- and {rest} more")
    return ("\n\n---\nSystem settings changed in this task (outside the work folder):\n" + "\n".join(lines)
            + "\n(Collected by the harness from the commands that ran. Whether they were undone is not checked.)")


def gate_message(changes: list[str]) -> str:
    lst = "; ".join(f"`{c}`" for c in changes[:3])
    # ★2026-10-22 Terminal-Bench の診断の再走行（build-initramfs-qemu）: 旧の「要らない変更ならせず、別の道を探せ」を読んで、
    #   要る `apt-get install busybox-static` を 1 回止められただけで諦め、手作りの initramfs で起動できずに落ちた。
    #   許されている・同じ呼び出しで走る、を先に言う。
    return ("[Harness] Not run yet. It is allowed: this changes system settings outside the work folder (" + lst + "). "
            "First write one line to the user: what it changes and why the task needs it. Then call it again unchanged "
            "and it will run. Do not ask for permission; just say it. Your final answer will list every system setting "
            "you changed.")


# ── 文書に「調べられなかった」を書いたか ─────────────────────────────────────

#: 答えが「調べる・取りに行く」ことができなかったと言っている（情報の出どころに触れた言い方だけ）。
_LIMIT = re.compile(
    r"(調べ|検索|取得|入手|収集|アクセス|接続|閲覧|参照)(が|は|を|も)?(でき|出来|行え|られ)(ず|なかった|ませんでした|ません|ない)|"
    r"(調べ|取得|入手|収集|確認|検証)(られ|でき|出来)(ず|なかった|ませんでした|ません|ない)[^。\n]{0,30}(ネット|ウェブ|Web|web|外部|オンライン|最新|ニュース|サイト|情報源)|"
    r"(ネット|インターネット|ウェブ|Web|web|外部|オンライン|サイト|DNS)[^。\n]{0,20}(つなが|繋が|届か|出られ|接続でき|アクセスでき|使え)(ず|なかった|ません|ない)|"
    r"最新の(情報|ニュース|動向)[^。\n]{0,20}(得|取得|確認|入手)(でき|られ)(ず|なかった|ません|ない)|"
    r"\b(could not|couldn't|was unable to|were unable to|unable to|cannot|can't|not able to)\s+"
    r"(access|reach|fetch|retrieve|browse|search|look up|connect to|get to|download|verify (?:it|this|them)? ?online)|"
    r"\bno (?:internet|network|web) access\b|\bwithout (?:internet|network|web) access\b|\b(?:is|was|am) offline\b",
    re.I)

#: 文書の中で「確かめていない・出どころはモデルの記憶」と断っている言い方。
_NOTED = re.compile(
    r"調べられ|検索でき|取得でき|入手でき|アクセスでき|接続でき|確認でき(ず|なかった|ていない|ません)|未確認|確認していない|"
    r"検証していない|オフライン|学習(データ|時点|した知識)|知識(を|に)(もと|基)|記憶(を|に)(もと|基)|最新では(ない|ありません)|"
    r"could not|couldn't|unable to|not verified|unverified|offline|training data|from memory|may be out of date|"
    r"may be outdated|not checked", re.I)

#: 読み手が開く文書の形（文字で読める物だけ）。
DOC_EXTS = (".md", ".markdown", ".txt", ".html", ".htm", ".rst", ".adoc", ".org", ".tex")


def limitation(answer: str) -> str:
    """答えの中の「調べられなかった」の一文（無ければ空）。"""
    m = _LIMIT.search(answer or "")
    if not m:
        return ""
    s = answer.rfind("\n", 0, m.start()) + 1
    for mark in ("。", ". "):
        k = answer.rfind(mark, s, m.start())
        if k >= 0:
            s = k + len(mark)
    e = len(answer)
    for mark in ("。", "\n", ". "):
        k = answer.find(mark, m.end())
        if k >= 0:
            e = min(e, k + (1 if mark == "。" else 0))
    return answer[s:e].strip()[:200]


def caveat_missing(paths: list[str]) -> list[str]:
    """文書のうち、確かめていないことに一言も触れていない物。"""
    out: list[str] = []
    for p in dict.fromkeys(paths):
        if not str(p).lower().endswith(DOC_EXTS):
            continue
        norm = str(p).replace("\\", "/")
        if "/_scratch/" in "/" + norm or os.path.basename(norm).startswith("."):
            continue
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
                continue
            with open(p, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        if text.strip() and not _NOTED.search(text):
            out.append(str(p))
    return out


def caveat_message(quote: str, files: list[str], lang: str) -> str:
    names = ", ".join(os.path.basename(f) for f in files[:4])
    return (f"[Harness] Before you finish: your answer says \"{quote}\", but the file(s) you wrote ({names}) do not say "
            "this. Someone who reads only the file will think its content was looked up and is current. Add a short note "
            "near the top of each file: what you could not look up or check, and what the content is based on instead "
            "(for example the model's own knowledge, which may be out of date). Remove or correct lines that suggest the "
            "content was checked or is current (for example \"based on trends as of <date>\"). Then finish, and write to "
            f"the user in {lang}.")
