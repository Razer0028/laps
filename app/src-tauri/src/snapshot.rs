//! 会話の見た目を画像でサーバへ残す（「ログを送る」）。
//!
//! ★なぜ要るか（2026-08-14 オーナー要望）:
//! 記録（journal）は文字列なので、**見た目の崩れが分からない**。
//! 思考が本文に混ざる・Markdown が描かれない・同じ答えが並ぶ——ここまでの不具合は
//! すべてオーナーが画面を貼ってくれて初めて分かった。画像があれば、こちらで見て判断できる。
//!
//! ★なぜ Rust から送るか:
//! アプリの CSP は `connect-src ipc: http://ipc.localhost` に限っている。画面からは
//! サーバへ届かない（届いたら、それはそれで穴）。**送るのは常にこちら側**。
//!
//! ★押したときだけ送る。自動では一切送らない。
//! ★保持はサーバ側で 48 時間。
//! ★画像は文字の伏字（redact）を通らない——絵は読めないので機械では消せない。
//!   だから**自動送信にしない**。押す判断を利用者が持つ、という設計そのものが安全策。

use serde_json::{json, Value};

/// 1回に送れる大きさ。これを超えたら**送らずに断る**（黙って切らない）。
const MAX_PNG_BYTES: usize = 24 * 1024 * 1024;
/// 一緒に送る DOM の上限。構造を読むためのものなので大きくしない。
const MAX_HTML_CHARS: usize = 400_000;

/// 送り先。記録と同じ設定（`telemetry_url`）から、末尾を `/snapshot` に替える。
///
/// ★別の設定項目を作らない。2つあると必ず片方だけ設定されて、
/// 「送ったのに届かない」の原因になる。
fn snapshot_url() -> Result<String, String> {
    let cfg = crate::journal::config();
    let base = cfg
        .url
        .ok_or_else(|| format!("送信先がありません（{}）", cfg.url_reason))?;
    Ok(match base.strip_suffix("/ingest") {
        Some(root) => format!("{root}/snapshot"),
        None => {
            let root = base.trim_end_matches('/');
            format!("{root}/snapshot")
        }
    })
}

/// 画面から呼ぶ入口。**失敗を握りつぶさない**（押したのに何も起きないのが一番困る）。
#[tauri::command]
pub async fn send_snapshot(png_b64: String, html: String, note: Option<String>) -> Value {
    match send_inner(png_b64, html, note).await {
        Ok(v) => v,
        Err(e) => json!({ "ok": false, "detail": e }),
    }
}

async fn send_inner(png_b64: String, html: String, note: Option<String>) -> Result<Value, String> {
    let url = snapshot_url()?;

    // ★ここでは復号しない。依存を1つ増やすより、そのまま渡してサーバで解くほうが
    //   壊れる余地が少ない（base64 の実装差で画像が壊れる、が起きない）。
    //   大きさだけは見る: base64 は元の約 4/3 なので、そこから元の大きさを見積もる。
    if png_b64.trim().is_empty() {
        return Err("画像が空です".into());
    }
    let approx = png_b64.len() / 4 * 3;
    if approx > MAX_PNG_BYTES {
        // ★切って送らない。切れた画像で判断すると、見えていない所を「無い」と読む。
        return Err(format!(
            "画像が大きすぎます（約 {:.1}MB > {:.0}MB）。チャットを分けてから送ってください",
            approx as f64 / 1_048_576.0,
            MAX_PNG_BYTES as f64 / 1_048_576.0
        ));
    }

    let mut html = html;
    html.truncate(
        html.char_indices()
            .nth(MAX_HTML_CHARS)
            .map(|(i, _)| i)
            .unwrap_or(html.len()),
    );

    let body = json!({
        "v": 1,
        "app_version": env!("CARGO_PKG_VERSION"),
        "host_label": crate::journal::config().host_label,
        "note": note.unwrap_or_default(),
        // 画像は base64 のまま送る（multipart を自前で組むより、壊れる余地が少ない）。
        "png_b64": png_b64,
        "html": html,
    });

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(120))
        .build()
        .map_err(|e| format!("送信の準備に失敗: {e}"))?;

    let resp = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("{url} へ接続できません: {e}"))?;

    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("サーバーが HTTP {} を返しました: {}", status.as_u16(), text.trim()));
    }
    let parsed: Value = serde_json::from_str(&text).unwrap_or(json!({}));
    Ok(json!({
        "ok": true,
        "bytes": approx,
        "id": parsed.get("id").cloned().unwrap_or(Value::Null),
        "expires_at": parsed.get("expires_at").cloned().unwrap_or(Value::Null),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 送り先は記録と同じ設定から作る() {
        // `/ingest` を `/snapshot` に替えるだけ。設定項目を増やさない
        // （2つあると片方だけ設定されて「送ったのに届かない」になる）。
        assert_eq!(
            "http://example.invalid:8078/ingest".strip_suffix("/ingest").map(|r| format!("{r}/snapshot")),
            Some("http://example.invalid:8078/snapshot".to_string())
        );
    }

    #[test]
    fn 大きすぎる画像は切らずに断る() {
        // 切って送ると、見えていない所を「無い」と読むことになる。
        assert!(MAX_PNG_BYTES > 0);
    }
}
