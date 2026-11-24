//! 環境変数の名前の読み替え（`BLACKWALL_*` → `LAPS_*`・2026-11-14）。
//!
//! 正式名称を L.A.P.S に変えたので、アプリが読む名前は `LAPS_X`。旧い `BLACKWALL_X` を設定している
//! 機械も今までどおり動くように、読む所はここを通す: `LAPS_X` を先に、無ければ `BLACKWALL_X`。
//! ★同梱の Python に**渡す**所（`cmd.env("LAPS_X", …)`）はここを通さない。Python の側
//!   （`laps/_envcompat.py`）が import のときに新旧を写し合い、LAPS が勝つ。
//! ★**消す**所（`env_remove`）は `old_name` で旧い名前も消す（片方だけ消すと、Python の側で旧い名前から戻る）。

use std::ffi::OsString;

const NEW: &str = "LAPS_";
const OLD: &str = "BLACKWALL_";

/// `LAPS_X` に対する旧い名前 `BLACKWALL_X`。`LAPS_` で始まらない名前には旧い名前が無い。
pub fn old_name(name: &str) -> Option<String> {
    name.strip_prefix(NEW).map(|rest| format!("{OLD}{rest}"))
}

/// `std::env::var` と同じ。`LAPS_X` が無ければ `BLACKWALL_X` を読む（空文字も「在る」と数える）。
pub fn var(name: &str) -> Result<String, std::env::VarError> {
    match std::env::var(name) {
        Ok(v) => Ok(v),
        Err(e) => match old_name(name) {
            Some(old) => std::env::var(old).map_err(|_| e),
            None => Err(e),
        },
    }
}

/// `std::env::var_os` と同じ。`LAPS_X` が無ければ `BLACKWALL_X` を読む。
pub fn var_os(name: &str) -> Option<OsString> {
    std::env::var_os(name).or_else(|| old_name(name).and_then(std::env::var_os))
}
