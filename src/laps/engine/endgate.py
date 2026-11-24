"""終わる手番の確かめ。言い回しではなく状態で見る（2026-10-30 オーナー「概要だけ説明してタスク終わらせる」）。

モデルが道具を呼ばずに手番を終えたとき、見るのは 2 つだけ:
  1. 頼まれたのに何も実行していない（世界を変える道具が 0 回で、手順を書いた・完了と言った・そもそも道具を呼んでいない）
  2. 予定表に未完了が残っている（手本: oh-my-opencode の todo の続行）

どちらも回数を決めて促すだけで、走行を止めない。質問への答えや、利用者に聞いている手番には当てない。"""
from __future__ import annotations

import re

_QUESTION = re.compile(r"[?？]\s*$|教えて|説明|とは|なぜ|なんで|どう(?:すれば|やって|なって|思)|どれ|どこ|いつ|ですか|ますか|でしょうか|"
                       r"\b(?:how|what|why|which|where|when|who|explain|is|are|does|do|can|could|should)\b", re.I)
#: 何かを作る・変える・持ってくる依頼（読むだけで「終わりました」と言ったら差し戻す）。
_MAKE = re.compile(r"作って|作成|直して|直せ|修正|追加|書いて|入れて|消して|削除|集めて|取得|取って|ダウンロード|インストール|移して|変えて|変更|"
                   r"実装して|更新して|リファクタ|デプロイして|ビルドして|移行して|最適化して|"
                   r"\b(?:make|create|fix|add|write|install|delete|remove|collect|download|build|implement|change|move|update|rename|refactor|deploy)\b", re.I)
#: 続きを頼む言い方。
_GO = re.compile(r"進めて|続けて|(?<!どう)やって|お願いします|\b(?:go ahead|continue|do it)\b", re.I)
#: 走らせる依頼（読むだけの命令でもよい。道具を 1 回も呼ばなかったときだけ差し戻す）。
_RUN = re.compile(r"実行|走らせ|動かして|試して|調べて|確認して|見て|\b(?:run|check|look|test)\b", re.I)
_EXEC = re.compile(r"実行|走らせ|動かして|試して|\brun\b", re.I)
_FENCE = re.compile(r"```[ \t]*(bash|sh|shell|zsh|console|powershell|pwsh|ps1|cmd|bat|batch)?[ \t]*\n(.*?)```", re.S | re.I)
_DONE = re.compile(r"完了(?:しました|です)|終わりました|できました|完成|しました。?\s*$|\b(?:done|completed|finished)\b", re.I)
_ASKS = re.compile(r"[?？]\s*$")
_ASK_END = re.compile(r"(?:ですか|ますか|でしょうか)[。．\s]*$")
#: 問いの形の頼み（「直してくれますか？」「Can you fix this?」）。尋ねているのではなく、やってほしい。
_POLITE = re.compile(r"[てで](?:くれ|くださ|下さ|もらえ|いただ|ほし|欲し)|\b(?:can|could|would|will) you\b|\bplease\b", re.I)
#: 計画だけを頼む言い方（作業へ押し戻さない）。
_PLAN_ONLY = re.compile(r"計画だけ|整理だけ|まとめ(?:る)?だけ|方針だけ|案だけ|一覧だけ|まだ(?:実装|作業|着手|実行)しないで|"
                        r"plan only|just (?:a )?plan|don'?t (?:start|implement)", re.I)
#: 止める・待つ言い方。「止めてくれない？」は問いの形でも問いとして扱わない。
_STOP = re.compile(r"止め|止ま|やめ|待って|待て|ストップ|中止|中断|キャンセル|もういい|いらない|要らない|不要|ここまで|終わりにし"
                   r"|\b(?:stop|wait|pause|hold on|cancel|abort|never mind)\b", re.I)


def only_question(request: str) -> bool:
    r = (request or "").strip()
    # 「走らせて、どれが落ちたか見せて」は問いの語があっても頼み（？で終わるなら問い）。「調べて教えて」は問いのまま。
    return bool(_QUESTION.search(r)) and not (_MAKE.search(r) or _GO.search(r) or (_EXEC.search(r) and not _ASKS.search(r)))


def no_work_asked(text: str) -> bool:
    """計画だけ・実行しないで、と頼まれたか（2026-10-10 の予定表の促しの決まり。作り直しで落としていた）。"""
    from ..oc import evidence
    return bool(_PLAN_ONLY.search(text or "")) or evidence.asks_not_to_run(text)


def is_question(text: str) -> bool:
    """問いか。「もう追加しましたか？」のように作る語が入っていても、問いの形なら問い。"""
    t = (text or "").strip()
    return bool(_ASKS.search(t) or _ASK_END.search(t)) or only_question(t)


def asks_to_stop(text: str) -> bool:
    return bool(_STOP.search(text or ""))


def unrun_commands(body: str, ran: list[str]) -> list[str]:
    """答えの中の、この走行で走らせていない命令。"""
    joined = "\n".join(ran)
    out: list[str] = []
    for lang, code in _FENCE.findall(body or ""):
        if not lang and not re.match(r"\s*(?:\$ |cd |ls |mkdir |py |python|pip |npm |git |curl |wget |bash |sudo )", code):
            continue
        for line in code.splitlines():
            s = line.strip().lstrip("$> ").strip()
            if s and not s.startswith(("#", "//", "@echo", "rem ")) and s not in joined:
                out.append(s)
    return out


def check(request: str, body: str, *, tools_used: int, effects: int, ran: list[str], todos: list[str], handoffs: int,
          todo_nudges: int) -> tuple[str, str]:
    """(種類, モデルへの一言)。問題なければ ("", "")。"""
    if _ASKS.search((body or "").strip()) or no_work_asked(request):
        return "", ""
    # ★尋ねられただけのものを「やれ」と押さない（「削除するコマンドは何ですか？」に答えたら、道具を強制して実際に消させかけた）
    if effects == 0 and handoffs < 2 and not (is_question(request) and not _POLITE.search(request or "")):
        req = request or ""
        work = bool(_MAKE.search(req) or _GO.search(req))
        cmds = unrun_commands(body, ran) if work else []
        said_done = work and bool(_DONE.search((body or "")[-400:]))
        idle = tools_used == 0 and (work or bool(_RUN.search(req)))
        if cmds or said_done or idle:
            what = ("Your message gives commands that you did not run." if cmds else
                    "Your message says the work is done, but nothing was changed or run in this task." if said_done else
                    "You ended your turn without doing anything.")
            return "handoff", (f"[Harness] {what} The user asked you to do it, not to explain it. Do the work now with "
                               "tools. If you truly cannot, say exactly what blocks you and what you tried.")
    if todos and todo_nudges < 2:
        items = "\n".join(f"- {t[:160]}" for t in todos[:10])
        return "todo", ("[Harness] Your plan still has unfinished items:\n" + items + "\nContinue with the next one. "
                        "If an item is done or no longer needed, update the plan with TodoWrite; if you are blocked, say why.")
    return "", ""
