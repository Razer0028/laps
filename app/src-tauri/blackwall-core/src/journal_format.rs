//! `journal.rs` の**純粋な**部分の実体: 封筒の組み立て・落とす順（ladder）・
//! 時刻の書式・素性のハッシュ・送信バッチの計画・古い日の削除計画・gzip・行の切り出し。
//!
//! ★なぜ本体（`src/journal.rs`）ではなくここに置くか（2026-09-05・r4/tests-never-run）:
//!   `journal.rs` は `tauri::async_runtime::spawn` と `#[tauri::command]` を使うので
//!   ホスト（Linux）では `cargo test` が**リンクできない**（GTK 依存）。29 本の試験の
//!   うち 28 本（この封筒組み立て・ladder・時刻・plan_evict/plan_batch/split_lines/gzip の
//!   部分）は preflight でも `cargo check --tests --target x86_64-pc-windows-gnu`＝
//!   **コンパイルされるだけで一度も実行されていなかった**。
//!   `journal.rs` は `#[path]` で参照するだけ＝実体は1本（`wire.rs`/`translate.rs`/
//!   `workspace_guard.rs` と同じ作法）。
//!
//!   `host_id` / `install_id` / `stable_hex_file` / `config` / `dir` は
//!   `crate::app_root()`（Tauri 側のアプリ設定ディレクトリ解決）に依存するので
//!   移していない。ただし `host_id` の**ハッシュ計算そのもの**（塩とラベルから
//!   16hex を作る部分）はファイルに触れない純粋な計算なので、[`host_id_with_salt`]
//!   として切り出した。`journal.rs` 側の `host_id()` はそちらを呼ぶ薄い皮になる。

use std::io::{Read, Seek, SeekFrom, Write as _};
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use std::sync::OnceLock;

use serde_json::{json, Map, Value};

// ---------------------------------------------------------------------------
// 定数
// ---------------------------------------------------------------------------

/// レコードの形の版。★増やしたら `scripts/check_protocol.py` を通すこと。
pub const REC_VERSION: u64 = 1;

/// 1レコードの上限。
pub const MAX_RECORD_BYTES: usize = 32 * 1024;

/// `limits` のために空けておくぶん。
pub const LIMITS_RESERVE: usize = 1024;
pub const BODY_BUDGET: usize = MAX_RECORD_BYTES - LIMITS_RESERVE;

/// どの文字列にも掛ける保険の頭打ち（文字数）。
pub const MAX_STRING_CHARS: usize = 8192;

/// `tools` の最大件数。超えた分は数だけ `tools_overflow` に残す。
pub const MAX_TOOLS: usize = 40;
/// 超えたときに頭から残す数と、間から拾う失敗の上限（残りは終わりから）。
const KEEP_HEAD: usize = 10;
const KEEP_MID_FAILS: usize = 8;

/// 上限を超えた道具の並びから、頭 `KEEP_HEAD` 件・終わり・間の失敗（`ok` が false）を元の順で選び、
/// 各件に元の位置 `i` を付ける（★既に `i` があれば上書きしない）。
fn pick_tools(tools: Vec<Value>) -> Vec<Value> {
    let n = tools.len();
    if n <= MAX_TOOLS {
        return tools;
    }
    let head = KEEP_HEAD.min(MAX_TOOLS);
    let mut tail = MAX_TOOLS - head - KEEP_MID_FAILS;
    let failed = |v: &Value| v.get("ok").and_then(Value::as_bool) == Some(false);
    let mid: Vec<usize> = (head..n - tail).filter(|&i| failed(&tools[i])).collect();
    let mids: Vec<usize> = mid[mid.len().saturating_sub(KEEP_MID_FAILS)..].to_vec();
    tail += KEEP_MID_FAILS - mids.len();
    let mut keep: Vec<usize> = (0..head).collect();
    keep.extend(mids.iter().copied().filter(|&i| i < n - tail));
    keep.extend(n - tail..n);
    keep.sort_unstable();
    keep.dedup();
    keep.truncate(MAX_TOOLS);
    keep.into_iter()
        .map(|i| {
            let mut v = tools[i].clone();
            if let Some(o) = v.as_object_mut() {
                o.entry("i").or_insert(json!(i));
            }
            v
        })
        .collect()
}

/// `counters` の最大キー数。
pub const MAX_COUNTER_KEYS: usize = 100;

// ---------------------------------------------------------------------------
// 設定の読み方（純粋な判定だけ）
// ---------------------------------------------------------------------------

/// 「止まっている」と読める書き方を全部受ける。★`telemetry = false` と書いた人が
/// 送信され続けるのは事故なので、迷ったら**止まる側**に倒す。
pub fn reads_as_off(v: &str) -> bool {
    matches!(
        v.trim().to_ascii_lowercase().as_str(),
        "off" | "false" | "no" | "0" | "none" | "disabled" | "stop"
    )
}

// ---------------------------------------------------------------------------
// 時刻（書式は自前。ずれだけ呼び出し側から貰う）
// ---------------------------------------------------------------------------

pub fn epoch_secs() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

pub fn epoch_millis() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// アプリが立ち上がった瞬間。`ts_mono_ms` の原点（壁時計が飛んでも順序が保てる）。
pub fn boot() -> Instant {
    static BOOT: OnceLock<Instant> = OnceLock::new();
    *BOOT.get_or_init(Instant::now)
}

/// epoch 秒 → (年, 月, 日)。Howard Hinnant の civil_from_days。
/// ★これを自前で持つのは、日付の境目が**手元のファイル名**になるから。
///   1日ずれると「今日のログ」が2つに割れて、調査の最初の一手が外れる。
pub fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as i64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// 現地日付の `YYYY-MM-DD`。ファイル名になる。
pub fn day_of(epoch_secs: i64, offset_secs: i32) -> String {
    let local = epoch_secs + offset_secs as i64;
    let days = local.div_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}-{m:02}-{d:02}")
}

/// ずれ付きの RFC3339。`2026-08-11T20:06:22.314+09:00`。
pub fn rfc3339(epoch_ms: i64, offset_secs: i32) -> String {
    let local_ms = epoch_ms + offset_secs as i64 * 1000;
    let secs = local_ms.div_euclid(1000);
    let ms = local_ms.rem_euclid(1000);
    let days = secs.div_euclid(86_400);
    let sod = secs.rem_euclid(86_400);
    let (y, mo, d) = civil_from_days(days);
    let (h, mi, s) = (sod / 3600, (sod % 3600) / 60, sod % 60);
    let sign = if offset_secs < 0 { '-' } else { '+' };
    let off = offset_secs.unsigned_abs();
    format!(
        "{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}.{ms:03}{sign}{:02}:{:02}",
        off / 3600,
        (off % 3600) / 60
    )
}

// ---------------------------------------------------------------------------
// 素性（install_id / host_id / レコードの id）
//
// ★ファイルに触れる部分（`stable_hex_file` の実体・`.install_id`/`.salt` の
//   在り処＝`crate::app_root()`）は `journal.rs` に残っている。ここにあるのは
//   「16 バイトの適当な値を作る」「ハッシュする」「塩とラベルから host_id を出す」
//   という、ファイルに触れない純粋な計算だけ。
// ---------------------------------------------------------------------------

/// 16 バイトの適当な値。**暗号用途ではない**（衝突しなければよい）。
pub fn weak_random_16() -> [u8; 16] {
    if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
        let mut b = [0u8; 16];
        if f.read_exact(&mut b).is_ok() {
            return b;
        }
    }
    use std::hash::{BuildHasher, Hasher};
    let mut mix = Vec::with_capacity(64);
    for i in 0..4u64 {
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u64(i);
        h.write_i64(epoch_millis());
        h.write_u32(std::process::id());
        h.write_u64(boot().elapsed().as_nanos() as u64);
        mix.extend_from_slice(&h.finish().to_le_bytes());
    }
    let mut out = [0u8; 16];
    out.copy_from_slice(&sha256(&mix)[..16]);
    out
}

pub fn sha256(b: &[u8]) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(b);
    h.finalize().into()
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// 塩とラベルから host_id を作る。**純粋関数**（ファイルに触れない）。
///
/// 元は `host_id()` に直書きされていたハッシュ計算部分（塩の読み書きは
/// `journal.rs` 側の `stable_hex_file(".salt")` が担う）。
pub fn host_id_with_salt(salt: &str, label: &str) -> String {
    let mut b = Vec::new();
    b.extend_from_slice(salt.as_bytes());
    b.push(0);
    b.extend_from_slice(label.as_bytes());
    hex(&sha256(&b)[..8])
}

/// レコードの id。`<epoch_ms を 36 進>-<8hex>`。サーバはこれで重複を捨てる。
pub fn new_id(epoch_ms: i64) -> String {
    static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    static SEED: OnceLock<[u8; 16]> = OnceLock::new();
    let seed = SEED.get_or_init(weak_random_16);
    let n = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let mut b = Vec::with_capacity(32);
    b.extend_from_slice(seed);
    b.extend_from_slice(&n.to_le_bytes());
    b.extend_from_slice(&epoch_ms.to_le_bytes());
    format!("{}-{}", base36(epoch_ms.max(0) as u64), hex(&sha256(&b)[..4]))
}

pub fn base36(mut n: u64) -> String {
    const D: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyz";
    if n == 0 {
        return "0".into();
    }
    let mut out = Vec::new();
    while n > 0 {
        out.push(D[(n % 36) as usize]);
        n /= 36;
    }
    out.reverse();
    String::from_utf8(out).unwrap_or_default()
}

// ---------------------------------------------------------------------------
// 1往復ぶんの入れ物
// ---------------------------------------------------------------------------

/// 呼ぶ側（sidecar.rs）が組んで渡す塊。**中身の意味はここでは解釈しない。**
///
/// 未設定は `Value::Null` のまま載る。★空オブジェクトに置き換えない。
/// 「呼ぶ側が埋めなかった」と「本当に空だった」が区別できなくなる。
#[derive(Debug, Default, Clone)]
pub struct Turn {
    /// `client` に足したいもの（`core_version` `protocol` など、線の向こうしか知らない値）。
    pub client: Value,
    pub engine: Value,
    pub run: Value,
    pub input: Value,
    pub output: Value,
    pub think: Value,
    pub tools: Vec<Value>,
    pub cost: Value,
    pub stop: Value,
    pub quality: Value,
    pub counters: Value,
    pub protocol: Value,
    /// JSON では `ref`。Rust の予約語なので綴りを変えている。
    pub refs: Value,
}

/// 落としたものの控え。`limits` に出る。
#[derive(Debug, Default)]
pub struct Dropped {
    pub map: Map<String, Value>,
    pub stage: u64,
    pub over_budget: bool,
}

impl Dropped {
    /// `field` から `chars` 文字ぶん落とした、と控える（同じ欄を2度削ったら足す）。
    fn note(&mut self, field: &str, chars: usize) {
        if chars == 0 {
            return;
        }
        let prev = self.map.get(field).and_then(|v| v.as_u64()).unwrap_or(0);
        self.map.insert(field.into(), json!(prev + chars as u64));
    }
}

// ---------------------------------------------------------------------------
// 組み立て
// ---------------------------------------------------------------------------

/// 文字数（バイトではない）で切る。切った文字数も返す。
/// ★バイトで切ると日本語の途中で割れる（sidecar.rs / agent.rs と同じ理由）。
pub fn clamp_chars(s: &str, max: usize) -> (String, usize) {
    let n = s.chars().count();
    if n <= max {
        return (s.to_string(), 0);
    }
    (s.chars().take(max).collect(), n - max)
}

/// 木の中の文字列を、どれも `MAX_STRING_CHARS` 以下にする（保険）。
pub fn clamp_all_strings(v: &mut Value, path: &str, d: &mut Dropped) {
    match v {
        Value::String(s) => {
            let (t, cut) = clamp_chars(s, MAX_STRING_CHARS);
            if cut > 0 {
                *s = t;
                d.note(path, cut);
            }
        }
        Value::Array(a) => {
            for x in a.iter_mut() {
                clamp_all_strings(x, path, d);
            }
        }
        Value::Object(o) => {
            for (k, x) in o.iter_mut() {
                let p = if path.is_empty() { k.clone() } else { format!("{path}.{k}") };
                clamp_all_strings(x, &p, d);
            }
        }
        _ => {}
    }
}

/// `obj.key` の文字列を `max` 文字へ詰める。落とした文字数を返す。
pub fn shrink_text(root: &mut Value, section: &str, key: &str, max: usize) -> usize {
    let Some(s) = root.get_mut(section).and_then(|x| x.get_mut(key)).and_then(|x| x.as_str().map(str::to_string))
    else {
        return 0;
    };
    let (t, cut) = clamp_chars(&s, max);
    if cut > 0 {
        root[section][key] = Value::String(t);
    }
    cut
}

/// `tools` の各要素の `path`（`args` / `result`）に手を入れる。
pub fn for_each_tool(root: &mut Value, mut f: impl FnMut(&mut Value)) {
    if let Some(a) = root.get_mut("tools").and_then(|x| x.as_array_mut()) {
        for t in a.iter_mut() {
            f(t);
        }
    }
}

/// 呼ぶ側から来た塊を、封筒に収めて1つの JSON にする。
///
/// `now_ms` / `offset` / `id` / `mono_ms` を引数で受けるのは、**試験で時計を止めるため**
/// （時刻を内側で読むと、組み立ての正しさを時計と切り離して確かめられない）。
pub fn assemble(
    t: Turn,
    now_ms: i64,
    offset: i32,
    id: String,
    mono_ms: u64,
    client_base: Value,
    header_only: bool,
) -> (Value, Dropped) {
    let mut d = Dropped::default();

    // client = 封筒側の既定 + 呼ぶ側が知っている値（向こうを優先）。
    let mut client = client_base;
    if let (Some(base), Some(extra)) = (client.as_object_mut(), t.client.as_object()) {
        for (k, v) in extra {
            base.insert(k.clone(), v.clone());
        }
    }

    // counters は**丸ごと逐語**が原則。上限に当たったことだけ控える。
    let mut counters = t.counters;
    if let Some(o) = counters.as_object_mut() {
        if o.len() > MAX_COUNTER_KEYS {
            let cut = o.len() - MAX_COUNTER_KEYS;
            let keys: Vec<String> = o.keys().skip(MAX_COUNTER_KEYS).cloned().collect();
            for k in keys {
                o.remove(&k);
            }
            d.note("counters", cut);
        }
    }

    // tools は件数を先に抑える（1件が数 KiB になり得るので、ここが最大の膨らみ元）。
    // ★残すのは「頭・終わり・間の失敗」（2026-10-09）。旧: 頭から 40 件。3 時間・173 回の走行で、詰まった所
    //   （終わり）の 133 回が記録から落ち、何が起きていたかを確かめられなかった。各件に元の位置 `i` を付ける。
    let tools = t.tools;
    let overflow = tools.len().saturating_sub(MAX_TOOLS);
    let tools = if overflow > 0 {
        d.note("tools", overflow);
        pick_tools(tools)
    } else {
        tools
    };

    let mut quality = t.quality;
    if header_only {
        push_flag(&mut quality, "day_cap");
    }

    let mut rec = json!({
        "rec": REC_VERSION,
        "id": id,
        "ts": rfc3339(now_ms, offset),
        "ts_mono_ms": mono_ms,
        "client": client,
        "engine": t.engine,
        "run": t.run,
        "cost": t.cost,
        "stop": t.stop,
        "quality": quality,
        "counters": counters,
        "protocol": t.protocol,
        "ref": t.refs,
        // ★以下は「何段でも落とす」側。header_only ならこの時点で置かない。
    });

    if !header_only {
        rec["input"] = t.input;
        rec["output"] = t.output;
        rec["think"] = t.think;
        rec["tools"] = Value::Array(tools);
        if overflow > 0 {
            rec["tools_overflow"] = json!(overflow);
        }
    } else {
        d.note("(header_only)", 1);
        d.over_budget = true;
    }

    // 保険。呼ぶ側が塊ごとの上限を掛け忘れても、ここで頭打ちになる。
    clamp_all_strings(&mut rec, "", &mut d);

    // 落とす順（ladder）。**上限を超えたときだけ**、上から順に。
    let mut len = rec.to_string().len();
    if !header_only {
        for stage in 1..=6u64 {
            if len <= BODY_BUDGET {
                break;
            }
            d.stage = stage;
            match stage {
                // ① 思考を 512 文字へ。⚠ 思考は漏えい risk が最も高く、量も最大。最初に削る。
                1 => {
                    let c = shrink_text(&mut rec, "think", "text", 512);
                    d.note("think.text", c);
                }
                // ② ツール結果の末尾を捨てる（頭のほうが診断に効く）。
                2 => for_each_tool(&mut rec, |t| {
                    if let Some(r) = t.get_mut("result").and_then(|x| x.as_object_mut()) {
                        r.remove("tail");
                    }
                }),
                // ③ ツールの引数の文字列を 64 文字へ。
                3 => {
                    let mut cut = 0usize;
                    for_each_tool(&mut rec, |t| {
                        if let Some(a) = t.get_mut("args") {
                            cut += clamp_strings_to(a, 64);
                        }
                    });
                    d.note("tools[].args", cut);
                }
                // ④ 本文を 2048 文字へ。
                4 => {
                    let c = shrink_text(&mut rec, "output", "text", 2048);
                    d.note("output.text", c);
                }
                // ⑤ 思考を全部捨てる（`think.chars` は残す＝「有ったが載せていない」が分かる）。
                5 => {
                    let c = shrink_text(&mut rec, "think", "text", 0);
                    d.note("think.text", c);
                    if let Some(o) = rec.get_mut("think").and_then(|x| x.as_object_mut()) {
                        o.remove("text");
                        o.insert("text_dropped".into(), json!(true));
                    }
                }
                // ⑥ ツールを先頭10＋末尾5に。
                6 => {
                    if let Some(a) = rec.get_mut("tools").and_then(|x| x.as_array_mut()) {
                        if a.len() > 15 {
                            let cut = a.len() - 15;
                            let tail: Vec<Value> = a[a.len() - 5..].to_vec();
                            a.truncate(10);
                            a.extend(tail);
                            d.note("tools[]", cut);
                        }
                    }
                }
                _ => {}
            }
            len = rec.to_string().len();
        }
    }

    // ここまで削っても収まらない＝封筒だけに落とす。**記録は止めない。**
    if len > BODY_BUDGET && !header_only {
        for k in ["input", "output", "think", "tools"] {
            if let Some(o) = rec.as_object_mut() {
                o.remove(k);
            }
        }
        d.note("(header_only)", 1);
        d.over_budget = true;
    }

    finalize(&mut rec, &mut d);
    (rec, d)
}

/// `limits` を入れて、`bytes` に**書き出す1行の長さ**を入れる。長さを返す。
///
/// ★不動点は**整数の計算だけ**で取る（書き出し直さない）。詳細は元の doc comment
///   （`journal.rs` の履歴）を参照。
pub fn finalize(rec: &mut Value, d: &mut Dropped) -> usize {
    rec["limits"] = json!({
        "bytes": 0,
        "dropped": Value::Object(d.map.clone()),
        // ★伏字はこのアプリが掛けない（送る直前に Python が掛ける）ので、件数を名乗らない。
        "redactions": Value::Null,
        "redaction_kinds": Value::Null,
        "over_budget": d.over_budget,
        "drop_stage": d.stage,
    });

    // `"bytes":0` のときの長さ。ここから桁の増分を足すだけで最終の長さが出る。
    let base = rec.to_string().len();
    let mut bytes = base;
    for _ in 0..4 {
        let n = base + digits(bytes) - 1;
        if n == bytes {
            break;
        }
        bytes = n;
    }
    rec["limits"]["bytes"] = json!(bytes);

    if bytes > MAX_RECORD_BYTES {
        // 封筒だけでも超えている＝呼ぶ側の塊（counters / engine 等）が異常に大きい。
        // ★それでも書く。落とすと「その往復が無かった」ことになる。
        d.over_budget = true;
        rec["limits"]["over_budget"] = json!(true);
    }
    bytes
}

/// 10 進の桁数。0 は 1 桁。
pub fn digits(mut n: usize) -> usize {
    let mut k = 1;
    while n >= 10 {
        n /= 10;
        k += 1;
    }
    k
}

/// 木の中の文字列を `max` 文字へ。落とした合計文字数を返す。
pub fn clamp_strings_to(v: &mut Value, max: usize) -> usize {
    match v {
        Value::String(s) => {
            let (t, cut) = clamp_chars(s, max);
            if cut > 0 {
                *s = t;
            }
            cut
        }
        Value::Array(a) => a.iter_mut().map(|x| clamp_strings_to(x, max)).sum(),
        Value::Object(o) => o.iter_mut().map(|(_, x)| clamp_strings_to(x, max)).sum(),
        _ => 0,
    }
}

/// `quality.flags` に旗を1つ足す。無ければ作る。
pub fn push_flag(quality: &mut Value, flag: &str) {
    if !quality.is_object() {
        *quality = json!({});
    }
    let o = quality.as_object_mut().expect("直前に作っている");
    match o.get_mut("flags").and_then(|f| f.as_array_mut()) {
        Some(a) => {
            if !a.iter().any(|x| x.as_str() == Some(flag)) {
                a.push(json!(flag));
            }
        }
        None => {
            o.insert("flags".into(), json!([flag]));
        }
    }
}

// ---------------------------------------------------------------------------
// 掃除（古い日を消す計画）
// ---------------------------------------------------------------------------

/// 消す日を決める。**純粋関数**（読み書きしない＝試験できる）。
///
/// 規則:
/// * `today` は何があっても消さない（いま書いている先を消すと、追記が宙に浮く）。
/// * 日数が `keep_days` を超えたぶんは、古いほうから消す。
/// * それでも合計が `max_bytes` を超えるなら、収まるまで古いほうから消す。
pub fn plan_evict(days: &[(String, u64)], max_bytes: u64, keep_days: usize, today: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut total: u64 = days.iter().map(|(_, b)| *b).sum();
    let mut alive: usize = days.len();

    for (day, bytes) in days.iter() {
        if day == today {
            continue;
        }
        let too_many = alive > keep_days;
        let too_big = total > max_bytes;
        if !too_many && !too_big {
            break;
        }
        out.push(day.clone());
        total = total.saturating_sub(*bytes);
        alive -= 1;
    }
    out
}

// ---------------------------------------------------------------------------
// 送る（純粋な計画・行の切り出し・圧縮）
// ---------------------------------------------------------------------------

/// 1回の POST に載せる行数を決める。**純粋関数**。
///
/// ★1行しか入らなくても必ず 1 を返す。0 を返すと、大きすぎる1行が先頭にある間
/// **列が永久に進まない**（静かに止まる。いちばん気づけない壊れ方）。
pub fn plan_batch(sizes: &[usize], max_bytes: usize, max_lines: usize) -> usize {
    let mut n = 0usize;
    let mut bytes = 0usize;
    for s in sizes {
        if n >= max_lines {
            break;
        }
        if n > 0 && bytes + s > max_bytes {
            break;
        }
        bytes += s;
        n += 1;
    }
    n.max(1).min(sizes.len())
}

/// 改行ごとに (行本文, その行の直後のファイル位置) へ切る。**純粋関数**。
///
/// * `\r\n` の `\r` は落とす（本文には残さない）が、**位置は落とさない**。
/// * 空行は返さない（濾し器が黙って落とすので、行数の突き合わせが合わなくなる）。
///   飛ばしても位置は次の行が持っているのでずれない。
pub fn split_lines(buf: &[u8], from: u64) -> Vec<(String, u64)> {
    let mut out: Vec<(String, u64)> = Vec::new();
    let mut start = 0usize;
    for (i, b) in buf.iter().enumerate() {
        if *b != b'\n' {
            continue;
        }
        let text = String::from_utf8_lossy(&buf[start..i]).trim_end_matches('\r').to_string();
        if !text.trim().is_empty() {
            out.push((text, from + i as u64 + 1));
        }
        start = i + 1;
    }
    out
}

pub fn read_full(f: &mut std::fs::File, buf: &mut [u8]) -> std::io::Result<usize> {
    let mut n = 0usize;
    while n < buf.len() {
        match f.read(&mut buf[n..])? {
            0 => break,
            k => n += k,
        }
    }
    Ok(n)
}

/// `from` 以降で最初の改行の**次**の位置を返す。見つからなければ `len`。
pub fn scan_to_newline(f: &mut std::fs::File, from: u64, len: u64) -> std::io::Result<u64> {
    f.seek(SeekFrom::Start(from))?;
    let mut at = from;
    let mut buf = [0u8; 65536];
    while at < len {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        if let Some(i) = buf[..n].iter().position(|b| *b == b'\n') {
            return Ok(at + i as u64 + 1);
        }
        at += n as u64;
    }
    Ok(len)
}

/// gzip で固める。
pub fn gzip(data: &[u8]) -> std::io::Result<Vec<u8>> {
    let mut e = flate2::write::GzEncoder::new(
        Vec::with_capacity(data.len() / 4 + 64),
        flate2::Compression::new(6),
    );
    e.write_all(data)?;
    e.finish()
}

// ---------------------------------------------------------------------------
// 試験
//
// ★この 28 本は移設前、`src/journal.rs` の `#[cfg(test)] mod tests` にあり、
//   `cargo check --tests --target x86_64-pc-windows-gnu` で**コンパイルされるだけ**
//   だった。ここに移してはじめて `cargo test --manifest-path blackwall-core/Cargo.toml`
//   で実際に走る。残る1本（元の `host_idは同じ名前なら同じ値になる`）は
//   `dir()`/`crate::app_root()` に依存する `stable_hex_file` を経由するので
//   `journal.rs` 側に残したが、その中核（塩とラベルから host_id を出す計算）を
//   `host_id_with_salt` として切り出し、下の `host_id_同じ塩と名前なら同じ値になる`
//   で実際に走らせている。
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn turn() -> Turn {
        Turn {
            stop: json!({ "reason": "accepted", "reason_source": "agent" }),
            counters: json!({ "completion_check_ran": 1 }),
            ..Default::default()
        }
    }

    fn build(t: Turn, header_only: bool) -> Value {
        assemble(t, 1_770_000_000_000, 9 * 3600, "id1".into(), 42, json!({ "app_version": "t" }), header_only).0
    }

    // ---- 時刻とファイル名 -------------------------------------------------

    #[test]
    fn 日付は現地時刻で決まる() {
        // ★UTC で切ると、JST の 9 時前に書いた記録が前日のファイルへ入る。
        //   「今日のログ」を開いた人が、今朝の分を見つけられなくなる。
        let t = 1_770_000_000; // 2026-02-02 02:40:00 UTC = 11:40 JST
        assert_eq!(day_of(t, 0), "2026-02-02");
        assert_eq!(day_of(t, 9 * 3600), "2026-02-02");
        // 2026-02-02 22:30 UTC は JST では翌日の 07:30
        let u = 1_770_071_400;
        assert_eq!(day_of(u, 0), "2026-02-02");
        assert_eq!(day_of(u, 9 * 3600), "2026-02-03");
    }

    #[test]
    fn 閏日と紀元の境目を間違えない() {
        assert_eq!(day_of(0, 0), "1970-01-01");
        assert_eq!(day_of(951_782_400, 0), "2000-02-29"); // 400 年の閏
        assert_eq!(day_of(1_078_012_800, 0), "2004-02-29");
        assert_eq!(day_of(-1, 0), "1969-12-31"); // 時計が狂って負になっても落ちない
    }

    #[test]
    fn 時刻にはずれが必ず付く() {
        let s = rfc3339(1_770_000_000_123, 9 * 3600);
        assert_eq!(s, "2026-02-02T11:40:00.123+09:00");
        assert_eq!(rfc3339(1_770_000_000_123, 0), "2026-02-02T02:40:00.123+00:00");
        // ★ずれが取れなかったときは +00:00 と出る＝「現地時刻のふりをしない」。
        assert!(rfc3339(1_770_000_000_123, -5 * 3600).ends_with("-05:00"));
        assert_eq!(rfc3339(1_770_000_000_123, -5 * 3600), "2026-02-01T21:40:00.123-05:00");
    }

    #[test]
    fn idは重複せず時刻順に並ぶ() {
        let a = new_id(1_770_000_000_000);
        let b = new_id(1_770_000_000_000);
        assert_ne!(a, b, "同じミリ秒でも別の id になること（サーバは id で重複を捨てる）");
        assert!(new_id(1_770_000_000_000) < new_id(1_770_000_001_000));
        assert_eq!(base36(1_770_000_000_000).len(), base36(1_899_999_999_999).len());
        assert_eq!(base36(0), "0");
        assert_eq!(base36(35), "z");
        assert_eq!(base36(36), "10");
    }

    // ---- 設定 -------------------------------------------------------------

    #[test]
    fn 止まっていると読める書き方をすべて受ける() {
        for v in ["off", "OFF", "false", "no", "0", " none ", "disabled"] {
            assert!(reads_as_off(v), "{v}");
        }
        for v in ["on", "true", "yes", "1", ""] {
            assert!(!reads_as_off(v), "{v}");
        }
    }

    // ---- 組み立て ---------------------------------------------------------

    #[test]
    fn 封筒には版と時刻と素性が必ず載る() {
        let r = build(turn(), false);
        assert_eq!(r["rec"], json!(REC_VERSION));
        assert_eq!(r["id"], json!("id1"));
        assert_eq!(r["ts"], json!("2026-02-02T11:40:00.000+09:00"));
        assert_eq!(r["ts_mono_ms"], json!(42));
        assert_eq!(r["stop"]["reason"], json!("accepted"));
        assert_eq!(r["limits"]["drop_stage"], json!(0));
        assert_eq!(r["limits"]["over_budget"], json!(false));
        assert_eq!(r["limits"]["redactions"], Value::Null);
    }

    #[test]
    fn 呼ぶ側が埋めなかった塊はnullのまま残る() {
        let r = build(turn(), false);
        assert_eq!(r["engine"], Value::Null);
        assert_eq!(r["think"], Value::Null);
        assert_eq!(r["tools"], json!([]));
    }

    #[test]
    fn 呼ぶ側のclientが封筒の既定を上書きする() {
        let t = Turn { client: json!({ "core_version": "0.9", "app_version": "x" }), ..turn() };
        let r = build(t, false);
        assert_eq!(r["client"]["core_version"], json!("0.9"));
        assert_eq!(r["client"]["app_version"], json!("x"));
    }

    #[test]
    fn 長すぎる文字列は保険で頭打ちになり落とした量が残る() {
        let t = Turn { output: json!({ "text": "あ".repeat(MAX_STRING_CHARS + 100) }), ..turn() };
        let r = build(t, false);
        assert_eq!(r["output"]["text"].as_str().unwrap().chars().count(), MAX_STRING_CHARS);
        assert_eq!(r["limits"]["dropped"]["output.text"], json!(100));
    }

    #[test]
    fn ツールは四十件で頭打ちになり超過数が残る() {
        let tools: Vec<Value> = (0..45).map(|i| json!({ "i": i, "name": "Bash" })).collect();
        let r = build(Turn { tools, ..turn() }, false);
        assert_eq!(r["tools"].as_array().unwrap().len(), MAX_TOOLS);
        assert_eq!(r["tools_overflow"], json!(5));
        assert_eq!(r["limits"]["dropped"]["tools"], json!(5));
    }

    #[test]
    fn 数え値は百キーまでは絞らない() {
        let mut o = Map::new();
        for i in 0..100 {
            o.insert(format!("k{i}"), json!(i));
        }
        let r = build(Turn { counters: Value::Object(o), ..turn() }, false);
        assert_eq!(r["counters"].as_object().unwrap().len(), 100);
        assert_eq!(r["limits"]["dropped"].get("counters"), None);
    }

    // ---- 落とす順（ladder） ----------------------------------------------

    #[test]
    fn 大きすぎるレコードは順に削られて上限に収まる() {
        let t = Turn {
            think: json!({ "text": "思".repeat(6000), "chars": 6000 }),
            output: json!({ "text": "答".repeat(6000) }),
            tools: (0..30)
                .map(|i| json!({
                    "i": i, "name": "Bash",
                    "args": { "command": "x".repeat(500) },
                    "result": { "head": "h".repeat(500), "tail": "t".repeat(300) }
                }))
                .collect(),
            ..turn()
        };
        let r = build(t, false);
        let bytes = r.to_string().len();
        assert!(bytes <= MAX_RECORD_BYTES, "{bytes} バイト残っている");
        assert!(r["limits"]["drop_stage"].as_u64().unwrap() >= 1);
        assert_eq!(r["limits"]["bytes"], json!(bytes));
    }

    #[test]
    fn 名乗ったバイト数は実際に書く行の長さと一致する() {
        for n in [0usize, 1, 40, 900, 5000] {
            let t = Turn { output: json!({ "text": "a".repeat(n) }), ..turn() };
            let r = build(t, false);
            assert_eq!(
                r["limits"]["bytes"].as_u64().unwrap() as usize,
                r.to_string().len(),
                "n={n}"
            );
        }
    }

    #[test]
    fn 上限に収まっているうちは一文字も削らない() {
        let t = Turn {
            think: json!({ "text": "思".repeat(3000), "chars": 3000 }),
            output: json!({ "text": "答".repeat(300) }),
            ..turn()
        };
        let r = build(t, false);
        assert!(r.to_string().len() > 8 * 1024, "8 KiB は超えている前提の試験");
        assert_eq!(r["think"]["text"].as_str().unwrap().chars().count(), 3000);
        assert_eq!(r["limits"]["drop_stage"], json!(0));
    }

    #[test]
    fn 削るのは思考から始まる() {
        let t = Turn {
            think: json!({ "text": "思".repeat(8000), "chars": 8000 }),
            output: json!({ "text": "答".repeat(3000) }),
            ..turn()
        };
        let r = build(t, false);
        assert_eq!(r["limits"]["drop_stage"], json!(1));
        assert!(r["think"]["text"].as_str().unwrap().chars().count() <= 512);
        assert_eq!(r["output"]["text"].as_str().unwrap().chars().count(), 3000, "本文はまだ削らない");
        assert_eq!(r["limits"]["dropped"]["think.text"], json!(8000 - 512));
    }

    #[test]
    fn 何段でも落とさない塊がある() {
        let t = Turn {
            think: json!({ "text": "思".repeat(8000) }),
            output: json!({ "text": "答".repeat(8000) }),
            tools: (0..40).map(|i| json!({ "i": i, "args": { "c": "x".repeat(200) } })).collect(),
            cost: json!({ "secs": 1.5, "iters": 3 }),
            engine: json!({ "kind": "core", "model": "m" }),
            quality: json!({ "did_work": true }),
            refs: json!({ "events_path": "sessions/x/events.jsonl" }),
            ..turn()
        };
        let r = build(t, false);
        assert_eq!(r["stop"]["reason"], json!("accepted"));
        assert_eq!(r["cost"]["iters"], json!(3));
        assert_eq!(r["engine"]["kind"], json!("core"));
        assert_eq!(r["quality"]["did_work"], json!(true));
        assert_eq!(r["ref"]["events_path"], json!("sessions/x/events.jsonl"));
        assert_eq!(r["counters"]["completion_check_ran"], json!(1));
    }

    #[test]
    fn 思考を全部捨てても有ったことは残る() {
        let t = Turn {
            think: json!({ "text": "思".repeat(8000), "chars": 8000 }),
            output: json!({ "text": "答".repeat(8000) }),
            tools: (0..40)
                .map(|i| json!({ "i": i, "args": { "c": "x".repeat(300) }, "result": { "head": "h".repeat(400) } }))
                .collect(),
            ..turn()
        };
        let r = build(t, false);
        if r["limits"]["drop_stage"].as_u64().unwrap() >= 5 {
            assert_eq!(r["think"]["chars"], json!(8000), "何文字あったかは残す");
            assert_eq!(r["think"]["text_dropped"], json!(true));
        }
    }

    #[test]
    fn 一日の上限を超えたら封筒だけにして記録は止めない() {
        let t = Turn {
            input: json!({ "text": "依頼" }),
            output: json!({ "text": "答" }),
            think: json!({ "text": "思" }),
            tools: vec![json!({ "name": "Bash" })],
            ..turn()
        };
        let r = build(t, true);
        assert_eq!(r.get("input"), None);
        assert_eq!(r.get("output"), None);
        assert_eq!(r.get("think"), None);
        assert_eq!(r.get("tools"), None);
        assert_eq!(r["stop"]["reason"], json!("accepted"));
        assert_eq!(r["limits"]["over_budget"], json!(true));
        assert_eq!(r["quality"]["flags"], json!(["day_cap"]));
    }

    #[test]
    fn 旗は重ねても増えない() {
        let mut q = json!({ "flags": ["day_cap", "empty_answer"] });
        push_flag(&mut q, "day_cap");
        assert_eq!(q["flags"], json!(["day_cap", "empty_answer"]));
        let mut none = Value::Null;
        push_flag(&mut none, "x");
        assert_eq!(none["flags"], json!(["x"]));
    }

    // ---- 束ねる -----------------------------------------------------------

    #[test]
    fn 束はバイト数と行数の両方で切れる() {
        let sizes = vec![100usize; 10];
        assert_eq!(plan_batch(&sizes, 350, 100), 3);
        assert_eq!(plan_batch(&sizes, 100_000, 4), 4);
        assert_eq!(plan_batch(&sizes, 100_000, 100), 10);
    }

    #[test]
    fn 行の位置は文字列の長さからでなく読んだ位置から決まる() {
        let buf = b"{\"a\":1}\r\n\n{\"b\":2}\n";
        let v = split_lines(buf, 100);
        assert_eq!(v.len(), 2, "空行は返さない");
        assert_eq!(v[0].0, "{\"a\":1}");
        assert_eq!(v[0].1, 100 + 9, "\\r\\n のぶんも位置には数える");
        assert_eq!(v[1].0, "{\"b\":2}");
        assert_eq!(v[1].1, 100 + buf.len() as u64, "空行を飛ばしても位置はずれない");
    }

    #[test]
    fn 日本語の行でも位置がずれない() {
        let line = json!({ "text": "日本語" }).to_string();
        let buf = format!("{line}\n{line}\n").into_bytes();
        let v = split_lines(&buf, 0);
        assert_eq!(v.len(), 2);
        assert_eq!(v[1].1, buf.len() as u64);
        assert_eq!(v[0].1, line.len() as u64 + 1);
    }

    #[test]
    fn 大きすぎる一行でも列は進む() {
        assert_eq!(plan_batch(&[5_000_000], 1024, 500), 1);
        assert_eq!(plan_batch(&[5_000_000, 10], 1024, 500), 1);
        assert_eq!(plan_batch(&[], 1024, 500), 0);
    }

    // ---- 掃除 -------------------------------------------------------------

    fn days(v: &[(&str, u64)]) -> Vec<(String, u64)> {
        v.iter().map(|(d, b)| (d.to_string(), *b)).collect()
    }

    #[test]
    fn 合計が上限を超えたら古い日から消す() {
        let d = days(&[("2026-08-08", 40), ("2026-08-09", 40), ("2026-08-10", 40), ("2026-08-11", 40)]);
        assert_eq!(plan_evict(&d, 100, 365, "2026-08-11"), vec!["2026-08-08", "2026-08-09"]);
    }

    #[test]
    fn 日数の上限でも消す() {
        let d = days(&[("2026-08-08", 1), ("2026-08-09", 1), ("2026-08-10", 1), ("2026-08-11", 1)]);
        assert_eq!(plan_evict(&d, u64::MAX, 2, "2026-08-11"), vec!["2026-08-08", "2026-08-09"]);
    }

    #[test]
    fn 今日は何があっても消さない() {
        let d = days(&[("2026-08-11", 10_000)]);
        assert!(plan_evict(&d, 1, 1, "2026-08-11").is_empty());
        let d = days(&[("2026-08-10", 10_000), ("2026-08-11", 10_000)]);
        assert_eq!(plan_evict(&d, 1, 1, "2026-08-11"), vec!["2026-08-10"]);
    }

    #[test]
    fn 収まっていれば何も消さない() {
        let d = days(&[("2026-08-10", 10), ("2026-08-11", 10)]);
        assert!(plan_evict(&d, 1000, 30, "2026-08-11").is_empty());
    }

    // ---- 圧縮 -------------------------------------------------------------

    #[test]
    fn gzipは展開して元に戻る() {
        let body = "{\"a\":\"日本語のテキスト\"}\n".repeat(200);
        let gz = gzip(body.as_bytes()).unwrap();
        assert!(gz.len() < body.len(), "縮んでいない: {} → {}", body.len(), gz.len());
        let mut back = String::new();
        flate2::read::GzDecoder::new(&gz[..]).read_to_string(&mut back).unwrap();
        assert_eq!(back, body);
    }

    // ---- 素性 -------------------------------------------------------------

    #[test]
    fn host_id_同じ塩と名前なら同じ値になる() {
        // ★元の `host_idは同じ名前なら同じ値になる`（journal.rs）は `host_id()` 経由で
        //   `dir()`/`crate::app_root()` に依存するファイル読み書きを挟むため、
        //   こちらへは移せなかった（journal.rs 側に残置・未実行のまま）。
        //   ここではハッシュ計算そのもの（`host_id_with_salt`）を直接叩く。
        let a = host_id_with_salt("同じ塩", "HOST-A");
        let b = host_id_with_salt("同じ塩", "HOST-A");
        let c = host_id_with_salt("同じ塩", "other-host");
        let d = host_id_with_salt("違う塩", "HOST-A");
        assert_eq!(a, b, "同じ塩・同じ名前なら同じ値");
        assert_ne!(a, c, "名前が違えば別の値");
        assert_ne!(a, d, "塩が違えば別の値");
        assert_eq!(a.len(), 16);
    }
}
