//! 作業ディレクトリ（ワークスペース）の管理と、そこに閉じたファイル操作。
//!
//! 目的: claudecode のように「フォルダを1つ選ぶと、その中を読み書きしながら答える」を成立させる。
//! ここが提供するのは**器**だけで、いつ読むか・いつ書くかを決めるのは上の層（ツール呼び出し）。
//!
//! ★ このモジュールの本体は封じ込め（[`resolve_in_ws`]）である
//! ---------------------------------------------------------------------------
//! 相対パスは最終的にモデルの出力から来る。つまり**信用できない文字列がパスになる**。
//! そこで判定は文字列の見た目ではなく、**実体を canonicalize してから接頭辞比較**で行う。
//! 文字列だけで弾くと、シンボリックリンク・ジャンクション・8.3 形式の別名・大文字小文字差の
//! どれか1つで破られる（どれも「見た目は中、実体は外」を作れる）。
//!
//! Windows 特有の抜け道への対処:
//!   - 8.3 短縮名（`PROGRA~1`）… `canonicalize` が長い名前へ展開するので、比較の時点で消える。
//!   - 大文字小文字     … ファイルシステムが区別しないので、比較も区別しない（下記 [`component_eq`]）。
//!   - UNC / ドライブ名 … 相対パスとして受け付けない（[`split_relative`]）。
//!   - 代替データストリーム（`a.txt:secret`）… `:` を含む名前を拒否。
//!   - 予約デバイス名（`CON` `NUL` `COM1`…）… 開くとファイルシステムの外へ出るので拒否。
//!   - 末尾のドット / 空白 … OS が黙って落とすため、要求した名前と別の場所に着地する。拒否。
//!
//! ★ 残る穴を正直に書いておく: 検査してから開くまでの間に相手を差し替える競合（TOCTOU）は
//! ここでは塞げない。ハンドルを握ったまま検査する API が要る。実害を出すには作業ディレクトリ内に
//! 既に書き込み権が要るので、当面は許容する。
//!
//! ★ 読み出した中身は「情報」であって「指示」ではない（他の取得系と同じ規律）。
//! ファイルには誰が書いたか分からない文字列が入る。このモジュールは中身を解釈して分岐する処理を
//! 意図的に持たない——読んで返すだけ。上の層も同じ扱いをすること。

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::AppHandle;

/// 封じ込め（[`resolve_in_ws`]）と一覧・読み・書き（[`list_in`]/[`read_text_in`]/
/// [`write_text_in`]）の実体。★2026-09-05（r4/tests-never-run）にここへ抜いた:
/// このファイルは `tauri::AppHandle` を参照するのでホスト（Linux）では
/// `cargo test` がリンクできず（GTK 依存）、パストラバーサル防御を検査する試験
/// 33 本が preflight でも `cargo check --tests`＝**コンパイルされるだけで一度も
/// 実行されていなかった**。`#[path]` で参照しているだけなので実体は1本
/// （`wire.rs`/`translate.rs` と同じ作法）。作業ディレクトリの選択・保存
/// （`WsState`・`config_path`・`adopt_root` 等）は `crate::app_root()` に
/// 依存するのでここには残す。
#[path = "../blackwall-core/src/workspace_guard.rs"]
mod workspace_guard;
use workspace_guard::{
    atomic_write, canonicalize_root, info_of, list_in, read_text_in, reject_dangerous_root,
    resolve_in_ws, write_text_in, WsEntry, WsInfo,
};
pub use workspace_guard::display_path;

// ---------------------------------------------------------------------------
// 定数
// ---------------------------------------------------------------------------
//
// ★2026-09-05（r4b/tests-never-run）: `FILES_COUNT_CAP`・`WsInfo`・
//   `reject_dangerous_root`・`info_of` は `crate::app_root()` に依存しない純粋な
//   コードだったので、上の `use` の通り `workspace_guard` へ移し、安全側の試験
//   （`the_filesystem_root_cannot_be_the_workspace` 等）を実行対象にした。
//   ここに残る `config_path`/`load_state_from`/`save_state_to`/`adopt_root`/
//   `current_root` は `crate::app_root()`（Tauri のアプリ設定ディレクトリ解決）に
//   依存する——これらはこの crate の外へは出せない。

/// 履歴の保持数。
const RECENT_MAX: usize = 8;   // ★2026-10-08 入力欄の上の札から選ぶので 5→8

// ---------------------------------------------------------------------------
// 設定の保存
// ---------------------------------------------------------------------------

/// `%LOCALAPPDATA%\Blackwall\workspace.json`（非 Windows は XDG 相当）。
///
/// 場所の決定は main.rs の `app_root()` に任せる。ここで同じ計算を書くと、片方だけ直したときに
/// 静かにズレる（hf.rs と llama.rs が同じ場所を別経路で出していて既に危うい）。
fn config_path() -> PathBuf {
    crate::app_root().join("workspace.json")
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct WsState {
    /// いま選ばれている作業ディレクトリ（正準化済みの絶対パス）。
    #[serde(default)]
    current: Option<String>,
    /// 直近に選ばれたもの。新しい順。
    #[serde(default)]
    recent: Vec<String>,
}

/// 状態を読む。**壊れていたら黙って既定に戻す**（起動と操作を優先する）。
///
/// 「黙って」と言っても痕跡は残す: stderr に1行出す。画面には出さない——利用者にできることが
/// 無いのに警告を出すと、次に本当に必要な警告を読まなくなる。
fn load_state_from(path: &Path) -> WsState {
    let Ok(text) = std::fs::read_to_string(path) else {
        return WsState::default(); // 未作成が普通。初回起動はここを通る。
    };
    match serde_json::from_str::<WsState>(&text) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[blackwall] {} を読めないので既定に戻します: {e}", path.display());
            WsState::default()
        }
    }
}

fn save_state_to(path: &Path, state: &WsState) -> Result<(), String> {
    let body = serde_json::to_vec_pretty(state)
        .map_err(|e| format!("設定を組み立てられません: {e}"))?;
    atomic_write(path, &body)
}

/// 履歴の先頭へ入れる。同じものは重複させない（比較はパス比較と同じ規則）。
fn push_recent(state: &mut WsState, path: &str) {
    state.recent.retain(|p| !path_str_eq(p, path));
    state.recent.insert(0, path.to_string());
    state.recent.truncate(RECENT_MAX);
}

/// パス文字列の同一判定。Windows は大文字小文字を区別しない。
fn path_str_eq(a: &str, b: &str) -> bool {
    if cfg!(windows) {
        a.eq_ignore_ascii_case(b)
    } else {
        a == b
    }
}

// ---------------------------------------------------------------------------
// パスの表示・封じ込め
//
// ★`display_path` / `component_eq` / `is_within` / `split_relative` /
//   `is_reserved_device_name` / `canonicalize_root` / `resolve_in_ws` の実体は
//   `workspace_guard`（冒頭の `#[path]` で取り込み済み）にある。ここでは
//   `use` で名前だけ持ち込んでいる。
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// 現在の作業ディレクトリ
// ---------------------------------------------------------------------------

/// 選ばれている作業ディレクトリを、正準形で返す。
///
/// 未設定（Ok(None) 相当）と、設定されているのに消えている（エラー）を混ぜない。
/// 前者は「選んでください」、後者は「選び直してください」で、利用者がやることが違う。
/// いまの作業ディレクトリ。★sidecar が Python 本体へ渡すのに使う。
pub fn current_root() -> Result<PathBuf, String> {
    let state = load_state_from(&config_path());
    let Some(p) = state.current.filter(|s| !s.trim().is_empty()) else {
        return Err("作業ディレクトリが未設定です。フォルダを選んでください".to_string());
    };
    canonicalize_root(Path::new(&p))
        .map_err(|e| format!("{e}。作業ディレクトリを選び直してください"))
}

// ★`reject_dangerous_root` / `info_of` の実体は `workspace_guard`（冒頭の `use` で
//   取り込み済み）にある。2026-09-05（r4b/tests-never-run）に移した——どちらも
//   `crate::app_root()` に依存しない純粋関数だったため（前回の移設で「config/state は
//   app_root 依存だから残す」とした説明はこの2本には当てはまっていなかった）。

/// 作業ディレクトリを決めて保存する。ws_set と ws_pick の共通処理。
fn adopt_root(path: &Path) -> Result<WsInfo, String> {
    let canon = canonicalize_root(path)?;
    reject_dangerous_root(&canon)?;

    let info = info_of(&canon);

    let cfg = config_path();
    let mut state = load_state_from(&cfg);
    state.current = Some(info.path.clone());
    push_recent(&mut state, &info.path);
    // 保存に失敗しても選択は成立させる。「今回は使えるが次回は覚えていない」方が、
    // 「フォルダを選べない」より遥かにましなので、ここでは落とさず理由だけ残す。
    if let Err(e) = save_state_to(&cfg, &state) {
        eprintln!("[blackwall] 作業ディレクトリを保存できません: {e}");
    }
    Ok(info)
}

// ---------------------------------------------------------------------------
// 一覧・読み・書き（root を引数で受ける形。commands は薄い皮）
//
// ★`list_in` / `read_text_in` / `write_text_in` の実体は `workspace_guard` に
//   ある（冒頭の `use` で持ち込み済み）。
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// commands
// ---------------------------------------------------------------------------

/// ネイティブのフォルダ選択を出す。取り消しは `Ok(None)`。
///
/// ★`blocking_pick_folder` は主スレッドで呼ぶと固まる（プラグインの注記）。非同期コマンドは
/// 主スレッドでは走らないが、暗黙に頼らず `spawn_blocking` に載せる。ダイアログが開いている間は
/// 呼び出し元のスレッドを占有するので、非同期ランタイムの作業用スレッドを塞がない形にしたい。
#[tauri::command]
pub async fn ws_pick(app: AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;

    // 前回の場所から開くと選び直しが速い。取れなければプラグインの既定に任せる。
    let start = load_state_from(&config_path())
        .current
        .map(PathBuf::from)
        .filter(|p| p.is_dir());

    let picked = tauri::async_runtime::spawn_blocking(move || {
        let mut b = app.dialog().file().set_title("作業ディレクトリを選ぶ");
        if let Some(d) = start {
            b = b.set_directory(d);
        }
        b.blocking_pick_folder()
    })
    .await
    .map_err(|e| format!("フォルダ選択を実行できません: {e}"))?;

    let Some(fp) = picked else {
        return Ok(None); // 取り消しは失敗ではない
    };
    let path = fp
        .into_path()
        .map_err(|e| format!("選ばれた場所をパスとして扱えません: {e}"))?;

    Ok(Some(adopt_root(&path)?.path))
}

/// いま選ばれている作業ディレクトリ。未設定なら `Ok(None)`。
///
/// 「未設定」と「設定されているが消えた」を区別する。後者はエラーで返す——黙って未設定に
/// 戻すと、利用者は「なぜか毎回選び直し」になり、原因（フォルダを移動した）に辿り着けない。
#[tauri::command]
pub async fn ws_get() -> Result<Option<WsInfo>, String> {
    let state = load_state_from(&config_path());
    let Some(p) = state.current.filter(|s| !s.trim().is_empty()) else {
        return Ok(None);
    };
    let canon = canonicalize_root(Path::new(&p))
        .map_err(|e| format!("{e}。移動または削除された可能性があります。選び直してください"))?;
    Ok(Some(info_of(&canon)))
}

#[tauri::command]
pub async fn ws_set(path: String) -> Result<WsInfo, String> {
    if path.trim().is_empty() {
        return Err("フォルダのパスが空です".to_string());
    }
    adopt_root(Path::new(path.trim()))
}

/// 直近に使った作業ディレクトリ（新しい順・最大5件）。
///
/// 消えたものは落として返す。選べないものを出しても押した先で失敗するだけ。
#[tauri::command]
pub async fn ws_recent() -> Result<Vec<String>, String> {
    let state = load_state_from(&config_path());
    Ok(state
        .recent
        .into_iter()
        .filter(|p| Path::new(p).is_dir())
        .take(RECENT_MAX)
        .collect())
}

/// 作業ディレクトリ（または `rel` で指したその下のフォルダ）の中身を1階層だけ返す。
/// エクスプローラーのための道の解決（作業ディレクトリの外は通さない・scm.rs の ws_dir などが使う）。
pub fn resolve_rel(rel: &str) -> Result<(PathBuf, PathBuf), String> {
    let root = current_root()?;
    let base = canonicalize_root(&root)?;
    let path = resolve_in_ws(&base, rel)?;
    Ok((base, path))
}

/// 新しいファイル（空）かフォルダを作る（2026-11-21 エクスプローラーの「新しいファイル」「新しいフォルダー」）。在る物は上書きしない。
#[tauri::command]
pub async fn ws_create(rel: String, dir: bool) -> Result<String, String> {
    let rel = rel.trim().trim_matches('/').replace('\\', "/");
    if rel.is_empty() || rel.split('/').any(|c| c.is_empty() || c == "." || c == "..") {
        return Err("名前が正しくありません".into());
    }
    let (_, path) = resolve_rel(&rel)?;
    if path.exists() {
        return Err(format!("{rel} は既にあります"));
    }
    if dir {
        std::fs::create_dir_all(&path).map_err(|e| format!("{rel} を作れません: {e}"))?;
    } else {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("{rel} の置き場を作れません: {e}"))?;
        }
        std::fs::OpenOptions::new().write(true).create_new(true).open(&path).map_err(|e| format!("{rel} を作れません: {e}"))?;
    }
    Ok(rel)
}

/// 名前を変える・動かす（作業ディレクトリの中だけ・行き先に在る物は上書きしない）。
#[tauri::command]
pub async fn ws_rename(from: String, to: String) -> Result<String, String> {
    let to = to.trim().trim_matches('/').replace('\\', "/");
    if to.is_empty() || to.split('/').any(|c| c.is_empty() || c == "." || c == "..") {
        return Err("新しい名前が正しくありません".into());
    }
    let from = from.trim().trim_matches('/').replace('\\', "/");
    if from.is_empty() {
        return Err("ワークスペースそのものの名前は変えられません".into());
    }
    let (_, src) = resolve_rel(&from)?;
    let (_, dst) = resolve_rel(&to)?;
    if !src.exists() {
        return Err(format!("{from} が見つかりません"));
    }
    // 大文字小文字だけを変える（Windows では同じ物を指す）のは通す
    let same = to.eq_ignore_ascii_case(&from) && to != from;
    if dst.exists() && !same {
        return Err(format!("{to} は既にあります"));
    }
    if let Some(parent) = dst.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("{to} の置き場を作れません: {e}"))?;
    }
    std::fs::rename(&src, &dst).map_err(|e| format!("名前を変えられません: {e}"))?;
    Ok(to)
}

#[tauri::command]
pub async fn ws_list(rel: Option<String>) -> Result<Vec<WsEntry>, String> {
    let root = current_root()?;
    list_in(&root, rel.as_deref().unwrap_or(""))
}

#[tauri::command]
pub async fn ws_read(rel: String) -> Result<String, String> {
    let root = current_root()?;
    read_text_in(&root, &rel)
}

/// 書き込む。親フォルダが無ければ作る。返すのは書き込んだ絶対パス。
#[tauri::command]
pub async fn ws_write(rel: String, content: String) -> Result<String, String> {
    let root = current_root()?;
    write_text_in(&root, &rel, &content)
}

// ── エディター（2026-11-17 オーナー「よりVSCode機能に近づけて。エディタとしても使えるように」・ui/explorer.js の Monaco）──
// ws_read は 256KB で切って注記を頭に足す（エージェント向け）ので、編集には使えない。ここは切らずに返し、編集してよいかを印で返す。

const EDITOR_LIMIT: u64 = 5 * 1024 * 1024;

/// 作業ディレクトリの中のファイルを、作業ディレクトリからの相対の名前にする（絶対パスでも中なら受ける）。
fn rel_in_root(root: &Path, path: &str) -> Result<String, String> {
    let p = Path::new(path.trim());
    if p.as_os_str().is_empty() {
        return Err("ファイルを指定してください".into());
    }
    if !p.is_absolute() {
        return Ok(path.trim().replace('\\', "/"));
    }
    let canon = std::fs::canonicalize(p).map_err(|_| format!("見つかりません: {path}"))?;
    let base = canonicalize_root(root)?;
    let rel = canon.strip_prefix(&base).map_err(|_| "ワークスペースの外のファイルは編集できません".to_string())?;
    Ok(rel.to_string_lossy().replace('\\', "/"))
}

/// エディターで開く中身。{ rel, text, size, binary, lossy, truncated, editable }。
/// binary（NUL を含む）・lossy（UTF-8 でない）・truncated（5MB 超）は編集させない（保存で中身を壊すため）。
#[tauri::command]
pub async fn editor_read(path: String) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        use std::io::Read;
        let root = current_root()?;
        let rel = rel_in_root(&root, &path)?;
        let base = canonicalize_root(&root)?;
        let full = std::fs::canonicalize(base.join(&rel)).map_err(|_| format!("見つかりません: {rel}"))?;
        if !full.starts_with(&base) {
            return Err("ワークスペースの外のファイルは開けません".into());
        }
        let md = std::fs::metadata(&full).map_err(|e| format!("{rel} を開けません: {e}"))?;
        if md.is_dir() {
            return Err(format!("{rel} はフォルダーです"));
        }
        let mut buf = Vec::new();
        std::fs::File::open(&full)
            .map_err(|e| format!("{rel} を開けません: {e}"))?
            .take(EDITOR_LIMIT + 1)
            .read_to_end(&mut buf)
            .map_err(|e| format!("{rel} を読めません: {e}"))?;
        let truncated = buf.len() as u64 > EDITOR_LIMIT;
        if truncated {
            buf.truncate(EDITOR_LIMIT as usize);
        }
        let binary = buf.iter().take(8000).any(|b| *b == 0);
        let (text, lossy) = if binary {
            (String::new(), false)
        } else {
            match std::str::from_utf8(&buf) {
                Ok(s) => (s.to_string(), false),
                Err(_) => (String::from_utf8_lossy(&buf).into_owned(), true),
            }
        };
        let modified = md.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_millis() as u64);
        Ok(serde_json::json!({
            "rel": rel, "path": display_path(&full), "text": text, "size": md.len(), "modified": modified,
            "binary": binary, "lossy": lossy, "truncated": truncated, "editable": !binary && !lossy && !truncated,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// エディターの保存。ws_write と同じ守り（作業ディレクトリの中だけ・原子的な書き込み）。
#[tauri::command]
pub async fn editor_write(path: String, content: String) -> Result<String, String> {
    let root = current_root()?;
    let rel = rel_in_root(&root, &path)?;
    write_text_in(&root, &rel, &content)
}

// ---------------------------------------------------------------------------
// 試験
//
// 依存を増やさないために tempdir を自作する。試験は Linux 上で回る形にしてあるが、
// 判定そのもの（予約デバイス名・末尾ドット・ADS）は Windows 前提のものも含めて全 OS で
// 同じ結果になるようにしてある——片方だけ緩いと、Linux の緑が実機の危険を隠す。
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static SEQ: AtomicUsize = AtomicUsize::new(0);

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            let n = SEQ.fetch_add(1, Ordering::SeqCst);
            let p = std::env::temp_dir()
                .join(format!("blackwall-ws-{}-{}-{}", std::process::id(), tag, n));
            let _ = std::fs::remove_dir_all(&p);
            std::fs::create_dir_all(&p).expect("試験用フォルダを作れません");
            // 実体で比べたいので、ここで正準化しておく（/tmp が symlink の環境がある）。
            TempDir(std::fs::canonicalize(&p).expect("正準化できません"))
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    // --- 設定 ---------------------------------------------------------------

    #[test]
    fn broken_config_falls_back_to_defaults() {
        let td = TempDir::new("cfg-broken");
        let p = td.path().join("workspace.json");
        std::fs::write(&p, "{ これは JSON ではない ").unwrap();
        let st = load_state_from(&p);
        assert!(st.current.is_none() && st.recent.is_empty());
    }

    #[test]
    fn config_round_trips_and_tolerates_missing_fields() {
        let td = TempDir::new("cfg-rt");
        let p = td.path().join("workspace.json");

        let mut st = WsState::default();
        st.current = Some("/x/y".to_string());
        push_recent(&mut st, "/x/y");
        save_state_to(&p, &st).unwrap();
        assert_eq!(load_state_from(&p).current.as_deref(), Some("/x/y"));

        // 片方の鍵しか無い古い形でも落ちない。
        std::fs::write(&p, r#"{"recent":["/a"]}"#).unwrap();
        let st2 = load_state_from(&p);
        assert!(st2.current.is_none());
        assert_eq!(st2.recent, vec!["/a".to_string()]);
    }

    #[test]
    fn recent_keeps_five_newest_without_duplicates() {
        let mut st = WsState::default();
        for i in 0..8 {
            push_recent(&mut st, &format!("/p{i}"));
        }
        assert_eq!(st.recent, vec!["/p7", "/p6", "/p5", "/p4", "/p3"]);

        push_recent(&mut st, "/p4"); // 既にある＝先頭へ移すだけ
        assert_eq!(st.recent.len(), RECENT_MAX);
        assert_eq!(st.recent[0], "/p4");
        assert_eq!(st.recent.iter().filter(|p| *p == "/p4").count(), 1);
    }

    // ★`the_filesystem_root_cannot_be_the_workspace` / `info_counts_only_visible_files_at_the_top_level`
    //   は 2026-09-05（r4b/tests-never-run）に `blackwall-core/src/workspace_guard.rs` へ移した。
    //   `reject_dangerous_root`/`info_of` 本体を移した後もこの2本だけここに残っていると、
    //   このファイルは tauri に依存するため `cargo test` がリンクできず
    //   （移設前の33本と同じ理由）「コンパイルされるだけで一度も実行されない」ままだった。
}

// ---------------------------------------------------------------------------
// 部分編集 — ★エージェント本体と同じ安全機構を通す（ACCEPTANCE D1）
//
// ws_write は「全文を差し替える」道具で、これだけで編集させると
// **モデルが本文を要約して書き戻し、中身を失う**（他実装で実際に起きた壊し方で、
// 設計時に「採らない」と決めた形）。編集は「古い文字列→新しい文字列」で行う。
//
// 判定そのものは edit_guard に置いてある。Python 側の tools/edit.py から移植した
// もので、実測で直してきた挙動（曖昧一致の拒否・occurrence・改行保持・原子的置換）を
// **再発明せずに**そのまま使う。
// ---------------------------------------------------------------------------

/// 部分編集の結果。画面が「何が起きたか」を出せるだけの情報を返す。
#[derive(Debug, Clone, serde::Serialize)]
pub struct WsEditResult {
    pub rel: String,
    pub replaced: usize,
    pub note: String,
}

#[tauri::command]
pub async fn ws_edit(
    rel: String,
    old_string: String,
    new_string: String,
    replace_all: Option<bool>,
    occurrence: Option<usize>,
) -> Result<WsEditResult, String> {
    let root = current_root()?;
    let path = resolve_in_ws(&root, &rel)?;

    let loaded = crate::edit_guard::load(&path)?;
    let applied = crate::edit_guard::apply_one(
        &loaded.body,
        &old_string,
        &new_string,
        replace_all.unwrap_or(false),
        occurrence,
    )?;

    // 構文を壊す編集は書かない。ただし「元から壊れているものを直す」邪魔はしない。
    if let Some(why) = crate::edit_guard::breaks_syntax(&path, &loaded.body, &applied.body) {
        return Err(format!(
            "この編集は構文を壊します（{why}）。ファイルは変更していません。"
        ));
    }

    crate::edit_guard::save_atomic(&path, &applied.body, loaded.crlf)?;
    Ok(WsEditResult {
        rel,
        replaced: applied.count,
        note: format!("{} 箇所を置き換えました", applied.count),
    })
}
