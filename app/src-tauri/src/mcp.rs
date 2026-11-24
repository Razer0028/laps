//! MCP サーバー（コネクタ）の設定画面（2026-10-08 オーナー「API や MCP、コネクタ対応させたい」）。
//!
//! ★設定の読み書きとつなぐ処理は本体（Python の laps/oc/mcp.py）だけが持つ。ここは命令行を呼ぶだけ。
//! ★書く中身は引数ではなく環境変数 LAPS_MCP_JSON で渡す（長い JSON を引数の引用で壊さない）。

use serde_json::Value;

#[tauri::command]
pub async fn mcp_get() -> Result<String, String> {
    let (code, out, err) = crate::sidecar::run_python_module("laps.oc.mcp", &["get"], &[], 60).await?;
    if code != 0 {
        return Err(format!("mcp.json を読めません: {}", err.lines().rev().take(3).collect::<Vec<_>>().join(" / ")));
    }
    Ok(out)
}

#[tauri::command]
pub async fn mcp_set(text: String) -> Result<Value, String> {
    let (_code, out, err) =
        crate::sidecar::run_python_module("laps.oc.mcp", &["set"], &[("LAPS_MCP_JSON", text)], 60).await?;
    let line = out.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("");
    let v: Value = serde_json::from_str(line)
        .map_err(|_| format!("本体の返事を読めません: {}", err.lines().rev().take(3).collect::<Vec<_>>().join(" / ")))?;
    if let Some(e) = v.get("error").and_then(|e| e.as_str()) {
        return Err(e.to_string());
    }
    Ok(v)
}

/// 各サーバーにつないで、道具の一覧か失敗の理由を返す（npx の初回は取り寄せで数分かかることがある）。
#[tauri::command]
pub async fn mcp_status() -> Result<Value, String> {
    let (_code, out, err) = crate::sidecar::run_python_module("laps.oc.mcp", &["status"], &[], 420).await?;
    let line = out.lines().rev().find(|l| l.trim_start().starts_with('[')).unwrap_or("");
    serde_json::from_str(line)
        .map_err(|_| format!("本体の返事を読めません: {}", err.lines().rev().take(3).collect::<Vec<_>>().join(" / ")))
}
