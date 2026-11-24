"""今の中核（OcAgent）の土台。旧エンジン（blackwall/agent.py）から、中核が使う物だけを写した（2026-10-21）。

オーナー「いろんな OSS を参考に極限まで Blackwall の無駄を省きますか…仕組みも OSS の使えそうな部分にすべて置き換えてもらっていい」
→ 旧エンジン（LAPS_ENGINE=legacy・minimal）と工程の流し方（phases）を外した（DESIGN-slim-20261021.md 段 3）。
旧エンジンの Agent は 86 のメソッドを持っていたが、中核が使っていたのは初期化と 14 だけ。旧エンジンが初期化で組み立てていた
道具の一覧（tools.by_name）も、中核が自分の一覧で置き換えるので組み立てない。中身は原本のまま（注記も含めて）写した。
"""
from __future__ import annotations

from laps import _envcompat
import functools
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..backend import Backend
from .. import backend as _backend_mod
from ..session import Session
from .. import context_budget
from .. import checkpoint as checkpoint_mod


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


#: 中身を取り出す版（`take_think`）。★捕獲群を持つ点だけが違う。
_THINK_BODY_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


#: ★開きタグを伴わない閉じタグ（2026-09-21）。Qwen 系はテンプレートが `<think>` を
#:   プロンプトへ焼き込むので、生成は**思考の途中から**始まり `</think>` で終わる。
#:   `_THINK_RE` は組でしか一致しないため、この形だと思考が丸ごと本文に出た（実測）。
#:   aider の `remove_reasoning_content` と同じ手当て: 閉じタグまでを落とす。
_THINK_CLOSE_LIT = "</think>"


_THINK_ORPHAN_CLOSE_RE = re.compile(r"^.*?</think>", re.DOTALL)


#: 1 手番ぶんの思考の上限（字）。長い思考は末尾を残して畳む。
_THINK_KEEP_CHARS = 1200


def take_think(text: str, keep_chars: int | None = None) -> str:
    r"""`<think>…</think>` の**中身だけ**を返す（`strip_think` の対）。

    ★2026-09-21・実測で決めた（オーナー「OSS の実コードと仕組み、動作を調べてるの？」）。
      goose と BLACKWALL の要求を**中継で記録して突き合わせた**ところ、決定的な差が 1 つ:

          goose     : assistant **32/32 通**が `reasoning_content` を持って履歴に載る（7,548 字）
          BLACKWALL : **0 通**（思考は 1 字も戻していない）

      ⇒ goose のモデルは「もう Win64 は見た」を**自分の言葉で**覚えている。
        こちらは毎回ゼロから同じ結論を導き直していた（＝反復の正体）。
    ★`content` には混ぜない。**別枠 `reasoning_content` で返す**——goose と同じ形で、
      テンプレート側が扱いを決められる（`content` に混ぜると本文と見分けが付かない）。
    ★keep_chars（2026-10-08）: 切り詰めの長さ。既定は _THINK_KEEP_CHARS（旧中核・窓 32K の前提）。
      oc は大きい値を渡す（oc/loop.py の注記）——切った思考を送り返すと、推論機の前置きの使い回しが
      思考の頭で外れ、その手番の生成を丸ごと読み直すため。
    """
    out = [m.group(1) for m in _THINK_BODY_RE.finditer(text or "")]
    body = "\n".join(x.strip() for x in out if x and x.strip())
    keep = _THINK_KEEP_CHARS if keep_chars is None else keep_chars
    if len(body) > keep:
        # 末尾を残す（結論は後ろにある）
        body = "…（前略）…" + body[-keep:]
    return body


def strip_think(text: str) -> str:
    """`<think>` の塊だけを落とす（他は一切触らない）。

    ★2026-09-21。推論機が別枠に出す思考を backend が `<think>` として本文へ
      戻すようにしたので、**そのまま会話履歴へ積むと毎ターン送り返される**。
      Qwen の chat template は前ターンの思考を落とす規約で、32K の窓では
      思考を積むだけで文脈が溢れる（実測で 1 回 1,617 字）。
      ⇒ **表示には出す。履歴には積まない。**

    `_clean_reply` を使わない理由: あちらは R- 札・道具の echo・畳みの札も
    消すので、履歴に残すべきものまで変えてしまう。
    """
    text = _THINK_RE.sub("", text)
    if _THINK_CLOSE_LIT in text:
        text = _THINK_ORPHAN_CLOSE_RE.sub("", text, count=1)
    return text


_USER_MARK = "[利用者]\n"


#: 中断した依頼の後ろに残す印（次の手番のモデルが「続けなくてよい」と分かる）
CANCEL_MARK = ("[中断] 利用者がこの依頼を中断しました。指示が無い限り、この依頼の続きは行いません。")


#: 中断だが**利用者ではない**とき（達成して止めた等）。理由をそのまま記録に残す。
#: ★2026-09-01: 工程が検収に合格した時点で止めるようになり、その走行が記録上
#:   「利用者が中断した」と残っていた＝**完遂を中断と数える**。事実と違う印は付けない。
STOP_MARK = "[終了] {why} これ以上この依頼の作業は行いません。"


def _user_part(goal: str) -> str:
    """serve.py が付ける包み（[アプリからの指示]…[この実行での事実]…）を外し、利用者の文だけにする。

    ★2026-08-23 端末版の実走で判明: 包みには「使える道具: …, TodoWrite, **Write**」が入り、
      `_ARTIFACT_ASK`（IGNORECASE）が道具名の Write に当たって、**どんな依頼も「物を作れ」**と
      判定されていた。「1+1 は？」にまで「ファイルが1つも作られず」と差し戻し、モデルが
      hello.py を勝手に作った（3 回中 3 回）。判定は利用者の文だけで行う。
    """
    i = goal.rfind(_USER_MARK)
    return goal[i + len(_USER_MARK):] if i >= 0 else goal


#: 対の欠けた道具呼び出しに、送る直前に補う結果の文面。
_UNRUN_TOOL_RESULT = ("（この呼び出しは**実行されませんでした**。ハーネスが先に差し戻したか、走行がここで中断されました。"
                      "結果はありません。必要なら、もう一度呼んでください。）")


#: 読めない引数を履歴に残すとき、何をしようとしたかが分かるように残す先頭の字数。
_BAD_ARGS_KEEP = 200


def _json_tool_args(m: dict) -> dict:
    """道具の呼び出しの引数が JSON の object として読めないものを、読める形に替えた写しを返す（無ければ m のまま）。

    ★2026-10-13（オーナー「エージェントの内部思考が途中でおかしくなったようです」）: 引数が繰り返しに崩れて
      生成の上限で切れ、閉じていない JSON がそのまま会話の記録に残った。llama.cpp は送られた引数を JSON として
      読んでから書式に描くので、次の要求から毎回 HTTP 500（Failed to parse tool call arguments as JSON）を返し、
      走行は失敗した。保存された会話にも残るので、「続けて」と頼むたびに同じ 500 になる。
      ⇒ 読めない引数は、先頭の一部を文字列として包んだ object に替える（何を呼ぼうとしたかは残る）。
    """
    fixed = None
    for k, c in enumerate(m.get("tool_calls") or []):
        fn = c.get("function") if isinstance(c, dict) else None
        if not isinstance(fn, dict):
            continue
        raw = fn.get("arguments")
        if not isinstance(raw, str):
            continue
        try:
            ok = isinstance(json.loads(raw or "{}"), dict)
        except ValueError:
            ok = False
        if ok:
            continue
        if fixed is None:
            fixed = [dict(x) if isinstance(x, dict) else x for x in m["tool_calls"]]
        cut = raw[:_BAD_ARGS_KEEP] + ("…" if len(raw) > _BAD_ARGS_KEEP else "")
        fixed[k] = {**fixed[k], "function": {**fn, "arguments": json.dumps(
            {"unreadable_arguments": cut}, ensure_ascii=False)}}
    return m if fixed is None else {**m, "tool_calls": fixed}


def pair_tool_calls(msgs: list[dict]) -> list[dict]:
    """assistant の `tool_calls` に**結果が付いていない**ものへ、「実行されなかった」という結果を補った写しを返す。

    ★記録（`session.messages`）は変えない。推論機へ**送る写しだけ**を整える。
    ★なぜ要るか（2026-09-20・敵対レビュー F11-1）: assistant の発言は道具を実行する**前**に記録へ積む。
      その後、ループ検知の差し戻し（`continue`）や打ち切り（`break`）、中断、プロセスの死で、結果を積まずに
      次へ進む経路がある。実測: 保存された会話 1,160 本のうち **55 本（4.7%）・92 件**で、
      道具を呼んだ発言の直後が `user`（または末尾）だった（7 本呼んで結果 0 本の例もある）。
      モデルから見ると「呼んだのに返事が無い」記録で、**自分で結果を書き足す**きっかけになる
      （逆向きの欠損＝「呼んでいないのに結果がある」は 2026-08-16 に直している。こちらが残っていた）。
    OSS: cline は再開時、結果の無い道具呼び出しに「中断されたので完了していない」という結果を補う。
    ★JSON として読めない引数も、ここで読める形に替える（_json_tool_args・2026-10-13）。
    """
    out: list[dict] = []
    i, n = 0, len(msgs)
    while i < n:
        m = msgs[i]
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if calls:
            m = _json_tool_args(m)
            calls = m["tool_calls"]
        out.append(m)
        i += 1
        if not calls:
            continue
        want = [str(c.get("id") or "") for c in calls]
        got_ids: set[str] = set()
        got_n = 0
        while i < n and msgs[i].get("role") == "tool":
            out.append(msgs[i])
            got_n += 1
            if msgs[i].get("tool_call_id"):
                got_ids.add(str(msgs[i]["tool_call_id"]))
            i += 1
        if got_ids:
            missing = [w for w in want if w and w not in got_ids]
        else:
            missing = want[got_n:]            # id の無い古い記録は、数で合わせる
        for w in missing:
            fix: dict = {"role": "tool", "content": _UNRUN_TOOL_RESULT}
            if w:
                fix["tool_call_id"] = w
            out.append(fix)
    return out


def _bump(obj, key: str, n: int = 1) -> None:
    """counters を安全に増やす。**モジュール関数**であることが要点。

    ★この現場の試験は Agent の 1〜2 メソッドだけを殻に束ねて叩く
      （`test_cancel_holes._A` など）。`self._bump` にすると殻に無い属性を触って
      AttributeError になり、周りの `except Exception: pass` が**黙って飲み込む**。
      同じ形で `_append_note` を一度壊している。数える道具を、呼び出し側の器に
      依存させない。
    """
    try:
        c = getattr(obj, "counters", None)
        if c is None:
            c = obj.counters = {}
        c[key] = c.get(key, 0) + n
    except Exception:  # noqa: BLE001 — 数え損ねても走行は止めない
        pass


def _emit_safe(obj, event_type: str, payload: dict) -> None:
    """`_emit` があれば呼ぶ。無い器（殻）でも落ちない。"""
    fn = getattr(obj, "_emit", None)
    if not callable(fn):
        return
    try:
        fn(event_type, payload)
    except Exception:  # noqa: BLE001
        pass


#: `_ensure_inject` の初回作成を直列化する（Agent 個別ではなくプロセス共通で足りる）
_INJECT_INIT_LOCK = threading.Lock()


def _stamps_run_end(fn):
    """run() の**どの出口を通っても**「この走行が終わった時刻」を刻む。

    ★2026-09-02 に実機で判明した故障（穴A の直しが作った副作用）:
      `_run_ended_at` を刻んでいたのは**最後まで走り切った出口 1 か所だけ**で、
      中断・推論器の失敗・hook の遮断・clarify/identity の早期 return は素通り。
      すると次の run() 冒頭の判定——

          for_this_run = cancel_at is not None and (prev_end is None or cancel_at > prev_end)

      が、**前の走行を止めた中断を「この走行宛て」と読み続ける**。
      `_run_ended_at` は二度と進まないので判定は永久に真のまま。
      ＝**停止ボタンを一度押したら、その後の依頼が全部 iter 0 で
        「(interrupted by user)」になる。** 実測（同一 Agent を使い回し）:

          run1 →「できました。」 run2（中断）→ (interrupted by user)
          run3 → (interrupted by user)   run4 → (interrupted by user)

      2026-08-16 に直したはずの故障（「一度でも停止ボタンを押したセッションは
      以後まともに動かなくなる」）の再発。既存の試験が緑だったのは、試験が
      run() の判定を**写した殻**（`test_cancel_holes._A.start_run/end_run`）を
      叩いていて、本番の `end_run` に当たる行を通らない出口を再現できなかったため。
      ★時刻の刻みも検知器と同じで、本番と試験が同じ 1 本を通ること。

    刻みを `finally` に置く＝出口を 1 つ足したときに忘れようがない。
    ★装飾で包むのは、`inspect.getsource(Agent.run)` で本体を読む試験が
      十数本あるため（`functools.wraps` の `__wrapped__` を `inspect` が辿る）。
    """
    @functools.wraps(fn)
    def _wrapped(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        finally:
            # 例外で抜けた回も刻む（刻まないと、落ちた走行の後ろが全部中断扱い）。
            self._run_ended_at = time.monotonic()
    return _wrapped


#: 推論機が答えていない窓（/props の失敗）を探り直すまでの秒数（Agent._ctx_window）。
NCTX_REPROBE_SECS = 60.0


#: Agent._ctx_window が /props の答えで LAPS_N_CTX に入れた値（次に探ったときも上書きしてよい）。
_NCTX_WRITTEN: str | None = None


@dataclass
class AgentState:
    """走行の状態（中核が使うのは手番の数と書いたファイルだけ）。"""
    iter: int = 0
    files_written: list[str] = field(default_factory=list)


class Agent:
    """中核の土台。推論機・会話の記録・道具の器・数え・中断と割り込みの受け口・控え（checkpoint）を持つ。"""

    def __init__(self, *, subagent: bool = False, session: Session | None = None, on_event: "Any | None" = None) -> None:
        self.subagent = subagent
        self.backend = Backend()
        self.backend.on_wait = self._on_backend_wait
        # 道具の開始と結果を対にする id（記録と線の両方に同じ物を残す）。読み取り道具は並行に走るので鍵で守る。
        self._call_seq = 0
        self._call_lock = threading.Lock()
        self.on_event = on_event
        self.session = session or Session()
        self.tools: dict = {}
        self.state = AgentState()
        self.counters: dict[str, int] = {}
        self.evidence: list[dict] = []
        self.checkpoint: "checkpoint_mod.Checkpoint | None" = None
        self._goal = ""
        self._n_ctx: int | None = None

    def inject(self, text: str, force: bool = False) -> None:
        """走行中に、人の一言を次の観測へ混ぜる。

        ★force（強制割り込み・2026-10-09）は oc の中核（oc/loop.py OcAgent.inject）だけが使う。
          この旧中核では区切り（道具の後）で渡すまま＝force は無視する。

        ★中断（request_cancel）との違い: **作業を捨てない**。
        「止める」しか無いと、方向を変えたいだけのときも最初からやり直しになる
        （エージェント UI が普通のチャットと違う点として、ClaudeCode の設計メモが
        名指ししていた欠落）。

        ★モデルの発言としてではなく **user の割り込み**として入れる。
        自分の言葉として混ぜると、モデルが「自分がそう決めた」と読む。
        """
        t = (text or "").strip()
        if not t:
            return
        self._ensure_inject()
        with self._inject_lock:
            self._injected.append(t)

    def _ensure_inject(self) -> None:
        # request_cancel と同じ遅延初期化の作法に揃える（__init__ を触らない）。
        # ★競合で作り直さない（2026-09-02）。旧: `hasattr` を見てから作る形だったので、
        #   利用者の `inject()`（UI の別スレッド）と走行側の `take_injections()` が
        #   同時に初回を通ると**錠と待ち行列が2組でき、先に入れた一言が黙って消える**。
        #   割り込みは「作業を捨てずに方向を変える」ための唯一の口で、消えても
        #   エラーは出ない＝利用者からは「言ったのに無視された」に見える。
        with _INJECT_INIT_LOCK:
            if not hasattr(self, "_inject_lock"):
                import threading
                self._injected: list[str] = []
                self._inject_lock = threading.Lock()   # 最後に立てる（これが完成の印）

    def take_injections(self) -> list[str]:
        """溜まっている割り込みを取り出す（取り出したら消える）。"""
        self._ensure_inject()
        with self._inject_lock:
            out, self._injected = self._injected, []
        return out

    def request_cancel(self, reason: str = "") -> None:
        """中断の旗を立て、飛んでいる HTTP を閉じて、走っている Bash を孫ごと殺す。

        ⚠ **この docstring の旧文面は事実に反していた**（2026-09-02 に訂正）。
          旧文面はこう書いていた——「ベンチでは検査ごとに新しい Agent を
          作るので `close()` は次のケースへ漏れない」。**アプリは Agent を使い回す。**
          ベンチの前提がそのまま実運用の説明として置かれていたせいで、
          2026-08-16 の実機故障（停止ボタンの後、前の問いの答えが 0.0 秒で返る／
          次の質問に前の生成の続きが出る）を招いた。
          **間違った助言は、助言が無いより悪い。**
          いまは `run()` の冒頭が `backend.soft_reset()` で開き直す（配線あり）。

        後始末の責任:
          ・旗（`_cancel_event`）— 次の `run()` が冒頭で下ろす。ただし
            **この走行宛て**の中断（`_cancel_at` が前の走行の終わりより後）は残す。
          ・推論器 — `run()` 冒頭の `soft_reset()` が `_hard_closed` を解く。
          ・走っている Bash — ここで `kill_running()`（反復の境界まで待たない）。
        """
        if not hasattr(self, "_cancel_event"):
            import threading
            self._cancel_event = threading.Event()
        self._cancel_event.set()
        # ★止めた理由を覚える（記録に「利用者が中断」と嘘を書かないため）。
        self._stop_reason = str(reason or "")
        # ★穴A（2026-08-23・敵対レビューで判明）: run() は冒頭で旗を消す（前の走行の
        #   中断を持ち越さない正しい処置）。だが serve が `_current` を公開してから
        #   run() が旗を消すまでの間に「止める」が来ると、**run() 自身が旗を消して
        #   走り切る**。エラーも警告も出ない。
        #   → 中断を「世代」で数える。run() は自分が始まった世代を覚え、
        #     それより**新しい**中断だけを旗の消去から守る。
        import time as _time
        self._cancel_at = _time.monotonic()
        # ★穴B: 道具の実行中は中断を見ていなかった（Bash は最長 720 秒）。
        #   走っている Bash を孫ごと殺す。
        # ★止められなかったことを**残す**（2026-09-02）。旧: 両方とも
        #   `except Exception: pass` だったので、走っている Bash（最長 720 秒）を
        #   殺し損ねても、飛んでいる HTTP を閉じ損ねても、記録に 1 文字も出なかった。
        #   利用者から見える症状は「止めたのに止まらない」だけで、原因が読めない。
        try:
            from ..tools import bash as _bash
            # ★2026-09-06（会議 [space:推論機不達]）: 旧は kill_running() の戻りを見ずに無条件で +1 していた
            #   ＝「Stop が押された」以上の意味が無いのに「Bash を殺した」と読めた（私と ClaudeCode が誤読）。
            #   殺した件数だけ足す。0 なら立てない。
            _n = int(_bash.kill_running() or 0)
            if _n > 0:
                self.counters["cancel_killed_bash"] = self.counters.get("cancel_killed_bash", 0) + _n
        except Exception as e:  # noqa: BLE001 — 中断そのものは続ける
            _bump(self, "cancel_kill_failed")
            _emit_safe(self, "cancel_error", {"what": "kill_running", "error": str(e)})
        try:
            self.backend.close()
        except Exception as e:  # noqa: BLE001
            _bump(self, "cancel_close_failed")
            _emit_safe(self, "cancel_error", {"what": "backend.close", "error": str(e)})

    def _is_cancelled(self) -> bool:
        return getattr(self, "_cancel_event", None) is not None and self._cancel_event.is_set()

    def _on_backend_wait(self, attempt: int, waited: float) -> None:
        """推論機が read timeout まで黙り、生存確認のうえ送り直す瞬間。**黙って**
        送り直すと利用者には沈黙が続くだけなので、1 行にして流す（serve が表示）。"""
        self._emit("notice", {
            "kind": "engine_retry", "attempt": int(attempt), "waited_secs": int(waited),
            "message": (f"[engine_retry {int(attempt)}] 推論機から {int(waited)} 秒応答が無く、"
                        "生存を確かめたうえで送り直しています"),
        })

    def _emit(self, event_type: str, payload: dict) -> None:
        """Ω-N: fire-and-forget event for TUI subscribers. Never raise.

        ★`self.on_event` を素で読まない（2026-09-02）: 「決して例外を出さない」と
          書いてある関数が、属性の無い殻では **AttributeError を出していた**。
        """
        if getattr(self, "on_event", None) is None:
            return
        try:
            self.on_event(event_type, payload)
        except Exception:
            pass

    def _backend_base_url(self) -> str | None:
        """試験の差し替えバックエンドには cfg が無い。無ければネットを触らない。"""
        cfg = getattr(self.backend, "cfg", None)
        return getattr(cfg, "base_url", None) if cfg is not None else None

    def _ctx_window(self) -> int:
        # ★推論機が答えていない窓（/props が失敗して env か既定に落ちた）は、NCTX_REPROBE_SECS 経ってから探り直す
        #   （2026-10-16 の点検: 最初の /props が 1 回失敗しただけで、銘柄表の既定 32,768 をこのエージェントの間ずっと使った）。
        if self._n_ctx is None or (getattr(self, "_n_ctx_guess", False)
                                   and time.time() - getattr(self, "_n_ctx_at", 0.0) >= NCTX_REPROBE_SECS):
            n, src = context_budget.probe_window_ex(self._backend_base_url())
            self._n_ctx = n
            self._n_ctx_guess = src != "props"
            self._n_ctx_at = time.time()
            # ★道具にも窓を知らせる（2026-08-19）。`Read` は返す量をこれで決める。
            #   知らせないと 50,000 文字を返し、**窓 16,384 の推論器では窓より大きい**
            #   ＝1回の Read で会話が入らなくなる。
            #   ★探った結果を env に置く＝道具側で HTTP を張り直さない。
            #     既に指定があるなら尊重する（利用者が明示した値を上書きしない）。
            #   ★ただし銘柄表が入れただけの値（backend.PROFILE_SEEDED_N_CTX）と、前にここで入れた値は、推論機が答えた窓で
            #     上書きする（2026-10-16: 旧は 32,768 のまま残り、窓 131,072 の推論機でも道具の出力を 32K 用に切っていた）。
            global _NCTX_WRITTEN
            env = os.environ.get("LAPS_N_CTX") or ""
            if src == "props" and env != str(n) and (
                    not env or env in (_backend_mod.PROFILE_SEEDED_N_CTX, _NCTX_WRITTEN)):
                _envcompat.setenv("LAPS_N_CTX", str(n))
                _NCTX_WRITTEN = str(n)
            else:
                _envcompat.setdefault("LAPS_N_CTX", str(self._n_ctx))
        return self._n_ctx

    def _begin_checkpoint(self) -> None:
        """依頼ごとに控えを張り替える。失敗しても本流は止めない。"""
        old = getattr(self, "checkpoint", None)
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        self.checkpoint = None
        if not checkpoint_mod.enabled():
            return
        try:
            # session id だけだと、同じ session を読み直した別プロセスと
            # 控えの置き場所がぶつかる（前の実行の控えが混ざる）。連番＋乱数で分ける。
            self._run_seq = getattr(self, "_run_seq", 0) + 1
            tid = f"{self.session.id}_{self._run_seq}_{uuid.uuid4().hex[:6]}"
            self.checkpoint = checkpoint_mod.begin(tid)
            self.counters["checkpoint_begin_fired"] = (
                self.counters.get("checkpoint_begin_fired", 0) + 1)
        except Exception as e:  # noqa: BLE001
            self.counters["checkpoint_error"] = self.counters.get("checkpoint_error", 0) + 1
            self._emit("checkpoint_error", {"tool": "begin", "error": str(e)})

    def _checkpoint_before_tool(self, name: str, inp: dict) -> None:
        """A5: 書き込み系ツールの直前に控えを取る。配線だけで、判断はモジュール側。

        **絶対に例外を出さない**。控えが取れないことは編集を止める理由にならない
        （この層の不具合でエージェントが動かなくなるのが一番まずい）。
        ★戻すのはここではない。戻すのは人が明示的に呼んだときだけ（cli の
        `/rollback`）。失敗したら黙って巻き戻す、をやると利用者が意図した変更まで
        消える。
        """
        cp = getattr(self, "checkpoint", None)
        if cp is None:
            return
        try:
            if name in checkpoint_mod.UNTRACKABLE_TOOLS:
                # Bash が何を変えたかは分からない。分からないことを分からないと
                # 記録しておかないと、rollback が「全部戻しました」と嘘をつく。
                cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                cp.note_untracked(name, str(cmd))
                self.counters["checkpoint_untracked"] = (
                    self.counters.get("checkpoint_untracked", 0) + 1)
                return
            paths = checkpoint_mod.paths_for_tool(name, inp)
            for st in cp.record_many(paths):
                # 0発火と無効果を区別できるようにする（既存の規律）。
                key = "checkpoint_skipped" if st.startswith("skipped:") else f"checkpoint_{st}"
                self.counters[key] = self.counters.get(key, 0) + 1
                if st == "skipped:store_full":
                    # ★第4回c（反証 must_fix 4）: 木ごとの控え＋復元前の控えが合算上限を食うと
                    #   （run53 級の作業場で 64MiB の 65%）以降の record が黙って飛び、利用者の
                    #   /rollback の守備範囲が静かに縮む。回数を単独の鍵で出して走行後に読める
                    #   ようにする（`checkpoint_skipped` は他の理由と混ざる）。
                    self.counters["checkpoint_record_skipped_store_full"] = (
                        self.counters.get("checkpoint_record_skipped_store_full", 0) + 1)
        except Exception as e:  # noqa: BLE001 — 控えの失敗で本流を止めない
            self.counters["checkpoint_error"] = self.counters.get("checkpoint_error", 0) + 1
            self._emit("checkpoint_error", {"tool": name, "error": str(e)})

    def _workspace_changed(self) -> bool | None:
        """作業場が**実際に**変わったか。判定できなければ None（嘘をつかない）。

        ★なぜ `acted` / `_produced_anything` では足りないか（2026-09-11・vdir #v94 の実測）:
          vdir が同じ故障・同じ依頼文で現場の大きさだけ変えたところ、大きい現場では
          **1本もファイルを書かずに `accepted`** で終わった。だが Bash で調査はしているので
          「道具を実行したか」では捕まらない。**「調べただけ」と「直した」は利用者に
          とって全く違う**のに、記録上は同じに見えていた。

        ★`files_written` だけでも足りない: Bash の `sed -i` や `> file` による変更を
          見落とす。checkpoint は `bash_effects_since_watermark()` を持っているので使う。

        ★判定しない場合に False を返さない: 控えが無い状況で「変更なし」と断定すると、
          本当は直っている回を「何もしていない」と誤って貼ることになる。
        """
        if self.state.files_written:
            return True
        ck = getattr(self, "checkpoint", None)
        if ck is None:
            return None
        try:
            if ck.touched():
                return True
            if ck.untracked_since_watermark():
                return True
            eff = ck.bash_effects_since_watermark() or {}
            if any(bool(v) for v in eff.values()):
                return True
            return False
        except Exception:
            return None

    def _set_last_changed(self) -> bool | None:
        """`_workspace_changed()` を1回だけ確定させ、serve が締めに載せられるよう控える。

        ★毎回計算し直さない理由: checkpoint の走査は大きい現場では安くない
          （vdir の実測で 40 本・17,904 行の現場が問題になっている）。
        """
        v = self._workspace_changed()
        self._last_changed = v
        return v

    def close(self) -> None:
        # ★依頼の控え（checkpoint）もここで捨てる（2026-10-16 の点検: serve は依頼ごとにエージェントを作って close するだけで、
        #   控えは次の _begin_checkpoint でしか捨てられなかった＝依頼ごとに書き換えたファイルの写しが置き場に溜まり続けた）。
        #   CLI の /rollback は close の前（同じエージェントの次の依頼まで）に使う。
        cp = getattr(self, "checkpoint", None)
        if cp is not None:
            try:
                cp.close()
            except Exception:  # noqa: BLE001
                pass
            self.checkpoint = None
        self.backend.close()
