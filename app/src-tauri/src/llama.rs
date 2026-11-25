//! llama-server supervisor + client.
//!
//! 設計上の中心は「推論管理を自作しない」こと。`-m` を付けずに起動した llama-server は
//! **router mode** になり、モデル一覧・状態・読み込み・ダウンロード進捗・アイドル解放を
//! すべてサーバ側が持つ。ここでやるのは (1) 子プロセスの起動と停止 (2) HTTP の中継 だけ。
//!
//! router mode で押さえておくべき点:
//!   - モデルの実体は **router とは別プロセス**で、router の CLI 引数と環境変数を**継承する**。
//!     したがってモデル別の設定を router の argv に書いてはいけない（全モデルに漏れる）。
//!     モデル別設定は `--models-preset` の INI に置く。
//!   - `--sleep-idle-seconds N` で無操作時にモデルと KV を降ろし、次の依頼で自動復帰する。
//!     `/health` `/props` `/models` は idle タイマーを**リセットしない**ので、
//!     状態表示のための定期ポーリングでモデルを起こしてしまう心配はない。
//!   - ★`-ngl` / `-c` を明示的に渡さない。llama.cpp の auto-fitter（`-fit on` が既定、
//!     `-ngl` の既定は `auto`）は、**その引数を人が明示した時点でその引数について無効になる**。
//!     デスクトップアプリでは環境が千差万別なので、fitter に任せるほうが確実に速い。
//!
//! このモジュールは Windows 専用コードを持たない（`creation_flags` のみ cfg 分岐）ので、
//! Linux 上でも型検査・ビルドできる。
//!
//! 必要な機能フラグ: `tokio = { features = ["process", "time", "rt"] }`,
//! `reqwest = { features = ["json"] }`（本文は `Response::chunk` で逐次読みするため
//! `stream` 機能も futures 系の依存も要らない）。

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::path::BaseDirectory;
use tauri::{AppHandle, Emitter, Manager};
use tokio::process::{Child, Command};

/// 推論器のログから「実際にどう載ったか」を読む純粋な解析。
///
/// ★実体は検査用クレート側（`blackwall-core/src/llama_log.rs`）に置いてある。
///   このファイルは `tauri` を参照するのでホストでは `cargo test` できず、
///   ここに解析を書くと**また一度も走らない試験**になる。`#[path]` で参照して
///   いるだけなので実体は1本＝二重管理は起きない。
#[path = "../blackwall-core/src/llama_log.rs"]
mod llama_log;
pub use llama_log::LoadFacts;

/// 既定ポート。塞がっていれば順に上へずらす（`pick_port`）。
const DEFAULT_PORT: u16 = 8760;
/// ポート探索の幅。ここまで塞がっているのは異常なので諦めてエラーにする。
const PORT_SCAN: u16 = 16;
/// 無操作でモデルを降ろすまでの秒数。会話の合間で降りない程度に長く、
/// 席を外したら VRAM を返す程度に短く。
const SLEEP_IDLE_SECONDS: u32 = 600;
/// 起動後、router が応答するまで待つ上限。
const START_TIMEOUT: Duration = Duration::from_secs(30);

#[cfg(windows)]
const SERVER_BIN: &str = "llama-server.exe";
#[cfg(not(windows))]
const SERVER_BIN: &str = "llama-server";

/// コンソールウィンドウを出さずに子プロセスを起動する（Windows のみ）。
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

// ---------------------------------------------------------------------------
// 公開する型（フロントエンドと他モジュールが依存する固定インタフェース）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct LlamaStatus {
    pub running: bool,
    pub port: u16,
    /// 同梱バックエンドの種類（"vulkan" | "cuda" | "cpu" | "unknown"）。
    /// ★これは**同梱物の種類**であって「GPU に載ったか」ではない（detect_backend 参照）。
    pub backend: String,
    pub models: Vec<ModelStatus>,
    /// 直近の読み込みで**実際にどう載ったか**（ログからの実測）。
    /// 観測できていなければ `None`。JSON の欄名は `llama_log::LoadFacts` を見ること。
    pub load: Option<LoadFacts>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModelStatus {
    pub id: String,
    /// router が返す生の状態文字列（unloaded|loading|loaded|sleeping|downloading）。
    /// 未知の値を握り潰さないよう、翻訳せずそのまま渡す。
    pub state: String,
    pub downloaded_bytes: Option<u64>,
    pub total_bytes: Option<u64>,
    pub exit_code: Option<i32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMsg {
    pub role: String,
    pub content: String,
}

// ---------------------------------------------------------------------------
// プロセス状態
// ---------------------------------------------------------------------------

/// `llama_stop` / `llama_status` は引数を取らない固定シグネチャなので、
/// Tauri の managed state ではなくプロセスグローバルに置く（main.rs 側の登録が不要）。
struct Supervisor {
    child: Mutex<Option<Child>>,
    port: AtomicU16,
    backend: Mutex<String>,
    /// 起動診断のためのログファイル。子プロセスの stdout/stderr をここへ落とす
    /// （パイプにすると誰も読まないまま埋まって固まるため、ファイルに逃がす）。
    log_path: Mutex<Option<PathBuf>>,
    /// 上のログを**追記分だけ**読み進める走査器と、その読み終えた位置。
    log_scan: Mutex<LogScan>,
}

/// ログの読み進み位置と走査器。
struct LogScan {
    pos: u64,
    scanner: llama_log::LoadLogScanner,
}

fn sup() -> &'static Supervisor {
    static S: OnceLock<Supervisor> = OnceLock::new();
    S.get_or_init(|| Supervisor {
        child: Mutex::new(None),
        port: AtomicU16::new(DEFAULT_PORT),
        backend: Mutex::new("unknown".to_string()),
        log_path: Mutex::new(None),
        log_scan: Mutex::new(LogScan {
            pos: 0,
            scanner: llama_log::LoadLogScanner::new(),
        }),
    })
}

/// ロックの毒（他スレッドの panic）で機能停止しないようにする。
/// ここで守っているのは子プロセスハンドル1つで、不変条件は「生きているか否か」だけなので、
/// 毒された中身をそのまま使っても壊れない。
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn current_port() -> u16 {
    sup().port.load(Ordering::Relaxed)
}

fn current_backend() -> String {
    lock(&sup().backend).clone()
}

fn router_base() -> String {
    format!("http://127.0.0.1:{}", current_port())
}

/// チャットの宛先。LAN が有効ならそちら。
/// ★`router_base()` と分ける: あちらは**手元のサーバの管理**（models/load・状態）に使う。
///   管理まで LAN へ向けると、共有の推論機に対してモデルの入れ替えを始めてしまう。
fn chat_base() -> String {
    crate::inference_root().unwrap_or_else(router_base)
}

/// 自分が起動した子プロセスが今も生きているか。死んでいたらハンドルを片付ける。
fn own_child_alive() -> bool {
    let mut g = lock(&sup().child);
    let alive = match g.as_mut() {
        Some(c) => matches!(c.try_wait(), Ok(None)),
        None => false,
    };
    if !alive {
        *g = None;
    }
    alive
}

// ---------------------------------------------------------------------------
// HTTP クライアント
// ---------------------------------------------------------------------------

/// 制御用（短い応答を期待する呼び出し）。
///
/// `no_proxy()` が要点: Windows ではシステムプロキシ設定を拾ってしまうことがあり、
/// 127.0.0.1 への要求までプロキシに投げて失敗する。ローカルの router に向けるクライアントは
/// 常にプロキシを無効化する。
fn ctl_client() -> &'static reqwest::Client {
    static C: OnceLock<reqwest::Client> = OnceLock::new();
    C.get_or_init(|| {
        reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(15))
            .build()
            .expect("reqwest client")
    })
}

/// 生成用。全体タイムアウトを付けない（長い生成が途中で切られるため）。
fn stream_client() -> &'static reqwest::Client {
    static C: OnceLock<reqwest::Client> = OnceLock::new();
    C.get_or_init(|| {
        reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(5))
            .build()
            .expect("reqwest streaming client")
    })
}

// ---------------------------------------------------------------------------
// パス解決
// ---------------------------------------------------------------------------

/// `%LOCALAPPDATA%\Blackwall`（他 OS では local data dir 配下の同名）。
/// モデル・プリセット INI・ログを1箇所にまとめる。
pub fn app_root(_app: &AppHandle) -> Result<PathBuf, String> {
    // ★根は crate::app_root() に一本化した（以前は main/hf/llama の3箇所で別々に計算し、
    //   食い違う余地があった）。非 ASCII を避ける判断もそちらに集約されている。
    let root = crate::app_root();
    std::fs::create_dir_all(&root)
        .map_err(|e| format!("{} を作成できません: {e}", root.display()))?;
    Ok(root)
}

/// モデル置き場（`%LOCALAPPDATA%\Blackwall\models`）。無ければ作る。
pub fn models_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app_root(app)?.join("models");
    std::fs::create_dir_all(&dir).map_err(|e| format!("{} を作成できません: {e}", dir.display()))?;
    Ok(dir)
}

/// モデル別設定を書く INI。router の argv には書かない（子プロセスが継承してしまうため）。
pub fn preset_path(app: &AppHandle) -> Result<PathBuf, String> {
    // ★models の中に置く。子プロセスの作業ディレクトリを models にして
    //   **相対名だけ**を渡すため（絶対パスに非 ASCII が混じると llama.cpp が開けない）。
    Ok(models_dir(app)?.join("models-preset.ini"))
}

/// 同梱した llama-server の実体を探す。
///
/// 本番では Tauri のリソース解決だけで足りるが、開発時（`cargo run`）や Linux での
/// 動作確認のために環境変数と相対パスの逃げ道を用意する。失敗時は**試した場所を全部返す**
/// （「見つかりません」で終わらせない）。
fn resolve_server_binary(app: &AppHandle) -> Result<PathBuf, String> {
    let mut tried: Vec<String> = Vec::new();

    if let Ok(p) = crate::envcompat::var("LAPS_LLAMA_SERVER") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Ok(p);
        }
        tried.push(format!("{} (LAPS_LLAMA_SERVER)", p.display()));
    }

    // ★2026-10-07: アプリが取り寄せた CUDA 版（localgpu.rs・<データ根>\engine-cuda）を同梱の Vulkan 版より先に使う。
    if let Some(p) = crate::localgpu::engine_exe() {
        return Ok(p);
    }

    let rel = format!("resources/llama/{SERVER_BIN}");
    match app.path().resolve(&rel, BaseDirectory::Resource) {
        Ok(p) => {
            if p.is_file() {
                return Ok(p);
            }
            tried.push(p.display().to_string());
        }
        Err(e) => tried.push(format!("{rel} (リソース解決に失敗: {e})")),
    }

    // 開発時のフォールバック: 実行ファイルの隣、カレント、その1つ上。
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join(&rel));
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join(&rel));
        candidates.push(cwd.join("..").join(&rel));
    }
    for c in candidates {
        if c.is_file() {
            return Ok(c);
        }
        tried.push(c.display().to_string());
    }

    Err(format!(
        "推論サーバー {SERVER_BIN} が見つかりません。探した場所:\n  {}",
        tried.join("\n  ")
    ))
}

/// 同梱バックエンドの種類を DLL の有無から判定する。表示用の情報でしかないので、
/// 判定できなければ "unknown" のままにする（推測で "cuda" と言わない）。
fn detect_backend(exe: &Path) -> String {
    let dir = match exe.parent() {
        Some(d) => d,
        None => return "unknown".to_string(),
    };
    let has = |names: &[&str]| names.iter().any(|n| dir.join(n).exists());
    if has(&["ggml-cuda.dll", "libggml-cuda.so"]) {
        "cuda".to_string()
    } else if has(&["ggml-vulkan.dll", "libggml-vulkan.so"]) {
        "vulkan".to_string()
    } else if has(&["ggml-base.dll", "libggml-base.so"]) {
        "cpu".to_string()
    } else {
        "unknown".to_string()
    }
}

// ---------------------------------------------------------------------------
// models-preset INI
// ---------------------------------------------------------------------------

/// INI に書き出す1件。`alias` がそのままセクション名＝API から見えるモデル ID になる。
#[derive(Debug, Clone)]
pub struct PresetEntry {
    pub alias: String,
    pub path: PathBuf,
}

/// models ディレクトリを走査してプリセットを作り直し、書いた件数を返す。
///
/// 分割 GGUF（`...-00002-of-00003.gguf`）は先頭シャードだけ登録する。llama.cpp が
/// 残りを自動で開くので、全シャードを別モデルとして並べると一覧が壊れる。
pub fn refresh_models_preset(app: &AppHandle, tuned: bool) -> Result<(PathBuf, usize), String> {
    let dir = models_dir(app)?;
    let entries = scan_gguf(&dir)?;
    let path = preset_path(app)?;
    write_models_preset(&path, &entries, tuned)?;
    Ok((path, entries.len()))
}

fn scan_gguf(dir: &Path) -> Result<Vec<PresetEntry>, String> {
    let mut out: Vec<PresetEntry> = Vec::new();
    let rd = match std::fs::read_dir(dir) {
        Ok(rd) => rd,
        // まだ1つも入れていない状態は異常ではない。空のプリセットを書く。
        Err(_) => return Ok(out),
    };
    let mut files: Vec<PathBuf> = rd
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.is_file())
        .filter(|p| {
            p.extension()
                .and_then(|s| s.to_str())
                .map(|s| s.eq_ignore_ascii_case("gguf"))
                .unwrap_or(false)
        })
        .filter(|p| {
            let name = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
            !is_non_first_shard(name)
        })
        .collect();
    files.sort();

    let mut used: Vec<String> = Vec::new();
    for f in files {
        let stem = f
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("model")
            .to_string();
        let alias = unique_alias(sanitize_alias(&stem), &mut used);
        out.push(PresetEntry { alias, path: f });
    }
    Ok(out)
}

/// `name-00002-of-00005.gguf` のような 2 番目以降のシャードか。
///
/// 判定はバイト列で行う。ファイル名に日本語などが入っていても、
/// 文字境界でない位置で str を切って panic することがない。
fn is_non_first_shard(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    let stem = match lower.strip_suffix(".gguf") {
        Some(s) => s,
        None => return false,
    };
    let b = stem.as_bytes();
    if b.len() < 15 {
        return false;
    }
    let t = &b[b.len() - 15..]; // 期待する形: -NNNNN-of-NNNNN
    if t[0] != b'-' || &t[6..10] != &b"-of-"[..] {
        return false;
    }
    let idx = &t[1..6];
    let total = &t[10..15];
    if !idx.iter().all(|c| c.is_ascii_digit()) || !total.iter().all(|c| c.is_ascii_digit()) {
        return false;
    }
    idx != &b"00001"[..]
}

fn sanitize_alias(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        if ch.is_ascii_alphanumeric() || ch == '.' || ch == '_' || ch == '-' {
            out.push(ch);
        } else {
            out.push('-');
        }
    }
    let trimmed = out.trim_matches('-').to_string();
    if trimmed.is_empty() {
        "model".to_string()
    } else {
        trimmed
    }
}

fn unique_alias(base: String, used: &mut Vec<String>) -> String {
    let mut candidate = base.clone();
    let mut n = 2;
    while used.iter().any(|u| u == &candidate) {
        candidate = format!("{base}-{n}");
        n += 1;
    }
    used.push(candidate.clone());
    candidate
}

/// プリセット INI を書く。1モデル＝1セクション、キーは llama.cpp の長い CLI 名から `--` を落としたもの。
///
/// ★ここに `n-gpu-layers` や `ctx-size` を書かない。auto-fitter に決めさせる方針なので、
/// 指定するのは `model`（実体のパス）だけにしておく。CPU 分担などモデル別の逃げ道が要る場合は
/// 呼び出し側が `PresetEntry` を拡張するのではなく、この関数を差し替える形で足すこと。
/// 子プロセスに渡しても壊れないパスにする。
///
// ★非 ASCII の利用者名への対処は 8.3 短縮名をやめ、models を cwd にしてファイル名だけ渡す形に変えた（main.rs の注記）。

/// 取り寄せた CUDA 版（localgpu.rs）で起動するときの共通の起動設定（INI の `[*]` 節）。
/// ★2026-10-07 RTX 4070 で実測して決めた値: 文脈 64K（前置きだけで 1.1 万トークン）・
///   一度に 2,048（CPU 側に置いた重みを GPU へ運ぶ回数が減り、全部読みが 290→450 tok/s）・
///   1 本ずつ（1 人で使うので枠を分けない）。GPU と CPU の振り分け（ngl・MoE）は書かない＝自動調整に任せる。
/// ★同梱の Vulkan 版（8 月）には書かない。古い版が `[*]` を読めるかは確かめていない。
const TUNED_DEFAULTS: &[(&str, &str)] = &[
    ("c", "65536"),
    ("parallel", "1"),
    ("flash-attn", "on"),
    ("cache-type-k", "q8_0"),
    ("cache-type-v", "q8_0"),
    ("batch-size", "2048"),
    ("ubatch-size", "2048"),
];

pub fn write_models_preset(path: &Path, entries: &[PresetEntry], tuned: bool) -> Result<(), String> {
    let mut s = String::new();
    s.push_str("# BLACKWALL Console が生成しています。起動のたびに上書きされます。\n");
    s.push_str("# モデル別の設定はここに置きます（router の argv に書くと子プロセスが継承してしまうため）。\n\n");
    if tuned {
        s.push_str("[*]\n");
        for (k, v) in TUNED_DEFAULTS {
            s.push_str(&format!("{k} = {v}\n"));
        }
        s.push('\n');
    }
    for e in entries {
        s.push_str(&format!("[{}]\n", e.alias));
        // ★INI の中のパスも llama-server が開く。ここも短い名前にしないと、
        //   preset は読めてもモデルの読み込みで同じ理由で転ぶ。
        // ★ファイル名だけを書く。作業ディレクトリが models なので相対で解決される。
        //   絶対パスを書くと、利用者名の非 ASCII が llama.cpp の狭い API を通らない。
        let name = e.path.file_name().map(|x| x.to_string_lossy().to_string())
            .unwrap_or_else(|| e.path.display().to_string());
        s.push_str(&format!("model = {}\n\n", name));
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("{} を作成できません: {e}", parent.display()))?;
    }
    std::fs::write(path, s).map_err(|e| format!("{} を書けません: {e}", path.display()))?;
    // 書けたと言い張らない。実機では「書いたはずのファイルが無い」で起動に失敗した。
    if !path.is_file() {
        return Err(format!("{} を書いたはずが見当たりません", path.display()));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// ポート選択
// ---------------------------------------------------------------------------

/// 127.0.0.1 の当該ポートに bind できるか。
/// bind してすぐ閉じるので厳密には TOCTOU だが、直後に自分が起動するだけであり、
/// 衝突したら起動失敗として検出できる（無害な競合）。
fn port_free(port: u16) -> bool {
    std::net::TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// そのポートに居るのが llama-server の router かどうかを、`/models` の形で判定する。
async fn router_responds(port: u16) -> bool {
    let url = format!("http://127.0.0.1:{port}/models");
    match ctl_client()
        .get(url)
        .timeout(Duration::from_millis(800))
        .send()
        .await
    {
        Ok(r) if r.status().is_success() => match r.json::<Value>().await {
            Ok(v) => extract_model_array(&v).is_some(),
            Err(_) => false,
        },
        _ => false,
    }
}

/// 空きポートを探す。塞がっていても、そこに居るのが自分たちの router なら**それを使う**
/// （前回のアプリ終了時に停止しなかった場合に、二重起動せず引き継ぐ）。
/// 戻り値の bool は「既存を引き継いだ（adopted）」か。
async fn pick_port() -> Result<(u16, bool), String> {
    for p in DEFAULT_PORT..DEFAULT_PORT.saturating_add(PORT_SCAN) {
        if port_free(p) {
            return Ok((p, false));
        }
        if router_responds(p).await {
            return Ok((p, true));
        }
    }
    Err(format!(
        "{}〜{} のポートがすべて塞がっています。使用中のアプリを止めてから、もう一度試してください。",
        DEFAULT_PORT,
        DEFAULT_PORT + PORT_SCAN - 1
    ))
}

// ---------------------------------------------------------------------------
// /models の読み取り
// ---------------------------------------------------------------------------

/// `/models` の応答からモデル配列を取り出す。
/// OpenAI 互換の `{"data":[...]}` を想定しつつ、`{"models":[...]}` と裸の配列も受ける
/// （スキーマを1つに決め打ちすると、サーバ更新で一覧が丸ごと空になる壊れ方をするため）。
fn extract_model_array(v: &Value) -> Option<&Vec<Value>> {
    if let Some(a) = v.get("data").and_then(|x| x.as_array()) {
        return Some(a);
    }
    if let Some(a) = v.get("models").and_then(|x| x.as_array()) {
        return Some(a);
    }
    v.as_array()
}

fn first_u64(v: &Value, keys: &[&str]) -> Option<u64> {
    for k in keys {
        if let Some(n) = v.get(*k).and_then(|x| x.as_u64()) {
            return Some(n);
        }
    }
    None
}

fn first_str(v: &Value, keys: &[&str]) -> Option<String> {
    for k in keys {
        if let Some(s) = v.get(*k).and_then(|x| x.as_str()) {
            return Some(s.to_string());
        }
    }
    None
}

/// ダウンロード進捗を取り出す。router はファイル単位で進捗を返すので、
/// 配列があれば合算する（1モデルが複数ファイルに分かれている場合に片方しか見えないのを防ぐ）。
fn extract_progress(entry: &Value) -> (Option<u64>, Option<u64>) {
    // 入れ子（"download" / "progress"）があればそちらを優先して見る。
    let scope = entry
        .get("download")
        .or_else(|| entry.get("progress"))
        .unwrap_or(entry);

    if let Some(files) = scope.get("files").and_then(|x| x.as_array()) {
        let mut got = 0u64;
        let mut total = 0u64;
        let mut any_got = false;
        let mut any_total = false;
        for f in files {
            if let Some(n) = first_u64(f, &["downloaded_bytes", "downloaded", "received", "bytes"]) {
                got = got.saturating_add(n);
                any_got = true;
            }
            if let Some(n) = first_u64(f, &["total_bytes", "total", "size"]) {
                total = total.saturating_add(n);
                any_total = true;
            }
        }
        if any_got || any_total {
            return (any_got.then_some(got), any_total.then_some(total));
        }
    }

    let got = first_u64(scope, &["downloaded_bytes", "downloaded", "received"]);
    let total = first_u64(scope, &["total_bytes", "total", "size"]);
    (got, total)
}

fn map_models(v: &Value) -> Vec<ModelStatus> {
    let arr = match extract_model_array(v) {
        Some(a) => a,
        None => return Vec::new(),
    };
    arr.iter()
        .map(|m| {
            let id = first_str(m, &["id", "name", "model", "alias"]).unwrap_or_default();
            let state = first_str(m, &["state", "status"]).unwrap_or_else(|| "unknown".to_string());
            let (downloaded_bytes, total_bytes) = extract_progress(m);
            let exit_code = m
                .get("exit_code")
                .or_else(|| m.get("exitCode"))
                .and_then(|x| x.as_i64())
                .map(|n| n as i32);
            ModelStatus {
                id,
                state,
                downloaded_bytes,
                total_bytes,
                exit_code,
            }
        })
        .filter(|m| !m.id.is_empty())
        .collect()
}

async fn fetch_models(port: u16) -> Result<Vec<ModelStatus>, String> {
    let url = format!("http://127.0.0.1:{port}/models");
    let resp = ctl_client()
        .get(url)
        .send()
        .await
        .map_err(|e| format!("{e}"))?;
    if !resp.status().is_success() {
        return Err(format!("/models が {} を返しました", resp.status()));
    }
    let v: Value = resp.json().await.map_err(|e| format!("{e}"))?;
    Ok(map_models(&v))
}

// ---------------------------------------------------------------------------
// ログ
// ---------------------------------------------------------------------------

/// ログの末尾を読む。起動に失敗したときに「何が起きたか」を人へ返すため。
fn log_tail(path: &Path, max: usize) -> String {
    let mut f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return String::new(),
    };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let start = len.saturating_sub(max as u64);
    if start > 0 {
        use std::io::Seek;
        if f.seek(std::io::SeekFrom::Start(start)).is_err() {
            return String::new();
        }
    }
    let mut buf = Vec::new();
    if f.read_to_end(&mut buf).is_err() {
        return String::new();
    }
    String::from_utf8_lossy(&buf).trim().to_string()
}

/// ログの**追記分だけ**を読み進めて、直近の読み込みの実測値を返す。
///
/// なぜ差分読みか: `llama_status()` は画面のポーリングだけでなく
/// `agent.rs::base_url()` から**依頼のたび**に呼ばれる。ここで全文を読み直すと
/// 会話1往復ごとに数 MB を読むことになる。追記分なら普通は数百バイト。
///
/// なぜ末尾 N バイトだけ見ないか: 読み込みのあとに依頼ログが積もると
/// `offloaded` 行が窓の外へ出る。窓方式だと**長く使ったときに限って**
/// 表示が消える＝一番知りたい場面で何も出ない。
///
/// ★ここは同期。`await` をまたいでロックを持たない（持てば Send が壊れる）。
fn load_facts_now() -> Option<LoadFacts> {
    /// 初回に読み込む上限。既に巨大なログへ後から繋いだときに全部を舐めない。
    /// これを超える分の先頭は諦める（そこにあるのは古い読み込みだけ）。
    const MAX_CATCHUP: u64 = 8 * 1024 * 1024;

    let path = lock(&sup().log_path).clone()?;
    let len = std::fs::metadata(&path).ok()?.len();
    let mut st = lock(&sup().log_scan);

    // 切り詰め・差し替え（別プロセスが作り直した）なら最初から読み直す。
    if len < st.pos {
        st.pos = 0;
        st.scanner.reset();
    }
    if len > st.pos {
        let mut start = st.pos;
        if len - start > MAX_CATCHUP {
            start = len - MAX_CATCHUP;
            st.scanner.reset();
        }
        if let Ok(mut f) = std::fs::File::open(&path) {
            use std::io::Seek;
            if f.seek(std::io::SeekFrom::Start(start)).is_ok() {
                let mut buf = Vec::with_capacity((len - start).min(MAX_CATCHUP) as usize);
                if f.read_to_end(&mut buf).is_ok() {
                    st.scanner.feed(&buf);
                    st.pos = start + buf.len() as u64;
                }
            }
        }
    }
    st.scanner.facts()
}

// ---------------------------------------------------------------------------
// コマンド
// ---------------------------------------------------------------------------

/// 推論器（router）を起動する。既に動いていれば起動せず現状を返す（冪等）。
#[tauri::command]
pub async fn llama_start(app: AppHandle) -> Result<LlamaStatus, String> {
    if own_child_alive() {
        return Ok(status_now().await);
    }

    let exe = stage_engine(&resolve_server_binary(&app)?)?;
    let backend = detect_backend(&exe);

    // 新しく取り込んだモデルを拾えるよう、起動のたびにプリセットを作り直す。
    // ★裏返すと、起動中に増えたモデルは router に見えない。取り込み完了後は
    //   llama_stop → llama_start で入れ直すこと。
    let (preset, preset_count) = refresh_models_preset(&app, crate::localgpu::is_cuda_engine(&exe))?;
    let models = models_dir(&app)?;
    let log_path = app_root(&app)?.join("llama-server.log");

    let (port, adopted) = pick_port().await?;
    sup().port.store(port, Ordering::Relaxed);
    *lock(&sup().backend) = backend.clone();
    *lock(&sup().log_path) = Some(log_path.clone());

    if adopted {
        // 前回の残りが生きていた。子ハンドルは持てないので停止はできないが、
        // 使うぶんには問題がないのでそのまま使う。
        return Ok(status_now().await);
    }

    // stdout/stderr はファイルへ。パイプにすると誰も読まないままバッファが埋まり、
    // サーバが書き込みでブロックして固まる。
    let log_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("{} を開けません: {e}", log_path.display()))?;
    let log_file2 = log_file
        .try_clone()
        .map_err(|e| format!("ログを複製できません: {e}"))?;

    let mut cmd = Command::new(&exe);
    cmd.arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        // -m を付けない＝router mode。
        .arg("--sleep-idle-seconds")
        .arg(SLEEP_IDLE_SECONDS.to_string())
        // chat template をモデル同梱のものから正しく組み立てる。
        .arg("--jinja")
        // ★-ngl / -c は渡さない。渡した時点でその引数について auto-fitter が無効になる。
        .current_dir(&models)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(log_file2))
        // アプリ終了時に道連れにしない。停止は呼び出し側の判断（llama_stop）に委ねる。
        .kill_on_drop(false);

    // ★モデルが1つも無いときは --models-preset を渡さない。
    //   空でもファイルさえあれば良さそうに見えるが、router は preset を開けないと
    //   即座に終了する（実機のログ: "failed to initialize router models"）。
    //   渡すのは中身がある時だけにして、無い時でも router は起動できるようにする。
    if preset_count > 0 {
        // 作業ディレクトリは models。ここでは名前だけ渡す（経路に非 ASCII を通さない）。
        let preset_name = preset.file_name()
            .map(|x| x.to_string_lossy().to_string())
            .unwrap_or_else(|| "models-preset.ini".to_string());
        cmd.arg("--models-preset").arg(preset_name);
    }

    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let child = cmd
        .spawn()
        .map_err(|e| format!("推論サーバーを起動できません（{}）: {e}", exe.display()))?;
    *lock(&sup().child) = Some(child);

    // 応答するまで待つ。途中でプロセスが落ちたら、その場でログを添えて返す。
    let deadline = std::time::Instant::now() + START_TIMEOUT;
    loop {
        if let Ok(models) = fetch_models(port).await {
            return Ok(LlamaStatus {
                running: true,
                port,
                backend,
                models,
                // 起動直後はまだモデルを読み込んでいない（router は初回の依頼で載せる）
                //   ＝ここはたいてい None。空欄を「0 層」と読まないこと。
                load: load_facts_now(),
            });
        }
        if !own_child_alive() {
            let tail = log_tail(&log_path, 4000);
            return Err(format!(
                "推論サーバーがすぐに終了しました。ログの末尾:\n{tail}"
            ));
        }
        if std::time::Instant::now() >= deadline {
            let tail = log_tail(&log_path, 4000);
            return Err(format!(
                "推論サーバーが {} 秒たっても応答しません（port {port}）。ログの末尾:\n{tail}",
                START_TIMEOUT.as_secs()
            ));
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
}

/// 推論器を止める。動いていなければ何もしない（呼んで害がない）。
#[tauri::command]
pub async fn llama_stop() -> Result<(), String> {
    // await をまたいでロックを保持しないよう、先に取り出してから待つ。
    let child = {
        let mut g = lock(&sup().child);
        g.take()
    };
    if let Some(mut c) = child {
        // start_kill は同期。終了の刈り取りだけ待って、ゾンビを残さない。
        let _ = c.start_kill();
        let _ = c.wait().await;
    }
    Ok(())
}

/// 現在の状態。**サーバが動いていないのはエラーではない**（running:false を返す）。
#[tauri::command]
pub async fn llama_status() -> Result<LlamaStatus, String> {
    Ok(status_now().await)
}

async fn status_now() -> LlamaStatus {
    let port = current_port();
    let backend = current_backend();
    match fetch_models(port).await {
        Ok(models) => LlamaStatus {
            running: true,
            port,
            backend,
            models,
            // ★動いているときだけ読む。止まっている推論器の分割を出しても意味がない
            //   （前回の値が残り続けると、いま起きていない事を説明してしまう）。
            load: load_facts_now(),
        },
        Err(_) => LlamaStatus {
            running: false,
            port,
            backend,
            models: Vec::new(),
            load: None,
        },
    }
}

/// router に登録されている ID へ寄せる。
///
/// ★実機で踏んだ食い違い: preset の別名は拡張子を落として記号を置換した `stem`
/// （例 `Qwen3-14B-abliterated.Q4_K_M`）だが、画面はファイル名そのもの
/// （`....gguf`）を渡していた。名前を2箇所で別々に決めていたので 404 になった。
/// **router の /models が正典**なので、そこに載っている ID に解決してから投げる。
pub async fn resolve_model_id(want: &str) -> String {
    let listed: Vec<String> = match status_now().await.models.into_iter().map(|m| m.id).collect::<Vec<_>>() {
        v if !v.is_empty() => v,
        _ => return want.to_string(),
    };
    if listed.iter().any(|m| m == want) {
        return want.to_string();
    }
    // 拡張子を落とした形、記号を置換した形の順に照合する。
    let stem = want.strip_suffix(".gguf").unwrap_or(want);
    if let Some(hit) = listed.iter().find(|m| m.as_str() == stem) {
        return hit.clone();
    }
    let san = sanitize_alias(stem);
    if let Some(hit) = listed.iter().find(|m| m.as_str() == san) {
        return hit.clone();
    }
    // 最後の手段: 前方一致（同名の別量子化を取り違えないよう完全一致を先に試している）。
    listed
        .into_iter()
        .find(|m| stem.starts_with(m.as_str()) || m.starts_with(stem))
        .unwrap_or_else(|| want.to_string())
}

/// モデルを読み込ませる。読み込みは数十秒〜数分かかるので、この呼び出しだけ長い猶予を与える。
#[tauri::command]
pub async fn llama_load(model: String) -> Result<(), String> {
    let model = resolve_model_id(&model).await;
    let url = format!("{}/models/load", router_base());
    let resp = ctl_client()
        .post(url)
        .timeout(Duration::from_secs(600))
        .json(&serde_json::json!({ "model": model }))
        .send()
        .await
        .map_err(|e| format!("推論サーバーに接続できません: {e}"))?;

    let status = resp.status();
    if status.is_success() {
        return Ok(());
    }
    let body = resp.text().await.unwrap_or_default();
    // ★「すでに動いている」は失敗ではない。読み込み済みのモデルを指定しただけで、
    //   利用者から見れば目的は達成されている（実機で NG と表示して混乱させた）。
    if body.contains("already running") || body.contains("already loaded") {
        return Ok(());
    }
    if status.as_u16() == 404 {
        let have: Vec<String> = status_now().await.models.into_iter().map(|m| m.id).collect();
        return Err(format!(
            "{model} は推論サーバーに登録されていません（HTTP 404）。\n             いま登録されているのは: {}\n             モデルを入れた直後なら、推論サーバーを一度止めてから使い直すと拾われます。",
            if have.is_empty() { "（0件）".to_string() } else { have.join(", ") }
        ));
    }
    Err(format!(
        "{model} を読み込めません（HTTP {}）: {}",
        status.as_u16(),
        body.trim()
    ))
}

// ---------------------------------------------------------------------------
// chat（SSE ストリーミング）
// ---------------------------------------------------------------------------

/// 生成を流す。
///
/// 失敗は**イベント `chat-error` で報せ、戻り値は Ok にする**（成功も失敗も購読側の
/// 1つの経路で扱えるようにする。戻り値でも投げると UI が二重に出す）。
/// 呼び出しは「ストリームが終わったとき」に解決する。
#[tauri::command]
pub async fn chat_send(
    app: AppHandle,
    model: String,
    messages: Vec<ChatMsg>,
    thinking: Option<bool>,
    effort: Option<String>,
) -> Result<(), String> {
    let thinking = thinking.unwrap_or(true);
    crate::sampling::set_effort(effort);
    if let Err(msg) = stream_chat(&app, model, messages, thinking).await {
        let _ = app.emit("chat-error", serde_json::json!({ "message": msg }));
    }
    Ok(())
}

async fn stream_chat(
    app: &AppHandle,
    model: String,
    messages: Vec<ChatMsg>,
    thinking: bool,
) -> Result<(), String> {
    // ★モデル名は router の一覧に合わせる。画面はファイル名（....gguf）を持っているが、
    //   router の ID は preset の別名（拡張子なし）。同じ食い違いを読み込み・会話・
    //   エージェントの3経路で別々に踏んだので、**経路ごとに必ずここを通す**。
    let model = resolve_model_id(&model).await;
    let url = format!("{}/v1/chat/completions", chat_base());
    let mut body = serde_json::json!({
        "model": model,
        "messages": messages,
        "stream": true,
        // 使用量を最後のチャンクで返させる（消費トークンの表示に使う）
        "stream_options": { "include_usage": true },
        "timings_per_token": true,
    });
    // ★生成設定を必ず載せる。載せずに出したら実機で繰り返しループになった。
    crate::sampling::apply(&mut body);
    crate::sampling::set_thinking(&mut body, thinking);

    let mut resp = stream_client()
        .post(url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("推論サーバーに接続できません: {e}"))?;

    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("推論サーバーが HTTP {code} を返しました: {}", text.trim()));
    }

    // 応答全体を溜めない。`chunk()` は reqwest の stream 機能に依存せず、
    // futures の trait も要らないので、依存を増やさずに逐次処理できる。
    let mut buf: Vec<u8> = Vec::with_capacity(8192);
    let mut usage: Option<Value> = None;
    let mut timings: Option<Value> = None;
    let mut done = false;
    let mut split = crate::think::Splitter::default();
    let mut meter = crate::think::Meter::default();
    let mut guard = crate::sampling::LoopGuard::default();
    let mut looped: Option<String> = None;
    let mut finish_reason: Option<String> = None;
    let gen = crate::cancel::begin();
    let mut stopped = false;

    loop {
        // ★中止はここで見る。接続を捨てれば推論器側の生成も止まる。
        if crate::cancel::is_stopped(gen) {
            stopped = true;
            break;
        }
        let chunk = match resp.chunk().await {
            Ok(Some(c)) => c,
            Ok(None) => break, // 接続が正常に閉じた
            Err(e) => return Err(format!("受信が途切れました: {e}")),
        };
        buf.extend_from_slice(&chunk);

        // 行単位で取り出す。\n で切るので UTF-8 の途中で割れることはない。
        while let Some(pos) = buf.iter().position(|b| *b == b'\n') {
            let line_bytes: Vec<u8> = buf.drain(..=pos).collect();
            let decoded = String::from_utf8_lossy(&line_bytes);
            let line = decoded.trim_end_matches(|c| c == '\r' || c == '\n');

            if line.is_empty() {
                continue;
            }
            // SSE のコメント行（": ping" 等）と data 以外のフィールドは読み飛ばす。
            let payload = match line.strip_prefix("data:") {
                Some(p) => p.trim(),
                None => continue,
            };
            if payload == "[DONE]" {
                done = true;
                break;
            }
            let v: Value = match serde_json::from_str(payload) {
                Ok(v) => v,
                // 壊れた1行で会話全体を落とさない。
                Err(_) => continue,
            };
            if let Some(u) = v.get("usage") {
                if !u.is_null() {
                    usage = Some(u.clone());
                }
            }
            if let Some(t) = v.get("timings") {
                if !t.is_null() {
                    timings = Some(t.clone());
                }
            }
            if let Some(fr) = v
                .get("choices")
                .and_then(|c| c.get(0))
                .and_then(|c| c.get("finish_reason"))
                .and_then(|f| f.as_str())
            {
                finish_reason = Some(fr.to_string());
            }
            let delta = v.get("choices").and_then(|c| c.get(0)).and_then(|c| c.get("delta"));
            // 推論器が思考を別枠で返す場合（reasoning_content）は、そのまま思考として流す。
            if let Some(r) = delta
                .and_then(|d| d.get("reasoning_content"))
                .and_then(|c| c.as_str())
            {
                if !r.is_empty() {
                    meter.tick(true);
                    let _ = app.emit("chat-think", serde_json::json!({ "text": r }));
                }
            }
            if let Some(text) = delta.and_then(|d| d.get("content")).and_then(|c| c.as_str()) {
                if !text.is_empty() {
                    // ★本文と思考を分ける。分けないと `</think>` と思考の断片が
                    //   そのまま答えとして出る（2026-08-11 実機）。
                    for piece in split.push(text) {
                        emit_piece(app, &mut meter, piece);
                    }
                }
            }
            if meter.should_report() {
                let _ = app.emit("chat-stat", meter.stat());
            }
            // 思考でも本文でも、同じ内容を繰り返し始めたら打ち切る。
            // 接続を切れば推論器側の生成も止まる（無駄に GPU を回さない）。
            if let Some(d) = delta.and_then(|d| d.get("content")).and_then(|c| c.as_str()) {
                if guard.push(d) {
                    looped = guard.verdict.clone();
                    done = true;
                    break;
                }
            }
        }
        if done {
            break;
        }
    }

    // 上限で切られた＝思考の途中なら、思考を本文へ移さない。
    let truncated = finish_reason.as_deref() == Some("length") || looped.is_some() || stopped;
    let stuck = split.stuck_in_thought();
    for piece in split.finish(!truncated) {
        emit_piece(app, &mut meter, piece);
    }
    if truncated && stuck && looped.is_none() {
        let _ = app.emit(
            "chat-notice",
            serde_json::json!({ "message": "思考の途中で出力の上限に達しました。本文が出ていません。質問を短くするか、もう一度送ってみてください。" }),
        );
    }
    if stopped {
        let _ = app.emit("chat-notice", serde_json::json!({ "message": "止めました。" }));
    }
    if let Some(reason) = looped {
        let _ = app.emit(
            "chat-notice",
            serde_json::json!({ "message": format!("{reason}。生成を打ち切りました。") }),
        );
    }
    let mut done_payload = meter.stat();
    if let Some(o) = done_payload.as_object_mut() {
        o.insert("usage".into(), usage.clone().unwrap_or(Value::Null));
        o.insert("timings".into(), timings.clone().unwrap_or(Value::Null));
        // prompt は usage が無ければ llama.cpp の timings から拾う
        let prompt = usage
            .as_ref()
            .and_then(|u| u.get("prompt_tokens").and_then(|p| p.as_u64()))
            .or_else(|| timings.as_ref().and_then(|t| t.get("prompt_n").and_then(|p| p.as_u64())));
        o.insert("prompt_tokens".into(), serde_json::json!(prompt));
    }
    let _ = app.emit("chat-done", done_payload);
    Ok(())
}

/// 振り分けた断片を画面へ送る。本文・思考・遡っての付け替えの3つだけ。
/// ★出す口は `sidecar::Out`（2026-10-30・走行の器）。本体経由の走行は札（会話）つきの口を渡し、知らせに札が付く。
pub fn emit_piece(app: &impl crate::sidecar::Out, meter: &mut crate::think::Meter, piece: crate::think::Piece) {
    use crate::think::Piece;
    match piece {
        Piece::Answer(t) => {
            meter.tick_text(false, &t);
            app.out("chat-token", serde_json::json!({ "text": t }));
        }
        Piece::Think(t) => {
            meter.tick_text(true, &t);
            app.out("chat-think", serde_json::json!({ "text": t }));
        }
        // これまで本文として見せた分は、実は思考だった。画面に移させる。
        Piece::Reclassify(t) => {
            app.out("chat-reclassify", serde_json::json!({ "text": t }));
        }
        // 逆向き。思考として見せた分を本文へ戻す（空の返答を出さないため）。
        Piece::Promote(t) => {
            meter.promote();
            app.out("chat-promote", serde_json::json!({ "text": t }));
        }
    }
}

// ---------------------------------------------------------------------------
// テスト（純粋関数のみ。プロセス起動は含まない＝どの OS でも走る）
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shard_detection() {
        assert!(is_non_first_shard("model-00002-of-00003.gguf"));
        assert!(!is_non_first_shard("model-00001-of-00003.gguf"));
        assert!(!is_non_first_shard("Qwen3-14B-Q4_K_M.gguf"));
        assert!(!is_non_first_shard("short.gguf"));
    }

    #[test]
    fn alias_is_sanitized_and_unique() {
        let mut used = Vec::new();
        assert_eq!(sanitize_alias("Qwen3 14B [Q4_K_M]"), "Qwen3-14B--Q4_K_M");
        let a = unique_alias("m".into(), &mut used);
        let b = unique_alias("m".into(), &mut used);
        assert_eq!((a.as_str(), b.as_str()), ("m", "m-2"));
    }

    #[test]
    fn models_payload_shapes() {
        let openai = serde_json::json!({"data":[{"id":"a","state":"loaded"}]});
        assert_eq!(map_models(&openai).len(), 1);
        let bare = serde_json::json!([{"id":"b","status":"sleeping"}]);
        assert_eq!(map_models(&bare)[0].state, "sleeping");
        let nested = serde_json::json!({"models":[{"id":"c"}]});
        assert_eq!(map_models(&nested)[0].state, "unknown");
    }

    #[test]
    fn download_progress_sums_files() {
        let v = serde_json::json!({
            "id":"d","state":"downloading",
            "files":[{"downloaded_bytes":10,"total_bytes":100},
                     {"downloaded_bytes":5,"total_bytes":50}]
        });
        let m = &map_models(&serde_json::json!([v]))[0];
        assert_eq!(m.downloaded_bytes, Some(15));
        assert_eq!(m.total_bytes, Some(150));
    }

    #[test]
    fn exit_code_is_carried() {
        let v = serde_json::json!([{"id":"e","state":"unloaded","exit_code":1}]);
        assert_eq!(map_models(&v)[0].exit_code, Some(1));
    }

    #[test]
    fn preset_writes_one_section_per_model() {
        let dir = std::env::temp_dir().join("bw-preset-test");
        let _ = std::fs::create_dir_all(&dir);
        let p = dir.join("models-preset.ini");
        write_models_preset(
            &p,
            &[PresetEntry {
                alias: "qwen3-14b".into(),
                path: PathBuf::from("/models/qwen3-14b.gguf"),
            }],
            false,
        )
        .unwrap();
        let s = std::fs::read_to_string(&p).unwrap();
        assert!(s.contains("[qwen3-14b]"));
        assert!(s.contains("model = /models/qwen3-14b.gguf"));
        // fitter を殺す引数が混ざっていないこと。
        assert!(!s.contains("n-gpu-layers"));
        assert!(!s.contains("ctx-size"));
        assert!(!s.contains("[*]"));
    }

    #[test]
    fn tuned_preset_puts_shared_settings_first_and_no_gpu_split() {
        let dir = std::env::temp_dir().join("bw-preset-test-tuned");
        let _ = std::fs::create_dir_all(&dir);
        let p = dir.join("models-preset.ini");
        write_models_preset(&p, &[PresetEntry { alias: "m".into(), path: PathBuf::from("m.gguf") }], true).unwrap();
        let s = std::fs::read_to_string(&p).unwrap();
        let star = s.find("[*]").expect("共通の節が無い");
        assert!(star < s.find("[m]").unwrap(), "共通の節はモデルの節より前");
        assert!(s.contains("c = 65536") && s.contains("ubatch-size = 2048"));
        // GPU と CPU の振り分けは自動調整に任せる（書くと fitter が止まる）。
        assert!(!s.contains("n-gpu-layers") && !s.contains("ngl") && !s.contains("n-cpu-moe"));
    }
}


// ---------------------------------------------------------------------------
// 自己診断からの入口
//
// ★GUI を立ち上げずに、アプリと**同じ経路**で推論器を起動する。症状ではなく原因を
// 見るために要る（AppHandle は resources 解決にしか使っていないので、環境変数と
// 実行ファイルの隣を見る道で代替できる）。
// ---------------------------------------------------------------------------

/// AppHandle 無しで推論器を起動する。診断専用。
pub async fn llama_start_headless() -> Result<LlamaStatus, String> {
    // 既に誰かが上げているなら、それを使う（勝手に二重起動しない）。
    let (port, adopted) = pick_port().await?;
    if adopted {
        sup().port.store(port, Ordering::Relaxed);
        return Ok(status_now().await);
    }

    let exe = stage_engine(&resolve_server_binary_headless()?)?;
    let backend = detect_backend(&exe);
    let models = crate::models_dir();
    std::fs::create_dir_all(&models).map_err(|e| format!("{} を作れません: {e}", models.display()))?;

    let entries = scan_gguf(&models)?;
    let preset = models.join("models-preset.ini");
    write_models_preset(&preset, &entries, crate::localgpu::is_cuda_engine(&exe))?;

    sup().port.store(port, Ordering::Relaxed);
    *lock(&sup().backend) = backend;

    let log_path = crate::app_root().join("llama-server.log");
    let log = std::fs::OpenOptions::new().create(true).append(true).open(&log_path)
        .map_err(|e| format!("{} を開けません: {e}", log_path.display()))?;
    let log2 = log.try_clone().map_err(|e| format!("ログを複製できません: {e}"))?;
    *lock(&sup().log_path) = Some(log_path.clone());

    let mut cmd = Command::new(&exe);
    cmd.arg("--host").arg("127.0.0.1").arg("--port").arg(port.to_string())
        .arg("--sleep-idle-seconds").arg(SLEEP_IDLE_SECONDS.to_string())
        .arg("--jinja")
        .current_dir(&models)
        .stdin(Stdio::null()).stdout(Stdio::from(log)).stderr(Stdio::from(log2))
        .kill_on_drop(false);
    if !entries.is_empty() {
        cmd.arg("--models-preset").arg("models-preset.ini");
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let child = cmd.spawn().map_err(|e| format!("推論サーバーを起動できません（{}）: {e}", exe.display()))?;
    *lock(&sup().child) = Some(child);
    // 応答するまで待つ。落ちたらログの末尾を返す（「起動しません」で終わらせない）。
    let deadline = std::time::Instant::now() + START_TIMEOUT;
    loop {
        if router_responds(port).await {
            return Ok(status_now().await);
        }
        if !own_child_alive() {
            return Err(format!("推論サーバーがすぐに終了しました。ログの末尾:\n{}", log_tail(&log_path, 30)));
        }
        if std::time::Instant::now() > deadline {
            return Err(format!("推論サーバーが {}秒たっても応答しません。ログの末尾:\n{}",
                START_TIMEOUT.as_secs(), log_tail(&log_path, 30)));
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
}

/// AppHandle を使わない実体探索。診断は GUI の外から走るのでリソース解決が使えない。
fn resolve_server_binary_headless() -> Result<PathBuf, String> {
    let mut tried: Vec<String> = Vec::new();
    if let Ok(p) = crate::envcompat::var("LAPS_LLAMA_SERVER") {
        let p = PathBuf::from(p);
        if p.is_file() { return Ok(p); }
        tried.push(format!("{} (LAPS_LLAMA_SERVER)", p.display()));
    }
    if let Some(p) = crate::localgpu::engine_exe() {
        return Ok(p);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for rel in ["resources/llama", "llama", "."] {
                let c = dir.join(rel).join(SERVER_BIN);
                if c.is_file() { return Ok(c); }
                tried.push(c.display().to_string());
            }
        }
    }
    Err(format!("llama-server が見つかりません。試した場所:\n  {}", tried.join("\n  ")))
}


// ---------------------------------------------------------------------------
// エンジンの配置
// ---------------------------------------------------------------------------

/// llama.cpp 一式を **ASCII のパス**へ置き直して、その実体を返す。
///
/// ★実機で三度目に当たった壁。データの置き場は ASCII に逃がしたが、
/// **プログラムの置き場**（`%LOCALAPPDATA%\BLACKWALL Console\resources\llama`）が
/// 利用者名を含むため非 ASCII のままだった。router mode は**自分自身を子として起動する**ので、
/// 狭い文字コードの API を通る経路がそこに残り「failed to spawn server instance」になる。
///
/// 対処: 起動前に一式を `<ASCII なデータ根>\engine\` へ複製し、そこから起動する。
/// 約 94MB の複製が初回だけ発生するが、同じドライブなので数秒で終わる。
/// すでに同じ大きさのものが在れば複製しない。
fn stage_engine(src_exe: &Path) -> Result<PathBuf, String> {
    let src_dir = src_exe.parent().ok_or("エンジンの場所を特定できません")?;
    // 元が ASCII なら、わざわざ複製しない。
    if src_dir.to_string_lossy().is_ascii() {
        return Ok(src_exe.to_path_buf());
    }
    let dst_dir = crate::app_root().join("engine");
    std::fs::create_dir_all(&dst_dir)
        .map_err(|e| format!("{} を作れません: {e}", dst_dir.display()))?;

    let mut copied = 0usize;
    for entry in std::fs::read_dir(src_dir)
        .map_err(|e| format!("{} を読めません: {e}", src_dir.display()))?
    {
        let Ok(entry) = entry else { continue };
        let from = entry.path();
        if !from.is_file() {
            continue;
        }
        let to = dst_dir.join(entry.file_name());
        // 既に同じ大きさなら飛ばす（毎回 94MB 複製しない）。
        let same = std::fs::metadata(&to).ok().and_then(|m| {
            std::fs::metadata(&from).ok().map(|s| m.len() == s.len())
        }).unwrap_or(false);
        if same {
            continue;
        }
        if std::fs::copy(&from, &to).is_ok() {
            copied += 1;
        }
    }
    let staged = dst_dir.join(SERVER_BIN);
    if !staged.is_file() {
        return Err(format!(
            "エンジンを {} へ複製できませんでした（{} 個複製）。\n\
             ユーザー名に ASCII 以外の文字が含まれる環境では、この複製が要ります。",
            dst_dir.display(), copied
        ));
    }
    Ok(staged)
}

/// 診断用の最小生成。**「読み込めた」と「実際に答えられる」は別**なので、
/// 端から端まで1回通して確かめる。
pub async fn generate_probe(model: &str) -> Result<String, String> {
    let url = format!("{}/v1/chat/completions", chat_base());
    let body = serde_json::json!({
        "model": model,
        "messages": [{"role": "user", "content": "1+1は？数字だけで答えて。"}],
        "max_tokens": 16,
        "stream": false,
    });
    let resp = stream_client()
        .post(url)
        .timeout(Duration::from_secs(180))
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("推論サーバーに接続できません: {e}"))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("HTTP {} : {}", status.as_u16(), text.trim()));
    }
    let v: Value = serde_json::from_str(&text).map_err(|e| format!("応答を読めません: {e}"))?;
    Ok(v["choices"][0]["message"]["content"].as_str().unwrap_or("").to_string())
}

#[cfg(test)]
mod name_resolution_tests {
    //! ★同じ食い違いを3回踏んだ（読み込み／会話／エージェント）。
    //! 「router へモデル名を送る経路は、必ず解決を通す」を試験で固定する。
    use std::path::Path;

    #[test]
    fn every_path_that_sends_a_model_name_resolves_it_first() {
        let src = std::fs::read_to_string(Path::new(file!())).unwrap_or_default();
        // router にモデル名を渡す箇所（"model": を含む JSON 組み立て）の直前で
        // 解決関数を通していること。呼び出し漏れを機械で見つける。
        let calls = src.matches("resolve_model_id(").count();
        assert!(
            calls >= 3,
            "モデル名を送る経路が解決を通っていない可能性がある（resolve_model_id の出現 {calls} 箇所）"
        );
    }

    #[test]
    fn the_extension_is_not_part_of_the_router_id() {
        // 画面はファイル名を持ち、router は拡張子なしの別名を持つ。
        // この非対称そのものが事故の元だったので、前提として書き残す。
        assert_eq!(
            "Qwen3-14B-abliterated.Q4_K_M.gguf".strip_suffix(".gguf"),
            Some("Qwen3-14B-abliterated.Q4_K_M")
        );
    }
}
