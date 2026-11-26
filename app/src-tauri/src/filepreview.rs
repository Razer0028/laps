//! Blackwall が作ったファイルを右の欄（Files）で見る。
//!
//! 2026-10-18 オーナー「Blackwall が作成した MD ファイルや画像ファイルなどをアプリから見れるようにしたいです。
//! Source のところに表示するようにすればいいですかね」。画面（app.js の files*）が会話の道具の行（Write・Edit・Doc）から
//! ファイルの場所を拾い、ここで中身を読む。
//!
//! ★「画面から任意のパスを開かせる口」を作らない（hf.rs の open_models_dir と同じ考え）。そのため:
//!   - 読めるのは作業フォルダかホームの下だけ。鍵らしい場所（.ssh・.gnupg・id_rsa など）は読まない。
//!   - 中身を返すのは決まった種類だけ（文字・画像・PDF）。文書（docx など）は「開く」だけ。
//!   - 既定のアプリで「開く」のは文書・画像・文字の種類だけ。台本・実行ファイル（.bat・.ps1・.exe・.html など）は開かない
//!     （エージェントが書いたファイルを押しただけで動き出すのを避ける）。フォルダで見せるのはどの種類でもよい。

use base64::Engine;
use serde::Serialize;
use std::path::{Path, PathBuf};

const MAX_TEXT: u64 = 2 * 1024 * 1024; // 文字はこれより後ろを切る
const MAX_BIN: u64 = 25 * 1024 * 1024; // 画像・PDF はこれより大きいと返さない

#[derive(Serialize, Debug)]
pub struct Preview {
    pub path: String,
    pub name: String,
    /// "markdown" | "text" | "html" | "csv" | "image" | "pdf" | "document" | "other"
    pub kind: String,
    pub mime: String,
    pub size: u64,
    pub modified: Option<u64>,
    pub text: Option<String>,
    /// base64（画像・PDF）
    pub data: Option<String>,
    pub truncated: bool,
    pub can_open: bool,
}

const TEXT_EXT: &[&str] = &[
    "txt", "log", "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "conf", "xml", "css", "js", "mjs", "cjs", "ts",
    "tsx", "jsx", "py", "rs", "go", "java", "kt", "c", "h", "cpp", "hpp", "cc", "cs", "rb", "php", "sh", "ps1", "psm1",
    "bat", "cmd", "lua", "sql", "gd", "shader", "glsl", "hlsl", "vue", "svelte", "env.example", "gitignore", "dockerfile",
    "makefile", "r", "swift", "dart", "scala", "pl", "srt", "vtt",
];
/// 既定のアプリで開いてよい種類（押しただけで何かが動き出さないもの）。
const OPEN_OK: &[&str] = &[
    "md", "markdown", "txt", "log", "csv", "tsv", "json", "png", "jpg", "jpeg", "gif", "webp", "bmp", "pdf", "docx",
    "xlsx", "pptx", "doc", "xls", "ppt", "odt", "ods", "odp", "mp3", "wav", "ogg", "flac", "mp4", "webm", "mkv",
];

fn ext_of(p: &Path) -> String {
    p.extension().map(|e| e.to_string_lossy().to_ascii_lowercase()).unwrap_or_default()
}

/// (種類, MIME)。返せない種類は ("other", …)。
pub fn kind_of(p: &Path) -> (&'static str, &'static str) {
    let e = ext_of(p);
    match e.as_str() {
        "md" | "markdown" => ("markdown", "text/markdown"),
        "csv" => ("csv", "text/csv"),
        "tsv" => ("csv", "text/tab-separated-values"),
        "html" | "htm" => ("html", "text/html"),
        "png" => ("image", "image/png"),
        "jpg" | "jpeg" => ("image", "image/jpeg"),
        "gif" => ("image", "image/gif"),
        "webp" => ("image", "image/webp"),
        "bmp" => ("image", "image/bmp"),
        "svg" => ("image", "image/svg+xml"),
        "ico" => ("image", "image/x-icon"),
        "pdf" => ("pdf", "application/pdf"),
        "docx" | "xlsx" | "pptx" | "doc" | "xls" | "ppt" | "odt" | "ods" | "odp" => ("document", "application/octet-stream"),
        _ => {
            let name = p.file_name().map(|n| n.to_string_lossy().to_ascii_lowercase()).unwrap_or_default();
            if TEXT_EXT.contains(&e.as_str()) || TEXT_EXT.contains(&name.as_str()) {
                ("text", "text/plain")
            } else {
                ("other", "application/octet-stream")
            }
        }
    }
}

pub fn can_open(p: &Path) -> bool {
    OPEN_OK.contains(&ext_of(p).as_str())
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
}

/// 鍵や資格情報がありそうな場所は読まない。
fn looks_secret(p: &Path) -> bool {
    let low = p.to_string_lossy().to_ascii_lowercase().replace('\\', "/");
    let name = p.file_name().map(|n| n.to_string_lossy().to_ascii_lowercase()).unwrap_or_default();
    low.contains("/.ssh/") || low.contains("/.gnupg/") || low.contains("/.aws/") || low.contains("/.config/gcloud/")
        || name.starts_with("id_rsa") || name.starts_with("id_ed25519") || name.starts_with("id_ecdsa")
        || name.ends_with(".pem") || name.ends_with(".key") || name.ends_with(".pfx") || name.ends_with(".p12")
        || name == "credentials" || name == ".env" || name.contains("secret")
}

/// 読んでよい場所か（作業フォルダかホームの下・鍵らしい場所は除く）。返すのは正規化した場所。
pub fn check_path(path: &str, workspace: &str) -> Result<PathBuf, String> {
    let raw = path.trim();
    if raw.is_empty() {
        return Err("No path given".into());
    }
    let mut p = PathBuf::from(raw);
    if p.is_relative() {
        if workspace.trim().is_empty() {
            return Err(format!("No workspace is set, so the relative path {raw} can't be opened"));
        }
        p = Path::new(workspace.trim()).join(p);
    }
    let canon = std::fs::canonicalize(&p).map_err(|_| format!("Not found on this PC: {}", p.display()))?;
    let mut roots: Vec<PathBuf> = Vec::new();
    if !workspace.trim().is_empty() {
        if let Ok(w) = std::fs::canonicalize(workspace.trim()) {
            roots.push(w);
        }
    }
    if let Some(h) = home_dir().and_then(|h| std::fs::canonicalize(h).ok()) {
        roots.push(h);
    }
    if !roots.iter().any(|r| canon.starts_with(r)) {
        return Err("Only files in the workspace or your home folder can be opened".into());
    }
    if looks_secret(&canon) {
        return Err("This looks like a key or credentials file, so it is not opened".into());
    }
    Ok(canon)
}

fn shown(p: &Path) -> String {
    // Windows の正規化は \\?\ を付ける。画面にはそのまま出さない。
    let s = p.display().to_string();
    s.strip_prefix(r"\\?\").map(|x| x.to_string()).unwrap_or(s)
}

pub fn preview_of(path: &str, workspace: &str) -> Result<Preview, String> {
    let p = check_path(path, workspace)?;
    let meta = std::fs::metadata(&p).map_err(|e| format!("Can't read: {e}"))?;
    if meta.is_dir() {
        return Err("This is a folder".into());
    }
    let size = meta.len();
    let modified = meta.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs());
    let (kind, mime) = kind_of(&p);
    let mut out = Preview {
        path: shown(&p),
        name: p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
        kind: kind.into(),
        mime: mime.into(),
        size,
        modified,
        text: None,
        data: None,
        truncated: false,
        can_open: can_open(&p),
    };
    match kind {
        "markdown" | "text" | "html" | "csv" => {
            use std::io::Read;
            let f = std::fs::File::open(&p).map_err(|e| format!("Can't read: {e}"))?;
            let mut buf = Vec::new();
            f.take(MAX_TEXT).read_to_end(&mut buf).map_err(|e| format!("Can't read: {e}"))?;
            out.truncated = size > MAX_TEXT;
            out.text = Some(String::from_utf8_lossy(&buf).into_owned());
        }
        "image" | "pdf" => {
            if size > MAX_BIN {
                return Err(format!("Too large to preview ({} MB). Use Open to view it in its app", size / 1_048_576));
            }
            let bytes = std::fs::read(&p).map_err(|e| format!("Can't read: {e}"))?;
            out.data = Some(base64::engine::general_purpose::STANDARD.encode(bytes));
        }
        _ => {}
    }
    Ok(out)
}

#[tauri::command]
pub async fn file_preview(path: String, workspace: String) -> Result<Preview, String> {
    tauri::async_runtime::spawn_blocking(move || preview_of(&path, &workspace))
        .await
        .map_err(|e| format!("Can't read the file: {e}"))?
}

/// エクスプローラでファイルの場所を開く（選んだ状態で）。
#[tauri::command]
pub async fn file_reveal(path: String, workspace: String) -> Result<(), String> {
    let p = check_path(&path, &workspace)?;
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        // explorer.exe は成功時も 1 を返すことがあるので、終了コードは見ない。
        std::process::Command::new("explorer.exe")
            .arg(format!("/select,{}", shown(&p)))
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("Can't open Explorer: {e}"))?;
    }
    #[cfg(not(windows))]
    {
        let _ = &p; // Linux で型検査を通すための分岐。開く動作はしない。
    }
    Ok(())
}

/// 既定のアプリで開く（文書・画像・文字の種類だけ）。
#[tauri::command]
pub async fn file_open(path: String, workspace: String) -> Result<(), String> {
    let p = check_path(&path, &workspace)?;
    if !can_open(&p) {
        return Err("This file type is not opened from the app (so scripts and programs don't run). Use Folder instead".into());
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        std::process::Command::new("explorer.exe")
            .arg(shown(&p))
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("Can't open: {e}"))?;
    }
    #[cfg(not(windows))]
    {
        let _ = &p;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// 別の窓（画面分離）
// ---------------------------------------------------------------------------
// 2026-10-18 オーナー「画面分離にも対応させたい」→「両方」（同じ窓で左右に並べる＋別の窓に切り離す）。
// 別の窓（label "viewer"・viewer.html）は**描かない**: メインの窓が描いた中身（HTML・PDF は base64）を受け取って出すだけ。
// Markdown の描き方を 2 か所に持たないため。渡す物はここに 1 つだけ預け、窓が開いたら viewer_pending で取りに来る
// （窓が出来上がる前に送った事象は届かないので、預けて取りに来させる）。開いているときは事象 viewer-show で送る。

static PENDING: std::sync::Mutex<Option<serde_json::Value>> = std::sync::Mutex::new(None);

#[tauri::command]
pub async fn viewer_open(app: tauri::AppHandle, payload: serde_json::Value) -> Result<(), String> {
    use tauri::{Emitter, Manager};
    if let Ok(mut g) = PENDING.lock() {
        *g = Some(payload.clone());
    }
    if let Some(w) = app.get_webview_window("viewer") {
        let _ = app.emit_to("viewer", "viewer-show", payload);
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        return Ok(());
    }
    // ★非同期の命令の中で作る（同期の命令で窓を作ると Windows で固まる＝Tauri の注意書き）。
    tauri::WebviewWindowBuilder::new(&app, "viewer", tauri::WebviewUrl::App("viewer.html".into()))
        .title("L.A.P.S — Files")
        .inner_size(980.0, 760.0)
        .min_inner_size(480.0, 360.0)
        .build()
        .map_err(|e| format!("Can't open the window: {e}"))?;
    Ok(())
}

/// 別の窓が開いたときに、預けてある中身を取りに来る。
#[tauri::command]
pub fn viewer_pending() -> Option<serde_json::Value> {
    PENDING.lock().ok().and_then(|g| g.clone())
}

// ── 道具が撮った写真の控え（2026-10-30）─────────────────────────────────────────
// オーナー「chrome やデスクトップ操作ツールをモデルが使っている時の ui を claude に寄せる」。本体（oc/loop._save_shot）が
// Browser・Desktop などの写真を縮めて 会話のフォルダの shots/ に置き、道具の要約に場所（summary.shot）を載せる。
// 画面はそれを道具の行に小さく並べる。★読めるのはアプリの根の下の shots/ にある写真だけ（画面から任意のパスを読ませない）。
const MAX_SHOT: u64 = 12 * 1024 * 1024;   // 2026-11-20 道具 Image（engine/tools/image.py）と揃える

pub fn shot_of(path: &str, root: &Path) -> Result<String, String> {
    let canon = std::fs::canonicalize(path.trim()).map_err(|_| "Not found".to_string())?;
    let base = std::fs::canonicalize(root).map_err(|_| "No app folder".to_string())?;
    let in_shots = canon.parent().and_then(|d| d.file_name()).map(|n| n == "shots").unwrap_or(false);
    if !canon.starts_with(&base) || !in_shots {
        return Err("Only pictures the agent took can be shown here".into());
    }
    let mime = match canon.extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()).as_deref() {
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("png") => "image/png",
        Some("webp") => "image/webp",
        Some("gif") => "image/gif",
        _ => return Err("Not a picture".into()),
    };
    let size = std::fs::metadata(&canon).map_err(|e| format!("Can't read: {e}"))?.len();
    if size > MAX_SHOT {
        return Err("Too large".into());
    }
    let bytes = std::fs::read(&canon).map_err(|e| format!("Can't read: {e}"))?;
    Ok(format!("data:{mime};base64,{}", base64::engine::general_purpose::STANDARD.encode(bytes)))
}

#[tauri::command]
pub async fn shot_data(path: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || shot_of(&path, &crate::app_root()))
        .await
        .map_err(|e| e.to_string())?
}

/// ほかの機械の会話の画像を中継越しに取り寄せる（2026-11-27 オーナー「リモートセッションの場合モデルが提示した画像ファイルとかが
/// 見えない」）。向こうのゲートウェイの shot.get が、会話の shots/ と、その会話の作業フォルダの中の画像だけを返す（決めるのは向こう）。
/// 返り値は data: URL（shot_data と同じ形）。
#[tauri::command]
pub async fn shot_remote(device: String, session: String, path: String) -> Result<String, String> {
    if device.trim().is_empty() || path.trim().is_empty() {
        return Err("マシンかパスが空です".into());
    }
    let Some(relay) = crate::config_value("remote_relay") else {
        return Err("中継サーバー（remote_relay）が設定されていません".into());
    };
    let (code, out, err) = crate::sidecar::run_python_module(
        "laps.ctlclient", &["shot", device.trim(), session.trim(), path.trim()],
        &[("LAPS_RELAY_URL", relay)], 90).await?;
    let url = out.trim();
    if code == 0 && url.starts_with("data:image/") {
        return Ok(url.to_string());
    }
    let msg = err.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("").trim_start_matches("error: ").to_string();
    Err(if msg.is_empty() { "画像を取得できませんでした".into() } else { msg })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shots_only_under_the_app_folder() {
        let root = std::env::temp_dir().join(format!("bw-shot-{}", std::process::id()));
        let shots = root.join("agent").join("sessions").join("s1").join("shots");
        std::fs::create_dir_all(&shots).unwrap();
        std::fs::write(shots.join("c1.jpg"), b"\xff\xd8\xff").unwrap();
        std::fs::write(shots.join("c1.txt"), b"x").unwrap();
        std::fs::write(root.join("agent").join("sessions").join("s1").join("a.jpg"), b"x").unwrap();
        let ok = shot_of(shots.join("c1.jpg").to_str().unwrap(), &root).unwrap();
        assert!(ok.starts_with("data:image/jpeg;base64,"));
        assert!(shot_of(shots.join("c1.txt").to_str().unwrap(), &root).is_err());
        assert!(shot_of(root.join("agent/sessions/s1/a.jpg").to_str().unwrap(), &root).is_err());
        let other = std::env::temp_dir().join(format!("bw-shot-out-{}", std::process::id())).join("shots");
        std::fs::create_dir_all(&other).unwrap();
        std::fs::write(other.join("c.jpg"), b"x").unwrap();
        assert!(shot_of(other.join("c.jpg").to_str().unwrap(), &root).is_err());
        let _ = std::fs::remove_dir_all(&root);
        let _ = std::fs::remove_dir_all(other.parent().unwrap());
    }

    #[test]
    fn kinds() {
        assert_eq!(kind_of(Path::new("a/README.md")).0, "markdown");
        assert_eq!(kind_of(Path::new("shot.PNG")).0, "image");
        assert_eq!(kind_of(Path::new("x.pdf")).0, "pdf");
        assert_eq!(kind_of(Path::new("run.ps1")).0, "text");
        assert_eq!(kind_of(Path::new("a.docx")).0, "document");
        assert_eq!(kind_of(Path::new("a.exe")).0, "other");
        assert_eq!(kind_of(Path::new("Dockerfile")).0, "text");
    }

    #[test]
    fn open_only_safe_kinds() {
        assert!(can_open(Path::new("report.md")));
        assert!(can_open(Path::new("deck.pptx")));
        for bad in ["run.bat", "x.ps1", "a.exe", "page.html", "s.js", "s.vbs", "a.lnk"] {
            assert!(!can_open(Path::new(bad)), "{bad} は開いてはいけない");
        }
    }

    #[test]
    fn reads_inside_workspace_and_refuses_outside_or_secret() {
        let ws = std::env::temp_dir().join(format!("bw-fp-{}", std::process::id()));
        std::fs::create_dir_all(ws.join("docs")).unwrap();
        std::fs::write(ws.join("docs/r.md"), "# 見出し\n本文").unwrap();
        std::fs::write(ws.join("id_rsa"), "secret").unwrap();
        let w = ws.to_string_lossy().into_owned();
        let pv = preview_of("docs/r.md", &w).unwrap();
        assert_eq!(pv.kind, "markdown");
        assert!(pv.text.unwrap().contains("見出し"));
        assert!(pv.can_open);
        assert!(preview_of("id_rsa", &w).is_err());
        assert!(preview_of("nope.md", &w).is_err());
        // ホームでも作業フォルダでもない場所（/ の直下など）は断る
        #[cfg(unix)]
        if std::fs::canonicalize("/etc/hostname").map(|p| !p.starts_with(home_dir().unwrap_or_default())).unwrap_or(false) {
            assert!(preview_of("/etc/hostname", &w).is_err());
        }
        std::fs::remove_dir_all(&ws).ok();
    }
}
