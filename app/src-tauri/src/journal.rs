//! journal.rs — 会話1往復ぶんの記録を **まず手元に書き**、**背景でサーバへ送る**。
//!
//! ## なぜ要るか（オーナー要求 2026-08-11）
//!
//! 「デバッグログとか会話ログをサーバ側に記録して、トラブルシューティングやアップグレードに
//! つなげたい。内部思考からツール使用、タスク成功率等も残しておきたい」。
//! 記録先は利用者が `telemetry_url` に書いた自前のサーバだけ（既定は未設定＝送らない）。**認証情報は送らない**。
//!
//! ## 破ってはいけない4つ（すべて、この木で実際に踏んだ失敗から来ている）
//!
//! 1. **作業を止めない。** [`write`] はネットワークに一切触らない。追記して帰るだけ。
//!    送るのは背景の1本（[`start`] が立てる）で、サーバが落ちていようが会話は進む。
//! 2. **機微を送らない。** 伏字は **Python の `laps.redact`** に通す。
//!    Rust 側に正規表現を書き写さない（sidecar.rs 冒頭と同じ理由。2026-08-11 に、
//!    両側を別々に書いたせいで同じ食い違いを3回踏んでいる）。
//!    ★**濾し器を通せないときは送らない**（fail-closed）。手元には残るので記録は失われない。
//! 3. **できると書いて、できないのが最悪。** 画面に出す文字列は [`journal_status`] が
//!    実際の状態から組み立てる。「送信中」などの固定文言をどこにも置かない。
//! 4. **静かな取りこぼしを作らない。** 落とした文字数・捨てた日・送れなかった量を全部数え、
//!    記録自身の `limits` と [`journal_status`] の両方に出す。
//!
//! ## 置き場所と形
//!
//! ```text
//! <app_root>/logs/2026-08-11.jsonl   1行1レコード（現地日付で1日1本）
//! <app_root>/logs/.shipped.json      どこまで送ったか（日付→バイト位置）
//! <app_root>/logs/.install_id        この導入を表す 16hex（更新をまたいで不変）
//! <app_root>/logs/.salt              host_id を作るときの塩。**送信物には入らない**
//! ```
//!
//! レコードの形（`rec`=1）は別担当がまとめた定義に従う。このファイルが自分で決めるのは
//! **封筒**（`rec` / `id` / `ts` / `ts_mono_ms` / `client` / `limits`）だけで、
//! 中身の各塊（`engine` `run` `input` `output` `think` `tools` `cost` `stop` `quality`
//! `counters` `protocol` `ref`）は呼ぶ側（sidecar.rs）が組んで [`Turn`] に載せる。
//! ★ここで中身を組み立てないのは、`tool_result` の `ok` の意味や `done.counters` の由来を
//!   知っているのは向こうだからで、知らない側が推測で埋めると必ず嘘になる。
//!
//! ## 送り方
//!
//! 追記 → （背景）伏字 → gzip → `POST <telemetry_url>`。1回の POST は
//! **圧縮前 1 MiB / 500 行**まで。送れた分だけ位置を進める。途中で失敗したら位置は
//! 進めないので、次の機会に同じところから送り直す（サーバは `id` で重複を捨てる約束）。
//!
//! ## 止め方
//!
//! ```toml
//! telemetry = "off"          # 何も書かない・送らない
//! telemetry_url = ""         # 手元には書く。送らない
//! ```
//!
//! ## やっていないこと（次の人が誤解しないように）
//!
//! * **レコードごとの伏字件数は数えていない。** 濾し器（`laps.redact --jsonl`）が
//!   返すのは1回ぶんの合計だけで、行ごとの内訳を出さない。行に印の数を数えて割り当てる案は
//!   採らなかった（印の書式を Rust 側にもう1つ持つことになり、書式が変わった日に
//!   **黙って 0 件と数える**）。合計は HTTP ヘッダと [`journal_status`] に出す。
//! * **暗号強度の乱数を使っていない。** `install_id` / 塩は「別の導入と衝突しない」ための物で、
//!   秘密ではない（`host_label` は元々そのまま送る）。
//! * 送信の再試行に上限を置いていない。手元の上限（日数・合計バイト）に当たって
//!   古い日が消えるときが、実質の打ち切りになる。**消したら数える。**

use std::io::{Seek, SeekFrom, Write as _};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

/// 封筒の組み立て・ladder・時刻書式・素性のハッシュ計算・送信バッチ計画・
/// 古い日の削除計画・gzip・行の切り出しの実体。★2026-09-05
/// （r4/tests-never-run）にここへ抜いた: このファイルは
/// `tauri::async_runtime::spawn` と `#[tauri::command]` を使うのでホスト
/// （Linux）では `cargo test` がリンクできず（GTK 依存）、29 本の試験のうち
/// 28 本が preflight でも `cargo check --tests`＝**コンパイルされるだけで
/// 一度も実行されていなかった**。`#[path]` で参照しているだけなので実体は1本
/// （`wire.rs`/`translate.rs`/`workspace_guard.rs` と同じ作法）。
/// `dir()`/`crate::app_root()` に依存する `stable_hex_file`/`install_id`/
/// `config` 等はここには移していない（journal_format 側から `crate::` は呼べない）。
#[path = "../blackwall-core/src/journal_format.rs"]
mod journal_format;
use journal_format::{
    assemble, boot, day_of, epoch_millis, epoch_secs, gzip, hex, host_id_with_salt, new_id,
    plan_batch, plan_evict, read_full, reads_as_off, rfc3339, scan_to_newline, split_lines,
    weak_random_16, MAX_RECORD_BYTES, REC_VERSION,
};
pub use journal_format::Turn;

// ---------------------------------------------------------------------------
// 定数
// ---------------------------------------------------------------------------

/// 1日1クライアントぶんの上限。超えたら封筒だけに劣化させ、**記録は止めない**
/// （沈黙が最悪。「その日は静かだった」と「その日は溢れた」を区別できなくなる）。
const DAY_HARD_BYTES: u64 = 64 * 1024 * 1024;
/// 思考の全文を分ける大きさ（字）。日本語でも 1 行 180 KB 程度＝受け口の 1 行の上限（256 KiB）に収まる。
const THINK_PART_CHARS: usize = 60_000;
/// 同、目安。越えたことを状態に出すだけ（削らない）。
const DAY_SOFT_BYTES: u64 = 32 * 1024 * 1024;

/// 手元に置く合計の既定上限（MiB）と保存日数。`telemetry_max_mb` / `telemetry_keep_days` で変えられる。
const DEFAULT_LOCAL_MAX_MB: u64 = 512;
const DEFAULT_KEEP_DAYS: usize = 30;

/// 1回の POST に載せる上限（圧縮前）。
const MAX_POST_BYTES: usize = 1024 * 1024;
const MAX_POST_LINES: usize = 500;
/// 1周で送る POST の回数。溜まっていても、ここで一度手を離す
/// （背景とはいえ、10 GB を一息に読み続けるとディスクを占有する）。
const MAX_BATCHES_PER_PASS: usize = 20;


/// 何も起きていないときに様子を見にいく間隔。前回の残りを拾う役目もある。
const IDLE_POLL: Duration = Duration::from_secs(30);
/// 失敗したあと、次に試すまで（秒）。最後の値で頭打ち。
const BACKOFF_SECS: [u64; 5] = [5, 15, 60, 300, 900];

/// 濾し器（Python）を待つ上限。
const REDACT_TIMEOUT: Duration = Duration::from_secs(60);
/// POST を待つ上限。
const POST_TIMEOUT: Duration = Duration::from_secs(30);

// ---------------------------------------------------------------------------
// 設定
// ---------------------------------------------------------------------------

/// 記録の設定。**毎回読む**（設定を直したら再起動なしで効く）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// 記録そのものを行うか。`telemetry = "off"` で偽。
    pub enabled: bool,
    /// 送り先。`None` なら送らない（手元には残る）。
    pub url: Option<String>,
    /// 送り先が `None` の理由（画面に出すため。**推測で文言を書かないための材料**）。
    pub url_reason: &'static str,
    pub host_label: String,
    pub max_bytes: u64,
    pub keep_days: usize,
}

/// `config.toml` を1行ずつ読む。
///
/// ★`crate::config_value` を使わない理由はひとつだけ。あちらは **値が空の行を
/// 「書かれていない」と同じ扱いにする**（`if v.is_empty() { return None }`）。
/// この記録では `telemetry_url = ""` が「送るな」という意思表示なので、
/// 「書かれていない（＝既定へ）」と区別できないと**止めたつもりが送り続ける**。
/// 危ないほうへ倒れる違いなので、ここだけは自分で読む。
pub(crate) fn config_raw(key: &str) -> Option<String> {
    let text = std::fs::read_to_string(crate::config_path()).ok()?;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with('[') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else { continue };
        if k.trim() != key {
            continue;
        }
        // 行末コメントは切らない（値に # を含む URL があり得る）。
        let v = v.trim().trim_matches('"').trim_matches('\'').trim();
        return Some(v.to_string());
    }
    None
}

// ★`reads_as_off` の実体は `journal_format` にある（冒頭の `use` で持ち込み済み）。

/// いまの設定。
pub fn config() -> Config {
    // 環境変数が最優先（開発と、実機での切り分けのため）。
    let env_off = crate::envcompat::var("LAPS_TELEMETRY").map(|v| reads_as_off(&v)).unwrap_or(false);
    let enabled = !env_off && !config_raw("telemetry").map(|v| reads_as_off(&v)).unwrap_or(false);

    let (url, url_reason) = match crate::envcompat::var("LAPS_TELEMETRY_URL") {
        Ok(v) if v.trim().is_empty() => (None, "LAPS_TELEMETRY_URL が空です"),
        Ok(v) => (Some(v.trim().to_string()), ""),
        Err(_) => match config_raw("telemetry_url") {
            // ★空文字は「書かれていない」ではなく「送るな」。
            Some(v) if v.trim().is_empty() => (None, "config.toml の telemetry_url が空です"),
            Some(v) => (Some(v.trim().to_string()), ""),
            None => (None, "telemetry_url が未設定です（送り先は設定した人だけが決める）"),
        },
    };
    let (url, url_reason) = if enabled { (url, url_reason) } else { (None, "telemetry = off") };

    Config {
        enabled,
        url,
        url_reason,
        host_label: config_raw("telemetry_host_label")
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(hostname),
        max_bytes: config_raw("telemetry_max_mb")
            .and_then(|v| v.trim().parse::<u64>().ok())
            .filter(|m| *m > 0)
            .unwrap_or(DEFAULT_LOCAL_MAX_MB)
            .saturating_mul(1024 * 1024),
        keep_days: config_raw("telemetry_keep_days")
            .and_then(|v| v.trim().parse::<usize>().ok())
            .filter(|d| *d > 0)
            .unwrap_or(DEFAULT_KEEP_DAYS),
    }
}

fn hostname() -> String {
    for k in ["COMPUTERNAME", "HOSTNAME"] {
        if let Ok(v) = std::env::var(k) {
            if !v.trim().is_empty() {
                return v.trim().to_string();
            }
        }
    }
    std::fs::read_to_string("/etc/hostname")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

// ---------------------------------------------------------------------------
// 置き場所
// ---------------------------------------------------------------------------

/// 記録の置き場。`LAPS_JOURNAL_DIR` で移せる（実機で1回だけ試すときのため）。
pub fn dir() -> PathBuf {
    if let Some(d) = crate::envcompat::var_os("LAPS_JOURNAL_DIR") {
        return PathBuf::from(d);
    }
    crate::app_root().join("logs")
}

fn cursor_path() -> PathBuf {
    dir().join(".shipped.json")
}

// ---------------------------------------------------------------------------
// 数え（画面に出すのはここに溜まった実測だけ）
// ---------------------------------------------------------------------------

macro_rules! counters {
    ($($name:ident),* $(,)?) => {
        $( static $name: AtomicU64 = AtomicU64::new(0); )*
    };
}
counters!(
    N_WRITTEN,          // 書けたレコード
    N_WRITE_BYTES,      // 書いたバイト
    N_WRITE_FAILED,     // 書けなかったレコード（★これが 0 でないなら記録は当てにならない）
    N_LADDER,           // 削って収めたレコード
    N_HEADER_ONLY,      // 封筒だけに劣化させたレコード
    N_SENT_RECORDS,
    N_SENT_BATCHES,
    N_SENT_BYTES,       // 圧縮後
    N_SEND_FAILED,      // POST が通らなかった回数
    N_REDACT_FAILED,    // 濾し器を通せず**送るのをやめた**回数
    N_REDACTIONS,       // 伏せた箇所の累計（濾し器の自己申告）
    N_EVICTED_DAYS,
    N_EVICTED_BYTES,
    N_EVICTED_UNSENT,   // ★送れないまま消したバイト。0 でないなら記録に穴がある
    N_SKIPPED_BYTES,    // 行として読めず飛ばしたバイト
);

/// 直近の失敗（人の言葉）。★理由を捨てると、画面には「送れていません」しか出せない。
static LAST_ERROR: Mutex<Option<(i64, String)>> = Mutex::new(None);
/// 最後に送れた時刻（epoch 秒）。0 なら一度も送れていない。
static LAST_OK: AtomicI64 = AtomicI64::new(0);
/// 伏字の種類別の累計。
static REDACT_KINDS: Mutex<Option<std::collections::BTreeMap<String, u64>>> = Mutex::new(None);

fn note_error(msg: impl Into<String>) {
    let msg = msg.into();
    eprintln!("[blackwall] journal: {msg}");
    if let Ok(mut g) = LAST_ERROR.lock() {
        *g = Some((epoch_secs(), msg));
    }
}

// ★`epoch_secs` / `epoch_millis` / `boot` / `day_of` / `rfc3339` の実体は
//   `journal_format`（冒頭の `use` で持ち込み済み）にある。

// ---------------------------------------------------------------------------
// 時刻（書式は自前。ずれだけ time クレートから貰う）
// ---------------------------------------------------------------------------

/// 現地時刻のずれ（秒）。取れなければ 0＝UTC。
///
/// ★`+00:00` と出ることが「取れなかった」の表明でもある（黙って現地時刻のふりをしない）。
/// Unix の多スレッドでは time クレートが安全側に倒れて失敗する。配布先は Windows で、
/// そこでは取得できる。
fn local_offset_secs() -> i32 {
    time::UtcOffset::current_local_offset().map(|o| o.whole_seconds()).unwrap_or(0)
}

// ---------------------------------------------------------------------------
// 素性（install_id / host_id / レコードの id）
//
// ★`weak_random_16` / `sha256` / `hex` / `new_id` / `base36` の実体は
//   `journal_format` にある（ファイルに触れない純粋な計算のため）。
//   ここに残るのは `crate::app_root()` に依存するファイル I/O（`.install_id`/
//   `.salt` の読み書き）だけ。
// ---------------------------------------------------------------------------

/// 中身が無ければ作って、あればそのまま読む小さなファイル。
///
/// ★`create_new`（O_EXCL 相当）で作る。同時に2つ立ち上がったときに
/// 「両方が別の id を書いて、後から書いたほうが勝つ」を起こさないため。
fn stable_hex_file(name: &str) -> String {
    let p = dir().join(name);
    if let Ok(s) = std::fs::read_to_string(&p) {
        let s = s.trim().to_string();
        if s.len() >= 16 && s.chars().all(|c| c.is_ascii_hexdigit()) {
            return s;
        }
    }
    let v = hex(&weak_random_16());
    let _ = std::fs::create_dir_all(dir());
    match std::fs::OpenOptions::new().write(true).create_new(true).open(&p) {
        Ok(mut f) => {
            let _ = f.write_all(v.as_bytes());
            v
        }
        // 競争に負けた or 書けない。負けた側は相手の値を読む（読めなければ自分の値で続ける）。
        Err(_) => std::fs::read_to_string(&p)
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| s.len() >= 16)
            .unwrap_or(v),
    }
}

fn install_id() -> String {
    static V: OnceLock<String> = OnceLock::new();
    V.get_or_init(|| stable_hex_file(".install_id")).clone()
}

/// ★ハッシュ計算そのもの（[`host_id_with_salt`]）は `journal_format` にある。
/// ここでは塩の読み書き（`crate::app_root()` 経由）だけを行う。
fn host_id(label: &str) -> String {
    static SALT: OnceLock<String> = OnceLock::new();
    let salt = SALT.get_or_init(|| stable_hex_file(".salt"));
    host_id_with_salt(salt, label)
}

// ★`Turn` / `Dropped` / `clamp_chars` / `clamp_all_strings` / `shrink_text` /
//   `for_each_tool` / `assemble` / `finalize` / `digits` / `clamp_strings_to` /
//   `push_flag` の実体は `journal_format` にある（冒頭の `use` と
//   `pub use journal_format::Turn;` で持ち込み済み）。

fn client_base(cfg: &Config) -> Value {
    let label = cfg.host_label.clone();
    json!({
        "app_version": env!("CARGO_PKG_VERSION"),
        // ★本体の版と protocol は線の向こうしか知らない。埋められないものを名乗らない。
        //   呼ぶ側が Turn::client に入れれば上書きされる。
        "core_version": Value::Null,
        "protocol": Value::Null,
        "os": format!("{} {}", std::env::consts::OS, std::env::consts::ARCH),
        "host_id": host_id(&label),
        "host_label": label,
        "install_id": install_id(),
    })
}

// ---------------------------------------------------------------------------
// 書く
// ---------------------------------------------------------------------------

/// 1往復ぶんを記録する。
///
/// ★**ネットワークに触らない。** やるのは「今日のファイルへ1行足す」だけで、
/// 送るのは背景の1本（[`start`]）。サーバが落ちていても、この呼び出しは同じ速さで帰る。
///
/// 呼ぶ場所は **終端イベント（`chat-done` / `chat-error`）を出したあと**。
/// 先に呼ぶと、追記の分だけ画面の応答が遅れる。
///
/// 実測（同じ最適化設定＝`opt-level="s"` + LTO、300 回の平均、この Linux 機）:
///
/// | 1行の大きさ | 追記1回 |
/// |---|---|
/// | 6.9 KB（ふつう） | 93 µs |
/// | 15 KB | 55 µs |
/// | 30 KB（ladder が6段とも走る） | 153 µs |
///
/// ⚠ **debug ビルドでは 8〜18 倍かかる**（30 KB で 2.8 ms）。開発中に「遅い」と見えても、
///   配布物の数字ではない。測るときは `--release` で。
#[allow(dead_code)] // 呼び出し側を繋ぐのは別担当・別コミット
pub fn write(turn: Turn) {
    let cfg = config();
    if !cfg.enabled {
        return;
    }
    let d = dir();
    if let Err(e) = std::fs::create_dir_all(&d) {
        N_WRITE_FAILED.fetch_add(1, Ordering::Relaxed);
        note_error(format!("ログの保存先を作れません（{}）: {e}", d.display()));
        return;
    }

    let now_ms = epoch_millis();
    let offset = local_offset_secs();
    let path = d.join(format!("{}.jsonl", day_of(now_ms.div_euclid(1000), offset)));

    // その日の量。★上限を超えていたら封筒だけに劣化させる（止めない）。
    let day_bytes = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    let header_only = day_bytes >= DAY_HARD_BYTES;

    // ★思考の全文は別の行（kind="think"）で残す（2026-10-12 オーナー「Think の内容もサーバー側のログに含めるようにしてください」）。
    //   本体の記録は 32 KiB に収めるため、思考を 8,192 字で切り、溢れれば 512 字まで削る（journal_format の ladder の 1 段目）。
    //   長い走行は 7〜18 万字の思考が 512 字しか残っていなかった。受け口は 1 行 256 KiB・1 日 64 MiB まで受ける。
    let think_full: String = turn.think.get("text").and_then(Value::as_str).unwrap_or("").to_string();
    let id = new_id(now_ms);
    let (rec, _d) = assemble(
        turn,
        now_ms,
        offset,
        id.clone(),
        boot().elapsed().as_millis() as u64,
        client_base(&cfg),
        header_only,
    );

    let mut line = rec.to_string();
    line.push('\n');
    let kept = rec.get("think").and_then(|t| t.get("text")).and_then(Value::as_str).map(|t| t.chars().count()).unwrap_or(0);
    let total = think_full.chars().count();
    if !header_only && total > kept {
        let chars: Vec<char> = think_full.chars().collect();
        let parts = chars.chunks(THINK_PART_CHARS).count();
        for (k, ch) in chars.chunks(THINK_PART_CHARS).enumerate() {
            let part = json!({
                "rec": REC_VERSION,
                "kind": "think",
                "id": format!("{id}.t{}", k + 1),
                "run": id,
                "ts": rec.get("ts").cloned().unwrap_or(Value::Null),
                "client": rec.get("client").cloned().unwrap_or(Value::Null),
                "part": k + 1,
                "parts": parts,
                "chars": total,
                "text": ch.iter().collect::<String>(),
            });
            line.push_str(&part.to_string());
            line.push('\n');
        }
    }

    // 追記。★1回の write_all で出す。途中で落ちても、次の行が続けて書ける
    //   （半端な行は改行が来た時点で「JSON でない1行」になり、濾し器も通す）。
    let r = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .and_then(|mut f| f.write_all(line.as_bytes()));

    match r {
        Ok(()) => {
            N_WRITTEN.fetch_add(1, Ordering::Relaxed);
            N_WRITE_BYTES.fetch_add(line.len() as u64, Ordering::Relaxed);
            if header_only {
                N_HEADER_ONLY.fetch_add(1, Ordering::Relaxed);
            }
            if _d.stage > 0 {
                N_LADDER.fetch_add(1, Ordering::Relaxed);
            }
            wake();
        }
        Err(e) => {
            N_WRITE_FAILED.fetch_add(1, Ordering::Relaxed);
            note_error(format!("記録を書けません（{}）: {e}", path.display()));
        }
    }
}

// ---------------------------------------------------------------------------
// 手元の掃除（古い日から消す）
// ---------------------------------------------------------------------------

/// 日付ファイルの一覧（名前, バイト数）。名前の昇順＝古い順。
fn list_days() -> Vec<(String, u64)> {
    let mut v: Vec<(String, u64)> = Vec::new();
    let Ok(rd) = std::fs::read_dir(dir()) else { return v };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        let Some(day) = name.strip_suffix(".jsonl") else { continue };
        // `YYYY-MM-DD` だけを見る（他人が置いたファイルを消さない）。
        if day.len() != 10 || !day.chars().all(|c| c.is_ascii_digit() || c == '-') {
            continue;
        }
        let bytes = e.metadata().map(|m| m.len()).unwrap_or(0);
        v.push((day.to_string(), bytes));
    }
    v.sort();
    v
}

// ★`plan_evict` の実体は `journal_format` にある（冒頭の `use` で持ち込み済み）。

/// 上限に従って古い日を消す。**消したものは必ず数える**（特に、送れないまま消した量）。
fn rotate(cfg: &Config, today: &str, cursor: &Cursor) {
    let days = list_days();
    for day in plan_evict(&days, cfg.max_bytes, cfg.keep_days, today) {
        let bytes = days.iter().find(|(d, _)| *d == day).map(|(_, b)| *b).unwrap_or(0);
        let sent = cursor.get(&day);
        let path = dir().join(format!("{day}.jsonl"));
        if std::fs::remove_file(&path).is_err() {
            continue;
        }
        N_EVICTED_DAYS.fetch_add(1, Ordering::Relaxed);
        N_EVICTED_BYTES.fetch_add(bytes, Ordering::Relaxed);
        let unsent = bytes.saturating_sub(sent);
        if unsent > 0 {
            // ★ここが 0 でないなら、サーバ側の記録には穴がある。黙って消さない。
            N_EVICTED_UNSENT.fetch_add(unsent, Ordering::Relaxed);
            note_error(format!("{day} を上限のため消しました。未送信の {unsent} バイトが失われました"));
        }
    }
}

// ---------------------------------------------------------------------------
// どこまで送ったか
// ---------------------------------------------------------------------------

/// 日付 → 送信済みバイト位置。
#[derive(Debug, Default, Clone)]
struct Cursor(std::collections::BTreeMap<String, u64>);

impl Cursor {
    fn load() -> Self {
        let Ok(text) = std::fs::read_to_string(cursor_path()) else { return Self::default() };
        let Ok(v) = serde_json::from_str::<Value>(&text) else {
            // 壊れていたら 0 から送り直す。サーバは id で重複を捨てる約束なので害は無い。
            return Self::default();
        };
        let mut m = std::collections::BTreeMap::new();
        if let Some(o) = v.get("days").and_then(|d| d.as_object()) {
            for (k, x) in o {
                if let Some(n) = x.as_u64() {
                    m.insert(k.clone(), n);
                }
            }
        }
        Self(m)
    }

    fn get(&self, day: &str) -> u64 {
        self.0.get(day).copied().unwrap_or(0)
    }

    fn set(&mut self, day: &str, at: u64) {
        self.0.insert(day.to_string(), at);
    }

    /// 消えた日の控えを落とす（際限なく増やさない）。
    fn retain_days(&mut self, days: &[(String, u64)]) {
        self.0.retain(|k, _| days.iter().any(|(d, _)| d == k));
    }

    fn save(&self) {
        let body = json!({ "v": 1, "days": self.0 }).to_string();
        let p = cursor_path();
        let tmp = p.with_extension("json.tmp");
        // 一時ファイル→置き換え。半端な控えを残すと、次回に**送った分をもう一度**送る
        //（害は無いが、サーバ側で重複が増えて調査が濁る）。
        if std::fs::write(&tmp, body.as_bytes()).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }
}

// ---------------------------------------------------------------------------
// 送る
// ---------------------------------------------------------------------------

// ★`plan_batch` の実体は `journal_format` にある（冒頭の `use` で持ち込み済み）。

/// ファイルの `from` から最大 `window` バイト読み、**完全な行だけ**を返す。
///
/// 返すのは (行の並び, 次に読む位置)。行には **その行の直後のファイル位置** を添える。
/// ★文字列の長さから位置を計算し直さない。`str::lines()` は `\r` を落とし、
///   空行はこちらが読み飛ばすので、`len()+1` を足していくと位置が**静かにずれる**。
///   ずれた位置から読み直すと、行の途中から送り始めて壊れた JSON をサーバへ渡す。
///   位置は読んだときに確定させて、そのまま持ち回る。
///
/// 改行で終わっていない末尾は含めない（書きかけの行を送ると、サーバ側に壊れた JSON が1件増える）。
fn read_complete_lines(
    path: &Path,
    from: u64,
    window: usize,
) -> std::io::Result<(Vec<(String, u64)>, u64)> {
    let mut f = std::fs::File::open(path)?;
    let len = f.metadata()?.len();
    if from >= len {
        return Ok((Vec::new(), from));
    }
    f.seek(SeekFrom::Start(from))?;
    let mut buf = vec![0u8; window.min((len - from) as usize)];
    let n = read_full(&mut f, &mut buf)?;
    buf.truncate(n);

    let Some(last) = buf.iter().rposition(|b| *b == b'\n') else {
        // 窓の中に改行が1つも無い。
        if from + n as u64 >= len {
            // 末尾が書きかけ。次の追記で改行が来るので、いまは待つ。
            return Ok((Vec::new(), from));
        }
        // 続きがあるのに改行が無い＝1行が窓より大きい。飛ばさないと列が止まるので、
        // 次の改行まで読み飛ばして**飛ばしたバイト数を数える**。
        let skipped = scan_to_newline(&mut f, from + n as u64, len)?;
        N_SKIPPED_BYTES.fetch_add(skipped.saturating_sub(from), Ordering::Relaxed);
        note_error(format!(
            "{} の {from} バイト目から {} バイトを飛ばしました（1行が {window} バイトを超えています）",
            path.display(),
            skipped.saturating_sub(from)
        ));
        return Ok((Vec::new(), skipped));
    };

    Ok((split_lines(&buf[..=last], from), from + last as u64 + 1))
}

// ★`split_lines` / `read_full` / `scan_to_newline` / `gzip` の実体は
//   `journal_format` にある（冒頭の `use` で持ち込み済み）。

/// 濾し器（Python）の実体。
///
/// ★sidecar.rs の `resolve_python()` は private で、公開するにはあちらを触ることになる
/// （いま別担当が同じファイルを触っている）。ここは探す先を3つに絞って自分で持つ。
/// **見つからなければ送らない**ので、この重複が漏えいの原因になることはない。
fn resolve_python() -> Result<PathBuf, String> {
    if let Ok(p) = crate::envcompat::var("LAPS_PYTHON") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Ok(p);
        }
    }
    // ★本体（sidecar.rs）と同じ Python を使う（2026-11-14）。前はアプリの置き場の写し（pyruntime）を先に見ていて、
    //   作り直されない古い写しを拾い、`laps` が無くて濾し器が落ち続けた（走行記録が 1 件も届かない）。
    crate::sidecar::runtime_python()
}

/// 伏字の結果。
struct Redacted {
    lines: Vec<String>,
    count: u64,
    kinds: std::collections::BTreeMap<String, u64>,
}

/// Python の `laps.redact --jsonl` に通す。
///
/// ★**ここが唯一の門。** 判定（何が機微か）は向こうにあり、こちらは値を運ぶだけ。
/// 通せなければ `Err`＝送らない。行数が合わなければ `Err`
/// （黙って減った行があるまま送ると、何が落ちたのか誰にも分からなくなる）。
async fn redact(lines: &[String]) -> Result<Redacted, String> {
    let py = resolve_python()?;
    let cwd = py.parent().map(Path::to_path_buf).unwrap_or_else(std::env::temp_dir);

    let mut cmd = tokio::process::Command::new(&py);
    cmd.arg("-u").arg("-m").arg("laps.redact").arg("--jsonl");
    cmd.current_dir(&cwd)
        // Windows の既定コードページ(cp932)だと日本語で落ちる（sidecar.rs と同じ理由）。
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONSTARTUP")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    match crate::envcompat::var("LAPS_PYAPP") {
        Ok(p) if !p.trim().is_empty() => {
            cmd.env("PYTHONPATH", p);
        }
        _ => {
            cmd.env_remove("PYTHONPATH");
        }
    }
    #[cfg(windows)]
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW

    let mut child = cmd.spawn().map_err(|e| format!("フィルタを起動できません（{}）: {e}", py.display()))?;
    let mut stdin = child.stdin.take().ok_or("フィルタの標準入力を掴めません")?;

    let mut body = lines.join("\n");
    body.push('\n');
    // ★書きながら読む。1 MiB をまとめて書くとパイプが詰まり、
    //   向こうが出力を掃けないまま互いに待つ（古典的な行き詰まり）。
    let writer = tokio::spawn(async move {
        use tokio::io::AsyncWriteExt;
        let _ = stdin.write_all(body.as_bytes()).await;
        let _ = stdin.flush().await;
        drop(stdin);
    });

    let out = tokio::time::timeout(REDACT_TIMEOUT, child.wait_with_output())
        .await
        .map_err(|_| format!("フィルタが {} 秒たっても終わりません", REDACT_TIMEOUT.as_secs()))?
        .map_err(|e| format!("フィルタの実行に失敗しました: {e}"))?;
    let _ = writer.await;

    if !out.status.success() {
        let tail = String::from_utf8_lossy(&out.stderr);
        let tail: String = tail.trim().chars().rev().take(400).collect::<Vec<_>>().into_iter().rev().collect();
        return Err(format!("フィルタが異常終了しました（{}）: {tail}", out.status));
    }

    let text = String::from_utf8_lossy(&out.stdout);
    let got: Vec<String> = text.lines().filter(|l| !l.trim().is_empty()).map(str::to_string).collect();
    if got.len() != lines.len() {
        // ★行数が合わない＝どれかが消えている。送らない（減ったことに気づけないほうが悪い）。
        return Err(format!("フィルタの出力が {} 行（入力は {} 行）", got.len(), lines.len()));
    }

    // 集計は向こうが標準エラーへ1行 JSON で出す。**その言葉をそのまま使う**
    //（こちらで印を数え直すと、印の書式が変わった日に黙って 0 件になる）。
    let mut count = 0u64;
    let mut kinds = std::collections::BTreeMap::new();
    if let Some(last) = String::from_utf8_lossy(&out.stderr).lines().last() {
        if let Ok(v) = serde_json::from_str::<Value>(last) {
            count = v.get("count").and_then(|c| c.as_u64()).unwrap_or(0);
            if let Some(o) = v.get("kinds").and_then(|k| k.as_object()) {
                for (k, n) in o {
                    kinds.insert(k.clone(), n.as_u64().unwrap_or(0));
                }
            }
        }
    }
    Ok(Redacted { lines: got, count, kinds })
}

fn http() -> Result<reqwest::Client, String> {
    static C: OnceLock<reqwest::Client> = OnceLock::new();
    if let Some(c) = C.get() {
        return Ok(c.clone());
    }
    // `no_proxy()` の理由: 相手は私設アドレスのことが多く、
    // システムプロキシを経由させると必ず失敗し、しかも原因が分からなくなる。
    let c = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(3))
        .timeout(POST_TIMEOUT)
        .user_agent(concat!("blackwall-console/", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|e| format!("HTTP クライアントを作れません: {e}"))?;
    let _ = C.set(c.clone());
    Ok(c)
}

/// 1回ぶん送る。送れた行数を返す。
async fn post(url: &str, r: &Redacted) -> Result<usize, String> {
    post_one(url, r).await
}

/// 実際に 1 回送る。
async fn post_one(url: &str, r: &Redacted) -> Result<usize, String> {
    let mut body = r.lines.join("\n");
    body.push('\n');
    let raw = body.len();
    let gz = gzip(body.as_bytes()).map_err(|e| format!("圧縮できません: {e}"))?;

    let kinds = r
        .kinds
        .iter()
        .map(|(k, n)| format!("{k}={n}"))
        .collect::<Vec<_>>()
        .join(",");

    let resp = http()?
        .post(url)
        .header("content-type", "application/x-ndjson")
        .header("content-encoding", "gzip")
        // ★レコード単位の伏字件数は名乗れない（冒頭「やっていないこと」）。
        //   この束ぶんの合計だけをヘッダで伝える。
        .header("x-blackwall-records", r.lines.len().to_string())
        .header("x-blackwall-raw-bytes", raw.to_string())
        .header("x-blackwall-redactions", r.count.to_string())
        .header("x-blackwall-redaction-kinds", kinds)
        .header("x-blackwall-install", install_id())
        .body(gz.clone())
        .send()
        .await
        .map_err(|e| format!("送れません（{url}）: {e}"))?;

    if !resp.status().is_success() {
        let code = resp.status();
        let tail = resp.text().await.unwrap_or_default();
        let tail: String = tail.trim().chars().take(200).collect();
        return Err(format!("サーバーが {code} を返しました: {tail}"));
    }
    N_SENT_BYTES.fetch_add(gz.len() as u64, Ordering::Relaxed);
    Ok(r.lines.len())
}

/// 溜まっているものを、古い日から順に送れるだけ送る。
async fn ship(cfg: &Config) -> Result<(), String> {
    let Some(url) = cfg.url.as_deref() else { return Ok(()) };

    let mut cursor = Cursor::load();
    let days = list_days();
    cursor.retain_days(&days);

    let mut passes = 0usize;
    for (day, size) in days.iter() {
        let path = dir().join(format!("{day}.jsonl"));
        loop {
            if passes >= MAX_BATCHES_PER_PASS {
                cursor.save();
                return Ok(());
            }
            let mut at = cursor.get(day);
            if at > *size {
                // ファイルが縮んだ（作り直された）。★0 から送り直す。
                //   進んだままにすると、その日の記録が丸ごと消える。
                at = 0;
            }
            if at >= *size {
                break;
            }
            let (lines, next) = read_complete_lines(&path, at, MAX_POST_BYTES)
                .map_err(|e| format!("{} を読めません: {e}", path.display()))?;
            if lines.is_empty() {
                if next > at {
                    cursor.set(day, next); // 飛ばした分だけ進める
                    cursor.save();
                    continue;
                }
                break; // 書きかけの行しかない。次の機会に。
            }

            // 束の大きさを決め、余った行は次回に回す。
            // 大きさは**位置の差**から出す（文字列の長さから数え直さない。上の注記を参照）。
            let mut prev = at;
            let sizes: Vec<usize> = lines
                .iter()
                .map(|(_, end)| {
                    let n = end.saturating_sub(prev) as usize;
                    prev = *end;
                    n
                })
                .collect();
            let take = plan_batch(&sizes, MAX_POST_BYTES, MAX_POST_LINES);
            let chunk: Vec<String> = lines[..take].iter().map(|(t, _)| t.clone()).collect();
            // 送り終えたら、**取った最後の行の直後**まで位置を進める。
            let upto = if take == lines.len() { next } else { lines[take - 1].1 };

            // ★門。通せなければ**送らない**。手元には残るので、直せば次に送れる。
            let red = match redact(&chunk).await {
                Ok(r) => r,
                Err(e) => {
                    N_REDACT_FAILED.fetch_add(1, Ordering::Relaxed);
                    cursor.save();
                    return Err(format!("マスク処理ができないため送信しません: {e}"));
                }
            };
            let n = post(url, &red).await.inspect_err(|_| {
                N_SEND_FAILED.fetch_add(1, Ordering::Relaxed);
            })?;

            N_SENT_RECORDS.fetch_add(n as u64, Ordering::Relaxed);
            N_SENT_BATCHES.fetch_add(1, Ordering::Relaxed);
            N_REDACTIONS.fetch_add(red.count, Ordering::Relaxed);
            if let Ok(mut g) = REDACT_KINDS.lock() {
                let m = g.get_or_insert_with(Default::default);
                for (k, v) in red.kinds {
                    *m.entry(k).or_insert(0) += v;
                }
            }
            LAST_OK.store(epoch_secs(), Ordering::Relaxed);

            cursor.set(day, upto);
            cursor.save();
            passes += 1;
        }
    }
    cursor.save();
    Ok(())
}

// ---------------------------------------------------------------------------
// 背景の1本
// ---------------------------------------------------------------------------

fn notify() -> &'static tokio::sync::Notify {
    static N: OnceLock<tokio::sync::Notify> = OnceLock::new();
    N.get_or_init(tokio::sync::Notify::new)
}

fn wake() {
    notify().notify_one();
}

/// 送信中かどうか（[`journal_flush`] と背景が同時に走らないように）。
fn ship_lock() -> &'static tokio::sync::Mutex<()> {
    static M: OnceLock<tokio::sync::Mutex<()>> = OnceLock::new();
    M.get_or_init(|| tokio::sync::Mutex::new(()))
}

static STARTED: AtomicU64 = AtomicU64::new(0);

/// 背景の送り役を1本だけ立てる。main.rs の `setup` から1回呼ぶ。
///
/// ★`tauri::async_runtime::spawn` を使うのは、呼ばれた場所に tokio の実行文脈が
/// あるとは限らないから（`setup` は同期のコールバック）。
pub fn start() {
    if STARTED.swap(1, Ordering::SeqCst) != 0 {
        return; // 二重に立てない
    }
    let _ = boot(); // 原点を確定させる
    tauri::async_runtime::spawn(async {
        let mut fails = 0usize;
        let mut next_try = Instant::now();
        loop {
            let cfg = config();
            let today = day_of(epoch_secs(), local_offset_secs());

            if cfg.enabled {
                // 掃除は送信の可否に関わらず行う（送れない日が続いても手元は溢れない）。
                rotate(&cfg, &today, &Cursor::load());
            }
            if cfg.enabled && cfg.url.is_some() && Instant::now() >= next_try {
                let _g = ship_lock().lock().await;
                match ship(&cfg).await {
                    Ok(()) => {
                        fails = 0;
                        next_try = Instant::now();
                    }
                    Err(e) => {
                        note_error(e);
                        let w = BACKOFF_SECS[fails.min(BACKOFF_SECS.len() - 1)];
                        fails += 1;
                        next_try = Instant::now() + Duration::from_secs(w);
                    }
                }
            }

            tokio::select! {
                _ = notify().notified() => {}
                _ = tokio::time::sleep(IDLE_POLL) => {}
            }
        }
    });
}

// ---------------------------------------------------------------------------
// 画面へ出す口
// ---------------------------------------------------------------------------

fn last_error_json() -> Value {
    match LAST_ERROR.lock().ok().and_then(|g| g.clone()) {
        Some((at, msg)) => json!({ "at": rfc3339(at * 1000, local_offset_secs()), "message": msg }),
        None => Value::Null,
    }
}

/// いまの記録の状態。**画面の文言はここから作る。**
///
/// ★固定の説明文を画面に置かないこと。`telemetry_url` を空にした人に
/// 「サーバへ送っています」と表示するのが、この種の機能で最悪の壊れ方
/// （送っているつもりで1件も届いていない、に気づけない）。
#[tauri::command]
pub fn journal_status() -> Value {
    let cfg = config();
    let offset = local_offset_secs();
    let today = day_of(epoch_secs(), offset);
    let days = list_days();
    let cursor = Cursor::load();

    let total: u64 = days.iter().map(|(_, b)| *b).sum();
    let pending: u64 = days.iter().map(|(d, b)| b.saturating_sub(cursor.get(d))).sum();
    let today_bytes = days.iter().find(|(d, _)| *d == today).map(|(_, b)| *b).unwrap_or(0);

    // 濾し器が居るか。★「送れます」と名乗る前に、実際に探して答える。
    let (redactor, redactor_reason) = match resolve_python() {
        Ok(p) => (Some(p.display().to_string()), Value::Null),
        Err(e) => (None, json!(e)),
    };

    json!({
        "enabled": cfg.enabled,
        "dir": dir().display().to_string(),
        "today": today,
        "tz_offset": format!("{}{:02}:{:02}",
            if offset < 0 { '-' } else { '+' }, offset.unsigned_abs() / 3600, (offset.unsigned_abs() % 3600) / 60),
        // 現地時刻のずれが取れなかったときも +00:00 と出る。区別できるように明示する。
        "tz_known": time::UtcOffset::current_local_offset().is_ok(),

        "send": {
            "url": cfg.url.clone(),
            // 送らない理由。★null 以外なら「送っていない」が確定している。
            "off_reason": if cfg.url.is_none() { json!(cfg.url_reason) } else { Value::Null },
            "redactor": redactor,
            "redactor_reason": redactor_reason,
            "last_ok": match LAST_OK.load(Ordering::Relaxed) {
                0 => Value::Null,
                t => json!(rfc3339(t * 1000, offset)),
            },
            "last_error": last_error_json(),
        },

        "local": {
            "days": days.len(),
            "bytes": total,
            "today_bytes": today_bytes,
            "pending_bytes": pending,
            "day_soft_bytes": DAY_SOFT_BYTES,
            "day_over_soft": today_bytes >= DAY_SOFT_BYTES,
            "day_hard_bytes": DAY_HARD_BYTES,
            "max_bytes": cfg.max_bytes,
            "keep_days": cfg.keep_days,
        },

        // ★数えたものだけ。ここが全部 0 なら「まだ何も起きていない」と読んでよい。
        "counts": {
            "written": N_WRITTEN.load(Ordering::Relaxed),
            "written_bytes": N_WRITE_BYTES.load(Ordering::Relaxed),
            "write_failed": N_WRITE_FAILED.load(Ordering::Relaxed),
            "shrunk": N_LADDER.load(Ordering::Relaxed),
            "header_only": N_HEADER_ONLY.load(Ordering::Relaxed),
            "sent_records": N_SENT_RECORDS.load(Ordering::Relaxed),
            "sent_batches": N_SENT_BATCHES.load(Ordering::Relaxed),
            "sent_bytes_gz": N_SENT_BYTES.load(Ordering::Relaxed),
            "send_failed": N_SEND_FAILED.load(Ordering::Relaxed),
            "redact_failed": N_REDACT_FAILED.load(Ordering::Relaxed),
            "redactions": N_REDACTIONS.load(Ordering::Relaxed),
            "redaction_kinds": REDACT_KINDS.lock().ok().and_then(|g| g.clone()).unwrap_or_default(),
            "evicted_days": N_EVICTED_DAYS.load(Ordering::Relaxed),
            "evicted_bytes": N_EVICTED_BYTES.load(Ordering::Relaxed),
            // ★0 でないなら、サーバ側の記録に穴が空いている。
            "evicted_unsent_bytes": N_EVICTED_UNSENT.load(Ordering::Relaxed),
            "skipped_bytes": N_SKIPPED_BYTES.load(Ordering::Relaxed),
        },

        "limits": {
            "record_bytes": MAX_RECORD_BYTES,
            "post_bytes": MAX_POST_BYTES,
            "post_lines": MAX_POST_LINES,
            "rec": REC_VERSION,
        },
    })
}

/// いま送る（画面の「今すぐ送る」用）。**失敗しても会話は止まらない。**
#[tauri::command]
pub async fn journal_flush() -> Value {
    let cfg = config();
    if !cfg.enabled {
        return json!({ "ok": false, "message": "ログ送信は停止しています（telemetry = off）" });
    }
    let Some(_) = cfg.url.as_deref() else {
        return json!({ "ok": false, "message": format!("送信先がありません（{}）", cfg.url_reason) });
    };
    let Ok(_g) = ship_lock().try_lock() else {
        return json!({ "ok": false, "message": "いま送信中です" });
    };
    let before = N_SENT_RECORDS.load(Ordering::Relaxed);
    match ship(&cfg).await {
        Ok(()) => {
            let n = N_SENT_RECORDS.load(Ordering::Relaxed) - before;
            json!({ "ok": true, "sent": n, "message": format!("{n} 件を送りました") })
        }
        Err(e) => {
            note_error(e.clone());
            json!({ "ok": false, "message": e })
        }
    }
}

/// 記録の置き場（画面で案内するため）。
#[tauri::command]
pub fn journal_path() -> String {
    dir().display().to_string()
}

// ★`creation_flags` は `tokio::process::Command` 自身が windows 向けに持っている。
//   std の `CommandExt` を use すると「使っていない」と警告される（sidecar.rs / llama.rs で
//   同じことを踏んでいる）。だからここには use を置かない。

// ---------------------------------------------------------------------------
// 試験
//
// ★2026-09-05（r4/tests-never-run）: 組み立て・ladder・時刻書式・plan_evict/
//   plan_batch/split_lines/gzip を検査する 28 本は `journal_format`
//   （blackwall-core）へ移し、`cargo test --manifest-path
//   blackwall-core/Cargo.toml` で実際に走る（このファイルは tauri を参照する
//   のでホストでは cargo test がリンクできない）。ここに残るのは `host_id()`
//   （`crate::app_root()` 経由でファイルに触れるので移せなかった）1本だけで、
//   これは従来どおり `cargo check --tests --target x86_64-pc-windows-gnu` で
//   型検査されるのみ。ただしその中核（塩とラベルから host_id を出す計算）は
//   `journal_format::host_id_with_salt` として切り出し済みで、そちらは
//   blackwall-core 側で実際に走っている（`host_id_同じ塩と名前なら同じ値になる`）。
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_idは同じ名前なら同じ値になる() {
        let a = host_id("HOST-A");
        let b = host_id("HOST-A");
        let c = host_id("other-host");
        assert_eq!(a, b);
        assert_ne!(a, c);
        assert_eq!(a.len(), 16);
    }
}
