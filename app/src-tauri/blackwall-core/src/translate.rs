//! 線（serve.py の NDJSON）1 行を、画面への操作（[`Signal`]）へ翻訳する**純粋関数**。
//!
//! ★なぜ本体（`src/sidecar.rs`）から出したか（2026-09-04・W1）:
//!   `sidecar.rs` は `tauri` を参照するのでホスト（Linux）では GTK（pango）が無くて
//!   **リンクできない**＝`cargo test` が走らない。preflight も
//!   `cargo check --tests`＝**コンパイルするだけ**なので、この翻訳器の試験は
//!   **一度も実行されたことがなかった**（同じ理由で 2026-09-02 に `llama_log` を移している）。
//!   ここは Windows 版の心臓部——本体が出す 1 行を取り違えると、道具が見えない・
//!   本文が消える・終わりに気づかない、という**静かな**壊れ方をする。3 つとも実機で
//!   起きている（`sidecar.rs` 冒頭の注記）。
//!
//! ★ファイルは 1 本。`src/sidecar.rs` が `#[path]` で参照するだけなので二重管理は起きない。
//!   `tauri::` を 1 つでも書いた瞬間に blackwall-core のビルドが落ちる＝純粋さが守られる。

use serde_json::{json, Value};

use super::wire;

// ---------------------------------------------------------------------------
// 翻訳（純粋関数。プロセスも AppHandle も要らない＝どの OS でも試験できる）
// ---------------------------------------------------------------------------

/// 1行を読んで決まる「画面に対して何をするか」。
#[derive(Debug, PartialEq)]
pub enum Signal {
    /// 本文の断片（`<think>` の分離を通してから流す）。
    Text(String),
    /// 思考だと確定している断片（分離を通さない）。
    Think(String),
    /// 1回の生成の区切り。思考の分離をここで作り直す。
    /// 反復が1周した（＝ふつうは道具を呼んだ）。**本文は捨てない。**
    /// ★`ctx`/`n_ctx`/`compact_at` は本体（engine/loop.py）が手番ごとに送る文脈の使用量（2026-11-10・入力欄の残量の輪）。
    ///   無い版の本体からは 0 が来る＝画面は輪を出さない。
    IterStart { ctx: u64, n_ctx: u64, compact_at: u64 },
    /// 差し戻しで作り直す。**ここでだけ前の試作を捨てる。**
    NewGeneration,
    /// ツールが始まった。引数を控えておく。
    /// ★`call` = serve.py が配る相関 id（`_CallIds`）。同名ツールを並行で複数
    ///   走らせると（読み取り専用の並行実行）、名前だけで対にすると引数が入れ替わる／
    ///   消える。id で対応を取る（無いイベントは名前にフォールバック）。
    ToolStart { name: String, call: Option<String>, args: Value },
    /// 利用者に聞く。画面へ回す。
    /// ★`descriptions` は 2026-09-03 に本体が足した「選択肢の説明」（serve.py:1020）。
    ///   `options` と同じ長さ。読まないと**選択肢の意味が画面に出ない**まま
    ///   利用者が当てずっぽうで押すことになる。
    Ask { id: String, question: String, options: Vec<String>, descriptions: Vec<String> },
    /// 問いが閉じた（答えた・時間切れ・止めた・強制割り込み）。画面の問いの箱を消す。
    /// ★2026-10-16 監査: 本体（serve.py `_ask`）が `notice kind=ask_closed` を出すまで、時間切れや
    ///   「止める」の後も箱が残り、押しても届かない答えを受け付けていた。
    AskClosed { id: String },
    /// 本体がアプリに頼む（Browser 道具など・2026-10-18）。アプリが実行して `app_reply` を stdin に書き戻す。
    /// ★名乗った物（run の `app_caps`）しか本体は頼まない。知らない `op` には ok=false で答える（黙ると本体が待ち続ける）。
    AppCall { call: String, op: String, args: Value },
    /// ツールが終わった。`agent-tool` を1件出す。
    /// ツールが終わった。`agent-tool` を1件出す。
    /// ★`kind`/`chars`/`truncated` を運ぶ（2026-08-18）。本体（serve.py）は送っているのに
    ///   ここで捨てていたため、画面は **0件(empty) と失敗(error) と拒否(denied) を区別できず**、
    ///   さらに切り詰めた後の長さを実サイズとして表示していた（2万字を「201 chars」）。
    ToolEnd {
        name: String,
        call: Option<String>,
        result: String,
        ok: bool,
        kind: Option<String>,
        chars: Option<u64>,
        truncated: Option<bool>,
        /// ★本体が道具ごとに作った1行要約（問題4・2026-08-23）。
        ///   画面はこれを描き、結果の全文は開いたときだけ出す。
        ///   無ければ Null（旧版の本体）＝画面は従来の描き方へ。
        summary: Value,
    },
    /// 経過の1行（本文に `— …` で混ぜる）。既定の薄い注記。
    Line(String),
    /// **目立たせる**1行（門・差し戻し・失敗の報せ）。左線と赤で立てる。
    /// ★この現場は「失敗の左線が存在しない」を実機で踏んでいる（2026-09-03 の UI 監査）。
    ///   薄い灰色に混ぜると、いちばん読ませたい行がいちばん読めない。
    Gate(String),
    /// **知らない**事象（未知の type / notice kind / outcome）。捨てずに出して数える。
    /// ★旧実装は `message` を持たない notice を `vec![]` で落としていたため、
    ///   `cancel` / `cancel_applied` / `cancelled` / `run_start` / `shutdown` が
    ///   画面にも診断にも1文字も出ていなかった。
    Unknown(String),
    /// 走行中だけ出す 1 行（wire::AFTER_KINDS に無く `final` も無い notice・経過の知らせ）。画面は状態行に出し、答えの下の注記には残さない。
    /// `gate` は目立たせ方（Gate と同じ赤）。
    Live { text: String, gate: bool },
    /// 沈黙中の心拍（serve.py `_heartbeat_loop`）。本文にも断り書きにも混ぜず、
    /// 状態行の「何を何秒待っているか」だけを更新する。
    /// ★`detail` は推論機の読み込みの進み具合（「読み込み 34%（…・残り約 2 分）」・2026-10-07）。無ければ空。
    Heartbeat { stage: String, quiet_secs: u64, detail: String },
    /// 握手。★payload は今のところ読んでいない（読んでいないものを「照合に使う」と書かない）。
    Ready,
    /// 正常終了。
    ///
    /// ★`meta` は `done` の中身から本文だけ抜いたもの。**`usage` とは別に持つ。**
    ///   以前は `usage` だけを運び、記録は `usage.get("model")` を見ていた。
    ///   だが `model` は `usage` の**兄弟**であって子ではないので、構造上いつまでも
    ///   見つからず、**実機40件すべてで「どのモデルか不明」になっていた**
    ///   （2026-08-16 実測）。本体は正しく `done` に載せていた＝参照先だけが違った。
    Done { usage: Option<Value>, meta: Value },
    /// 失敗（理由つき）。
    Failed(String),
    /// 読み飛ばした型。診断のために型名を残す。
    Ignore(String),
}
/// 翻訳の途中で持ち越す状態。
#[derive(Default)]
pub struct Fold {
    /// いまの生成で流した本文（`assistant` の重複判定に使う）。
    pub streamed: String,
    /// この実行で本文を1文字でも出したか（`result.text` を使うかの判断）。
    pub any_text: bool,
    /// 中断（cancel / cancel_applied / cancelled）を**1本にまとめる**ための状態。
    /// ★1 回の「止める」で中断の notice は最大 3 回届く。そのまま描くと同じ文が並ぶ。
    pub cancel: wire::CancelTrack,
}

impl Fold {
    pub fn new_generation(&mut self) {
        self.streamed.clear();
    }
    pub fn saw_text(&mut self, s: &str) {
        self.streamed.push_str(s);
        if !s.trim().is_empty() {
            self.any_text = true;
        }
    }
}
/// 封筒の中身を取り出す。`{"type":…,"payload":{…}}` と、payload を持たない平らな形の両方を受ける。
/// ★片方に決め打ちすると、送り手の小さな変更で**全部無視**という壊れ方をする
///   （llama.rs の `extract_model_array` が同じ理由で3つの形を受けている）。
pub fn payload_of(v: &Value) -> &Value {
    match v.get("payload") {
        Some(p) if p.is_object() => p,
        _ => v,
    }
}
/// 最初に見つかった文字列の鍵を返す。
pub fn text_field<'a>(p: &'a Value, keys: &[&str]) -> Option<&'a str> {
    for k in keys {
        if let Some(s) = p.get(*k).and_then(|x| x.as_str()) {
            if !s.is_empty() {
                return Some(s);
            }
        }
    }
    None
}
/// 1行（JSON）を、画面への操作へ翻訳する。
pub fn interpret(v: &Value, fold: &mut Fold) -> Vec<Signal> {
    let ty = v.get("type").and_then(|x| x.as_str()).unwrap_or("");
    let p = payload_of(v);
    match ty {
        "" => vec![Signal::Ignore("(type なし)".to_string())],

        // ★2026-08-11 実測で判明した食い違い。この Rust 側は `{"type":"ready"}` を待ち、
        //   serve.py は `{"type":"notice","kind":"ready", ..., "tools":[...]}` を送る。
        //   両側を別々に書いたため、握手が**永遠に成立しない**状態だった。
        //   受ける側が両方を認めるのが安全側（送る側を変えると古い版と繋がらなくなる）。
        //   ついでに、この行が道具一覧を運んでくるので拾っておく。
        "ready" => vec![Signal::Ready],
        //   ★道具一覧を覚えるのは呼び出し側（`Signal::Ready` を受ける 2 か所）へ移した
        //     （2026-09-04・W1）。この関数はグローバルに触らない＝**純粋さがコンパイラに守られる**。
        "notice" if v.get("kind").and_then(|k| k.as_str()) == Some("ready") => vec![Signal::Ready],

        // 診断用。cwd の食い違いは run_loop が別に見る（ここでは何もしない＝
        // 「知らない型」として数えると、正常な行が診断ログに並んで紛らわしい）。
        "init" => vec![],

        // ★`iter_start` は「ループが1周した」であって「答えを作り直す」ではない。
        //   本体は**道具を呼ぶたび**にこれを出す（agent.py:1067）。
        //   ここで本文を捨てていたので、**道具を呼ぶたびに、それまで書いた報告が
        //   画面からも記録からも消えていた**（2026-08-16 実機。7手順の作業で
        //   最後の1行しか残らず、正しい報告は思考欄に埋もれた）。
        //   ★名前を合わせたときに、意味を合わせていなかった。
        "iter_start" => {
            fold.new_generation();
            let n = |k: &str| p.get(k).and_then(|x| x.as_u64()).unwrap_or(0);
            vec![Signal::IterStart { ctx: n("ctx"), n_ctx: n("n_ctx"), compact_at: n("compact_at") }]
        }

        "token" => match text_field(p, &["text"]) {
            Some(t) => {
                fold.saw_text(t);
                vec![Signal::Text(t.to_string())]
            }
            None => vec![],
        },

        "think" | "reasoning" => match text_field(p, &["text", "content"]) {
            Some(t) => vec![Signal::Think(t.to_string())],
            None => vec![],
        },

        // ★`assistant` は「その回の返答の全文」と「枠組みからの通知」の両方に使われている
        //   （Python 側 agent.py が `[Ω-O] …` のような通知を同じ型で流す）。
        //   全文をそのまま出すと、直前まで流した本文と二重になる。
        "assistant" => match text_field(p, &["text"]) {
            None => vec![],
            Some(t) => {
                if fold.streamed.trim().is_empty() {
                    // 一度も流れていない＝非ストリームの返答。これが本文。
                    fold.saw_text(t);
                    vec![Signal::Text(t.to_string())]
                } else if fold.streamed.contains(t.trim()) {
                    // 既に流したものと同じ。捨てる。
                    vec![]
                } else {
                    // 流した本文に含まれない＝通知。本文には混ぜず、注記として出す。
                    vec![Signal::Line(t.to_string())]
                }
            }
        },

        // ★2026-08-11: 送る側（serve.py）は `tool_call` / `tool_result`、
        //   受ける側（ここ）は `tool_start` / `tool_end` を待っていた。両側を別々に
        //   書いたための食い違いで、**実行したコマンドが画面に一切出ない**状態だった。
        //   道具の実行が見えないのは、このアプリで最も危険な壊れ方なので両方を認める。
        "tool_start" | "tool_call" => {
            let name = text_field(p, &["name", "tool"]).unwrap_or("(名前なし)").to_string();
            let call = text_field(p, &["call"]).map(str::to_string);
            let args = p
                .get("input")
                .or_else(|| p.get("args"))
                .cloned()
                .unwrap_or_else(|| json!({}));
            vec![Signal::ToolStart { name, call, args }]
        }

        "tool_end" | "tool_result" => {
            let name = text_field(p, &["name", "tool"]).unwrap_or("(名前なし)").to_string();
            let call = text_field(p, &["call"]).map(str::to_string);
            let result = match p.get("result") {
                Some(Value::String(s)) => s.clone(),
                Some(other) if !other.is_null() => other.to_string(),
                _ => String::new(),
            };
            // ★推測しない。明示された `ok` だけを見る。`error` があれば失敗。
            //   どちらも無ければ成功として扱う（理由は冒頭の表を参照）。
            let ok = match p.get("ok").and_then(|x| x.as_bool()) {
                Some(b) => b,
                None => p.get("error").map(|e| e.is_null()).unwrap_or(true),
            };
            let kind = text_field(p, &["kind"]).map(str::to_string);
            let chars = p.get("chars").and_then(|x| x.as_u64());
            let truncated = p.get("truncated").and_then(|x| x.as_bool());
            let summary = p.get("summary").cloned().unwrap_or(Value::Null);
            vec![Signal::ToolEnd { name, call, result, ok, kind, chars, truncated, summary }]
        }

        // ★本体からアプリへの頼み（serve.py の _app_call・2026-10-18）。
        "app_call" => {
            let call = text_field(p, &["call"]).unwrap_or("").to_string();
            let op = text_field(p, &["op"]).unwrap_or("").to_string();
            let args = p.get("args").cloned().unwrap_or(Value::Null);
            if call.is_empty() {
                return vec![];
            }
            vec![Signal::AppCall { call, op, args }]
        }

        // ★利用者に聞く。画面へ回して、返事を stdin に書き戻す（core_answer）。
        "ask" => {
            let id = text_field(p, &["ask", "id"]).unwrap_or("a1").to_string();
            let q = text_field(p, &["question", "text"]).unwrap_or("").to_string();
            let strs = |key: &str| -> Vec<String> {
                p.get(key)
                    .and_then(|o| o.as_array())
                    .map(|a| a.iter().map(|x| x.as_str().unwrap_or("").to_string()).collect())
                    .unwrap_or_default()
            };
            let opts = strs("options");
            // ★説明は options と同じ長さで来る約束（serve.py `_ask_payload`）だが、
            //   古い本体は送ってこない。**足りない分は空**で埋め、画面が添字で引けるようにする
            //   （長さがずれたまま zip すると、説明が1つずれて別の選択肢に付く）。
            let mut descs = strs("descriptions");
            descs.resize(opts.len(), String::new());
            vec![Signal::Ask { id, question: q, options: opts, descriptions: descs }]
        }

        // ★2026-09-02: 心拍。実利用者の記録で「600 秒のあいだ Python から 1 バイトも
        //   届きませんでした」×3 の正体は**推論機が遅いだけ**だった（再現: iter_start
        //   の後 400 秒無出力）。行が来れば無出力の見張りは進まないし、段階が画面に出る。
        "notice" if v.get("kind").and_then(|k| k.as_str()) == Some("heartbeat") => {
            let stage = v.get("stage").and_then(|x| x.as_str()).unwrap_or("推論機").to_string();
            let quiet_secs = v.get("quiet_secs").and_then(|x| x.as_u64()).unwrap_or(0);
            let detail = v.get("detail").and_then(|x| x.as_str()).unwrap_or("").to_string();
            vec![Signal::Heartbeat { stage, quiet_secs, detail }]
        }

        // ★2026-09-04（W1）全面改め。旧実装の壊れ方は 3 つあった:
        //   ① 印の一覧を**この場に直書き**していたので、同日増えた `[invent ` `[contradiction `
        //      が差し戻しとして扱われず、**差し戻し前の下書きが画面に残って同じ塊が 2 回**出た
        //      （端末版は同じ穴を `_RESTART_MARKS` に足して塞いだ・sbx-B2）。
        //   ② `message` も `text` も無い notice を `None => vec![]` で**黙って捨てて**いた。
        //      本体が文言を持たせずに出す `cancel` / `cancel_applied` / `cancelled` /
        //      `run_start` / `shutdown` は、画面にも診断にも 1 文字も出ていなかった。
        //   ③ 門の通知（差し戻し・作業場ガード・工程不合格）が、ふつうの注記と**同じ薄い灰色**
        //      で流れていた。いちばん読ませたい行がいちばん読めない。
        //   表と判定は `wire`（実際に試験が走る側）に置き、ここは配線だけにする。
        "notice" => {
            let kind = v
                .get("kind")
                .or_else(|| p.get("kind"))
                .and_then(|k| k.as_str())
                .filter(|k| !k.is_empty())
                // ★kind を持たない素の notice は「窓が狭い」の事前警告（agent.py:2780）。
                //   端末版も `_CONTEXT_KINDS` に `"notice"` を入れて同じ扱いにしている。
                //   ここで "" のままにすると**未知扱い**になり、正常な行が診断に並ぶ。
                .unwrap_or("notice");
            // 問いが閉じた（serve.py `_ask`）。文は出さず、画面の箱を消す合図だけ渡す。
            if kind == "ask_closed" {
                let id = text_field(p, &["ask"]).unwrap_or("").to_string();
                return vec![Signal::AskClosed { id }];
            }
            // 中断の連鎖は 1 本にまとめる（同じ文を 2〜3 回並べない）。
            if wire::CANCEL_KINDS.contains(&kind) {
                return fold
                    .cancel
                    .on_notice(kind, p)
                    .into_iter()
                    // ★中断の経過は状態行にだけ出す（2026-11-19 オーナー: 止めると 4 行並んだ）。答えの下には
                    //   `done` の「中断しました。」1 行だけが残る。本体に伝えられなかったときだけは残して立てる。
                    .map(|l| if l.contains("伝えられませんでした") { Signal::Gate(l) } else { Signal::Live { gate: false, text: l } })
                    .collect();
            }
            let msg = text_field(p, &["message", "text"]);
            let mut out = Vec::new();
            // **本文を捨ててよいのはここだけ**（term の `_RESTART_MARKS` と同じ表）。
            if msg.map(wire::discards_draft).unwrap_or(false) || wire::RESTART_KINDS.contains(&kind) {
                out.push(Signal::NewGeneration);
            }
            // 答えの下に残すのは走行の後に知っておく事だけ（wire::AFTER_KINDS か、本体が `final` を付けた走行の終わりの状態）。
            let keep = wire::AFTER_KINDS.contains(&kind)
                || v.get("final").or_else(|| p.get("final")).and_then(Value::as_bool) == Some(true);
            if let Some(view) = wire::notice_view(kind, msg, p) {
                out.push(match view.level {
                    wire::Level::Plain | wire::Level::Gate if !keep => {
                        Signal::Live { gate: view.level == wire::Level::Gate, text: view.text }
                    }
                    wire::Level::Plain => Signal::Line(view.text),
                    wire::Level::Gate => Signal::Gate(view.text),
                    wire::Level::Unknown => Signal::Unknown(view.text),
                });
            }
            out
        }

        "cancelled" => vec![Signal::Line("打ち切られました。".to_string())],

        "error" => {
            let m = text_field(p, &["message", "error", "text"])
                .unwrap_or("理由が示されていません")
                .to_string();
            // ★終端にするのは `done` が来ない失敗だけ（2026-10-16 監査）。旧: どの error も Failed にしていたので、
            //   id の無い `bad_request`（読めない 1 行）や割り込みの `inject_failed` で走行を終えたうえ本体を殺していた
            //   （sidecar.rs の End::Failed → kill_child）。serve.py は走行中の失敗（crash・backend など）の後に必ず
            //   `done` を出すので、それ以外は目立つ 1 行で出して `done` を待つ。kind の無い古い形は従来どおり終端。
            let kind = text_field(p, &["kind"]).or_else(|| v.get("kind").and_then(|k| k.as_str()));
            match kind {
                Some(k) if !wire::TERMINAL_ERROR_KINDS.contains(&k) => vec![Signal::Gate(m)],
                _ => vec![Signal::Failed(m)],
            }
        }

        // ★serve.py は終端を `done` で出す。こちらは `result` だけを見ていたので、
        //   **終わったことに気づけず画面が固まる**（run の食い違いを直すと次に出る番だった）。
        "result" | "done" => {
            // 失敗として終わることもある。error があればそちらが優先。
            if let Some(m) = text_field(p, &["error"]) {
                return vec![Signal::Failed(m.to_string())];
            }
            let mut out = Vec::new();
            {
                // ★2026-09-04（W1）: 旧実装は「正常5種の一覧」を直書きし、それ以外を
                //   **内部識別子のまま**「終わり方: max_iters」と出していた。2 つ壊れていた:
                //     ① 同日足された `user_stop`（利用者が AskUser で終了を指示）が一覧に無く、
                //        利用者の意思どおりの終了が「変わった終わり方」として出ていた。
                //     ② `context_exhausted` や `phase_failed` が英語の識別子のまま画面に出る。
                //   語は端末版 `_OUTCOME_JA` と同じ表（`wire::OUTCOME_JA`）から引く。
                //   ★`ok`/`done`/`completed` は本体が出さない旧値。`wire` の表にも無いので
                //     未知として出る——それでよい（出さない理由が無い）。
                let o = text_field(p, &["outcome"]).unwrap_or("");
                if o == "cancelled" {
                    // 利用者が止めた＝失敗ではない。赤の「終わり方: 中断」ではなく、ふつうの 1 行にする（2026-11-19）。
                    out.push(Signal::Line("中断しました。".to_string()));
                } else if let Some(line) = wire::outcome_line(o) {
                    out.push(if wire::outcome_word(o).is_none() {
                        Signal::Unknown(line)
                    } else if wire::outcome_is_accepted(o) {
                        Signal::Line(line)
                    } else {
                        Signal::Gate(line)
                    });
                }
            }
            // 何も流れていないときだけ、まとめの本文を使う。
            // ★流れていたら使わない（同じ内容が二度出る／`result.text` は切り詰められている）。
            if !fold.any_text {
                if let Some(t) = text_field(p, &["text"]) {
                    fold.saw_text(t);
                    out.push(Signal::Text(t.to_string()));
                }
            }
            // 本文だけ落として残りを持つ（本文は別経路で既に流れている）。
            let meta = {
                let mut m = p.clone();
                if let Some(o) = m.as_object_mut() {
                    o.remove("text");
                }
                m
            };
            out.push(Signal::Done {
                usage: p.get("usage").filter(|u| !u.is_null()).cloned(),
                meta,
            });
            out
        }

        other => vec![Signal::Ignore(other.to_string())],
    }
}

// ---------------------------------------------------------------------------
// 試験（★ここは blackwall-core 側なので**実際に走る**。移す前は 1 本も走っていなかった）
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sig(json_text: &str, fold: &mut Fold) -> Vec<Signal> {
        interpret(&serde_json::from_str(json_text).unwrap(), fold)
    }

    #[test]
    fn 反復の切れ目では本文を捨てない() {
        // ★実機で壊れた形（2026-08-16 v0.1.51）。本体は**道具を呼ぶたび**に
        //   `iter_start` を出す（agent.py:1067）。これを「答えを作り直す」と読んで
        //   本文を捨てていたので、7手順の作業で**最後の1行しか画面に残らず**、
        //   正しい報告は思考欄に埋もれた。名前を合わせたときに意味を合わせていなかった。
        let mut f = Fold::default();
        assert_eq!(
            sig(r#"{"type":"iter_start","payload":{"iter":1}}"#, &mut f),
            vec![Signal::IterStart { ctx: 0, n_ctx: 0, compact_at: 0 }],
            "反復の切れ目で本文を捨てる合図を出している"
        );
    }

    #[test]
    fn 差し戻しのときだけ本文を捨てる() {
        let mut f = Fold::default();
        let out = sig(
            r#"{"type":"notice","payload":{"kind":"harness","message":"[fabrication 1] 裏付けのない数値を差し戻し"}}"#,
            &mut f,
        );
        assert!(out.contains(&Signal::NewGeneration), "差し戻しで作り直していない: {out:?}");
    }

    #[test]
    fn 背景の仕事の知らせは走行中だけの一行にする() {
        // ★2026-10-28 オーナーの画面: 長い走行の後、答えの下に背景の仕事の知らせが十数行並んだ。
        let mut f = Fold::default();
        let out = sig(r#"{"type":"notice","payload":{"kind":"monitor_error","message":"pid 3600 ended (exit 1) after 2m 14s"}}"#, &mut f);
        assert_eq!(out, vec![Signal::Live { text: "pid 3600 ended (exit 1) after 2m 14s".into(), gate: true }], "{out:?}");
        let out = sig(r#"{"type":"notice","payload":{"kind":"auto_bg","message":"Still running after 30 s; continues as background job J11"}}"#, &mut f);
        assert!(matches!(out.as_slice(), [Signal::Live { gate: false, .. }]), "{out:?}");
        // ★0.1.595 で「治ってなかった」: 見直し・促し・資源の警告など残りの経過も走行中だけにする（既定は走行中だけ）
        for k in ["review", "host_low", "preempted", "answer_nudge", "verifier", "harness"] {
            let line = format!(r#"{{"type":"notice","payload":{{"kind":"{k}","message":"経過 {k}"}}}}"#);
            let out = sig(&line, &mut f);
            // preempted は作り直しの合図（NewGeneration）も伴う。見るのは最後の 1 件
            assert!(matches!(out.last(), Some(Signal::Live { .. })), "{k} が答えの下に残る: {out:?}");
        }
        // 走行の終わりの状態（残したサーバー・止めた仕事・作ったファイル）は final を付けて来る＝答えの下に残る（平らな形でも）
        let out = sig(r#"{"type":"notice","payload":{"kind":"harness","message":"Servers left running (pid 42)","final":true}}"#, &mut f);
        assert_eq!(out, vec![Signal::Line("Servers left running (pid 42)".into())], "{out:?}");
        let out = sig(r#"{"v":1,"type":"notice","id":"r1","kind":"verifier","message":"Claims done but check fails: `pytest`","final":true}"#, &mut f);
        assert!(matches!(out.as_slice(), [Signal::Gate(_)]), "{out:?}");
        // 止められた・打ち切った事は final が無くても残す
        let out = sig(r#"{"v":1,"type":"notice","id":"r1","kind":"ws_guard","tool":"Bash","target":"/etc","n":1}"#, &mut f);
        assert!(matches!(out.as_slice(), [Signal::Gate(_)]), "{out:?}");
    }

    #[test]
    fn ふつうの注記では本文を捨てない() {
        let mut f = Fold::default();
        let out = sig(r#"{"type":"notice","payload":{"message":"道具を7つ渡しました"}}"#, &mut f);
        assert!(!out.contains(&Signal::NewGeneration), "ただの注記で本文を捨てている: {out:?}");
        assert_eq!(out, vec![Signal::Line("道具を7つ渡しました".into())],
                   "kind の無い注記が未知扱いになっている: {out:?}");
    }

    /// ★2026-09-04（W1）: `[invent ` `[contradiction ` は同日増えた門で、
    ///   旧実装の直書き一覧に無かった＝**差し戻し前の下書きが画面に残って同じ塊が 2 回**出た。
    ///   一覧は `wire::RESTART_MARKS` の 1 本にした（端末・Android と同じ表）。
    #[test]
    fn 新しい門でも下書きを捨てる() {
        for m in ["[invent 1] 実在しない対象", "[contradiction 1] 前の発言と食い違う",
                  "[verifier 1] 実体と違う", "[red_tests 1] 赤のまま", "[rerun 1] 1回だけ"] {
            let mut f = Fold::default();
            let line = format!(r#"{{"type":"notice","kind":"harness","message":"{m}"}}"#);
            let out = sig(&line, &mut f);
            assert!(out.contains(&Signal::NewGeneration), "{m}: {out:?}");
            // 差し戻しは走行中の経過（モデルが作り直す）＝状態行に赤で出し、答えの下には残さない（2026-10-28）
            assert!(matches!(out.last(), Some(Signal::Live { gate: true, .. })), "{m} が目立たない: {out:?}");
        }
    }

    /// ★旧実装は `message` を持たない notice を丸ごと捨てていた。
    ///   `cancel` / `cancel_applied` / `cancelled` は文言を持たないので、
    ///   「止める」を押した後の画面には本体からの合図が 1 行も出ていなかった。
    #[test]
    fn 中断の連鎖は一本にまとまる() {
        let mut f = Fold::default();
        let a = sig(r#"{"type":"notice","kind":"cancel","accepted":true,"applied":"running"}"#, &mut f);
        assert_eq!(a.len(), 2, "受理が消えている: {a:?}");
        assert!(matches!(&a[0], Signal::Live { text, .. } if text.contains("中断")), "{a:?}");
        assert!(matches!(&a[1], Signal::Live { gate: false, text } if text.starts_with("→ ")),
                "続報が答えの下に残る: {a:?}");
        let b = sig(r#"{"type":"notice","kind":"cancel","applied":"running"}"#, &mut f);
        assert!(b.is_empty(), "同じことを二度言っている: {b:?}");
        let c = sig(r#"{"type":"notice","kind":"cancelled","iter":3}"#, &mut f);
        assert!(c.is_empty(), "確定の合図で文が増えている: {c:?}");
    }

    /// ★知らない kind を黙って捨てない（数えて出す）。
    #[test]
    fn 知らない通知は不明な事象として出る() {
        let mut f = Fold::default();
        let out = sig(r#"{"type":"notice","kind":"brand_new_kind"}"#, &mut f);
        match out.as_slice() {
            [Signal::Unknown(t)] => assert!(t.contains("brand_new_kind"), "{t}"),
            other => panic!("黙って捨てている: {other:?}"),
        }
    }

    /// ★`user_stop` は利用者の意思どおりの終了。「変わった終わり方」ではない。
    #[test]
    fn outcome_は端末と同じ語で出る() {
        let mut f = Fold::default();
        let out = sig(r#"{"type":"done","outcome":"user_stop"}"#, &mut f);
        assert!(out.iter().any(|s| matches!(s, Signal::Line(t) if t == "✓ 終わり方: 利用者が終了")),
                "{out:?}");
        let mut f = Fold::default();
        let out = sig(r#"{"type":"done","outcome":"context_exhausted"}"#, &mut f);
        assert!(out.iter().any(|s| matches!(s, Signal::Gate(t) if t == "終わり方: 文脈切れ")),
                "識別子のまま出ている: {out:?}");
        let mut f = Fold::default();
        let out = sig(r#"{"type":"done","outcome":"accepted"}"#, &mut f);
        assert!(!out.iter().any(|s| matches!(s, Signal::Line(_) | Signal::Gate(_))),
                "正常終了で余計な行が出ている: {out:?}");
    }

    /// ★選択肢の説明（serve.py:1020）を落とさない。長さがずれても添字で引ける。
    #[test]
    fn 選択肢の説明を運ぶ() {
        let mut f = Fold::default();
        let s = sig(r#"{"type":"ask","ask":"a1","question":"どれ","options":["A","B"],"descriptions":["説明A"]}"#, &mut f);
        match &s[0] {
            Signal::Ask { options, descriptions, .. } => {
                assert_eq!(options.len(), descriptions.len(), "説明が1つずれる形");
                assert_eq!(descriptions[0], "説明A");
                assert_eq!(descriptions[1], "");
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn 封筒があってもなくても同じ意味に読む() {
        let mut f = Fold::default();
        assert_eq!(
            sig(r#"{"ts":1,"seq":2,"type":"token","payload":{"text":"あ"}}"#, &mut f),
            vec![Signal::Text("あ".into())]
        );
        let mut f2 = Fold::default();
        assert_eq!(sig(r#"{"type":"token","text":"あ"}"#, &mut f2), vec![Signal::Text("あ".into())]);
    }

    #[test]
    fn ツールの開始と終了はそのまま渡る() {
        let mut f = Fold::default();
        let s = sig(r#"{"type":"tool_start","payload":{"name":"Bash","input":{"command":"ls"}}}"#, &mut f);
        assert_eq!(s, vec![Signal::ToolStart { name: "Bash".into(), call: None, args: json!({"command":"ls"}) }]);
        let s = sig(r#"{"type":"tool_end","payload":{"name":"Bash","result":"a\nb","ok":false}}"#, &mut f);
        assert_eq!(s, vec![Signal::ToolEnd { name: "Bash".into(), call: None, result: "a\nb".into(), ok: false, kind: None, chars: None, truncated: None, summary: Value::Null }]);
    }

    /// ★相関 id（call）を捨てない。serve.py は `tool_call`/`tool_result` に `call` を
    ///   付けて対を保証しているのに、ここで落とすと同名並行ツールの引数が入れ替わる／消える
    ///   （実機 mstrnuc8）。id が来たら控えること。
    #[test]
    fn 相関idは信号に運ばれる() {
        let mut f = Fold::default();
        // ★実運用は「平らな形」（serve.py の out.emit は call/name/input を最上位に置く）。
        //   payload 包みと平ら、両方が payload_of で正規化されることを確かめる。
        let s = sig(r#"{"type":"tool_call","call":"c2","name":"Read","input":{"file_path":"a"}}"#, &mut f);
        assert_eq!(s, vec![Signal::ToolStart { name: "Read".into(), call: Some("c2".into()), args: json!({"file_path":"a"}) }]);
        let s = sig(r#"{"type":"tool_result","call":"c2","name":"Read","result":"x","ok":true}"#, &mut f);
        assert_eq!(s, vec![Signal::ToolEnd { name: "Read".into(), call: Some("c2".into()), result: "x".into(), ok: true, kind: None, chars: None, truncated: None, summary: Value::Null }]);
        // payload 包みでも同じ（両対応の担保）。
        let s = sig(r#"{"type":"tool_call","payload":{"call":"c9","name":"Grep","input":{"pattern":"x"}}}"#, &mut f);
        assert_eq!(s, vec![Signal::ToolStart { name: "Grep".into(), call: Some("c9".into()), args: json!({"pattern":"x"}) }]);
    }

    /// ★0件・失敗・実サイズを**捨てない**（2026-08-18 の UI 敵対レビュー）。
    ///   本体は送っているのにここで落としていたため、画面は「該当なし」を赤い失敗として
    ///   見せ、2万字の出力を preview の長さ（201字）として表示していた。
    #[test]
    fn 種類と実サイズを運ぶ() {
        let mut f = Fold::default();
        let s = sig(r#"{"type":"tool_result","call":"c1","name":"Grep","result":"(no matches)","ok":true,"kind":"empty","chars":20000,"truncated":true}"#, &mut f);
        match &s[0] {
            Signal::ToolEnd { kind, chars, truncated, ok, .. } => {
                assert_eq!(kind.as_deref(), Some("empty"), "0件と失敗を区別できない");
                assert_eq!(*chars, Some(20000), "切り詰める前の長さが落ちている");
                assert_eq!(*truncated, Some(true));
                assert!(*ok, "0件は失敗ではない");
            }
            other => panic!("{other:?}"),
        }
    }

    /// ★成否を結果本文から推測しない。明示が無ければ成功、`error` があれば失敗。
    #[test]
    fn 成否は明示された値だけで決める() {
        let mut f = Fold::default();
        let ok_of = |t: &str, f: &mut Fold| match &sig(t, f)[0] {
            Signal::ToolEnd { ok, .. } => *ok,
            other => panic!("{other:?}"),
        };
        assert!(ok_of(r#"{"type":"tool_end","payload":{"name":"R","result":"error: 何か"}}"#, &mut f));
        assert!(!ok_of(r#"{"type":"tool_end","payload":{"name":"R","result":"x","ok":false}}"#, &mut f));
        assert!(!ok_of(r#"{"type":"tool_end","payload":{"name":"R","error":"拒否"}}"#, &mut f));
    }

    /// 流し終えた本文と同じ `assistant` は捨てる（二重に出さない）。
    #[test]
    fn 同じ本文を二度出さない() {
        let mut f = Fold::default();
        sig(r#"{"type":"token","payload":{"text":"直します"}}"#, &mut f);
        assert_eq!(sig(r#"{"type":"assistant","payload":{"text":"直します"}}"#, &mut f), vec![]);
    }

    /// 断片が流れていなければ、`assistant` が本文そのもの（非ストリームの経路）。
    #[test]
    fn 断片が来ないときは本文として出す() {
        let mut f = Fold::default();
        assert_eq!(
            sig(r#"{"type":"assistant","payload":{"text":"直しました"}}"#, &mut f),
            vec![Signal::Text("直しました".into())]
        );
    }

    /// 本文と違う `assistant` は枠組みからの通知。本文に混ぜず注記にする。
    #[test]
    fn 本文と違う通知は注記にする() {
        let mut f = Fold::default();
        sig(r#"{"type":"token","payload":{"text":"読みます"}}"#, &mut f);
        assert_eq!(
            sig(r#"{"type":"assistant","payload":{"text":"[Ω-O] 正規の Write を作り直します"}}"#, &mut f),
            vec![Signal::Line("[Ω-O] 正規の Write を作り直します".into())]
        );
    }

    /// ★2026-09-04（W1）に**初めて走らせて**落ちた試験。期待値が
    ///   `Signal::NewGeneration`（＝画面の本文を捨てる）のままで、
    ///   **2026-08-16 に `iter_start` を「本文を捨てない」へ直した日から
    ///   ずっと嘘だった**（すぐ上の `反復の切れ目では本文を捨てない` と正面から矛盾する）。
    ///   一度も実行されない試験は、実装と反対のことを書いていても誰も気づかない——
    ///   移した目的がそのまま証明された形なので、記録として残す。
    #[test]
    fn 生成の区切りで思考の分離を作り直す() {
        let mut f = Fold::default();
        sig(r#"{"type":"token","payload":{"text":"あ"}}"#, &mut f);
        // 合図は IterStart（＝分離器だけ作り直す）。本文は捨てない。
        assert_eq!(sig(r#"{"type":"iter_start","payload":{"iter":1}}"#, &mut f), vec![Signal::IterStart { ctx: 0, n_ctx: 0, compact_at: 0 }]);
        // 文脈の使用量は平らな形（engine の wire.py）でも運ぶ（2026-11-10）。
        assert_eq!(sig(r#"{"type":"iter_start","iter":2,"ctx":4100,"n_ctx":131072,"compact_at":120000}"#, &mut f),
                   vec![Signal::IterStart { ctx: 4100, n_ctx: 131072, compact_at: 120000 }]);
        assert!(f.streamed.is_empty(), "区切りで流した本文の記憶を消す");
        assert!(f.any_text, "実行全体で本文が出た事実は消さない");
    }

    #[test]
    fn 終端は結果か誤りのどちらか一方() {
        let mut f = Fold::default();
        let s = sig(r#"{"type":"result","payload":{"outcome":"ok","usage":{"prompt_tokens":12}}}"#, &mut f);
        assert_eq!(s, vec![Signal::Done {
            usage: Some(json!({"prompt_tokens":12})),
            // ★`meta` は done の中身から本文だけ抜いたもの。model / outcome / iters は
            //   ここに載る（usage の中ではない）。
            meta: json!({"outcome":"ok","usage":{"prompt_tokens":12}}),
        }]);

        let mut f = Fold::default();
        assert_eq!(
            sig(r#"{"type":"error","payload":{"message":"落ちました"}}"#, &mut f),
            vec![Signal::Failed("落ちました".into())]
        );
        // result の中の error も失敗として扱う（"終わった" と読み違えない）
        let mut f = Fold::default();
        assert_eq!(
            sig(r#"{"type":"result","payload":{"error":"backend error"}}"#, &mut f),
            vec![Signal::Failed("backend error".into())]
        );
    }

    /// 一度も本文が流れなかったときだけ、まとめの本文を使う。
    #[test]
    fn 何も流れなければ結果の本文を出す() {
        let mut f = Fold::default();
        let s = sig(r#"{"type":"result","payload":{"text":"できました","outcome":"ok"}}"#, &mut f);
        assert_eq!(s[0], Signal::Text("できました".into()));
        assert!(matches!(s[1], Signal::Done { .. }));

        // 流れていれば使わない（result.text は切り詰められている）
        let mut f2 = Fold::default();
        sig(r#"{"type":"token","payload":{"text":"できました。詳しくは…"}}"#, &mut f2);
        let s2 = sig(r#"{"type":"result","payload":{"text":"できました","outcome":"ok"}}"#, &mut f2);
        assert_eq!(s2.len(), 1);
        assert!(matches!(s2[0], Signal::Done { .. }));
    }

    #[test]
    fn 知らない型は数えるだけで落とさない() {
        let mut f = Fold::default();
        assert_eq!(sig(r#"{"type":"heartbeat"}"#, &mut f), vec![Signal::Ignore("heartbeat".into())]);
        assert_eq!(sig(r#"{"payload":{}}"#, &mut f), vec![Signal::Ignore("(type なし)".into())]);
    }

    /// ★2026-10-16 監査: 終端にするのは done が来ない error（rejected・fatal・kind の無い古い形）だけ。
    ///   id の無い bad_request や inject_failed で走行を終えて本体を殺していた。
    #[test]
    fn 走行中の失敗の行では走行を終えない() {
        let mut f = Fold::default();
        for line in [
            r#"{"v":1,"type":"error","id":null,"kind":"bad_request","message":"type が無い"}"#,
            r#"{"v":1,"type":"error","id":"r1","kind":"inject_failed","message":"boom"}"#,
            r#"{"v":1,"type":"error","id":"r1","kind":"crash","message":"RuntimeError()"}"#,
        ] {
            let s = sig(line, &mut f);
            assert!(matches!(s.as_slice(), [Signal::Gate(_)]), "終端にしてしまった: {line} → {s:?}");
        }
        for line in [
            r#"{"v":1,"type":"error","id":"r1","kind":"rejected","message":"受け付けません"}"#,
            r#"{"v":1,"type":"error","id":null,"kind":"fatal","message":"入力ループが落ちた"}"#,
        ] {
            let s = sig(line, &mut f);
            assert!(matches!(s.as_slice(), [Signal::Failed(_)]), "終端にしていない: {line} → {s:?}");
        }
    }

    /// ★2026-10-18: 本体からアプリへの頼み（Browser 道具）を読む。id の無い頼みは捨てる（答えようがない）。
    #[test]
    fn 本体からの頼みを読む() {
        let mut f = Fold::default();
        assert_eq!(
            sig(r#"{"v":1,"type":"app_call","id":"r1","call":"c2","op":"browser","args":{"action":"read"}}"#, &mut f),
            vec![Signal::AppCall { call: "c2".into(), op: "browser".into(), args: json!({"action": "read"}) }]
        );
        assert!(sig(r#"{"v":1,"type":"app_call","id":"r1","op":"browser"}"#, &mut f).is_empty());
    }

    /// ★2026-10-16 監査: 問いが閉じたら箱を消す合図を出す（文は出さない）。
    #[test]
    fn 問いが閉じたら箱を消す合図を出す() {
        let mut f = Fold::default();
        assert_eq!(
            sig(r#"{"v":1,"type":"notice","id":"r1","kind":"ask_closed","ask":"a3","reason":"timeout"}"#, &mut f),
            vec![Signal::AskClosed { id: "a3".into() }]
        );
    }
}
