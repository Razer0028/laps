"""依頼の読み方の注記 — あいまいな日本語・誤字を、モデルが文脈で読み直せるようにする
（2026-10-06 オーナー「あいまいな日本語のプロンプトを適切に解釈させるようにできますか？誤字等も」）。

★実際の読み違い（オーナー機の走行記録・2026-10-05）:
  ・「どんなゲーム化調べて」（＝どんなゲーム**か**調べて・IME の変換違い）を字面どおり「ゲーム化
    （ゲーミフィケーション）」と読み、ウェブ検索して一般論を答えた。作業フォルダのゲームは見なかった。
  ・「MOD」とだけ送られた手番で、思考は「ユーザーは『このフォルダには何がある？』と聞いています」と
    一つ前の依頼に答え直した（次の発言「MODとか作れそう？」が本来の意図）。

ここでは**機械で確かに見分けられるものだけ**を注記にする（推論機は呼ばない・数ミリ秒）。意味の読み直し
そのものはモデルの仕事で、前置きの「Reading the user's request」節（oc/prompt.py）に書いた。
  1. 短い続きの発言 → 「前の依頼の続きとして、この発言に答える」（前の依頼の冒頭を添える）
  2. 「どんな…化調べて」の型 → 「化」は「か」（〜か）の変換違いかもしれない
  3. 依頼に出てくる英数字の名前が作業場に無く、よく似た実在の名前がある → その名前を添える
  4. 日本語の依頼なのに会話の写しが英語に寄っている（要約・直近の本文）→ 返答は日本語と名指しする（reply_language）
置き場は道具の案内と同じく、推論機へ送る依頼の発言の写しだけ（前置きは変えない＝キャッシュを崩さない）。
LAPS_READING_NOTES=0 で止める。
"""
from __future__ import annotations

import difflib
import os
import re

SHORT_CHARS = 12          # これ以下の発言は「続き」とみなす（「MOD」「結果は」「変換もしてみてください」）
_REFERENTIAL = re.compile(r"^(続け|つづけ|それ|さっき|先ほど|前の|同じ|もう一度|もういちど|やり直|再開|再度|"
                          r"結果|その後|次は|では|じゃあ|OK|ok|はい|お願いします|任せ)")
_QWORD = re.compile(r"(どんな|何|なに|なん|どこ|いつ|誰|だれ|どう|どれ|なぜ|いくつ|どの|どっち|どちら)")
_KA_SLIP = re.compile(r"([^\s、。]{1,12}?)(化|課|下|科|可|価|蚊)(調べ|しらべ|教え|おしえ|わか|分か|知り|しり|確かめ|"
                      r"チェック|見て|みて|判断|特定)")
_NAME_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{4,}")
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vs", "dist", "build"}
_MAX_NAMES = 600
_KANA = re.compile(r"[぀-ヿ]")
_JA = re.compile(r"[぀-ヿ㐀-鿿]")
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_COMPACTION_QUESTION = "What did we do so far?"      # oc/loop.py の COMPACTION_QUESTION と同じ文


def enabled() -> bool:
    return os.environ.get("LAPS_READING_NOTES", "1").strip() != "0"


# ★予告だけで止まる（2026-10-10・Gemma 4）: 本文の最後の一文が「次にやること」の予告なのに道具を呼ばずに手番を終える。
#   見るのは最後の一文だけ。申し出（「必要なら〜します」「let me know」）と問いかけは予告に数えない。
_ANNOUNCE_EN = re.compile(r"(?:^|[,;:]\s*)(?:(?:first|next|now|then|so|okay|ok)[,]?\s+)*(?:let'?s|let me|i(?:'ll| will| am going to))\s+\w", re.I)
_ACT_JA = (r"(?:確認|調べ|読み|読ん|見て|見ます|探し|探索|作成|作り|作っ|実行|開い|開き|書き|書い|修正|直し|進め|取り掛か|始め|試し|"
           r"調査|解析|整理|抽出|移動|コピー|導入|インストール|ビルド|テスト|検証|実装|追加|変更|編集|生成|展開|起動|保存|削除|"
           r"特定|停止|終了|再起動|止め|切り替え|設定|対応|対処|再開|続け|検索|取得|比較|参照)")
# 予告の語尾。★「〜します」だけでなく「〜ます」（探します・作ります・調べます・読みます）も（2026-10-11 の見回り: 0.1.393 の走行が
#   「…を探します。」の 1 文で道具を呼ばずに 2 回続けて終わり、促しが当たらなかった＝する動詞以外は全部外れていた）。
#   状態の言い方（あります・います・できます・なります・おります・〜ています）は予告ではないので除く。
_MASU_END = r"(?<!あり)(?<!てい)(?<!でい)(?<!でき)(?<!なり)(?<!おり)(?:ます|ましょう)[。．.!！]?$"
_ANNOUNCE_JA = re.compile(r"^(?:まずは?|次に|次は|これから|続いて|続けて|それでは|では|さらに|先に|最初に|引き続き|今から|ここから)[、,]?"
                          r".*" + _ACT_JA + r".{0,12}" + _MASU_END
                          + r"|" + _ACT_JA + r"[^。、\n]{0,8}(?:しましょう|してみます|していきます|てみます|ていきます|でみます|でいきます)[。．.!！]?$"
                          r"|(?:可能な限り|改めて|再度|もう一度|直ちに|すぐに|順に)[^。\n]{0,30}" + _ACT_JA + r"[^。、\n]{0,6}" + _MASU_END)
#: 割り込みへの返事で予定だけを書いて止まる形（「了解しました。…を停止させます。」）。書き出しが了承で、終わりが「〜ます。」で、
#: 終わった・できたことを報告していない短い本文。
_ANNOUNCE_JA_BARE = re.compile(_ACT_JA + r"[^。．!！?？]{0,12}" + _MASU_END)
_ACK_HEAD = re.compile(r"^(?:了解(?:しました|です)?|承知(?:しました|いたしました)?|わかりました|分かりました|かしこまりました|OK|Understood|Got it)[。、!！,.]?", re.I)
_DONE_JA = re.compile(r"(?:完了しました|終わりました|できました|済みました|しました。|でした。|ありました。|ません。)")
_OFFER = re.compile(r"必要(?:であれば|なら|に応じて)|ご希望|よろしければ|指示(?:を|が)|いかがでしょう|どう(?:しますか|でしょう)|"
                    r"if (?:you|needed|necessary)|let me know|would you like|do you want|shall i", re.I)


def _sentences(body: str) -> list[str]:
    s = _CODE.sub(" x ", body or "").strip()
    return [p.strip() for p in re.split(r"(?<=[。．！？!?])\s*|\n+", s) if p and p.strip()]


def asks_user(body: str) -> bool:
    """利用者に何か聞いているか（最後の 3 文のどれかが問い）。
    ★2026-10-13（作業用のコンテナ の試験）: 「解析したいファイルの場所を教えていただけますか？見つかり次第、調査を開始します。」を、
      最後の一文だけ見て予告と読み、3 回続けて「続けて」と促した＝モデルは共有フォルダを探し回り、関係の無い zip を取ってきた。
      聞いた後に先の予定を添えた本文は、予告ではなく聞き返しとして扱う。"""
    return any(p.endswith(("?", "？")) or _ASK_REQ.search(p) for p in _sentences(body)[-3:])


#: 利用者へのお願いの形で聞く文（「パスを教えてください。」「指定していただけますか。」）。問い（？）と同じに扱う。
_ASK_REQ = re.compile(r"(?:て|で)(?:ください|下さい|くれますか|もらえますか|いただけますか|いただけませんか)[。．!！]?$")


def prose_len(body: str) -> int:
    """本文の長さ（符号の塊と空白を除いた字数）。"""
    return len(re.sub(r"\s", "", _CODE.sub(" ", body or "")))


def announces_action(body: str, asked: bool = False) -> bool:
    """利用者への本文の最後の一文が、次にやる作業の予告か（道具を呼ばずに終えた手番に当てる）。
    asked=True は依頼が問いのとき。書き出しの無い形でも、主語のある文（「この関数は…を書き込みます。」）は答えの説明であって予告ではない。"""
    s = _CODE.sub(" x ", body or "").strip()
    if not s:
        return False
    parts = [p.strip() for p in re.split(r"(?<=[。．！？!?])\s*|\n+|(?<=\.)\s+", s) if p and p.strip()]
    if not parts:
        return False
    last = parts[-1]
    if len(last) > 300 or asks_user(body) or _OFFER.search(last):
        return False
    if _ANNOUNCE_JA.search(last) or _ANNOUNCE_EN.search(last):
        return True
    # ★書き出しの無い予告（2026-10-10 0.1.371 の走行: 本文が「X の中身を確認し、X が含まれているか…を確認します。」の
    #   1 文だけで、道具を呼ばずに終わった＝上の「まず／次に…」の規則に当たらなかった）。本文が短いときだけ当てる。
    if len(s) <= 300 and _ANNOUNCE_JA_BARE.search(last) and not (asked and "は" in last):
        return True
    # ★了承の書き出し自体（「了解しました。」）は「〜しました。」なので、終わった報告の判定から外して見る（2026-10-10 の点検）。
    head = _ACK_HEAD.match(parts[0])
    rest = s[head.end():] if head else s
    return bool(head and len(s) <= 800 and re.search(r"ます[。．.!！]?$", last) and not _DONE_JA.search(rest))


# ★終わっていない印（2026-10-12 オーナー「タスク完了していないのに勝手に終わるんですよね」）。手番が道具なしで終わったとき、
#   本文がまだ作業が残っていると言っていれば、走行を終えずに続けさせる（oc/loop._continue_pushback）。見るのは最後の 3 文。
_REMAIN = re.compile(r"まだ[^。]{0,20}(?:いません|ていない|できて|終わって|確認|残って|途中)|未完了|調査中です|終わっていません|完了していません|"
                     r"できていません|残っています|残りの作業|残りは|これから[^。]{0,30}ます|次に[^。]{0,30}ます|引き続き[^。]{0,30}ます|"
                     r"続けて[^。]{0,20}ます|続行します|再試行します|やり直します|試みます|予定です|"
                     r"\b(?:not (?:yet )?(?:done|finished|complete)|still need|still (?:investigating|looking|checking|working)|remaining|next,? i will|i will now|i'll now)\b", re.I)
_FAILED = re.compile(r"失敗しました|できませんでした|うまくいきませんでした|うまくいっていません|開けません|読めません|動きません|"
                     r"エラーが(?:出|発生)|\b(?:failed|could not|couldn't|did not work|didn't work)\b", re.I)
_DONE_CLAIM = re.compile(r"完了しました|終わりました|できました|保存しました|直しました|修正しました|確認しました|\b(?:done|completed|finished)\b", re.I)


#: 次の一手の許可を求める言い方（auto のとき・2026-10-12 オーナー「ユーザーに許可を求めないように」）。
_PERMISSION = re.compile(r"(?:して|て)(?:も)?(?:よろしい|いい|良い|大丈夫)|よろしいでしょうか|よろしいですか|"
                         r"(?:実行|修正|作成|削除|変更|適用|開始|続行|上書き|再実行|変換|移動|コピー)(?:し)?ますか|進めますか|"
                         r"\bshall i\b|\bshould i (?:proceed|go ahead|run|apply|continue|start)|\bmay i\b|"
                         r"\bdo you want me to\b|\bwould you like me to\b|\bok to proceed\b|\bproceed\?", re.I)


def unfinished(body: str, asked: bool = False) -> str:
    """本文が「まだ終わっていない」と言っていればその種類（announce / remaining / failed / code_only / question / permission）。
    言っていなければ ""。permission は auto（oc/prompt.auto_mode）のときだけ。
    asked=True は依頼が問いのとき（announces_action を参照・2026-10-31 課題集: 正しい答えの説明を予告と読んで差し戻した）。"""
    raw = (body or "").strip()
    if not raw:
        return ""
    s = _CODE.sub(" x ", raw).strip()
    prose = re.sub(r"\bx\b", " ", s)
    if raw.startswith("```") and len(re.sub(r"\s", "", prose)) < 120:
        return "code_only"
    if announces_action(raw, asked=asked):
        return "announce"
    parts = [p.strip() for p in re.split(r"(?<=[。．！？!?])\s*|\n+", s) if p and p.strip()]
    tail = " ".join(parts[-3:])
    if parts and parts[-1].endswith(("?", "？")) and _PERMISSION.search(parts[-1]) and not _DONE_CLAIM.search(prose):
        try:
            from .prompt import auto_mode
            if auto_mode():
                return "permission"                # 終えていないまま、次の一手の許可を求めた
        except Exception:
            pass
    if _OFFER.search(parts[-1] if parts else ""):
        return ""                                  # 申し出（「必要なら〜」）は残りの作業ではない
    if parts and asks_user(raw):
        if _DONE_CLAIM.search(prose):
            return ""                              # 頼まれた分は終えて、追加をやるか聞いている
        if len(prose) < 400:
            return "question"                      # 報告なしの聞き返しだけ
        return ""                                  # ★報告や答えを書いたうえで聞いている（2026-10-13）＝終えてよい
    if _REMAIN.search(tail):
        return "remaining"
    if _FAILED.search(tail) and not _DONE_CLAIM.search(tail):
        return "failed"
    return ""


# ★返答の言語（2026-10-09・走行記録 10/6〜9 の日本語の依頼 31 本）: 2 本で利用者への本文がほぼ英語になった
#   （10/7 17:02 は 21 段落中 20・10/8 17:58 の 3.7 時間の走行は 17 段落中 13）。どちらも要約の後の会話で、
#   最初の一言から英語だった（思考は英語・要約の指示や道具の結果も英語＝前置きの「利用者の言語で」が効かない）。
#   言語は機械で見分けて名指しする（「利用者の言語」とだけ書くと、英語ばかりの文脈では英語と読まれる）。
def reply_language(text: str, prev: list[str] | None = None) -> str:
    """利用者が書いた言語＝返答に使う言語。いまは日本語だけを見分ける（かなで判定）。分からなければ ""。"""
    if _KANA.search(text or ""):
        return "Japanese"
    if text and not re.search(r"[A-Za-z]{3}", text) and any(_KANA.search(p or "") for p in (prev or [])[-3:]):
        return "Japanese"                       # 「OK」「1」など英字の少ない短い発言は、直前の発言の言語
    return ""


def english_only(body: str) -> bool:
    """利用者に向けた本文が英語だけで書かれているか（符号の塊と `…` は数えない）。短い本文は見ない。"""
    s = _CODE.sub(" ", body or "")
    return not _JA.search(s) and len(re.findall(r"[A-Za-z]", s)) >= 40


def _english_context(history: list[dict]) -> bool:
    """会話の写しが英語に寄っているか（要約が英語・直近の自分の本文が英語）。"""
    if (len(history) >= 2 and isinstance(history[0], dict) and history[0].get("content") == _COMPACTION_QUESTION
            and english_only(str(history[1].get("content") or ""))):
        return True
    said = [str(m.get("content") or "") for m in history
            if isinstance(m, dict) and m.get("role") == "assistant" and str(m.get("content") or "").strip()]
    return any(english_only(s) for s in said[-6:])


_ANCHOR = re.compile(r"The user's request for this task, verbatim:\n「(.*?)」(?:\n|$)", re.S)


def _user_texts(history: list[dict]) -> list[str]:
    out = []
    for m in history:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            # ★要約の問いと要約の後の「続けて」は利用者の発言ではない（2026-10-09: 要約の後の短い発言に
            #   「前の依頼は 「Continue if you have next steps…」」と添えていた）。続きの指示に載せた依頼の原文を使う。
            if c == _COMPACTION_QUESTION:
                continue
            if isinstance(c, str) and c.startswith(("Continue if you have next steps",
                                                    "The previous request exceeded the model's context window")):
                a = _ANCHOR.search(c)
                if a and a.group(1).strip():
                    out.append(a.group(1).strip())
                continue
            if isinstance(c, str) and c.strip() and not c.lstrip().startswith(("[Harness]", "[Request interrupted")):
                from .base import _user_part      # serve.py の包み（[アプリからの指示]…[利用者]）を外す
                out.append(_user_part(c).split("\n\n<", 1)[0].strip())   # 写しに付けた <git…>/<tool_hints> なども外す
    return out


def _workspace_names(cwd: str) -> list[str]:
    names: list[str] = []
    try:
        top = list(os.scandir(cwd))
    except OSError:
        return names
    for e in top:
        if e.name.startswith(".") or e.name in _SKIP_DIRS:
            continue
        names.append(e.name)
        if e.is_dir(follow_symlinks=False):
            try:
                sub = list(os.scandir(e.path))
            except OSError:
                continue
            for s in sub[:200]:
                if not s.name.startswith(".") and s.name not in _SKIP_DIRS:
                    names.append(s.name)
        if len(names) >= _MAX_NAMES:
            break
    return names


def _name_slips(prompt: str, cwd: str) -> list[tuple[str, str]]:
    tokens = {t.strip("._-") for t in _NAME_TOKEN.findall(prompt)}
    tokens = {t for t in tokens if len(t) >= 5 and not t.lower().startswith(("http", "www."))}
    if not tokens:
        return []
    names = _workspace_names(cwd)
    if not names:
        return []
    low = {n.lower(): n for n in names}
    stems = {os.path.splitext(n)[0].lower(): n for n in names}
    out = []
    for t in sorted(tokens):
        tl = t.lower()
        if tl in low or tl in stems or any(tl in n for n in low):   # そのまま在る（名前の一部でもよい）
            continue
        hit = difflib.get_close_matches(tl, list(low) + list(stems), n=1, cutoff=0.82)
        if hit:
            real = low.get(hit[0]) or stems.get(hit[0])
            if real and real.lower() != tl:
                out.append((t, real))
    return out[:3]


def notes(prompt: str, history: list[dict], cwd: str) -> tuple[str, list[str]]:
    """(依頼の発言の写しに足す塊, 注記の種類)。何も無ければ ("", [])。"""
    text = (prompt or "").strip()
    if not text:
        return "", []
    lines: list[str] = []
    kinds: list[str] = []
    prev = _user_texts(history)
    if prev and (len(text) <= SHORT_CHARS or (_REFERENTIAL.match(text) and len(text) <= 25)):
        p = " ".join(prev[-1].split())
        p = p if len(p) <= 80 else p[:80] + "…"
        lines.append(f"- This is a short follow-up. Read it as continuing the current topic and the state of the work "
                     f"(the previous request was 「{p}」). Answer this new message; do not answer the previous one again.")
        kinds.append("followup")
    q = _QWORD.search(text)
    if q:
        tail = text[q.start():]                     # 疑問詞から先だけを見る（「どんな…化調べて」）
        for m in _KA_SLIP.finditer(tail):
            whole = tail[:m.end()]
            fixed = tail[:m.start(2)] + "か" + m.group(3)
            lines.append(f"- 「{whole}」 is probably an input-method slip for 「{fixed}」 "
                         f"(the question particle か, not the kanji {m.group(2)}). Use whichever reading fits the context.")
            kinds.append("ka_slip")
            break
    try:
        slips = _name_slips(text, cwd) if len(text) <= 2000 else []
    except Exception:
        slips = []
    for typed, real in slips:
        lines.append(f"- 「{typed}」 does not exist in the working folder; the closest existing name is 「{real}」.")
        kinds.append("name_slip")
    lang = reply_language(text, prev)
    if lang and _english_context(history):
        lines.append(f"- The user writes in {lang}. Write every message to the user in {lang}, even though earlier "
                     "parts of this conversation (the summary, your past messages, tool output) are in English. "
                     "Your thinking may stay in English.")
        kinds.append("reply_lang")
    if not lines:
        return "", []
    return "<reading_notes>\n" + "\n".join(lines) + "\n</reading_notes>", kinds
