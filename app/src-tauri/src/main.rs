// リリースビルドでコンソール窓を出さない。デバッグ時は残す（stderr を読みたいため）。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! L.A.P.S Console — エントリポイント。
//!
//! 設計上の位置づけ（DESIGN §0）: **このアプリはクライアントに徹する。**
//! 推論もエージェントも内側に抱えない。`llama-server` は子プロセスとして起動するだけ、
//! 推論サーバーは HTTP で叩くだけ。ネットワーク I/O はすべて Rust 側が持ち、WebView は
//! `invoke` とイベントしか使わない（CORS が構造的に発生せず、失敗の診断が1箇所に集まる）。
//!
//! モジュール構成:
//!   gpu.rs   … DXGI / NVML で GPU と空き VRAM を見る
//!   fit.rs   … 落とす前に「載るか」を見積もる（純粋計算・OS 非依存）
//!   llama.rs … 同梱 llama-server を router mode で起動・監視し、チャットを中継する
//!   hf.rs    … Hugging Face の検索・ファイル一覧・再開可能ダウンロード

mod fit;
mod gguf;
mod catalog;
mod gpu;
mod hf;
mod localgpu;
mod llama;
mod selftest;
mod integrity;
mod apikeys;
mod mcp;
mod cancel;
mod convos;
mod chromebridge;
mod metrics;
mod sampling;
mod scm;
mod snapshot;
mod sidecar;
mod attach;
mod filepreview;
mod browser;
mod voiceinput;
mod calltoast;
/// 会話1往復ぶんの記録を手元に書き、背景でサーバへ送る（オーナー要求 2026-08-11）。
mod journal;

/// 画面に出す版。★どのビルドで起きた不具合かが分からず、直っているのに
/// 直っていないと読む往復が発生したので出す。
/// いま実際に渡している道具の名前。★画面はこれを一覧に出す。
/// 画面が持つ固定の一覧を出すと、実装とずれたときに**嘘の一覧**になる。
/// 生成を止める。★入口だけをここに置き、判断は cancel.rs に置く
/// （tauri::command が付いていると、Tauri 抜きの試験殻でコンパイルできない）。
#[tauri::command]
fn chat_stop(lane: Option<String>) { cancel::stop_lane(lane.as_deref().unwrap_or("").trim()); }

/// 利用者の返事を本体へ返す（AskUserQuestion の受け口）。
/// 走行中に一言を差し込む。★中断ではない（作業を捨てない）。
#[tauri::command]
async fn core_inject(text: String, force: Option<bool>, lane: Option<String>) -> Result<(), String> {
    // force: 区切りを待たず、いまの生成と Bash を止めて渡す（強制割り込み・2026-10-09）。古い画面は送らない＝false。
    // lane: どの会話の走行へ（2026-10-30・走行の器）。無ければ器 0（今までどおり）。
    sidecar::inject(text, force.unwrap_or(false), lane).await
}

#[tauri::command]
async fn core_answer(ask_id: String, text: String, lane: Option<String>) -> Result<(), String> {
    sidecar::answer(ask_id, text, lane).await
}

/// 裏の会話が終わった・聞いてきた（2026-10-30・走行の器）。Windows の通知を出し、押されたら画面へ peer-open（key=local:<会話>）。
#[tauri::command]
fn conv_toast(app: tauri::AppHandle, title: String, body: String, key: String) {
    calltoast::note(&app, title, body, key);
}

/// 利用者が「覚えて」と頼んだ事柄（2026-10-30 オーナー「メモリ機能の強化」・本体の oc/remember）。Memory 画面と入力欄の
/// `/remember` が呼ぶ。★中身の処理（置き場・区画・資格情報の断り）は Python の 1 か所だけに置き、ここは呼んで JSON を返すだけ。
#[tauri::command]
async fn remember(op: String, text: Option<String>, n: Option<u32>) -> Result<serde_json::Value, String> {
    let args: Vec<String> = match op.as_str() {
        "list" => vec!["list".into()],
        "add" => vec!["add".into(), text.unwrap_or_default()],
        "remove" => vec!["remove".into(), n.map(|x| x.to_string()).unwrap_or_default()],
        _ => return Err("op は list / add / remove のどれか".into()),
    };
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    let (_code, out, err) = sidecar::run_python_module("laps.oc.remember", &refs, &[], 30).await?;
    let line = out
        .lines()
        .rev()
        .find(|l| l.trim_start().starts_with('{'))
        .ok_or_else(|| format!("本体が答えませんでした: {}", err.trim().chars().take(300).collect::<String>()))?;
    serde_json::from_str(line).map_err(|e| format!("答えを読めません: {e}"))
}

#[tauri::command]
async fn agent_tools() -> Vec<String> {
    // ★本体（Python）が使えるなら、そちらの道具一覧が本当の答え。
    //   アプリ内蔵の5つを返すと、22個使える状態でも5個と表示してしまう。
    if sidecar::core_available().await {
        if let Some(list) = sidecar::core_tools().await {
            return list;
        }
    }
    agent::tool_names()
}

/// いまどちらの中身で動いているか。画面に出す（利用者が能力を誤解しないため）。
#[tauri::command]
async fn engine_status() -> serde_json::Value {
    let ok = sidecar::core_available().await;
    serde_json::json!({
        "core": ok,
        "reason": if ok { None } else { sidecar::last_unavailable_reason() },
        "fallback_tools": agent::tool_names(),
    })
}

#[tauri::command]
fn app_version() -> String { env!("CARGO_PKG_VERSION").to_string() }

/// アプリを開き直す（2026-10-30・差分の更新の後）。★画面の `window.__TAURI__.process.relaunch()` は process プラグインを
/// 入れていないので呼べず、その例外が「差分では入れられない」の側に落ちて、差分で入れ終えた直後に 79MB の全体の更新を
/// 走らせていた（配信の記録: 0.1.609→0.1.610 は差分 9 ファイル・2.3 秒で ok の 1 秒後に setup.exe を丸ごと取った）。
#[tauri::command]
fn app_restart(app: tauri::AppHandle) {
    crate::sidecar::stop_bundled_python();
    app.restart();
}
mod think;
mod workspace;
mod remotectl;
// ★ほかの端末の会話（全端末で 1 つの一覧・閉じていても届く通知・2026-10-30）。
mod auth;
mod peers;
mod localshell;
mod agent;
mod edit_guard;
/// 環境変数の名前の読み替え（`LAPS_X` を先に、無ければ `BLACKWALL_X`・2026-11-14）。
mod envcompat;
/// 置き場の名前（`laps`・旧い `blackwall` が在ればそのまま使う・2026-11-14）。Python の laps/paths.py と同じ規則。
mod paths;

// ★推論の宛先を決める純粋な論理（2026-09-06）。実体は blackwall-core 側にあり、
//   ここは `#[path]` で参照するだけ（ファイルは1本＝二重管理は起きない）。
//   あちらに置くのは、本体 `blackwall` が Tauri のクレートでホストの
//   `cargo test` がリンクできないため——ここに書いた試験は**一度も走らない**。
#[path = "../blackwall-core/src/backend_choice.rs"]
mod backend_choice;

use std::path::PathBuf;

use tauri::Manager;

// ---------------------------------------------------------------------------
// 置き場所
//
// %LOCALAPPDATA%\Blackwall に モデル・設定・ログをまとめる（DESIGN §6「置き場所」）。
// llama.rs は AppHandle 経由（`local_data_dir()/Blackwall`）、hf.rs は環境変数経由で
// 同じ場所を出す。ここは AppHandle を持たない config_value のために環境変数から解決する。
// Windows では3者とも同じパスに落ちる。
// ---------------------------------------------------------------------------

/// ★データ根は「非 ASCII を含まない場所」でなければならない。
///
/// 実機で二度失敗して分かったこと（推測ではなく、同梱バイナリの import を見て確定）:
/// **llama.cpp は `CreateFileA` と `fopen`＝狭い文字コードのファイル API を使う。**
/// 利用者名が `C:\Users\田中　優輝\...` のとき、英語ロケールの Windows では
/// この変換ができず「ファイルが存在しない」になる。ログには正しく出るのに開けない、
/// という紛らわしい壊れ方をする（実際にそう見えた）。
///
/// 8.3 の短縮名で渡す手も試したが、**8.3 名の生成が無効な環境では効かない**（実機がそれ）。
/// したがって場所そのものを ASCII に寄せる。`%PUBLIC%`（既定 `C:\Users\Public`）は
/// どの Windows でも ASCII で、利用者が書ける。
/// ★2026-11-14 置き場の名前: 新しく作るのは `LAPS`。旧い `Blackwall` が在ればそのまま使う（`paths::pick_in`）。
///   親（%LOCALAPPDATA% か %PUBLIC%）の決め方は変えていない。
#[cfg(windows)]
fn platform_root() -> Option<PathBuf> {
    if let Some(base) = std::env::var_os("LOCALAPPDATA").map(PathBuf::from) {
        if base.to_string_lossy().is_ascii() {
            return Some(paths::pick_in(&base, ROOT_NEW, ROOT_OLD));
        }
    }
    // 非 ASCII を含むなら公開フォルダへ逃がす。同じ C: 上なので移動も一瞬で済む。
    let public = std::env::var_os("PUBLIC")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("C:\\Users\\Public"));
    Some(paths::pick_in(&public, ROOT_NEW, ROOT_OLD))
}

/// アプリのデータ根の名前（新しい名前・旧い名前）。
#[cfg(windows)]
const ROOT_NEW: &str = "LAPS";
#[cfg(windows)]
const ROOT_OLD: &str = "Blackwall";
#[cfg(not(windows))]
const ROOT_NEW: &str = "laps";
#[cfg(not(windows))]
const ROOT_OLD: &str = "blackwall";

/// 旧い場所（利用者プロファイル配下）。移し替えの元として使う。
#[cfg(windows)]
pub fn legacy_root() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA").map(|p| PathBuf::from(p).join("Blackwall"))
}
#[cfg(not(windows))]
pub fn legacy_root() -> Option<PathBuf> {
    None
}

/// 非 Windows は Linux 上で型検査と単体試験を回すための代替路。配布対象ではない。
#[cfg(not(windows))]
fn platform_root() -> Option<PathBuf> {
    if let Some(x) = std::env::var_os("XDG_DATA_HOME") {
        return Some(paths::pick_in(&PathBuf::from(x), ROOT_NEW, ROOT_OLD));
    }
    std::env::var_os("HOME").map(|p| paths::pick_in(&PathBuf::from(p).join(".local").join("share"), ROOT_NEW, ROOT_OLD))
}

/// アプリのデータ根。取れなかった場合でも panic させず、一時フォルダへ退避する。
pub fn app_root() -> PathBuf {
    platform_root().unwrap_or_else(|| std::env::temp_dir().join("blackwall"))
}

/// モデル置き場。**すべてのモジュールがこれを使う**（以前は3箇所で別々に計算していた）。
pub fn models_dir() -> PathBuf {
    app_root().join("models")
}

/// 旧い場所に残っているモデルを新しい場所へ移す。
///
/// 同じドライブなら rename で一瞬。失敗しても起動は続ける（移せなくても、
/// 利用者が置き場を開いて自分で移せる。起動しないほうが困る）。
pub fn migrate_from_legacy() -> Option<String> {
    let new = models_dir();
    let old = legacy_root()?.join("models");
    if !old.is_dir() || old == new {
        return None;
    }
    let _ = std::fs::create_dir_all(&new);
    let mut moved = 0usize;
    for e in std::fs::read_dir(&old).ok()? {
        let Ok(e) = e else { continue };
        let p = e.path();
        if p.extension().and_then(|x| x.to_str()) != Some("gguf") {
            continue;
        }
        let dst = new.join(e.file_name());
        if dst.exists() {
            continue;
        }
        if std::fs::rename(&p, &dst).is_ok() {
            moved += 1;
        } else if std::fs::copy(&p, &dst).is_ok() {
            let _ = std::fs::remove_file(&p);
            moved += 1;
        }
    }
    (moved > 0).then(|| format!("{} 個のモデルを {} へ移しました", moved, new.display()))
}

pub fn config_path() -> PathBuf {
    app_root().join("config.toml")
}

/// 実行の権限モード。`config.toml` の `policy` を読む。
///
/// 公開版の既定は `workspace`（作業フォルダの中だけ書ける）。`config.toml` の `policy = "trusted"` で広げられる。
///
/// ★正直に書いておく: trusted は境界を外す。モデルが誤ればこの機械が壊れる。
///   止めない代わりに**すべての呼び出しが audit に残る**。戻したいときは
///   `config.toml` の `policy` を `workspace`（作業フォルダ内だけ）や
///   `readonly`（読むだけ）にすればよい——**書き換えは1行**。
pub fn policy_mode() -> String {
    let body = match std::fs::read_to_string(config_path()) {
        Ok(b) => b,
        Err(_) => return "workspace".into(),
    };
    for line in body.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        if let Some((k, v)) = line.split_once('=') {
            if k.trim() == "policy" {
                let v = v.trim().trim_matches('"').trim();
                if matches!(v, "readonly" | "workspace" | "off" | "trusted") {
                    return v.to_string();
                }
            }
        }
    }
    "workspace".into()
}

/// 作業の予算（`config.toml`）。**既定はどれも 0 ＝ 無制限**。
///
/// ★2026-08-22 オーナー決定「上限をなしにします」。
///   それまで反復 40 で切られ、「ClaudeCode のような…を作って」規模の依頼が
///   **実際に手を動かしていたのに全滅に見える**形で終わっていた。
///
/// ⚠ 反復だけ外しても意味が薄い。時間と道具呼び出しの予算が次の天井になるので、
///   3つとも同じ扱いにする。止めるものが消えるわけではない——
///   ループ検知・文脈切れ・利用者の「止める」は生きている。
pub fn budget_value(key: &str) -> i64 {
    let body = match std::fs::read_to_string(config_path()) {
        Ok(b) => b,
        Err(_) => return 0,
    };
    for line in body.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        if let Some((k, v)) = line.split_once('=') {
            if k.trim() == key {
                if let Ok(n) = v.trim().trim_matches('"').trim().parse::<i64>() {
                    return n.max(0);
                }
            }
        }
    }
    0
}

/// 倫理・慎重さの刻み。`config.toml` の `ethics` を読む。
///
/// ★**実行できることは変わらない。** 何を実行してよいかは `policy` が決める。
///   ここが変えるのは「モデルがどれだけ慎重に振る舞うか」——確認を待つか、
///   スコープを問い直すか、エスカレーションするか——だけ。
///   研究側の一次資料が「system prompt に書いた安全ルールは境界にならない」と
///   している以上、境界を prompt に持たせるのは設計として誤り。境界は policy。
///
/// ★正直さの規律（捏造しない・未検証を完了と言わない）は**刻みから外してある**。
///   あれは倫理検閲ではなく測定の前提で、外すと「速く完了したように見えて
///   何も出来ていない」走行が高得点になり、刻みの比較そのものが壊れる。
pub fn ethics_level() -> String {
    // ★2026-08-30 訂正: オーナー指示「倫理的制限もすべて排除」で ethics.py の DEFAULT は
    //   "raw" に変わっている（term/config.py の DEFAULTS も "raw"）。ここだけ "standard" の
    //   ままだと、設定ファイルが無い/壊れている/`ethics` 行が無い場合に GUI だけ
    //   慎重さの塊（guardrails.md のスコープ確認等）を積んだまま走る——policy と違い
    //   実行できることは変わらないが、既定を全部揃えるという指示の対象からここだけ漏れていた。
    let body = match std::fs::read_to_string(config_path()) {
        Ok(b) => b,
        Err(_) => return "raw".into(),
    };
    for line in body.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        if let Some((k, v)) = line.split_once('=') {
            if k.trim() == "ethics" {
                let v = v.trim().trim_matches('"').trim();
                if matches!(v, "strict" | "standard" | "direct" | "raw") {
                    return v.to_string();
                }
            }
        }
    }
    "raw".into()
}

/// 設定ファイルから、宛先に関わる値だけを読む。
pub fn lan_config() -> backend_choice::LanConfig {
    backend_choice::parse_config(&std::fs::read_to_string(config_path()).unwrap_or_default())
}

/// この機械の推論機が**実際に答えられる見込みがあるか**。
///
/// 判定は**模型（`.gguf`）が1つでもあるか**の1点だけ。
///
/// ★「port が開いているか」を根拠にしない。`llama-server` は模型が1つも無くても
///   起動して 200 を返すので、それを根拠にすると**空の推論機に向かって45秒待つ**
///   （2026-09-06 の実機障害がこの形だった）。
///
/// ★同梱バイナリの有無は**あえて見ない**。見ようとすると導入先の階層を推測する
///   ことになり、推測が外れた機械は「手元が使えない」と判定されて
///   **黙って別のマシンへ話しに行く**——この木の中で何度も名指しで警告されている、
///   いちばん避けたい壊れ方。判定を誤るなら**手元側へ倒す**：
///   その場合でも `sidecar::local_health_block()` が3秒で「応答しません」と出すので、
///   利用者は見える形で次の手（LAN に切り替える）へ進める。
///
/// ★非同期にしない。この判定は送信経路3本（sidecar / agent / llama::chat_base）
///   すべてから呼ばれ、うち2本は同期の文脈にある。HTTP を1本でも挟むと、
///   「宛先を決めるためにネットワークを待つ」という筋の悪い依存が生まれる。
///
/// ⚠ 限界（正直に）: GPU の有無は見ていない。`probe_gpu()` は非同期かつ
///   `nvidia-smi` を起こすことがあり、依頼のたびに子プロセスを生やす価値は無い。
///   CPU だけでも小さい模型なら実際に動くので、「GPU 無し＝使えない」も嘘になる。
pub fn local_engine_usable() -> bool {
    use std::sync::{Mutex, OnceLock};
    use std::time::{Duration, Instant};
    static CACHE: OnceLock<Mutex<Option<(Instant, bool)>>> = OnceLock::new();
    let cell = CACHE.get_or_init(|| Mutex::new(None));
    let mut g = cell.lock().unwrap_or_else(|e| e.into_inner());
    if let Some((at, v)) = *g {
        if at.elapsed() < Duration::from_secs(5) {
            return v;
        }
    }
    let v = has_local_gguf();
    *g = Some((Instant::now(), v));
    v
}

/// 模型置き場に `.gguf` が1つでもあるか。
fn has_local_gguf() -> bool {
    let Ok(rd) = std::fs::read_dir(models_dir()) else { return false };
    rd.flatten().any(|e| {
        e.path()
            .extension()
            .and_then(|x| x.to_str())
            .map(|x| x.eq_ignore_ascii_case("gguf"))
            .unwrap_or(false)
    })
}

/// **推論の宛先を決める唯一の場所。**
///
/// ★2026-08-16 の敵対レビューで、送信経路が3本あり LAN 対応が1本だけだったことが
///   分かった——sidecar::core_send（道具あり）／agent::run_agent（道具なし）／
///   llama::chat_send（素のチャット）。根の解決を散らすと、この食い違いは必ずまた生える。
///   3本とも `inference_root()` を通っているので、決め方はここだけを直せばよい。
///
/// ★2026-09-06 追加: 判定が 2 値（true/false）だったため、設定が既定へ戻った実機が
///   **黙って `127.0.0.1:8760` へ落ちて 45 秒待ってから失敗**した。3 値にして、
///   「鍵が無い／auto」のときは**この機械で本当に推論できるときだけ**ローカルにする。
pub fn backend_decision() -> backend_choice::Decision {
    backend_choice::decide(&lan_config(), local_engine_usable())
}

/// 推論の宛先の根（`/v1` は付けない）。ローカルへ行くなら `None`。
pub fn inference_root() -> Option<String> {
    let d = backend_decision();
    d.remote.then(|| {
        let r = d.root.trim_end_matches('/');
        r.strip_suffix("/v1").unwrap_or(r).to_string()
    })
}

/// LAN の推論機を使うときの `(URL, モデル名)`。ローカルへ行くなら `None`。
///
/// ★「値があれば使う」にはしない。`lan_backend_url` は既定の設定ファイルに
///   最初から書いてあるので、それだけで有効化すると**全ての既存インストールが
///   黙って別のマシンへ話しに行く**。有効になるのは
///   `use_lan_backend = true`（明示）か、鍵が無い／auto（2026-09-06 から既定は常に別の推論サーバー）。
pub fn lan_backend() -> Option<(String, String)> {
    let root = inference_root()?;
    Some((root, lan_config().model))
}

/// 起動時に解決した実パス。画面から場所を聞かれたときのために状態として持つ。
#[derive(Debug, Clone)]
pub struct AppPaths {
    pub root: PathBuf,
    pub models: PathBuf,
    pub config: PathBuf,
}

impl AppPaths {
    fn resolve() -> Self {
        Self {
            root: app_root(),
            models: models_dir(),
            config: config_path(),
        }
    }

    fn ensure(&self) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.root)?;
        std::fs::create_dir_all(&self.models)?;
        Ok(())
    }
}

/// config.toml から 1 個のキーを読む素朴な行パーサ。
///
/// toml クレートを足していないのは依存を増やさないため（設定は人が読める TOML＝アプリが無くても中身が分かる）。
/// 対応するのは `key = "value"` / `key = value` の平坦な行と `#` コメントだけで、テーブルや配列は見ない。
/// 読めない行は黙って飛ばす。値が空の行は「書かれていない」と同じ（None）。
pub fn config_value(key: &str) -> Option<String> {
    let text = std::fs::read_to_string(config_path()).ok()?;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with('[') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        if k.trim() != key {
            continue;
        }
        // 行末コメントは扱わない（値に # を含む URL があり得るため、切ると壊す方が痛い）
        let v = v.trim().trim_matches('"').trim_matches('\'').trim();
        if v.is_empty() {
            return None;
        }
        return Some(v.to_string());
    }
    None
}

/// 初回起動時だけ既定の設定を書く。**既存ファイルは絶対に上書きしない**
/// （利用者が書き換えた値を黙って消すのは、この種のアプリで最も嫌われる壊し方）。
/// 既にある設定ファイルに、**足りない鍵だけ**を追記する。
///
/// ★なぜ要るか（2026-08-18 に自分で作った穴）:
/// 既定の設定は「**既存ファイルを絶対に上書きしない**」作りで、それは正しい——
/// 利用者が書き換えた値を黙って消すのは、この種のアプリで最も嫌われる壊し方だから。
/// だが新しい鍵（`policy`）を足したとき、**既に使っている人の設定には一生現れない**。
/// 実機がまさにそれで、「設定ファイルを見ても policy が無い」状態になった
/// （既定値で動くので害は無いが、**変えたい人が変えられない**）。
///
/// 上書きはしない。**無い鍵を末尾に足すだけ**。値が既にあれば何もしない。
/// 設定ファイルに無ければ足す鍵と、その説明の塊（起動時の ensure_config_keys と、整合性の確認が同じ一覧を使う）。
pub(crate) fn config_wanted() -> [(&'static str, &'static str); 7] {
    [
            ("max_iters", "\n# 作業の上限（2026-08-22 追加）。**0 は「上限なし」**。\n\
                           #   大きな依頼は 40往復では終わりません。実際に手を動かしていても\n\
                           #   途中で切られ、全部失敗したように見えていました。\n\
                           #   0 にしても、ループ検知・文脈切れ・「止める」は効きます。\n\
                           max_iters = 0\n"),
            ("max_seconds", "\n# 何秒まで走るか。0 は上限なし。\nmax_seconds = 0\n"),
            ("max_tool_calls", "\n# 道具を何回まで呼ぶか。0 は上限なし。\nmax_tool_calls = 0\n"),
            ("ethics", "\n# 倫理・慎重さの刻み（2026-08-19 追加、2026-08-30 既定を raw に変更）。\n\
                        #   strict   … 確認義務を増やす。書き換え前に必ず読む・解釈が割れたら聞く。\n\
                        #   standard … 旧既定。\n\
                        #   direct   … 迷いを生む部分を外す。破壊的操作は先に述べるが確認は待たない。\n\
                        #   raw      … 慎重さの塊を丸ごと無し（オーナー指示 2026-08-29「倫理的制限もすべて排除」）。\n\
                        # ★これは**熟慮の量**を変えるだけで、実行できることは変わらない。\n\
                        #   出来ることを決めるのは上の policy。raw にしても policy は緩まない。\n\
                        # ★「捏造しない／未検証を完了と言わない」はどの刻みでも残る（測定の前提のため）。\n\
                        ethics = \"raw\"\n"),
            ("policy", "\n# 実行の権限（2026-08-18 追加）。既定は workspace（作業フォルダの中だけ書ける）。\n\
                        #   trusted   … Windows/Linux のコマンドを制限なく実行し、どこへでも書ける。\n\
                        #               すべての呼び出しは audit に残る。\n\
                        #   workspace … **直接の**書き込みは作業フォルダの中だけ。\n\
                        #               ⚠ ただし作業フォルダにスクリプトを書いて実行できるので、\n\
                        #                 外部到達や昇格は**防げない**（`python3 ./x.py` は通る）。\n\
                        #                 「境界」ではなく「うっかりを減らす」ものと考えること。\n\
                        #   readonly  … 読むだけ（実測: 12 通りの迂回はすべて拒否された。保証ではない）。\n\
                        policy = \"workspace\"\n"),
            ("use_lan_backend", "\n# LAN の推論機を使うか（2026-08-18 追加、2026-09-06 に 3 値化）。\n\
                                 #   \"auto\" … 既定。この機械で本当に推論できるときだけ手元を使う。\n\
                                 #   true    … いつも LAN（手元の GPU を起こさない）。\n\
                                 #   false   … いつも手元。\n\
                                 # ★画面の「推論の場所」で切り替えられる（サーバー＝true／この PC の GPU＝false）。\n\
                                 use_lan_backend = true\n"),
            ("remote_control", "\n# スマホ（Android 版）から、この機械のセッションを見る・続ける・止める（2026-10-08 追加）。\n\
                                 #   true  … 既定。起動すると中継（remote_relay）へ名乗る。この機械の受け口は開けない。\n\
                                 #   false … 名乗らない（スマホの一覧に出ない）。\n\
                                 # ⚠ スマホからこの機械で道具（コマンド）を走らせられます。自前の中継を立てた人だけ使います。\n\
                                 remote_control = true\n\
                                 # 中継の場所（例 ws://relay.example:18400）。書くまでは何も名乗りません。\n\
                                 # remote_relay = \"ws://relay.example:18400\"\n"),
    ]
}

/// 設定ファイルに無い鍵（整合性の確認から・2026-10-08）。ファイルが読めなければ空。
pub(crate) fn missing_config_keys(path: &PathBuf) -> Vec<&'static str> {
    let Ok(body) = std::fs::read_to_string(path) else { return Vec::new() };
    config_wanted().iter().map(|(k, _)| *k).filter(|key| !body.lines().any(|l| {
        let l = l.split('#').next().unwrap_or("").trim();
        l.split_once('=').map(|(k, _)| k.trim() == *key).unwrap_or(false)
    })).collect()
}

pub(crate) fn ensure_config_keys(path: &PathBuf) -> std::io::Result<()> {
    if !path.exists() {
        return Ok(());
    }
    let body = std::fs::read_to_string(path)?;
    //: (鍵, 追記する塊)。**説明ごと**足す——鍵だけ現れても意味が分からない（中身は config_wanted）。
    let wanted = config_wanted();
    // ★同じ形の直しをもう1つ（2026-09-06）。**アプリが既定として書いた**
    //   `use_lan_backend = false` だけを `"auto"` へ移す。
    //   利用者が画面から保存した値には `lan_choice_pinned = true` が付くので触らない。
    //
    //   なぜ要るか: 0.1.194 以前の既定はこの1行を**利用者に聞かずに**書いていた。
    //   設定が既定へ戻った機械はそのせいで永久に手元固定になり、
    //   模型も載っていない推論機に 45 秒待ってから失敗し続けた。
    //   `auto` は手元が使えるなら手元を選ぶので、この移行で動きが変わるのは
    //   **手元では推論できない機械だけ**。
    let body = match backend_choice::migrate_machine_default_false(&body) {
        Some(fixed) => {
            let _ = std::fs::write(path, &fixed);
            eprintln!("[blackwall] use_lan_backend: 既定の false を \"auto\" へ移行しました");
            fixed
        }
        None => body,
    };

    let mut add = String::new();
    for (key, block) in wanted {
        let present = body.lines().any(|l| {
            let l = l.split('#').next().unwrap_or("").trim();
            l.split_once('=').map(|(k, _)| k.trim() == key).unwrap_or(false)
        });
        if !present {
            add.push_str(block);
        }
    }
    if add.is_empty() {
        return Ok(());
    }
    std::fs::write(path, format!("{}{}", body.trim_end(), add))
}

/// データ根が変わっても設定を引き継ぐ（2026-09-06）。
///
/// ★なぜ要るか（実機の障害で唯一残った、コードから見える経路）:
///   インストーラ（NSIS）は `%LOCALAPPDATA%` を触らない——`nsis/hooks.nsh` は
///   使用中のプロセスを止めるだけで、`bundle.resources` が置くのは
///   **インストール先**の `resources\llama` と `pyruntime` だけ。つまり
///   「更新が設定ファイルを上書きした」は**起こらない**。
///   起こりうるのは逆で、**見に行く場所が変わる**こと:
///   `platform_root()` は利用者名に非 ASCII が混ざるかで
///   `%LOCALAPPDATA%\Blackwall` と `%PUBLIC%\Blackwall` を出し分けるので、
///   環境変数の見え方が変われば根が動き、新しい根には設定が無い＝既定が書かれる。
///
/// 見つけたら**写す**（元は消さない）。移動にすると、根が元に戻ったときに
/// 今度は逆向きに設定を失う。
fn adopt_config_from_other_root(current_root: &PathBuf, dest: &PathBuf) {
    if dest.exists() {
        return;
    }
    let mut others: Vec<PathBuf> = Vec::new();
    #[cfg(windows)]
    {
        if let Some(p) = std::env::var_os("LOCALAPPDATA") {
            others.push(PathBuf::from(&p).join(ROOT_NEW));
            others.push(PathBuf::from(p).join(ROOT_OLD));
        }
        let public = std::env::var_os("PUBLIC")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("C:\\Users\\Public"));
        others.push(public.join(ROOT_NEW));
        others.push(public.join(ROOT_OLD));
        if let Some(p) = std::env::var_os("APPDATA") {
            others.push(PathBuf::from(&p).join(ROOT_NEW));
            others.push(PathBuf::from(p).join(ROOT_OLD));
        }
    }
    #[cfg(not(windows))]
    {
        if let Some(h) = std::env::var_os("HOME") {
            others.push(PathBuf::from(&h).join(".local").join("share").join(ROOT_NEW));
            others.push(PathBuf::from(&h).join(".local").join("share").join(ROOT_OLD));
            others.push(PathBuf::from(&h).join(paths::WS_NEW));
            others.push(PathBuf::from(&h).join(paths::WS_OLD));
        }
    }
    if let Some(l) = legacy_root() {
        others.push(l);
    }
    for cand in backend_choice::adoption_candidates(current_root, &others) {
        if !cand.is_file() {
            continue;
        }
        match std::fs::copy(&cand, dest) {
            Ok(_) => {
                eprintln!("[blackwall] 設定を引き継ぎました: {} → {}", cand.display(), dest.display());
                return;
            }
            Err(e) => eprintln!("[blackwall] 設定を引き継げません（{}）: {e}", cand.display()),
        }
    }
}

fn write_default_config_if_missing(path: &PathBuf) -> std::io::Result<()> {
    if path.exists() {
        return Ok(());
    }
    let body = "\
# L.A.P.S Console — 設定
# アプリが無くても中身が分かるように、人が読める形式にしている。
# 値を変えたら、画面の「再試行」を押せばその場で効く（再起動は要らない）。

# LAN の推論機（任意）。手元 GPU に載らないモデルの逃げ道。
# ★3つの値を取る（2026-09-06）:
#     \"auto\" … 既定。**この機械で本当に推論できるときだけ**手元を使う。
#               模型（.gguf）が1つも無い等で手元が使えないなら、下の宛先へ投げる。
#     true    … いつも LAN。手元の GPU は起こさない。
#     false   … いつも手元。LAN へは投げない。
# ★届かないときは黙って手元へ戻らず、画面にそう出す
#   ——「気づかないうちに別のマシンで動いていた」を作らないため。
# ★なぜ auto が既定か: 以前の既定は false で、設定が既定へ戻った機械は
#   この1行のせいで永久に手元固定になり、空の推論機に45秒待ってから
#   失敗し続けた（2026-09-06 実機）。auto なら、手元が使えないことは
#   その場で分かるので黙って詰まらない。
# ★2026-10-07: 画面で切り替えられる（接続画面と Models 画面の「推論の場所」）。
#   サーバー＝true／この PC の GPU＝false。Models 画面で手元のモデルを選ぶと false になる。
use_lan_backend = true
lan_backend_url = \"http://127.0.0.1:8080\"
# LAN 側で待っているモデルの名前（llama-server の -a と同じ文字列）。
# （llama.cpp の server は名前を気にしない。Ollama / vLLM ならそちらのモデル名に合わせる）
lan_model = \"default\"

# 実行の権限。既定は workspace（作業フォルダの中だけ書ける）。
#   trusted   … Windows/Linux のコマンドを制限なく実行し、どこへでも書ける。
#               すべての呼び出しは audit に残る（何が走ったかは後から必ず辿れる）。
#   workspace … 作業フォルダの中だけ書ける。外部到達や昇格は不可。
#   readonly  … 読むだけ。
# ★trusted は境界を外す選択です。モデルが誤ればこの機械が壊れます。
#   使い捨ての VM やコンテナの中でだけ trusted にしてください。
policy = \"workspace\"

# 倫理・慎重さの刻み。既定は raw（オーナー指示 2026-08-29「倫理的制限もすべて排除」）。
#   strict   … 確認義務を増やす。書き換え前に必ず読む・解釈が割れたら聞く。
#   standard … 旧既定。
#   direct   … 迷いを生む部分を外す。破壊的操作は先に述べるが確認は待たない。
#   raw      … 慎重さの塊を丸ごと無し。
# ★これは**熟慮の量**を変えるだけで、実行できることは変わりません。
#   出来ることを決めるのは上の policy です。raw にしても policy は緩みません。
# ★「捏造しない／未検証を完了と言わない」はどの刻みでも残ります。
#   あれは倫理検閲ではなく測定の前提で、外すと「速く終わったように見えて
#   何も出来ていない」走行が高得点になり、刻みの比較そのものが壊れるため。
ethics = \"raw\"

# 作業の上限。**0 は「上限なし」**（2026-08-22 オーナー決定）。
#   max_iters      … 何往復まで考えるか
#   max_seconds    … 何秒まで走るか
#   max_tool_calls … 道具を何回まで呼ぶか
# ★0 にしても止まらなくなるわけではありません。同じ手を繰り返したときの
#   ループ検知、文脈が尽きたとき、そして画面の「止める」は効きます。
# ★大きな依頼（「〜のようなものを作って」）は 40往復では終わりません。
#   実際に手を動かしていても途中で切られ、全部失敗したように見えていました。
max_iters = 0
max_seconds = 0
max_tool_calls = 0

# ここに認証情報を書かないこと。このファイルは平文で、バックアップにも入る。
";
    std::fs::write(path, body)
}

fn main() {
    // ★GUI を立ち上げずに診断だけ行う入口。開発側が SSH から同じ経路を確かめるために使う。
    //   待ち受けポートは作らない（起動できるポートは任意コード実行の口になる）。
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--selftest") {
        let write = args.iter().any(|a| a == "--write");
        let rt = tokio::runtime::Runtime::new().expect("runtime");
        let steps = rt.block_on(selftest::run(write));
        selftest::print(&steps);
        std::process::exit(if steps.iter().any(|s| !s.ok) { 1 } else { 0 });
    }

    tauri::Builder::default()
        // フォルダ選択に要る。★dialog の capability は画面側に開けない
        //（呼ぶのは Rust だけ。WebView に任意パスを開く口を作らないため）。
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            chromebridge::start();   // ふだんの Chrome をつなぐ口（2026-11-18）
            // ★同梱の道具の部品（Listen・Desktop）を裏でそろえる（2026-10-07・sidecar.rs）。
            sidecar::prepare_parts_in_background();
            // ★スマホから、この機械のセッションを扱えるよう中継へ名乗る（2026-10-08・sidecar.rs）。
            sidecar::start_remote_host_in_background();
            // ★スマホから、この機械の管理の操作も受ける（受け箱を見る橋・待ち受けの口は作らない・2026-10-10）。
            remotectl::start(app.handle().clone());
            let paths = AppPaths::resolve();

            // 失敗しても起動は続ける。ここで落とすと「なぜ起動しないのか」が
            // 画面に出せなくなり、いちばん直しにくい壊れ方になる。
            if let Err(e) = paths.ensure() {
                eprintln!("[blackwall] データフォルダを作れません ({}): {e}", paths.root.display());
            }
            // ★既定を書く**前**に、別の根に残っている設定を引き継ぐ。
            //   逆にすると、引き継ぐ相手が居ても既定が先に書かれて二度と拾えない。
            adopt_config_from_other_root(&paths.root, &paths.config);
            if let Err(e) = write_default_config_if_missing(&paths.config) {
                eprintln!("[blackwall] 設定ファイルを書けません ({}): {e}", paths.config.display());
            }
            // ★既に設定ファイルがある人にも、後から足した鍵を届ける（上書きはしない）。
            if let Err(e) = ensure_config_keys(&paths.config) {
                eprintln!("[blackwall] 設定に鍵を足せません ({}): {e}", paths.config.display());
            }

            // 旧い場所（利用者プロファイル配下）にモデルが残っていれば移す。
            // 非 ASCII のパスだと llama.cpp が開けないため、場所ごと ASCII 側へ寄せる。
            if let Some(msg) = migrate_from_legacy() {
                eprintln!("[blackwall] {msg}");
            }

            app.manage(paths);

            // 記録の送り役を1本だけ立てる。★ここで失敗しても起動は続く
            //（記録が送れないより、アプリが開かないほうが困る。中は全部 fail-soft）。
            journal::start();

            Ok(())
        })
        // ★ llama-server の子プロセスハンドルは、あえてここで manage していない。
        //   固定インターフェイスの llama_stop() / llama_status() は引数を取らない＝
        //   tauri::State を受け取れないため、ハンドルは llama.rs 内のプロセスグローバル
        //   （OnceLock<Mutex<..>>）として持つ設計になっている。二重管理は事故のもとなので
        //   ここでは触らない。
        //
        // ★ 終了時に llama-server を殺していないのも意図的。
        //   「アプリが落ちても推論と作業は死なない」（DESIGN §0・旧 UI の合格条件⑦）を保つ。
        //   停止は画面から明示的に行う（破壊操作なので確認ダイアログ付き）。
        .invoke_handler(tauri::generate_handler![
            app_version,
            app_restart,
            agent_tools,
            engine_status,
            chat_stop,
            core_answer,
            core_inject,
            remember,
            conv_toast,
            snapshot::send_snapshot,
            attach::attach_pick,
            attach::attach_save,
            attach::attach_bytes,
            filepreview::file_preview,
            filepreview::file_reveal,
            filepreview::file_open,
            filepreview::shot_data,
            filepreview::viewer_open,
            filepreview::viewer_pending,
            browser::browser_show,
            browser::browser_hide,
            browser::browser_nav,
            browser::browser_popout,
            browser::browser_dock,
            browser::browser_diag,
            voiceinput::voice_transcribe,
            peers::peers_load,
            peers::remote_status,
            auth::auth_local,
            auth::auth_call,
            convos::convos_load,
            convos::session_recover_run,
            convos::convos_save,
            convos::convos_path,
            convos::memory_stats,
            convos::memory_delete_sessions,
            convos::session_fork,
            convos::change_base,
            chromebridge::chrome_bridge_info,
            chromebridge::chrome_ext_open,
            scm::scm_status,
            scm::scm_diff,
            scm::scm_stage,
            scm::scm_discard,
            scm::scm_commit,
            scm::scm_branches,
            scm::scm_switch,
            scm::ws_files,
            scm::ws_dir,
            workspace::ws_create,
            workspace::ws_rename,
            scm::explorer_window_open,
            scm::scm_show,
            workspace::editor_read,
            workspace::editor_write,
            convos::memory_purge,
            convos::memory_trash_restore,
            convos::memory_trash_empty,
            convos::sessions_rebuild,
            convos::session_runs,
            convos::convos_restore_history,
            metrics::server_metrics,
            journal::journal_status,
            journal::journal_flush,
            journal::journal_path,
            gpu::probe_gpu,
            fit::estimate_fit,
            fit::fit_local,
            catalog::catalog_list,
            // ★command は main.rs（generate_handler! と同じモジュール）に置かない。
            //   置くと生成される use と定義が衝突し、絶対パスでも回避できない
            //   （E0255 ＋ macro-expanded macro_export … cannot be referred to by absolute paths）。
            sidecar::backend_info,
            sidecar::backend_probe,
            sidecar::lan_settings_get,
            sidecar::lan_settings_set,
            llama::llama_start,
            llama::llama_stop,
            llama::llama_status,
            llama::llama_load,
            llama::chat_send,
            hf::hf_search,
            hf::hf_repo_files,
            hf::hf_download,
            localgpu::local_gpu_status,
            localgpu::local_gpu_prepare,
            hf::list_local_models,
            hf::open_models_dir,
            selftest::self_test,
            integrity::integrity_check,
            integrity::integrity_fix,
            apikeys::apikeys_list,
            apikeys::apikeys_set,
            apikeys::apikeys_delete,
            mcp::mcp_get,
            mcp::mcp_set,
            mcp::mcp_status,
            workspace::ws_pick,
            workspace::ws_get,
            workspace::ws_set,
            workspace::ws_recent,
            workspace::ws_list,
            workspace::ws_read,
            workspace::ws_write,
            workspace::ws_edit,
            agent::agent_send,
            localshell::local_shell_open,
            localshell::local_shell_input,
            localshell::local_shell_resize,
            localshell::local_shell_close,
            localshell::shell_window_open,
            localshell::shell_window_cwd,
        ])
        .build(tauri::generate_context!())
        .expect("L.A.P.S Console を起動できませんでした")
        // ★llama-server（作業の推論機）は上の注記のとおり道連れにしない。
        //   （音声アシスタントは 2026-11-17 オーナー「あんま使ってないので音声アシスタントは廃止」で外した。閉じるときに止める物は無い）
        .run(|_app, _ev| {});
}

#[cfg(test)]
mod tests {
    use super::*;

    /// ★実機で二度失敗した条件を固定する。
    /// llama.cpp は CreateFileA / fopen を使う（同梱バイナリの import で確認）ので、
    /// 非 ASCII を含むパスは開けないことがある。根が ASCII であることを保証する。
    #[test]
    #[cfg(windows)]
    fn the_data_root_is_always_ascii() {
        // 非 ASCII の LOCALAPPDATA を与えても、根は ASCII になる
        std::env::set_var("LOCALAPPDATA", r"C:\Users\田中　優輝\AppData\Local");
        std::env::set_var("PUBLIC", r"C:\Users\Public");
        let r = app_root();
        assert!(r.to_string_lossy().is_ascii(), "根に非 ASCII が残っている: {}", r.display());
        assert!(models_dir().to_string_lossy().is_ascii());
    }

    #[test]
    #[cfg(windows)]
    fn an_ascii_profile_keeps_using_localappdata() {
        // 普通の利用者名なら、わざわざ公開フォルダへ逃がさない
        std::env::set_var("LOCALAPPDATA", r"C:\Users\alice\AppData\Local");
        let r = app_root();
        assert!(r.to_string_lossy().contains("AppData"), "{}", r.display());
    }
}
