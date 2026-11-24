//! サーバーのリソース（CPU・メモリ・GPU）を、左の「Server」グラフ用に取ってくる（2026-10-06 利用者の要望
//! 「サーバー側のリソース状態をリアルタイムでグラフとして表示すると良いかも。左側に」
//! 「メモリ、CPU、GPU の三つがわかればいい」）。
//!
//! ★推論機（llama-server）の API には GPU の使用率・VRAM・温度が無い。計測の口を自前で用意した人だけが
//!   `metrics_url` に書く（読み取り専用の JSON）。ここはそれを読むだけ。
//! ★宛先は config.toml の `metrics_url`（利用者が自前で用意した計測の口）だけ。未設定なら「サーバーが無い」と答える（画面はグラフを出さない）。

use std::sync::OnceLock;
use std::time::Duration;

static HTTP: OnceLock<reqwest::Client> = OnceLock::new();

fn http() -> Result<reqwest::Client, String> {
    if let Some(c) = HTTP.get() {
        return Ok(c.clone());
    }
    // no_proxy: 相手は LAN の私設アドレスのことが多い（系のプロキシに吸わせない）。
    let client = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(5))
        .user_agent("blackwall-console")
        .build()
        .map_err(|e| format!("HTTP クライアントを作れません: {e}"))?;
    let _ = HTTP.set(client.clone());
    Ok(client)
}

fn metrics_url() -> Option<String> {
    if let Some(u) = crate::journal::config_raw("metrics_url") {
        let u = u.trim().to_string();
        if !u.is_empty() {
            return Some(u);
        }
    }
    None
}

/// `since`（UNIX 秒）より新しい測定を返す（JSON 文字列）。初回は 0 で直近 10 分ぶん。
/// ★誤りは `NO_SERVER:` / `UNREACHABLE:` で始める（画面が言い分けるため）。
#[tauri::command]
pub async fn server_metrics(since: f64) -> Result<String, String> {
    let Some(url) = metrics_url() else {
        return Err("NO_SERVER:サーバーの計測先（config.toml の metrics_url）が未設定なので、グラフはありません".into());
    };
    let host = reqwest::Url::parse(&url)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.to_string()))
        .unwrap_or_else(|| url.clone());
    let resp = http()?
        .get(&url)
        .query(&[("since", format!("{since}"))])
        .send()
        .await
        .map_err(|e| format!("UNREACHABLE:{host} の計測に届きません（{e}）"))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(format!("UNREACHABLE:{host} の計測が {status} を返しました"));
    }
    let text = resp.text().await.map_err(|e| format!("UNREACHABLE:読み取りに失敗しました（{e}）"))?;
    serde_json::from_str::<serde_json::Value>(&text)
        .map_err(|_| "UNREACHABLE:計測の答えが JSON ではありません".to_string())?;
    Ok(text)
}
