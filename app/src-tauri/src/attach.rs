//! チャットへのファイルの添付（2026-10-09 オーナー「チャット欄に直接ファイルを送れるような仕組みが欲しい。
//! 送ったものはワークスペース内の .blackwall 内に専用のフォルダを作成しておいて保存しておくとか、
//! Blackwall が読み込めるようにしておくとか」）。
//!
//! 置き場: `<作業フォルダ>/.blackwall/attachments/<YYYYMMDD-HHMMSS>/<名前>`（送るたびに 1 つ）。
//!   作業フォルダの中に置くので、エージェントは Read・Doc・Listen でそのまま読める（外を読む許可が要らない）。
//! 入口は 3 つ: 選ぶ（`attach_pick`）・窓へ落とす（画面が Tauri の drag-drop の道のりを受けて `attach_save`）・
//!   貼り付ける（画面がバイト列を base64 で `attach_bytes`）。どれも最後は送るときに `attach_save` で写す。
//! ★元のファイルは動かさない・消さない（写すだけ）。名前は置き場の中に閉じ込める（`..` や区切りを残さない）。

use serde::Serialize;
use std::path::{Path, PathBuf};
use tauri::AppHandle;

/// 1 回に添付できる数・1 つの大きさの上限。
const MAX_FILES: usize = 20;
const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024 * 1024; // 2 GiB（写すだけなので大きくてもよいが、際限はつける）
const MAX_PASTE_BYTES: usize = 64 * 1024 * 1024; // 貼り付けは base64 で画面から渡るので小さめ

#[derive(Serialize, Clone)]
pub struct Attached {
    /// 置いた名前（重なったら「名前 (2).拡張子」）
    pub name: String,
    /// 作業フォルダからの相対の道のり（区切りは `/`）。エージェントへはこれを渡す。
    pub rel: String,
    /// 絶対の道のり
    pub path: String,
    pub size: u64,
}

fn ws_root(workspace: &str) -> Result<PathBuf, String> {
    let w = workspace.trim();
    if w.is_empty() {
        return Err("先に作業フォルダを選んでください（添付は作業フォルダの中に保存します）".into());
    }
    let c = std::fs::canonicalize(w).map_err(|e| format!("作業フォルダが見つかりません（{w}）: {e}"))?;
    if !c.is_dir() {
        return Err(format!("作業フォルダではありません: {w}"));
    }
    Ok(c)
}

fn attach_root(root: &Path) -> PathBuf {
    // 置き場の名前は規則で選ぶ（.laps・旧い .blackwall が在ればそのまま＝Python の paths.workspace_dir と同じ）
    crate::paths::workspace_dir(root).join("attachments")
}

/// 送るたびの入れ物（現地時刻の秒まで）。同じ秒に重なったら後ろに番号を付ける。
fn new_batch_dir(root: &Path) -> Result<PathBuf, String> {
    let now = time::OffsetDateTime::now_utc();
    let now = time::UtcOffset::current_local_offset().map(|o| now.to_offset(o)).unwrap_or(now);
    let stamp = format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}",
        now.year(), u8::from(now.month()), now.day(), now.hour(), now.minute(), now.second()
    );
    let base = attach_root(root);
    let mut dir = base.join(&stamp);
    let mut n = 2;
    while dir.exists() {
        dir = base.join(format!("{stamp}-{n}"));
        n += 1;
    }
    std::fs::create_dir_all(&dir).map_err(|e| format!("保存先を作れません（{}）: {e}", dir.display()))?;
    Ok(dir)
}

/// 置き場の中に閉じ込めた名前（区切り・制御文字・Windows で使えない字を `_` に、予約名には `_` を前置）。
fn safe_name(raw: &str) -> String {
    let base = raw.rsplit(['/', '\\']).next().unwrap_or("");
    let mut s: String = base
        .chars()
        .map(|c| if c.is_control() || "<>:\"/\\|?*".contains(c) { '_' } else { c })
        .collect();
    while s.ends_with('.') || s.ends_with(' ') {
        s.pop();
    }
    let s = s.trim_start_matches(' ').to_string();
    let mut s = if s.is_empty() || s == "." || s == ".." { "file".to_string() } else { s };
    let stem = s.split('.').next().unwrap_or("").to_ascii_uppercase();
    let reserved = ["CON", "PRN", "AUX", "NUL"].contains(&stem.as_str())
        || ((stem.starts_with("COM") || stem.starts_with("LPT")) && stem.len() == 4
            && stem.as_bytes()[3].is_ascii_digit());
    if reserved {
        s = format!("_{s}");
    }
    if s.chars().count() > 120 {
        let (stem, ext) = match s.rfind('.') {
            Some(i) if s.len() - i <= 12 => (s[..i].to_string(), s[i..].to_string()),
            _ => (s.clone(), String::new()),
        };
        s = stem.chars().take(120 - ext.chars().count()).collect::<String>() + &ext;
    }
    s
}

fn unique_in(dir: &Path, name: &str) -> PathBuf {
    let p = dir.join(name);
    if !p.exists() {
        return p;
    }
    let (stem, ext) = match name.rfind('.') {
        Some(i) if i > 0 => (&name[..i], &name[i..]),
        _ => (name, ""),
    };
    (2..).map(|n| dir.join(format!("{stem} ({n}){ext}"))).find(|p| !p.exists()).unwrap()
}

fn rel_of(root: &Path, p: &Path) -> String {
    p.strip_prefix(root).unwrap_or(p).to_string_lossy().replace('\\', "/")
}

fn entry(root: &Path, p: &Path) -> Attached {
    Attached {
        name: p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
        rel: rel_of(root, p),
        path: p.to_string_lossy().into_owned(),
        size: std::fs::metadata(p).map(|m| m.len()).unwrap_or(0),
    }
}

/// 標準の base64（`=` の埋め・改行や空白は読み飛ばす）を復号する。依存を増やさないための最小の実装。
pub(crate) fn b64_decode(s: &str) -> Result<Vec<u8>, String> {
    let val = |c: u8| -> Option<u32> {
        match c {
            b'A'..=b'Z' => Some((c - b'A') as u32),
            b'a'..=b'z' => Some((c - b'a' + 26) as u32),
            b'0'..=b'9' => Some((c - b'0' + 52) as u32),
            b'+' | b'-' => Some(62),
            b'/' | b'_' => Some(63),
            _ => None,
        }
    };
    let body = s.split_once("base64,").map(|(_, b)| b).unwrap_or(s); // data: URL の頭は外す
    let mut out = Vec::with_capacity(body.len() / 4 * 3);
    let (mut acc, mut bits) = (0u32, 0u32);
    for &c in body.as_bytes() {
        if c == b'=' {
            break;
        }
        if c.is_ascii_whitespace() {
            continue;
        }
        let v = val(c).ok_or_else(|| "貼り付けたデータを読めません（base64 ではありません）".to_string())?;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// commands
// ---------------------------------------------------------------------------

/// ファイルを選ぶ（複数可）。写すのは送るとき（`attach_save`）。取り消しは空の一覧。
#[tauri::command]
pub async fn attach_pick(app: AppHandle) -> Result<Vec<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let picked = tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().set_title("チャットに添付するファイルを選ぶ").blocking_pick_files()
    })
    .await
    .map_err(|e| format!("ファイル選択を実行できません: {e}"))?;
    Ok(picked
        .unwrap_or_default()
        .into_iter()
        .filter_map(|f| f.into_path().ok())
        .map(|p| p.to_string_lossy().into_owned())
        .collect())
}

/// 選んだ・落としたファイルを `<作業フォルダ>/.blackwall/attachments/<日時>/` へ写す（元は触らない）。
/// 既に置き場の中にあるもの（貼り付けで先に置いたもの）は写さずにそのまま返す。
#[tauri::command]
pub async fn attach_save(workspace: String, paths: Vec<String>) -> Result<Vec<Attached>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = ws_root(&workspace)?;
        if paths.is_empty() {
            return Ok(vec![]);
        }
        if paths.len() > MAX_FILES {
            return Err(format!("一度に添付できるのは {MAX_FILES} 個までです（{} 個）", paths.len()));
        }
        let store = attach_root(&root);
        let mut dir: Option<PathBuf> = None;
        let mut out = Vec::new();
        for raw in &paths {
            let src = PathBuf::from(raw);
            let meta = std::fs::metadata(&src).map_err(|e| format!("読めません（{raw}）: {e}"))?;
            if meta.is_dir() {
                return Err(format!("フォルダは添付できません（{raw}）。作業フォルダの中に置くか、zip にまとめてください"));
            }
            if meta.len() > MAX_FILE_BYTES {
                return Err(format!("大きすぎます（{raw}・{} MB）", meta.len() / 1_048_576));
            }
            let canon = std::fs::canonicalize(&src).unwrap_or(src.clone());
            if canon.starts_with(&store) {
                out.push(entry(&root, &canon)); // 貼り付けで先に置いたもの
                continue;
            }
            if dir.is_none() {
                dir = Some(new_batch_dir(&root)?);
            }
            let d = dir.as_ref().unwrap();
            let dst = unique_in(d, &safe_name(&src.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()));
            std::fs::copy(&src, &dst).map_err(|e| format!("写せません（{raw} → {}）: {e}", dst.display()))?;
            out.push(entry(&root, &dst));
        }
        Ok(out)
    })
    .await
    .map_err(|e| format!("添付を保存できません: {e}"))?
}

/// 貼り付けたデータ（画面の FileReader が作った base64）を置き場へ保存する。
#[tauri::command]
pub async fn attach_bytes(workspace: String, name: String, data: String) -> Result<Attached, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let root = ws_root(&workspace)?;
        if data.len() / 4 * 3 > MAX_PASTE_BYTES + 3 {
            return Err(format!("貼り付けたデータが大きすぎます（上限 {} MB）。ファイルとして添付してください", MAX_PASTE_BYTES / 1_048_576));
        }
        let bytes = b64_decode(&data)?;
        let dir = new_batch_dir(&root)?;
        let dst = unique_in(&dir, &safe_name(if name.trim().is_empty() { "pasted.bin" } else { &name }));
        std::fs::write(&dst, &bytes).map_err(|e| format!("保存できません（{}）: {e}", dst.display()))?;
        Ok(entry(&root, &dst))
    })
    .await
    .map_err(|e| format!("貼り付けを保存できません: {e}"))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_stay_inside() {
        assert_eq!(safe_name("../../etc/passwd"), "passwd");
        assert_eq!(safe_name("C:\\a\\b\\report.pdf"), "report.pdf");
        assert_eq!(safe_name("con.txt"), "_con.txt");
        assert_eq!(safe_name("a<b>c?.png"), "a_b_c_.png");
        assert_eq!(safe_name(".."), "file");
    }

    #[test]
    fn b64_roundtrip() {
        assert_eq!(b64_decode("aGVsbG8=").unwrap(), b"hello");
        assert_eq!(b64_decode("data:image/png;base64,aGk=").unwrap(), b"hi");
    }
}
