"""Glob tool: list files matching a glob pattern."""
from __future__ import annotations
from collections import OrderedDict
from pathlib import Path

from . import limits
from . import fold

from . import ToolSpec, ToolResult
from ..policy import authorize_walk, PolicyDenied


def _run(inp: dict) -> str:
    """★入る前に、見た目が同じで文字だけ違うパスを実在の名前に直す（2026-10-05・pathfix の注記）。"""
    import re as _re
    from . import pathfix
    fixed, notes = dict(inp), []
    gp = inp.get("path")
    if gp and not Path(str(gp)).exists():
        f = pathfix.fix_confusable(str(gp))
        if f:
            fixed["path"] = f
            notes.append(pathfix.note(str(gp), f))
    pat = str(inp.get("pattern") or "")
    if pat.startswith("/") or (len(pat) > 2 and pat[1] == ":"):
        head = _re.split(r"[*?\[]", pat, 1)[0]
        root_str = head if head.endswith("/") else head.rsplit("/", 1)[0] or "/"
        if root_str and not Path(root_str).exists():
            f = pathfix.fix_confusable(root_str)
            if f:
                fixed["pattern"] = f.rstrip("/\\") + "/" + pat[len(root_str):].lstrip("/")
                notes.append(pathfix.note(root_str, f))
    res = _run_inner(fixed)
    for n in notes:
        res = pathfix.attach(res, n)
    return res


def _run_inner(inp: dict) -> str:
    pattern = inp.get("pattern")
    if not pattern:
        return "Error: missing 'pattern'"
    import re as _re
    # ★絶対パスのパターンを受ける（2026-08-27・run29 の実測）:
    #   旧実装は `pattern.lstrip("./")` で先頭の `/` を削り、cwd 配下を探して
    #   **必ず 0 件**になった。しかもそれを `(no matches)` として返すので、
    #   モデルは「/srv/packages は空だ」という**誤った確信**を持った
    #   （同じ走行で `path=/srv/packages, pattern=*.deb` は 100 件超を返している）。
    #   ＝このファイル自身が冒頭で戒めている「0 件と、一致しえないを混ぜない」の再発。
    #   ★lstrip は文字集合を剥がすので `../x` や `.hidden` も壊していた。removeprefix にする。
    pat = str(pattern)
    given_path = inp.get("path")
    if pat.startswith("/") or (len(pat) > 2 and pat[1] == ":"):
        head = _re.split(r"[*?\[]", pat, 1)[0]
        root_str = head if head.endswith("/") else head.rsplit("/", 1)[0] or "/"
        rel = pat[len(root_str):].lstrip("/")
        if given_path and str(Path(given_path).resolve()) != str(Path(root_str).resolve()):
            # 黙って片方を捨てない（どちらを無視しても利用者の意図とずれる）。
            return (f"Error: pattern が絶対パス（{pat}）なのに path も指定されています"
                    f"（{given_path}）。どちらか一方にしてください: "
                    f"pattern=\"{rel or '*'}\" path=\"{root_str}\" が同じ意味です。")
        path = Path(root_str)
        pat = rel or "*"
    else:
        path = Path(given_path if given_path is not None else ".")
        pat = pat.removeprefix("./")
    # ★走査の許可は**実際に歩く根**に対して確かめる（2026-09-02・再現済み）。
    #   旧実装は「pattern の、ワイルドカードより前の部分」だけを見ていたので、
    #   場所を `path=` で渡すと**門を素通り**した（実測）:
    #       Glob(pattern="/srv/notes/secret/**/*.md")  → 正しく拒否
    #       Glob(path="/srv/notes/secret", pattern="**/*.md")
    #                                                     → **機微ツリーを一覧できた**
    #   Grep は同じ引数を `authorize_walk(path)` で正しく拒否している
    #   ＝門はあるのに、この道具だけ**別のものを門に通していた**
    #   （「書いたが繋いでいない」＋「片方で直して片方に残した」の両方）。
    #   ★存在確認より先に置く（拒否された場所の有無も漏らさない）。
    try:
        authorize_walk(str(path))
    except PolicyDenied as e:
        return f"Error: policy denied: {e}"
    if not path.exists():
        # ★「そこが無い」と「0 件」を混ぜない（0 件は探索が成立した証拠）。
        return (f"Error: 走査の起点が存在しません: {path}"
                f"（パターン `{pattern}` から導きました）。"
                f"親ディレクトリを Bash の ls で確かめてください。")
    # ★列挙そのものに上限を置く（旧実装は木を全部materializeしてから [:500]）。
    #   20,000 で止め、止めたことは本文に書く（黙って止めない）。
    # ★★件数だけでは止まらない（2026-09-06・owner 実機 P0）。`rglob` が**返す**のは
    #   一致したものだけなので、`**/*.exe` のように一致が少ないパターンでは
    #   `_ENUM_MAX` は一度も発火せず、その裏で木を歩ききるまで返ってこない。
    #   実機（`D:\Blackwall`＝ゲームの入ったフォルダ）で道具が `(running)` のまま
    #   戻らなくなった形がこれ。**歩く時間**にも上限を置く（`limits.Walk`）。
    #   上限が在るのに効かない、が、この現場でいちばん高くつく形。
    _ENUM_MAX = 20000
    _walk = limits.Walk()
    _all: list[str] = []
    _capped = False
    # ★雑音の巣は既定で歩かない（2026-08-30・第三者レビュー E3）。
    #   `**/*` が `.git` の中身と `node_modules` で埋まり、本物のファイルが埋もれる。
    #   ★**隠すのではない**: パターンがその名前を明示していれば従来どおり出す
    #   （`**/.git/**` は歩く）。Grep / Read と可視性を揃えるだけ。
    _NOISE = (".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache")
    _asked = tuple(n for n in _NOISE if n in pat)
    # ★歩くのは**別のスレッド**（2026-09-06・owner 実機 P0）。
    #   最初はこのループの頭で時計を見るだけにしたが、それでは効かない:
    #   `rglob` が返すのは**一致したものだけ**なので、`**/*.exe` のように
    #   一致が 0 件のまま何万ものディレクトリを降りる形では、ループの中身が
    #   **一度も実行されない**＝時計を見る機会が来ない。止めたい当の形が
    #   まさにそれ（実機は `D:\Blackwall` で `(running)` のまま戻らなかった）。
    #   ＝「上限を書いたが、止めたい経路には繋がっていない」。この現場の常習。
    #
    #   `rglob` の中の走査に手は入れられないので、走査ごと別スレッドへ出し、
    #   **こちらは受け取る側として時計を見る**。時間が尽きたら
    #   **殺さずに、受け取るのをやめる**（`bash.py` の `_wait_or_release` と同じ判断。
    #   歩いている最中のスレッドを安全に止める手は Python に無い）。
    #   置いていったスレッドは daemon なので、プロセスの終了を妨げない。
    #   受け皿に上限を付けてあるので、誰も汲まなくなればそのスレッドは
    #   `put` で止まる＝走り続けて CPU を食うこともない。
    import queue as _queue
    import threading as _threading
    _q: "_queue.Queue" = _queue.Queue(maxsize=4096)
    _DONE = object()

    def _producer() -> None:
        try:
            for _p in path.rglob(pat):
                _q.put(_p)
        except (OSError, ValueError) as e:
            # 権限・切れたネットワークドライブ・消えた途中の階層・壊れたパターン。
            _q.put(e)
            return
        except BaseException as e:        # noqa: BLE001 - 受け側を永久に待たせない
            _q.put(e if isinstance(e, Exception) else RuntimeError(repr(e)))
            return
        _q.put(_DONE)

    _th = _threading.Thread(target=_producer, name="bw-glob", daemon=True)
    _th.start()
    _err: Exception | None = None
    while True:
        if _walk.expired():
            break
        try:
            _item = _q.get(timeout=0.05)
        except _queue.Empty:
            continue
        if _item is _DONE:
            break
        if isinstance(_item, Exception):
            _err = _item
            break
        if any(part in _NOISE and part not in _asked for part in _item.parts):
            continue
        _all.append(str(_item))
        if len(_all) >= _ENUM_MAX:
            _capped = True
            break
    if _err is not None and not _all:
        # ★集まったぶんが在るなら返す。0 件と偽らない。
        return f"Error: 走査に失敗しました: {path}（{_err}）"
    matches = sorted(_all)
    if not matches and _walk.hit:
        # ★ここを「0 件」と言ってはいけない。この道具は上で「0 件は探索が
        #   成立した証拠」と書いており、時間切れの 0 件はその条件を満たさない。
        #   黙って `(no matches)` を返すと、モデルは「そのファイルは無い」という
        #   **誤った確信**を持つ（同じ誤りをこの道具は 2026-08-27 に一度踏んでいる）。
        return (f"Error: {limits.walk_secs():g} 秒で走査を打ち切りました（0 件ではありません）: "
                f"{path} / `{pattern}`。"
                f"path= でもっと狭い場所を指すか、`*` で直下だけを見てください。"
                f"（時間を延ばすなら LAPS_WALK_SECS）")
    if not matches:
        # ★「0件」と「そのパターンでは**何にも一致しえない**」を混ぜない。
        #   2026-08-16 実機（omega_j_prime）: モデルが `Glob(pattern=".py")` と呼び、
        #   rglob が当然 0 件を返し、モデルは「Python ファイルは存在しません」と
        #   **正直に**報告した——.py が3つあるフォルダで。捏造ではなく、
        #   道具が使い方の誤りを「空の結果」として返したのが原因。
        #   grep で同じ欠陥（検索の失敗を 0 件として返す）を直したのと同じ理屈。
        #   ★診断だけでは足りない（実測: 診断文だけだと 70% が黙り込む）。
        #     必ず**次の一手＝正しいパターン**を書いて返す。
        no_wild = not any(ch in pattern for ch in "*?[")
        if no_wild and pattern.startswith(".") and "/" not in pattern and "\\" not in pattern:
            return (f"Error: `{pattern}` は拡張子そのものなので、パターンとしては"
                    f"何にも一致しません（0 件ではありません）。"
                    f"配下すべてなら `**/{'*' + pattern}`、直下だけなら `{'*' + pattern}` を使ってください。")
        return ToolResult("(no matches)", ok=True, kind="empty")   # ★0件は失敗ではない
    body = _render(matches, capped=_capped, timed_out=_walk.hit)
    # ★同じ Glob の 2 回目以降は畳む（2026-09-02）。実体は Bash と**同じ 1 本**。
    #   実測 8 走行: Glob は 369 回のうち 129 回（35.0%）が道具＋引数＋本文まで
    #   完全に同じ反復で、本番のこの関数を掛け直すと **57 件・331,453 バイト**
    #   （≒41 KB/走行）が畳めた。畳みは Bash にしか無かった＝典型的な
    #   「片方で直して片方に残す」。
    # ★鍵は**利用者が書いた引数そのもの**（解決後の path/pat ではない）。
    #   同じ意味の別の書き方は畳まれないだけで、誤って畳むことはない。
    # ★成功も型で名乗る（2026-09-21・read.py と同じ理由）。素の str で返すと
    #   `as_result` が**一覧に並んだファイル名**で成否を当てにいく。
    #   例: `No such file or directory` という名前の記録や、`command not found`
    #   を含むパスが 1 本混ざるだけで、Glob が失敗したことになる。
    return ToolResult(
        fold.fold_repeat(
            "Glob", f"{given_path if given_path is not None else '.'}|{pattern}",
            None, body, body, what="この一覧"),
        ok=True, kind="ok")


def _render(matches: list[str], *, capped: bool = False,
            timed_out: bool = False) -> str:
    """一覧を**ディレクトリごとに畳んで**返す。件数は必ず書く。

    ★直したのはここ（2026-08-29・run29/31/32/34 の実測）。旧実装は

        return "\n".join(sorted(...)[:500])

    で、**文字数の上限が一つも無かった**。実測 239 回・896,469 バイト
    ＝全道具結果の 11.1%（Read に次いで 2 番目）。最大は `pattern="**"` の
    1 回で **26,952 バイト（500 行）**、その大半が
    `images/田中花子/山田太郎0006_1.png` のような**同じ形の並び**だった。
    そして `[:500]` は無言なので、501 件目以降が在ることはどこにも出ない。

    ★畳み方は「ディレクトリごとに公平に何本か見せ、残りは件数で言う」。
      一覧を求める問いに対して欲しいのは**どこに何があるか**であって、
      同じフォルダの 312 個目のファイル名ではない。
    """
    total = len(matches)
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for m in matches:
        # ★区切りは `/` だけではない（2026-09-02）。Windows では `str(Path)` が
        #   `C:\\src\\a.py` を返すので、`/` だけで切ると**全部が "." の 1 束**になり、
        #   「ディレクトリごとに公平に配る」という畳み方がまるごと働かない。
        #   この道具も説明文で「OS を問わず同じように動く」と約束している。
        i = max(m.rfind("/"), m.rfind("\\"))
        groups.setdefault(m[:i] if i > 0 else ".", []).append(m)

    cap = limits.body_cap(30000)
    body_all = "\n".join(matches)
    if fold.nbytes(body_all) <= cap and not capped and not timed_out:
        return body_all

    reserve = 200 + min(8, len(groups)) * 40
    clipped_line = False
    shown, omitted = fold.round_robin(groups, cap, reserve=reserve)
    if not shown and matches:
        # ★1本も見せないのは「切った」ではなく「答えていない」。
        #   1行が予算より大きいときでも、必ず先頭の1本は切って見せる。
        head = fold.clip_bytes(matches[0], max(200, cap // 2))
        shown = [head + " …(この1行だけで予算を超えるため切りました)"]
        clipped_line = True
        omitted = {k: len(v) for k, v in groups.items()}
        omitted[next(iter(groups))] -= 1
        omitted = {k: v for k, v in omitted.items() if v > 0}

    out = "\n".join(shown)
    out += "\n" + fold.coverage(
        total, len(shown), what="一致",
        hint="path= で範囲を狭めるか、pattern を具体的にしてください。")
    if clipped_line:
        out += "（※1行が長すぎるため、その行の中身も切っています）"
    note = fold.omitted_note(omitted, label="省いたディレクトリ")
    if note:
        out += "\n" + note
    if capped:
        out += (f"\n  ★列挙は {total:,} 件で打ち切りました"
                f"（これより多くのファイルがある可能性があります）。")
    if timed_out:
        # ★件数で切ったのか**時間で切ったのか**を混ぜない。次の一手が違う
        #   （件数なら pattern を絞る／時間なら場所を狭める）。
        out += (f"\n  ★{limits.walk_secs():g} 秒で走査を打ち切りました"
                f"（見えているのは木の一部です）。"
                f"path= でもっと狭い場所を指してください。")
    hard = limits.out_cap(30000)
    if len(out) > hard:
        out = out[:hard] + limits.note(len(out), hard)
    return out


SPEC = ToolSpec(
    name="Glob",
    # ★実測 2026-08-13（REPORTS/tool-selection-20260813.md）。旧文面は
    #   "List files matching a glob pattern (recursive)."（47字）で、**中身を探す問い
    #   （`def main` はどこ、api_key を使っているファイルは）にまで Glob が選ばれていた**
    #   ＝DEV 24 回中 22。引数は `**/*`＝「全部並べて自分で探す」つもり。
    #
    #   ここを直すのに2回失敗していて、失敗の仕方のほうが再利用できる:
    #   ① 「中身に書かれた文字列を探すなら Grep、中身を読むなら Read」と**誘導を書いたら
    #      Glob が全部を吸い込んだ**（読み取り課題 24/24 → Read 12・Glob 11）。
    #   ② 「ファイルを開かないので中に何が書いてあるかは分からない」と**否定形で書いても
    #      同じ**（読み取り 7/24・検索 18/24 が Glob のまま）。
    #   どちらも理由は一つ。**誘導文も否定文も、他の道具の引き金になる語を
    #   この説明文の中に置いてしまう。**選ぶ側にとって語は語であって、否定は効かない。
    #   → 他の道具の語彙を一切使わず、自分の出力の形だけを言う。誘導は Bash 側に置く。
    description=(
        "ファイル名の一覧を取る（再帰・OS を問わず同じように動く）。"
        "**このフォルダにどんなファイルが置かれているかを知りたいときは、"
        "シェルで ls / dir / find を組み立てるより、こちらを使うこと。**"
        "pattern は `*` で直下の全部、`**/*` で配下の全部、`**/*.py` で拡張子を絞る。"
        "★出力はパスの一覧だけ（1行1パス）。"
    ),
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                        "description": "ファイル名に当てるパターン。例: `*`, `**/*.py`"},
            "path": {"type": "string", "default": "."},
        },
        "required": ["pattern"],
    },
    readonly=True, parallel_safe=True,
    handler=_run,
)
