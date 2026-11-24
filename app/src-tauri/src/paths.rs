//! 置き場の名前（`blackwall` → `laps`・2026-11-14）。
//!
//! 正式名称を L.A.P.S に変えたので、新しく作る置き場は `laps` の名前にする。ただし **既にある旧い名前の
//! 置き場は、そのまま使い続ける**（移さない・写さない・消さない）。利用者の会話・設定・モデル・作業ログが
//! 見えなくなる事故を起こさないため。
//!
//! 規則（Python の `laps/paths.py` とまったく同じにする）:
//!   1. new が在れば new
//!   2. new が無く old が在れば old（旧い置き場を使い続ける。new は作らない）
//!   3. どちらも無ければ new（ここで初めて作る）
//!
//! - 選ぶ単位は「置き場のいちばん上のフォルダ」。中の 1 ファイルごとに選ばない。
//! - ★**作る所も必ずここを通す**。規則を通さずに new を作ると、Python の側が「new が在る」と読んで
//!   旧い置き場のファイルを見失う（作業場の `.blackwall/attachments` は Rust が置き、Python が読む）。
//! - 結果を覚えない（毎回見る）。
//! - 環境変数で場所を明示している場合（`LAPS_AUTH_FILE` など）は、呼ぶ側でその値を先に使う。

use std::path::{Path, PathBuf};

/// 作業場の中の置き場の名前（新しい名前・旧い名前）。
pub const WS_NEW: &str = ".laps";
pub const WS_OLD: &str = ".blackwall";

/// 規則どおりに new か old を返す。何も作らない。
pub fn pick(new: PathBuf, old: PathBuf) -> PathBuf {
    if new.exists() {
        return new;
    }
    if old.exists() {
        return old;
    }
    new
}

/// `base` の下の、名前だけが違う組から選ぶ。
pub fn pick_in(base: &Path, new: &str, old: &str) -> PathBuf {
    pick(base.join(new), base.join(old))
}

/// 作業場の中の置き場（`<作業場>/.laps`・旧い `<作業場>/.blackwall`）。作らない。
/// Python の `paths.workspace_dir` と同じ。
pub fn workspace_dir(ws: &Path) -> PathBuf {
    pick_in(ws, WS_NEW, WS_OLD)
}

/// 利用者の設定の置き場（`~/.config/laps`・旧い `~/.config/blackwall`）。作らない。
/// Python の `paths.config_dir(xdg=False)` と同じ（`XDG_CONFIG_HOME` は見ない）。
pub fn config_dir() -> Option<PathBuf> {
    let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })?;
    Some(pick_in(&PathBuf::from(home).join(".config"), "laps", "blackwall"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("laps-paths-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn rule_four_cases() {
        let d = tmp("rule");
        // どちらも無い → new（作らない）
        assert_eq!(workspace_dir(&d), d.join(WS_NEW));
        assert!(!d.join(WS_NEW).exists());
        // old だけ → old（new は作らない）
        std::fs::create_dir_all(d.join(WS_OLD)).unwrap();
        assert_eq!(workspace_dir(&d), d.join(WS_OLD));
        assert!(!d.join(WS_NEW).exists());
        // 両方 → new
        std::fs::create_dir_all(d.join(WS_NEW)).unwrap();
        assert_eq!(workspace_dir(&d), d.join(WS_NEW));
        // new だけ → new
        std::fs::remove_dir_all(d.join(WS_OLD)).unwrap();
        assert_eq!(workspace_dir(&d), d.join(WS_NEW));
        let _ = std::fs::remove_dir_all(&d);
    }
}
