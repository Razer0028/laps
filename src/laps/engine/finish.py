"""終える前の確かめ。モデルが道具を呼ばずに手番を終えたときに見る（走行を閉じる前の最後の関門）。

見るのは状態だけで、推論機は呼ばない。差し戻す点は 1 通にまとめ（1 つずつ差し戻すと答えを毎回書き直す）、差し戻しの文では
「答えは画面に残っているので書き直さない」と伝える。1 走行 3 回まで。差し戻した後に作業の道具を呼ばずにまた終えたら、それ以上は戻さない。
中身の判定は blackwall/oc の部品（evidence・checktool・diffreview・mediacheck・disclose・selfreview・reading）。

頼まれたのに何もしていない・予定が残っている、の確かめは endgate.py（走行の中身が無いときの確かめ）。"""
from __future__ import annotations

import collections
import difflib
import os
import re
from typing import Any

from .aids import _quiet
from .memory import _is_cache

#: 「終わったら報告します・通知が来ます」の形（2026-11-11 オーナー: 列に投げた仕事を待つと言って止まったまま再開しなかった）。
_PROMISE = re.compile(
    r"(終わ(った|り次第)|完了(した|次第)|済んだ|揃った|上がった).{0,40}(報告|知らせ|通知|確か|続け|仕上げ|再開)"
    r"|通知が(来|届)|後で(報告|確認|続け)|見届けて(から)?(報告|続け)"
    r"|\b(?:I'll|I will|will)\s+(?:report|check back|let you know|follow up|continue|pick (?:it|this) up)\b[^.\n]{0,80}\b(?:when|once|after)\b"
    r"|\byou(?:'ll| will) (?:get|be) (?:a )?notif", re.I)

END_ROUNDS_MAX = 3
CONTINUE_MAX = 4
#: ファイルを変えた走行のうち、この回数以上の道具を使ったものだけ終える前に見直させる（小さい依頼に 1 周足さない）。
REVIEW_MIN_TOOLS = 8
_HEAD = re.compile(r"^\[Harness\]\s*")
EXIT_NOTE = (" If your turn is already rightly finished — the user only asked a question and your message answers it, or you need "
             "something only the user can give (which file or folder, a choice between options) — end the turn again with that "
             "reply and no tool call.")
_CONTINUE = {
    "announce": ("Your message says what you will do next, but you ended the turn. Do it now: call the tool.",
                 "Stopped after announcing a step; continued"),
    "remaining": ("Your message says work is still left, so the task is not finished. Continue with the next step now (call the "
                  "tool). Do not end the turn until the task is done or you are truly blocked.", "Said work remains; continued"),
    "failed": ("Your message says something failed. The task is not done. Find the cause and try another way now (call a tool). "
               "Only stop if there is no way forward, and then say exactly what is needed.", "Stopped while failing; asked for another way"),
    "code_only": ("You pasted code instead of doing the work. Write it to a file and run it yourself with the tools, check the "
                  "result, then report what happened.", "Pasted code only; asked to run it"),
    "permission": ("You asked the user for permission. The user runs you in auto mode and does not want to be asked before you run "
                   "commands, edit files or take the next step of their request. Do it now (call the tool), check the result, and "
                   "report what you did.", "Asked permission mid-task; continued (auto)"),
}


def _review_list(text: str) -> bool:
    """本文の半分以上の行が ✓/✗ の行か。"""
    from ..oc import selfreview
    ok, ng = selfreview.counts(text)
    return ok + ng >= 1 and (ok + ng) * 2 >= sum(1 for x in text.splitlines() if x.strip())


def _without_review_rows(text: str) -> str:
    """末尾の ✓/✗ の行を除いた本文。"""
    from ..oc import selfreview
    rows = text.splitlines()
    while rows and (not rows[-1].strip() or selfreview.OK_LINE.match(rows[-1]) or selfreview.NG_LINE.match(rows[-1])):
        rows.pop()
    return "\n".join(rows)


def _has_value(body: str) -> bool:
    """短くても答えの中身があるか（数字・識別子・パス、または文）。「完了。」「調べました。」は中身が無い。"""
    return bool(re.search(r"\d|\w[._/-]\w|`", body)) or any(len(x.strip()) >= 15 for x in re.split(r"[。．!！?？\n]", body))


class Finish:
    def __init__(self, agent: Any, aids: Any) -> None:
        self.a, self.aids = agent, aids
        self.last_error = ""
        self.rounds = 0
        self.round_calls = -1
        self.kept = ""
        self.fold_next = False             # 次の手番の本文は思考の欄へ流す（確かめの手番の語りで答えを埋めない）
        self.pushes: collections.Counter = collections.Counter()
        self.continued_last = False
        self.done: set[str] = set()
        self.review_at: int | None = None
        self.review_pushed = False
        self.review_reported = False
        self.review_cited: list[str] = []
        self.draft_hidden = False          # 見直しの前に書いた報告を画面に出していない（見直しの後に 1 回だけ報告させる）
        self.draft_held = False            # loop がこの手番の本文を預かっている（見直しをこの手番で頼んだら、出さずに隠す）

    def work_calls(self) -> int:
        a = self.a
        return a.tool_calls - a.tool_counts.get("TodoWrite", 0)

    # ── 本文が「まだ終わっていない」と言っている ──
    @_quiet(("", False))
    def unfinished(self, body: str) -> tuple[str, bool]:
        """(モデルへの一言, 道具を必ず呼ばせるか)。2026-10-12 オーナー「タスク完了していないのに勝手に終わるんですよね」。"""
        from ..oc import reading
        from . import endgate
        a = self.a
        was_pushed, self.continued_last = self.continued_last, False
        n = self.pushes["all"]
        asked = endgate.only_question(a.request)
        if n >= CONTINUE_MAX or (asked and not a.tool_calls):
            return "", False
        why = reading.unfinished(body, asked=asked)
        if not why:
            return "", False
        if why == "failed" and asked and getattr(self.aids, "last_ok", True):
            return "", False               # 調べて「238 回が失敗しました」と答えた本文。いま失敗しているのではない（2026-10-31 課題集: 正しい答えを差し戻し 120 秒のうち 67 秒）
        if why in ("announce", "remaining", "question") and was_pushed and reading.prose_len(body) >= 400:
            return "", False               # 促した直後にしっかりした答えを返した＝モデルが終えると決めた
        if why == "question":
            if self.pushes["question"] >= 1:
                return "", False
            text = (f"The user already told you what they want: \"{a.request[:300]}\". Continue with the most reasonable reading of "
                    "that request and do the work. Ask only if it is truly impossible to proceed without an answer.")
            short = "Asked back what was requested; continued"
        else:
            if why == "permission" and (not a.effects or self.pushes["permission"] >= 3):
                return "", False
            if why in ("failed", "code_only") and self.pushes[why] >= 1:
                return "", False           # 1 回言っても同じなら、モデルの判断として受け取る
            text, short = _CONTINUE[why]
        self.pushes["all"] += 1
        self.pushes[why] += 1
        a._count("continue_push")
        a._count(f"continue_push:{why}")
        if why in ("announce", "remaining", "question", "failed"):
            text += EXIT_NOTE
        self.continued_last = True
        a.out.notice("announce_nudge", a.rid, message=short)
        return f"[Harness] {text} (Reply to the user in {a.lang()} when you finish.)", n >= 2 or why == "permission"

    # ── 状態で決める確かめ ─────────────────
    @_quiet("")
    def end_checks(self, body: str) -> str:
        """差し戻す点があれば 1 通にして返す。無ければ ""。"""
        a = self.a
        if self.rounds >= END_ROUNDS_MAX:
            a._count("end_round_capped")
            return ""
        if self.rounds and self.work_calls() <= self.round_calls and not self._review_unchecked():
            a._count("end_round_repeat")   # 差し戻した後に作業せずまた終えた＝書き直しただけ
            return ""
        notes: list[str] = []
        asked_before = self.review_at is not None
        for fn in (self._autoverify, self._diffreview, self._unverified, self._verdict, self._media, self._claims,
                   self._caveat, self._promise, self._self_review):
            try:
                n = fn(body)
            except Exception as e:
                a._count("finish_error")
                self.last_error = f"{fn.__name__}: {type(e).__name__}: {e}"
                n = ""
            if n:
                notes.append(_HEAD.sub("", n.strip()))
        if not notes:
            return ""
        if self.draft_held and not asked_before and self.review_at is not None:
            self.draft_hidden = True       # 見直しをこの手番で頼んだ＝預かった報告は出さない
            a._count("review_draft_hidden")
        self.kept = self.merge(body)
        self.rounds += 1
        self.round_calls = self.work_calls()
        a._count("end_rounds")
        self.fold_next = True
        if len(notes) == 1:
            text = "[Harness] " + notes[0]
        else:
            text = (f"[Harness] Before you finish, {len(notes)} things need attention:\n\n"
                    + "\n\n".join(f"{i}) {x}" for i, x in enumerate(notes, 1)))
        if self.draft_hidden:
            # ★見直しの前の報告は画面に出していない＝見直しの後に、報告を 1 回だけ書かせる
            self.kept = ""
            self.fold_next = False
            return (text + "\n\nYour answer above has NOT been shown to the user; it was only a draft. Do not think the whole task "
                    "through again from the start. Do what is needed with tools, then write the final report to the user once, in "
                    f"{a.lang()}: what you did, what is ready now (with paths), what the review found and what you fixed, and what is "
                    "left. Outcome first; one fact per bullet; a table for numbers that compare several items. Do not mention the draft.")
        return (text + "\n\nYour answer above stays on the user's screen: do not write it again, and do not think the whole task "
                f"through again from the start. Do what is needed with tools, then finish with a short note in {a.lang()} with only "
                "what you changed, checked or found now (if something in your answer above was wrong, say what).")

    def _once(self, key: str) -> bool:
        if key in self.done:
            return False
        self.done.add(key)
        return True

    def _autoverify(self, body: str) -> str:
        """コードを書き換えたまま確かめずに終えようとしたら、プロジェクト自身の速い検査（linter・型）を 1 回走らせる。試験は走らせない。"""
        from ..oc import checktool, evidence
        a = self.a
        if (os.environ.get("LAPS_AUTOVERIFY") or "1").strip() == "0" or "autoverify" in self.done:
            return ""
        v = evidence.verdict(self.aids.ledger)
        if v.get("status") != "unverified" or evidence.asks_not_to_run(a.request):
            return ""
        paths = [p for p in (v.get("paths") or []) if p]
        roots = [r for r in dict.fromkeys(checktool.project_root(p) or "" for p in paths) if r]
        if len(roots) != 1:                # プロジェクトの外、または複数にまたがる＝自動では決めない
            return ""
        root = roots[0]
        rel = [checktool.cp.rel(os.path.abspath(p), root) for p in paths if (checktool.project_root(p) or "") == root]
        cmds = [c for c in checktool.detect(root, "auto", rel) if c.kind in ("lint", "types") and c.name not in ("gradle", "maven")]
        if not cmds:
            return ""
        self.done.add("autoverify")
        a._count("autoverify:ran")
        results = [checktool.run_one(c, timeout=120) for c in cmds]
        text, ok = checktool.report(results, "auto", root)
        evidence.observe(self.aids.ledger, "Check", {"kind": "auto"}, ok, text)
        a._count(f"autoverify:{'passed' if ok else 'failed'}")
        a.out.notice("autoverify", a.rid, message="Checked the changes automatically: passed" if ok
                     else "Checked the changes automatically: problems found")
        if ok:
            return ""
        return ("I ran this project's own checks on the files you changed and they did not pass. Fix these, then say what you "
                "changed in your answer:\n" + text)

    def _diffreview(self, body: str) -> str:
        """この走行で足した行を 1 回見直す（衝突の印・デバッグ出力・秘密らしい値など）。差し戻すのは衝突の印と秘密だけ。"""
        from ..oc import diffreview
        a = self.a
        paths = list(dict.fromkeys(p for _, p in self.aids.ledger.edits))
        if not paths or "diffreview" in self.done:
            return ""
        self.done.add("diffreview")
        text, hard = diffreview.review(paths, a.ctx.workspace)
        if not text:
            return ""
        a._count("diffreview:flags", text.count("\n- "))
        a.out.notice("diffreview", a.rid, message=(text.split("\n", 1)[1] if "\n" in text else text)[:400])
        if not hard:
            return ""
        a._count("diffreview:pushback")
        return text

    def review_pending(self) -> bool:
        """終えようとしたら見直しを頼む状態か（2026-11-06 オーナー「タスク完了後に報告してからレビューを行って更に報告を行ってくる。
        レビューを最後に行うタスクの場合はレビュー後に報告させる」）。loop はこの間の本文を預かり、終える手番なら画面に出さない。"""
        from ..oc import selfreview
        try:
            return bool(selfreview.enabled() and self._review_due())
        except Exception:
            return False

    def _review_due(self) -> bool:
        from ..oc import selfreview
        return (selfreview.enabled() and self.review_at is None and not self.a.review_asked and bool(self._review_targets())
                and self.work_calls() >= self._review_min())

    @staticmethod
    def _review_min() -> int:
        try:
            return int(os.environ.get("LAPS_SELF_REVIEW_MIN_TOOLS") or REVIEW_MIN_TOOLS)
        except ValueError:
            return REVIEW_MIN_TOOLS

    def _unverified(self, body: str) -> str:
        """編集の後に何も走らせていない。見直しを頼む回には言わない（同じことを 2 回言わない）。"""
        from ..oc import evidence
        a = self.a
        v = evidence.verdict(self.aids.ledger)
        if v.get("status") != "unverified" or evidence.asks_not_to_run(a.request) or self._review_due():
            return ""
        if not self._once("unverified"):
            return ""
        a._count("verify_nudge")
        a.out.notice("verify_nudge", a.rid, message=f"Edits unverified ({', '.join((v.get('files') or [])[:3])}); asked to run a check")
        return evidence.unverified_message(v, evidence.suggest_checks(v.get("paths") or [], a.ctx.workspace))

    def _verdict(self, body: str) -> str:
        """編集後の確認が落ちたのに、答えが失敗に触れていない。"""
        from ..oc import evidence
        led = self.aids.ledger
        v = evidence.verdict(led)
        if v.get("status") != "failed" or led.pushbacks >= 1 or evidence.acknowledges_failure(body):
            return ""
        led.pushbacks += 1
        a = self.a
        a._count("verdict_pushback")
        code = f" → exit {v['exit']}" if v.get("exit") is not None else ""
        a.out.notice("verifier", a.rid, message=f"Claimed done after failed check `{v['cmd']}`{code}; sent back")
        return evidence.pushback_message(v)

    def _media(self, body: str) -> str:
        """答えに挙げた作った物（画像・音声・動画など）が拡張子どおりの中身でない（2026-10-12 オーナー「サポートされていない形式」）。"""
        from ..oc import evidence
        a = self.a
        if not body.strip() or "media" in self.done:
            return ""
        n, bad = evidence.bad_outputs(body, a.ctx.workspace, a.started_at, remote=evidence.ran_remote(a.t.raw))
        if not bad:
            return ""
        self.done.add("media")
        a._count("media_pushback")
        lst = "; ".join(f"{os.path.basename(p)}: {why}" for p, why in bad[:5])
        a.out.notice("verifier", a.rid, message=f"{len(bad)} of {n} outputs don't match their type; sent back")
        return (f"I checked {n} of the files your answer says you produced, and {len(bad)} are not what their extension says ({lst}). "
                "They will not open. Find out why (Read one output file and one source file to compare the first bytes), fix the "
                "conversion, check an output again, and only then answer. Do not say files open correctly unless you checked their contents.")

    def _claims(self, body: str) -> str:
        """答えに挙げた場所が実在しない。"""
        from ..oc import evidence
        a = self.a
        if not body.strip() or "claims" in self.done:
            return ""
        miss = evidence.missing_claims(body, a.ctx.workspace, remote=evidence.ran_remote(a.t.raw))
        if not miss:
            return ""
        self.done.add("claims")
        a._count("claims_pushback")
        a.out.notice("verifier", a.rid, message=f"Answer names {len(miss)} paths that do not exist; sent back")
        return ("Your answer says you made or saved these, but they do not exist: " + "; ".join(miss)
                + ". Check where the files really are and correct the answer, or make them.")

    def _caveat(self, body: str) -> str:
        """答えは「調べられなかった」と言うのに、書いた文書にそれが書いていない（文書だけ読む人に伝わらない・2026-10-20）。"""
        from ..oc import disclose
        a = self.a
        if "caveat" in self.done:
            return ""
        quote = disclose.limitation(body)
        if not quote:
            return ""
        missing = disclose.caveat_missing(list(a.ctx.written))
        if not missing:
            return ""
        self.done.add("caveat")
        a._count("caveat_pushback")
        a.out.notice("verifier", a.rid, message="Answer says it could not look things up, but the file does not; sent back")
        return disclose.caveat_message(quote, missing, a.lang())

    def _promise(self, body: str) -> str:
        """「終わったら報告します」と書いて終えるのに、待っている物が無い（走行は閉じ、誰も起こさない・2026-11-11）。1 走行 1 回。"""
        a = self.a
        if getattr(a, "child", False) or "promise" in self.done:
            return ""
        m = next((x for x in _PROMISE.finditer(body or "") if "ください" not in body[x.end():x.end() + 8]), None)
        if not m:
            return ""                      # 「終わったら確かめてください」は利用者へのお願い（約束ではない）
        jobs = getattr(a.ctx, "jobs", None)
        if jobs is not None and jobs.waiting():
            return ""
        self.done.add("promise")
        a._count("promise_pushback")
        a.out.notice("verifier", a.rid, message="Promised to report later with nothing waiting; sent back")
        return (f"You wrote \"{m.group(0)[:80]}\", but nothing here is waiting for that work: when you end the turn the run closes "
                "and nobody wakes you. If the work runs on another machine or in a queue, start a waiter now with Bash "
                "run_in_background=true: `bwtool wait --until '<command that succeeds when it is done>' --every 60` "
                "(for example a check that the result file exists over ssh), then end your turn; you are woken when it exits. "
                "If you cannot check it from here, tell the user plainly to send \"continue\" when it is done.")

    def _changes(self) -> list[str]:
        a = self.a
        out: list[str] = []
        ch = a.memory.changes if a.memory else {}
        for how in ("created", "modified", "deleted"):
            out += [f"{how} {p}" for p in sorted(ch.get(how) or ()) if not _is_cache(p)]
        if not out:
            out = [f"edited {p}" for p in dict.fromkeys(a.ctx.written)]
        return out

    def _review_targets(self) -> list[str]:
        """見直しの相手。作業フォルダの変更に、外で変えた物（サーバの設定など）を足す。
        ★2026-11-02 Terminal-Bench の git-multibranch: 作業がすべて /git と /etc だったので、変更なしと見て見直しが走らなかった。"""
        return self._changes() + [f"system: {c}" for c in dict.fromkeys(c for c, ok in self.aids.sys_changes if ok)]

    def _review_unchecked(self) -> bool:
        at = self.review_at if self.review_at is not None else (self.a.review_mark if self.a.review_asked else None)
        return at is not None and not self.review_pushed and self.work_calls() <= at

    def _self_review(self, body: str) -> str:
        """作業したモデル自身の敵対レビュー（2026-10-21 オーナー「親モデルが自分で敵対レビューするように」「レビューしてとかでも反応するように」）。"""
        from ..oc import evidence, selfreview
        a = self.a
        if not selfreview.enabled():
            return ""
        if self.review_at is None and not a.review_asked:
            if not self._review_due():
                return ""
            self.review_at = self.work_calls()
            a._count("self_review_asked")
            # ★手元の推論機では見直しの思考を浅くする（2026-10-31 課題集の paginate: 試験が通った後の見直しで 468 秒考え、
            #   自分の試験の書き間違いから正しい修正を git checkout で戻した）。見直しそのものは残す（オーナーの決まり）。
            if a.llm.info().llama:
                a.effort = "low"
            todos = [str(t.get("content") or "").strip()[:160] for t in (a.ctx.todos if a.ctx.todos_touched else [])
                     if isinstance(t, dict) and t.get("status") != "cancelled" and t.get("content")]
            a.out.notice("review", a.rid, message="Review: checking its own work adversarially before finishing")
            self.review_cited = evidence.passed_checks(self.aids.ledger)
            if self.review_cited:
                a._count("self_review_cited", len(self.review_cited))
            return selfreview.before_finish(str(a._goal()).strip(), self._review_targets(), a.lang(), todos, self.review_cited)
        if self._review_unchecked():
            self.review_pushed = True
            a._count("self_review_no_tools")
            return selfreview.NO_TOOLS_CITED if self.review_cited else selfreview.NO_TOOLS
        if not self.review_reported:
            self.review_reported = True
            ok, ng = selfreview.counts(body)
            a._count("self_review_ok", ok)
            a._count("self_review_ng", ng)
            a.out.notice("review", a.rid, message=f"Review: {ok} ✓, {ng} ✗" if ok or ng else "Review: the answer has no ✓/✗ review list")
        return ""

    # ── 最後の答え ────────────────────────
    @_quiet("")
    def weak_report(self, body: str) -> str:
        """終わりの答えが報告になっていない（コードを貼っただけ・短すぎる）。1 回だけ書かせる（2026-10-12 オーナー「途中経過を報告しないで終了する」）。"""
        from ..oc import evidence
        from . import endgate
        a = self.a
        if "report" in self.done:
            return ""
        why = evidence.weak_final(self.merge(body), self.work_calls(), japanese=a._ja())
        if not why or (why == "too_short" and endgate.only_question(a.request) and _has_value(body)):   # 調べて値を答える問いは短くて正しい
            return ""
        self.done.add("report")
        a._count("final_report")
        a._count(f"final_report:{why}")
        a.out.notice("answer_nudge", a.rid, message="No final report; asked for one")
        return (f"[Harness] Before you finish, write the report to the user in {a.lang()}. Cover: 1) what you did, 2) what you found "
                "and what is ready now (with paths), 3) what did not work and why, 4) what is left, or what you need from the user. "
                "Outcome first in one or two lines; one fact per bullet; a table for numbers that compare several items; no code unless the user asked for it.")

    def merge(self, body: str) -> str:
        """差し戻す前に画面に出た答えに、差し戻しの後の短い一言を足す。書き直した（長い・似ている）なら新しいほう。"""
        kept, new = self.kept.strip(), (body or "").strip()
        if not kept or self.draft_hidden:
            return body
        if not new or new in kept or self.a.last_fold:
            return kept
        if kept in new:
            return body
        if _review_list(new):              # 見直しの ✓/✗ の一覧は書き直しではなく追記（長さが近いと報告が消えて、短いと差し戻していた）。前の一覧は新しい一覧に替える
            self.a._count("end_answer_merged")
            base = _without_review_rows(kept)
            return f"{base}\n\n{new}" if base else new
        if len(new) >= 0.7 * len(kept):
            return body
        if difflib.SequenceMatcher(None, kept[:3000], new[:3000]).ratio() >= 0.6:
            return body
        self.a._count("end_answer_merged")
        return kept + "\n\n" + new

    @_quiet(None)
    def report(self, body: str) -> None:
        """確かめの結果を画面に残す（✓ 通った・⚠ 確かめていない・落ちたまま）。"""
        from ..oc import evidence
        a = self.a
        v = evidence.verdict(self.aids.ledger)
        a._count(f"verdict:{v['status']}")
        acked = evidence.acknowledges_failure(body)
        nv = evidence.notice_for(v, acked)
        if nv:
            # ★答えの下に残すのは「できたと言っているのに確かめが落ちている」「確かめていない」だけ。答えの中で失敗を伝えている（reported）
            #   ときは答えと重なるので状態行だけ（2026-11-21 オーナーの画面「タスク完了後にこれが表示されました」）。
            keep = v.get("status") == "unverified" or (v.get("status") == "failed" and not acked)
            a.out.notice(nv[0], a.rid, message=nv[1], **({"final": True} if keep else {}))

    @_quiet("")
    def footer(self, text: str) -> str:
        """この走行で変えたシステムの設定の一覧（最後の答えの下に付ける）。"""
        from ..oc import disclose
        changes = self.aids.sys_changes
        if not changes:
            return ""
        self.a._count("syschange_footer")
        return disclose.footer(changes, japanese=self.a._ja())

    @_quiet(None)
    def tidy_scratch(self, final: str) -> None:
        """この走行で作業場の直下に作った試しの台本を _scratch へ移す（2026-10-13 オーナー「任せます」）。答えで名前を挙げた物は動かさない。"""
        from ..oc.tools import _SCRIPT_EXTS, _THROWAWAY_NAME
        a = self.a
        if (os.environ.get("LAPS_SCRATCH_TIDY") or "1").strip().lower() in ("0", "false", "off", "no"):
            return
        here = os.path.abspath(a.ctx.workspace)
        moved: list[str] = []
        for p in list(dict.fromkeys(a.ctx.created)):
            ap = os.path.abspath(p)
            if os.path.normcase(os.path.dirname(ap)) != os.path.normcase(here) or not os.path.isfile(ap):
                continue
            name = os.path.basename(ap)
            stem, ext = os.path.splitext(name)
            if ext.lower() not in _SCRIPT_EXTS or not _THROWAWAY_NAME.search(stem) or name in final or name in str(a._goal()):
                continue
            try:
                d = os.path.join(here, "_scratch")
                os.makedirs(d, exist_ok=True)
                dst, k = os.path.join(d, name), 2
                while os.path.exists(dst):
                    dst, k = os.path.join(d, f"{stem}.{k}{ext}"), k + 1
                os.replace(ap, dst)
            except OSError:
                continue
            moved.append(name)
            a.ctx.created[a.ctx.created.index(p)] = dst
        if moved:
            a._count("scratch_tidied", len(moved))
            a.out.notice("harness", a.rid, message=f"Moved {len(moved)} throwaway scripts to _scratch: " + ", ".join(moved[:8]))  # 2026-11-20 オーナー「答えの下にまだ出る」→ 状態行だけ
