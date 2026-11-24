//! API キーの設定画面（2026-10-08 オーナー「他のプラットフォームの API を Blackwall から扱えるようにしたい」）。
//!
//! ★置き場と暗号化は本体（Python の laps/apikeys.py）だけが持つ。ここは命令行を呼ぶだけ
//!   （2 つの言語で同じファイルの書式を持つと、片方だけ直す型になる）。
//! ★値は引数に載せない（プロセスの一覧に出る）。環境変数 LAPS_APIKEY_VALUE で渡す。
//! ★画面へ返すのは名前・ドメイン・メモ・値の頭 4 字だけ。値そのものは返さない。

use serde_json::Value;

async fn run(args: &[&str], value: Option<String>) -> Result<Value, String> {
    let env: Vec<(&str, String)> = match value {
        Some(v) => vec![("LAPS_APIKEY_VALUE", v)],
        None => vec![],
    };
    let (code, out, err) = crate::sidecar::run_python_module("laps.apikeys", args, &env, 60).await?;
    let line = out.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("");
    let v: Value = serde_json::from_str(line).map_err(|_| {
        let tail = err.lines().rev().take(3).collect::<Vec<_>>().join(" / ");
        format!("本体の返事を読めません（終了 {code}）{tail}")
    })?;
    if let Some(e) = v.get("error").and_then(|e| e.as_str()) {
        return Err(e.to_string());
    }
    Ok(v)
}

#[tauri::command]
pub async fn apikeys_list() -> Result<Value, String> {
    run(&["list"], None).await
}

#[tauri::command]
pub async fn apikeys_set(name: String, value: String, hosts: String, note: String) -> Result<Value, String> {
    let mut args = vec!["set", name.as_str(), hosts.as_str()];
    if !note.trim().is_empty() {
        args.push(note.as_str());
    }
    run(&args, Some(value)).await
}

#[tauri::command]
pub async fn apikeys_delete(name: String) -> Result<Value, String> {
    run(&["delete", name.as_str()], None).await
}
