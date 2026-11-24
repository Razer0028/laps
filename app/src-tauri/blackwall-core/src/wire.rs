//! 線（serve.py の NDJSON）で来る事象を、Windows 版の画面の言葉へ翻訳する**純粋な表**。
//!
//! ★なぜ本体（`src/sidecar.rs`）ではなくここに置くか（2026-09-04・W1）:
//!   `sidecar.rs` は `tauri` を参照するのでホスト（Linux）では `cargo test` が
//!   **リンクできない**（GTK 依存 pango が無い）。つまり `sidecar.rs` の
//!   `#[test]` 195 本は preflight でも `cargo check --tests`＝**コンパイルされる
//!   だけで一度も実行されていない**。この現場は「一度も走らせていない検査」を
//!   既に踏んでいる（Rust 195 本・`method-diversity.md`）ので、**両側の食い違いを
//!   決める表と判定は、実際に走る側（blackwall-core）に置く**。
//!   `sidecar.rs` は `#[path]` で参照するだけ＝実体は1本・二重管理は起きない
//!   （`llama_log.rs` と同じ作法）。
//!
//! ★突き合わせの正本は Python 側:
//!     印       `blackwall/agent.py` の `_push_back_on_*` が出す `[<gate> ` 印
//!     一覧     `blackwall/term/render.py` の `_RESTART_MARKS` / `_GATE_MARKS`
//!     outcome  `blackwall/term/render.py` の `_OUTCOME_JA` / `_ACCEPTED`
//!     notice   `blackwall/serve.py` が出す `kind`
//!   食い違いは `src/tests/test_windows_parity.py` が機械で捕まえる（目視は3回外している）。

use serde_json::Value;

// ---------------------------------------------------------------------------
// 本文の印（assistant と同じ型で流れてくるハーネスの通知）
// ---------------------------------------------------------------------------

/// この印で始まる通知は「いま書いた下書きは捨てて書き直す」の合図。
/// **本文を捨ててよいのはここだけ**（term の `_RESTART_MARKS` と同じ表）。
pub const RESTART_MARKS: &[&str] = &[
    "[fabrication ",
    "[pushback ",
    "[reset ",
    // 2026-09-03 の 3 つの門
    "[verifier ",
    "[red_tests ",
    "[rerun ",
    // ★発明・矛盾の門（同日夜）。差し戻し前の「完了しました」の下書きを画面に残すと
    //   同じ塊が 2 回見える。この 2 つも下書きを捨てる側。
    "[invent ",
    "[contradiction ",
];

/// ★2026-10-13: 文の印ではなく**種類**で「いま流れた下書きは答えではない」と決まる通知（term の `_RESTART_KINDS` と同じ表）。
///   degenerate_retry＝崩れた生成をやり直す／preempted＝強制割り込みで生成を途中で止めた／
///   answer_nudge＝思考だけ・英語の分析・繰り返し・報告なしの終わり方を書き直させた。
///   Gemma 4 で、崩れた途中の本文が答えとして残るのを TUI で見て足した（アプリも同じ穴）。
pub const RESTART_KINDS: &[&str] = &["degenerate_retry", "preempted", "answer_nudge"];

/// 捨てはしないが**目立たせる**印（term の `_GATE_MARKS` と同じ表）。
/// ★`RESTART_MARKS` とは別の分類: `host_scope` や `loop` は下書きを捨てる対象では
///   ないが、見落とすと実害が大きい（別ホスト未接触の指摘・堂々巡りの遮断）。
pub const GATE_MARKS: &[&str] = &[
    "[fabrication ",
    "[pushback ",
    "[reset ",
    "[verifier ",
    "[red_tests ",
    "[rerun ",
    "[host_scope ",
    "[invent ",
    "[contradiction ",
    "[offline ",
    "[search_limit ",
    "[loop]",
    "[Ω-",
    "[予算]",
    "[検査]",
];

/// 本文の下書きを捨てる印か。
pub fn discards_draft(message: &str) -> bool {
    RESTART_MARKS.iter().any(|m| message.starts_with(m))
}

/// 目立たせる印か。
pub fn is_gate_mark(message: &str) -> bool {
    GATE_MARKS.iter().any(|m| message.starts_with(m))
}

// ---------------------------------------------------------------------------
// done の outcome
// ---------------------------------------------------------------------------

/// `done` の `outcome` → 画面の言葉（term の `_OUTCOME_JA` と同じ表）。
/// 空文字の語は「言うことが無い」＝行を出さない。
pub const OUTCOME_JA: &[(&str, &str)] = &[
    ("accepted", ""),
    ("accepted_after_pushback", "差し戻しあり"),
    ("cancelled", "中断"),
    // 2026-09-04: AskUser の答えが停止の意図（回答の強制）
    ("user_stop", "利用者が終了"),
    ("max_iters", "反復上限"),
    ("context_exhausted", "文脈切れ"),
    ("context_overflow", "文脈あふれ"),
    ("budget_exhausted", "予算切れ"),
    ("loop_detected", "同じ手の繰り返し"),
    ("crashed", "本体が落ちた"),
    ("backend_error", "推論器に届かない"),
    // ★2026-10-30: 推論機が応答を断った（finish_reason=content_filter・oc/loop）。
    ("declined", "モデルが断った"),
    ("phase_failed", "工程 不合格"),
    ("unknown", "不明"),
    ("", "締めが来ていない"),
    // ★ここから下はアプリだけが作る outcome（本体は出さない）。
    //   `cancelled_grace` … 本体が STOP_GRACE 内に done を返さず、アプリ側で閉じた。
    ("cancelled_grace", "中断（本体の返事を待たずに閉じた）"),
];

/// 本体はもう出さない旧い成功値。**表には入れない**（term の `_OUTCOME_JA` と 1:1 で
/// 突き合わせたいので）が、古い本体に繋いだときに「不明な結末」と騒がないよう黙って通す。
pub const LEGACY_QUIET_OUTCOMES: &[&str] = &["ok", "done", "completed"];

/// 「うまくいった」として ✓ を出す outcome（term の `_ACCEPTED` と同じ）。
/// ★`user_stop` は利用者の意思どおりなので ✓ 側（失敗ではない）。
pub const ACCEPTED: &[&str] = &["accepted", "accepted_after_pushback", "user_stop"];

/// 画面の言葉。表に無ければ `None`（＝**未知の outcome**）。
pub fn outcome_word(outcome: &str) -> Option<&'static str> {
    OUTCOME_JA
        .iter()
        .find(|(k, _)| *k == outcome)
        .map(|(_, v)| *v)
}

/// 成功として締めてよい outcome か。
pub fn outcome_is_accepted(outcome: &str) -> bool {
    ACCEPTED.contains(&outcome)
}

/// `done` に添える 1 行。`None` なら何も言わない（accepted の既定）。
///
/// ★未知の outcome は**黙って捨てない**。内部識別子のまま
///   「終わり方: <識別子>（不明な結末）」と出す——隠すより、調べられるほうがよい。
pub fn outcome_line(outcome: &str) -> Option<String> {
    if LEGACY_QUIET_OUTCOMES.contains(&outcome) {
        return None;
    }
    match outcome_word(outcome) {
        Some("") => None,
        // ★うまくいった側には ✓ を付ける（2026-09-04・W1）。`user_stop`（利用者が
        //   AskUser で終了を指示）は**利用者の意思どおり**なので、失敗の見た目にしない。
        //   ✗ の側は付けない——画面の `.notice.is-gate::before` が付ける（二重にしない）。
        Some(w) if outcome_is_accepted(outcome) => Some(format!("✓ 終わり方: {w}")),
        Some(w) => Some(format!("終わり方: {w}")),
        None => Some(format!("終わり方: {outcome}（不明な結末）")),
    }
}

// ---------------------------------------------------------------------------
// notice の kind
// ---------------------------------------------------------------------------

/// 画面での目立たせ方。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    /// ふつうの注記（薄く 1 行）。
    Plain,
    /// 門・失敗の報せ。左線と赤で立てる。
    /// ★この現場は「失敗の左線が存在しない」を実機で踏んでいる（2026-09-03）。
    Gate,
    /// **知らない事象**。捨てずに「不明な事象」として出し、数える。
    Unknown,
}

impl Level {
    /// 画面（app.js）へ渡す文字列。
    pub fn as_str(self) -> &'static str {
        match self {
            Level::Plain => "plain",
            Level::Gate => "gate",
            Level::Unknown => "unknown",
        }
    }
}

/// 画面に出す文を持たないが、**知っている** kind。未知として数えない。
/// （アプリが送った命令への返事・診断だけの行）
pub const QUIET_KINDS: &[&str] = &[
    "ready", "heartbeat", "pong", "status", "run_start", "request", "iter", "init",
    // ★2026-10-16: 問いが閉じた（serve.py `_ask`）。文は出さない——translate.rs が箱を消す合図（AskClosed）に変える。
    "ask_closed",
];

/// 走行を終わらせる `error` の kind（`done` が来ない失敗）。それ以外の error は 1 行で出して `done` を待つ。
/// ★rejected＝依頼を受け付けなかった（serve.py `_accept_send`・done を出さない）／fatal＝本体の入力ループが落ちた。
pub const TERMINAL_ERROR_KINDS: &[&str] = &["rejected", "fatal"];

/// 失敗・割り込みの報せ。目立たせる kind。
/// ★term の `_NOTICE`（hook_error / handoff_gap / phase_retry / phase_failed が
///   `MARK_BAD`）と、`_ws_guard_notice` などの専用描画に対応する。
pub const GATE_KINDS: &[&str] = &[
    // 見張り（oc/monitor.py・2026-10-08）がエラーらしい行・命令の異常終了を見つけた（モデルが対応する）。
    "monitor_error",
    "hook_error",
    "handoff_gap",
    "phase_retry",
    "phase_failed",
    "ws_guard",
    "answer_guard",
    "offline", // ★2026-09-08 監査 C2: offline 門の結末（message 付き・本体と同じ言葉）
    "cancel_error",
    "checkpoint_error",
    "context_exhausted",
    "verifier",
    "red_tests",
    // ★r4b/app-term-clients（先取り配線・2026-09-05）: core-hold-fix が
    //   phases.py:1677 に足す予定の notice（手番の終わりの検収がベストより減って
    //   いたので直前のベストへ戻した＝ハーネスの安全網）。まだこの worktree の
    //   本体（serve.py）はこの kind を一度も出さない（別ワーカーの未マージ変更）
    //   ので `serve_notice_kinds()` の走査にも出てこないが、phase_retry と同格の
    //   「ハーネスの介入」を黙って流さない、という既存原則に従いここへ先に足す。
    //   term/render.py（`_NOTICE["phase_hold_restored"]`）・Android
    //   （`EventReducer.kt` の `onPlainNotice`）にも同じ形で足してある。
    "phase_hold_restored",
    // ★2026-09-06（owner 実機 P0）: serve.py が起動時に出す「この宛先はプロキシ経由に
    //   なる」の報せ。Windows の httpx は環境変数が空でも**レジストリ**の系プロキシを
    //   拾い、しかも除外リスト（ProxyOverride）は読まないので、LAN の推論機宛てが
    //   黙ってプロキシへ回る＝推論機に要求が 1 件も届かないまま heartbeat だけが続く。
    //   ふつうの注記に落とすと、いちばん読ませたい 1 行が薄い灰色で流れてしまう。
    "backend_proxy",
    // ★2026-10-05（オーナー実機で「不明な事象（notice kind=compaction_failed）」と出た）:
    //   oc 中核（blackwall/oc/loop.py）が出す失敗の報せ。表への登録を忘れたまま出荷していた
    //   （0.1.231 の gen_truncated と同じ型）。要約できない＝この先で文脈切れになる。
    "compaction_failed",
    // ★2026-10-10（オーナー実機で「不明な事象（notice kind=answer_nudge）」と出た）: oc 中核が出す報せを表に足し忘れていた。
    //   以後は出荷の連鎖の scripts/check_notice_kinds.py が本体（serve.py・oc/・agent.py）の kind と突き合わせる。
    //   degenerate_retry＝出力が崩れたので手番をやり直した／image_rejected＝推論機が画像を開けなかったので外した。
    "degenerate_retry",
    "image_rejected",
    // ★2026-10-30: 推論機が応答を断った（finish_reason=content_filter）。空の答えのまま黙って終えない。
    "declined",
];

/// 知っている・ふつうに 1 行で出す kind。
pub const PLAIN_KINDS: &[&str] = &[
    // 見張り（oc/monitor.py・2026-10-08）: ログ・命令の出力に新しく出た行の報せ。失敗ではない。
    "monitor",
    "harness",
    "notice",
    "injected",
    "inject_ignored",
    // 本体が差し込みを読んだ（2026-11-15・画面はその行を queued → read にする・走行中だけ）
    "inject_taken",
    "ask_expired",
    "queued",
    "queue_cleared",
    "session",
    "cancel_detail",
    "phase_guidance_dropped",
    "phase_plan",
    "phase_start",
    "phase_done",
    "phase_skip",
    "context_compacted",
    "context_reset",
    "engine_retry",
    // ★2026-09-21: 0.1.231 で core が出し始めた（生成が上限で切れたので続きを書かせる）。
    //   表に足し忘れて出荷し、実機では「不明な事象（notice kind=gen_truncated）」と出ていた。
    "gen_truncated",
    // ★2026-10-31: 子が要約した（記録に残すための知らせ・AFTER_KINDS には入れない）。
    "child_compacted",
    // ★2026-11-11（Codex から）: changes＝この依頼で変えたファイルと +/− 行（`/undo` で戻せる）。
    "changes",
    // ★2026-10-05: oc 中核の報せ。compacting＝要約を始めた（数分かかる）／doom_loop＝同じ道具を
    //   同じ引数で 3 回続けて呼んだ（止めずに知らせるだけ・opencode は許可を聞く）。
    "compacting",
    "doom_loop",
    // ★2026-10-10: oc 中核の促し（Gemma）と強制割り込み。answer_nudge＝答えが空・思考だけ・繰り返しを切った手番に一言／
    //   announce_nudge＝予告だけで止まった手番に一言／preempted＝強制割り込みで生成を止めて指示を先に渡した（2026-10-09 から未登録だった）。
    "answer_nudge",
    "announce_nudge",
    // ★2026-10-13: 小さい核（Gemma 4）で、道具を呼ばずに手番を終えたので、終わったか（Done）次の道具かを選ばせた。
    "done_check",
    // ★2026-10-13: 別の目（oc/review）。作業の語りを見せず、道具の結果とフォルダだけで報告・要約を点検した（始め・終わり・要約・詰まり）。
    "review",
    // ★2026-10-13: ホストの資源（oc/hostload）が限界に近い（メモリ・ディスク・GPU のメモリと温度・CPU の張り付き）ことを道具の結果に添えた。
    "host_low",
    "preempted",
    // ★2026-10-10: 編集の後に何も実行・試験せずに終えようとしたので、確かめるよう 1 回促した（Gemma）。
    "verify_nudge",
    // ★2026-10-17（開発の強化 第 1 段）: 促すのではなくハーネス自身が動いた 2 つ。
    //   autoverify＝終わる前にプロジェクト自身の速い検査（linter・型）を 1 回走らせた（oc/loop._autoverify）／
    //   diffreview＝足した行を見直した（衝突の印・デバッグ出力・秘密・試験の絞り込み・oc/diffreview）。
    "autoverify",
    "diffreview",
    // TodoWrite の項目が残ったまま整理だけで終えようとした → 続けさせた（oc/loop._todo_pushback・2026-10-10）。
    "todo_nudge",
    // ★2026-10-12: 途中経過の報告を頼んだ（oc/pace の 20 分ごと）。声の係はこの文で見分けて着信する。
    "checkpoint",
    // 背景の処理が動いたまま終えようとした → 終わりを待たせた（oc/loop._bg_pushback・2026-10-10）。
    "bg_wait_nudge",
    // 背景の仕事が動いている間は走行を閉じずに待つ（oc/loop._park_for_jobs・2026-10-28）。
    "bg_park",
    // ★2026-10-18: 前で走らせた長い Bash を止めずに背景の仕事（J1…）として続けた／走行の終わりに止めた（oc/loop._run_fg）。
    "auto_bg",
    // ★2026-10-18: 最初の下見（子を最初から使う・oc/loop._scout_first）が落ちた。
    "explore_first",
    "user_stop",
    "shutdown",
    // ★2026-09-05（r4/app-term-clients・Python 側の run50 実記録を再生していて発見）:
    //   `blackwall/tools/task.py` が委譲した子の道具活動を `kind="subagent"` の
    //   notice 1本にまとめて親へ中継する（`"子: " + msg`）。この表にもう1つ下の
    //   `is_gate_mark` と同じ形の判定（`kind == "subagent" && t.contains('✗')`）が
    //   あるので、既定は PLAIN（▶ 開始・✓ 成功の中継）——子の道具が実際に失敗した
    //   ときだけ Gate へ昇格する。実測: run50-52 の3本の実記録で 120 件発火・
    //   うち 5 件が ✗ 失敗（`verifier`/`red_tests` の0件よりずっと高頻度）。
    //   端末版（term/render.py の `_on_notice`）にも同じ形の判定を入れてある。
    "subagent",
    // ★2026-10-23: モデルが書いたスクリプトの中から bwtool で Blackwall の道具を呼んだ（数と失敗・oc/bwtool）。
    "bwtool",
    // ★2026-10-30: 作業フォルダの外のシステムの設定を変える呼び出しを、本文で言わなかったのでハーネスが 1 行知らせて走らせた（oc/disclose）。
    "syschange",
];

/// 中断の連鎖（`cancel` / `cancel_applied` / `cancelled`）。
/// ここでは翻訳しない——`CancelTrack` が 1 本にまとめる。
pub const CANCEL_KINDS: &[&str] = &["cancel", "cancel_applied", "cancelled"];

/// 走行が終わった後も答えの下に残す kind。**ここに無い kind は走行中だけ**（状態行の一言にする・答えの下には残さない）。
/// 本体が `final: true` を付けた notice（走行の終わりの状態: 作ったファイル・止めた仕事・残したサーバー・確かめの警告）も残す。
/// ★2026-10-28 オーナーの画面: 3 時間の走行が終わると、答えの下に背景の仕事の知らせ・文脈の整理・確かめの結果などが十数行並んだ。
///   0.1.595 では「走行中だけの kind」（monitor・auto_bg など 8 種）を数えたが、見直し・促し・資源の警告・割り込みなど
///   残りの経過がまだ並び「治ってなかった」。経過は種類が増え続けるので、**残す側を数える**（増えた kind は既定で走行中だけ）。
///   残すのは、走行の後に利用者が知っておく事: 止められた・断られた・打ち切った・後始末に失敗した・利用者の入力に関わる事。
///   kind は GATE_KINDS / PLAIN_KINDS にも載ったまま（色と「知っている」の判定はそちら）。
pub const AFTER_KINDS: &[&str] = &[
    // 作業場の外への変更を止めた／利用者の答えで実行しなかった・終えた／オフラインの門
    "ws_guard",
    "answer_guard",
    "user_stop",
    "offline",
    // 文脈が尽きて打ち切った（「急に終わった」の理由）・窓が狭いの事前警告（kind なしの notice）
    "context_exhausted",
    "notice",
    // 中断と後始末の失敗・控え（巻き戻しの元）を取れなかった
    "cancel_error",
    "cancel_detail",
    "checkpoint_error",
    "hook_error",
    "phase_failed",
    // 利用者の入力に関わる事: 画像を受け付けなかった・問いが答えられずに閉じた・別の画面からの割り込み
    "image_rejected",
    "ask_expired",
    "injected",
    // 推論機が応答を断った（走行の後も知っておく事）
    "declined",
    // 子（Explore・Task）の道具の中継。画面が状態行に出し、答えの下には 1 行にまとめる（app.js の kids）
    "subagent",
    // この依頼で変えたファイル（`/undo` で戻せる・2026-11-11）
    "changes",
];

/// 画面に出す 1 件。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NoticeView {
    pub level: Level,
    pub text: String,
}

fn s<'a>(p: &'a Value, k: &str) -> Option<&'a str> {
    p.get(k).and_then(|x| x.as_str()).filter(|x| !x.is_empty())
}

fn n(p: &Value, k: &str) -> Option<u64> {
    p.get(k).and_then(|x| x.as_u64())
}

/// `notice` を画面の 1 件へ。`None` は「知っていて、出すことが無い」（＝黙ってよい）。
///
/// ★**未知の kind を黙って捨てない**のがこの関数の主目的。旧実装（sidecar.rs:535）は
///   `message`/`text` が無い notice を `vec![]` で落としていたため、
///   `cancel` / `cancel_applied` / `cancelled` / `run_start` / `shutdown` が
///   **画面にも診断にも 1 文字も出ていなかった**（＝「止めたのに何も出ない」の一因）。
pub fn notice_view(kind: &str, message: Option<&str>, p: &Value) -> Option<NoticeView> {
    // 文言が無い kind は、本体（serve.py / term）と同じ言葉をここで組み立てる。
    let composed: Option<String> = match kind {
        "ws_guard" => {
            let tool = s(p, "tool").unwrap_or("?");
            let target = s(p, "target").unwrap_or("?");
            let times = n(p, "n").unwrap_or(1);
            Some(if times > 1 {
                format!("作業場の外への {tool} を止めました（{target}・{times} 回目）")
            } else {
                format!("作業場の外への {tool} を止めました（{target}）")
            })
        }
        "answer_guard" => {
            let tool = s(p, "tool").unwrap_or("?");
            let answer = s(p, "answer").unwrap_or("?");
            let times = n(p, "n").unwrap_or(1);
            Some(format!(
                "利用者が直前の質問に「{answer}」と答えたので、{tool} を実行しませんでした（{times} 回目）"
            ))
        }
        "user_stop" => {
            let answer = s(p, "answer").unwrap_or("?");
            Some(format!(
                "利用者の回答「{answer}」により、ここで終了します（以後は何も変えていません）"
            ))
        }
        "shutdown" => Some("本体を終了します。".to_string()),
        _ => None,
    };

    let text = message
        .filter(|m| !m.is_empty())
        .map(str::to_string)
        .or(composed);
    // ★別の画面（Android など）から届いた割り込みは、どこから来たかを頭に付ける（2026-10-10・画面のリンク）。
    //   この画面から送った割り込みは source が "app" か空（画面側が自分で吹き出しを出す）。
    let text = match (kind, s(p, "source")) {
        ("injected", Some(src)) if !src.is_empty() && src != "app" => {
            let who = if src == "android" { "Android" } else { src };
            text.map(|t| format!("{who} から割り込み: {t}"))
        }
        _ => text,
    };

    if QUIET_KINDS.contains(&kind) {
        return None;
    }
    if CANCEL_KINDS.contains(&kind) {
        return None; // CancelTrack の担当
    }

    let known = GATE_KINDS.contains(&kind) || PLAIN_KINDS.contains(&kind);
    match text {
        Some(t) => {
            let level = if GATE_KINDS.contains(&kind)
                || (kind == "harness" && is_gate_mark(&t))
                || (kind == "subagent" && t.contains('✗'))
            {
                Level::Gate
            } else if known {
                Level::Plain
            } else {
                // 文はあるが kind は知らない。文は出す——ただし「知らない」と名乗る。
                Level::Unknown
            };
            let text = if level == Level::Unknown {
                format!("不明な事象（notice kind={kind}）: {t}")
            } else {
                t
            };
            Some(NoticeView { level, text })
        }
        // 文も無い・知らない kind。**数えて出す**（旧実装はここで消えていた）。
        None if !known => Some(NoticeView {
            level: Level::Unknown,
            text: format!("不明な事象（notice kind={kind}・文言なし）"),
        }),
        // 知っている kind だが今回は文が無い（本体の版が古い等）。黙る。
        None => None,
    }
}

// ---------------------------------------------------------------------------
// 中断（cancel）の 1 本化
// ---------------------------------------------------------------------------

/// 中断を送ってから `done` も来ないまま何秒で「まだ止まっていない」と言うか。
/// ★端末版 `term/app.py` の `CANCEL_STALL_SECS` と同じ 10 秒に揃える
///   （実機で中断が 73 秒止まらなかった走行がある）。
pub const CANCEL_STALL_SECS: u64 = 10;

/// `cancel` の `applied`（serve.py `_accept_cancel`）を利用者の言葉に。
pub fn cancel_applied_ja(applied: &str) -> &str {
    match applied {
        "running" => "走行中に届きました",
        "pending" => "待ち行列に残っていたので取り消しました",
        "failed" => "本体に伝えられませんでした（もう一度「止める」で強制終了してください）",
        other => other,
    }
}

/// 中断の連鎖を **1 本**にまとめる。
///
/// ★なぜ要るか: 1 回の「止める」で中断まわりの notice は最大 3 回届く
///   （①アプリ自身の即時表示 ②serve の受理 `cancel` ③agent が反復境界で確定 `cancelled`）。
///   そのまま描くと同じ文が 2〜3 回並ぶ（端末版で実機報告あり・2026-09-03）。
///   **最初の 1 回だけ本文を出し、以降は新しく分かったことだけ**を足す。
#[derive(Default, Debug)]
pub struct CancelTrack {
    announced: bool,
    applied_seen: Vec<String>,
    detail_seen: bool,
    stall_warned: bool,
}

impl CancelTrack {
    /// アプリが自分で「中断を要求しました」を出したときに呼ぶ（重複を止める）。
    pub fn announce_locally(&mut self) -> Option<String> {
        if self.announced {
            return None;
        }
        self.announced = true;
        Some("中断を要求しました。".to_string())
    }

    /// `notice kind=cancel|cancel_applied|cancelled` を受けて、**新しく言うことだけ**返す。
    pub fn on_notice(&mut self, kind: &str, p: &Value) -> Vec<String> {
        let mut out = Vec::new();
        if !self.announced {
            self.announced = true;
            out.push("中断を要求しました。".to_string());
        }
        if let Some(a) = s(p, "applied") {
            if !self.applied_seen.iter().any(|x| x == a) {
                self.applied_seen.push(a.to_string());
                out.push(format!("→ {}", cancel_applied_ja(a)));
            }
        }
        if kind == "cancel_applied" && !self.detail_seen {
            self.detail_seen = true;
            let mut bits: Vec<String> = Vec::new();
            if let Some(v) = n(p, "sockets") {
                bits.push(format!("HTTP {v} 本"));
            }
            if let Some(v) = n(p, "inflight") {
                bits.push(format!("送信中 {v}"));
            }
            if let Some(v) = n(p, "bash_killed") {
                if v > 0 {
                    bits.push(format!("走っていたコマンド {v} 件"));
                }
            }
            if let Some(v) = n(p, "kill_failed") {
                if v > 0 {
                    bits.push(format!("止められなかったコマンド {v} 件"));
                }
            }
            if let Some(ms) = p.get("request_cancel_ms").and_then(|x| x.as_f64()) {
                bits.push(format!("要求 {ms:.0}ms"));
            }
            if !bits.is_empty() {
                out.push(format!("→ 切ったもの: {}", bits.join("・")));
            }
        }
        out
    }

    /// 中断から `secs` 秒たっても `done` が来ていない、を**1 回だけ**言う。
    pub fn stall(&mut self, secs: u64) -> Option<String> {
        if self.stall_warned || secs < CANCEL_STALL_SECS {
            return None;
        }
        self.stall_warned = true;
        Some(format!(
            "まだ止まっていない（{secs}s）・本体はまだ動いているかもしれません"
        ))
    }
}

// ---------------------------------------------------------------------------
// 試験（★ここは blackwall-core 側なので**実際に走る**）
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn restart_marks_are_a_subset_of_gate_marks() {
        for m in RESTART_MARKS {
            assert!(GATE_MARKS.contains(m), "目立たせない下書き破棄の印: {m}");
        }
    }

    #[test]
    fn the_four_2026_09_03_gates_discard_the_draft() {
        for m in ["[verifier 1] x", "[red_tests 1] x", "[rerun 1] x", "[invent 1] x",
                  "[contradiction 1] x", "[fabrication 1] x", "[pushback 1] x", "[reset 1] x"] {
            assert!(discards_draft(m), "下書きを捨てない: {m}");
        }
    }

    #[test]
    fn host_scope_and_search_limit_are_prominent_but_do_not_discard() {
        // ★host_scope / search_limit は「見落とすと痛い」が、下書きを捨てる対象ではない。
        for m in ["[host_scope 1] x", "[search_limit 1] x"] {
            assert!(is_gate_mark(m), "目立たない: {m}");
            assert!(!discards_draft(m), "捨ててはいけない下書きを捨てている: {m}");
        }
    }

    #[test]
    fn plain_text_is_neither() {
        assert!(!discards_draft("作業は完了しました。"));
        assert!(!is_gate_mark("作業は完了しました。"));
    }

    #[test]
    fn accepted_outcomes_say_nothing_or_a_short_word() {
        assert_eq!(outcome_line("accepted"), None);
        // ★利用者の意思どおりの終了は ✓ 側（失敗の見た目にしない）。
        assert_eq!(outcome_line("user_stop").as_deref(), Some("✓ 終わり方: 利用者が終了"));
        // ✗ は画面の CSS が付けるので、文には入れない（二重にしない）。
        assert_eq!(outcome_line("cancelled").as_deref(), Some("終わり方: 中断"));
        assert!(outcome_is_accepted("user_stop"), "利用者の終了が失敗扱い");
        assert!(!outcome_is_accepted("cancelled"));
    }

    #[test]
    fn unknown_outcome_is_shown_not_swallowed() {
        let l = outcome_line("brand_new_outcome").unwrap();
        assert!(l.contains("brand_new_outcome"), "{l}");
        assert!(l.contains("不明"), "{l}");
    }

    #[test]
    fn legacy_success_values_stay_quiet() {
        for o in LEGACY_QUIET_OUTCOMES {
            assert_eq!(outcome_line(o), None, "古い本体の成功値で騒いでいる: {o}");
        }
    }

    #[test]
    fn missing_outcome_is_reported_as_unclosed() {
        assert_eq!(outcome_line("").as_deref(), Some("終わり方: 締めが来ていない"));
    }

    #[test]
    fn every_outcome_word_is_japanese_or_empty() {
        // ★内部識別子が画面へ漏れない（この現場は「ws_guard がそのまま画面に出る」を踏んでいる）。
        for (k, v) in OUTCOME_JA {
            assert!(
                v.is_empty() || !v.is_ascii(),
                "outcome {k} の語が識別子のまま: {v}"
            );
        }
    }

    #[test]
    fn ws_guard_without_message_still_says_something() {
        let p = json!({"tool": "Bash", "target": "docker rm videodir", "n": 1});
        let v = notice_view("ws_guard", None, &p).expect("消えている");
        assert_eq!(v.level, Level::Gate);
        assert!(v.text.contains("docker rm videodir"), "{}", v.text);
        assert!(!v.text.contains("ws_guard"), "識別子が画面に出ている: {}", v.text);
    }

    #[test]
    fn answer_guard_and_user_stop_have_words() {
        let a = notice_view("answer_guard", None, &json!({"tool":"Write","answer":"いいえ","n":1}))
            .expect("消えている");
        assert_eq!(a.level, Level::Gate);
        assert!(a.text.contains("いいえ"), "{}", a.text);
        let u = notice_view("user_stop", None, &json!({"answer":"終了"})).expect("消えている");
        assert_eq!(u.level, Level::Plain);
        assert!(u.text.contains("終了"), "{}", u.text);
    }

    #[test]
    fn harness_with_a_gate_mark_is_prominent() {
        let m = "[verifier 1] 完了の主張と作業場の実体が食い違う";
        let v = notice_view("harness", Some(m), &json!({})).unwrap();
        assert_eq!(v.level, Level::Gate);
        let plain = notice_view("harness", Some("[Ω-O] 覚え書き"), &json!({})).unwrap();
        assert_eq!(plain.level, Level::Gate, "Ω も目立たせる側（term と同じ）");
        let quiet = notice_view("harness", Some("ふつうの注記"), &json!({})).unwrap();
        assert_eq!(quiet.level, Level::Plain);
    }

    #[test]
    fn subagent_relay_is_quiet_unless_the_child_tool_failed() {
        // ★2026-09-05（r4/app-term-clients）: `tools/task.py` は委譲した子の道具
        //   活動（開始・成功・失敗）を全部 `kind="subagent"` の notice 1本にまとめて
        //   親へ中継する——kind だけでは成功と失敗を区別できない
        //   （実測 run50-52: 120 件中 5 件が子の道具の実際の失敗）。
        let start = notice_view("subagent", Some("子: ▶ Bash"), &json!({})).unwrap();
        assert_eq!(start.level, Level::Plain);
        let ok = notice_view("subagent", Some("子: ✓ Bash"), &json!({})).unwrap();
        assert_eq!(ok.level, Level::Plain);
        let failed = notice_view("subagent", Some("子: ✗ Bash 失敗: boom"), &json!({})).unwrap();
        assert_eq!(failed.level, Level::Gate, "子の道具の失敗が目立たない: {failed:?}");
    }

    #[test]
    fn phase_hold_restored_is_a_prominent_gate() {
        // ★r4b/app-term-clients（先取り配線）: core-hold-fix が phases.py:1677 に
        //   足す予定の notice（手番の終わりの検収がベストより減っていたので直前の
        //   ベストへ戻した）。まだ本体はこの kind を出さない（未マージ）が、
        //   phase_retry と同格の「ハーネスの介入」として黙って流さない。
        let v = notice_view(
            "phase_hold_restored",
            Some("工程 2/3 ベストへ戻す: gallery（検収 1→ベスト 2/3・復元 3 件・対象外 0 件）"),
            &json!({}),
        )
        .unwrap();
        assert_eq!(v.level, Level::Gate, "{v:?}");
    }

    #[test]
    fn unknown_kind_without_message_is_counted_not_dropped() {
        let v = notice_view("brand_new_kind", None, &json!({})).expect("黙って捨てている");
        assert_eq!(v.level, Level::Unknown);
        assert!(v.text.contains("brand_new_kind"), "{}", v.text);
    }

    #[test]
    fn unknown_kind_with_message_keeps_the_message_and_says_it_is_unknown() {
        let v = notice_view("brand_new_kind", Some("なにか起きた"), &json!({})).unwrap();
        assert_eq!(v.level, Level::Unknown);
        assert!(v.text.contains("なにか起きた"), "{}", v.text);
        assert!(v.text.contains("brand_new_kind"), "{}", v.text);
    }

    #[test]
    fn known_quiet_kinds_stay_quiet() {
        for k in QUIET_KINDS {
            assert_eq!(notice_view(k, None, &json!({})), None, "{k} が騒いでいる");
        }
    }

    #[test]
    fn cancel_chain_collapses_to_one_line_plus_new_facts() {
        let mut t = CancelTrack::default();
        // ①アプリ自身の即時表示
        assert_eq!(t.announce_locally().as_deref(), Some("中断を要求しました。"));
        // ②serve の受理（applied=running）
        let a = t.on_notice("cancel", &json!({"accepted": true, "applied": "running"}));
        assert_eq!(a, vec!["→ 走行中に届きました".to_string()], "本文が重複している: {a:?}");
        // ③同じ applied がもう一度来ても黙る
        assert!(t.on_notice("cancel", &json!({"applied": "running"})).is_empty());
        // ④実際に切ったもの
        let d = t.on_notice(
            "cancel_applied",
            &json!({"applied":"running","sockets":2,"inflight":1,"bash_killed":1,"request_cancel_ms":12.3}),
        );
        assert_eq!(d.len(), 1, "{d:?}");
        assert!(d[0].contains("HTTP 2 本") && d[0].contains("12ms"), "{}", d[0]);
        // ⑤agent の確定（applied なし）は何も足さない
        assert!(t.on_notice("cancelled", &json!({"iter": 3})).is_empty());
    }

    #[test]
    fn cancel_notice_alone_still_announces() {
        // アプリが先に言っていない経路（本体が勝手に止めた等）でも 1 行は出る。
        let mut t = CancelTrack::default();
        let a = t.on_notice("cancel", &json!({"applied": "pending"}));
        assert_eq!(a[0], "中断を要求しました。");
        assert!(a[1].contains("待ち行列"), "{}", a[1]);
    }

    #[test]
    fn after_kinds_are_known_kinds() {
        // 答えの下に残す kind も、色と「知っている」の判定は GATE_KINDS / PLAIN_KINDS が持つ（載っていないと未知の事象になる）。
        for k in AFTER_KINDS {
            assert!(GATE_KINDS.contains(k) || PLAIN_KINDS.contains(k), "{k} が GATE_KINDS にも PLAIN_KINDS にも無い");
        }
    }

    #[test]
    fn stall_speaks_once_after_ten_seconds() {
        let mut t = CancelTrack::default();
        assert_eq!(t.stall(3), None, "早すぎる警告");
        let m = t.stall(CANCEL_STALL_SECS).expect("10 秒で黙っている");
        assert!(m.contains("まだ止まっていない"), "{m}");
        assert_eq!(t.stall(30), None, "同じ警告を 2 回言っている");
    }

    #[test]
    fn cancel_kinds_are_not_handled_by_notice_view() {
        for k in CANCEL_KINDS {
            assert_eq!(notice_view(k, None, &json!({})), None, "{k} が二重に出る");
        }
    }

    #[test]
    fn kind_tables_do_not_overlap() {
        // 同じ kind が 2 つの表にあると、直した側と直していない側が生まれる。
        for k in GATE_KINDS {
            assert!(!PLAIN_KINDS.contains(k), "{k} が両方の表にある");
            assert!(!QUIET_KINDS.contains(k), "{k} が両方の表にある");
        }
        for k in PLAIN_KINDS {
            assert!(!QUIET_KINDS.contains(k), "{k} が両方の表にある");
        }
    }
}
