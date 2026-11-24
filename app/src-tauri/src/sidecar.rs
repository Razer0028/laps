//! sidecar.rs — 同梱した Python 本体（BLACKWALL エージェント）を子プロセスとして飼い、
//! その標準出力を**画面がすでに知っているイベント**へ翻訳する。
//!
//! ## なぜ子プロセスなのか（ここを変えると設計が崩れる）
//!
//! アプリ側にツールを作り直さない。実行可否の判断は Python の `policy.py`（660 行の実行ゲート）
//! が持っており、Rust に写せば**二重実装になって必ず乖離する**。乖離した実行ゲートは、
//! 「片方だけ緩い」状態を誰も気づかないまま作る。だからここは
//! **測定済みの機構をそのまま起動して繋ぐだけ**にする（完了基準 D）。
//!
//! ★このモジュールは policy ゲートを一切迂回しない。`LAPS_YES`（確認を飛ばす環境変数）は
//!   **意図的に渡していない**。渡すとアプリ経由の実行だけが無確認になり、
//!   「実行可否をモデルの外側に置く」という本プロジェクトの中核原則が、アプリという
//!   別の入口からだけ空洞になる。ここは弱めない。
//!
//! ## 約束ごと（画面との契約。破ると UI が固まる）
//!
//! 画面の `ask()` は `chat-done` でのみ解決し `chat-error` でのみ棄却される（ui/app.js:432-433）。
//! どちらも来ないと送信ボタンが戻らない。そこで:
//!
//! * 正常終了 … ここで `chat-done` を**ちょうど1回**出して `Ok(())` を返す。
//! * 起動できない／1文字も出せずに壊れた … `chat-done` を**出さずに** `Err(理由)` を返す。
//!   呼び出し側（agent.rs）が `chat-error` を1回出す＝終端はやはり1回。
//! * **本文を出したあとに壊れた** … 理由を本文へ `— …` の形で足し、`chat-done` で閉じる。
//!   ここまでの答えを捨てないため（llama.rs / agent.rs と同じ作法）。
//!   ★「黙って正常終了する」ことは絶対にしない。壊れたら必ず理由が画面に出る。
//!
//! 出すイベントは既存のものだけ: `chat-token` / `chat-think` / `chat-reclassify` /
//! `chat-promote` / `chat-stat` / `chat-done` / `chat-notice` / `agent-tool`。**増やしていない。**
//!
//! ★`chat-notice` は画面側で**上書き**される（app.js:430 `notice = ev.payload?.message`）。
//!   複数出すと最後の1つしか残らないので、途中経過の注記は agent.rs の `notice()` と同じく
//!   `chat-token` に「— …」で流す。`chat-notice` は終端で1つだけ言いたいことに使う。
//!
//! ## 電文（JSON Lines・1行1オブジェクト）
//!
//! 送る（stdin）:
//! ```text
//! {"v":1,"type":"run","id":"r3","model":"…","workspace":"C:\\work\\x","messages":[…]}
//! ```
//! `messages` は OpenAI 形式の配列（画面が組み立てたもの。末尾が今回の依頼）。
//! `workspace` は**すでに子プロセスの cwd になっている**場所。照合用に載せる
//! （ここを見て `os.chdir` で別の場所へ移らないこと。移ると「前のフォルダに書いた」が起きる）。
//!
//! 受ける（stdout）: `laps/engine/server.py` の Out と同じ封筒（旧い laps/events.py は 2026-11-19 に外した）。
//! ```text
//! {"ts":…,"seq":3,"type":"token","payload":{"text":"…"}}
//! ```
//! 読む型と扱い（**知らない型は黙って捨てず、数えて診断に出す**）:
//!
//! | type | payload | 扱い |
//! |---|---|---|
//! | `ready` | 任意（**中身は今のところ読んでいない**） | 起動の握手。これが来るまで「使える」と言わない |
//! | `init` | `{session_id,model,tools,cwd,…}` | 診断用。`cwd` が依頼と違えば注記を出す |
//! | `iter_start` | `{iter}` | 1回の生成の区切り（思考の分離をここで作り直す） |
//! | `token` | `{text}` | 本文の断片。`<think>` の分離を通して流す |
//! | `think` | `{text}` | 思考だと**送り手が確定させている**断片（分離を通さない） |
//! | `assistant` | `{text}` | ①まだ何も流れていなければ本文 ②流れた本文と同じなら捨てる ③違えば注記 |
//! | `tool_start` | `{name,input}` | 引数を控える |
//! | `tool_end` | `{name,result,ok?}` | `agent-tool` を1件出す |
//! | `notice` | `{message}` | 注記 |
//! | `cancelled` | `{iter}` | 注記 |
//! | `error` | `{message}` | この実行は失敗（終端） |
//! | `result` | `{text?,usage?,outcome?,error?}` | 正常終了（終端） |
//!
//! ★`tool_end` に `ok` が無いときは**成功として扱う**。Python 側は拒否も文字列で返すので、
//!   `ok` を送ってくれないと「policy に止められた」が緑で表示される。**serve.py は `ok` を必ず載せること。**
//!   ここで結果本文から成否を推測することはしない（"error" を含む正常な出力を赤くしてしまう）。
//!
//! ### serve.py 側に要る3つ（これが無いと繋がらない）
//!
//! 1. 起動したら**まず `{"type":"ready"}` を1行**出す。以後 stdin の `run` を待つ。
//! 2. 1回の `run` は、必ず **`result` か `error` のどちらか1つ**で終える
//!    （どちらも出さずに黙ると、こちらは無出力の見張り（既定 600 秒）で切るしかない）。
//! 3. `--probe` を付けて起動されたら、**`ready` を1行出して終了コード 0 で終わる**。
//!    `core_available()` がこれで生存を確かめる（居るかどうかを、待ち受けを作らずに測る唯一の手）。
//!
//! stdout は電文専用。**print でのデバッグ出力を混ぜないこと**（JSON でない行は読み飛ばすが、
//! 混ざるほど診断が濁る）。人が読む出力は stderr へ（こちらが `sidecar.log` に落とす）。
//!
//! ## やっていないこと（次の人が誤解しないように）
//!
//! * **孫プロセスは道連れにできない。** Windows では親を殺しても子は生き残る。
//!   エージェントが起こしたシェルやコンパイラまで確実に始末するには Job Object
//!   (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) が要り、`windows` crate の feature 追加を伴う
//!   （＝gpu.rs の読み直しが必要）。**別コミットの仕事**として残す。
//! * 待ち受けポートを作らない。stdin/stdout だけで話す（selftest.rs と同じ理由＝
//!   起動できるポートは任意コード実行の口）。
//!
//! ★推論器（llama-server）は**わざと生かしたまま**にしてある（main.rs:222-230, DESIGN §0）。
//!   こちらは逆で、**必ず殺す**。推論器は状態を持たず読み直せば済むが、エージェントは
//!   作業ディレクトリに書き込む主体で、画面が閉じたあとも見えない場所で走り続けるのが最悪だから。
//!   この非対称は意図的。「llama を殺していないのだから揃えよう」と直さないこと。

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::sync::Mutex as AsyncMutex;

/// 線で来る事象を画面の言葉へ翻訳する表（印・outcome・notice の kind・中断の 1 本化）。
///
/// ★実体は検査用クレート側（`blackwall-core/src/wire.rs`）に置いてある。
///   このファイルは `tauri` を参照するのでホストでは `cargo test` がリンクできず、
///   ここに表を書くと**また一度も走らない試験**になる（Rust 195 本が既にそれ）。
///   `#[path]` で参照しているだけなので実体は1本＝二重管理は起きない。
#[path = "../blackwall-core/src/wire.rs"]
mod wire;

/// 線の 1 行を画面への操作（`Signal`）へ翻訳する純粋関数。
///
/// ★同じ理由でここから出した（2026-09-04・W1）: このファイルは `tauri` を参照するので
///   ホストでは `cargo test` がリンクできず、**翻訳器の試験は一度も実行されていなかった**。
///   実体を `blackwall-core` に置き、preflight 5.05 で実際に走らせる。
#[path = "../blackwall-core/src/translate.rs"]
mod translate;
use translate::{interpret, payload_of, Fold, Signal};

// ---------------------------------------------------------------------------
// 定数
// ---------------------------------------------------------------------------

/// 同梱 Python の実体名。
#[cfg(windows)]
const PY_BIN: &str = "python.exe";
#[cfg(not(windows))]
const PY_BIN: &str = "python3";

/// 起動するモジュール。
const PY_MODULE: &str = "laps.serve";

/// コンソールウィンドウを出さずに子プロセスを起動する（Windows のみ・llama.rs と同じ値）。
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 中止を伝えてから、本体の「終わり」を待つ猶予。過ぎたらこちらで閉じる
/// （待ち続けると画面が「止める」のまま戻らない）。
///
/// ★2026-09-04（W1）3 秒 → `wire::CANCEL_STALL_SECS`（10 秒）へ。理由は 2 つ:
///   ① 端末版は同じ場面を `term/app.py::CANCEL_STALL_SECS = 10.0` で測っており、
///      **同じ出来事を別の物差しで測っていた**（実機で中断が 73 秒止まらなかった走行がある）。
///   ② 3 秒で黙って閉じると、本体がまだ動いているのに画面は「終わった」と言う。
///      いまは閉じる前に「まだ止まっていない」を必ず 1 行出す（`CancelTrack::stall`）。
///   ⚠ 引き換えに、本体が返事をしない病的な場合はボタンが最大 10 秒戻らない。
///      黙って嘘の完了を出すより良い、という取り替え。
const STOP_GRACE: std::time::Duration = std::time::Duration::from_secs(wire::CANCEL_STALL_SECS);

/// 握手（`ready` が来るまで）の上限。Python の起動は 0.3〜2 秒程度だが、
/// 初回はウイルス対策の走査が入って数秒かかることがある。
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);

/// 生存確認（`--probe`）の上限。
const PROBE_TIMEOUT: Duration = Duration::from_secs(20);

/// 生成中、**1バイトも来ない**まま待てる長さ。
/// ★全体の上限は置かない（長い生成が途中で切られる。llama.rs と同じ判断）。
/// ⚠ 長いビルドを回すツールは無出力のまま数分かかることがあるので短くしない。
///
/// ★2026-09-02 に 600 → 1800 秒。**実測で足りていなかった。**
///   検収トレース8本 424,104 行のイベント間隔を測ると、無音の最大は **659.4 秒**
///   （成功した `tool_result` の直前＝道具が正常に走っていただけ）で、旧 600 秒を
///   超えていた。8 本中 1 本。442 秒・410 秒も出ており、そもそも余裕が無い。
///   ここで切ると走行は**失敗として画面に出るのに、子は走り続けてファイルを書く**
///   ——利用者に嘘をついたまま作業が進む、いちばん質の悪い外し方になる。
///   この製品の相手は apt / ビルド / ssh を回す仕事なので、10 分は現実に短い。
/// ⚠ 引き換えに、本当に固まった子は表に出るまで最大 30 分かかる。それを埋めるのは
///   この定数ではなく「止める」（押せば即座に cancel が飛ぶ）。時間で殴らない。
///   環境変数 `LAPS_SIDECAR_IDLE_SECS` でいつでも縮められる。
const IDLE_TIMEOUT_DEFAULT: Duration = Duration::from_secs(1800);

/// 1行の上限（バイト）。超えた行は捨てる。
/// ツールが巨大な出力を1行で吐いたときに、こちらが際限なくメモリを食って落ちるのを防ぐ。
/// ★捨てたことは必ず画面に出す（黙って落とすと「なぜ結果が無いのか」が分からなくなる）。
const MAX_LINE_BYTES: usize = 1_048_576;

/// 生存確認の結果を使い回す長さ。
/// 恒久キャッシュにすると、更新で実体の場所が変わったときに復帰できない（§6.2）。
const AVAIL_CACHE: Duration = Duration::from_secs(30);

/// 画面に出すツール結果の抜粋の長さ（文字数。バイトではない）。agent.rs と揃える。
const PREVIEW_CHARS: usize = 200;

/// `agent-tool` に載せる引数の、文字列1つあたりの上限（文字数）。
/// ★画面は引数を JSON にして1行で出す（app.js の `addToolLine`）。Write の `content` が
///   そのまま入ると、会話に数千文字の1行が生える。切ったことが分かる形で切る。
const ARG_STRING_MAX: usize = 200;

/// 終了指示を出してから強制終了するまでの猶予。
const SHUTDOWN_GRACE: Duration = Duration::from_millis(200);

// ---------------------------------------------------------------------------
// 行の切り出し
// ---------------------------------------------------------------------------

/// 1行ぶんの読み取り結果。
#[derive(Debug, PartialEq, Eq)]
enum Chunk {
    /// 完全な1行（改行と `\r` は落としてある）。
    Line(String),
    /// 上限を超えたので捨てた行。捨てたおおよそのバイト数。
    Oversized(usize),
}

/// 受け取ったバイト列を行に切る。
///
/// ★UTF-8 の途中で割れても壊れない。区切りは `\n`(0x0A) で、UTF-8 の多バイト文字の
///   2バイト目以降は必ず 0x80〜0xBF なので **0x0A が文字の内側に現れることはない**。
///   したがって「改行で切ってから丸ごと復号する」この順序なら、
///   1バイトずつ届いても文字が壊れない（llama.rs / agent.rs の SSE 読みと同じ理屈）。
struct LineReader {
    buf: Vec<u8>,
    /// 上限を超えた行を読み飛ばしている最中か。
    dropping: bool,
    /// 読み飛ばした量。
    dropped: usize,
    max: usize,
}

impl LineReader {
    fn new(max: usize) -> Self {
        Self { buf: Vec::with_capacity(8192), dropping: false, dropped: 0, max }
    }

    /// 抱えたままの「改行が来ていない書きかけ」を捨て、そのバイト数を返す。
    /// ★依頼と依頼のあいだにだけ使う。1回の実行が終わった時点で書きかけが残っているのは
    ///   送り手が行を閉じずに終えたということで、それを次の依頼の1行目に継ぎ足すと
    ///   **先頭が壊れた JSON** になり、次の実行が丸ごと読み飛ばされる。
    fn discard_partial(&mut self) -> usize {
        let n = self.buf.len() + self.dropped;
        self.buf.clear();
        self.dropping = false;
        self.dropped = 0;
        n
    }

    fn push(&mut self, data: &[u8]) -> Vec<Chunk> {
        let mut out = Vec::new();
        let mut rest = data;
        loop {
            match rest.iter().position(|b| *b == b'\n') {
                Some(i) => {
                    let (head, tail) = rest.split_at(i + 1);
                    rest = tail;
                    if self.dropping {
                        // 捨てている途中。この改行で1行ぶんが終わったので、そこで復帰する。
                        self.dropped += head.len();
                        out.push(Chunk::Oversized(self.dropped));
                        self.dropping = false;
                        self.dropped = 0;
                        self.buf.clear();
                        continue;
                    }
                    self.buf.extend_from_slice(&head[..head.len() - 1]);
                    while self.buf.last() == Some(&b'\r') {
                        self.buf.pop();
                    }
                    // 壊れたバイト列でも落とさない（from_utf8_lossy）。捨てるより見せるほうがよい。
                    let s = String::from_utf8_lossy(&self.buf).into_owned();
                    self.buf.clear();
                    if !s.trim().is_empty() {
                        out.push(Chunk::Line(s));
                    }
                }
                None => {
                    if self.dropping {
                        self.dropped += rest.len();
                    } else {
                        self.buf.extend_from_slice(rest);
                        if self.buf.len() > self.max {
                            // 抱えている分ごと捨てる。ここで確保も返す（巨大な Vec を持ち続けない）。
                            self.dropping = true;
                            self.dropped = self.buf.len();
                            self.buf = Vec::with_capacity(8192);
                        }
                    }
                    break;
                }
            }
        }
        out
    }
}




/// この行は**いま待っている依頼のもの**か。
///
/// ★2026-09-02 監査で判明した穴。serve.py は**全ての行に依頼 id を載せている**
///   （`Emitter.emit` が `"id": id_` を必ず書く。実トレース 424,104 行すべてに乗っていた）。
///   受ける側はそれを一度も見ていなかったので、**前の依頼の続きが次の依頼の答えに混ざる**。
///
///   混ざる経路は1つではない（どれも実在する）:
///     ・道具の実行が `idle` を超えた → こちらは失敗として抜けるが、**子は走り続ける**
///       （実測: 実トレース8本のうち1本に 659.4 秒の無音がある＝当時の上限 600 秒超）
///     ・`error` を受けて抜ける → その直後に来る `done` はパイプに残る
///     ・「止める」を押して STOP_GRACE(3秒) で抜ける → 本体は道具の切れ目まで止まらない
///   いずれの場合も、次の依頼は**生きている子を使い回す**（core_send の `child_alive()`）。
///   残っていた `done` を次の依頼の終端として読むと、**別の問いの答えが、いまの問いの
///   答えとして画面にも記録にも残る**。黙って起きるので、後から見ても気づけない。
///
/// ★捨てるのは「**別の**依頼 id が明示されている行」だけ。
///   `id` が無い行（握手の `ready`、`kind=fatal` / `bad_request` の `error`）は
///   依頼に属さない全体の知らせなので**必ず通す**。ここを取り違えると、
///   起動できない理由が1行も出なくなる（沈黙は最悪の壊れ方）。
fn belongs_to(v: &Value, id: &str) -> bool {
    match v.get("id") {
        Some(Value::String(s)) => s == id,
        _ => true, // null・欠落＝依頼に紐づかない知らせ
    }
}

/// 読み飛ばした行の控え。記録の `protocol` に載せる形にする。
///
/// ★run_loop から切り出してあるのは、**試験が本番と同じ1本を呼ぶ**ため。
///   試験の中で同じ組み立てを書き直すと、本番だけ直っていなくても緑になる。
///
/// 何も飛ばしていなければ `None`＝欄ごと作らない（0 件と「見ていない」を混ぜない）。
fn skipped_record(unknown: &std::collections::BTreeMap<String, usize>) -> Option<Value> {
    if unknown.is_empty() {
        return None;
    }
    let mut skipped = serde_json::Map::new();
    for (k, n) in unknown {
        skipped.insert(k.clone(), json!(n));
    }
    Some(json!({ "skipped": Value::Object(skipped) }))
}



// ---------------------------------------------------------------------------
// 文字列の整形（agent.rs に同じ考えの双子がある。あちらは private なので写している）
// ---------------------------------------------------------------------------

/// 文字数（バイトではない）で切る。切ったかどうかも返す。
/// ★バイトで切ると日本語の途中で割れて panic する。
fn clamp_chars(s: &str, max: usize) -> (String, bool) {
    let mut out = String::new();
    let mut n = 0usize;
    for c in s.chars() {
        if n == max {
            return (out, true);
        }
        out.push(c);
        n += 1;
    }
    (out, false)
}

/// 画面に出す1行ぶんの抜粋。改行はつぶすが、全角空白などはそのまま残す。
fn preview(s: &str) -> String {
    let flat: String = s
        .chars()
        .map(|c| if c == '\n' || c == '\r' || c == '\t' { ' ' } else { c })
        .collect();
    let (mut p, cut) = clamp_chars(flat.trim(), PREVIEW_CHARS);
    if cut {
        p.push('…');
    }
    p
}

/// `agent-tool` に載せる引数を、画面に出して害の無い大きさへ落とす。
/// ★値を黙って短くしない。切ったことと元の長さを本文に残す
///   （「content が空だったのか、切られたのか」が分からないと調査できない）。
fn clamp_args(v: &Value) -> Value {
    match v {
        Value::String(s) => {
            let (head, cut) = clamp_chars(s, ARG_STRING_MAX);
            if cut {
                Value::String(format!("{head}…（全 {} 文字）", s.chars().count()))
            } else {
                Value::String(head)
            }
        }
        Value::Array(a) => Value::Array(a.iter().map(clamp_args).collect()),
        Value::Object(o) => {
            Value::Object(o.iter().map(|(k, x)| (k.clone(), clamp_args(x))).collect())
        }
        other => other.clone(),
    }
}

/// 終了時にまとめて渡す数値。画面の「消費」表示（app.js の `fmtStat`）がこの鍵を直に読む。
/// ★鍵名を変えると表示が黙って空になる。
/// 画面へ出した断片を、そのまま記録にも積む。
///
/// ★**画面へ出す箇所が3つあるのに、記録に積んでいたのは1箇所だけだった**（〜0.1.49）。
///   残る2つは `split.finish(..)` の吐き出し——反復の切れ目と、走行の終わり。
///   分離器が保留していた本文はここで出るので、**画面には出て記録には残らない**。
///
///   実機 0.1.49 msuxsowl で実際にこうなった:
///     画面  思考欄=「字数が多すぎて…」56字 ／ 本文=「このファイルは、画像スクラピング…」
///     記録  think=両方(167字)            ／ output=**空**
///   これを見て私は「画面も空だ」と読み、**存在しない不具合を直そうとした**。
///   記録が画面と食い違うと、直す対象そのものを間違える。
///
/// ★だから出口を1つの関数にまとめる。**3箇所に書き写さない。**
fn record_piece(
    p: &crate::think::Piece,
    ans: &mut String,
    think: &mut String,
    promoted: &mut usize,
    reclassified: &mut usize,
    demoted: &mut String,
) {
    use crate::think::Piece;
    match p {
        Piece::Answer(x) => ans.push_str(x),
        Piece::Think(x) => think.push_str(x),
        // 思考として積んだ分を本文へ移す。**同じ文字列を消してから足す**
        // （断片が文字列を持っているのは、移す量を受け側に知らせるため）。
        Piece::Promote(x) => {
            if let Some(i) = think.rfind(x.as_str()) {
                think.replace_range(i..i + x.len(), "");
            }
            ans.push_str(x);
            *promoted += x.chars().count();
        }
        // 逆向き。本文として積んだ分を思考へ移す。
        Piece::Reclassify(x) => {
            if let Some(i) = ans.rfind(x.as_str()) {
                ans.replace_range(i..i + x.len(), "");
            }
            think.push_str(x);
            *reclassified += x.chars().count();
            *demoted = x.clone();
        }
    }
}

fn done_payload(meter: &crate::think::Meter, usage: &Option<Value>) -> Value {
    let mut v = meter.stat();
    if let Some(o) = v.as_object_mut() {
        o.insert("usage".into(), usage.clone().unwrap_or(Value::Null));
        o.insert(
            "prompt_tokens".into(),
            json!(usage.as_ref().and_then(|u| u.get("prompt_tokens").and_then(|p| p.as_u64()))),
        );
    }
    v
}

/// 依頼の1行を組み立てる。
fn build_request(id: &str, model: &str, messages: &[Value], workspace: &Path, thinking: bool,
                 // ★この 1 回だけの道具一式（2026-09-21・会話モード）。None なら設定の値。
                 toolset: Option<&str>,
                 // ★作業のモード（2026-10-15 オーナー「解析・開発・3D モデリングで分けましょう」）。
                 //   general（既定）／development／modeling／analysis。本体が渡す道具と前置きの節を変える。
                 mode: Option<&str>) -> Value {
    // ★アプリの指示（省略しない・道具の使い方 等）は system ロールでは届かない。
    //   本体は「線の向こうの文字列を system に入れない」という正しい理由で捨てるが、
    //   アプリ自身が書いた指示はそれに当たらない。別の口（guidance）で渡す。
    //   捨てられていたせいで、実機で「字数制限を超えたので、続きを表示する」対策が
    //   効いていなかった。
    // ★オーナー提案（2026-08-11）: 動いている環境をモデルに伝える。
    //   実機の記録で、MSYS2 だと思い込んで 52 秒、lscpu を試して失敗、が続いた。
    //   **知らないから推測する**のであって、渡せば起きない。
    //   アプリ側（OS・作業ディレクトリ・モデル）と本体側（policy・道具）の両方から作る。
    // ★2026-10-16 監査: モデルが読む文なので英語に。Linux のシェルを "sh" と書いていたが、本体の <env> と Bash の説明は
    //   bash（Bash 道具は bash で走る）。食い違う事実を渡さない。推論器の行の前に紛れていた空白も外した。
    let env_brief = format!(
        "[Environment]\nOS: {}\nShell: {}\nWorking directory: {}\nModel: {}\nInference: local GPU (llama.cpp)\n",
        if cfg!(windows) { "Windows" } else { "Linux" },
        if cfg!(windows) { "cmd.exe (not MSYS2, not WSL)" } else { "bash" },
        crate::workspace::display_path(workspace),
        if model.is_empty() { "(not set)" } else { model },
    );

    let (guidance, rest): (Vec<&Value>, Vec<&Value>) = messages
        .iter()
        .partition(|m| m.get("role").and_then(|r| r.as_str()) == Some("system"));
    let guidance: String = std::iter::once(env_brief.as_str())
        .chain(guidance.iter().filter_map(|m| m.get("content").and_then(|c| c.as_str())))
        .collect::<Vec<_>>()
        .join("\n");
    let rest: Vec<Value> = rest.into_iter().cloned().collect();
    json!({
        "v": 1,
        "type": "run",
        "id": id,
        "model": model,
        // ★区切りに `\` を含む Windows のパスをそのまま載せる（serde が正しく脱出する）。
        // ★verbatim 接頭辞（`\\?\`）を外して渡す。canonicalize が付けるもので、
        //   Python の pathlib はこれを解決できず「path not found」になる（実機で発生）。
        //   モデルが自力で外して再試行していたが、1往復ぶん無駄になっていた。
        "workspace": crate::workspace::display_path(workspace),
        "messages": rest,
        "guidance": guidance,
        // ★このアプリが頼まれて答えられること（2026-10-18）。本体はここに名乗った物だけを道具に出す（Browser）。
        "app_caps": ["browser", "browser_cdp", "editor", "chrome"],   // editor: Editor 道具でエディターに開く（2026-11-17）
        // ★オーナー要求「ClaudeCode レベルに道具を使えるように」。
        //   ★既定は **full**（下の unwrap_or_else。2026-09-02 検査で訂正: 旧文「既定は focused（8個）」は
        //   実物と逆だった）。道具数が選択精度を下げる実測はあるので、**増やすと選択精度は下がりうる**ので、
        //   効果は記録（telemetry）で測ってから判断する。
        // ★呼び出しごとの指定が在ればそれを使う（会話モード="chat"）。
        //   無ければ今までどおり設定の値（既定 full）。
        "tools": toolset.map(|s| s.to_string()).unwrap_or_else(
            || crate::config_value("toolset").unwrap_or_else(|| "full".into())),
        // ★作業のモード（画面の選択・2026-10-15）。無ければ設定の値、それも無ければ送らない＝本体の既定（general）。
        "mode": mode.map(|s| s.to_string()).or_else(|| crate::config_value("mode"))
            .map(Value::from).unwrap_or(Value::Null),
        // ★画面の「思考を使う」を本体へ届ける。渡していなかったので切替が効かず、
        //   本体の既定（ON）のまま動いていた。
        "think": thinking,
        // ★思考の深さ（画面の Think の「浅く／深く」・2026-10-11）。無ければ送らない＝本体の既定（浅く）。
        "effort": if thinking { crate::sampling::effort().map(Value::from).unwrap_or(Value::Null) } else { Value::Null },
        // ★作業の上限（2026-08-22 オーナー決定「上限をなしにします」）。
        //   既定 0 ＝ 無制限。渡していなかったので本体の既定 40 が効いており、
        //   「ClaudeCode のような…を作って」規模の依頼が 40反復で切られていた。
        //   ⚠ 反復だけ外しても、時間と道具呼び出しの予算が次の天井になる。
        //     3つとも config.toml から渡す（既定はどれも 0＝無制限）。
        //   止めるものが消えるわけではない: ループ検知・文脈切れ・「止める」は生きている。
        "max_iters": crate::budget_value("max_iters"),
    })
}

/// 経過の1行を断り書きとして出す。
///
/// ★以前は本文チャンネル（chat-token）へ流していた。理由は「chat-notice は
/// 上書きされるから」だったが、それは**画面側が最後の1件しか持たない**という
/// 別の欠陥で、直す場所を間違えていた。本文へ混ぜると、断り書きが回答の一部として
/// 保存され、次の送信で**モデル自身の発言として戻る**（agent.rs で同じ誤りを直した）。
/// 画面側を「溜める」に変え、こちらは断り書きのまま出す。
fn line_note(app: &impl Out, text: &str) {
    note_at(app, text, wire::Level::Plain);
}

/// 目立たせる 1 行（門・差し戻し・失敗）。
fn gate_note(app: &impl Out, text: &str) {
    note_at(app, text, wire::Level::Gate);
}

/// 知らない事象。**黙って捨てない**——画面にも診断にも出す。
fn unknown_note(app: &impl Out, text: &str) {
    note_at(app, text, wire::Level::Unknown);
}

/// 走行中だけ出す 1 行（経過の知らせ・wire::AFTER_KINDS に無く `final` も無い notice）。画面は状態行に出し、答えの下の注記には残さない（`live`）。
fn live_note(app: &impl Out, text: &str, gate: bool) {
    let level = if gate { wire::Level::Gate } else { wire::Level::Plain };
    app.out(
        "chat-notice",
        json!({ "message": text, "level": level.as_str(), "live": true }),
    );
}

/// ★`level` を載せる（2026-09-04・W1）。旧: 画面はすべての注記を同じ薄い灰色で描いており、
///   差し戻し・作業場ガード・工程不合格が**ふつうの経過報告と見分けられなかった**。
fn note_at(app: &impl Out, text: &str, level: wire::Level) {
    app.out(
        "chat-notice",
        json!({ "message": text, "level": level.as_str() }),
    );
}

/// 画面へ出す口（2026-10-30・走行の器）。★会話ごとに同時に走るので、走行の知らせには札（`lane`＝会話）を付ける。
///   画面は自分の札の知らせだけを拾う。札の無い走行（古い呼び手・声の係など）は今までどおり何も付けない。
pub trait Out {
    fn out(&self, name: &str, v: Value);
}

impl Out for AppHandle {
    fn out(&self, name: &str, v: Value) {
        let _ = self.emit(name, v);
    }
}

pub struct LaneOut<'a> {
    pub app: &'a AppHandle,
    pub lane: Option<&'a str>,
}

impl Out for LaneOut<'_> {
    fn out(&self, name: &str, mut v: Value) {
        if let (Some(l), Some(o)) = (self.lane, v.as_object_mut()) {
            o.insert("lane".into(), json!(l));
        }
        let _ = self.app.emit(name, v);
    }
}

// ---------------------------------------------------------------------------
// 実体の場所
// ---------------------------------------------------------------------------

/// 同梱 Python を探す。失敗したら**試した場所を全部返す**（llama.rs と同じ作法。
/// 「見つかりません」だけでは、実機で何を直せばよいか誰にも分からない）。
///
/// AppHandle を取らないのは、`core_available()` が引数を持たない固定シグネチャだから。
/// Tauri のリソース解決の代わりに、実行ファイルからの相対で辿る（llama.rs の headless 版と同じ）。
fn resolve_python() -> Result<PathBuf, String> {
    let mut tried: Vec<String> = Vec::new();

    if let Ok(p) = crate::envcompat::var("LAPS_PYTHON") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Ok(p);
        }
        tried.push(format!("{} (LAPS_PYTHON)", p.display()));
    }

    let mut roots: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        // 配布時: <インストール先>\pyruntime\python.exe
        //（tauri.conf.json の resources が "../pyruntime" → "pyruntime" を指している）
        if let Some(d) = exe.parent() {
            roots.push(d.to_path_buf());
            // 念のため resources/ 配下も見る（対応表を変えても動くように）
            roots.push(d.join("resources"));
            // 開発時: target/<triple>/<profile>/app.exe から見て 4 つ上が app/
            if let Some(up) = d.parent().and_then(|x| x.parent()).and_then(|x| x.parent()) {
                roots.push(up.to_path_buf());
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        roots.push(cwd.clone());
        roots.push(cwd.join(".."));
    }

    for r in roots {
        let c = r.join("pyruntime").join(PY_BIN);
        if c.is_file() {
            return Ok(c);
        }
        tried.push(c.display().to_string());
    }

    // ★Linux の GUI 版（.deb・2026-11-11）: 入れたとき postinst が同梱の wheel から作る自分専用の venv。
    #[cfg(not(windows))]
    {
        let c = PathBuf::from("/opt/blackwall-console/venv/bin/python3");
        if c.is_file() {
            return Ok(c);
        }
        tried.push(c.display().to_string());
    }

    Err(format!(
        "同梱 Python ({PY_BIN}) が見つかりません。探した場所:\n  {}\n\
         開発中は LAPS_PYTHON に実体を指してください。",
        tried.join("\n  ")
    ))
}

/// Python 一式を **ASCII のパス**へ置き直して、その実体を返す。
///
/// ★実機の利用者名は `C:\Users\田中　優輝`。インストール先は `installMode: currentUser` なので
/// **配布物は必ず非 ASCII のパスに載る**。CPython 自身は Windows の広い文字コード API を
/// 使うので直接の障害にはならないが、**エージェントはそこからさらに孫を起こす**
/// （シェル・コンパイラ・llama.cpp 系の道具）。狭い文字コード API を使う孫は同じ壁に当たる。
/// この壁で既に4回壊れているので、ここでも実績のある「場所ごと ASCII へ寄せる」を採る。
///
/// llama.rs の `stage_engine` は**直下のファイルしか複製しない**（サブフォルダを見ない）。
/// Python は `app/` `site-packages/` を持つ木なので、こちらは再帰で写す。
/// 目印ファイルに「ファイル数とバイト数」を書いておき、一致していれば複製しない
/// （838 ファイル・32MB の複製が毎回走ると起動が目に見えて遅くなる）。
/// 同梱の道具の部品の置き場（2026-10-07）。
/// ★`~/.blackwall` ではなくアプリのデータ根に置く。利用者のフォルダは消されることがあり（実際に消えた例がある）、
///   インストール先（%LOCALAPPDATA%\BLACKWALL Console）は利用者名の全角空白で csc がつまずいた前例がある。
/// ★0.1.308: Blackwall が取り寄せる道具の置き場（tools・ffmpeg など）も同じ理由でデータ根へ。
pub fn parts_env() -> [(&'static str, PathBuf); 4] {
    let r = crate::app_root();
    [
        ("LAPS_LISTEN_DIR", r.join("listen")),
        ("LAPS_DESKTOP_DIR", r.join("desktop")),
        ("LAPS_TOOLS_DIR", r.join("tools")),
        // 声の道具（oc/voice.py）の専用の Python・PyTorch・モデル（5〜7GB）。起動時のそろえには入れない（使うときに取り寄せる）。
        ("LAPS_VOICE_DIR", r.join("voice")),
    ]
}

/// 本体（と部品そろえ）に置き場を渡す。利用者が環境変数で指定していればそちらが勝つ。
fn set_parts_env(cmd: &mut Command) {
    for (k, v) in parts_env() {
        if crate::envcompat::var_os(k).is_none() {
            cmd.env(k, v);
        }
    }
    if let Some(d) = rea_dir() {
        cmd.env("LAPS_REA_DIR", d);
    }
}

/// 同梱の REA（逆解析の MCP サーバ・2026-11-03）の置き場＝インストール先の pyruntime\rea。
/// ★Python の写し（利用者名が ASCII でない環境）には REA を写さない（263MB・8,680 個）ので、
/// 本体にはインストール先のほうを教える（oc/mcp.py の _rea_dir が LAPS_REA_DIR を最初に見る）。
pub fn rea_dir() -> Option<PathBuf> {
    if crate::envcompat::var_os("LAPS_REA_DIR").is_some() {
        return None; // 利用者が自分で指したもの（親から引き継がれる）を上書きしない
    }
    install_pyruntime_dir().map(|d| d.join("rea")).filter(|d| d.is_dir())
}

/// 起動のたびに裏で部品をそろえる（`python -m laps.oc.parts`）。そろっていれば数秒で終わる。
/// インストール・更新の後の最初の起動で、Listen（whisper.cpp と モデル 約 1.5GB）・ffmpeg（約 0.11GB）の取り寄せと
/// Desktop の補助プログラムの組み立てが一度だけ走る。失敗しても困らない（使うときにもう一度そろえに行く）。
/// ★起動の邪魔をしないよう 20 秒待ってから・優先度を下げて走らせる。記録は <データ根>\parts.log。
pub fn prepare_parts_in_background() {
    if crate::envcompat::var("LAPS_PREPARE_PARTS").map(|v| v.trim() == "0").unwrap_or(false) {
        return;
    }
    std::thread::spawn(|| {
        std::thread::sleep(Duration::from_secs(20));
        let py = match resolve_python().and_then(|p| stage_runtime(&p)) {
            Ok(p) => p,
            Err(_) => return,
        };
        let root = crate::app_root();
        let _ = std::fs::create_dir_all(&root);
        let log_path = root.join("parts.log");
        let Ok(log) = std::fs::OpenOptions::new().create(true).append(true).open(&log_path) else { return };
        let Ok(log2) = log.try_clone() else { return };
        let mut cmd = std::process::Command::new(&py);
        cmd.arg("-u").arg("-m").arg("laps.oc.parts")
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUTF8", "1")
            .env_remove("PYTHONHOME")
            .env_remove("PYTHONSTARTUP")
            .current_dir(&root)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::from(log))
            .stderr(std::process::Stdio::from(log2));
        for (k, v) in parts_env() {
            if crate::envcompat::var_os(k).is_none() {
                cmd.env(k, v);
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // 窓を出さない ＋ BELOW_NORMAL_PRIORITY_CLASS（利用者の作業を優先する）
            cmd.creation_flags(0x0800_0000 | 0x0000_4000);
        }
        if let Ok(mut child) = cmd.spawn() {
            let _ = child.wait();
        }
    });
}

/// この機械をスマホから扱えるよう、中継（設定 `remote_relay`・未設定なら何もしない）へ名乗る係を裏で起こす（2026-10-08）。
/// オーナー「全てのデバイスにインストールされているセッションを Android から管理できるようにしたい」
/// 「コマンドを使わずともデフォルトで」。中身は `python -m laps.remotehost`（この機械の中だけに口を開け、
/// 中継へは**こちらから**繋ぐ）。走行の記録は画面と同じ置き場（<データ根>\agent）＝画面で始めた会話も一覧に出る。
/// ★切る: 設定 `remote_control = false`（または環境変数 `LAPS_REMOTE=0`）。中継の場所は `remote_relay`。
/// ★アプリが終わると 5 秒以内に自分も終わる（`LAPS_PARENT_PID`）。落ちたら 30 秒おいて起こし直す。
/// 記録は <データ根>\remote.log。
/// 名乗る係を起こし直すループが動いているか（整合性の確認から見る・2026-10-08）。
static REMOTE_LOOP: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

pub fn remote_loop_alive() -> bool {
    REMOTE_LOOP.load(std::sync::atomic::Ordering::Relaxed)
}

/// スマホからの遠隔操作（中継へ名乗る・操作の橋 remotectl.rs）を使うか。中継（remote_relay）を設定した人だけ使う。
/// 切るのは 環境変数 LAPS_REMOTE=0 か 設定 remote_control=false。★判定はここ 1 か所（2 か所に書かない）。
pub fn remote_control_enabled() -> bool {
    if crate::envcompat::var("LAPS_REMOTE").map(|v| v.trim() == "0").unwrap_or(false) {
        return false;
    }
    if crate::config_value("remote_control")
        .map(|v| matches!(v.to_ascii_lowercase().as_str(), "false" | "0" | "off" | "no"))
        .unwrap_or(false)
    {
        return false;
    }
    // ★中継の場所（remote_relay）を利用者が書くまでは使わない（既定の中継は無い）。
    crate::config_value("remote_relay").is_some()
}

pub fn start_remote_host_in_background() {
    if REMOTE_LOOP.swap(true, std::sync::atomic::Ordering::SeqCst) {
        return; // ★もう動いている（2 本にしない）
    }
    let stop_loop = || REMOTE_LOOP.store(false, std::sync::atomic::Ordering::SeqCst);
    if !remote_control_enabled() {
        stop_loop();
        return;
    }
    std::thread::spawn(move || {
        // ループを抜けるときは必ず印を下ろす（return が何か所もあるので、落とし物の無い形にする）。
        struct Down;
        impl Drop for Down {
            fn drop(&mut self) {
                REMOTE_LOOP.store(false, std::sync::atomic::Ordering::SeqCst);
            }
        }
        let _down = Down;
        std::thread::sleep(Duration::from_secs(8));
        loop {
            let py = match resolve_python().and_then(|p| stage_runtime(&p)) {
                Ok(p) => p,
                Err(_) => return,
            };
            let root = crate::app_root();
            let _ = std::fs::create_dir_all(&root);
            let Ok(log) = std::fs::OpenOptions::new().create(true).append(true).open(root.join("remote.log")) else {
                return;
            };
            let Ok(log2) = log.try_clone() else { return };
            // 推論機は画面と同じ行き先（いま有効なもの）を渡す。
            let backend = tauri::async_runtime::block_on(backend_url());
            let model = crate::lan_backend().map(|(_u, m)| m).unwrap_or_default();
            let mut cmd = std::process::Command::new(&py);
            cmd.arg("-u").arg("-m").arg("laps.remotehost")
                .env("LAPS_HOME", root.join("agent"))
                .env("LAPS_REMOTE_WSFILE", root.join("workspace.json"))
                .env("LAPS_REMOTE_KIND", "windows")
                .envs(rea_dir().map(|d| ("LAPS_REA_DIR", d)))
                .env("LAPS_PARENT_PID", std::process::id().to_string())
                .env("LAPS_APP_VERSION", env!("CARGO_PKG_VERSION"))
                .env("LAPS_BACKEND", &backend)
                .env("LAPS_POLICY", crate::policy_mode())
                .env("LAPS_ETHICS", crate::ethics_level())
                .env("LAPS_SSH_HOSTS", crate::config_value("ssh_hosts").unwrap_or_default())
                .env("PYTHONIOENCODING", "utf-8")
                .env("PYTHONUTF8", "1")
                .env("PYTHONUNBUFFERED", "1")
                .env_remove("PYTHONHOME")
                .env_remove("PYTHONSTARTUP")
                .current_dir(&root)
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::from(log))
                .stderr(std::process::Stdio::from(log2));
            if !model.trim().is_empty() {
                cmd.env("LAPS_MODEL", &model);
            }
            if let Some(relay) = crate::config_value("remote_relay") {
                cmd.env("LAPS_RELAY_URL", relay);
            }
            for (k, v) in parts_env() {
                if crate::envcompat::var_os(k).is_none() {
                    cmd.env(k, v);
                }
            }
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                cmd.creation_flags(0x0800_0000); // 窓を出さない
            }
            let started = std::time::Instant::now();
            let code = match cmd.spawn() {
                Ok(mut child) => child.wait().ok().and_then(|s| s.code()),
                Err(_) => None,
            };
            // 0 で終わった＝切られている（LAPS_REMOTE=0 等）。起こし直さない。
            // ★それ以外（3＝別の係が居た・落ちた）は 30 秒おいて起こし直す（2026-10-08）。
            //   旧: 「既に動いている」も 0 で返していたので、更新で入れ替わった直後に古い係が居残ると、
            //   新しい係は二度と起きず、古い係が終わった時点でどちらも居なくなった（スマホに 503）。
            if code == Some(0) {
                return;
            }
            let _ = started;
            std::thread::sleep(Duration::from_secs(30));
        }
    });
}

/// 入れ直し・更新の前に、同梱の Python で動いているもの（本体・名乗る係・整合性の確認）を止める（2026-10-08）。
/// ★残っていると python312.dll などを掴んだままになり、インストーラが途中で止まる（実機で、照合値の一覧だけ新しく
///   変わったファイルは古いまま、になった）。インストーラの前処理（nsis/hooks.nsh）でも同じ止め方をする（二重の備え）。
/// ★名前で撃たない。インストール先と写しの置き場の pyruntime から動いているものだけ。
pub fn stop_bundled_python() {
    shutdown();
    #[cfg(windows)]
    {
        let mut dirs: Vec<String> = Vec::new();
        if let Some(d) = install_pyruntime_dir() {
            dirs.push(d.display().to_string());
        }
        dirs.push(crate::app_root().join("pyruntime").display().to_string());
        let cond = dirs.iter()
            // ★-like は [ ] を文字の組として読む（フォルダ名に角括弧があると当たらない）。前方一致で比べる（2026-11-16 全体のレビュー）。
            .map(|d| format!("$_.Path.StartsWith('{}\\', [System.StringComparison]::OrdinalIgnoreCase)", d.replace('\'', "''")))
            .collect::<Vec<_>>()
            .join(" -or ");
        let script = format!(
            "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -and ({cond}) }} | Stop-Process -Force");
        let mut cmd = std::process::Command::new("powershell");
        cmd.args(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", &script]);
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x0800_0000);
        }
        let _ = cmd.status();
        std::thread::sleep(Duration::from_millis(1500));
    }
}

/// インストール先の同梱 Python の置き場（整合性の確認から・2026-10-08）。
pub fn install_pyruntime_dir() -> Option<PathBuf> {
    resolve_python().ok().and_then(|p| p.parent().map(|d| d.to_path_buf()))
}

/// 同梱の Python で `python -u -m <module> <args>` を走らせる（整合性の確認・直す）。(終了コード, 標準出力, 標準エラー)。
/// 本体（serve）と同じ置き場の環境（部品の置き場・データ根の agent）を渡す。窓は出さない。
pub async fn run_python_module(module: &str, args: &[&str], extra_env: &[(&str, String)], timeout_secs: u64)
    -> Result<(i32, String, String), String> {
    run_python_module_input(module, args, extra_env, timeout_secs, None).await
}

/// 標準入力に `input` を渡して走らせる（パスワードを引数や環境変数に出さない・2026-10-31）。
pub async fn run_python_module_input(module: &str, args: &[&str], extra_env: &[(&str, String)], timeout_secs: u64,
                                     input: Option<&str>)
    -> Result<(i32, String, String), String> {
    let py = stage_runtime(&resolve_python()?)?;
    let mut cmd = Command::new(&py);
    cmd.arg("-u").arg("-m").arg(module);
    for a in args {
        cmd.arg(a);
    }
    cmd.current_dir(crate::app_root())
        .env("LAPS_HOME", crate::app_root().join("agent"))
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONSTARTUP")
        .stdin(if input.is_some() { std::process::Stdio::piped() } else { std::process::Stdio::null() })
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    set_parts_env(&mut cmd);
    for (k, v) in extra_env {
        cmd.env(k, v);
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let mut child = cmd.spawn().map_err(|e| format!("Python を起動できません: {e}"))?;
    if let Some(text) = input {
        use tokio::io::AsyncWriteExt;
        if let Some(mut si) = child.stdin.take() {
            si.write_all(text.as_bytes()).await.map_err(|e| format!("Python へ渡せません: {e}"))?;
            // ここで閉じる（読み手は 1 行読んで終わる）
        }
    }
    let out = tokio::time::timeout(Duration::from_secs(timeout_secs), child.wait_with_output())
        .await
        .map_err(|_| format!("{timeout_secs} 秒で終わりませんでした"))?
        .map_err(|e| format!("Python の終わりを待てません: {e}"))?;
    Ok((out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned()))
}

/// 本体と同じ Python（同梱の物・要るときだけ写しを作る）。走行記録の濾し器（journal.rs）が使う。
/// ★journal.rs は自分で探していて、アプリの置き場の古い写し（pyruntime）を先に拾っていた。写しは入れ先が ASCII なら作り直されないので、
///   改名（0.1.672）の後は `laps` の無い古い Python で濾し器が落ち続け、走行記録が 1 件も届かなかった（2026-11-14 実機）。
pub fn runtime_python() -> Result<PathBuf, String> {
    stage_runtime(&resolve_python()?)
}

fn stage_runtime(src_exe: &Path) -> Result<PathBuf, String> {
    // ★同時に 2 本が複製しないよう 1 本ずつ（起動直後の部品そろえと本体の起動が重なりうる・2026-10-07）。
    static STAGE: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _one = STAGE.lock().unwrap_or_else(|e| e.into_inner());
    let src_dir = src_exe.parent().ok_or("Python の場所を特定できません")?;
    if src_dir.to_string_lossy().is_ascii() {
        return Ok(src_exe.to_path_buf()); // 元が ASCII なら複製しない
    }
    let dst_dir = crate::app_root().join("pyruntime");
    let stamp = dst_dir.join(".staged");
    let want = tree_stats(src_dir, Some(STAGE_SKIP));
    // ★版も目印に入れる（2026-11-16 全体のレビュー）。前は数と合計の大きさだけ＝大きさの変わらない直し（数字の書き換えなど）が
    //   更新で写されず、日本語の利用者名の機械（オーナーの実機）で古い本体の Python が動き続けることがあった。
    let want_s = format!("files={} bytes={} app={}", want.0, want.1, env!("CARGO_PKG_VERSION"));

    if std::fs::read_to_string(&stamp).map(|s| s.trim() == want_s).unwrap_or(false) {
        let staged = dst_dir.join(PY_BIN);
        if staged.is_file() {
            return Ok(staged);
        }
    }

    // 途中で落ちた複製を「完了」と誤認しないよう、目印は最後に書く。
    let _ = std::fs::remove_file(&stamp);
    copy_tree(src_dir, &dst_dir, Some(STAGE_SKIP))
        .map_err(|e| format!("{} へ複製できません: {e}", dst_dir.display()))?;
    let staged = dst_dir.join(PY_BIN);
    if !staged.is_file() {
        return Err(format!(
            "Python 一式を {} へ複製できませんでした。\n\
             利用者名に ASCII 以外の文字が含まれる環境では、この複製が要ります。",
            dst_dir.display()
        ));
    }
    let _ = std::fs::write(&stamp, want_s);
    Ok(staged)
}

/// 写しに入れない直下のフォルダ。REA（node.exe と node_modules・263MB）は ASCII の場所を要らないので、
/// インストール先のものをそのまま使う（rea_dir）。写すと初回の起動が数百 MB の複製で止まる（2026-11-03）。
const STAGE_SKIP: &str = "rea";

/// 木のファイル数と合計バイト数。複製が済んでいるかの判定にだけ使う。`skip` は直下のフォルダ名だけを見る。
fn tree_stats(dir: &Path, skip: Option<&str>) -> (usize, u64) {
    let mut files = 0usize;
    let mut bytes = 0u64;
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        for e in rd.flatten() {
            let p = e.path();
            match e.file_type() {
                Ok(t) if t.is_dir() && d == dir && skip.is_some_and(|s| e.file_name() == s) => {}
                Ok(t) if t.is_dir() => stack.push(p),
                Ok(t) if t.is_file() => {
                    files += 1;
                    bytes += e.metadata().map(|m| m.len()).unwrap_or(0);
                }
                _ => {}
            }
        }
    }
    (files, bytes)
}

fn copy_tree(from: &Path, to: &Path, skip: Option<&str>) -> std::io::Result<()> {
    std::fs::create_dir_all(to)?;
    for e in std::fs::read_dir(from)? {
        let e = e?;
        let src = e.path();
        let dst = to.join(e.file_name());
        let ft = e.file_type()?;
        if ft.is_dir() && skip.is_some_and(|s| e.file_name() == s) {
            continue; // 直下の skip だけ（下の階層には渡さない）
        }
        if ft.is_dir() {
            copy_tree(&src, &dst, None)?;
        } else if ft.is_file() {
            // 同じ大きさで、更新の時刻も同じなら飛ばす（更新のたびに 32MB 全部は写さない）。
            // ★時刻も見る（2026-11-16）: 大きさだけだと、大きさの変わらない直しを写さなかった。Windows の複製は時刻を引き継ぐので、
            //   写した後は同じになる。
            let same = std::fs::metadata(&dst)
                .ok()
                .zip(std::fs::metadata(&src).ok())
                .map(|(a, b)| a.len() == b.len() && a.modified().ok() == b.modified().ok())
                .unwrap_or(false);
            if !same {
                std::fs::copy(&src, &dst)?;
            }
        }
        // シンボリックリンクは写さない（配布物には無い。辿ると木の外へ出る）。
    }
    Ok(())
}

/// ログの末尾。起動に失敗したとき「何が起きたか」を人へ返すため。
/// （llama.rs に同じ関数がある。あちらは private なので写している。）
fn log_tail(path: &Path, max: usize) -> String {
    use std::io::{Read, Seek};
    let Ok(mut f) = std::fs::File::open(path) else { return String::new() };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let start = len.saturating_sub(max as u64);
    if start > 0 && f.seek(std::io::SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut buf = Vec::new();
    if f.read_to_end(&mut buf).is_err() {
        return String::new();
    }
    String::from_utf8_lossy(&buf).trim().to_string()
}

// ---------------------------------------------------------------------------
// 子プロセス
// ---------------------------------------------------------------------------

/// この鍵が変わったら子を作り直す。
/// ★cwd は外から変えられない。作業ディレクトリが変わったのに使い回すと、
///   **前のフォルダに書く**という最悪の壊れ方をする（それを chdir 頼みにしない）。
/// ★推論器のポートも入れる。llama.rs はポートが塞がっていると上へずらすので、
///   再起動後に古いポートへ話し続けるのを防ぐ。
/// URL の userinfo（user:pass@）を伏せる。record は telemetry として .9 へ送られるので、値を残さない。
fn redact_userinfo(url: &str) -> String {
    match url.find('@') {
        Some(at) if url[..at].contains("://") => {
            let scheme_end = url.find("://").map(|i| i + 3).unwrap_or(0);
            format!("{}***@{}", &url[..scheme_end], &url[at + 1..])
        }
        _ => url.to_string(),
    }
}

#[derive(Clone, PartialEq, Eq, Debug)]


struct SpawnKey {
    workspace: PathBuf,
    backend: String,
    /// ★ほかの機械の本体へ中継越しにつなぐとき "<機械の名札>/<線の名札>"（2026-10-30・blackwall/relaybridge）。空ならこの PC の serve。
    remote: String,
}

struct Io {
    stdout: ChildStdout,
    reader: LineReader,
}

struct Sidecar {
    /// 子プロセス本体。**停止は同期関数から行う**ので std の Mutex に置く。
    child: Mutex<Option<Child>>,
    /// 入出力（読み側）。await をまたいで保持するので tokio の Mutex。
    /// これを握っている間が「1回の実行」＝同時に2つ走らない。
    io: AsyncMutex<Option<Io>>,
    /// ★書き側（stdin）は**別のロック**に置く。
    ///   読み側と同じロックに入れていたため、走行中は `io` を握りっぱなしになり、
    ///   `inject()` の try_lock が**必ず失敗**した＝**割り込みが原理的に届かなかった**。
    ///   画面は「Inject」と表示しながら、押すと走行を捨てる（停止）動きをしていた
    ///   （2026-08-18 の UI 敵対レビュー）。
    ///   割り込みは「作業を捨てずに方向を変える」ための道で、エージェントの画面が
    ///   普通のチャットと違う点として設計メモが名指ししている機能。
    stdin: AsyncMutex<Option<ChildStdin>>,
    key: Mutex<Option<SpawnKey>>,
    log: Mutex<Option<PathBuf>>,
    /// 直近の生存確認（時刻と結果）。
    avail: Mutex<Option<(Instant, Result<(), String>)>>,
    /// 握手の行が運んできた道具一覧。
    tools: Mutex<Option<Vec<String>>>,
    seq: AtomicU64,
}

impl Sidecar {
    fn new() -> Self {
        Sidecar {
            child: Mutex::new(None),
            io: AsyncMutex::new(None),
            stdin: AsyncMutex::new(None),
            key: Mutex::new(None),
            log: Mutex::new(None),
            avail: Mutex::new(None),
            tools: Mutex::new(None),
            seq: AtomicU64::new(0),
        }
    }
}

/// ★走行の器（レーン・2026-10-30）。オーナー「api を使っている間は別チャットで gpu の推論に切り替えて使えるように」
///   ＋「モデルがタスク完了待ちしている間は…別タスクを進められるように」。本体（serve）は 1 つの子で 1 走行ずつなので、
///   会話ごとに同時に走らせるには子を分ける。器 0 は今までの 1 本（札の無い呼び手・生存確認・道具の一覧もここ）。
///   札（会話）つきの依頼は空いている器を使う（同じ会話が前に使った器を先に見る＝温まった子を使い回す）。
const LANES: usize = 3;

fn slots() -> &'static [Sidecar; LANES] {
    static S: OnceLock<[Sidecar; LANES]> = OnceLock::new();
    S.get_or_init(|| std::array::from_fn(|_| Sidecar::new()))
}

fn sc() -> &'static Sidecar {
    &slots()[0]
}

/// いまどの会話がどの器で走っているか（札の無い走行は None のまま）。
static OWNERS: Mutex<[Option<String>; LANES]> = Mutex::new([None, None, None]);
/// 会話が最後に使った器（次も同じ器を先に試す）。
static LAST_SLOT: Mutex<Vec<(String, usize)>> = Mutex::new(Vec::new());

/// 札の走行が使っている器。札が無ければ器 0。★札があるのに走っていなければ None
///   （止めた後の割り込み・答えを、別の会話の走行へ書かない）。
fn lane_slot(lane: Option<&str>) -> Option<&'static Sidecar> {
    match lane.map(str::trim).filter(|l| !l.is_empty()) {
        None => Some(sc()),
        Some(l) => lock(&OWNERS).iter().position(|o| o.as_deref() == Some(l)).map(|i| &slots()[i]),
    }
}

/// 走っている札の一覧（画面の確かめ用）。
pub fn lanes_running() -> Vec<String> {
    lock(&OWNERS).iter().flatten().cloned().collect()
}

/// 走行が終わったら器の持ち主を外す（どの抜け道でも）。
struct OwnerMark(usize);
impl Drop for OwnerMark {
    fn drop(&mut self) {
        lock(&OWNERS)[self.0] = None;
    }
}

/// 器 1・2 の子は、使われないまま 10 分たったら落とす（Python 1 本ぶんの RAM を返す）。器 0 は今までどおり残す。
fn reap_later(i: usize) {
    if i == 0 {
        return;
    }
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(600)).await;
        let s = &slots()[i];
        if lock(&OWNERS)[i].is_some() {
            return;
        }
        if let Ok(mut g) = s.io.try_lock() {
            *g = None;
            *s.stdin.lock().await = None;
            kill_child(s);
        }
    });
}

/// ロックの毒（他スレッドの panic）で機能停止させない（llama.rs と同じ理由）。
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn child_alive(s: &Sidecar) -> bool {
    let mut g = lock(&s.child);
    let alive = match g.as_mut() {
        Some(c) => matches!(c.try_wait(), Ok(None)),
        None => false,
    };
    if !alive {
        *g = None;
    }
    alive
}

/// 子を確実に終わらせる。**理由を問わず、生かしたまま忘れない。**
fn kill_child(s: &Sidecar) {
    let child = {
        let mut g = lock(&s.child);
        g.take()
    };
    if let Some(mut c) = child {
        let _ = c.start_kill();
        // 刈り取りは非同期。ランタイムがあれば任せ、無ければ諦める
        //（Windows ではゾンビは残らない。Drop の kill_on_drop も効く）。
        if let Ok(h) = tokio::runtime::Handle::try_current() {
            h.spawn(async move {
                let _ = c.wait().await;
            });
        }
    }
    *lock(&s.key) = None;
}

/// 子に渡す環境を組み立てる。
///
/// ★`LAPS_BACKEND` を必ず明示する。Python 側の既定は
///   別ホストの推論サーバーなので、渡し忘れると
///   手元の GPU を起こしたのに黙って別のマシンへ話しに行く。実機で必ず踏む種類の食い違い。
/// 画面に「いまどこで推論しているか」を出すための情報。
/// ★これが無いと、遠隔で動いているのに手元で動いている顔をした画面になる
///   ——記録と画面が食い違うと、直す対象そのものを取り違える（#11 で学んだ型）。
#[derive(serde::Serialize)]
pub struct BackendInfo {
    /// true = LAN の推論機（手元の llama-server は起動しない）
    pub remote: bool,
    pub url: String,
    pub model: String,
    /// 実際に本体（serve）へ渡した `LAPS_BACKEND` の**そのままの値**。
    /// ★画面はここから宛先を出す。設定から作り直すと、また食い違う
    ///   （設定から作り直して宛先を表示すると、実際の宛先とずれた前例がある）。
    pub effective: String,
    /// なぜその宛先になったか（機械可読）。`backend_choice::Source::code()`。
    pub source: String,
    /// 同じものを日本語で。画面にそのまま出す。
    pub source_label: String,
    /// この機械で推論できる見込みがあるか（gguf と推論器の実体の有無）。
    pub local_ok: bool,
    /// ローカルの推論機のポート（`local_ok` が false でも、いま見ているポートを出す）。
    pub local_port: u16,
}

/// LAN の推論機に**実際に届くか**を測る。設定を読むだけの `backend_info` と分ける。
///
/// ★「設定が有効」を「生きている」と読ませない。旧版は設定が true というだけで
///   ランプを緑にしていた——`backend_url()` 上のコメントが名指しで警告している
///   「遠隔で動いているのに手元で動いている顔をした画面」の逆向きそのもの。
/// ★**届くこと**と**設定したモデルが居ること**は別（2026-08-19 追加）。
///   旧版は `/v1/models` が 200 なら緑にしていた。だが推論機は入れ替わる——
///   別のモデルを載せて起動し直しても 200 は返るので、**設定に書いた名前とは
///   違うモデルが黙って答える**。記録には既にその実例がある
///   （「前の起動の残骸が居て、画面は LAN を表示したまま別のモデルが答えた」）。
///   名前まで照合しないと、この画面は「繋がっている」を保証しない。
#[tauri::command]
pub async fn backend_probe() -> bool {
    // ★測るのは**実際に使う宛先**（2026-09-06）。設定から作り直すと、
    //   auto で LAN になった回を「off」と読んでしまう。
    let dest = resolve_backend().await;
    if !dest.remote || dest.root.is_empty() {
        return false;
    }
    let url = format!("{}/v1/models", dest.root.trim_end_matches('/'));
    let cli = match reqwest::Client::builder()
        // ★OS の HTTP(S)_PROXY を使わない（2026-09-06）。推論機を叩く経路は
        //   agent.rs・llama.rs・serve.py（0.1.195）とも no_proxy なのに、
        //   **ここだけが素通しだった**。系にプロキシが刺さっていると、
        //   画面のランプだけが「別のもの」を測ることになり、
        //   「設定は緑なのに送れない」という最も紛らわしい形になる。
        .no_proxy()
        .connect_timeout(std::time::Duration::from_secs(3))
        .timeout(std::time::Duration::from_secs(5))
        .build()
    {
        Ok(c) => c,
        Err(_) => return false,
    };
    let Ok(resp) = cli.get(&url).send().await else { return false };
    if !resp.status().is_success() {
        return false;
    }
    // 設定に名前が無いなら、届いた時点で良しとする（従来どおり）。
    let want = dest.model.trim().to_string();
    if want.is_empty() {
        return true;
    }
    let Ok(body) = resp.text().await else { return false };
    // llama-server は `-a` の文字列を、指定が無ければモデルのパスを返す。
    // どちらでも拾えるよう、**含まれるか**で見る。
    body.contains(&want)
}

/// 画面で編集するための LAN 設定。**無効のときも値を返す**。
///
/// ★`lan_backend()` は「無効なら None」を返す作りなので、それだけでは
///   画面に住所やモデル名を出せない——利用者は**有効にする前に**
///   宛先を入れたい。編集用の読み出しは別に用意する。
#[derive(serde::Serialize, serde::Deserialize, Clone, Debug, Default)]
pub struct LanSettings {
    /// `use_lan_backend = true`（LAN 固定）のときだけ true。
    /// ★`mode` が "auto" のときも false になる——画面のチェックは
    ///   「**固定する**」の意味で、いま LAN で動いているかとは別。
    pub enabled: bool,
    pub url: String,
    pub model: String,
    /// "on" | "off" | "auto"。3 値をそのまま画面へ渡す（2026-09-06）。
    pub mode: String,
}

fn read_lan_settings() -> LanSettings {
    // ★読み方を2つ持たない。行の読み方が本体（`backend_choice::parse_config`）と
    //   1文字でもずれると、「画面には正しく見えるのに宛先が違う」が生まれる。
    let c = crate::lan_config();
    LanSettings {
        enabled: c.choice == Some(crate::backend_choice::LanChoice::Lan),
        url: c.url,
        model: c.model,
        mode: match c.choice {
            Some(crate::backend_choice::LanChoice::Lan) => "on",
            Some(crate::backend_choice::LanChoice::Local) => "off",
            _ => "auto",
        }
        .to_string(),
    }
}

#[tauri::command]
pub fn lan_settings_get() -> LanSettings {
    read_lan_settings()
}

/// 画面から LAN の推論機を設定する。
///
/// ★**書き換えるのは該当の3行だけ。** 設定ファイルには説明のコメントが
///   たくさん入っていて、それが「アプリが無くても中身が分かるように」という
///   設計意図そのもの。丸ごと書き直すと、その意図を消してしまう。
///   行単位で置き換え、無い鍵は説明つきで末尾に足す。
#[tauri::command]
pub fn lan_settings_set(enabled: bool, url: String, model: String)
    -> Result<LanSettings, String>
{
    let url = url.trim().trim_end_matches('/').to_string();
    let model = model.trim().to_string();

    // ★有効にするときだけ厳しく見る。無効なら空でも保存できてよい
    //   （宛先を消してから無効にする、という順序を塞がない）。
    if enabled {
        if !(url.starts_with("http://") || url.starts_with("https://")) {
            return Err("宛先は http:// か https:// で始めてください".into());
        }
        if url.split("://").nth(1).map(|h| h.is_empty()).unwrap_or(true) {
            return Err("宛先にホスト名がありません".into());
        }
    }
    // 設定ファイルは行単位で読む作りなので、値に引用符や改行が入ると壊れる。
    for (name, v) in [("宛先", &url), ("モデル名", &model)] {
        if v.contains('"') || v.contains('\n') || v.contains('#') {
            return Err(format!("{name}に使えない文字（\" 改行 #）が入っています"));
        }
    }

    let path = crate::config_path();
    let body = std::fs::read_to_string(&path).unwrap_or_default();
    let want = [
        ("use_lan_backend", if enabled { "true".to_string() } else { "false".to_string() }),
        ("lan_backend_url", format!("\"{url}\"")),
        ("lan_model", format!("\"{model}\"")),
        // ★**利用者が画面で選んだ**印（2026-09-06）。
        //   これが無い `false` は「アプリが既定として書いた値」であって
        //   利用者の意思ではない。起動時の移行（`migrate_machine_default_false`）は
        //   印の無い false だけを `"auto"` へ移す。ここで印を付けておけば、
        //   意図して切った OFF は二度と触られない。
        (crate::backend_choice::PIN_KEY, "true".to_string()),
    ];

    let mut out: Vec<String> = Vec::new();
    let mut seen: Vec<&str> = Vec::new();
    for line in body.lines() {
        let bare = line.split('#').next().unwrap_or("").trim();
        let key = bare.split_once('=').map(|(k, _)| k.trim()).unwrap_or("");
        if let Some((k, v)) = want.iter().find(|(k, _)| *k == key) {
            out.push(format!("{k} = {v}"));
            seen.push(k);
            continue;
        }
        out.push(line.to_string());
    }
    for (k, v) in &want {
        if !seen.contains(k) {
            out.push(String::new());
            out.push(format!("{k} = {v}"));
        }
    }
    let mut text = out.join("\n");
    if !text.ends_with('\n') {
        text.push('\n');
    }
    std::fs::write(&path, text).map_err(|e| format!("設定を保存できません: {e}"))?;
    Ok(read_lan_settings())
}

/// 実際に使う宛先。**ここが唯一の合流点**——画面も serve も同じ値を見る。
///
/// ★`crate::backend_decision()`（設定＋この機械で推論できるか）に、
///   ローカルのポート（`llama.rs` が塞がっていれば上へずらす）を足して
///   `LAPS_BACKEND` の文字列まで作る。
pub struct Resolved {
    pub remote: bool,
    /// LAN の根（`/v1` なし）。ローカルなら空。
    pub root: String,
    /// `LAPS_BACKEND` に渡す値そのもの。
    pub effective: String,
    pub source: String,
    pub source_label: String,
    pub local_port: u16,
    pub local_ok: bool,
    pub model: String,
}

pub async fn resolve_backend() -> Resolved {
    // ★2026-09-06 オーナー決定「推論機はサーバー側で固定」: 宛先の解決で**手元の推論機を叩かない**。
    //   旧: 冒頭で無条件に llama_status()（127.0.0.1:8760 へ接続）を呼んでいた＝LAN 固定でも
    //   走行のたびに loopback へ SYN（Windows は拒否に 2 秒×複数）＝実機で 27 秒の無駄（ClaudeCode 実測）。
    //   ローカルを選んだ（明示 false）ときだけ port を取りに行く。
    let d = crate::backend_decision();
    let local_port = if d.remote {
        8760
    } else {
        crate::llama::llama_status().await.map(|s| s.port).unwrap_or(8760)
    };

    // ★環境変数の指定が最優先。開発と診断のための逃げ道で、設定より強い。
    if let Ok(u) = crate::envcompat::var("LAPS_SIDECAR_BACKEND") {
        let u = u.trim().to_string();
        if !u.is_empty() {
            let local = u.contains("127.0.0.1") || u.contains("localhost") || u.contains("[::1]");
            return Resolved {
                remote: !local,
                root: u.trim_end_matches("/v1").trim_end_matches('/').to_string(),
                effective: u,
                source: crate::backend_choice::Source::Env.code().to_string(),
                source_label: crate::backend_choice::Source::Env.label().to_string(),
                local_port,
                local_ok: if local { crate::local_engine_usable() } else { false },
                model: crate::lan_config().model,
            };
        }
    }

    Resolved {
        remote: d.remote,
        root: d.root.clone(),
        effective: crate::backend_choice::backend_env(&d, local_port),
        source: d.source.code().to_string(),
        source_label: d.source.label().to_string(),
        local_port,
        local_ok: if d.remote { false } else { crate::local_engine_usable() },
        model: crate::lan_config().model,
    }
}

/// 画面へ出す宛先。★**設定ではなく、実際に渡した値**を返す（2026-09-06）。
///
/// ★旧実装は設定だけを読んでいたので、「設定は LAN なのに serve はローカルへ
///   行っていた」型の食い違いを画面から見つけられなかった——2026-09-06 の
///   実機障害（手元の機械が 45 秒待って失敗し、サーバーには1件も届いていない）が
///   まさにそれで、原因の切り分けに丸1日かかった。
#[tauri::command]
pub async fn backend_info() -> BackendInfo {
    let r = resolve_backend().await;
    BackendInfo {
        remote: r.remote,
        // 従来の意味を保つ（遠隔のときだけ住所、ローカルは空）。画面の既存の分岐が読む。
        url: if r.remote { r.effective.clone() } else { String::new() },
        model: if r.remote { r.model.clone() } else { String::new() },
        effective: r.effective,
        source: r.source,
        source_label: r.source_label,
        local_ok: r.local_ok,
        local_port: r.local_port,
    }
}

async fn backend_url() -> String {
    resolve_backend().await.effective
}

/// 会話ごとの宛先（2026-10-30 オーナー「会話ごとにグレード含めモデルか API か切り替えれるように」）。画面は種類だけを渡し、
/// 住所はここで作る（画面から住所を受け取らない＝思わぬ宛先へ送らない）。知らない種類は None（いつもの宛先）。
///   "lan" … 別の推論サーバー（設定の住所。未設定なら既定の 127.0.0.1:8080）
///   "local" … この PC の推論機
pub async fn backend_for(kind: &str) -> Option<String> {
    const DEFAULT_SERVER: &str = "http://127.0.0.1:8080/v1";
    match kind {
        "local" => {
            let port = crate::llama::llama_status().await.map(|s| s.port).unwrap_or(8760);
            Some(format!("http://127.0.0.1:{port}/v1"))
        }
        "lan" => {
            let u = read_lan_settings().url.trim().trim_end_matches('/').trim_end_matches("/v1").to_string();
            if u.is_empty() || !(u.starts_with("http://") || u.starts_with("https://")) {
                Some(DEFAULT_SERVER.to_string())
            } else {
                Some(format!("{u}/v1"))
            }
        }
        _ => None,
    }
}

/// ローカルの推論機が**いま答えられるか**を短く測る。駄目なら利用者への文を返す。
///
/// ★なぜ要るか: ローカルへ向いているのに推論機が上がっていないと、本体は
///   45 秒（`chat-heartbeat` の刻み）黙ってから失敗する。実機の報告は
///   「推論機の応答を待っています（N秒）→ エラー／後に何も出ない」で、
///   **どこへ送っているのかも、なぜ駄目なのかも画面に出ていなかった**。
///   3 秒で断ってしまえば、待たせずに次の手（設定を LAN にする）を示せる。
///
/// ★LAN 側は測らない。届かないことは `backend_probe`（RIG 画面）が測っており、
///   送信のたびに他所のマシンを叩くのは礼儀に反する。
async fn local_health_block(port: u16) -> Option<String> {
    let cli = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(std::time::Duration::from_secs(2))
        .timeout(std::time::Duration::from_secs(3))
        .build()
        .ok()?;
    for path in ["/health", "/v1/models"] {
        let url = format!("http://127.0.0.1:{port}{path}");
        if let Ok(resp) = cli.get(&url).send().await {
            // 404 は「その口が無い」だけ＝サーバは生きている。次の口を試す。
            if resp.status().is_success() {
                return None;
            }
        }
    }
    Some(format!(
        "ローカルの推論機（http://127.0.0.1:{port}/v1）が応答しません。\n\
         ・この機械で動かすなら、モデル画面から推論機を起動してください。\n\
         ・LAN の推論機を使うなら、設定の「この機械ではなく LAN の推論機を使う」を ON にしてください。"
    ))
}

fn idle_timeout() -> Duration {
    crate::envcompat::var("LAPS_SIDECAR_IDLE_SECS")
        .ok()
        .and_then(|s| s.trim().parse::<u64>().ok())
        .filter(|n| *n > 0)
        .map(Duration::from_secs)
        .unwrap_or(IDLE_TIMEOUT_DEFAULT)
}

/// 起動の共通部分。`extra` は追加の引数（生存確認の `--probe` を渡す）。
/// ほかの機械の本体へ中継越しにつなぐ橋（2026-10-30・laps/relaybridge.py）。子は serve と同じ 1 行ずつの形を流すので、
/// 握手（ready）も走行の読み方も、この PC の serve の子と同じ（run_loop をそのまま使う）。
fn bridge_command(py: &Path, key: &SpawnKey) -> Command {
    let relay = crate::config_value("remote_relay").unwrap_or_default();
    let (device, line) = key.remote.split_once('/').unwrap_or((key.remote.as_str(), "app"));
    let mut cmd = Command::new(py);
    cmd.arg("-u").arg("-m").arg("laps.relaybridge")
        .arg("--relay").arg(relay).arg("--device").arg(device);
    match line.strip_prefix("follow:").and_then(|x| x.split_once(':')) {
        Some((sid, as_id)) => { cmd.arg("--follow").arg(sid).arg("--as-id").arg(as_id); }
        None => { cmd.arg("--session").arg(line); }
    }
    cmd.current_dir(crate::app_root())
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONSTARTUP");
    set_parts_env(&mut cmd);
    match crate::envcompat::var("LAPS_PYAPP") {
        Ok(p) if !p.trim().is_empty() => {
            cmd.env("PYTHONPATH", p);
        }
        _ => {
            cmd.env_remove("PYTHONPATH");
        }
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}

fn build_command(py: &Path, cwd: &Path, key: &SpawnKey, model: &str, extra: &[&str]) -> Command {
    if !key.remote.is_empty() {
        return bridge_command(py, key);
    }
    let mut cmd = Command::new(py);
    // -u … 行が届かないまま溜まるのを防ぐ（対話的な出力ではないので Python はまとめ買いする）
    cmd.arg("-u").arg("-m").arg(PY_MODULE);
    for a in extra {
        cmd.arg(a);
    }
    cmd.current_dir(cwd)
        .env("LAPS_BACKEND", &key.backend)
        // ★走行の記録に「アプリで走った」と残す（2026-11-01）。ゲートウェイで走った物（"gateway"）を画面が見分けて実況で追う。
        .env("LAPS_RUN_VIA", "app")
        // セッション記録は作業ディレクトリにも利用者プロファイルにも置かない
        // （前者は作業の邪魔、後者は非 ASCII になりうる）。
        .env("LAPS_HOME", crate::app_root().join("agent"))
        // ★実行の境界。既定の readonly のままだと本体は書けず走らせられない＝
        //   道具を渡しても何もできない。workspace は「作業ディレクトリの中でだけ書ける」
        //   で、許可リストに無いプログラム（del/format/powershell 等）は拒否される。
        //   **off にしない**（off は境界を外す設定で、実機で使う理由が無い）。
        // ★実行の権限は config.toml で決める（既定 trusted＝何も止めない）。
        //   2026-08-18 オーナー指示。それまでは workspace 固定で、pip/curl/powershell/
        //   docker が全モードで禁止＝**使えない道具**になっていた。
        //   戻したいときは config.toml の policy を1行変えるだけ。
        .env("LAPS_POLICY", crate::policy_mode())
        // ★倫理・慎重さの刻み（2026-08-19）。policy とは**別の軸**。
        //   policy = 何を実行してよいか（本物の境界・policy.py が強制）
        //   ethics = どれだけ慎重に振る舞うか（system prompt の一部・宣言にすぎない）
        //   混ぜると「raw にしたら何でも出来るようになった」という誤解を生むので分けてある。
        .env("LAPS_ETHICS", crate::ethics_level())
        // ★時間と道具呼び出しの予算（2026-08-22）。既定 0 ＝ 無制限。
        //   反復だけ外すと、次はこの2つが天井になる（実測で道具80回・600秒）。
        //   本体は「0 以下なら無制限」の作り。
        .env("LAPS_MAX_SECONDS", crate::budget_value("max_seconds").to_string())
        .env("LAPS_MAX_TOOL_CALLS", crate::budget_value("max_tool_calls").to_string())
        // ★ssh の接続先。**空なら ssh は全拒否**（policy 側の既定）。
        //   ラボの作業はほぼ SSH 越しなので使えないと運用の道具にならないが、
        //   「どこへ行ってよいか」は利用者が書く。許可した先でも、遠隔で走らせる
        //   コマンドには手元と同じゲートが当たる（policy._ssh_verdict）。
        .env(
            "LAPS_SSH_HOSTS",
            crate::config_value("ssh_hosts").unwrap_or_default(),
        )
        .env("LAPS_WORKSPACE", &key.workspace)
        // ★Windows の既定コードページ(cp932)だと、日本語を含む出力で UnicodeEncodeError を出して
        //   途中で死ぬ。ここは必ず UTF-8 に固定する。
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1")
        // 利用者の環境に残っている値で同梱ランタイムが壊れるのを防ぐ
        //（埋め込み配布は `python312._pth` で解決するので、これらは邪魔にしかならない）。
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONSTARTUP");
    set_parts_env(&mut cmd);

    // モデルは既定値としてだけ渡す。実際に使うのは依頼の1行に載せた `model`
    //（子は使い回すので、環境変数は最初の1回で固定されてしまう）。
    // ★空文字は渡さない。`os.environ.get("LAPS_MODEL", 既定)` は空文字を**値として返す**ので、
    //   生存確認（--probe・モデル不要）のために空を渡すと、Python 側でモデル名が空になる。
    // LAN の推論機を使う設定なら、そこで待っているモデル名を既定にする。
    // ★手元の GGUF 名（カタログ由来）をそのまま LAN へ送ると、相手は
    //   「そんなモデルは無い」と答える。既定を差し替えておけば、画面が
    //   モデル名を載せ忘れた経路でも正しい名前で届く。
    let lan_model = crate::lan_backend().map(|(_u, m)| m).unwrap_or_default();
    let effective = if !model.trim().is_empty() {
        model.to_string()
    } else {
        lan_model
    };
    if effective.trim().is_empty() {
        cmd.env_remove("LAPS_MODEL");
        cmd.env_remove("BLACKWALL_MODEL"); // 旧い名前も消す（片方だけ消すと Python の側で旧い名前から値が戻る）
    } else {
        cmd.env("LAPS_MODEL", &effective);
    }

    // 開発時だけ: 本体パッケージの場所を教える（配布物では ._pth が効くので使わない）。
    match crate::envcompat::var("LAPS_PYAPP") {
        Ok(p) if !p.trim().is_empty() => {
            cmd.env("PYTHONPATH", p);
        }
        _ => {
            cmd.env_remove("PYTHONPATH");
        }
    }

    // ★`LAPS_YES`（破壊的コマンドの確認を飛ばす）は**渡さない**。
    //   アプリ経由の実行だけが無確認になる穴を作らない。冒頭の注記を参照。

    // ★`creation_flags` は tokio::process::Command 自身が windows 向けに持っている
    //   （std の CommandExt を use すると「使っていない」と言われる。llama.rs も同じ形）。
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}

/// 子を起こして、`ready` を受け取るまで待つ。
async fn spawn_child(s: &Sidecar, key: &SpawnKey, model: &str) -> Result<Io, String> {
    let py = stage_runtime(&resolve_python()?)?;

    if !key.workspace.is_dir() {
        return Err(format!(
            "作業ディレクトリが見つかりません: {}",
            key.workspace.display()
        ));
    }

    let log_path = crate::app_root().join("sidecar.log");
    let _ = std::fs::create_dir_all(crate::app_root());
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("{} を開けません: {e}", log_path.display()))?;
    *lock(&s.log) = Some(log_path.clone());

    // ★**渡した宛先を必ず 1 行残す**（2026-09-06・owner 実機 P0）。
    //   実機で「推論機の応答を待っています（N 秒）」だけが続き、推論機側には要求が
    //   1 件も届いていなかった。そのとき `sidecar.log` から読み取れたのは Python の
    //   出力だけで、**アプリがどの URL を渡したのか**はどこにも残っていなかった
    //   （LAN 設定が効いているのか、手元の 127.0.0.1 に向いたままなのかを、
    //   利用者の画面からも記録からも区別できない）。子が 1 文字も喋れずに死んでも、
    //   この行だけは残る。
    {
        use std::io::Write as _;
        let _ = writeln!(
            &log,
            "[sidecar] 起動: backend={} model={} workspace={} policy={} python={}",
            key.backend,
            model,
            key.workspace.display(),
            crate::policy_mode(),
            py.display()
        );
    }

    let mut cmd = build_command(&py, &key.workspace, key, model, &[]);
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        // ★stderr はファイルへ。パイプにして誰も読まないと、埋まった時点で子が
        //   書き込みで止まる（llama.rs で同じ理由からファイルに逃がしている）。
        .stderr(Stdio::from(log))
        // アプリ終了時に道連れにする。★推論器とは逆（冒頭の注記）。
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("Python を起動できません（{}）: {e}", py.display()))?;

    let stdin = child.stdin.take().ok_or("stdin を掴めません")?;
    let stdout = child.stdout.take().ok_or("stdout を掴めません")?;
    *lock(&s.child) = Some(child);
    *lock(&s.key) = Some(key.clone());

    // ★書き側は別ロックへ預ける（読み側を握ったままでも割り込みが書けるように）。
    *s.stdin.lock().await = Some(stdin);
    let mut io = Io { stdout, reader: LineReader::new(MAX_LINE_BYTES) };

    // 握手。ここを飛ばすと「起動はしたが喋れない」状態を「使える」と言ってしまう。
    let deadline = Instant::now() + HANDSHAKE_TIMEOUT;
    let mut fold = Fold::default();
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            kill_child(s);
            return Err(format!(
                "Python が {} 秒たっても応答しません（{PY_MODULE}）。ログの末尾:\n{}",
                HANDSHAKE_TIMEOUT.as_secs(),
                log_tail(&log_path, 2000)
            ));
        }
        let chunks = match read_chunks(&mut io, left).await {
            Ok(c) => c,
            Err(e) => {
                kill_child(s);
                return Err(format!("{e}\nログの末尾:\n{}", log_tail(&log_path, 2000)));
            }
        };
        for c in chunks {
            let Chunk::Line(line) = c else { continue };
            let Ok(v) = serde_json::from_str::<Value>(&line) else {
                // JSON でない行＝起動時の警告など。診断に残すだけで落とさない。
                continue;
            };
            for sig in interpret(&v, &mut fold) {
                match sig {
                    Signal::Ready => {
                        // ★握手の行が運ぶ道具一覧を覚える（`interpret` から移した副作用）。
                        //   画面はこれを出す。アプリ内蔵の一覧を出すと、本体が動いていても
                        //   「5個しか使えない」と表示してしまう（実際に一度やった）。
                        remember_tools(&v);
                        return Ok(io);
                    }
                    Signal::Failed(m) => {
                        kill_child(s);
                        return Err(format!("Python が起動に失敗しました: {m}"));
                    }
                    _ => {}
                }
            }
        }
    }
}

/// stdout から届いた分を行にして返す。`0` バイト＝相手が閉じた。
async fn read_chunks(io: &mut Io, idle: Duration) -> Result<Vec<Chunk>, String> {
    let mut buf = [0u8; 8192];
    let n = match tokio::time::timeout(idle, io.stdout.read(&mut buf)).await {
        Err(_) => {
            return Err(format!(
                "{} 秒のあいだ Python から1バイトも届きませんでした",
                idle.as_secs()
            ))
        }
        Ok(Err(e)) => return Err(format!("Python の出力を読めません: {e}")),
        Ok(Ok(0)) => return Err(STDOUT_CLOSED.to_string()),
        Ok(Ok(n)) => n,
    };
    Ok(io.reader.push(&buf[..n]))
}

const STDOUT_CLOSED: &str = "Python の出力が閉じました（子プロセスが終了した可能性）";

/// 出力が閉じた子の終わり方（終了コードとその意味）。閉じてから終わるまで少し間があるので 2 秒まで待つ。
async fn exit_detail(s: &Sidecar) -> String {
    for _ in 0..20 {
        let st = {
            let mut g = lock(&s.child);
            match g.as_mut() {
                Some(c) => c.try_wait().ok().flatten(),
                None => return "子の記録がありません".to_string(),
            }
        };
        if let Some(st) = st {
            return describe_exit(st.code());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    "子はまだ終わっていません（出力だけが閉じた）".to_string()
}

fn describe_exit(code: Option<i32>) -> String {
    let Some(c) = code else { return "終了コード不明".to_string() };
    let u = c as u32;
    let what = match u {
        0 => "＝自分で終わった（入力が閉じたか Ctrl+C を受けた・sidecar.log の「終わる:」の行に理由）",
        1 => "＝外から止められた（taskkill /F など）か Python の例外",
        0xC000_013A => "＝Ctrl+C / Ctrl+Break を受けた",
        0xC000_0005 => "＝メモリ違反で落ちた（sidecar.log に Python のスタック）",
        0xC000_0006 => "＝プログラムの読み込みに失敗した（置き場のディスクが一時的に読めない）",
        0xC000_00FD => "＝スタックがあふれた",
        0xC000_0409 => "＝致命的な異常で即時終了した",
        0xC000_0017 | 0xC000_012D => "＝メモリが足りなかった",
        _ => "",
    };
    format!("終了コード {c}（0x{u:08X}）{what}")
}

// ---------------------------------------------------------------------------
// 公開する口
// ---------------------------------------------------------------------------

/// 同梱 Python の本体が**実際に起動して喋れるか**。
///
/// ★「ファイルがある」で真を返さない。起動して `ready` を受け取れたときだけ真。
///   ここで嘘をつくと、呼び出し側が ws_ 4ツールへ落ちる判断を誤り、
///   利用者には「道具が動かない」としか見えなくなる。
///
/// 生きた子が居ればそれが答え（余計なプロセスを起こさない）。
/// 居なければ `--probe` で1回だけ起こして確かめ、結果を 30 秒だけ使い回す。
pub async fn core_available() -> bool {
    if child_alive(sc()) {
        return true;
    }
    if let Some((at, r)) = lock(&sc().avail).clone() {
        if at.elapsed() < AVAIL_CACHE {
            return r.is_ok();
        }
    }
    let r = probe().await;
    let ok = r.is_ok();
    *lock(&sc().avail) = Some((Instant::now(), r));
    ok
}

/// 握手の行か（2つの書式のどちらでも）。
fn is_ready(v: &Value) -> bool {
    match v.get("type").and_then(|t| t.as_str()) {
        Some("ready") => true,
        Some("notice") => v.get("kind").and_then(|k| k.as_str()) == Some("ready"),
        _ => false,
    }
}

/// 握手の行が運んできた道具一覧を覚える。
/// ★画面はこれを出す。アプリ内蔵の一覧を出すと、本体が動いていても
///   「5個しか使えない」と表示してしまう（実際に一度やった）。
fn remember_tools(v: &Value) {
    if let Some(list) = v.get("tools").and_then(|t| t.as_array()) {
        let names: Vec<String> =
            list.iter().filter_map(|x| x.as_str().map(str::to_string)).collect();
        if !names.is_empty() {
            *lock(&sc().tools) = Some(names);
        }
    }
}

/// 本体がいま渡している道具の名前。握手していなければ None。
pub async fn core_tools() -> Option<Vec<String>> {
    if !core_available().await {
        return None;
    }
    lock(&sc().tools).clone()
}

/// 走行中に一言を差し込む（作業は捨てない）。
///
/// ★「止める」しか無いと、方向を変えたいだけのときも最初からやり直しになる。
/// ClaudeCode の設計メモが、エージェント UI の欠落として名指ししていた点。
/// ★force（2026-10-09 オーナー「強制割り込み可能にしたい」）: 本体は区切りを待たず、いまの生成と Bash を止めて渡す。
pub async fn inject(text: String, force: bool, lane: Option<String>) -> Result<(), String> {
    // ★書き側のロックだけを取る。読み側（io）は走行中ずっと握られているので、
    //   ここで io を取ろうとすると**走行中は絶対に届かない**（それが従来の姿）。
    // ★札（会話）があれば、その会話が走っている器へだけ書く（2026-10-30）。
    let s = lane_slot(lane.as_deref()).ok_or_else(|| "この会話はいま走っていません".to_string())?;
    let mut guard = s.stdin.lock().await;
    let stdin = guard.as_mut().ok_or_else(|| "本体が動いていません".to_string())?;
    let line = json!({ "v": 1, "type": "inject", "text": text, "force": force }).to_string() + "\n";
    stdin.write_all(line.as_bytes()).await.map_err(|e| format!("書き込めません: {e}"))?;
    stdin.flush().await.map_err(|e| format!("送れません: {e}"))?;
    Ok(())
}

/// 本体の頼み（app_call）への答えを書き戻す（2026-10-18・Browser 道具）。
async fn app_reply(s: &Sidecar, call: String, result: Value) -> Result<(), String> {
    let mut guard = s.stdin.lock().await;
    let io = guard.as_mut().ok_or_else(|| "本体が動いていません".to_string())?;
    let line = json!({ "v": 1, "type": "app_reply", "call": call, "result": result }).to_string() + "\n";
    io.write_all(line.as_bytes()).await.map_err(|e| format!("書き込めません: {e}"))?;
    io.flush().await.map_err(|e| format!("送れません: {e}"))?;
    Ok(())
}

/// 利用者の返事を本体へ書き戻す。
///
/// ★聞いておいて返事を届けられないと、本体は待ち続けて会話が固まる。
/// 本体側にも待ち時間の上限を置いてあるが、こちらが届けるのが正しい道。
pub async fn answer(ask_id: String, text: String, lane: Option<String>) -> Result<(), String> {
    let s = lane_slot(lane.as_deref()).ok_or_else(|| "この会話はいま走っていません".to_string())?;
    let mut guard = s.stdin.lock().await;
    let io = guard.as_mut().ok_or_else(|| "本体が動いていません".to_string())?;
    let line = json!({ "v": 1, "type": "answer", "ask": ask_id, "text": text }).to_string() + "\n";
    io.write_all(line.as_bytes()).await.map_err(|e| format!("書き込めません: {e}"))?;
    io.flush().await.map_err(|e| format!("送れません: {e}"))?;
    Ok(())
}

/// 直近の生存確認が失敗した理由。`core_available()` が偽を返した理由を画面に出すため。
/// ★bool だけを返して理由を捨てると、「なぜ道具が4つしか無いのか」を利用者が知る手段が消える。
pub fn last_unavailable_reason() -> Option<String> {
    lock(&sc().avail).as_ref().and_then(|(_, r)| r.as_ref().err().cloned())
}

/// 1回だけ起こして確かめる。**待ち受けポートは作らない**（selftest.rs と同じ規律）。
async fn probe() -> Result<(), String> {
    let py = stage_runtime(&resolve_python()?)?;
    let cwd = py.parent().map(|p| p.to_path_buf()).unwrap_or_else(std::env::temp_dir);
    let key = SpawnKey { workspace: cwd.clone(), backend: backend_url().await, remote: String::new() };
    let mut cmd = build_command(&py, &cwd, &key, "", &["--probe"]);
    let child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        // 時間切れでこの future を捨てたとき、道連れに殺されるようにする。
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("Python を起動できません（{}）: {e}", py.display()))?;

    let out = tokio::time::timeout(PROBE_TIMEOUT, child.wait_with_output())
        .await
        .map_err(|_| {
            format!("Python が {} 秒たっても応答しません（{PY_MODULE} --probe）", PROBE_TIMEOUT.as_secs())
        })?
        .map_err(|e| format!("Python の実行に失敗しました: {e}"))?;

    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        let tail: String = err.trim().chars().rev().take(600).collect::<Vec<_>>().into_iter().rev().collect();
        return Err(format!(
            "{PY_MODULE} が異常終了しました（{}）。stderr の末尾:\n{tail}",
            out.status
        ));
    }
    // 出力が `ready` を含む1行であることまで確かめる（終了コード 0 だけでは足りない）。
    let text = String::from_utf8_lossy(&out.stdout);
    for line in text.lines() {
        if let Ok(v) = serde_json::from_str::<Value>(line) {
            if is_ready(&v) {
                remember_tools(&v);
                return Ok(());
            }
        }
    }
    Err(format!(
        "{PY_MODULE} が ready を返しませんでした。受け取った出力:\n{}",
        preview(text.trim())
    ))
}

/// Python 本体に1回の依頼を投げ、返ってくるものを画面へ流す。
///
/// 終端の約束は冒頭のとおり。**成功なら `chat-done` を1回だけ出して Ok**、
/// **1文字も出せずに壊れたら `chat-done` を出さずに Err**（呼び出し側が `chat-error` を出す）。
pub async fn core_send(
    app: &AppHandle,
    model: String,
    messages: Vec<Value>,
    workspace: PathBuf,
    thinking: bool,
    // ★この 1 回だけの道具一式（2026-09-21・オーナー指示「ウェブ検索だけを使う
    //   通常会話モード」）。None なら今までどおり設定の値。`Some("chat")` で
    //   本体側が WebSearch / WebFetch だけを渡す（作業場の道具は 1 本も渡らない）。
    toolset: Option<String>,
    // ★同じチャットの前の走行のセッション（2026-10-05）。渡すと本体はその会話（道具の結果を含む）の
    //   続きとして走る。渡さないと依頼のたびに新しいセッションになり、前の手番で調べた結果が
    //   消えて同じ調査を繰り返していた（実機の記録）。
    session: Option<String>,
    // ★作業のモード（2026-10-15）。None なら設定の値（既定 general）。
    mode: Option<String>,
    // ★依頼を出した画面（2026-10-30）。本体の run_spec と写しの run_start に残る＝ほかの端末の一覧が「どこから来たか」を出せる。
    source: Option<String>,
    // ★この会話の宛先（backend_for が作った住所・2026-10-30）。None ならいつもの宛先（子を起こしたときの LAPS_BACKEND）。
    backend: Option<String>,
    // ★走行の器の札（会話・2026-10-30）。渡すと空いている器で走り、知らせに同じ札が付く。None なら器 0（今までどおり）。
    lane: Option<String>,
    // ★この依頼の思考の深さ（2026-10-30）。全体の値（sampling::effort）は、待っている間に別の会話の送信が書き換えうるので、
    //   入口で受けた値をそのまま依頼に載せる。Some(None)＝本体の既定。None なら今までどおり全体の値。
    effort: Option<Option<String>>,
    // ★ほかの機械（中継の名札）で走らせる（2026-10-30・Windows からほかの機械の会話を続ける）。session がその機械の会話の名札。
    remote: Option<String>,
    // ★依頼を送らず、その機械で走っている会話を追う（2026-10-31 オーナー「リモートコントロールを Claude と同じ感じに」）。
    //   走行の写しが同じ形の行で届くので、run_loop も画面もこの PC の走行と同じ。
    follow: Option<String>,
) -> Result<(), String> {
    let id = format!("r{}", sc().seq.fetch_add(1, Ordering::Relaxed) + 1);
    // ★採番と問いは**先に**作る。早期 return の経路からも記録を残すため
    //   （以前は run_loop の直前で作っていたので、始まる前に落ちた回は何も残らなかった）。
    let ask = messages
        .iter()
        .rev()
        .find(|m| m.get("role").and_then(|r| r.as_str()) == Some("user"))
        .and_then(|m| m.get("content").and_then(|c| c.as_str()))
        .unwrap_or("")
        .to_string();

    // 実行は同時に1つ。★待たせずに断る。UI 側は state.busy で二重送信を止めているので、
    //   ここに2つ来るのは何かが壊れている合図であり、黙って待つと原因が見えなくなる。
    let lane = lane.map(|l| l.trim().to_string()).filter(|l| !l.is_empty() && l.len() <= 64);
    let (si, mut guard) = match pick_slot(lane.as_deref()) {
        Some(x) => x,
        None => {
            // ★`BUSY:` を付けて**区別できる形**にする（2026-08-22）。
            //   呼び出し側はこれまで「本体が応答しない」と一緒くたに扱い、
            //   **道具5個の控えへ黙って落ちていた**。忙しいのと壊れているのは別で、
            //   忙しいときに能力の低い側へ落ちるのは、待つより悪い
            //   （実機で、退行した答えを返した走行がまさにこれだった）。
            //   ★記録の呼び出しと return の間に行を挟まないこと——
            //     preflight 3.6 が隣接で見ており、挟んだら落ちた（実際に踏んだ）。
            record_early_failure("busy", &workspace, &id, &ask);
            return Err(if lane.is_some() && lanes_running().len() >= LANES {
                format!("BUSY:同時に走らせられるのは {LANES} つの会話までです")
            } else {
                "BUSY:前の依頼がまだ終わっていません".to_string()
            });
        }
    };
    let s: &'static Sidecar = &slots()[si];
    // 器の持ち主（この会話）。どの抜け道でも外す（OwnerMark の Drop）。
    let _owner = lane.as_ref().map(|l| {
        lock(&OWNERS)[si] = Some(l.clone());
        let mut last = lock(&LAST_SLOT);
        last.retain(|(x, _)| x != l);
        last.push((l.clone(), si));
        if last.len() > 32 {
            last.remove(0);
        }
        OwnerMark(si)
    });

    // ★宛先を1回だけ解決して、以降はその値を使う（門・鍵・記録が同じ値を見る）。
    let dest = resolve_backend().await;
    let remote = remote.filter(|d| !d.is_empty() && d.len() <= 64
        && d.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-'));
    let follow = follow.filter(|f| remote.is_some() && !f.is_empty() && f.len() <= 128
        && f.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-'));
    let key = match remote.as_deref() {
        // 線の名札は会話の札ごと（アプリが動いている間は同じ＝向こうの serve を使い回す・切れても猶予の間は続きを受け取れる）
        Some(d) => SpawnKey {
            workspace: crate::app_root(),
            backend: format!("relay:{d}"),
            remote: match follow.as_deref() {
                Some(f) => format!("{d}/follow:{f}:{id}"),
                None => format!("{d}/app{}-{}", std::process::id(),
                                lane.as_deref().unwrap_or("x").chars().filter(|c| c.is_ascii_alphanumeric()).collect::<String>()),
            },
        },
        None => SpawnKey { workspace: workspace.clone(), backend: dest.effective.clone(), remote: String::new() },
    };

    // ★ローカルへ向いているなら、送る前に3秒だけ確かめる（2026-09-06）。
    //   上がっていない推論機に投げると、本体は45秒黙ってから失敗する＝
    //   実機の報告「応答を待っています（N秒）→ エラー」がそれ。
    //   待たせずに、宛先と次の手を添えて断る。
    if !dest.remote && remote.is_none() {
        if let Some(why) = local_health_block(dest.local_port).await {
            record_early_failure("local_backend_down", &workspace, &id, &ask);
            return Err(format!("{why}\n（送り先: {} ／ 理由: {}）", dest.effective, dest.source_label));
        }
    }

    // 使い回す。ただし (作業ディレクトリ, 推論器) が変わっていたら作り直す。
    let stale = lock(&s.key).as_ref() != Some(&key);
    if guard.is_none() || !child_alive(s) || stale {
        if guard.is_some() || child_alive(s) {
            kill_child(s);
        }
        *guard = None;
        // ★書き側も一緒に捨てる。読み側(io)だけ None にして stdin を残すと、
        //   死んだ子のパイプに書き続ける「動いているつもり」の状態が残る
        //   （stdin を別ロックに分けたときに生まれる、分けたからこその落とし穴）。
        *s.stdin.lock().await = None;
        *guard = Some(match spawn_child(s, &key, &model).await {
            Ok(c) => c,
            Err(e) => {
                record_early_failure("spawn_failed", &workspace, &id, &ask);
                return Err(e);
            }
        });
    }
    let mut req_v = build_request(&id, &model, &messages, &workspace, thinking,
                                  toolset.as_deref(), mode.as_deref());
    if let (true, Some(e)) = (thinking, effort) {
        req_v["effort"] = e.map(Value::from).unwrap_or(Value::Null);
    }
    // ★ほかの機械で走らせるとき: この PC の作業フォルダ・モデル・宛先・アプリの頼み（ブラウザ）は渡さない（向こうの機械の物を使う）。
    if remote.is_some() {
        // ★アプリの環境の説明（OS: Windows・cmd.exe・この PC の作業フォルダ・この PC の GPU）も渡さない。向こうの本体が自分の
        //   環境を前置きに書く＝食い違う事実を渡すと、モデルが Windows の作法で Linux を触る。アプリの指示（system）だけ残す。
        let app_lines: String = messages
            .iter()
            .filter(|m| m.get("role").and_then(|r| r.as_str()) == Some("system"))
            .filter_map(|m| m.get("content").and_then(|c| c.as_str()))
            .collect::<Vec<_>>()
            .join("\n");
        if let Some(o) = req_v.as_object_mut() {
            for k in ["workspace", "model", "backend", "app_caps"] {
                o.remove(k);
            }
            o.insert("guidance".into(), json!(app_lines));
        }
    }
    if let Some(s) = session.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        req_v["session"] = json!(s);
    }
    let src = source.as_deref().map(str::trim).filter(|s| !s.is_empty() && s.len() <= 16
        && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')).unwrap_or("app");
    req_v["source"] = json!(src);
    if let Some(b) = backend.as_deref().filter(|b| remote.is_none() && b.len() < 200 && (b.starts_with("http://") || b.starts_with("https://"))) {
        req_v["backend"] = json!(b);
    }
    let req = req_v.to_string();

    // 書き込みだけを先に閉じた借用で行う。★ここで `guard` を触りたい（壊れたら None に戻す）ので、
    //   `io` の借用を跨がせない。
    // 追うときは依頼を送らない（向こうで既に走っている）。
    let wrote = if follow.is_some() { Ok(()) } else {
        // ★読み側(io)がここに在ることは確かめるが、書き込みには使わない。
        //   stdin を別ロックへ分けた（割り込みを通すため）ので、書くのは下の `sin`。
        //   ★`let _ = ...expect(..)` にしているのは**存在確認を残すため**——
        //     この行を消すと「子が居なくても書きに行く」経路が生まれる。
        let _ = guard.as_mut().expect("直前に入れている");
        // ★書き側は別ロック（inject が走行中でも書けるようにするため）。
        //   ここでは**短く借りて返す**。io を握ったまま stdin を握り続けると、
        //   割り込みが再び届かなくなる（分離した意味が消える）。
        let mut sguard = s.stdin.lock().await;
        let sin = match sguard.as_mut() {
            Some(x) => x,
            None => {
                // ★早期に抜ける道も記録に残す（preflight 3.6 が要求する作法）。
                //   黙って抜けると、記録には「何も起きなかった」ように見える。
                record_early_failure("no_stdin", &workspace, &id, &ask);
                return Err("本体が動いていません".into());
            }
        };
        let mut r = sin.write_all(req.as_bytes()).await;
        if r.is_ok() {
            r = sin.write_all(b"\n").await;
        }
        if r.is_ok() {
            r = sin.flush().await;
        }
        r
    };
    if let Err(e) = wrote {
        let log = lock(&s.log).clone();
        kill_child(s);
        *guard = None;
        *s.stdin.lock().await = None;     // ★書き側も一緒に（上と同じ理由）
        record_early_failure("write_failed", &workspace, &id, &ask);
        return Err(format!(
            "Python へ依頼を渡せません: {e}{}",
            log.map(|p| format!("\nログの末尾:\n{}", log_tail(&p, 2000))).unwrap_or_default()
        ));
    }

    let io = guard.as_mut().expect("直前に入れている");
    let ws_for_check = if remote.is_some() { PathBuf::new() } else { workspace.clone() };
    let r = run_loop(app, s, lane.as_deref(), io, &ws_for_check, &id, &ask, &key.backend, &dest.source).await;
    reap_later(si);
    r
}

/// 依頼に使う器を選ぶ。札が無ければ器 0（今までどおり・空いていなければ None＝BUSY）。
/// 札があれば: 同じ会話が走っていれば None（二重に走らせない）。前に使った器 → 0 → 1 → 2 の順に空きを探す。
fn pick_slot(lane: Option<&str>) -> Option<(usize, tokio::sync::MutexGuard<'static, Option<Io>>)> {
    let Some(l) = lane else {
        return sc().io.try_lock().ok().map(|g| (0, g));
    };
    if lock(&OWNERS).iter().any(|o| o.as_deref() == Some(l)) {
        return None;
    }
    let pref = lock(&LAST_SLOT).iter().find(|(x, _)| x == l).map(|(_, i)| *i);
    let order: Vec<usize> = pref.into_iter().chain(0..LANES).collect();
    for i in order {
        if lock(&OWNERS)[i].is_some() {
            continue;
        }
        if let Ok(g) = slots()[i].io.try_lock() {
            return Some((i, g));
        }
    }
    None
}


/// 始まる前に落ちた回を、それでも記録に残す。
///
/// ★`core_send` には run_loop へ到達する前に抜ける経路が3つある
///   （前の依頼が終わっていない／子を起こせない／依頼を渡せない）。
///   どれも記録を1件も残さずに終わっていた。
///   すぐ下の記録ブロックには「記録は成功でも失敗でも残す。失敗こそ後から知りたい」と
///   書いてあるのに、**失敗の入口が抜けていた**（2026-08-16 に判明）。
///
///   実害: オーナーが試した問いが**履歴にも記録にも現れず**、
///   「送っていないのか、失敗したのか、まだ動いているのか」を区別できなかった。
///   沈黙は3つの別々の状態を1つに潰す。
fn record_early_failure(reason: &str, workspace: &Path, id: &str, ask: &str) {
    let mut t = crate::journal::Turn::default();
    t.client = json!({ "app_version": env!("CARGO_PKG_VERSION") });
    t.engine = json!({
        "kind": "core",
        "model": Value::Null,
        "workspace": crate::workspace::display_path(workspace),
    });
    t.run = json!({ "request_id": id });
    let (head, cut) = clamp_chars(ask, 2000);
    t.input = json!({ "text": head, "chars": ask.chars().count(), "truncated": cut });
    t.output = json!({ "text": "", "chars": 0 });
    t.think = json!({ "text": "", "chars": 0 });
    t.tools = Vec::new();
    // ★終わり方に「始まる前に落ちた」と分かる名前を付ける。done に混ぜない。
    t.stop = json!({ "reason": format!("not_started: {reason}") });
    crate::journal::write(t);
}

/// 終端。どちらか一方だけが1回起きる。
enum End {
    /// `usage` は消費トークン、`meta` は `done` の中身から本文だけ抜いたもの。
    ///
    /// ★**2つ持つ理由**: 以前は `usage` だけを持ち、記録は `usage.get("model")` と
    ///   `usage.get("outcome")` を見ていた。どちらも `usage` の**兄弟**なので
    ///   構造上いつまでも見つからず、実機40件で **model は全件 null**、
    ///   **outcome は全件 "done" に落ちていた**（＝利用者が中断した3件まで
    ///   「正常終了」として記録されていた）。参照先を1つ直すと両方が直る。
    Done { usage: Option<Value>, meta: Value },
    Failed(String),
}

async fn run_loop(
    app: &AppHandle,
    s: &'static Sidecar,
    lane: Option<&str>,
    io: &mut Io,
    workspace: &Path,
    id: &str,
    ask: &str,
    backend: &str,
    backend_source: &str,
) -> Result<(), String> {
    // ★記録用に溜める。journal.rs は書かれ、起動もし、コマンドも登録されていたのに
    //   **`journal::write` を呼ぶ場所が1つも無く**、実機から1件も届かなかった。
    //   「書いたが繋いでいない」は今日4回目。溜めるのはここ＝本文も思考も道具も通る唯一の場所。
    // ★知らせには札を付ける（会話ごとに同時に走る・画面は自分の札だけを拾う）。
    let out = LaneOut { app, lane };
    let rec_started = std::time::Instant::now();
    let mut rec_answer = String::new();
    let mut rec_think = String::new();
    let mut rec_tools: Vec<Value> = Vec::new();
    // ★本文が**どう組み立てられたか**を記録に残すための数。
    //   無いと、二重や空白が「転送側の問題」なのか「モデルの問題」なのかを
    //   毎回コードを読んで推理することになる（実際にそうしていた）。
    let mut rec_promoted = 0usize;        // 思考→本文へ移した文字数
    let mut rec_reclassified = 0usize;    // 本文→思考へ移した文字数
    let mut rec_restarts = 0usize;        // 差し戻しで前の試作を捨てた回数
    // 最後に降格された本文（内訳を数えるために覚えておく）。
    let mut rec_demoted = String::new();
    let mut rec_args: std::collections::HashMap<String, Value> = std::collections::HashMap::new();
    let mut rec_t0: std::collections::HashMap<String, std::time::Instant> =
        std::collections::HashMap::new();
    let mut split = crate::think::Splitter::default();
    let mut meter = crate::think::Meter::default();
    // ★本体経由は逐次で来ない。断片を数えると 9,760 字が 4 トークンになる（実機の記録で発覚）。
    meter.count_by_chars();
    let mut fold = Fold::default();
    // ツール名 → 開始時の引数。★列で持つ。Python は読み取り専用のツールを**並列に**実行するので
    //   （agent.py の `_exec_calls_parallel_safe`）、同じ名前の開始が2つ続くことがある。
    //   1つしか覚えないと、片方の終了に**別の呼び出しの引数**が付く。
    let mut pending: std::collections::HashMap<String, std::collections::VecDeque<Value>> =
        std::collections::HashMap::new();
    let mut unknown: std::collections::BTreeMap<String, usize> = std::collections::BTreeMap::new();
    let mut usage: Option<Value> = None;
    let idle = idle_timeout();

    // 前の実行の書きかけを持ち越さない（上の discard_partial 参照）。
    let residue = io.reader.discard_partial();
    if residue > 0 {
        eprintln!("[blackwall] sidecar: 前回の書きかけ {residue} バイトを捨てました");
    }

    // ★中止。本体経路には中止の確認が1箇所も無く、「止める」を押しても
    //   何も起きなかった（実機で報告）。ここで見て、**本体にも cancel を送る**
    //   （読むのをやめるだけでは向こうの生成が続き、GPU が回りっぱなしになる）。
    //   ★`current()` ではなく **`begin()`**（2026-08-21・敵対レビューで判明）。
    //     `current()` は世代を進めないので、**一度「止める」を押すと
    //     以後この経路（道具あり＝出荷の既定）が毎回その場で即死する**——
    //     `stop()` が `STOPPED = GEN` にしたまま、次の依頼も同じ GEN を読むため。
    //     画面には「止めました。」の1行だけが出て、依頼は何もせず終わる。
    //     cancel.rs の冒頭が**まさにこの壊れ方**を警告している:
    //       「単なる真偽値にすると、止めた直後に始めた生成が、
    //         前の中止指示を引き継いで即死する」
    //     モジュールは正しく作られていて、この呼び出し側だけが作法を守っていなかった。
    //     `run_loop` は `core_send` から**1依頼につき1回**呼ばれるので、
    //     ここで世代を進めるのが正しい（llama.rs / agent.rs と同じ）。
    let gen = crate::cancel::begin_lane(lane.unwrap_or(""));
    let mut stopped = false;
    let mut stopped_at: Option<std::time::Instant> = None;

    let end: End = 'outer: loop {
        if crate::cancel::is_stopped(gen) && !stopped {
            stopped = true;
            stopped_at = Some(std::time::Instant::now());
            let cancel = json!({ "v": 1, "type": "cancel", "id": id }).to_string() + "\n";
            if let Some(sin) = s.stdin.lock().await.as_mut() {
                let _ = sin.write_all(cancel.as_bytes()).await;
                let _ = sin.flush().await;
            }
            // ★「止めました。」は**嘘になりうる**（送っただけで、まだ止まっていない）。
            //   `CancelTrack` に記録して 1 本にまとめる——本体からの `cancel` /
            //   `cancel_applied` / `cancelled` は、以後この行を重ねず**新しく分かったこと
            //   だけ**を足す（旧: 同じ文が最大 3 回並んだ）。
            if let Some(m) = fold.cancel.announce_locally() {
                live_note(&out, &m, false);   // 状態行だけ（答えの下には done の「中断しました。」が残る・2026-11-19）
            }
        }
        // ★本体が終わりを返さないことがある。待ち続けると画面のボタンが
        //   「止める」のまま戻らない（実機で報告）。少し待って、来なければこちらで閉じる。
        if let Some(t) = stopped_at {
            // ★閉じる前に「まだ止まっていない」を 1 回だけ言う（端末版 `cancel_stall` と同じ）。
            //   これが無いと、本体がまだ動いているのに画面は黙って「終わった」と言う。
            if let Some(m) = fold.cancel.stall(t.elapsed().as_secs()) {
                gate_note(&out, &m);
            }
            if t.elapsed() > STOP_GRACE {
                // ★穴C（2026-08-23）: ここで meta を空で抜けると、記録側は
                //   outcome を `done`（正常完了）にする。今日「止める」を何度も
                //   押したのに記録に cancelled が **0 件**だったのはこれ。
                //   止めた走行が正常完了として残ると、止めた事実も、止めるのに
                //   何秒かかったかも、後から追えない。
                //   → meta に outcome と経過を載せて抜ける。
                break 'outer End::Done {
                    usage: None,
                    meta: json!({
                        "outcome": "cancelled_grace",
                        "cancel_latency_ms": t.elapsed().as_millis() as u64,
                        "note": "本体が STOP_GRACE 内に done を返さなかったのでアプリ側で閉じた",
                    }),
                };
            }
        }
        let chunks = match read_chunks(io, idle).await {
            Ok(c) => c,
            Err(e) => {
                // ★出力が閉じた＝子が終わった。どう終わったか（終了コード）を添える。無いと Ctrl+C か落ちたか殺されたかが
                //   分からない（2026-11-12 実機で走行の途中に 2 回消え、記録にも画面にも手掛かりが無かった）。
                let e = if e.starts_with(STDOUT_CLOSED) { format!("{e}・{}", exit_detail(s).await) } else { e };
                break 'outer End::Failed(e);
            }
        };
        for c in chunks {
            let line = match c {
                Chunk::Line(l) => l,
                // ★黙って捨てない。捨てた事実が見えないと、欠けた結果の原因を追えない。
                Chunk::Oversized(n) => {
                    line_note(
                        &out,
                        &format!("受け取った1行が大きすぎるので捨てました（約 {n} バイト）。ツールの出力を絞ってください。"),
                    );
                    continue;
                }
            };
            let v: Value = match serde_json::from_str(&line) {
                Ok(v) => v,
                // 壊れた1行で会話全体を落とさない（llama.rs / agent.rs と同じ）。
                Err(_) => {
                    *unknown.entry("(JSON でない行)".to_string()).or_insert(0) += 1;
                    continue;
                }
            };

            // ★他の依頼の行は読まない（belongs_to の注記を参照）。
            //   握り潰さずに数える——「前の依頼の残りを N 行捨てた」が見えないと、
            //   混線が起きていたことに後から気づけない。
            if !belongs_to(&v, id) {
                *unknown.entry("(別の依頼の行)".to_string()).or_insert(0) += 1;
                continue;
            }

            // cwd の食い違いだけは最初に見る。別のフォルダに書かれるのが最悪の壊れ方なので。
            if v.get("type").and_then(|t| t.as_str()) == Some("init") {
                if let Some(cwd) = payload_of(&v).get("cwd").and_then(|c| c.as_str()) {
                    if !workspace.as_os_str().is_empty() && !same_dir(Path::new(cwd), workspace) {
                        line_note(
                            &out,
                            &format!(
                                "⚠ 依頼した作業ディレクトリ（{}）と、Python が居る場所（{cwd}）が違います。",
                                workspace.display()
                            ),
                        );
                    }
                }
            }

            for sig in interpret(&v, &mut fold) {
                match sig {
                    Signal::Text(t) => {
                        for piece in split.push(&t) {
                            // 画面へ出すのと同じ断片を記録にも積む（別々に組み立てない）。
                            //
                            // ★2026-08-16: ここは4種類ぜんぶ扱う。以前は `_ => {}` で
                            //   `Promote`（思考→本文へ移す）と `Reclassify`（本文→思考へ移す）を
                            //   捨てていた。この2つは**位置を移す指示**なので、落とすと
                            //   画面と記録が食い違う——
                            //     Promote     … 画面には出たのに**記録は空**
                            //     Reclassify  … 画面から消えたのに**記録には残る**
                            //   実機の「本文が空 8/40」「重複 6/40」を記録から数えていたが、
                            //   その数字が画面の値である保証が無かった。すぐ上のコメントが
                            //   「別々に組み立てない」と約束しているのに、守っていなかった。
                            record_piece(&piece, &mut rec_answer, &mut rec_think,
                                         &mut rec_promoted, &mut rec_reclassified,
                                         &mut rec_demoted);
                            crate::llama::emit_piece(&out, &mut meter, piece);
                        }
                    }
                    Signal::Think(t) => {
                        rec_think.push_str(&t);
                        crate::llama::emit_piece(&out, &mut meter, crate::think::Piece::Think(t));
                    }
                    // 反復の切れ目。**本文は残す**。分離器だけ作り直す
                    // （`</th` で止まった断片を捨てないよう、保留分は吐き出す）。
                    Signal::IterStart { ctx, n_ctx, compact_at } => {
                        for piece in split.finish(false) {
                            record_piece(&piece, &mut rec_answer, &mut rec_think,
                                         &mut rec_promoted, &mut rec_reclassified,
                                         &mut rec_demoted);
                            crate::llama::emit_piece(&out, &mut meter, piece);
                        }
                        split = crate::think::Splitter::default();
                        // 手番の区切りを画面へ（走行の線: 1 手番＝1 行・2026-11-02）。
                        // 文脈の使用量（入力欄の残量の輪・2026-11-10）。0 は「知らない」。
                        out.out("chat-iter", json!({ "ctx": ctx, "n_ctx": n_ctx, "compact_at": compact_at }));
                    }
                    Signal::NewGeneration => {
                        // ★差し戻し（pushback）で作り直したとき、前の試作が画面に
                        //   残ったままだと同じ答えが2回3回と並ぶ（実機で3回並んだ）。
                        //   新しい生成は**前の答えを置き換える**のが正しい。
                        if !rec_answer.is_empty() {
                            out.out("chat-restart", json!({}));
                            rec_answer.clear();
                            rec_restarts += 1;
                        }
                        // 保留中の断片を吐き出してから作り直す（`</th` で止まった分を捨てない）。
                        // ★`allow_promote=false`。次の生成が始まる＝ふつうはツールを呼んだあとで、
                        //   think.rs の言う C（思考のあとツールを呼んだ）に当たる。ここで思考を
                        //   本文へ戻すと、思考が答えに混ざったうえ履歴にも入る。
                        // ★ここも記録に積む。積んでいなかったので、保留していた本文が
                        //   画面にだけ出て記録から消えていた。
                        for piece in split.finish(false) {
                            record_piece(&piece, &mut rec_answer, &mut rec_think,
                                         &mut rec_promoted, &mut rec_reclassified,
                                         &mut rec_demoted);
                            crate::llama::emit_piece(&out, &mut meter, piece);
                        }
                        split = crate::think::Splitter::default();
                    }
                    Signal::Ask { id, question, options, descriptions } => {
                        out.out(
                            "agent-ask",
                            json!({
                                "id": id,
                                "question": question,
                                "options": options,
                                // ★選択肢の説明（serve.py:1020・2026-09-03）。読まないと
                                //   利用者は label だけを見て当てずっぽうで押すことになる。
                                "descriptions": descriptions,
                            }),
                        );
                    }
                    // ★問いが閉じた（答えた・時間切れ・止めた・強制割り込み・2026-10-16 監査）。画面の箱を消させる。
                    Signal::AskClosed { id } => {
                        out.out("agent-ask-closed", json!({ "id": id }));
                    }
                    // ★本体からの頼み（Browser 道具・2026-10-18）。待たずに別のタスクで行い、終わったら答えを書き戻す
                    //   （ここで待つと、その間の本体の出力を読めなくなる）。
                    Signal::AppCall { call, op, args } => {
                        let app2 = app.clone();
                        tauri::async_runtime::spawn(async move {
                            let result = crate::browser::agent_call(app2, op, args).await;
                            if let Err(e) = app_reply(s, call, result).await {
                                eprintln!("app_reply を返せませんでした: {e}");
                            }
                        });
                    }
                    Signal::ToolStart { name, call, args } => {
                        // ★対応キーは相関 id（call）。無ければ名前へフォールバック。
                        //   名前だけで対にすると、同名ツールを並行/連続で呼んだとき
                        //   `rec_args[name]` が後の呼び出しで上書きされ、最初の ToolEnd が
                        //   その1件を奪い、残りは args=null になっていた（実機 mstrnuc8: 並行 Read
                        //   3本で [1]=別ファイルの args・[2][3]=null。実行自体は正しく、記録側の取り違え）。
                        let key = call.clone().unwrap_or_else(|| name.clone());
                        // ★1つ1つの所要秒。無いと「86秒かかった」の内訳が分からず、
                        //   モデルが遅いのか道具が遅いのかを推測で語ることになる
                        //   （実機の 86秒・道具7回で、実際に行き詰まった）。
                        rec_t0.insert(key.clone(), std::time::Instant::now());
                        rec_args.insert(key.clone(), args.clone());
                        // ★**始まったことを画面へ出す。** これが無いと「道具が動いている」
                        //   という状態が画面に**存在しない**（2026-08-18 の UI 敵対レビュー）。
                        //   道具が走っている数十秒〜数分、画面は無音のまま「Thinking」と
                        //   **積極的に誤った状態**を出していた。受け手（app.js の tool()）は
                        //   最初から start/end の生涯を扱える作りで、`phase:"start"` と `id` を
                        //   見て「実行中」に積む——送っていなかったのは送り手だけ。
                        out.out(
                            "agent-tool",
                            json!({
                                "id": key,
                                "name": name,
                                "args": clamp_args(&args),
                                "phase": "start",
                            }),
                        );
                        pending.entry(key).or_default().push_back(args);
                    }
                    Signal::ToolEnd { name, call, result, ok, kind, chars, truncated, summary } => {
                let key = call.clone().unwrap_or_else(|| name.clone());
                // ★引数を残す。道具名と文字数だけでは「dir が 19 文字を返した」までしか
                //   分からず、**何を実行したか**が追えなかった（実機で行き詰まった）。
                //   結果の先頭も残す＝定型の失敗文（`(exit 0, no output)` 等）を後から数えられる。
                let args = rec_args.remove(&key).unwrap_or(Value::Null);
                let secs = rec_t0
                    .remove(&key)
                    .map(|t| (t.elapsed().as_secs_f64() * 10.0).round() / 10.0);
                let (head, _) = clamp_chars(&result, 400);
                rec_tools.push(json!({
                    "name": name,
                    "ok": ok,
                    "secs": secs,
                    "args": args,
                    "result_chars": result.chars().count(),
                    "result_head": head,
                }));
                        let args = pending
                            .get_mut(&key)
                            .and_then(|q| q.pop_front())
                            .unwrap_or_else(|| json!({}));
                        // 何が起きたかを画面へ。ツールが黙って走らないこと自体が要件。
                        out.out(
                            "agent-tool",
                            json!({
                                // ★`id` を必ず載せる。無いと受け手は名前で対を取るので、
                                //   start を出した途端に**1回の呼び出しが2行に割れる**
                                //   （同名ツールの並行実行では混線する）。
                                "id": key,
                                "name": name,
                                "args": clamp_args(&args),
                                "result_preview": preview(&result),
                                "ok": ok,
                                // ★0件(empty)・失敗(error)・拒否(denied) を画面が区別できるように。
                                //   これが無いと「該当なし」を赤い失敗として見せてしまう。
                                "kind": kind,
                                // ★切り詰める**前**の長さ。preview の長さを実サイズとして
                                //   出していたので、2万字の出力が「201 chars」に見えていた。
                                "chars": chars.unwrap_or(result.chars().count() as u64),
                                "truncated": truncated.unwrap_or(false),
                                "phase": "end",
                                // ★1行に所要秒を出す。どこで待たされたかを、
                                //   推測でなく画面で見つけられるように。
                                "secs": secs,
                                // ★本体が作った要約（問題4）。画面はこれを1行目に出す。
                                "summary": summary,
                            }),
                        );
                    }
                    Signal::Line(t) => line_note(&out, &t),
                    Signal::Gate(t) => gate_note(&out, &t),
                    Signal::Live { text, gate } => live_note(&out, &text, gate),
                    Signal::Unknown(t) => {
                        // 数えたうえで**出す**。旧実装は数えて stderr にしか書かず、
                        // 利用者の画面には一度も出なかった（＝実質「黙って捨てた」）。
                        *unknown.entry(t.clone()).or_insert(0) += 1;
                        unknown_note(&out, &t);
                    }
                    Signal::Heartbeat { stage, quiet_secs, detail } => {
                        out.out("chat-heartbeat",
                                         json!({ "stage": stage, "quiet_secs": quiet_secs, "detail": detail }));
                    }
                    Signal::Ready => remember_tools(&v),
                    Signal::Ignore(t) => {
                        // ★知らない **type**。旧実装は数えるだけで stderr にしか出さず、
                        //   利用者にも記録にも見えなかった。最初の1回だけ画面へ出す
                        //   （毎回出すと、本体が新しい型を流し始めた日に画面が埋まる）。
                        let c = unknown.entry(t.clone()).or_insert(0);
                        *c += 1;
                        if *c == 1 {
                            unknown_note(&out, &format!("不明な事象（type={t}）"));
                        }
                    }
                    Signal::Done { usage: u, meta } => {
                        if u.is_some() {
                            usage = u;
                        }
                        break 'outer End::Done { usage: usage.take(), meta };
                    }
                    Signal::Failed(m) => break 'outer End::Failed(m),
                }
            }
            if meter.should_report() {
                out.out("chat-stat", meter.stat());
            }
        }
    };

    // 保留分を吐き出す。引数は `allow_promote`＝思考欄に入れた分を本文へ戻してよいか。
    // ★自然に終わったときだけ戻す。途中で壊れた＝思考の途中かもしれないので戻さない
    //   （think.rs の A/B/C。B と C を A として扱うと思考が答えに混ざる）。
    let allow_promote = matches!(end, End::Done { .. });
    for piece in split.finish(allow_promote) {
        record_piece(&piece, &mut rec_answer, &mut rec_think,
                     &mut rec_promoted, &mut rec_reclassified, &mut rec_demoted);
        crate::llama::emit_piece(&out, &mut meter, piece);
    }
    let _ = &rec_demoted;

    // 知らない型は握り潰さない。数だけでも出しておくと、送り手の変更にすぐ気づける。
    if !unknown.is_empty() {
        let s = unknown.iter().map(|(k, n)| format!("{k}×{n}")).collect::<Vec<_>>().join(" ");
        eprintln!("[blackwall] sidecar: 読み飛ばした行 {s}");
    }

    // ★記録は成功でも失敗でも残す。失敗こそ後から知りたい。
    //   `done` の生の中身を使う（done_payload は outcome や counters を落とす）。
    {
        // ★`outcome` も `model` も `done` の**直下**にある（`usage` の中ではない）。
        //   ここを `usage` から引いていたせいで、実機40件すべてで
        //   outcome が既定値 "done" に落ち、**中断が正常終了として記録されていた**。
        let (outcome, meta) = match &end {
            End::Done { meta, .. } => (
                meta.get("outcome")
                    .and_then(|x| x.as_str())
                    .unwrap_or("done")
                    .to_string(),
                meta.clone(),
            ),
            End::Failed(m) => (format!("failed: {m}"), Value::Null),
        };
        let mut t = crate::journal::Turn::default();
        t.client = json!({ "app_version": env!("CARGO_PKG_VERSION") });
        // モデル名は done イベント（本体が backend.cfg.model を載せる）から拾う。
        //   無ければ null（＝分からない）。控えの一覧は入れない＝嘘の値を作らない。
        let rec_model = meta
            .get("model")
            .and_then(|m| m.as_str())
            .filter(|m| !m.is_empty());
        // ★2026-09-06（会議 [space:推論機不達]）: **serve へ実際に渡した宛先**と、それを決めた根拠を
        //   record に残す。画面の「LAN」表示と実際の宛先が食い違って一日見えなかった。
        t.engine = json!({
            "kind": "core",
            "model": rec_model,
            "workspace": crate::workspace::display_path(workspace),
            "backend": redact_userinfo(backend),
            "backend_source": backend_source,
        });
        t.run = json!({ "request_id": id });
        // ★利用者の問いを残す。残していなかったので、評価用の問いを実データから
        //   起こせず、記録の価値が半減していた（設計書は input.text を必須にしていた）。
        //   最後の user 発言＝この回の問い。長いものは切る。
        let (ask_head, ask_cut) = clamp_chars(ask, 2000);
        t.input = json!({
            "text": ask_head,
            "chars": ask.chars().count(),
            "truncated": ask_cut,
        });
        t.output = json!({ "text": rec_answer, "chars": rec_answer.chars().count() });
        // ★組み立ての内訳。`server/report.py` が以前からこの3つを読もうとしていたが、
        //   記録側が**一度も書いていなかった**（読む側だけ先に出来ていた）。
        t.think = json!({
            "text": rec_think,
            "chars": rec_think.chars().count(),
            "promoted": rec_promoted,
            "reclassified": rec_reclassified,
            "stuck_in_thought": rec_answer.trim().is_empty() && !rec_think.trim().is_empty(),
        });
        t.tools = rec_tools.clone();
        let mut cost = meter.stat();
        if let Some(o) = cost.as_object_mut() {
            // 何回まわったか。無いと「二重は1回の生成か、2回まわった結果か」が分からない。
            if let Some(v) = meta.get("iters").cloned() {
                o.insert("iters".into(), v);
            }
            o.insert("restarts".into(), json!(rec_restarts));
            // ★推論器が finish_reason を返していれば載せる。無ければ**入れない**
            //   （null と「読んでいない」を混ぜない）。
            if let Some(v) = meta.get("finish_reason").cloned() {
                o.insert("finish_reason".into(), v);
            }
        }
        t.cost = cost;
        // ★失敗の本文（例外の型・宛先入り）を stop に添える。数字だけの record では
        //   閉じた loopback への拒否と LAN の無応答が区別できなかった。
        let stop_err = if outcome.starts_with("backend_error") || outcome.starts_with("crashed")
            || outcome.starts_with("not_started") {
            Some(rec_answer.chars().take(300).collect::<String>())
        } else if outcome.starts_with("failed") {
            // ★本体が消えた・壊れた。sidecar.log の末尾（本体の stderr）を添える。記録は伏字の濾し器を通ってから送られる。
            //   無いと「どう終わったか」を利用者の機械まで見に行くしかない（2026-11-12）。
            lock(&s.log).clone().map(|p| log_tail(&p, 1500)).filter(|t| !t.is_empty())
        } else { None };
        t.stop = json!({ "reason": outcome, "error": stop_err });
        t.counters = meta.get("counters").cloned().unwrap_or(Value::Null);
        // ★読み飛ばした行は**記録にも**残す（2026-09-02 監査）。
        //   上の `eprintln!` は製品では誰にも届かない: release ビルドは
        //   `windows_subsystem = "windows"`（main.rs:2）＝コンソールが無く、
        //   stderr の行き先が無い。つまり「捨てた事実が見える」はずが、
        //   開発ビルドでしか見えていなかった。
        //
        //   ここに残すのは、混線が**黙って**起きるから:
        //   「止める」を押した相手が >3 秒の道具を回していると、本体は道具が
        //   返るまで cancel を受け取れず、run_loop は STOP_GRACE(3秒) で先に
        //   抜ける＝子は生きたまま残る。実測で道具の 245/10,904 = 2.25% が
        //   3 秒を超える（scratch/gui-audit・実トレース8本）。残った行は次の
        //   依頼で読まれ、その中の `done` を自分の終端として使ってしまう。
        //   `belongs_to` が捨てるようになったので実害は消えたが、**起きたこと
        //   自体**が残らないと、混線していた事実に後から気づけない。
        //
        //   置き場は `protocol`（counters ではない）。counters は本体からの
        //   逐語で、実測で 68〜83 鍵ある（上限 100）。ここに足すと、増えたとき
        //   に**本体の鍵のほうが**溢れて落ちる。journal.rs の note_error と
        //   同じ作法＝eprintln は残し、控えも残す。
        if let Some(v) = skipped_record(&unknown) {
            t.protocol = v;
        }
        crate::journal::write(t);
        let _ = rec_started;
    }

    match end {
        End::Done { usage, meta } => {
            // ★本体が走らせたセッションを画面へ返す（次の依頼で同じ会話を続けるため）。
            let mut p = done_payload(&meter, &usage);
            if let (Some(o), Some(s)) = (p.as_object_mut(),
                                         meta.get("session").and_then(|v| v.as_str())) {
                o.insert("session".into(), json!(s));
            }
            out.out("chat-done", p);
            Ok(())
        }
        End::Failed(msg) => {
            // 壊れた子は使い回さない。次の依頼で作り直す。
            let log = lock(&s.log).clone();
            kill_child(s);
            let detail = log
                .map(|p| {
                    let t = log_tail(&p, 1200);
                    if t.is_empty() { String::new() } else { format!("\nログの末尾:\n{t}") }
                })
                .unwrap_or_default();

            if fold.any_text || meter.total() > 0 {
                // ここまでの答えは捨てない。理由を本文に足して、正規の終端で閉じる。
                line_note(&out, &format!("途中で止まりました: {msg}"));
                out.out("chat-done", done_payload(&meter, &usage));
                Ok(())
            } else {
                // 1文字も出せていない。呼び出し側に理由ごと返す（chat-done は出さない）。
                Err(format!("{msg}{detail}"))
            }
        }
    }
}

/// 2つの場所が同じ場所を指しているか。正準化できないときは文字列で比べる
/// （比べられないことを「違う」と断定して、無意味な警告を出さないため）。
fn same_dir(a: &Path, b: &Path) -> bool {
    match (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
        (Ok(x), Ok(y)) => x == y,
        _ => a == b,
    }
}

/// アプリを閉じるときに呼ぶ。**必ず殺す。**
///
/// ★同期関数なのは、`RunEvent::Exit` のコールバックが同期だから。
/// 手順は「終わってくれと伝える → 少し待つ → 強制終了」。いきなり TerminateProcess を
/// 呼ぶと Python は後始末（worktree の片付けなど）ができない。
///
/// ⚠ **孫プロセスはこれでは死なない**（Windows では親が死んでも子は残る）。
///   確実に道連れにするには Job Object が要る＝冒頭の注記のとおり別の仕事。
#[allow(dead_code)] // 統合前（core_available と同じ理由）
pub fn shutdown() {
    // stdin を閉じる＝「もう依頼は来ない」の合図。実行中なら握れないので、その時は飛ばす。
    // ★器は 3 本（2026-10-30・走行の器）。全部閉じる。
    for s in slots().iter() {
        if let Ok(mut g) = s.io.try_lock() {
            *g = None; // ChildStdin が落ちてパイプが閉じる
            if child_alive(s) {
                std::thread::sleep(SHUTDOWN_GRACE);
            }
        }
        kill_child(s);
    }
    *lock(&sc().avail) = None;
}

// ---------------------------------------------------------------------------
// 試験（★プロセスを起こさない純粋関数だけ。この Linux 機では実行できない＝§7 の制約。
//   ここに書いてあるのは「Windows 実機か、同じ論理を写した検証用 crate で走る」もの。）
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(r: &mut LineReader, s: &[u8]) -> Vec<Chunk> {
        r.push(s)
    }

    /// 1バイトずつ届いても、日本語が壊れない。
    /// ★これが壊れると「文字化けした JSON」＝全行読み飛ばし、という静かな死に方をする。
    #[test]
    fn 行分割は多バイト文字の途中で割れても壊れない() {
        let src = r#"{"type":"token","payload":{"text":"作業ディレクトリを読みます"}}"#;
        let mut r = LineReader::new(MAX_LINE_BYTES);
        let mut got = Vec::new();
        for b in src.as_bytes() {
            got.extend(lines(&mut r, &[*b]));
        }
        assert!(got.is_empty(), "改行が来るまでは1行も出ない");
        got.extend(lines(&mut r, b"\n"));
        assert_eq!(got, vec![Chunk::Line(src.to_string())]);
    }

    #[test]
    fn 改行の作法が違っても同じ1行になる() {
        let mut r = LineReader::new(MAX_LINE_BYTES);
        assert_eq!(r.push(b"a\r\nb\n"), vec![Chunk::Line("a".into()), Chunk::Line("b".into())]);
        // 空行は落とす（SSE の区切りと同じ扱い）
        assert_eq!(r.push(b"\n  \n"), vec![]);
    }

    /// 上限を超える行は捨てるが、**捨てたことが分かり**、次の行から復帰する。
    #[test]
    fn 大きすぎる行を捨てても次の行は読める() {
        let mut r = LineReader::new(64);
        let mut got = r.push(&vec![b'x'; 200]);
        assert!(got.is_empty(), "改行が来るまでは何も返さない");
        got.extend(r.push(b"\n{\"type\":\"token\"}\n"));
        match &got[0] {
            Chunk::Oversized(n) => assert!(*n >= 200, "捨てた量が分かる: {n}"),
            other => panic!("捨てた報告が要る: {other:?}"),
        }
        assert_eq!(got[1], Chunk::Line("{\"type\":\"token\"}".into()));
        assert_eq!(got.len(), 2);
    }

    /// 誤爆しないこと。上限ちょうどの行は捨てない。
    #[test]
    fn 上限ちょうどの行は捨てない() {
        let mut r = LineReader::new(64);
        let body: String = "y".repeat(64);
        let got = r.push(format!("{body}\n").as_bytes());
        assert_eq!(got, vec![Chunk::Line(body)]);
    }

    /// 書きかけを持ち越すと次の1行目が壊れる。捨ててから読み始める。
    #[test]
    fn 書きかけは次の依頼へ持ち越さない() {
        let mut r = LineReader::new(MAX_LINE_BYTES);
        assert!(r.push(b"{\"type\":\"tok").is_empty());
        assert_eq!(r.discard_partial(), 12); // 捨てた量も分かる
        assert_eq!(
            r.push(b"{\"type\":\"ready\"}\n"),
            vec![Chunk::Line("{\"type\":\"ready\"}".into())]
        );
    }

    /// 画面に出す引数は切るが、**切ったことが分かる**。短いものは触らない（誤爆しない）。
    #[test]
    fn 長い引数だけを印つきで切る() {
        let long = "あ".repeat(ARG_STRING_MAX + 10);
        let v = clamp_args(&json!({"rel":"src/main.rs","content":long}));
        assert_eq!(v["rel"], json!("src/main.rs"), "短い値はそのまま");
        let c = v["content"].as_str().unwrap();
        assert!(c.contains("（全 210 文字）"), "元の長さが残る: {c}");
        assert!(c.chars().count() < ARG_STRING_MAX + 20);
    }

    #[test]
    fn 抜粋は日本語でも壊れない() {
        let s = preview(&format!("全角\u{3000}空白\n改行{}", "長".repeat(400)));
        assert!(s.ends_with('…'));
        assert!(s.contains('\u{3000}'), "全角空白を潰さない");
        assert!(!s.contains('\n'));
    }

    /// 画面の「消費」表示が読む鍵を固定する（app.js の fmtStat）。
    #[test]
    fn 終了時の数値は画面が読む鍵を持つ() {
        let m = crate::think::Meter::default();
        let v = done_payload(&m, &Some(json!({"prompt_tokens": 7})));
        for k in ["answer_tokens", "think_tokens", "tok_s", "secs", "usage", "prompt_tokens"] {
            assert!(v.get(k).is_some(), "{k} が要る");
        }
        assert_eq!(v["prompt_tokens"], json!(7));
    }

    /// 依頼の1行は JSON として読め、Windows のパスがそのまま載る。
    // -- 依頼 id で選り分ける（2026-09-02 監査）-----------------------------
    //
    // ★下の3本は**実トレースから取った本物の行**を使う（scratch/vdirtest/run44）。
    //   手で書いた形を試すと、送り手が実際に送っている形とずれても気づけない。

    /// 本番と同じ判定を使って選り分ける。run_loop の `if !belongs_to(..) { continue }`
    /// と**同じ関数**を呼ぶ（試験だけの判定を書かない）。
    fn 選り分け<'a>(lines: &[&'a str], id: &str) -> Vec<&'a str> {
        lines
            .iter()
            .filter(|l| {
                serde_json::from_str::<Value>(l).map(|v| belongs_to(&v, id)).unwrap_or(true)
            })
            .copied()
            .collect()
    }

    #[test]
    fn 前の依頼の終端で次の依頼を終わらせない() {
        // r1 が置き去りにした done（道具が idle を超えた／error で抜けた／
        // 「止める」を 3 秒で諦めた、のどれでも起きる）。これを r2 の終端として
        // 読むと、**別の問いの答えが今の問いの答えになる**。
        let 置き去り = r#"{"v":1,"seq":27332,"ts":1788215593.549,"type":"done","id":"r1","outcome":"phase_failed","text":"工程の結果: 4 工程中 3 工程が検収"}"#;
        let 本物 = r#"{"v":1,"seq":5,"ts":1788206791.392,"type":"token","id":"r2","text":"道具"}"#;

        let 残る = 選り分け(&[置き去り, 本物], "r2");
        assert_eq!(残る, vec![本物], "前の依頼の行が残っている");

        // 逆向きも見る（r1 を待っているなら r1 の行が残る）。
        assert_eq!(選り分け(&[置き去り, 本物], "r1"), vec![置き去り]);
    }

    #[test]
    fn 依頼に紐づかない知らせは必ず通す() {
        // 握手と、致命的な失敗は `id` が null で来る（serve.py の Emitter）。
        // ここを落とすと「起動できない理由が1行も出ない」＝沈黙になる。
        for line in [
            r#"{"v":1,"seq":1,"type":"notice","id":null,"kind":"ready","tools":["Bash"]}"#,
            r#"{"v":1,"seq":9,"type":"error","id":null,"kind":"fatal","message":"入力ループが落ちた"}"#,
            r#"{"v":1,"seq":2,"type":"error","id":null,"kind":"bad_request","message":"type が無い"}"#,
        ] {
            let v: Value = serde_json::from_str(line).unwrap();
            assert!(belongs_to(&v, "r7"), "落としてはいけない行を落とした: {line}");
        }
    }

    #[test]
    fn id_を持たない行は落とさない() {
        // 送り手の版が古い／別の封筒（init 等）でも、黙って全部消えないこと。
        let v: Value = serde_json::from_str(r#"{"type":"init","cwd":"C:\\work"}"#).unwrap();
        assert!(belongs_to(&v, "r1"));
    }

    /// 捨てた行が**記録に残る**こと。
    ///
    /// ★run_loop と同じ道筋で作る: 実トレースの行を `belongs_to` に通し、
    ///   落ちた分を数え、その数え物を `skipped_record` に渡す。
    ///   （手で組んだ Map を渡すと、run_loop 側が壊れても試験は緑のままになる。）
    ///
    /// なぜ要るか: `eprintln!` は release では行き先が無い
    /// （main.rs:2 の `windows_subsystem = "windows"`）。混線は黙って起きるので、
    /// 残らなければ「起きたこと自体」に誰も気づけない。
    #[test]
    fn 捨てた行は記録に残る() {
        let 置き去り = r#"{"v":1,"type":"done","id":"r1","outcome":"phase_failed"}"#;
        let 本物 = r#"{"v":1,"type":"token","id":"r2","text":"道具"}"#;

        // run_loop の数え方をそのまま：belongs_to で落ちた行を数える。
        let mut unknown: std::collections::BTreeMap<String, usize> =
            std::collections::BTreeMap::new();
        for line in [置き去り, 本物, 置き去り] {
            let v: Value = serde_json::from_str(line).unwrap();
            if !belongs_to(&v, "r2") {
                *unknown.entry("(別の依頼の行)".to_string()).or_insert(0) += 1;
            }
        }

        let rec = skipped_record(&unknown).expect("捨てたのに控えが無い");
        assert_eq!(rec["skipped"]["(別の依頼の行)"], json!(2), "件数が合わない: {rec}");
    }

    /// 何も捨てていないなら欄を作らない（0 件と「見ていない」を混ぜない）。
    #[test]
    fn 捨てていないなら控えを作らない() {
        let empty = std::collections::BTreeMap::new();
        assert!(skipped_record(&empty).is_none());
    }

    #[test]
    fn 依頼の一行は形が決まっている() {
        let req = build_request(
            "r1",
            "qwen3",
            &[json!({"role":"user","content":"直して"})],
            Path::new(r"C:\work\x"),
            true,
            None,          // ★会話モードではない（従来どおり設定の道具一式）
            None,          // ★作業のモードの指定なし（設定の値・既定 general）
        );
        let s = req.to_string();
        let back: Value = serde_json::from_str(&s).unwrap();
        assert_eq!(back["type"], json!("run"));
        assert_eq!(back["id"], json!("r1"));
        assert_eq!(back["model"], json!("qwen3"));
        assert_eq!(back["workspace"], json!(r"C:\work\x"));
        assert_eq!(back["messages"][0]["role"], json!("user"));
        assert!(!s.contains('\n'), "1行に収まる");
    }
}
