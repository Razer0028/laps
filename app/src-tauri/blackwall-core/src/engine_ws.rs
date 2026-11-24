//! 本体（エンジン）に渡す作業場を決める（2026-11-23）。
//!
//! ★なぜここに置くか: 呼ぶ側の `src/agent.rs` は `tauri` を参照するので、ホスト（Linux）では試験がリンクできない
//!   （`workspace_guard.rs`・`convos_guard.rs` と同じ事情）。判定だけをここに置き、`agent.rs` が `#[path]` で参照する。
//!
//! 道具の組（toolset）ごとの決まり:
//! - `"talk"`（チャットモード・「作業フォルダとかいらないやつ」）: 選んでいる作業フォルダーは**使わない**。
//!   データ根の下の `chat/` を渡す（エンジンは作業場へ chdir するので、何かは渡す必要がある。道具はウェブだけ）。
//! - `"chat"`（画面の Ask モード・読む道具つき。名前は歴史的な物）: 作業フォルダーがあればそれ、無ければ今のフォルダー。
//! - ほかの機械で走る会話: この PC の作業フォルダーは要らない（データ根）。
//! - それ以外（Agent・Plan）: 作業フォルダーが要る。無ければ理由をそのまま返す。

use std::path::{Path, PathBuf};

/// チャットモードの道具の組の名前。画面（app.js）とエンジン（engine/server.py の TOOLSETS）と同じ文字列。
pub const TALK: &str = "talk";

/// チャットモードのときにエンジンへ渡す作業場（データ根の下）。
pub fn talk_dir(data_root: &Path) -> PathBuf {
    data_root.join("chat")
}

/// エンジンへ渡す作業場を決める。`current` は今選んでいる作業フォルダー（無ければ理由）、
/// `cwd` は Ask モードで作業フォルダーが無いときの代わり（取れなければ None）。
pub fn engine_workspace(
    toolset: Option<&str>,
    current: Result<PathBuf, String>,
    remote: bool,
    data_root: &Path,
    cwd: Option<PathBuf>,
) -> Result<PathBuf, String> {
    if toolset == Some(TALK) {
        return Ok(talk_dir(data_root));
    }
    match current {
        Ok(w) => Ok(w),
        Err(_) if remote => Ok(data_root.to_path_buf()),
        Err(e) if toolset == Some("chat") => cwd.ok_or(e),
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root() -> PathBuf { PathBuf::from("/data/laps") }

    #[test]
    fn talk_never_uses_the_selected_folder() {
        let ws = engine_workspace(Some("talk"), Ok(PathBuf::from("/work/repo")), false, &root(), None).unwrap();
        assert_eq!(ws, PathBuf::from("/data/laps/chat"));
        // 作業フォルダーが無くても話せる
        let ws = engine_workspace(Some("talk"), Err("未設定".into()), false, &root(), None).unwrap();
        assert_eq!(ws, talk_dir(&root()));
        // ほかの機械の会話でも、チャットは手元の chat/
        let ws = engine_workspace(Some("talk"), Err("未設定".into()), true, &root(), None).unwrap();
        assert_eq!(ws, talk_dir(&root()));
    }

    #[test]
    fn ask_mode_keeps_the_old_rules() {
        // Ask（"chat"）は作業フォルダーを使い、無ければ今のフォルダー。"talk" と取り違えない。
        let ws = engine_workspace(Some("chat"), Ok(PathBuf::from("/work/repo")), false, &root(), None).unwrap();
        assert_eq!(ws, PathBuf::from("/work/repo"));
        let ws = engine_workspace(Some("chat"), Err("未設定".into()), false, &root(), Some(PathBuf::from("/cwd"))).unwrap();
        assert_eq!(ws, PathBuf::from("/cwd"));
        assert!(engine_workspace(Some("chat"), Err("未設定".into()), false, &root(), None).is_err());
    }

    #[test]
    fn agent_needs_a_folder() {
        assert_eq!(engine_workspace(None, Ok(PathBuf::from("/w")), false, &root(), None).unwrap(), PathBuf::from("/w"));
        assert_eq!(engine_workspace(Some("plan"), Err("未設定".into()), false, &root(), Some(PathBuf::from("/cwd"))).unwrap_err(), "未設定");
        assert_eq!(engine_workspace(None, Err("未設定".into()), true, &root(), None).unwrap(), root());
    }
}
