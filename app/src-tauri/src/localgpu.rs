//! この PC の GPU で推論する準備（2026-10-07 オーナー「アップデート入れてください。モデルも同梱して」）。
//!
//! ★モデル（16.5GB）はインストーラに入れない。NSIS の上限は 2GB で、更新のたびに全体を落とし直す作りでもある。
//!   代わりにアプリが初回に取り寄せ、SHA256 を照合してから置く（モデルは hf.rs の再開できる取り込みを使う）。
//! ★推論機は llama.cpp の CUDA 版を別に取り寄せる。同梱の Vulkan 版（8 月）より新しく、4070 での読み込みが速い。
//!   置き場は `<データ根>\engine-cuda\`。llama.rs はここに実体があれば同梱版より先に使う。
//! ★実測（RTX 4070 12GB・2026-10-07）: Qwen3.6-35B-A3B UD-IQ4_XS は 4070 に約 8GB・残りを CPU 側。
//!   前置き 1.08 万トークンの全部読み 450 tok/s（一度に 2,048）・生成 16〜27 tok/s・VRAM 11.3/12.3GB・RAM 約 10GB。
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter};
use tokio::io::AsyncWriteExt;

/// 推論機の版（llama.cpp のリリース名）。上げるときは下の 2 つの照合値も取り直す。
pub const ENGINE_TAG: &str = "b11457";
/// 推奨モデル（4070 12GB に載る最新の Qwen。Qwen3.8 には 12GB に載る版が無い・2026-10-07 時点）。
pub const MODEL_REPO: &str = "unsloth/Qwen3.6-35B-A3B-GGUF";
pub const MODEL_FILE: &str = "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf";

struct Asset {
    url: &'static str,
    sha256: &'static str,
    file: &'static str,
}

/// GitHub のリリースの添付（照合値は GitHub の API が出す digest・2026-10-07 に取得）。
const ENGINE_ZIPS: [Asset; 2] = [
    Asset {
        url: "https://github.com/ggml-org/llama.cpp/releases/download/b11457/llama-b11457-bin-win-cuda-12.4-x64.zip",
        sha256: "c901dbb473c9472288e38ef06bf85e4e13d7de6bb24732b03dbe8aa05fcc483c",
        file: "llama-b11457-bin-win-cuda-12.4-x64.zip",
    },
    Asset {
        url: "https://github.com/ggml-org/llama.cpp/releases/download/b11457/cudart-llama-bin-win-cuda-12.4-x64.zip",
        sha256: "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
        file: "cudart-llama-bin-win-cuda-12.4-x64.zip",
    },
];

#[cfg(windows)]
const SERVER_BIN: &str = "llama-server.exe";
#[cfg(not(windows))]
const SERVER_BIN: &str = "llama-server";

static PREPARING: AtomicBool = AtomicBool::new(false);

pub fn engine_dir() -> PathBuf {
    crate::app_root().join("engine-cuda")
}

/// 取り寄せた CUDA 版の実体。無ければ None（同梱の版を使う）。
pub fn engine_exe() -> Option<PathBuf> {
    let p = engine_dir().join(SERVER_BIN);
    p.is_file().then_some(p)
}

/// この実体が取り寄せた CUDA 版か（INI に起動設定を書くかの判定に使う）。
pub fn is_cuda_engine(exe: &Path) -> bool {
    exe.starts_with(engine_dir())
}

fn model_path() -> PathBuf {
    crate::models_dir().join(crate::hf::target_file_name(MODEL_REPO, MODEL_FILE))
}

#[derive(Debug, Clone, Serialize)]
pub struct LocalGpuStatus {
    /// NVIDIA の GPU が見えるか（CUDA 版はこれが無いと動かない）。
    pub nvidia: bool,
    pub gpu_name: String,
    pub vram_total_mb: u64,
    pub engine_ready: bool,
    pub engine_tag: String,
    pub model_ready: bool,
    /// 画面に出すモデルの名前（ファイル名）。
    pub model_file: String,
    pub model_bytes: u64,
    /// いま準備（取り寄せ）をしているか。
    pub preparing: bool,
}

async fn status_now() -> LocalGpuStatus {
    let gpu = crate::gpu::probe_gpu().await.ok();
    let name = gpu.as_ref().map(|g| g.name.clone()).unwrap_or_default();
    let mp = model_path();
    LocalGpuStatus {
        nvidia: name.to_ascii_uppercase().contains("NVIDIA") || name.to_ascii_uppercase().contains("GEFORCE")
            || name.to_ascii_uppercase().contains("RTX"),
        gpu_name: name,
        vram_total_mb: gpu.as_ref().map(|g| g.vram_total_mb).unwrap_or(0),
        engine_ready: engine_exe().is_some(),
        engine_tag: ENGINE_TAG.to_string(),
        model_ready: mp.is_file(),
        model_file: MODEL_FILE.to_string(),
        model_bytes: std::fs::metadata(&mp).map(|m| m.len()).unwrap_or(0),
        preparing: PREPARING.load(Ordering::Relaxed),
    }
}

#[tauri::command]
pub async fn local_gpu_status() -> LocalGpuStatus {
    status_now().await
}

/// 推論機（CUDA 版）と推奨モデルを取り寄せる。どちらも在れば何もしない（落とし直さない）。
/// 進捗は hf.rs と同じ `download-progress` で出す（画面は同じ受け口で読める）。
#[tauri::command]
pub async fn local_gpu_prepare(app: AppHandle) -> Result<LocalGpuStatus, String> {
    if PREPARING.swap(true, Ordering::SeqCst) {
        return Err("準備はすでに進んでいます".into());
    }
    let r = prepare(&app).await;
    PREPARING.store(false, Ordering::SeqCst);
    r?;
    Ok(status_now().await)
}

async fn prepare(app: &AppHandle) -> Result<(), String> {
    if !cfg!(windows) {
        return Err("この PC の GPU で動かす準備は Windows 版だけです".into());
    }
    ensure_engine(app).await?;
    if !model_path().is_file() {
        crate::hf::hf_download(app.clone(), MODEL_REPO.to_string(), MODEL_FILE.to_string()).await?;
    }
    Ok(())
}

/// 推論機（llama.cpp の CUDA 版）が無ければ取り寄せて置く（声で話す係も同じ推論機を使う）。
pub async fn ensure_engine(app: &AppHandle) -> Result<(), String> {
    if engine_exe().is_some() {
        return Ok(());
    }
    let dl = crate::app_root().join("dl");
    tokio::fs::create_dir_all(&dl).await.map_err(|e| format!("{} を作れません: {e}", dl.display()))?;
    let mut zips = Vec::new();
    for a in &ENGINE_ZIPS {
        zips.push(download_to(app, a.url, a.sha256, &dl, a.file, &format!("llama.cpp {ENGINE_TAG}")).await?);
    }
    extract_engine(&zips)?;
    for z in &zips {
        let _ = std::fs::remove_file(z);
    }
    Ok(())
}

/// 1 本を `dir/file` へ落として SHA256 を照合する。照合が合わなければ消して誤りを返す（壊れた物を置かない）。
/// `label` は進み具合の表示に出す名前。
pub async fn download_to(app: &AppHandle, url: &str, sha256: &str, dir: &Path, file: &str, label: &str)
    -> Result<PathBuf, String> {
    let a = Asset2 { url, sha256, file, label };
    let dest = dir.join(a.file);
    if let Ok(true) = sha256_file(&dest).map(|h| h.eq_ignore_ascii_case(a.sha256)) {
        return Ok(dest);
    }
    let part = dir.join(format!("{}.part", a.file));
    let cli = reqwest::Client::builder()
        .user_agent("blackwall-console/0.1")
        .connect_timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| format!("通信の準備ができません: {e}"))?;
    let mut last = String::new();
    for attempt in 1..=3u64 {
        match fetch_once(app, &cli, &a, &part).await {
            Ok(digest) if digest.eq_ignore_ascii_case(a.sha256) => {
                let _ = std::fs::remove_file(&dest);
                std::fs::rename(&part, &dest).map_err(|e| format!("{} を置けません: {e}", dest.display()))?;
                return Ok(dest);
            }
            Ok(digest) => {
                let _ = std::fs::remove_file(&part);
                last = format!("{} の内容が壊れています（SHA256 が一致しません: 期待 {} / 実際 {digest}）", a.file, a.sha256);
            }
            Err(e) => last = e,
        }
        tokio::time::sleep(Duration::from_secs(attempt * 2)).await;
    }
    Err(format!("{} をダウンロードできません: {last}", a.file))
}

struct Asset2<'a> {
    url: &'a str,
    sha256: &'a str,
    file: &'a str,
    label: &'a str,
}

async fn fetch_once(app: &AppHandle, cli: &reqwest::Client, a: &Asset2<'_>, part: &Path) -> Result<String, String> {
    let mut resp = cli.get(a.url).send().await.map_err(|e| format!("転送を開始できません: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status().as_u16()));
    }
    let total = resp.content_length();
    let mut f = tokio::fs::File::create(part).await.map_err(|e| format!("{} を作れません: {e}", part.display()))?;
    let mut h = Sha256::new();
    let mut got: u64 = 0;
    let started = Instant::now();
    let mut last_emit = Instant::now() - Duration::from_secs(1);
    while let Some(chunk) = resp.chunk().await.map_err(|e| format!("転送が切れました: {e}"))? {
        h.update(&chunk);
        f.write_all(&chunk).await.map_err(|e| format!("書けません: {e}"))?;
        got += chunk.len() as u64;
        if last_emit.elapsed() >= Duration::from_millis(250) {
            last_emit = Instant::now();
            let bps = got as f64 / started.elapsed().as_secs_f64().max(0.001);
            let _ = app.emit("download-progress", serde_json::json!({
                "path": a.file, "repo": a.label, "received": got,
                "total": total, "bytes_per_sec": bps,
            }));
        }
    }
    f.flush().await.map_err(|e| format!("書けません: {e}"))?;
    if let Some(t) = total {
        if got != t {
            return Err(format!("大きさが合いません（受信 {got} / 期待 {t}）"));
        }
    }
    Ok(hex(&h.finalize()))
}

fn sha256_file(p: &Path) -> Result<String, String> {
    use std::io::Read;
    let mut f = std::fs::File::open(p).map_err(|e| e.to_string())?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex(&h.finalize()))
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// zip を一時の場所へ展開し、llama-server が在るのを確かめてから engine-cuda に入れ替える
/// （途中で失敗しても、半端な engine-cuda を残さない＝次の起動で使われない）。
fn extract_engine(zips: &[PathBuf]) -> Result<(), String> {
    let dst = engine_dir();
    let tmp = crate::app_root().join("engine-cuda.tmp");
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).map_err(|e| format!("{} を作れません: {e}", tmp.display()))?;
    for z in zips {
        // Windows 10 以降の tar.exe は zip を展開できる（追加の部品が要らない）。
        let mut cmd = std::process::Command::new("tar");
        cmd.arg("-xf").arg(z).arg("-C").arg(&tmp);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x0800_0000);
        }
        let out = cmd.output().map_err(|e| format!("展開を始められません: {e}"))?;
        if !out.status.success() {
            return Err(format!("{} を展開できません: {}", z.display(), String::from_utf8_lossy(&out.stderr).trim()));
        }
    }
    if !tmp.join(SERVER_BIN).is_file() {
        return Err(format!("展開した中に {SERVER_BIN} が見つかりません（{}）", tmp.display()));
    }
    let _ = std::fs::remove_dir_all(&dst);
    std::fs::rename(&tmp, &dst).map_err(|e| format!("{} に置けません: {e}", dst.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_is_lowercase_and_padded() {
        assert_eq!(hex(&[0x0a, 0xff]), "0aff");
    }

    #[test]
    fn engine_assets_are_pinned() {
        for a in &ENGINE_ZIPS {
            assert!(a.url.contains(ENGINE_TAG) || a.url.contains("cudart"), "{}", a.url);
            assert_eq!(a.sha256.len(), 64);
            assert!(a.url.ends_with(a.file));
        }
    }
}
