"""Blackwall standalone tools."""
from __future__ import annotations
import contextvars
from dataclasses import dataclass
from typing import Any, Callable


#: 道具が失敗したことを示す、この現場の約束。
#: すべての道具が `Error: ...` で始めることで揃えてある（policy 拒否も含む）。
_FAILED_PREFIX = "Error:"

#: 実行そのものは通ったが、中身が失敗を告げている形。**シェルの言い分を拾う。**
#: ★Windows は日本語で返すので、英語だけを見ていると取り逃す。
_FAILED_BODY = (
    "は、内部コマンドまたは外部コマンド",          # 'x' は認識されていません（cp932→UTF-8 修正後に読める）
    "指定されたパスが見つかりません",
    "アクセスが拒否されました",
    "command not found",
    "No such file or directory",
    "Permission denied",
)


def tool_failed(result: str) -> bool:
    """その道具呼び出しが失敗したか。**1箇所で決める。**

    ★2026-08-16 実測: 明らかに失敗している道具呼び出し5件が、記録では **5件とも
    `ok=True`** だった。本体が `ok` を一度も送っておらず（serve.py:920）、
    アプリ側は「明示が無ければ成功」として扱っていたため。
    policy 拒否も、Windows の「認識されていません」も、Edit の不一致も、
    すべて「成功」として記録されていた。

    ＝**失敗率を測れない。** そして同じ情報が無いので、エージェント自身も
    「道具が失敗した」と「道具が0件を返した」を区別できていない
    （実機で、成功した dir の結果を「実行できません」と誤読する例も出ている）。

    ★判定は保守的に。**成功を失敗と呼ばない**ほうを優先する
      （偽の失敗は、うまくいった作業をやり直させる）。
    """
    t = str(result or "")
    if t.lstrip().startswith(_FAILED_PREFIX):
        return True
    head = t[:400]
    return any(m in head for m in _FAILED_BODY)


#: 道具が「走ったが該当0件」を表すときに使う綴り。**この現場が自分で書いた閉じた集合**。
#:
#: ★なぜ1箇所に集めるか（2026-08-18・敵対レビューで実測）:
#:   6つの道具がそれぞれ別の綴りを持ち（grep/glob/bash/task_mgmt/system_state/remote_read）、
#:   `grounding._is_empty_output` は**そのどれも空と認識していなかった**。
#:   結果、道具が本当に0件を返したときにモデルが正しく「該当なし」と報告すると、
#:   grounding が「引用元の出力は空ではありません」と**正しい答えを咎め**、
#:   出口の床がその行を決定論的に取り下げていた。
#:   ＝**最も正直な答えが、最も確実に壊される。**
#:
#: ⚠ これは「モデルの散文を語彙で当てる」語彙表とは違う。**自分が書いた出力**の
#:   一覧なので閉じている。新しい道具が空の綴りを増やすなら**ここに足す**
#:   （試験がそれを強制する: test_empty_sentinels）。
EMPTY_SENTINELS = (
    "(no matches)",             # grep / glob
    "(exit 0, no output)",      # bash
    "(no active tasks)",        # task_mgmt
    "(出力なし＝該当ゼロ件)",      # system_state
    "(出力なし)",                # remote_read
    "(no tasks)",               # task_mgmt（一覧が空）
    "(no results for",          # websearch（前方一致。後ろに問いが続く）
)


#: 道具の結果が取りうる「型」。散文から推測させないための語彙。
#:   ok      … 走って成功した
#:   error   … 走ったが失敗した（rc≠0・拒否・例外）
#:   empty   … 走って成功したが、該当が0件だった（**失敗ではない**）
#:   denied  … policy/hook が止めた（そもそも走っていない）
KINDS = ("ok", "error", "empty", "denied")


class ToolResult(str):
    """道具の結果。**文字列でありながら、成否と種類を持つ。**

    ★なぜ str の派生か（2026-08-18・敵対レビュー §1.3 の「型を入れる」の第一歩）:
    この設計の最も深い問題は「何が起きたか」が一度も型になっていないことだった——
    成功か失敗か、空か未実行か、すべて `[n] 名前 → 生文字列` に潰され、
    下流の3層（モデル・guard・画面）が**それぞれ散文から推測し直していた**。
    その推測が外れる形の違いが、捏造・停滞・誤検知として現れていた。

    かといって戻り値の型を一斉に変えると、道具22個と試験1001本を同時に動かすことになる。
    **str の派生なら、既存の呼び出し側は1行も変えずに済み**、道具は1つずつ型を
    名乗れるようになる。名乗らない道具は今までどおり語彙表（`tool_failed`）で
    推測し、その回数を数える＝**「まだ推測している箇所」が記録から見える。**

    ⚠ 落とし穴: `str` のメソッド（strip/replace/f-string など）は**素の str を返す**ので
      属性は失われる。**属性は境界で読む**こと（`as_result` で正規化してから使う）。
    """

    __slots__ = ("ok", "kind", "inferred", "data", "truncated")

    def __new__(cls, text: object = "", *, ok: bool = True,
                kind: str = "ok", inferred: bool = False,
                data: dict | None = None,
                truncated: bool = False) -> "ToolResult":
        self = super().__new__(cls, "" if text is None else str(text))
        self.ok = bool(ok)
        self.kind = kind if kind in KINDS else "ok"
        #: True = 道具が名乗らず、語彙表から推測した（＝まだ型になっていない）
        self.inferred = bool(inferred)
        #: 画面向けの構造化データ（例: Edit の差分 `{"patch": {...}}`）。
        #: ★モデルには渡さない。モデルに渡す文字列から画面を作り直すと崩れる
        #:   （Claude Code 解析 §6: 差分は適用時に旧/新から作って結果に保存する）。
        self.data = data if isinstance(data, dict) else None
        #: True = **道具が自分で切り詰めた**（2026-09-02 追加）。
        #: ★なぜ要るか: 受け口の `truncated` は「100,000 字の上限で切ったか」しか
        #:   見ておらず、記録 10,904 件すべてで false だった。一方で本文に
        #:   「切り詰め」と書かれた記録は 161 件ある——**道具が各自で切っていた**。
        #:   名前が「切り詰められたか」と読めるのに中身が違い、
        #:   「道具ごとの上限の効果」を測ろうとして**見えるはずのない欄**を使いかけた。
        #:   道具が自分で名乗れば、受け口はそれを合成して正しく報告できる。
        self.truncated = bool(truncated)
        return self


def looks_empty(text: str) -> bool:
    """その出力が「走ったが該当 0 件」か。**判定は 1 か所**（EMPTY_SENTINELS）。

    綴りは自分たちが書いたものだけなので閉じている。空行を除く全ての行が
    sentinel で始まるときだけ空とみなす（`grounding._is_empty_output` と同じ規則）。
    """
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    return all(any(ln.startswith(m) for m in EMPTY_SENTINELS) for ln in lines)


def as_result(value: object) -> ToolResult:
    """道具の戻り値を `ToolResult` に揃える。

    型を名乗った道具はそのまま。名乗らない道具（まだ大半）は、これまでどおり
    `tool_failed` の語彙表で推測し、`inferred=True` を立てる。

    ★2026-08-31（レビュー E10・再現済み）: 全 22 道具の失敗経路を実際に呼んだところ、
      **22/22 が素の str** を返しており、成否も種類も語彙表からの推測だった。
      推測が外れている実例が同じ日に 2 つ見つかっている:
        ・ApplyDiff の `Applied 0/1 file(s)`（1 本も当たっていない）→ ok=True
        ・`(no tasks)` / `(no results for …)` → kind="ok"
          （grounding は EMPTY_SENTINELS で「0 件」と読むのに、こちらは「ok」。
            **同じ文字列を 2 つの層が違うものとして扱っていた**）
      前者は各道具側で型を名乗って直した。後者は**ここで 1 か所にまとめる**——
      道具ごとに `kind="empty"` を書き足して回ると、書き忘れがまた食い違いを生む。
    """
    if isinstance(value, ToolResult):
        return value
    text = "" if value is None else str(value)
    # ★2026-09-21 根本の直し（オーナー「ひとつづつ対応してもらちが明かない」）。
    #   全 22 道具を機械で列挙したところ（tests/test_all_tools_name_their_status.py）、
    #   素の str を返す場所は **12 か所・すべて `Error: ` で始まる文字列**だった。
    #   ＝道具側は既に名乗っていて、受け手が**本文を読んで当てていた**だけ。
    #   ⇒ 語彙表（`tool_failed`）で内容を走査するのをやめ、**道具が書いた前置き**で決める。
    #
    #   これで消える型（今日の実測）:
    #     `Grep(pattern="denied")` → ★探した語は必ず出力に入るので **100% 失敗に見えた**
    #     `WebFetch("command not found の直し方")` → ★役に立つ取得ほど失敗に見えた
    #   どちらも「本文に失敗の語が在る」を失敗と読んでいた。前置きなら、その取り違えは
    #   **原理的に起きない**（前置きは道具が書く・取ってきた中身には無い）。
    #
    #   ⚠ 危ないのは**成功を素の str で返す道具**が現れたとき（本文がたまたま
    #     `Error:` で始まると失敗に化ける）。上の門が全道具を毎回列挙して、
    #     `Error:` で始まらない素の str を**その場で落とす**。
    if text.lstrip().startswith("Error:"):
        return ToolResult(text, ok=False, kind="error", inferred=True)
    if looks_empty(text):
        return ToolResult(text, ok=True, kind="empty", inferred=True)
    return ToolResult(text, ok=True, kind="ok", inferred=True)


#: いま道具を実行している **親 Agent**。`Agent._exec_tool` が handler を呼ぶ間だけ立つ。
#: なぜ: 道具の handler は `handler(inp)` で親への参照を受け取れない。委譲（Task）が子の事象を
#: 親へ流すには親の `on_event` が要る。ワーカー案はスタック歩き（`sys._getframe`）だったが、
#: フレーム名への依存は壊れやすい（並列 dispatch・将来の改名）。ContextVar はスレッドごとに
#: 独立で、`_exec_calls_parallel_safe` の ThreadPool でも混ざらない。
CURRENT_AGENT: "contextvars.ContextVar" = contextvars.ContextVar("blackwall_current_agent", default=None)


@dataclass
class ToolSpec:
    """道具1つの宣言。**能力は道具自身が名乗る。**

    ★なぜ能力を持たせるか（2026-08-18・敵対レビューの構造的指摘）:
    「並行してよいか」「Plan モードで止めるか」「実行を主張したときの裏付けになるか」は、
    これまで**呼ぶ側が書いた道具名の一覧**で決めていた（agent.py と action_guard.py に
    3つの frozenset）。道具を足した人が一覧に載せ忘れると、**黙って無防備になる**——
    しかも気づけない。実際、`_EXEC_TOOLS` には**実在しない RunTests** が入っていた。

    宣言にすれば、道具を足した瞬間に分類が決まり、試験が「宣言し忘れ」を落とす。
    """

    name: str
    description: str
    schema: dict
    handler: Callable[[dict], str]
    #: 何も書き換えない（Plan モードで通してよい）
    readonly: bool = False
    #: **同時に走らせても安全**。★readonly とは別物。
    #:   例: RemoteRead は読み取りだけだが、**同じホストへ並列 SSH すると締め出される**
    #:   （自ラボで実際に起きた事故）。SystemState も外の共有資源を叩くので、
    #:   「書き換えない」ことと「同時に叩いてよい」ことを1つの旗にまとめない。
    parallel_safe: bool = False
    #: ファイルや外の状態を書き換える（Plan モードで止める対象）
    mutates: bool = False
    #: **帳簿**の道具（予定・課題の記帳）。世界を変えず、情報も持ってこない。
    #: ★連続で呼ばれたら「まとめられる」と伝えるために宣言する（2026-08-19 実測）。
    #:   複雑な課題の実機で、モデルは予定を**1件ずつ5〜7回**閉じ、
    #:   反復の約40%を記帳に使っていた。説明文で「まとめて渡せる」と教えても、
    #:   従わない走行がある（3走行中1本）。**結果の中で気づかせる**ほうが確実。
    bookkeeping: bool = False
    #: 外のプログラムを走らせる／その出力を持ってくる
    #: （「実行しました」という主張の裏付けになるか）
    executes: bool = False


#: 能力を1つも持たない道具（帳簿・利用者への問いかけ）。**明示しておく**ことで、
#: 「宣言し忘れ」と「本当に中立」を試験が区別できる。
NEUTRAL_TOOLS = frozenset({
    "TodoWrite", "AskUserQuestion",
    "TaskCreate", "TaskUpdate", "TaskStop", "TaskList", "TaskGet",
})


def tools_where(**flags: bool) -> frozenset[str]:
    """指定した能力を持つ道具の名前。**呼ぶ側が一覧を手書きしないため。**"""
    return frozenset(
        t.name for t in all_specs()
        if all(getattr(t, k, False) is v for k, v in flags.items())
    )


def all_specs() -> list[ToolSpec]:
    from . import (
        bash, read, write, edit, grep, glob, task,
        webfetch, todowrite, bash_output,
        ask_user, websearch, notebook_edit, task_mgmt, verify,
        apply_diff, system_state,
    )
    specs = [
        bash.SPEC, read.SPEC, write.SPEC, edit.SPEC,
        grep.SPEC, glob.SPEC, task.SPEC,
        webfetch.SPEC, todowrite.SPEC, bash_output.SPEC,
        ask_user.SPEC, websearch.SPEC, notebook_edit.SPEC,
        # ★2026-10-05: RemoteRead（09-13 廃止）に続き RemoteShell と台帳（labhosts）も消した。
        #   オーナー「台帳は必要ないし、モデルの自由度を下げる機能」。別のホストは Bash の ssh で触る。
        apply_diff.SPEC, system_state.SPEC,
        # ★Verify（2026-09-21・オーナー指示①）。ハーネスが押し戻すのではなく、
        #   **確かめるという工程を道具として見せる**。呼ぶかはモデルが決める。
        verify.SPEC,
    ]
    specs.extend(task_mgmt.SPECS)
    return specs


# Measured on the 60-case bench: Write 142 / Bash 129 / Read 61 / Glob 30 /
# Grep 19 / Edit 13 uses, versus 5 or fewer for everything else and *zero* for
# ApplyDiff. Tool count is one of the strongest levers on selection accuracy —
# published measurements show 50 tools scoring 84-95%, 200 tools 41%, and a
# shorter list beating a longer one even when both contain the answer. Carrying
# 19 definitions to cover ~1% of calls costs prompt budget and choices.
# ★TodoWrite を足した（2026-08-19）。使用頻度が低いから外す、では済まなくなった:
#   **ハーネスの完了判定がこの道具の書いた予定を読んでいる**（`_open_todos`）。
#   予定を書く手段が無い toolset では、「まだ終わっていない」と差し戻す仕組みが
#   **黙って死ぬ**——鳴らないので気づけない。
#   `serve.py` は `tools` 未指定のとき focused を既定にするので、アプリ以外の
#   呼び出し（CLI・スクリプト・将来の統合）が静かにこの穴に落ちる。
#   ★道具数は選択精度に効く（下の測定）ので、足すのは**ハーネスが依存するものだけ**。
FOCUSED_TOOLS = ("Bash", "Read", "Write", "Edit", "Grep", "Glob", "TodoWrite")


#: 実機でもベンチでも **0 回**の宣言（2026-09-02 実測: 10,904 件の道具結果に 1 件も無し）。
#: full の固定前置きで 217 tok を占めるだけ（推論機:8741 の tokenizer で exact 実測。22→20 本の
#: 実配列で 4270→4053 tok、tool schema 全体の 5.1%）。既定の道具表から下ろす（下の by_name）。
#: ★能力は消さない: SPEC は all_specs() に残す（tools_where / checkpoint.TRACKED_TOOLS /
#:   postedit.EDIT_TOOLS / agent._LEDGER_TOOLS はいずれも「呼び出しに現れた道具名」の
#:   frozenset メンバシップ判定なので、登録から外しても集合が空振りするだけで壊れない）。
#:   LAPS_DEAD_TOOLS=1 で即座に戻せる（測定のためにも戻せる）。
#: ★オーナー方針「消すのは死んでいる宣言だけで能力は減らさない」に沿う: 落とすのは 0/0 の 2 本のみ。
_DEAD_DECLARATIONS = ("NotebookEdit", "TaskStop")

#: `ApplyDiff` と `Edit` は同じ仕事（ファイル編集）を2通りに宣言している。
#: aider/cline は同種の道具を同時に出さない（cline: definitions.ts:934
#: "Do not enable two similar tools at the same time."／opencode: registry.ts:296-301）。
#: ★測定（2026-09-04・root+bwdog の全走行履歴 498 本・events.jsonl 実走査）:
#:   `ApplyDiff` は**生涯で 1 回**しか呼ばれていない（その1回は成功）。
#:   `Edit` は 577 回・失敗 119 回（20.6%）。60-case bench でも ApplyDiff は 0 回
#:   （上の FOCUSED_TOOLS コメント）。実配列を跨いだ2つの独立した数字が同じ結論
#:   （実質未使用）に落ちる。研究知見（道具数は選択精度に最大級に効く／似た道具は
#:   選択を乱す）とも整合するので、既定は `Edit` だけを広告する。
#: ★能力は消さない: `apply_diff.py` はそのまま・SPEC は all_specs() に残る。
#:   unified diff の翻訳や照合カスケードなど、実失敗を減らす直しは 2026-09-01/02 に
#:   別途入っている（道具として選ばれなくても、コードは Edit の失敗経路の翻訳器として
#:   生きている）。LAPS_APPLYDIFF=1 で戻せる（測定のためにも戻せる）。
#: ★focused では元から不在（FOCUSED_TOOLS に無い）なので、これが効くのは
#:   full/未指定の経路だけ＝all_specs() は 22 本のまま変わらない。
_EXCLUSIVE_WITH_EDIT = ("ApplyDiff",)

#: ★予定の記帳が二系統あった（2026-10-05）。`TodoWrite`（.progress.md）と
#:   `TaskCreate/TaskUpdate/TaskList/TaskGet`（tasks.json）。同じ仕事を 2 通りに宣言している。
#: ★実測（既にある走行記録・09-01 以降の 99 走行）: 記帳は Task 系 **475 回**・TodoWrite **288 回**
#:   ＝モデルは 6 割を Task 系に書いていた。ところがハーネスが読むのは **TodoWrite の帳簿だけ**
#:   （`agent._plan_block` の末尾の予定・`agent._open_todos` の完了判定）。
#:   ⇒ 記帳の 6 割は「残りの予定」表示にも完了判定にも**届いていなかった**
#:     （TodoWrite の注記が警告していた「予定を書く手段が無いと仕組みが黙って死ぬ」の別の形）。
#: ★focused は元から TodoWrite だけ（FOCUSED_TOOLS）。二系統なのはアプリ既定の full だけだった。
#:   Task 系は定義 1,040 字・過去に TaskList⇄TaskUpdate の記帳ループも起こしている。
#: ★能力は消さない（SPEC は all_specs() に残る・画面の表示名も残す）。LAPS_TASK_TOOLS=1 で戻せる。
#:   `Task`（子エージェントへの委譲）は別物なので触らない。
_LEDGER_DUPLICATES = ("TaskCreate", "TaskUpdate", "TaskList", "TaskGet")

#: ★`LAPS_TOOLSET=chat` のときに渡す道具（2026-09-21・オーナー指示）。
#:   「ウェブ検索だけを使う通常会話モード」。★作業場の読み書きも実行も渡さない。
#:   ⚠ ここに Bash / Read / Write / Edit / Glob / Grep / Task を**足さないこと**。
#:     足した瞬間に「会話のつもりが作業場を触った」が起こりうる形に戻る。
#:   ⚠ 知識サーバー を入れない理由: 書庫は**ラボの内部資料**で、ふつうの会話の相手ではない
#:     （必要なら利用者が Agent モードで明示的に引く。オーナー指示 2026-09-21
#:      「知識サーバー 検索は明示的に指示を出したときのみ」とも整合）。
CHAT_TOOLS = ("WebSearch", "WebFetch")

#: ★受け付ける道具一式の名前の**正本**（2026-10-05）。`by_name()` が分岐する名前と一致させる。
#:   ★`serve.py` の要求検査はここを読む。0.1.251 で `chat` をここ（by_name）に足したのに
#:     serve の検査が `("full", "focused")` の写しのままで、画面の会話モードは
#:     **出荷した日から一度も動いていなかった**（実機 09-22 に 2 回
#:     `failed: tools は full か focused` で即死・道具 0 本）。写しを持たせない。
TOOLSETS = ("full", "focused", "chat")


def by_name() -> dict[str, ToolSpec]:
    """Registry for the agent.

    LAPS_TOOLSET=focused trims to the six tools that do the work;
    `full` (default, for now) keeps all 19 so the two can be A/B measured.
    """
    import os
    specs = {t.name: t for t in all_specs()}
    _mode = os.environ.get("LAPS_TOOLSET", "full").lower()
    # ★`chat` = ふつうの会話（2026-09-21・オーナー指示「ウェブ検索だけを使う通常会話モード」）。
    #   ★作業場を触る道具を**1 本も渡さない**。渡さなければ、誤って書く事故は
    #     「起きにくい」ではなく **起こしようがない**（規律ではなく構造で切る）。
    #   ★WebFetch を入れる理由: 検索は見出しと抜粋しか返さない。取りに行けないと
    #     「検索した」で終わり、根拠のある答えにならない（検索だけでは半分の道具）。
    #     ⚠ 取ってきた本文は**非信頼データ**。中の指示に従わないのは従来どおり。
    if _mode == "chat":
        return {n: specs[n] for n in CHAT_TOOLS if n in specs}
    if _mode == "focused":
        names = list(FOCUSED_TOOLS)
        # SystemState is in the focused set by default as of V9: measured at
        # n=100 per arm, abstention 0% against 39%, completion 100% against 59%,
        # fabrication 0% against 5%. It removes the cost that citation checking
        # was paying — grounding alone quartered fabrication but cost 13 points
        # of completion and 35% abstention, and this returns both.
        # LAPS_SYSSTATE=0 removes it, so the arms remain measurable.
        if os.environ.get("LAPS_SYSSTATE", "1") != "0" and "SystemState" in specs:
            names.append("SystemState")
        specs = {n: specs[n] for n in names if n in specs}
    # ★死んだ宣言を既定の道具表から下ろす（2026-09-02）。
    #   focused では元から不在なので、これが効くのは full/未指定の経路
    #   （CLI・スクリプト・将来の統合＝上のコメントが名指しで警告してきた側）だけ。
    #   ★GUI の既定も **full**（sidecar.rs:760・serve.py:1702。2026-09-02 検査で訂正: 旧文「GUI は focused」は誤り）＝**GUI にも効く**。all_specs() は 22 本のまま。
    if os.environ.get("LAPS_DEAD_TOOLS") != "1":
        for _dead in _DEAD_DECLARATIONS:
            specs.pop(_dead, None)
    # ★ApplyDiff と Edit を同時に広告しない（2026-09-04・A2）。既定は Edit だけ。
    #   focused では ApplyDiff はそもそも FOCUSED_TOOLS に無いので、これが効くのは
    #   full/未指定の経路だけ。LAPS_APPLYDIFF=1 で両方戻せる（測定用の逃げ道）。
    if os.environ.get("LAPS_APPLYDIFF") != "1":
        for _excl in _EXCLUSIVE_WITH_EDIT:
            specs.pop(_excl, None)
    # ★記帳を一系統にする（2026-10-05）。TodoWrite が在るなら Task 系 4 本は出さない。
    #   理由は `_LEDGER_DUPLICATES` の注記（ハーネスは TodoWrite の帳簿しか読まない）。
    if os.environ.get("LAPS_TASK_TOOLS") != "1" and "TodoWrite" in specs:
        for _dup in _LEDGER_DUPLICATES:
            specs.pop(_dup, None)
    return _drop_offline_tools(specs)


#: 外に出られないときに棚から下ろす道具。**在るのに必ず失敗する道具は、
#: 選択肢としてゼロより悪い**（研究知見: 道具数は選択精度に最大級に効く）。
_ONLINE_ONLY = ("WebSearch", "WebFetch")


def _drop_offline_tools(specs: dict) -> dict:
    """DNS が引けないなら、ネット専用の道具を一覧から消す（2026-08-27・run29）。

    ★run29 は DNS 不達の環境で WebSearch を 3 回呼び、3 回とも
      `Temporary failure in name resolution` を受け取った。棚に無ければ選べない。
    ★測定のために外せる: LAPS_ASSUME_ONLINE=1 で常に載せる。
    """
    from ..envpack import network_reachable
    if network_reachable():
        return specs
    return {k: v for k, v in specs.items() if k not in _ONLINE_ONLY}
