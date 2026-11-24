//! アカウント（2026-10-31 オーナー「自分のアカウントでログインしているデバイスならどこからでも見れるように」
//! 「メールアドレスで登録の方が楽かも」「システムは自作する」）。
//! ★中身の処理（中継との話・認証情報の置き場・TLS）は laps/relayauth.py の 1 か所だけに置く。ここはパスワードを
//!   標準入力で渡して答えを返すだけ（引数や環境変数に出さない）。認証情報そのものは画面へ渡さない。
use serde_json::{json, Value};
use std::path::PathBuf;

fn auth_path() -> Option<PathBuf> {
    if let Some(f) = crate::envcompat::var_os("LAPS_AUTH_FILE").filter(|v| !v.is_empty()) {
        return Some(PathBuf::from(f));
    }
    // 置き場の名前は規則で選ぶ（~/.config/laps・旧い ~/.config/blackwall が在ればそのまま＝relayauth.py と同じ）
    Some(crate::paths::config_dir()?.join("auth.json"))
}

/// 手元だけで分かるサインインの状態（網に出ない・すぐ返る）。起動の直後と見回りで使う。
#[tauri::command]
pub fn auth_local() -> Value {
    let env = crate::envcompat::var("LAPS_RELAY_TOKEN").map(|v| !v.trim().is_empty()).unwrap_or(false);
    let saved = auth_path()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|t| serde_json::from_str::<Value>(&t).ok());
    let email = saved.as_ref().and_then(|v| v.get("email")).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let has = saved.as_ref().and_then(|v| v.get("token")).and_then(|v| v.as_str()).map(|t| !t.is_empty()).unwrap_or(false);
    json!({ "signed_in": env || has, "email": email })
}

/// relayauth.py の `--json` に 1 通渡して答えを返す。
#[tauri::command]
pub async fn auth_call(req: Value) -> Result<Value, String> {
    let line = format!("{req}\n");
    let env: Vec<(&str, String)> = crate::config_value("remote_relay")
        .map(|r| vec![("LAPS_RELAY_URL", r)])
        .unwrap_or_default();
    let (_code, out, err) =
        crate::sidecar::run_python_module_input("laps.relayauth", &["--json"], &env, 45, Some(&line)).await?;
    let text = out
        .lines()
        .rev()
        .find(|l| l.trim_start().starts_with('{'))
        .ok_or_else(|| format!("本体が答えませんでした: {}", err.trim().chars().take(300).collect::<String>()))?;
    serde_json::from_str(text).map_err(|e| format!("答えを読めません: {e}"))
}

/// サインインしていれば、このアカウントの機械の一覧（整合性の確認から）。していなければ None。
pub async fn machines() -> Option<Value> {
    if !auth_local().get("signed_in").and_then(|v| v.as_bool()).unwrap_or(false) {
        return None;
    }
    let got = auth_call(json!({ "op": "machines" })).await.ok()?;
    if !got.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
        return None;
    }
    got.get("items").cloned()
}
