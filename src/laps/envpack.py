"""Facts about this machine, and about what the gate will refuse.

Measured need: on its first real run in the non-root account the agent wrote a
correct fizz.py, ran `python fizz.py`, got "python: not found" (this host only
has python3), concluded Python was missing, and tried `apt install -y python3`
— which the policy gate denied, as it always will. Three turns spent on a
question the harness could have answered before the first one.

So this block states two things the model cannot know but the process does:
which interpreters actually exist, and which whole categories of command are
refused no matter how the request is phrased. The second half matters more.
Telling a model "you may not install packages" after it has already tried is
feedback; telling it beforehand is context.

Kept deliberately short. Tool schemas and system prompt already occupy the
window, and published measurements show accuracy falling as the prompt grows —
this is facts the run needs, not a description of the lab.
"""
from __future__ import annotations
import os
import platform
import shutil
import subprocess
import sys
from functools import lru_cache


def _works(name: str) -> bool:
    """★「在るか」ではなく「**動くか**」を見る（2026-08-22・実機で踏んだ）。

    Windows の `where python` は `C:\\Windows\\System32\\python` を見つけるが、
    これは **Microsoft Store を開くためのスタブ**で Python ではない。
    実機で「python が見つかる」→「実行すると認識されていません」となり、
    エージェントは絶対パスでも失敗して第1段階で止まった。
    私の動作確認プロンプトも `where` の成功を「使える」と報告していた
    ——**同じ誤りを、道具とプロンプトの両方に書いていた。**
    """
    if not shutil.which(name):
        return False
    probe = {"python": ["-c", "print(1)"], "python3": ["-c", "print(1)"],
             "py": ["-c", "print(1)"], "node": ["-e", "1"],
             "git": ["--version"], "make": ["--version"],
             "bash": ["-c", "true"], "pytest": ["--version"]}.get(name, ["--version"])
    # ★2026-09-06（owner 実機・Windows）: 旧 `run(capture_output=True, timeout=10)` は
    #   TimeoutExpired 後に `communicate()` でパイプの EOF を待ち、孫がパイプを握ると
    #   **無期限に固まる**（HTTP の前に無音で止まる 3 経路の 1 つ）。戻り値しか要らないので
    #   パイプを持たず（全部 DEVNULL）`wait(timeout)` で待ち、超えたら木ごと殺す。
    #   Windows の `bash` は WSL 起動スタブ＝探るだけで対話プロンプトになり得るので探らない。
    if sys.platform == "win32" and name == "bash":
        return False
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen([name, *probe], **kw)
    except Exception:
        return False
    try:
        return proc.wait(timeout=10) == 0
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return False
    except Exception:
        return False


def _kill_tree(proc) -> None:
    """探りが戻らないときに木ごと止める（Windows は taskkill /T・他は kill）。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


@lru_cache(maxsize=1)
def _interpreters() -> str:
    names = ("python3", "python", "py", "node", "bash", "git", "make", "pytest")
    found, broken = [], []
    for name in names:
        if _works(name):
            found.append(name)
        elif shutil.which(name):
            broken.append(name)      # 在るのに動かない（Store のスタブなど）
    missing = [n for n in ("python", "python3", "py", "node")
               if n not in found and n not in broken]
    line = "動くもの: " + ", ".join(found) if found else "動くもの: (無し)"
    if broken:
        line += (f" ／ ★**在るが動かない**: {', '.join(broken)}"
                 "（Windows の Store スタブなど。これを使おうとしないこと）")
    if missing:
        line += f" ／ 無し: {', '.join(missing)}"
    return line


#: ★「在るか」だけを `shutil.which` で見る道具（走らせない＝速い・安全）。
#:   2026-09-21: 環境ブロックに載るのが python/node/git/make など 8 個だけで、しかも Bash の説明が
#:   「載っていないコマンドを試さないこと」と言っていた＝**モデルの手を 8 個に縛っていた**
#:   （オーナー指摘「コマンドの幅が狭い」）。OSS は OS とシェルを必ず渡す
#:   （opencode tool/shell/shell.txt「OS: …, Shell: …」・aider base_coder.py:1127-）。
#:   ここでは**シェルの名前**と、**実際に在る調査向けコマンド**を 1 行で渡す。
_TOOLBOX = {
    "win32": ("powershell", "pwsh", "where", "findstr", "certutil", "tar", "curl", "winget",
              "robocopy", "tasklist", "netstat", "ipconfig", "sc", "reg", "ssh", "docker"),
    "darwin": ("rg", "find", "file", "strings", "xxd", "curl", "jq", "tar", "unzip", "sqlite3",
               "lsof", "launchctl", "brew", "ssh", "rsync", "docker", "awk", "sed"),
    "linux": ("rg", "find", "file", "strings", "xxd", "curl", "wget", "jq", "tar", "unzip", "sqlite3",
              "ss", "ip", "systemctl", "journalctl", "docker", "ssh", "rsync", "awk", "sed",
              "apt", "dpkg", "dnf", "php", "go", "cargo", "gcc", "java"),
}


@lru_cache(maxsize=1)
def _toolbox() -> str:
    key = "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")
    have = [n for n in _TOOLBOX[key] if shutil.which(n)]
    return ", ".join(have) if have else "(見つからず)"


def _shell_name() -> str:
    """Bash の道具が**実際に使う**シェル。推測でなく、道具と同じ決め方で言う。"""
    if sys.platform == "win32":
        return "cmd.exe（PowerShell は `powershell -NoProfile -Command \"…\"` で呼べる）"
    return "bash" if shutil.which("bash") else "sh"


def environment_block() -> str:
    """A compact block for the system prompt. Recomputed per run, not per turn."""
    # ★`trusted` を一覧に入れ忘れていた（2026-08-22・実機の記録で判明）。
    #   `trusted` は **アプリの出荷時の既定**。一覧に無いので readonly へ倒れ、
    #   モデルはこう聞かされていた——
    #     「モード: readonly。ファイルへの書き込み・リダイレクトは実行できません。
    #       調査と報告のみ行ってください」
    #     「次のものは、どう書いても実行できません: パッケージ導入、sudo、ssh…」
    #   **全部嘘。** trusted では何でも実行できる。
    #   このファイルの docstring は「実行前に事実を伝えるのが要点」と書いてあるのに、
    #   伝えていたのは事実の逆だった。既定を増やしたとき、ここを直し忘れている。
    # ★2026-08-30: 既定を policy.py に合わせる。policy.py の _mode() は未設定なら
    #   "trusted"（オーナー指示 2026-08-29「制限を全部外す」）。ここが "readonly" の
    #   ままだと、直上のコメントで直した「一覧に無い→readonly に落ちる」バグと
    #   同じ形の嘘を、値が来ていないだけの場合にまた言うことになる。
    mode = os.environ.get("LAPS_POLICY", "trusted").strip().lower()
    if mode not in ("readonly", "workspace", "off", "trusted"):
        mode = "readonly"

    lines = [
        "\n## この実行環境（事実。推測で補わないこと）",
        f"- OS: {platform.system()} {platform.release()}",
        f"- シェル: {_shell_name()}",
        f"- コマンド: {_interpreters()}",
        f"- ほかに在る道具（一部。**ここに無いものも試してよい**）: {_toolbox()}",
        f"- 作業ディレクトリ: {os.getcwd()}",
    ]
    # ★作業場のパスに「見た目では区別できない文字」があるなら、絶対パスを書き写させない（2026-10-05）。
    #   実機（オーナー機）: フォルダ名の空白が全角（U+3000）で、モデルは半角で書き写し、
    #   絶対パスを使った Glob/Bash が 3 回とも「見つかりません」で落ちた（相対パスの回は通っていた）。
    #   道具の側でも直すが（tools/pathfix.py）、最初から相対パスで書けば往復が要らない。
    try:
        from .tools.pathfix import describe_traps as _traps
        _t = _traps(os.getcwd())
    except Exception:
        _t = []
    if _t:
        lines.append(
            f"- ★作業ディレクトリのパスには、見た目では区別できない文字（{'、'.join(_t)}）が"
            "含まれます。**絶対パスを書き写さず、相対パス（`.` や `sub\\file.txt`）で指してください。**")
    if not network_reachable():
        lines.append(
            "- **ネットワーク: 外に出られません（名前解決に失敗）。**"
            " apt / pip のリモート取得・git clone・WebSearch は使えません。"
            " 必要な物は**この機械の中**（オフラインのパッケージ置き場・既存の資産）から探してください")

    if mode == "readonly":
        lines.append(
            "- **モード: readonly。ファイルへの書き込み・リダイレクト(> >>)は実行できません。**"
            " 調査と報告のみ行ってください"
        )
    elif mode == "workspace":
        ws = os.environ.get("LAPS_WORKSPACE", os.getcwd())
        lines.append(f"- **モード: workspace。書き込めるのは {ws} の配下だけです**")
    elif mode == "trusted":
        lines.append(
            "- モード: trusted。**この機械のコマンドを制限なく実行でき、どこへでも書けます。**"
            " 破壊的な操作をするときは、実行するコマンドと対象を先に1行で述べてください"
        )
        # ★オーナー決定（2026-08-22）「インストールもエージェントが行う。
        #   タスクに必要なことは何でもやるように」。
        #   実行ゲートは元から許していた（trusted は全部通る）。止めていたのは
        #   **伝え方**で、「足りなければ報告してください」しか言っていなかった。
        lines.append(
            "- ★**足りないものは自分で導入してください。** 報告して止まらないこと。"
            " 必要な言語処理系・ライブラリ・道具は、この機械に入れてよいと"
            "利用者から許可されています"
        )
        # ★VDIR 再現テスト（2026-08-23・3 走行）で実測した負け筋を名指しで塞ぐ:
        #   apt が Permission denied → sudo を一度も試さず（3/3）→ 実在しない URL の wget →
        #   偽物のファイルで「完了」。**できないことをできたと言う**のが最悪の結末で、
        #   それを防ぐ 2 行。順路（sudo）と、行き止まりでの正しい振る舞い（正直に報告）。
        lines.append(
            "- 導入で Permission denied になったら **sudo を試してください**（trusted では許可されています）。"
            " sudo も駄目なら pip --user や静的バイナリ等の代替を試す——ただし **URL やコマンドを推測ででっち上げない**"
        )
        lines.append(
            "- ★どうしても作れない・できないものは、**「できなかった」と理由つきで報告してください。**"
            " 偽物のファイルや空のデータで埋めて「完了」と言うことは、失敗より重い違反です"
        )
        if platform.system() == "Windows":
            # ★実機で必ず踏む2つ。これを言わないと、導入したのに失敗と判断する。
            lines.append(
                "- Windows で導入するときの実際:\n"
                "  - **winget を使う**（例: `winget install -e --id Python.Python.3.12`）。"
                "Microsoft Store は使わない\n"
                "  - ★**導入直後、いまのシェルでは PATH が更新されません。**"
                "同じ名前で呼んでも「認識されていません」のままです。"
                "**入った先の絶対パスで呼んでください**"
                "（Python なら `%LOCALAPPDATA%\\Programs\\Python\\Python3xx\\python.exe` か、"
                "`py` ランチャ）。『動かないから導入に失敗した』と判断しないこと\n"
                "  - `C:\\Windows\\System32\\python` は **Store を開くだけのスタブ**で"
                "Python ではありません。これを使おうとしないこと"
            )
    else:
        lines.append("- モード: off（ゲートによる制限なし）")

    # ★「どう書いても実行できない」のは**止めるモードのときだけ**。
    #   trusted / off で同じことを言うと、できることを「できない」と教えることになり、
    #   モデルは要らない回避策に手番を使う（このブロックが防ぐはずだった無駄そのもの）。
    if mode in ("readonly", "workspace"):
        lines.append(
            "- ★**次のものは、どう書いても実行できません**: パッケージ導入 (apt/pip/dnf…)、"
            "sudo などの権限昇格、ssh/scp による他ホストへの到達、systemctl による状態変更、"
            "シャットダウン・再起動。**これらを試みても拒否されるだけなので、別の手段を採ってください**"
        )
        lines.append(
            "- 依存が足りないときは、導入しようとせず、"
            "**今あるもので済ませるか、足りない旨を報告してください**"
        )
    return "\n".join(lines) + "\n"

#: 名前解決の可否を**この走行で1回だけ**測る（毎ターン測ると遅く、揺れる）。
_NET_CACHE: dict[str, bool] = {}


def network_reachable() -> bool:
    """外に出られるか（DNS が引けるか）を 1 回だけ実測して覚える。

    ★なぜ要るか（2026-08-27・run29 の解剖）: この走行は DNS が引けない環境なのに、
      ハーネスは WebSearch を道具棚に載せ、知識サーバー の外れ時の助言でも
      「WebSearch で調べ直す」と勧めていた。モデルは 3 回呼んで 3 回とも
      `Temporary failure in name resolution` を受け取った＝**自分で不可能と
      分かるはずの手を勧める**という、道具の設計としての誤り。
    ★2 秒で諦める。走行開始が遅れるほうが害。
    """
    if "ok" in _NET_CACHE:
        return _NET_CACHE["ok"]
    forced = os.environ.get("LAPS_ASSUME_ONLINE", "").strip()
    if forced in ("0", "1"):
        _NET_CACHE["ok"] = forced == "1"
        return _NET_CACHE["ok"]
    # ★2026-09-06: `socket.setdefaulttimeout(2.0)` は **getaddrinfo を縛らない**（名前解決は
    #   OS のリゾルバ待ち＝Windows で DNS が死んでいると 30 秒以上黙る）。しかも全プロセスの
    #   既定 timeout を 2 秒に書き換えたまま戻していなかった。スレッドで引いて 2 秒で見切る。
    import socket, threading
    box = {}
    def _resolve():
        try:
            socket.getaddrinfo("deb.debian.org", 80)
            box["ok"] = True
        except Exception:
            box["ok"] = False
    t = threading.Thread(target=_resolve, name="bw-dns-probe", daemon=True)
    t.start()
    t.join(2.0)
    ok = bool(box.get("ok", False))   # 2 秒で返らなければ「引けない」扱い
    _NET_CACHE["ok"] = ok
    return ok
