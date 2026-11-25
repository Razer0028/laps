//! Hugging Face 連携 — 検索 / ファイル一覧 / 再開可能ダウンロード / 手持ちモデルの列挙。
//!
//! ネットワーク I/O は全て Rust 側に置く（WebView からは fetch しない）。CORS を回避でき、
//! 失敗の診断が1箇所に集まる。
//!
//! 追加依存（Cargo.toml）:
//!   sha2 = "0.10"     ← ダウンロード完了時の SHA256 照合に必要（本モジュールのみが要求）
//!   reqwest は features = ["json"] で足りる。本モジュールは Response::chunk() を使うので
//!   "stream" 機能や futures-util は不要。
//!
//! 使う HF エンドポイント（2026-08-11 実測・これ以外は使わない）:
//!   GET  /api/models?search=&filter=gguf&limit=&sort=downloads&direction=-1
//!   GET  /api/models/{repo}                          → HF が解析済みの "gguf" ブロック
//!   GET  /api/models/{repo}/tree/{rev}?recursive=true → path / size / lfs.oid(=SHA256)
//!   GET  /{repo}/resolve/{rev}/{path}                 → 302 で署名付き CDN URL

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter};
use tokio::fs;
use tokio::fs::{File, OpenOptions};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const HF_HOST: &str = "https://huggingface.co";
/// リビジョンは main 固定。ユーザーに revision を選ばせる UI は無い（設計 4.2）。
const REVISION: &str = "main";
const USER_AGENT: &str = "blackwall-console/0.1";
/// 進捗イベントの上限頻度。毎チャンク emit すると WebView 側が詰まる。
const PROGRESS_INTERVAL: Duration = Duration::from_millis(250);
/// URL 失効・瞬断からの再開回数。18.6GB 級は家庭回線で2時間級＝必ず1回は切れる前提。
const MAX_ATTEMPTS: u32 = 8;
const READ_BUF: usize = 1 << 20;

// ---------------------------------------------------------------------------
// 公開型（フロントエンドと他モジュールが依存する固定インターフェース）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HfRepo {
    pub id: String,
    pub downloads: u64,
    pub likes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HfFile {
    pub path: String,
    pub size: u64,
    /// LFS の oid（＝実体の SHA256）。分割シャードを1件にまとめた行では None。
    pub sha256: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HfRepoInfo {
    pub repo: String,
    pub files: Vec<HfFile>,
    pub architecture: Option<String>,
    pub context_length: Option<u32>,
    pub total_params: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalModel {
    pub path: String,
    pub name: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, Serialize)]
struct DownloadProgress {
    /// 要求時に渡された HF 上のファイルパス。フロントはこれで行を突き合わせる。
    path: String,
    /// 同じファイル名が別リポジトリにあり得るので repo も添える（追加フィールド）。
    repo: String,
    received: u64,
    /// 総サイズが分からない場合は null。
    total: Option<u64>,
    bytes_per_sec: f64,
}

// ---------------------------------------------------------------------------
// HTTP クライアント
// ---------------------------------------------------------------------------

static API_CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
static RESOLVE_CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
static STREAM_CLIENT: OnceLock<reqwest::Client> = OnceLock::new();

fn client_from(
    cell: &'static OnceLock<reqwest::Client>,
    build: fn() -> reqwest::ClientBuilder,
) -> Result<&'static reqwest::Client, String> {
    if let Some(c) = cell.get() {
        return Ok(c);
    }
    let built = build()
        .build()
        .map_err(|e| format!("HTTP クライアントを作れません: {e}"))?;
    // 競合しても余分な Client が捨てられるだけで害は無い。
    let _ = cell.set(built);
    cell.get()
        .ok_or_else(|| "HTTP クライアントの初期化に失敗しました".to_string())
}

/// JSON API 用。リダイレクトは既定どおり追い、全体タイムアウトを持たせる。
fn api_client() -> Result<&'static reqwest::Client, String> {
    client_from(&API_CLIENT, || {
        reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .connect_timeout(Duration::from_secs(15))
            .timeout(Duration::from_secs(30))
    })
}

/// /resolve/ 用。★リダイレクトを追ってはいけない —— 302 のヘッダ
/// （location / x-linked-etag / x-linked-size）そのものが欲しい情報。
fn resolve_client() -> Result<&'static reqwest::Client, String> {
    client_from(&RESOLVE_CLIENT, || {
        reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(15))
            .timeout(Duration::from_secs(30))
    })
}

/// 本体転送用。★全体タイムアウトを設定しない（数 GB の転送を途中で殺してしまう）。
fn stream_client() -> Result<&'static reqwest::Client, String> {
    client_from(&STREAM_CLIENT, || {
        reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .connect_timeout(Duration::from_secs(15))
    })
}

// ---------------------------------------------------------------------------
// 置き場所
// ---------------------------------------------------------------------------

/// モデルの置き場所は1箇所（設計 §6「置き場所」）。
/// Windows: %LOCALAPPDATA%\Blackwall\models
/// それ以外: $XDG_DATA_HOME/blackwall/models（Linux で型検査・単体試験するための代替路）
pub fn models_dir() -> Result<PathBuf, String> {
    // ★根は crate::models_dir() に一本化（非 ASCII 回避の判断もそこに集約）。
    let d = crate::models_dir();
    std::fs::create_dir_all(&d).map_err(|e| format!("{} を作成できません: {e}", d.display()))?;
    Ok(d)
}

// ---------------------------------------------------------------------------
// 入力の検証とパス組み立て
// ---------------------------------------------------------------------------

fn validate_repo(repo: &str) -> Result<(), String> {
    if repo.is_empty() || repo.len() > 200 {
        return Err("リポジトリ名が空か長すぎます".to_string());
    }
    if repo.contains("..") {
        return Err("リポジトリ名に .. は使えません".to_string());
    }
    let mut parts = repo.split('/');
    let owner = parts.next().unwrap_or("");
    let name = parts.next().unwrap_or("");
    if owner.is_empty() || name.is_empty() || parts.next().is_some() {
        return Err(format!(
            "リポジトリは owner/name の形式で指定してください（例: unsloth/Qwen3-14B-GGUF）。受け取った値: {repo}"
        ));
    }
    for c in repo.chars() {
        if !(c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '/')) {
            return Err(format!("リポジトリ名に使えない文字があります: {c}"));
        }
    }
    Ok(())
}

fn validate_file_path(path: &str) -> Result<(), String> {
    if path.is_empty() || path.len() > 400 {
        return Err("ファイルパスが空か長すぎます".to_string());
    }
    // ここを通す値がそのままローカルのファイル名生成に使われる。素通しにしない。
    if path.starts_with('/') || path.contains("..") || path.contains('\\') {
        return Err(format!("ファイルパスが不正です: {path}"));
    }
    if path.chars().any(|c| c.is_control()) {
        return Err("ファイルパスに制御文字が含まれています".to_string());
    }
    Ok(())
}

/// URL パスとして安全な文字のみ素通しし、'/' はセグメント区切りとして残す。
fn encode_path(path: &str) -> String {
    let mut out = String::with_capacity(path.len());
    for &b in path.as_bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~' | b'/') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}

/// クエリ値用。'/' も含めて全てエスケープする。
fn encode_query(v: &str) -> String {
    let mut out = String::with_capacity(v.len());
    for &b in v.as_bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}

/// Windows のファイル名に使えない文字を潰す。'.' は拡張子に要るので残す。
fn sanitize_component(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') {
            out.push(c);
        } else {
            out.push('_');
        }
    }
    out
}

/// `%LOCALAPPDATA%\Blackwall\models\<repo-sanitised>--<filename>`
/// 別リポジトリの同名ファイル（`Qwen3-14B-Q4_K_M.gguf` は何十も存在する）が
/// 互いを踏まないようにリポジトリ名を前置する。
pub(crate) fn target_file_name(repo: &str, path: &str) -> String {
    let file = path.rsplit('/').next().unwrap_or(path);
    format!("{}--{}", sanitize_component(repo), sanitize_component(file))
}

fn part_path_of(final_path: &Path) -> PathBuf {
    let mut os = final_path.to_path_buf().into_os_string();
    os.push(".part");
    PathBuf::from(os)
}

// ---------------------------------------------------------------------------
// 同時実行の抑止
// ---------------------------------------------------------------------------

static IN_FLIGHT: OnceLock<Mutex<HashSet<PathBuf>>> = OnceLock::new();

/// 同じ .part を2つのダウンロードが追記すると静かに壊れる（長さは伸びるが中身が混ざる）。
/// ハッシュ照合で最後に気づけるが、GB 単位を捨てることになるので入口で止める。
struct InFlight(PathBuf);

impl InFlight {
    fn acquire(p: &Path) -> Result<Self, String> {
        let set = IN_FLIGHT.get_or_init(|| Mutex::new(HashSet::new()));
        // ロックは await をまたがない（std の Mutex を使ってよい理由）。
        let mut guard = set
            .lock()
            .map_err(|_| "内部状態が壊れています（ロック取得に失敗）".to_string())?;
        if !guard.insert(p.to_path_buf()) {
            return Err("このファイルは既にダウンロード中です".to_string());
        }
        Ok(InFlight(p.to_path_buf()))
    }
}

impl Drop for InFlight {
    fn drop(&mut self) {
        if let Some(set) = IN_FLIGHT.get() {
            if let Ok(mut guard) = set.lock() {
                guard.remove(&self.0);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// hf_search
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct SearchHit {
    #[serde(default)]
    id: String,
    #[serde(default)]
    downloads: u64,
    #[serde(default)]
    likes: u64,
}

#[tauri::command]
pub async fn hf_search(query: String) -> Result<Vec<HfRepo>, String> {
    let q = query.trim();
    // 空入力でエラー帯を出さない（入力途中で毎回叱らない）。
    if q.is_empty() {
        return Ok(Vec::new());
    }
    let url = format!(
        "{HF_HOST}/api/models?search={}&filter=gguf&limit=8&sort=downloads&direction=-1",
        encode_query(q)
    );
    let resp = api_client()?
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("Hugging Face に接続できません: {e}"))?;
    let hits: Vec<SearchHit> = read_json(resp, "検索").await?;
    Ok(hits
        .into_iter()
        .filter(|h| !h.id.is_empty())
        .map(|h| HfRepo {
            id: h.id,
            downloads: h.downloads,
            likes: h.likes,
        })
        .collect())
}

// ---------------------------------------------------------------------------
// hf_repo_files
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct TreeEntry {
    #[serde(default)]
    path: String,
    #[serde(default)]
    size: u64,
    #[serde(default)]
    lfs: Option<TreeLfs>,
}

#[derive(Deserialize)]
struct TreeLfs {
    /// LFS の oid はコンテンツの SHA256（302 の x-linked-etag と一致する）。
    #[serde(default)]
    oid: Option<String>,
    #[serde(default)]
    size: Option<u64>,
}

#[derive(Deserialize)]
struct ModelInfoResp {
    #[serde(default)]
    gguf: Option<GgufBlock>,
}

/// HF が GGUF ヘッダを解析済みで返してくれるブロック。
/// ★これがあるので、落とす前の判定に GGUF ヘッダの range 読みは要らない（設計 決定3）。
#[derive(Deserialize)]
struct GgufBlock {
    #[serde(default)]
    total: Option<u64>,
    #[serde(default)]
    architecture: Option<String>,
    #[serde(default)]
    context_length: Option<u32>,
}

/// `model-00001-of-00005.gguf` → (グループ名 "model", 番号 1, 総数 5)
fn split_shard(file_name: &str) -> Option<(String, u32, u32)> {
    let lower = file_name.to_ascii_lowercase();
    if !lower.ends_with(".gguf") {
        return None;
    }
    let stem = &file_name[..file_name.len() - ".gguf".len()];
    // to_ascii_lowercase はバイト長を変えないので、小文字化した側で得た添字を
    // そのまま元の文字列に使ってよい（非 ASCII はそのまま残る＝境界も一致する）。
    let cut = stem.to_ascii_lowercase().rfind("-of-")?;
    let total_str = &stem[cut + "-of-".len()..];
    if total_str.is_empty() || !total_str.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let head = &stem[..cut]; // "…-00001"
    let dash = head.rfind('-')?;
    let index_str = &head[dash + 1..];
    if index_str.is_empty() || !index_str.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let base = &head[..dash];
    if base.is_empty() {
        return None;
    }
    let index = index_str.parse::<u32>().ok()?;
    let total = total_str.parse::<u32>().ok()?;
    if total < 2 || index == 0 || index > total {
        return None;
    }
    Some((base.to_string(), index, total))
}

/// GGUF 本体以外（imatrix・mmproj）は取り込み候補から外す。
/// imatrix は量子化に使う統計、mmproj は視覚投影で、どちらも単体では動かない。
fn is_auxiliary(file_name_lower: &str) -> bool {
    file_name_lower.contains("imatrix") || file_name_lower.contains("mmproj")
}

#[tauri::command]
pub async fn hf_repo_files(repo: String) -> Result<HfRepoInfo, String> {
    validate_repo(&repo)?;

    let tree_url = format!(
        "{HF_HOST}/api/models/{}/tree/{REVISION}?recursive=true",
        encode_path(&repo)
    );
    let resp = api_client()?
        .get(&tree_url)
        .send()
        .await
        .map_err(|e| format!("Hugging Face に接続できません: {e}"))?;
    let entries: Vec<TreeEntry> = read_json(resp, "ファイル一覧の取得").await?;

    // メタ情報は「あれば嬉しい」もの。ここで落ちてもファイル一覧は返す
    // （リポジトリによっては gguf ブロックが無い）。
    let meta_url = format!("{HF_HOST}/api/models/{}", encode_path(&repo));
    let meta: Option<GgufBlock> = match api_client()?.get(&meta_url).send().await {
        Ok(r) if r.status().is_success() => r
            .json::<ModelInfoResp>()
            .await
            .ok()
            .and_then(|m| m.gguf),
        _ => None,
    };

    // --- .gguf だけを残し、分割シャードは1行にまとめる -----------------------
    // まとめた行は「先頭シャードのパス＋全シャードの合計サイズ」を持つ。
    // ★制限: hf_download は1ファイル単位なので、この行を落としても先頭シャードしか来ない。
    //   残りのシャードの取得は未実装（UI 側で明示するか、将来 download を分割対応させる）。
    //   合計サイズを見せるのは「載るか」の判定を誤らせないため（先頭だけの容量で判断させない）。
    struct Group {
        path: String,
        min_index: u32,
        size: u64,
    }
    let mut singles: Vec<HfFile> = Vec::new();
    let mut groups: Vec<(String, Group)> = Vec::new();

    for e in entries {
        if e.path.is_empty() {
            continue;
        }
        let file_name = e.path.rsplit('/').next().unwrap_or(&e.path).to_string();
        let lower = file_name.to_ascii_lowercase();
        if !lower.ends_with(".gguf") || is_auxiliary(&lower) {
            continue;
        }
        // LFS 実体のサイズを優先。非 LFS なら top-level の size。
        let size = e.lfs.as_ref().and_then(|l| l.size).unwrap_or(e.size);
        let oid = e.lfs.as_ref().and_then(|l| l.oid.clone());

        match split_shard(&file_name) {
            None => singles.push(HfFile {
                path: e.path.clone(),
                size,
                sha256: oid,
            }),
            Some((base, index, _total)) => {
                // グループ名はディレクトリまで含めて一意にする。
                let dir = match e.path.rfind('/') {
                    Some(i) => &e.path[..=i],
                    None => "",
                };
                let key = format!("{dir}{base}");
                match groups.iter_mut().find(|(k, _)| *k == key) {
                    Some((_, g)) => {
                        g.size = g.size.saturating_add(size);
                        if index < g.min_index {
                            g.min_index = index;
                            g.path = e.path.clone();
                        }
                    }
                    None => groups.push((
                        key,
                        Group {
                            path: e.path.clone(),
                            min_index: index,
                            size,
                        },
                    )),
                }
            }
        }
    }

    let mut files = singles;
    for (_, g) in groups {
        files.push(HfFile {
            path: g.path,
            size: g.size,
            // 合算した行に単一ファイルのハッシュは付けられない（照合は各シャード個別に行う）。
            sha256: None,
        });
    }
    files.sort_by(|a, b| a.path.cmp(&b.path));

    Ok(HfRepoInfo {
        repo,
        files,
        architecture: meta.as_ref().and_then(|g| g.architecture.clone()),
        context_length: meta.as_ref().and_then(|g| g.context_length),
        total_params: meta.as_ref().and_then(|g| g.total),
    })
}

// ---------------------------------------------------------------------------
// hf_download
// ---------------------------------------------------------------------------

/// 302 から取り出す、その回限りの情報。
struct ResolveInfo {
    /// 署名付き CDN URL。★正確に 3600 秒で失効するので保存して使い回さない。
    url: String,
    /// x-linked-etag ＝ 実体の SHA256（tree の lfs.oid と一致）。
    sha256: Option<String>,
    /// x-linked-size ＝ 実体の総サイズ。
    size: Option<u64>,
}

enum StreamErr {
    /// 一時的な失敗。URL を引き直して再試行する。
    Retry(String),
    /// レンジが効かなかった等。先頭からやり直す。
    RestartFromZero(String),
    /// 回復しない。
    Fatal(String),
}

/// URL 解決の失敗。★文字列を見て「再試行してよいか」を判断しない
/// （「見つかりません」と「繋がりません」がどちらも同じ語を含み、実際に取り違える）。
enum ResolveErr {
    Retry(String),
    Fatal(String),
}

fn header_str(resp: &reqwest::Response, name: &str) -> Option<String> {
    resp.headers()
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.trim().to_string())
}

/// ETag 形式の値から 64 桁 16 進だけを取り出す。
/// ★ここに罠がある: CDN が返す `etag` は xetHash であって内容の SHA256 ではない。
///   照合に使ってよいのは 302 応答の `x-linked-etag` だけ。取り違えると検証が必ず失敗する。
fn clean_content_hash(raw: &str) -> Option<String> {
    let s = raw.trim();
    let s = s.strip_prefix("W/").unwrap_or(s);
    let s = s.trim_matches('"');
    if s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()) {
        Some(s.to_ascii_lowercase())
    } else {
        None
    }
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{:02x}", b));
    }
    out
}

/// `bytes 100-199/12345` → Some(12345)。総サイズ不明の `*` は None。
fn parse_content_range_total(v: &str) -> Option<u64> {
    let (_, total) = v.rsplit_once('/')?;
    total.trim().parse::<u64>().ok()
}

/// ★毎回引き直す。多 GB の転送は署名の寿命（3600 秒）を必ず超える。
async fn resolve_download_url(repo: &str, path: &str) -> Result<ResolveInfo, ResolveErr> {
    let url = format!(
        "{HF_HOST}/{}/resolve/{REVISION}/{}",
        encode_path(repo),
        encode_path(path)
    );
    let resp = resolve_client()
        .map_err(ResolveErr::Fatal)?
        .get(&url)
        .send()
        .await
        .map_err(|e| ResolveErr::Retry(format!("ダウンロード URL を取得できません: {e}")))?;
    let status = resp.status();

    if status.is_redirection() {
        let location = header_str(&resp, "location").ok_or_else(|| {
            ResolveErr::Retry("302 が返りましたが location がありません".to_string())
        })?;
        let abs = if location.starts_with("http://") || location.starts_with("https://") {
            location
        } else if location.starts_with('/') {
            format!("{HF_HOST}{location}")
        } else {
            return Err(ResolveErr::Fatal(format!(
                "location を解釈できません: {location}"
            )));
        };
        return Ok(ResolveInfo {
            url: abs,
            sha256: header_str(&resp, "x-linked-etag").and_then(|v| clean_content_hash(&v)),
            size: header_str(&resp, "x-linked-size").and_then(|v| v.parse::<u64>().ok()),
        });
    }

    if status.is_success() {
        // 非 LFS の小さいファイルはリダイレクトせず直接返る。この場合 x-linked-* は無く、
        // 照合できない（.gguf は通常 LFS なのでまず通らない経路）。
        return Ok(ResolveInfo {
            url,
            sha256: header_str(&resp, "x-linked-etag").and_then(|v| clean_content_hash(&v)),
            size: resp.content_length(),
        });
    }

    if status.as_u16() == 404 {
        return Err(ResolveErr::Fatal(format!(
            "{repo} に {path} がありません（リポジトリ名かファイル名を確認してください）"
        )));
    }
    if status.as_u16() == 401 || status.as_u16() == 403 {
        return Err(ResolveErr::Fatal(format!(
            "{repo} は認証が要ります（gated / private）。ブラウザで利用条件に同意するか、公開リポジトリを選んでください"
        )));
    }
    // 5xx や 429 はこちらに落ちる＝待って引き直せば通ることがある。
    Err(ResolveErr::Retry(format!(
        "ダウンロード URL の取得に失敗しました: HTTP {status}"
    )))
}

/// 既存の .part を読み直してハッシュ器に積む。
/// 中断→再開ではプロセスが変わっている（＝途中経過のハッシュは残っていない）ので、
/// 追記を始める前に必ずここを通す。数 GB でもディスク読みは分単位にならない。
async fn rehash_existing(p: &Path, hasher: &mut Sha256) -> Result<u64, String> {
    let mut f = File::open(p)
        .await
        .map_err(|e| format!("途中のファイルを開けません {}: {e}", p.display()))?;
    let mut buf = vec![0u8; READ_BUF];
    let mut total: u64 = 0;
    loop {
        let n = f
            .read(&mut buf)
            .await
            .map_err(|e| format!("途中のファイルを読めません {}: {e}", p.display()))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        total += n as u64;
    }
    Ok(total)
}

#[allow(clippy::too_many_arguments)]
async fn stream_to_part(
    app: &AppHandle,
    repo: &str,
    hf_path: &str,
    info: &ResolveInfo,
    part_path: &Path,
    received: &mut u64,
    hasher: &mut Sha256,
    expected_total: &mut Option<u64>,
) -> Result<(), StreamErr> {
    let mut req = stream_client().map_err(StreamErr::Fatal)?.get(&info.url);
    if *received > 0 {
        // ★Range は huggingface.co では無視される。302 を追った後の CDN にだけ効くので、
        //   ここ（CDN の URL）で初めて付ける。
        req = req.header(reqwest::header::RANGE, format!("bytes={}-", *received));
    }

    let mut resp = req
        .send()
        .await
        .map_err(|e| StreamErr::Retry(format!("転送を開始できません: {e}")))?;
    let status = resp.status();

    if status.as_u16() == 416 {
        // 416 は「開始位置が実体の長さを超えた」。Content-Range の分母（`bytes */12345`）が
        // 取れれば、既に取り切っていただけなのか .part が壊れているのかを区別できる。
        if let Some(t) = header_str(&resp, "content-range").and_then(|v| parse_content_range_total(&v))
        {
            if expected_total.is_none() {
                *expected_total = Some(t);
            }
            if *received == t {
                return Ok(());
            }
        }
        return Err(StreamErr::RestartFromZero(
            "途中のファイルが実体と食い違うため、先頭からダウンロードし直します".to_string(),
        ));
    }
    if status.as_u16() == 403 || status.as_u16() == 401 {
        // 署名の失効がこの形で出る。URL を引き直せば直る。
        return Err(StreamErr::Retry(
            "署名付き URL が失効しました（取得し直します）".to_string(),
        ));
    }
    if status.as_u16() == 404 {
        return Err(StreamErr::Fatal(
            "配信元にファイルがありません（HTTP 404）".to_string(),
        ));
    }
    if !status.is_success() {
        return Err(StreamErr::Retry(format!("転送に失敗しました: HTTP {status}")));
    }
    if *received > 0 && status.as_u16() != 206 {
        // レンジを無視して先頭から返してきた。追記すると混ざるので必ず捨てる。
        return Err(StreamErr::RestartFromZero(
            "配信元が途中再開に対応しませんでした。先頭からダウンロードし直します".to_string(),
        ));
    }

    // 総サイズ: x-linked-size > Content-Range の分母 > Content-Length + 開始位置
    if expected_total.is_none() {
        *expected_total = header_str(&resp, "content-range")
            .and_then(|v| parse_content_range_total(&v))
            .or_else(|| resp.content_length().map(|l| l + *received));
    }

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(part_path)
        .await
        .map_err(|e| StreamErr::Fatal(format!("書き込み先を開けません {}: {e}", part_path.display())))?;

    let mut last_emit = Instant::now();
    let mut last_received = *received;

    loop {
        let chunk = match resp.chunk().await {
            Ok(Some(c)) => c,
            Ok(None) => break,
            Err(e) => {
                let _ = file.flush().await;
                return Err(StreamErr::Retry(format!("転送が切れました: {e}")));
            }
        };
        // 「書けたものだけ」をハッシュに積む。順序を逆にすると、
        // 書き込み失敗時にハッシュとファイルが食い違う。
        file.write_all(&chunk).await.map_err(|e| {
            StreamErr::Fatal(format!("書き込みに失敗しました（空き容量を確認してください）: {e}"))
        })?;
        hasher.update(&chunk);
        *received += chunk.len() as u64;

        let now = Instant::now();
        let dt = now.duration_since(last_emit);
        if dt >= PROGRESS_INTERVAL {
            let rate = (*received - last_received) as f64 / dt.as_secs_f64();
            emit_progress(app, hf_path, repo, *received, *expected_total, rate);
            last_emit = now;
            last_received = *received;
        }
    }

    file.flush()
        .await
        .map_err(|e| StreamErr::Fatal(format!("書き込みを確定できません: {e}")))?;

    // 本体が途中で終わったのにエラーにならない経路がある（接続が正常終了扱いで切れる）。
    // 黙って「完了」にすると壊れた GGUF が手持ちに並ぶので、足りなければ再開させる。
    if let Some(t) = *expected_total {
        if *received < t {
            return Err(StreamErr::Retry(format!(
                "転送が途中で終わりました（{} / {} バイト）",
                *received, t
            )));
        }
    }
    Ok(())
}

/// .part を 0 バイトに戻す（ファイルは残す）。
async fn truncate_part(part_path: &Path) -> Result<(), String> {
    OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(part_path)
        .await
        .map_err(|e| format!("途中のファイルを初期化できません {}: {e}", part_path.display()))?;
    Ok(())
}

fn emit_progress(
    app: &AppHandle,
    path: &str,
    repo: &str,
    received: u64,
    total: Option<u64>,
    bytes_per_sec: f64,
) {
    let payload = DownloadProgress {
        path: path.to_string(),
        repo: repo.to_string(),
        received,
        total,
        bytes_per_sec,
    };
    // 進捗が届かないことは致命ではない（転送は続ける）。
    let _ = app.emit("download-progress", payload);
}

#[tauri::command]
pub async fn hf_download(app: AppHandle, repo: String, path: String) -> Result<String, String> {
    validate_repo(&repo)?;
    validate_file_path(&path)?;

    let dir = models_dir()?;
    fs::create_dir_all(&dir)
        .await
        .map_err(|e| format!("保存先を作れません {}: {e}", dir.display()))?;

    let final_path = dir.join(target_file_name(&repo, &path));
    if fs::metadata(&final_path).await.is_ok() {
        // 既にある。落とし直さない（18GB を二度取らない）。
        return Ok(final_path.to_string_lossy().into_owned());
    }
    let part_path = part_path_of(&final_path);
    let _guard = InFlight::acquire(&part_path)?;

    // 既存の .part があれば、その中身をハッシュに積んでから続きを取る。
    let mut hasher = Sha256::new();
    let mut received: u64 = 0;
    if let Ok(md) = fs::metadata(&part_path).await {
        if md.is_file() && md.len() > 0 {
            received = rehash_existing(&part_path, &mut hasher).await?;
        }
    }

    let mut expected_total: Option<u64> = None;
    let mut expected_sha: Option<String> = None;
    let mut attempt: u32 = 0;
    let mut last_err = String::new();

    loop {
        attempt += 1;
        if attempt > MAX_ATTEMPTS {
            return Err(format!(
                "{MAX_ATTEMPTS} 回試して完了しませんでした。途中まで（{received} バイト）は残してあるので、もう一度実行すると続きから再開します。最後の失敗: {last_err}"
            ));
        }

        // ★毎回ここで URL を引き直す。前回の署名は 3600 秒で失効している可能性がある。
        let info = match resolve_download_url(&repo, &path).await {
            Ok(i) => i,
            // リポジトリ/ファイルが無い・認証が要る場合は何度やっても同じ。すぐ返す。
            Err(ResolveErr::Fatal(e)) => return Err(e),
            Err(ResolveErr::Retry(e)) => {
                last_err = e;
                tokio::time::sleep(Duration::from_secs(attempt.min(5) as u64)).await;
                continue;
            }
        };
        if info.size.is_some() {
            expected_total = info.size;
        }
        if info.sha256.is_some() {
            expected_sha = info.sha256.clone();
        }

        // 前回の実行で取り切っていた場合（照合の直前で落ちた等）。
        if let Some(t) = expected_total {
            if t > 0 && received == t {
                break;
            }
            if t > 0 && received > t {
                // .part が実体より長い＝壊れている。放っておくと毎回サイズ不一致で
                // 詰まり、利用者は手で消すしかなくなる。ここで捨ててやり直す。
                truncate_part(&part_path).await?;
                received = 0;
                hasher = Sha256::new();
            }
        }

        match stream_to_part(
            &app,
            &repo,
            &path,
            &info,
            &part_path,
            &mut received,
            &mut hasher,
            &mut expected_total,
        )
        .await
        {
            Ok(()) => break,
            Err(StreamErr::Fatal(m)) => return Err(m),
            Err(StreamErr::Retry(m)) => {
                last_err = m;
                tokio::time::sleep(Duration::from_secs(attempt.min(5) as u64)).await;
            }
            Err(StreamErr::RestartFromZero(m)) => {
                last_err = m;
                truncate_part(&part_path).await?;
                received = 0;
                hasher = Sha256::new();
            }
        }
    }

    // --- 検証 ---------------------------------------------------------------
    if let Some(t) = expected_total {
        if received != t {
            return Err(format!(
                "サイズが合いません（受信 {received} / 期待 {t}）。途中のファイルは残してあるので、もう一度実行すると続きから再開します"
            ));
        }
    }

    let digest = hex_encode(&hasher.finalize());
    match &expected_sha {
        Some(expected) => {
            if !expected.eq_ignore_ascii_case(&digest) {
                let _ = fs::remove_file(&part_path).await;
                return Err(format!(
                    "内容が壊れています（SHA256 が一致しません: 期待 {expected} / 実際 {digest}）。壊れたファイルは削除しました。もう一度実行してください"
                ));
            }
        }
        None => {
            // x-linked-etag が無い（非 LFS の小さいファイル等）。照合できないので、
            // サイズ一致だけを根拠に通す。★CDN の `etag` で代用しない —— あれは
            // xetHash であって内容の SHA256 ではなく、必ず不一致になる。
        }
    }

    if fs::metadata(&final_path).await.is_ok() {
        // 競合で先に置かれた場合。Windows の rename は宛先があると失敗する。
        let _ = fs::remove_file(&final_path).await;
    }
    fs::rename(&part_path, &final_path)
        .await
        .map_err(|e| format!("ファイル名を確定できません {}: {e}", final_path.display()))?;

    emit_progress(&app, &path, &repo, received, expected_total, 0.0);
    Ok(final_path.to_string_lossy().into_owned())
}

// ---------------------------------------------------------------------------
// list_local_models
// ---------------------------------------------------------------------------

/// モデル置き場をエクスプローラで開き、そのパスを返す。
///
/// ★引数を取らないのは意図的。開く先はこちらが決めた1箇所だけで、画面側からパスを渡せない。
/// 「WebView に任意のパスを開かせる口」を作らないための形（shell プラグインを入れていないのと同じ理由）。
/// 置き場が無ければ先に作る——「開いたら空だった」より「開いたら入れる場所がある」ほうがよい。
#[tauri::command]
pub async fn open_models_dir() -> Result<String, String> {
    let dir = models_dir()?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("保存先を作れません: {e}"))?;

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        // explorer.exe は成功時も 1 を返すことがあるので、終了コードは見ない。
        // 起動そのものに失敗したときだけ誤りとして返す。
        std::process::Command::new("explorer.exe")
            .arg(&dir)
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("エクスプローラを開けません: {e}"))?;
    }
    #[cfg(not(windows))]
    {
        // Linux で型検査を通すための分岐。開く動作はしない。
        let _ = &dir;
    }
    Ok(dir.display().to_string())
}

#[tauri::command]
pub async fn list_local_models() -> Result<Vec<LocalModel>, String> {
    let dir = models_dir()?;
    let mut out: Vec<LocalModel> = Vec::new();

    let mut rd = match fs::read_dir(&dir).await {
        Ok(rd) => rd,
        // 置き場所がまだ無い＝1つも入れていないだけ。空状態は UI が扱う（設計 4.2）。
        Err(_) => return Ok(out),
    };

    while let Some(entry) = rd
        .next_entry()
        .await
        .map_err(|e| format!("保存先を読めません {}: {e}", dir.display()))?
    {
        let path = entry.path();
        let name = match path.file_name().and_then(|n| n.to_str()) {
            Some(n) => n.to_string(),
            None => continue,
        };
        // 取り寄せ途中の .part は「手持ち」ではない。
        if !name.to_ascii_lowercase().ends_with(".gguf") {
            continue;
        }
        let md = match entry.metadata().await {
            Ok(m) => m,
            Err(_) => continue,
        };
        if !md.is_file() {
            continue;
        }
        out.push(LocalModel {
            path: path.to_string_lossy().into_owned(),
            name,
            size_bytes: md.len(),
        });
    }

    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}

// ---------------------------------------------------------------------------
// 共通
// ---------------------------------------------------------------------------

async fn read_json<T: serde::de::DeserializeOwned>(
    resp: reqwest::Response,
    what: &str,
) -> Result<T, String> {
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        let snippet: String = body.chars().take(200).collect();
        return Err(format!("{what}に失敗しました: HTTP {status} {snippet}"));
    }
    resp.json::<T>()
        .await
        .map_err(|e| format!("{what}の応答を解釈できません: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shard_names_are_grouped() {
        assert_eq!(
            split_shard("Qwen3-Coder-30B-Q4_K_M-00001-of-00005.gguf"),
            Some(("Qwen3-Coder-30B-Q4_K_M".to_string(), 1, 5))
        );
        assert_eq!(split_shard("Qwen3-14B-Q4_K_M.gguf"), None);
        assert_eq!(split_shard("model.safetensors"), None);
        // 番号が数字でないものはシャードとして扱わない。
        assert_eq!(split_shard("weird-abc-of-00002.gguf"), None);
    }

    #[test]
    fn only_x_linked_etag_shaped_values_are_accepted() {
        let sha = "a".repeat(64);
        assert_eq!(clean_content_hash(&format!("\"{sha}\"")), Some(sha.clone()));
        // xetHash は 64 桁 16 進ではない形で来ることが多い。長さで弾く。
        assert_eq!(clean_content_hash("\"deadbeef\""), None);
    }

    #[test]
    fn target_names_keep_repo_and_extension() {
        assert_eq!(
            target_file_name("unsloth/Qwen3-14B-GGUF", "Q4_K_M/Qwen3-14B-Q4_K_M.gguf"),
            "unsloth_Qwen3-14B-GGUF--Qwen3-14B-Q4_K_M.gguf"
        );
    }

    #[test]
    fn traversal_is_rejected() {
        assert!(validate_file_path("../../etc/passwd").is_err());
        assert!(validate_repo("owner").is_err());
        assert!(validate_repo("owner/name").is_ok());
    }

    #[test]
    fn content_range_total_is_the_denominator() {
        assert_eq!(parse_content_range_total("bytes 100-199/12345"), Some(12345));
        assert_eq!(parse_content_range_total("bytes 100-199/*"), None);
    }
}
