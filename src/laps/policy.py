"""Policy gate — the single place where a side effect can be authorised.

Design notes (why it looks like this)
-------------------------------------
The old guard was a list of 13 "destructive" regexes. It caught `rm -rf` and
missed `find . -delete`, `> file`, `truncate`, `systemctl restart`, `curl | sh`,
and every `ssh host '...'`. A denylist over shell text cannot be made complete:
aliases, paths, encodings and pipes all defeat string matching. So this gate
inverts it — nothing runs unless its program is on an allowlist for the current
mode, and anything unparseable is refused.

Guardrails written in the system prompt are not a boundary: the model sees
instructions and untrusted data on the same channel, and prompt-injected text
can ask for anything. The boundary has to sit outside the model, which is what
this module is. Constraints also decay when a long conversation gets compacted
away, and this file does not care what is left in the context window.

Two properties are deliberate:

* The agent may not edit its own permissions. PROMPTS/, policy.py, permission.py,
  settings.json, postedit.json, hooks/, plugins/ and the audit log are refused
  **for writing** in every mode (reading them is fine — see _self_protected_path;
  until 2026-09-02 the Bash path refused reads too, which was never the promise).
  A real CVE (2025-53773) went the other way: injected text had the assistant
  enable its own auto-approve setting, and every later command ran unattended.
* The audit record is written before the call is dispatched, and it chains each
  entry to the hash of the previous one, so a later edit to the log is visible.

Modes (LAPS_POLICY, default "workspace" — see _mode() below; the mode list
       is kept in reading order, least to most permissive):
    readonly   — read-only programs only. The conservative default.
    workspace  — adds writes under LAPS_WORKSPACE (default: cwd) and a
                 slightly wider program list. Still no remote, no privilege
                 escalation, no package management, no service control.

    ⚠ **この2つを「境界」として売らないこと**（2026-09-02 実測・実物の Bash 道具で
      最後まで走らせて確認）。readonly は「読むだけ」と言えず、workspace は
      「作業フォルダの中だけ」と言えない:
        readonly   `sort -o /外/x /etc/hostname` / `awk 'BEGIN{print > "/外/x"}'`
                   → 外にファイルが**実際に**出来た（許可リストのプログラムが
                     持っている機能で、プログラムごとに違う＝列挙が閉じない）
        workspace  `python3 evil.py` / `pytest`(conftest.py) / `tar xPf`
                   → 外に書けた。インタプリタは**試験を走らせるために意図的に
                     開けてある**ので閉じられない（_WORKSPACE_EXTRA のコメント参照）
      閉じたのは「全プログラムに一度に効く」種類だけ（`cd` の cwd 追従・
      `LD_PRELOAD=` の前置）。本当の直し方は別 uid / bwrap / コンテナで、
      それは**このファイルの外**にある。
    off        — no authorisation. Every call is still logged, loudly. Intended
                 for a throwaway sandbox, never for a host that matters.
                 ★`_ALWAYS_DENIED`（sudo/ssh/curl 等）は off でも止まる。
    trusted    — **何も止めない。** 利用者が「自分の機械で全部やらせる」と決めた状態。
                 `_ALWAYS_DENIED` も許可リストも書き込み先の封じ込めも外れる。

                 ★2026-08-18 オーナー指示「フルに自由度を与えたい。ユーザー環境を
                   自由に操れるように。Windows コマンドや Linux コマンドもちゃんと
                   使えるように」に応えるモード。
                 ★**正直に書いておく**: これは境界を外す選択で、モデルが誤れば
                   その機械が壊れる。止めない代わりに、**すべての呼び出しを audit に
                   残す**（何が走ったかを後から必ず辿れる）。readonly/workspace は
                   そのまま残してあるので、config.toml でいつでも戻せる。
                 ★自己保護（`_self_protected_path`＝権限定義・プロンプト・監査ログ・hooks/plugins）だけは
                   trusted でも残す。エージェントが自分の枷と記録を書き換えられると、
                   「後から辿れる」という対価まで失われるため。
"""
from __future__ import annotations
from . import paths as _lp
import contextvars
import glob
import hashlib
import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path


class PolicyDenied(Exception):
    """Raised instead of running something the gate would not authorise."""


# Programs that only read. Anything not listed is refused in readonly mode.
_READ_ONLY = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "find",
    "stat", "file", "du", "df", "readlink", "realpath", "basename", "dirname",
    "pwd", "echo", "printf", "date", "uname", "hostname", "id", "whoami",
    "env", "printenv", "which", "type", "uptime", "free", "ps", "top",
    "systemctl", "journalctl", "nvidia-smi", "lsblk", "lscpu", "ip", "ss",
    "netstat", "ping", "dig", "host", "sort", "uniq", "cut", "tr", "awk",
    "sed", "diff", "cmp", "md5sum", "sha256sum", "jq", "column",
    "git", "true", "false", "sleep", "seq", "test", "[",
    # ★bwtool（スクリプトから Blackwall の道具を呼ぶ口・2026-10-23）は自分で決まりを見る: readonly では読むだけの道具しか
    #   走らせず、読む場所・書く場所は道具の中の検査（authorize_read・authorize_path）がそのまま効く。
    "bwtool",
    # ★2026-08-11: 実機は Windows。cmd.exe には ls も cat も無く、
    #   POSIX 名だけの許可リストでは**何も実行できない**（実機で `ls` が失敗した）。
    #   読み取りだけの Windows 版を足す。**破壊系（del/rd/format/move）は足さない。**
    "dir", "findstr", "where", "tree", "more", "cd", "ver",
    "systeminfo", "tasklist", "ipconfig", "nslookup", "vol", "chcp",
})

# Interpreters and anything that spawns a program of its own choosing. These
# read fine, but they also execute: `echo code | python3` needs no -c, `xargs`
# runs whatever it is handed, `make` runs a Makefile it was pointed at. They
# are only available where writing is already permitted.
_INTERPRETERS = frozenset({"python3", "python", "node", "pytest", "make"})

# Additional programs allowed once writing is permitted at all.
#
# ★Interpreters are in here and that is a deliberate, load-bearing compromise.
# Measured: in workspace mode, `python3 evil.py` (a file the agent may legally
# write) read 6,050 bytes of a credential file and ran arbitrary commands as
# root. Everything this gate checks — allowlists, argument traps, secret paths —
# lives in the shell string, and an interpreter takes its instructions from a
# file the gate never sees. There is no string test that closes this.
#
# The honest consequence: **workspace mode is not a security boundary.** It is a
# convenience that keeps ordinary mistakes inside a directory. readonly mode is
# the mode with a claim to make, and even there the boundary for reads is the
# OS, not this file (see _SECRET_READ).
#
# The real fix is a different uid or a sandbox, not more patterns; until that
# exists, workspace must be pointed at work the operator would accept losing,
# and never used on a host that matters. LAPS_NO_INTERPRETERS=1 removes
# them for runs that want the stricter shape at the cost of running tests.
_WORKSPACE_EXTRA = frozenset({
    "mkdir", "touch", "cp", "mv", "chmod", "rm", "rmdir", "tar", "unzip",
    "gzip", "gunzip",
})
if os.environ.get("LAPS_NO_INTERPRETERS") != "1":
    _WORKSPACE_EXTRA = _WORKSPACE_EXTRA | _INTERPRETERS
# `ln` is absent on purpose: a hard link into the workspace makes an outside
# file writable through a path that resolve() reports as inside it.

# Refused in every mode, whatever the allowlist says. These are the verbs that
# reach beyond this host, escalate, or reconfigure the machine.
_ALWAYS_DENIED = frozenset({
    "sudo", "su", "doas", "pkexec",
    "ssh", "scp", "sftp", "rsync", "sshpass", "telnet", "nc", "ncat",
    "curl", "wget",  # egress: fetch/exfil off the host; `curl | sh` is code-in
    "apt", "apt-get", "dpkg", "yum", "dnf", "pacman", "snap", "pip", "pip3",
    "mkfs", "fdisk", "parted", "dd", "shutdown", "reboot", "poweroff", "halt",
    "iptables", "nft", "ufw", "modprobe", "insmod", "rmmod", "mount", "umount",
    "useradd", "usermod", "userdel", "passwd", "visudo", "chown", "chpasswd",
    "docker", "podman", "kubectl", "virsh", "esxcli", "vim-cmd",
    # `xargs`/`env`/`nohup`/`timeout`/`watch` all launch a program this parser
    # never sees; `ln` creates hard links out of the workspace.
    "xargs", "env", "nohup", "timeout", "watch", "setsid", "sh", "bash", "zsh",
    "eval", "exec", "ln", "tee",
})

# Arguments that turn an otherwise read-only program into a writer or a launcher.
# `w`/`W` used as a sed command (start of script, or after ; or }).
_SED_WRITE_RE = re.compile(r"(?:^|[;{}\n])\s*[wW]\s+\S")

_ARG_TRAPS = {
    "sed": ("-i", "--in-place", "-e", "--expression", "-f", "--file"),
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprintf",
             "-fls", "-fprint", "-fprint0"),
    "git": ("--upload-pack", "--receive-pack", "-c"),
    "sort": ("-o",),  # sort -o /外/x で外に書ける穴（readonly の穴）
}

# systemctl reads state with these and only these.
_SYSTEMCTL_READ_VERBS = frozenset({
    "status", "show", "list-units", "list-unit-files", "list-timers",
    "is-active", "is-enabled", "is-failed", "cat", "get-default",
})

# git subcommands that do not mutate a repo or talk to a remote.
_GIT_READ_VERBS = frozenset({
    "status", "log", "show", "diff", "branch", "remote", "config", "ls-files",
    "rev-parse", "describe", "blame", "shortlog", "tag", "stash",
})

# Paths the agent must never write, in any mode: its own permissions, its own
# prompt, and its own audit trail.
#
# ★2026-09-02: 「名前の部分一致」をやめた。実測した誤りは2つあり、向きが逆だった。
#   ① 緩すぎ（trusted）: 判定が `_PATH_WRITERS` 11 個の引数しか見ていなかったので、
#      `sed -i` / `dd of=` / `tee` / `truncate` / `install` / `ln` / glob
#      （`polic?.py`）/ `rm -rf ~/.blackwall`（親ごと）は**書き手として存在しない**
#      扱いになり、14 形のうち 10 形が出荷既定の trusted で通っていた。
#   ② 厳しすぎ（readonly/workspace）: 呼び出し側が読み書きを区別せず全トークンに
#      部分一致を当てていたので `cat …/policy.py` まで拒否していた。docstring の
#      約束は "refused **for writing**"、実装だけが読みも断っていた。しかも
#      `settings.json` は一般名なので、利用者の `.vscode/settings.json` も巻き込む。
#   → いまは `_self_protected_path()` が「**書く側の指定が、このプロセスが実際に
#      使っている** 権限定義／プロンプト／監査ログに解決したとき」だけ拒む。
#
# どこに在っても危険な名前だけは、従来どおり部分一致で残す（他人の機械の鍵で
# あっても、エージェントが書いてよい理由が無い）。
_SELF_PROTECTED_ANYWHERE = ("authorized_keys", "/.ssh/", "sudoers",
                            "/.config/blackwall/auth.json", "\\.config\\blackwall\\auth.json",
                            "/.config/laps/auth.json", "\\.config\\laps\\auth.json")

# Paths the agent must never READ, because their contents are credentials and
# reading them lands secrets in the model's context (A6). Added after the
# adversarial review confirmed the gate let `grep SSHPASS <a credentials note>`
# through in readonly mode: reads were
# entirely unrestricted. Matched as substrings of a resolved path.
#
# ★This is defense-in-depth, not a boundary. A path guard over shell strings
# cannot be complete — `python3 -c "open(secret).read()"`, /proc/<pid>/root
# symlinks, and an interpreter reading a file all bypass it. The real boundary
# for reads is OS-level: run the agent as a uid that cannot read these files.
# The guard closes the easy, common paths; it does not make readonly airtight.
_SECRET_READ = (
    "/.ssh/", "/etc/shadow", "/etc/ssh/ssh_host_",
    "id_rsa", "id_ed25519", ".token", "corridor-tokens",
    "/.config/blackwall/auth.json", "\\.config\\blackwall\\auth.json",   # 中継の認証情報（relayauth.py）
    "/.config/laps/auth.json", "\\.config\\laps\\auth.json",
)
# Directories whose whole contents are off-limits to recursive readers and to a
# cwd: a recursive read over an ancestor, or a cwd inside one, could reach a
# secret regardless of the literal arguments.
#: 環境ごとの機微の置き場は LAPS_SECRET_TREES（: 区切り）で足す。
_SECRET_TREES = tuple(p for p in os.environ.get("LAPS_SECRET_TREES", "").split(os.pathsep) if p)
_RECURSIVE_READERS = {"grep", "egrep", "fgrep", "rg", "find", "ls", "cat", "tail", "head"}


#: 道具 1 回ぶんだけ関所のモードを上書きする（Ask モードの Bash・2026-11-11）。道具は並べて走るので環境変数は書き換えず、
#: 走っている糸ごとに効く ContextVar で持つ。
_MODE_OVERRIDE: "contextvars.ContextVar[str]" = contextvars.ContextVar("bw_policy_mode", default="")
#: Ask モードで、もとの設定が trusted のときだけ立つ。ssh の接続先を縛らない（遠隔の命令は readonly で見る）。
_ANSWER_SSH_ANY: "contextvars.ContextVar[bool]" = contextvars.ContextVar("bw_answer_ssh_any", default=False)


def _mode() -> str:
    o = _MODE_OVERRIDE.get()
    if o:
        return o
    # 公開版の既定は workspace（作業フォルダーの中だけ書ける・sudo や curl は止める）。
    #   広げるときは LAPS_POLICY=trusted か設定の policy。
    m = os.environ.get("LAPS_POLICY", "workspace").strip().lower()
    return m if m in ("readonly", "workspace", "off", "trusted") else "readonly"


def _workspace() -> Path:
    return Path(os.environ.get("LAPS_WORKSPACE", os.getcwd())).resolve()


def _audit_path() -> Path:
    home = Path(str(_lp.home_dir()))
    home.mkdir(parents=True, exist_ok=True)
    return home / "audit.jsonl"


#: 監査ログに書けないことは**静かに起きてはいけない**。実行は続けるが、
#: 端末には1度だけ出す（毎回出すと本来の出力が読めなくなる）。
_AUDIT_WARNED = False


def _audit_warn_once(msg: str) -> None:
    global _AUDIT_WARNED
    if _AUDIT_WARNED:
        return
    _AUDIT_WARNED = True
    try:
        import sys
        sys.stderr.write(f"[policy] {msg}（以後この警告は出しません）\n")
    except Exception:
        pass


def _last_hash(p: Path) -> str:
    if not p.exists():
        return "0" * 64
    try:
        last = p.read_text(errors="replace").rstrip("\n").rsplit("\n", 1)[-1]
        return json.loads(last).get("hash", "0" * 64) if last else "0" * 64
    # ★`except (ValueError, OSError)` では足りない: 行が `null` や `123` だと
    #   json.loads は通り、`.get` で AttributeError になる（壊れたログ1行で
    #   全ての実行が止まる）。鎖が切れていることはファイルに残る＝見える。
    except Exception:
        return "0" * 64


def audit(kind: str, detail: dict, decision: str, reason: str = "") -> None:
    """Append one hash-chained record. Never raises: losing the command is worse
    than losing the log line, but a broken chain stays visible in the file.

    ★2026-09-02: 「Never raises」は**書いてあるだけ**だった。raise しうる箇所が
      3 つ外に出ていて、どれも `authorize_command` → 道具の外まで抜ける:
        * `_audit_path()` の `mkdir`（LAPS_HOME が読取専用/ファイル/権限なし）
        * `json.dumps(entry)`（detail に Path など JSON にできない値が入る）
        * `_last_hash` の json 行が dict でないとき（`null` → AttributeError）
      抜けると全ての Bash/Read/Write が「Error in Bash: …」で落ちる——
      **記録が取れないことが実行そのものを止める**、この設計が避けたかった側。
    """
    try:
        p = _audit_path()
    except OSError as e:
        _audit_warn_once(f"監査ログの置き場所を作れない: {e!r}")
        return
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": _mode(),
        "kind": kind,
        "decision": decision,
        "reason": reason,
        "detail": detail,
        "prev": _last_hash(p),
    }
    try:
        # default=str: detail に Path や例外が混ざっても記録を落とさない
        # （TypeError で外へ抜けると、記録できないことが実行を止める）。
        body = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
        entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        _audit_warn_once(f"監査ログに書けない: {e!r}")


#: 断られたときに指し示す、**同じ目的への通っている道**。
#: ★通る道が実在するものだけ。書けないものは書かない。
_ALTERNATIVES = {
    # 外の内容を取る → WebFetch は通っている（本物の別ルート）
    "curl": "外の内容を取りたいなら WebFetch ツールを使ってください（許可されています）",
    "wget": "外の内容を取りたいなら WebFetch ツールを使ってください（許可されています）",
    # 状態を**見るだけ**なら道がある（操作したい場合は無いので、条件付きで書く）
    "systemctl": "状態を見たいだけなら SystemState ツールが使えます（操作はできません）",
}


def _alternative_for(prog: str) -> str:
    """`prog` を断ったときに添える案内。無ければ空（**でっち上げない**）。

    ★拒否文が理由だけだと、モデルは「この環境ではできない」と読んで**止まる**。
      実測: `python3 -c` を断られた走行が「python3: command not found」という
      **どの道具も返していない出力**を自分の言葉で書き、Python は使えないと結論して
      終了した。`python3 file.py` は最初から通っていた。
      開いている扉を名指しするのは譲歩ではない。扉は元から開いていて、
      モデルからは見えていなかっただけ。

    ★**無い道は書かない。** sudo / apt / ssh のように通る道が存在しない拒否は、
      理由だけで終わらせる。それらしい案内を足すと、存在しない扉を探しに行く。
      （この方針は `test_no_alternative_is_better_than_a_wrong_one` が守っている。
       2026-08-19 に私が sudo/apt にも案内を足そうとして、その試験に止められた。）
    """
    return _ALTERNATIVES.get(prog, "")


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    # What the model should do instead, when a permitted route to the same goal
    # exists. A refusal that only says no is read as "this environment cannot do
    # it": the bench caught a run that was denied `python3 -c`, wrote in its own
    # words that `python3: command not found` — an error no tool had returned —
    # concluded Python was unavailable, and stopped, without ever trying
    # `python3 file.py`, which the gate allows. Naming the open door is not a
    # concession; the door was already open, and the model could not see it.
    alternative: str = ""

    def message(self) -> str:
        m = f"policy denied: {self.reason}"
        return f"{m}\n→ 代わりにこうできます: {self.alternative}" if self.alternative else m


#: ヒアドキュメントの始まり（`<<EOF`・`<< 'EOF'`・`<<-"EOF"`・`<<\EOF`）。`<<<`（ヒア文字列）は含めない。
_HEREDOC_START = re.compile(r"(?<!<)<<(?!<)-?\s*\\?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
#: ヒアドキュメントの本文を「命令として走らせる」側のプログラム（本文は命令のまま検査する）。
_HEREDOC_RUNS_BODY = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "ssh", "su"})


def _split_commands(command: str, *, heredocs: bool = True) -> list[list[str]]:
    """Split a shell string into argv lists, one per pipeline stage / separator.

    Unparseable input is an error, not a best guess — the caller refuses it.
    heredocs=False: ヒアドキュメントの本文を読み飛ばさない（本文を先に外してから呼ぶ側＝oc/disclose）。
    """
    cmds: list[list[str]] = []
    # ★ヒアドキュメントの本文は命令ではない（2026-10-22 Terminal-Bench の診断の走行）。`cat > f.c << 'EOF'` や
    #   `python3 << 'PYEOF'` の本文の `a,b=0,1`・`header_size = 16` を段として読み、プログラム名が空になって
    #   trusted でも「プログラム名を特定できない」で拒否していた（10 課題で 7 回・自作の模擬器を 3 回とも走らせられなかった）。
    #   本文は終わりの印まで読み飛ばす。ただしシェルや ssh へ流し込む本文は実際に命令として走るので、従来どおり段として読む
    #   （自己保護が本文の `rm` を見るため）。
    pending: list[str] = []
    # A newline separates commands just as `;` does, but shlex treats it as
    # ordinary whitespace — so a two-line command used to parse as one argv and
    # everything after line 1 went unchecked. Split lines first, then tokenise.
    for line in command.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        if not line.strip():
            continue
        if heredocs and "((" not in line:
            tags = [m.group(2) for m in _HEREDOC_START.finditer(line)]
            if tags and not any(os.path.basename(w) in _HEREDOC_RUNS_BODY for w in re.split(r"[\s;|&()]+", line)):
                pending = tags
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)  # raises ValueError on unbalanced quotes
        cur: list[str] = []
        for t in tokens:
            if t in ("|", "||", "&&", ";", "&", "|&"):
                if cur:
                    cmds.append(cur)
                cur = []
            else:
                cur.append(t)
        if cur:
            cmds.append(cur)
    return cmds


def _family(name: str) -> str:
    """Program name without a version suffix: python3.12 -> python, gcc-12 -> gcc."""
    return re.sub(r"[-_.]?\d[\d.]*$", "", name) or name


#: `VAR=value cmd` の前置で、**どのプログラムでも任意コード実行になる**もの。
#: `_program()` は前置を中身を見ずに読み飛ばすので、許可リストに載った任意の
#: プログラムがそのまま器になる。実測（2026-09-02・readonly で最後まで実行）:
#:     LD_PRELOAD=/tmp/evil.so ls /tmp   → **任意コードが走り**作業フォルダの外に書いた
#: これは `sort -o` のような**プログラムごとの機能**とは種類が違い（あれは列挙が
#: 閉じないので罠を足さないと決めた）、**全プログラムに一度に効く1つの仕組み**なので
#: 1 本の規則で閉じる。⚠ 誤爆の実測: 許可済み 20,337 件の前置は
#: `a=`(56) `PYTHONPATH=`(10) `LAPS_TELEMETRY_URL=`(1) だけで、**下の名前は 0 件**。
#: PYTHONPATH は入れない（この現場の bench の走らせ方そのもので、10 件は実在の作業）。
_ENV_PREFIX_DENIED = frozenset({
    "LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "LD_DEBUG_OUTPUT",
    "GLIBC_TUNABLES", "BASH_ENV", "ENV", "IFS", "SHELLOPTS", "BASHOPTS",
    "PERL5OPT", "PERL5LIB", "PYTHONSTARTUP", "NODE_OPTIONS", "RUBYOPT",
})


def _env_prefixes(argv: list[str]) -> list[str]:
    """`VAR=value` names written in front of the program."""
    out = []
    for tok in argv:
        if "=" in tok and not tok.startswith("/") and not tok.startswith("-"):
            out.append(tok.split("=", 1)[0])
            continue
        break
    return out


def _program(argv: list[str]) -> str:
    """First real program name, skipping env-var assignments.

    A path is returned as-is (not basename'd) so the caller can refuse it:
    `/tmp/ls` symlinked to sudo would otherwise authorise as `ls`.

    ⚠ 読み飛ばした前置そのものが危険な場合がある（`_ENV_PREFIX_DENIED`）。
      呼び出し側が別に検査する。
    """
    for tok in argv:
        if "=" in tok and not tok.startswith("/") and not tok.startswith("-"):
            continue  # VAR=value prefix
        return tok
    return ""


def _redirect_targets(argv: list[str]) -> list[str]:
    """Files this argv would write via > or >>."""
    targets = []
    for i, tok in enumerate(argv):
        if tok in (">", ">>", "1>", "2>", "&>", ">|") and i + 1 < len(argv):
            targets.append(argv[i + 1])
    return targets


def _secret_read_verdict(argv: list[str], prog: str, cwd: str | None) -> Verdict | None:
    """Refuse commands that would read credential files. See _SECRET_READ."""
    # A cwd inside a secret tree makes every bare filename and glob dangerous:
    # `cat *.md` there needs no path at all.
    if cwd:
        rc = os.path.realpath(cwd)
        for tree in _SECRET_TREES:
            if rc == tree or rc.startswith(tree + "/"):
                return Verdict(False, f"作業ディレクトリが機微ツリー内（{tree}）のため不可")

    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        target = tok if tok.startswith("/") else os.path.join(cwd or os.getcwd(), tok)
        rt = os.path.realpath(target)
        if any(marker in rt for marker in _SECRET_READ):
            return Verdict(False, f"資格情報を含むパスの読み取りは不可（{tok}）")
        # A recursive reader aimed at a secret tree, or at any ancestor of one,
        # reaches the secrets from above — the prefix check must go both ways.
        if prog in _RECURSIVE_READERS:
            for tree in _SECRET_TREES:
                if rt == tree or rt.startswith(tree + "/") or tree.startswith(rt.rstrip("/") + "/"):
                    return Verdict(
                        False,
                        f"`{prog}` が機微ツリー（{tree}）に到達しうるため不可（{tok}）",
                    )
    return None


# ── ssh（明示的に開いたときだけ通る） ────────────────────────────────
#
# ★2026-08-11 オーナー要望「今は SSH もできない」。ラボの作業はほぼ SSH 越しなので、
# 使えないと運用エージェントとして成立しない。ただし許可リストに ssh を足すだけでは
# **`ssh host 'rm -rf /'` が素通りする**（ゲートは手元のプログラム名しか見ないため）。
#
# ここでは3つを同時に要求する:
#   1. 行き先が **明示された許可リスト**（LAPS_SSH_HOSTS）に載っていること
#   2. 遠隔で走らせるコマンドが、**この同じゲートを通ること**（再帰）
#      ＝手元で拒む操作は、向こうでも拒む
#   3. **手元でプログラムを起こせる指定**を含まないこと
#      （`-o ProxyCommand=...` は ssh が手元で任意のコマンドを実行する。
#        `-F` で作業ディレクトリ内の設定を読ませると、そこに ProxyCommand を
#        書いて同じことができるので、作業ディレクトリ内の設定は拒む）
#
# 許可リストが空なら従来どおり全拒否。**既定では開かない。**

_SSH_DANGEROUS_OPTS = ("proxycommand", "localcommand", "permitlocalcommand")


def _ssh_hosts() -> set[str]:
    raw = os.environ.get("LAPS_SSH_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _ssh_verdict(argv: list[str], cwd: str | None) -> "Verdict":
    # ★trusted では通す（2026-08-23・オーナーの目標「ラボの全システムを管理」）。
    #   trusted は「何も止めない」とオーナーが決めたモード。scp も sudo も
    #   systemctl も通るのに ssh だけ止まるのは不整合で、敵対レビューも
    #   「SSH を許可する手段が製品のどこにも無い」と指摘していた（P1-18）。
    #   ラボの作業はほぼ SSH 越しなので、これが通らないと目標に届かない。
    #   ★ProxyCommand 等の「手元で任意実行」の検査は trusted でも残す——
    #     それは遠隔の話ではなく、手元の境界（policy）の話だから。
    #   workspace / readonly / off は従来どおり許可リスト制（既定では開かない）。
    #   ★off は「手元の破壊は許すが外へ出る経路は拒む」という別の約束を持つ
    #     （test_off_mode_still_refuses_egress）。trusted だけが外へも出られる。
    mode = _mode()
    hosts = _ssh_hosts()
    any_host = _ANSWER_SSH_ANY.get()      # Ask モード（もとが trusted）: 接続先は縛らず、遠隔の命令を readonly で見る
    if not hosts and mode != "trusted" and not any_host:
        return Verdict(False, "`ssh` は既定で禁止。使うなら接続先を明示して許可すること")

    rest = argv[1:]
    host: str | None = None
    remote: list[str] = []
    i = 0
    # 値を取るオプション。ここに無い `-x` 形式は単独フラグとして読む。
    takes_value = {"-i", "-p", "-l", "-o", "-F", "-b", "-c", "-D", "-e", "-I",
                   "-J", "-L", "-m", "-O", "-Q", "-R", "-S", "-W", "-w"}
    while i < len(rest):
        a = rest[i]
        if host is None and a.startswith("-"):
            # `-oProxyCommand=...` のように詰めて書く形も見る
            val = None
            if a in takes_value:
                if i + 1 >= len(rest):
                    return Verdict(False, f"ssh の `{a}` に値が無い（解析できないため不可）")
                val = rest[i + 1]
                i += 2
            else:
                val = a[2:] if len(a) > 2 else None
                i += 1
            probe = f"{a}{val or ''}".lower()
            if any(d in probe for d in _SSH_DANGEROUS_OPTS):
                return Verdict(False, "ssh の ProxyCommand/LocalCommand は手元で任意のコマンドを実行するため不可")
            if a == "-F" and val:
                # 作業ディレクトリの中の設定は、エージェント自身が書ける＝
                # そこに ProxyCommand を書けば上の判定を回避できる。
                try:
                    cfg = Path(val).expanduser().resolve()
                    ws = Path(cwd).resolve() if cwd else _workspace()
                    if cfg == ws or ws in cfg.parents:
                        return Verdict(False, "作業ディレクトリ内の ssh 設定は使えない（ProxyCommand を書けるため）")
                except OSError:
                    return Verdict(False, "ssh の設定パスを解決できない（fail-closed）")
            continue
        if host is None:
            host = a
            i += 1
            continue
        remote.append(a)
        i += 1

    if host is None:
        return Verdict(False, "ssh の接続先が読み取れない（fail-closed）")
    target = host.split("@", 1)[-1].strip().lower()
    # trusted は接続先を縛らない（上の注記）。他のモードは許可リスト制。
    if mode != "trusted" and target not in hosts and not any_host:
        return Verdict(False, f"`{target}` は許可された接続先ではない（許可: {', '.join(sorted(hosts))}）")
    if not remote:
        # 対話シェルは端末が無いので、開いても固まるだけ。用途も無い。
        return Verdict(False, "ssh に遠隔コマンドが無い（対話シェルは開けない）",
                       alternative=("`ssh ホスト 'コマンド'` の形で、1回に1つずつ実行してください"))

    inner = " ".join(remote)
    v = check_command(inner, cwd=None)
    if not v.allowed:
        return Verdict(False, f"遠隔側で拒否: {v.reason}")
    return Verdict(True, "")


def check_command(command: str, cwd: str | None = None) -> Verdict:
    """Authorise a shell command string. Fail closed.

    `cwd` is the directory the command would run in. It matters: the same
    `cat *.md` is harmless in a project directory and a credential dump inside
    a secrets folder, and the gate cannot tell without it.
    """
    mode = _mode()

    if not command.strip():
        return Verdict(False, "empty command")

    # Redirection is handled per stage below, where the target is known.
    # A blanket string test here used to reject `2>/dev/null` as well, which
    # writes nothing and is how stderr is silenced everywhere.

    # Command substitution hides the real program from this parser.
    # ★trusted では通す（2026-08-30・オーナー指示）。このモードは自身の説明で
    #   「**何も止めない**」と定義されており、`rm -rf` も `sudo` も通る。
    #   そこで `kill $(cat server.pid)` のような**普通のシェル構文だけ**が
    #   通らないのは、守っている物が無いのに使い勝手だけを削っている状態。
    #   実測: 4 走行で 16 回踏み、そのたびに 2 回に分ける書き直しを強いていた。
    #   ★readonly / workspace では従来どおり拒む。あちらは「中身を検査して
    #     読み取りだけに保つ」ことがモードの目的なので、検査できない構文は通せない。
    if mode != "trusted" and ("$(" in command or "`" in command):
        return Verdict(False, "コマンド置換は解析できないため不可（fail-closed）",
                       alternative=("`$(…)` や逆引用符を使わず、**2回に分けて**ください。"
                                    "1回目で値を取り、その値を見てから2回目を組み立てる"))

    try:
        stages = _split_commands(command)
    except ValueError as e:
        # ★trusted は「何も止めない」モード。解析できない構文（複数行の `python3 -c "…"` で引用符が
        #   入れ子になる等）だけ拒むのは、守っている物が無いのに使い勝手を削っている状態
        #   （上の `$(` と同じ理屈）。実測（2026-09-03 dogfood）: 記録 15,645 道具結果のうち 43 件が
        #   この拒否で、全部 trusted の走行だった。readonly / workspace は従来どおり fail-closed。
        if mode == "trusted":
            return Verdict(True, f"mode=trusted（構文は解析できないがそのまま通す: {e}）")
        return Verdict(False, f"解析できないため不可（fail-closed）: {e}",
                       alternative=("引用符の対応を確かめて、**1コマンドずつ**送ってください。"
                                    "複雑な1行より、短い数行のほうが通ります"))
    if not stages:
        return Verdict(False, "解析結果が空")

    allowed = set(_READ_ONLY)
    if mode == "workspace":
        allowed |= _WORKSPACE_EXTRA

    # ★`cwd` は段をまたいで変わる（2026-09-02）。`cd` は _READ_ONLY に入っている
    #   ので readonly でも通り、ゲートは**最初の cwd** で検査し、シェルは **cd 後の
    #   場所**で実行していた。引数の cwd は (1)書き込み先の封じ込め (2)機微ツリーの
    #   判定 (3)相対パスの解決 の3つの土台なので、これは denylist の抜けではなく
    #   **ゲートが自分で検査した前提が実行時に成立していない**という誤り。実測:
    #     workspace  `cd /外 && rm -rf victim`   → 外のディレクトリが実際に消えた
    #     readonly   `cd <機微の置き場> && ls | head` → 機微ツリーを走査できた
    #                （同じ内容を `grep -rl … <機微の置き場>` で直接やると
    #                  約束どおり拒否される。`cd` を挟むだけで通っていた）
    #   段ごとに cwd を持ち回り、`cd` を見たら以降の段の cwd を差し替える。
    cur_cwd = cwd

    for argv in stages:
        raw = _program(argv)
        if not raw:
            # ★代入だけの段（`N=5`・`x=1; echo $x`）はプログラムを走らせない。trusted でも拒否していた
            #   （2026-10-22 の診断で見つけた・here document の外でも起きる）。readonly / workspace は従来どおり。
            if mode in ("trusted", "off") and argv and all("=" in t for t in argv):
                continue
            return Verdict(False, "プログラム名を特定できない")
        # A path is allowed only when it still resolves to the program it
        # claims to be. Refusing every absolute path (the first version of this
        # rule) also blocked `/path/to/.venv/bin/python`, which left the agent
        # unable to run its own tests — measured as a direct cause of S1
        # failures. Resolving symlinks keeps the attack closed: `/tmp/ls`
        # pointed at sudo resolves to `sudo` and no longer matches `ls`.
        prog = os.path.basename(raw)
        if "/" in raw:
            # Resolving the symlink only works for paths on this machine. A
            # command aimed at another host, or at a file that does not exist
            # yet, resolves to itself and the check silently passes — which is
            # exactly the case the safety suite caught: /tmp/x/ls pointing at
            # sudo was authorised as `ls` because the probe host's filesystem
            # is not visible from here.
            # So a path is trusted only when it resolves to the same program
            # *and* lives somewhere system binaries live. Everything else is
            # refused; a legitimate venv interpreter still passes because it
            # resolves into /usr.
            real_path = os.path.realpath(raw)
            real = os.path.basename(real_path)
            trusted_root = any(real_path.startswith(p) for p in
                               ("/usr/", "/bin/", "/sbin/", "/opt/"))
            # ★trusted ではこの検査を飛ばす（2026-08-23・開発タスクで実測）。
            #   作業フォルダに自分で書いた `./start.sh` が
            #   「パス指定のプログラムを信用できない」で拒否された。
            #   trusted は「何も止めない」とオーナーが決めたモードで、
            #   自分で書いたスクリプトを実行できないと開発タスクが成立しない。
            #   この検査が守るのは「偽の ls を /tmp に置いて sudo を呼ぶ」形で、
            #   それは allowlist のあるモード（readonly / workspace）の話。
            if _mode() != "trusted" and (_family(real) != _family(prog) or not trusted_root):
                return Verdict(
                    False,
                    f"パス指定のプログラムを信用できない（`{raw}` → `{real_path}`）",
                )
        if prog == "cd":
            nxt = _cd_target(argv, cur_cwd)
            if nxt is None:
                # 行き先が解けない `cd`（引数なし＝$HOME・`cd -`・変数展開）。
                # 以降の段をどの cwd で検査してよいか分からない＝fail-closed。
                # trusted/off は「止めない」モードなので従来どおり素通し。
                if mode in ("readonly", "workspace"):
                    return Verdict(
                        False,
                        f"`cd` の行き先を特定できないため不可（fail-closed）: "
                        f"{' '.join(argv[1:]) or '(引数なし)'}",
                        alternative=("`cd` は行き先を1つ、できれば絶対パスで書いてください"
                                     "（`cd -`・変数展開・引数なしは解決できません）。"
                                     "cwd を変えずに、コマンド側にフルパスを渡すのが確実です"))
            else:
                cur_cwd = nxt
        if prog == "ssh":
            v = _ssh_verdict(argv, cur_cwd)
            if not v.allowed:
                return v
            continue
        if mode != "trusted":
            for name in _env_prefixes(argv):
                if name.upper() in _ENV_PREFIX_DENIED:
                    return Verdict(
                        False,
                        f"`{name}=` の前置は任意コード実行になるため不可",
                        alternative="環境変数を前に置かず、コマンドだけを送ってください")
        if prog in _ALWAYS_DENIED and mode != "trusted":
            return Verdict(False,
                           f"`{prog}` はこのモードでは禁止（外部到達/昇格/構成変更/別プログラム起動）",
                           alternative=_alternative_for(prog))
        # off stops here. The always-denied floor and the path-spoof and
        # command-substitution guards above hold in every mode — off means "this
        # throwaway host may be broken", not "this host may be used to reach
        # vCenter" (P5: the test host routes to vCenter/ESXi/all GPU boxes). But
        # the allowlist, arg-traps, redirection and write-target containment are
        # deliberately dropped: that is what off is for.
        # trusted は「利用者が自分の機械で、全部やらせると決めた」状態。
        # ★何も止めないが、**すべて記録する**（audit）。止めない代わりに、
        #   後から「何が走ったか」を必ず辿れるようにするのが、この設計の対価。
        if mode in ("off", "trusted"):
            # ★自己保護だけは残す——**そう書いてあるのに、書いていなかった**
            #   （2026-09-02）。この 2 モードは次の行で全部を飛ばしていたので、
            #   実際に自己保護を掛けていたのは check_path（Write/Edit/リダイレクト
            #   の先）だけ。Bash 経由の `cp evil.py …/policy.py`・
            #   `rm ~/.blackwall/audit.jsonl`・`mv PROMPTS/… /tmp` は
            #   **出荷既定の trusted で通っていた**。モジュール冒頭の約束
            #   （「自己保護は trusted でも残す」）と test_policy_trusted の
            #   `test_自己保護だけは残る` は check_path しか見ておらず、
            #   片側で直して片側に残した形になっていた。
            #   判定は check_path に一本化する（2 箇所で判定すると必ず食い違う）。
            #   off/trusted の check_path は自己保護以外は素通しなので、
            #   「何も止めない」という約束はそのまま。
            #   ★2026-09-02（第2版）: 最初の版は `_redirect_targets + _write_targets`
            #     しか見ておらず、`_write_targets` は `_PATH_WRITERS`（11 個）限定
            #     だったので `sed -i` / `dd of=` / `tee` / `truncate` / `install` /
            #     `ln` は**書き手として存在しない**扱いになっていた。14 形を実際に
            #     check_command へ通したところ 10 形が許可された（glob で名前を隠す
            #     `polic?.py`、親ごと消す `rm -rf ~/.blackwall` を含む）。
            #     ＝「自己保護は trusted でも残す」は、直した直後もまだ嘘だった。
            _v = _self_protect_verdict(prog, argv, cur_cwd)
            if not _v.allowed:
                return _v
            continue
        if prog not in allowed:
            return Verdict(False, f"`{prog}` は許可リストに無い（mode={mode}）",
                           alternative=_alternative_for(prog))

        # An allowlisted program is still refused when handed an argument that
        # makes it write or exec — `sed -i`, `find -fprintf`, `git -c`.
        for trap in _ARG_TRAPS.get(prog, ()):
            if any(a == trap or a.startswith(trap + "=") for a in argv[1:]):
                return Verdict(False, f"`{prog} {trap}` は書き込み/実行になるため不可",
                                   alternative=(
                                       "ディレクトリの容量を知りたいなら find は要りません: "
                                       "`du -sh 対象/*` または `du -h --max-depth=1 対象` "
                                       "（どちらも許可されています）。"
                                       "検索だけなら -exec を外してください。"
                                       "見つけた対象への操作は1件ずつ行ってください"))
                # The first version of this said only "drop -exec", which is
                # true and useless: the run wanted sizes, and dropping -exec
                # loses them. Measured consequence — of 200 grounded runs, 80
                # abstained on the directory-size item, every one of them tried,
                # and all 97 attempts were `find … -exec du -sh {} +`, refused
                # every time. The 40% abstention rate that V5 recorded was this
                # gate, not the model giving up, and `du -sh dir/*` was
                # permitted the whole time. Naming an open door is not enough if
                # it opens somewhere else.

        # sed writes files from inside its script too: `sed -n 'w /path' file`
        # needs no flag at all, so the script text has to be inspected.
        if prog == "sed" and any(
            _SED_WRITE_RE.search(a) for a in argv[1:] if not a.startswith("-")
        ):
            return Verdict(False, "sed のスクリプト内 w/W（ファイル書き出し）は不可",
                                  alternative="sed の出力を Write ツールでファイルに書いてください")

        # awk executes shell through system()/print|"cmd"; there is no flag to
        # switch that off, so the program text itself has to be refused.
        if prog == "awk" and any(
            tok in a for a in argv[1:] for tok in ("system(", '"|', '| "', "|&")
        ):
            return Verdict(False, "awk の system()/パイプ実行は不可",
                                  alternative="awk は整形だけに使い、実行は別の Bash 呼び出しに分けてください")

        # Redirection: refused outright in readonly, bounded by the workspace
        # otherwise. Without this, `echo x > /etc/cron.d/y` passed in workspace.
        # A redirect operator with nothing after it cannot be authorised.
        if any(t in (">", ">>", "1>", "2>", "&>", ">|") for t in argv) and not _redirect_targets(argv):
            return Verdict(False, "リダイレクト先を特定できない（fail-closed）")
        for target in _redirect_targets(argv):
            # `2>/dev/null` is how everyone silences stderr; it writes nothing.
            # Refusing it made the gate look arbitrary and cost real tasks —
            # measured: `du -sh /home/user/project/* 2>/dev/null | sort -hr` was
            # denied, so the agent could not answer "which directories are
            # large" at all.
            if target in ("/dev/null", "/dev/stderr", "/dev/stdout"):
                continue
            if mode == "readonly":
                return Verdict(False, "readonly モードでリダイレクト（書き込み）は不可",
                                  alternative="Write ツールを使うか、書き込みが要るなら workspace モードを求めてください")
            v = check_path(target, write=True)
            if not v.allowed:
                return Verdict(False, f"リダイレクト先が許可されない: {v.reason}")

        # Reading credentials is a leak even though nothing is written.
        secret = _secret_read_verdict(argv, prog, cur_cwd)
        if secret is not None:
            return secret

        # The command path must not reach the agent's own permissions either;
        # check_path only ever saw Write/Edit, so `cp x policy.py` slipped by.
        # ★2026-09-02: ここは**読みまで断っていた**。`cat …/policy.py`・
        #   `wc -l …/permission.py`・`cat ~/.blackwall/settings.json` が
        #   readonly/workspace で拒否される（docstring の約束は "refused for
        #   writing"、棚卸し報告も「読みは通る」と書いていた＝報告者は trusted で
        #   確かめたのでこの枝に到達しなかった）。判定は off/trusted と同じ1本に
        #   まとめる（2箇所で判定すると必ず食い違う）。
        _v = _self_protect_verdict(prog, argv, cur_cwd)
        if not _v.allowed:
            return _v

        rest = [a for a in argv[1:] if not a.startswith("-")]
        verb = rest[0] if rest else ""
        if prog == "systemctl":
            # `systemctl --failed` and `systemctl --version` carry no verb at
            # all; the earlier rule read that as "(動詞なし)" and refused, so a
            # perfectly read-only status query was blocked. Flags that only
            # read are listed here; a bare `systemctl` (which pages all units)
            # is fine too. Anything with an actual verb still has to be in the
            # read set.
            _READ_FLAGS = ("--failed", "--version", "--help", "--all", "--type",
                           "--state", "--no-pager", "--no-legend", "-a")
            if verb:
                if verb not in _SYSTEMCTL_READ_VERBS:
                    return Verdict(False, f"systemctl {verb} は状態を変えるため不可",
                                       alternative="読み取りなら systemctl status / --failed / show が使えます")
            elif not all(
                a.startswith("-") and any(a.startswith(f) for f in _READ_FLAGS)
                for a in argv[1:]
            ):
                return Verdict(False, "systemctl に読み取り以外の指定がある")
        if prog == "git" and verb not in _GIT_READ_VERBS:
            return Verdict(False, f"git {verb or '(動詞なし)'} は読み取り動詞ではない",
                           alternative="git status / log / diff / show / rev-parse は使えます")
        if prog == "git" and verb in ("push", "fetch", "pull", "clone"):
            return Verdict(False, f"git {verb} は外部到達のため不可")
        if prog == "git" and verb == "config" and not any(
            a in ("--get", "--list", "-l", "--get-all") for a in argv[1:]
        ):
            # `git config core.pager 'sh -c id'` turns every later git call into
            # arbitrary execution.
            return Verdict(False, "git config は --get/--list のみ可")
        if prog == "git" and verb == "stash" and (len(rest) < 2 or rest[1] not in ("list", "show")):
            return Verdict(False, "git stash は list/show のみ可（作業ツリーを壊すため）")
        if prog in _INTERPRETERS and any(a in ("-c", "-e") for a in argv[1:]):
            return Verdict(False, f"{prog} の -c/-e インライン実行は不可",
                           alternative=f"Write でスクリプトを保存してから `{prog} ファイル名.py` "
                                       f"で実行してください（ファイル指定の実行は許可されています）")

        # Where a write-capable program is pointed. Nothing above asked.
        #
        # check_path has enforced workspace containment since the beginning, and
        # redirects and the Write/Edit tools went through it — so the gate looked
        # closed. The programs added by _WORKSPACE_EXTRA never did, and an
        # adversarial review of the plan found the consequence by simply asking:
        # `rm -rf /etc/cron.d`, `chmod -R 777 /srv`, `touch /root/pwned` and
        # `mv blackwall/agent.py /tmp/x` were all permitted in workspace mode —
        # the mode every development benchmark in this project runs in, on this
        # machine.
        #
        # The docstring above blames interpreters for workspace being porous.
        # That was measured and is true, but it is not the whole story and it let
        # this sit: the same commands are permitted with
        # LAPS_NO_INTERPRETERS=1, which the docstring offers as the stricter
        # shape. A gate that authorises a program without looking at what it was
        # aimed at is not checking anything.
        for arg in _write_targets(prog, argv[1:]):
            # Relative to the command's directory, not this process's. check_path
            # resolves through Path.resolve(), which would otherwise anchor `../x`
            # and a bare `*` wherever the agent happens to be running from.
            target = arg if os.path.isabs(arg) else os.path.join(cur_cwd or os.getcwd(), arg)
            v = check_path(target, write=True)
            if not v.allowed:
                return Verdict(False, f"`{prog}` の対象 `{arg}` への書き込みは不可: {v.reason}",
                               alternative="作業ディレクトリ内のパスを指定してください")

        # ★interpreters の穴：`python3 evil.py` で evil.py が workspace の外にあると
        # 抜けられる。-c/-e のインライン実行は上で止めているが、ファイル指定の実行は
        # 対象が workspace 内にあることを確認しないままだった。workspace モード時のみ
        # ファイル引数を検査し、外に出たら拒む（readonly は readonly で止まる）。
        if mode == "workspace" and prog in _INTERPRETERS:
            for arg in argv[1:]:
                if arg.startswith("-") or "=" in arg:
                    continue
                if not arg:
                    continue
                # ファイルとして扱う引数：.py/.js/.ts/.sh/.rb/.pl/.php/.go/.rs/.c/.cpp/.h/.java/.kt/.scala/.r/.lua/.yaml/.yml/.json/.xml/.toml/.ini/.cfg/.conf/.txt/.md/.csv/.sql/.html/.css/.scss/.less/.svg/.png/.jpg/.jpeg/.gif/.bmp/.webp/.ico/.woff/.woff2/.ttf/.otf/.eot/.map/.lock/.env/.gitignore/.dockerignore/.editorconfig/.prettierrc/.eslintrc/.babelrc/.npmrc/.yarnrc/.pypirc/.pip.conf/.gitconfig/.sshconfig/.vimrc/.emacs/.bashrc/.zshrc/.profile/.bash_profile/.zsh_profile/.bash_logout/.zsh_logout/.profile.d/.bash_aliases/.zsh_aliases/.gitattributes/.gitmodules/.gitkeep/.gitkeep/.gitkeep/で始まるか、パスに/が含まれるか、拡張子を持つ
                has_ext = "." in os.path.basename(arg) and not arg.endswith(".")
                is_path = "/" in arg
                if has_ext or is_path:
                    target = arg if os.path.isabs(arg) else os.path.join(cur_cwd or os.getcwd(), arg)
                    v = check_path(target, write=True)
                    if not v.allowed:
                        return Verdict(
                            False,
                            f"`{prog}` の対象 `{arg}` は作業場外のため不可: {v.reason}",
                            alternative="作業ディレクトリ内のファイルを指定してください")

    return Verdict(True, f"mode={mode}")



# Programs whose arguments name something they will create, move, or destroy.
_PATH_WRITERS = frozenset({
    "rm", "rmdir", "mkdir", "touch", "cp", "mv", "chmod", "tar", "unzip",
    "gzip", "gunzip",
})
# chmod's first non-flag argument is a mode, not a path.
_CHMOD_MODE_RE = re.compile(r"^(?:[0-7]{3,4}|[ugoa]*[+\-=][rwxXstugo]*)$")


def _write_targets(prog: str, args: list[str]) -> list[str]:
    """The arguments a write-capable program will write to.

    Sources are separated from destinations only where getting it wrong would
    block ordinary work: `cp outside/file .` reads the source, and refusing it
    would stop the agent copying anything into its workspace. Everything else is
    treated as a write, including `mv`'s sources — moving a file out of /root
    deletes it from /root, whatever the destination is.
    """
    if prog not in _PATH_WRITERS:
        return []
    positional = [a for a in args if not a.startswith("-")]
    if prog == "chmod" and positional and _CHMOD_MODE_RE.match(positional[0]):
        positional = positional[1:]
    # `tar czf out.tgz dir` — tar's first argument may be an option bundle with
    # no dash, and treating `czf` as a path denies every ordinary archive call.
    if prog == "tar" and positional and re.fullmatch(r"[A-Za-z]+", positional[0]):
        positional = positional[1:]
    if prog == "cp" and len(positional) > 1:
        return positional[-1:]          # only the destination is written
    return positional


def _cd_target(argv: list[str], cwd: str | None) -> str | None:
    """Where a `cd` stage lands, or None when it cannot be resolved here.

    Returning None is not "no change" — the caller refuses (fail-closed) in the
    modes that have something to protect, because it no longer knows which
    directory the *next* stage will run in.
    """
    args = [a for a in argv[1:] if not a.startswith("-")]
    if len(args) != 1:
        return None                      # bare `cd` (→ $HOME), or `cd a b`
    tgt = os.path.expanduser(args[0])
    if tgt == "-" or any(c in tgt for c in "$*?`"):
        return None                      # `cd -`, variable, glob
    base = cwd or os.getcwd()
    return os.path.realpath(tgt if os.path.isabs(tgt) else os.path.join(base, tgt))


# ── 自己保護（権限定義・プロンプト・監査ログ） ──────────────────────────────
#
# 判定は「名前が似ている」ではなく「**このプロセスが実際に使っているファイル**に
# 解決する」で行う。理由は _SELF_PROTECTED_ANYWHERE の上のコメントを参照。

def _self_files() -> tuple[str, ...]:
    """Absolute paths of the agent's own leash and record, as it is running now."""
    here = os.path.dirname(os.path.realpath(__file__))
    home = _self_home()
    return tuple(os.path.realpath(x) for x in (
        os.path.join(here, "policy.py"),
        os.path.join(here, "permission.py"),
        os.path.join(home, "settings.json"),
        os.path.join(home, "audit.jsonl"),
        # ★postedit.json は「編集のたびに走るコマンド」を決める。実行自体は
        #   authorize_command を通るが、trusted はそこを通す側なので、
        #   これを書けることは「毎回の編集の後に任意のコマンドを走らせる」に等しい
        #   （postedit.py:36 が読み、347 行で shell=True で走らせる）。
        os.path.join(home, "postedit.json"),
    ))


def _self_home() -> str:
    return str(_lp.home_dir())


def _self_trees() -> tuple[str, ...]:
    """Directories whose whole contents decide what the agent is and may run.

    ★hooks/ と plugins/ を 2026-09-02 に足した。どちらも「エージェントが自分で
      書いたファイルが、次の起動で**このゲートを通らずに**実行される」経路で、
      CVE-2025-53773 と同じ形（injected text が自分の自動承認を有効化した）:
        hooks.py:43     `subprocess.run([script])` — policy を通らない
        plugin_loader.py:50 `exec_module(mod)`     — **プロセス内**で実行される
      前者は 2026-08-16 の別セッションの報告（claudecode-gap §hooks）で
      「未修正」と名指しされたまま残っていたもの。出荷構成では LAPS_HOME が
      作業フォルダと別根なので届かないが、bench/scratch 構成
      （`src/bench/real_tasks.py:261` は LAPS_HOME を workspace の中に置く）
      では**いま届く**。＝自分のベンチだけが穴を開けている、という報告どおりの形。
      ⚠ 誤爆の実測: 許可済み edit/write/bash 37,967 件のうち hooks/ plugins/
        postedit.json への書き込みは **0 件**（2026-08-07〜09-02）。
        利用者が自分で置くもので、エージェントが書く用は無い。
    """
    home = _self_home()
    out = [os.path.join(home, "hooks"), os.path.join(home, "plugins")]
    d = os.environ.get("LAPS_PROMPT_DIR")
    if d:
        out.append(d)
    else:
        base = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        out += [os.path.join(base, "PROMPTS"), os.path.join(base, "PROMPTS_v2")]
    return tuple(os.path.realpath(x) for x in out)


def _self_protected_path(path: str, *, ancestor: bool = False) -> str | None:
    """The protected thing this write target would damage, or None.

    `ancestor=True` adds "the target *contains* a protected file" — only for
    programs that destroy what they are pointed at (`rm -rf ~/.blackwall` never
    names audit.jsonl). It is off for the rest on purpose: `cp x .` inside the
    package directory writes a new file, it does not destroy policy.py, and
    refusing it would be the same over-reach this rewrite is removing.
    """
    rp = os.path.realpath(os.path.expanduser(path))
    for marker in _SELF_PROTECTED_ANYWHERE:
        if marker in rp:
            return marker
    pref = rp if rp.endswith("/") else rp + "/"
    for f in _self_files():
        if rp == f or (ancestor and f.startswith(pref)):
            return f
    for t in _self_trees():
        if rp == t or rp.startswith(t + "/") or (ancestor and t.startswith(pref)):
            return t
    return None


# Programs that create, overwrite or destroy the path they are handed. Wider
# than _PATH_WRITERS, which exists for workspace containment: the 2026-09-02
# measurement found `sed -i`, `dd of=`, `tee`, `truncate`, `install` and `ln`
# writing over the agent's own files precisely because they were not in that
# 11-item set.
_SELF_WRITER_PROGS = frozenset({
    "rm", "rmdir", "mkdir", "touch", "cp", "mv", "chmod", "tar", "unzip",
    "gzip", "gunzip", "install", "ln", "truncate", "tee", "dd", "shred",
    "split", "csplit", "patch", "unlink", "chown", "chgrp", "sponge", "zip",
})
# Programs that *remove* the named path, so naming a parent is enough.
_SELF_REMOVERS = frozenset({"rm", "rmdir", "mv", "shred", "unlink"})
# Programs that take their real instructions from a string or a file this gate
# never parses. The path cannot be resolved, but an absolute path written
# literally inside the argument still shows up.
_SELF_CODE_RUNNERS = frozenset({
    "python", "python3", "node", "perl", "ruby", "php", "sh", "bash", "zsh",
    "eval", "exec", "make", "pytest",
})


def _writes_by_argument(prog: str, args: list[str]) -> bool:
    if prog in _SELF_WRITER_PROGS:
        return True
    if prog == "sed":
        return any(a.startswith("-i") or a.startswith("--in-place") for a in args
                   if a.startswith("-")) or any(
            _SED_WRITE_RE.search(a) for a in args if not a.startswith("-"))
    return False


def _self_protect_targets(prog: str, argv: list[str], cwd: str | None) -> list[str]:
    """Absolute paths this stage could write, for self-protection only.

    Deliberately coarser than _write_targets: sources are not separated from
    destinations, `VAR=value` is looked at from both ends, and globs are
    expanded — `cp evil.py …/polic?.py` hid the name from the old check.
    A false positive here costs one refused command that named policy.py.
    """
    toks: list[str] = [t for t in _redirect_targets(argv)
                       if t not in ("/dev/null", "/dev/stderr", "/dev/stdout")]
    if _writes_by_argument(prog, argv[1:]):
        for tok in argv[1:]:
            if tok.startswith("-"):
                continue
            toks.append(tok)
            if "=" in tok and not os.path.isabs(tok):
                toks.append(tok.split("=", 1)[1])   # dd of=…, --output=…
    out: list[str] = []
    for tok in toks:
        if not tok:
            continue
        t = os.path.expanduser(tok)
        t = t if os.path.isabs(t) else os.path.join(cwd or os.getcwd(), t)
        matches = glob.glob(t) if any(c in tok for c in "*?[") else []
        out.extend(matches or [t])
    return out


def _self_protect_verdict(prog: str, argv: list[str], cwd: str | None) -> Verdict:
    """The one place self-protection is decided, for every mode.

    Two callers used to decide it separately (check_path for Write/Edit, an
    ad-hoc token loop for Bash) and they disagreed in both directions at once:
    trusted let 10 of 14 write shapes through, readonly refused ordinary reads.
    """
    ancestor = prog in _SELF_REMOVERS
    for target in _self_protect_targets(prog, argv, cwd):
        hit = _self_protected_path(target, ancestor=ancestor)
        if hit:
            return Verdict(False,
                           f"自身の権限定義/プロンプト/監査ログに書くため不可（{hit}）",
                           alternative=("読むだけなら通ります。変更が要るなら"
                                        "利用者に依頼してください"))
    if prog in _SELF_CODE_RUNNERS:
        # `python3 -c "open('…/policy.py','w')"` — the path is right there in
        # the argument even though this parser cannot resolve it. This closes
        # the literal form only; `python3 evil.py`, where the path lives in a
        # file the gate never sees, stays open and no string test closes it.
        protected = _self_files() + _self_trees()
        for tok in argv[1:]:
            for f in protected:
                if f in tok:
                    return Verdict(False,
                                   f"自身の権限定義/プロンプト/監査ログを名指しするコードは不可（{f}）")
    return Verdict(True, "")


#: 決まりの例（手本: Codex の execpolicy の match / not_match＝決まりと一緒に書き、読み込みのたびに試す）。
#: (モード, 命令, 通すか)。{policy} はこのファイルのパス。決まりを変えたら、ここに当たる例と当たらない例を足す。
#: 試験は tests/test_policy_examples_20261111.py（作業ディレクトリは空のフォルダで走らせる）。
EXAMPLES: tuple[tuple[str, str, bool], ...] = (
    ("readonly", "ls -la", True),
    ("readonly", "git status", True),
    ("readonly", "git commit -m x", False),
    ("readonly", "rm build.log", False),
    ("readonly", "find . -delete", False),
    ("readonly", "sed -i s/a/b/ f.txt", False),
    ("readonly", "echo hi > out.txt", False),
    ("readonly", "kill $(cat server.pid)", False),
    ("readonly", "python3 x.py", False),
    ("readonly", "cat ~/.ssh/id_ed25519", False),
    ("workspace", "rm -rf build", True),
    ("workspace", "echo hi > out.txt", True),
    ("workspace", "python3 x.py", True),
    ("workspace", "find . -delete", False),
    ("workspace", "cd / && rm -rf victim", False),
    ("workspace", "sudo systemctl restart nginx", False),
    ("workspace", "curl https://example.com", False),
    ("workspace", "echo x >> {policy}", False),
    ("trusted", "rm -rf build", True),
    ("trusted", "kill $(cat server.pid)", True),
    ("trusted", "sudo systemctl restart nginx", True),
    ("trusted", "rm ~/.ssh/authorized_keys", False),
    ("trusted", "echo x >> {policy}", False),
)


def self_check(mode: str, cwd: str) -> list[str]:
    """EXAMPLES のうち、いまのモード（LAPS_POLICY）の物を cwd で確かめ、外れた物を返す。"""
    bad: list[str] = []
    for m, cmd, want in EXAMPLES:
        if m != mode:
            continue
        cmd = cmd.format(policy=os.path.abspath(__file__))
        got = check_command(cmd, cwd=cwd).allowed
        if got != want:
            bad.append(f"{mode}: {cmd!r} → {'allow' if got else 'deny'}（例では {'allow' if want else 'deny'}）")
    return bad


def check_answer_command(command: str, cwd: str | None = None) -> Verdict:
    """Ask モード（質問に答えるために道具を使う・2026-11-11 オーナー）の Bash の判定。読むだけ（readonly）の決まりで見る。
    もとの設定より緩めない。もとが trusted のときだけ ssh の接続先を縛らない（遠隔の命令も readonly で見る）。"""
    base = _mode()
    t_mode = _MODE_OVERRIDE.set("readonly")
    t_ssh = _ANSWER_SSH_ANY.set(base == "trusted")
    try:
        return check_command(command, cwd=cwd)
    finally:
        _ANSWER_SSH_ANY.reset(t_ssh)
        _MODE_OVERRIDE.reset(t_mode)


def _denial_text(v: "Verdict") -> str:
    """The refusal as the model will read it, alternative included.

    The tools print this exception verbatim, so anything not in this string does
    not exist as far as the model is concerned.
    """
    # 抜け道を探させない一言（手本: Codex の guardian の拒否文）。断られた命令を書き換え・別の道具・スクリプト経由で
    # 通そうとする形を止める。代わりの手か、利用者に聞くかを選ばせる。
    text = f"{v.reason}\n→ 代わりにこうできます: {v.alternative}" if v.alternative else v.reason
    return (text + "\nDo not try to get the same result another way (rewording the command, another tool, a script or an "
            "indirect route). Take a safer alternative, or ask the user.")


def check_path(path: str, *, write: bool) -> Verdict:
    """Authorise a file access by path."""
    mode = _mode()
    p = Path(path).expanduser()
    try:
        rp = p.resolve()
    except OSError as e:
        return Verdict(False, f"パスを解決できない: {e}")

    s = str(rp)
    if write:
        # Refused even with policy=off: this is the CVE-2025-53773 shape.
        # ★2026-09-02: 部分一致をやめた。`settings.json` / `permission.py` は
        #   一般名で、利用者の `.vscode/settings.json` や利用者自身が書いた
        #   `permission.py` まで書けなくなっていた（この機械の記録には現れない＝
        #   商用で現れる形）。守るのは**このプロセスが使っている実体**だけ。
        hit = _self_protected_path(s)
        if hit:
            return Verdict(False,
                           f"エージェント自身の権限定義/プロンプト/監査ログは書き換え不可（{hit}）")

    if mode in ("off", "trusted") or not write:
        return Verdict(True, f"mode={mode}")
    if mode == "readonly":
        return Verdict(False, "readonly モードでは書き込み不可")

    ws = _workspace()
    if rp == ws or ws in rp.parents:
        return Verdict(True, f"workspace={ws}")
    return Verdict(False, f"作業ディレクトリ外への書き込みは不可（{ws} の外）")


def authorize_command(command: str, cwd: str | None = None) -> None:
    v = check_command(command, cwd=cwd)
    audit("bash", {"command": command, "cwd": cwd},
          "allow" if v.allowed else "deny", v.reason)
    if not v.allowed:
        raise PolicyDenied(_denial_text(v))


def authorize_path(path: str, *, write: bool, kind: str = "file") -> None:
    v = check_path(path, write=write)
    audit(kind, {"path": path, "write": write},
          "allow" if v.allowed else "deny", v.reason)
    if not v.allowed:
        raise PolicyDenied(_denial_text(v))


def check_read_path(path: str) -> Verdict:
    """Refuse reads of credential files, for tools that open files directly.

    Blocking this in the Bash gate alone was pointless: Read/Grep/Glob open
    files without going through a shell, so `Read(<a credentials note>)` returned
    the credentials verbatim while the identical `cat` was refused.
    """
    try:
        rp = os.path.realpath(os.path.expanduser(path))
    except OSError as e:
        return Verdict(False, f"パスを解決できない: {e}")
    if any(marker in rp for marker in _SECRET_READ):
        return Verdict(False, f"資格情報を含むパスの読み取りは不可（{path}）")
    # ★ここに `_SECRET_TREES` を歩いて「機微ツリー内の資格情報ファイル」を断る枝が
    #   あったが、その条件（`any(m in rp for m in _SECRET_READ)`）は直前の判定の
    #   **部分集合**で、到達しても必ず False ＝**一度も発火しない枝**だった
    #   （2026-09-02 に削除）。読むと「ツリーの中も見張っている」ように見えるので、
    #   守っていない物を守っているように見せる分だけ悪い。
    #   実際の約束はこう: **ツリーの中は読める。名指しした資格情報ファイルだけ断る。**
    #   （ツリーには安全な文書と機微が同居していて、伏字を掛けて読む正規の経路は
    #     LabDoc。ツリー全体を断つと運用の記録がまるごと読めなくなる。）
    #   再帰の走査だけは別で、そちらは check_recursive_read が本当に断っている。
    return Verdict(True, "read ok")


def authorize_read(path: str, kind: str = "read") -> None:
    v = check_read_path(path)
    audit(kind, {"path": path, "read": True},
          "allow" if v.allowed else "deny", v.reason)
    if not v.allowed:
        raise PolicyDenied(_denial_text(v))


def check_recursive_read(path: str) -> Verdict:
    """For tools that walk a directory (Grep, Glob): refuse if the walk can
    reach a secret tree, from inside it or from an ancestor above it."""
    try:
        rp = os.path.realpath(os.path.expanduser(path or "."))
    except OSError as e:
        return Verdict(False, f"パスを解決できない: {e}")
    if any(marker in rp for marker in _SECRET_READ):
        return Verdict(False, f"資格情報を含むパスの走査は不可（{path}）")
    for tree in _SECRET_TREES:
        if rp == tree or rp.startswith(tree + "/"):
            return Verdict(False, f"機微ツリー（{tree}）の走査は不可（{path}）")
        if tree.startswith(rp.rstrip("/") + "/"):
            # ★上位からの再帰は**本当に**機微ツリーへ降りるので断る（緩めない）。
            #   ただし断り方を直す（2026-08-27・run29）: 旧文言は「/srv は不可」としか
            #   言わず、モデルは課題文が名指しした /srv/packages を諦めた。
            #   通る書き方を返す＝空観測の置換と同じ作法。
            try:
                import os as _os
                sibs = [d for d in sorted(_os.listdir(rp))
                        if _os.path.isdir(_os.path.join(rp, d))
                        and not (rp.rstrip("/") + "/" + d + "/").startswith(tree + "/")
                        and tree != rp.rstrip("/") + "/" + d][:8]
            except OSError:
                sibs = []
            hint = ("。走査できる下位を直接指定してください: "
                    + "、".join(rp.rstrip("/") + "/" + d for d in sibs)) if sibs else ""
            return Verdict(False,
                           f"{path} からの再帰は機微ツリー（{tree}）に降りるので不可{hint}")
    return Verdict(True, "walk ok")


def authorize_walk(path: str, kind: str = "walk") -> None:
    v = check_recursive_read(path)
    audit(kind, {"path": path, "walk": True},
          "allow" if v.allowed else "deny", v.reason)
    if not v.allowed:
        raise PolicyDenied(_denial_text(v))
