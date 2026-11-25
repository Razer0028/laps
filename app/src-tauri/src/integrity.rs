//! 整合性の確認（2026-10-08 オーナー「Windows 版に整合性のチェック機能を追加してほしい」）。
//!
//! 接続の画面（Connection）の「Integrity」から呼ぶ。アプリの中身・道具の部品・設定と推論機・遠隔操作・保存データを
//! 上から順に確かめ、1 行ずつ ok / warn / fail と、直せるものには「直す」の手（fix）を付けて返す。
//! ★確かめるだけでは何も書き換えない。書き換えるのは `integrity_fix` だけ。
//! 本体（Python）の側で見る所（同梱の Python の照合値・import・Listen/Desktop/ffmpeg）は
//! `python -m laps.integrity --json` に任せ、その行をそのまま混ぜる。

use serde::{Deserialize, Serialize};
use std::time::Duration;
use tauri::AppHandle;

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Row {
    pub id: String,
    pub group: String,
    pub title: String,
    pub status: String,
    pub detail: String,
    pub fix: Option<String>,
}

fn row(id: &str, group: &str, title: &str, status: &str, detail: impl Into<String>, fix: Option<&str>) -> Row {
    Row {
        id: id.into(),
        group: group.into(),
        title: title.into(),
        status: status.into(),
        detail: detail.into(),
        fix: fix.map(|s| s.to_string()),
    }
}

fn client() -> Option<reqwest::Client> {
    reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(8))
        .build()
        .ok()
}

/// 本体（Python）の側の行。動かせなければ、そのこと自体を 1 行にする。
async fn python_rows() -> Vec<Row> {
    let install = crate::sidecar::install_pyruntime_dir()
        .map(|d| d.display().to_string())
        .unwrap_or_default();
    match crate::sidecar::run_python_module(
        "laps.integrity",
        &["--json"],
        &[("LAPS_INSTALL_PYRUNTIME", install)],
        180,
    )
    .await
    {
        Ok((_code, out, err)) => {
            let line = out.lines().rev().find(|l| l.trim_start().starts_with('[')).unwrap_or("");
            match serde_json::from_str::<Vec<Row>>(line) {
                Ok(v) => v,
                Err(_) => vec![row("app.python", "app", "エンジン（Python）", "fail",
                    format!("点検の結果を読めません。{}", err.lines().rev().take(3).collect::<Vec<_>>().join(" / ")),
                    None)],
            }
        }
        Err(e) => vec![row("app.python", "app", "エンジン（Python）", "fail",
            format!("同梱の Python を動かせません: {e}。アプリを再インストールすると直ります。"), None)],
    }
}

/// 設定ファイルと、推論機に届くか・モデルが出ているか。
async fn config_rows() -> Vec<Row> {
    let mut out = Vec::new();
    let path = crate::config_path();
    match std::fs::read_to_string(&path) {
        Err(e) => out.push(row("config.file", "config", "設定ファイル", "fail",
            format!("{} を読めません（{e}）", path.display()), None)),
        Ok(_) => {
            let missing = crate::missing_config_keys(&path);
            if missing.is_empty() {
                out.push(row("config.file", "config", "設定ファイル", "ok", path.display().to_string(), None));
            } else {
                out.push(row("config.file", "config", "設定ファイル", "fail",
                    format!("不足している設定項目: {}（{}）。「修復」で説明付きで追加します（他の値には触りません）。",
                            missing.join("・"), path.display()),
                    Some("config")));
            }
        }
    }
    let d = crate::backend_decision();
    if d.remote {
        let root = d.root.trim_end_matches('/').trim_end_matches("/v1").to_string();
        let model = crate::lan_config().model;
        match client() {
            None => out.push(row("config.backend", "config", "推論サーバー", "warn", "確認する手段を作れません", None)),
            Some(c) => {
                match c.get(format!("{root}/health")).send().await {
                    Ok(r) if r.status().is_success() => {
                        out.push(row("config.backend", "config", "推論サーバー", "ok", format!("{root} に接続できます"), None));
                        let ids: Vec<String> = match c.get(format!("{root}/v1/models")).send().await {
                            Ok(r) => r.json::<serde_json::Value>().await.ok()
                                .and_then(|v| v.get("data").and_then(|d| d.as_array()).cloned())
                                .map(|a| a.iter().filter_map(|m| m.get("id").and_then(|x| x.as_str()).map(String::from)).collect())
                                .unwrap_or_default(),
                            Err(_) => Vec::new(),
                        };
                        if model.trim().is_empty() || ids.iter().any(|i| i == &model) {
                            out.push(row("config.model", "config", "モデルの名前", "ok",
                                if model.trim().is_empty() { "指定なし（推論サーバーの既定を使う）".to_string() } else { format!("{model} が出ています") }, None));
                        } else {
                            out.push(row("config.model", "config", "モデルの名前", "fail",
                                format!("設定の {model} が推論サーバーの一覧にありません（出ている物: {}）。接続画面の「モデル」を直してください。",
                                        if ids.is_empty() { "取れず".to_string() } else { ids.join(", ") }), None));
                        }
                    }
                    Ok(r) => out.push(row("config.backend", "config", "推論サーバー", "fail",
                        format!("{root} が {} を返しました", r.status()), None)),
                    Err(e) => out.push(row("config.backend", "config", "推論サーバー", "fail",
                        format!("{root} に接続できません（{e}）。サーバーが動いているか・LAN に接続しているかを確認してください。"), None)),
                }
            }
        }
    } else {
        let st = crate::localgpu::local_gpu_status().await;
        if st.engine_ready && st.model_ready {
            out.push(row("config.local", "config", "この PC の推論サーバー", "ok",
                format!("推論サーバー（{}）とモデル（{}）がそろっています", st.engine_tag, st.model_file), None));
        } else {
            let lack: Vec<&str> = [("推論サーバー", st.engine_ready), ("モデル", st.model_ready)]
                .iter().filter(|(_, ok)| !*ok).map(|(n, _)| *n).collect();
            out.push(row("config.local", "config", "この PC の推論サーバー", "fail",
                format!("推論の場所が「この PC」なのに、{} がありません。「修復」でダウンロードします。", lack.join("・")),
                Some("localgpu")));
        }
    }
    out
}

/// スマホから扱うための中継に、この機械が出ているか（中継を設定した人だけ）。
async fn remote_rows() -> Vec<Row> {
    let off = crate::config_value("remote_control")
        .map(|v| matches!(v.to_ascii_lowercase().as_str(), "false" | "0" | "off" | "no"))
        .unwrap_or(false)
        || crate::envcompat::var("LAPS_REMOTE").map(|v| v.trim() == "0").unwrap_or(false);
    if off {
        return vec![row("remote.state", "remote", "スマホからの操作", "ok", "切ってあります（設定 remote_control = false）", None)];
    }
    let Some(relay) = crate::config_value("remote_relay") else {
        return vec![row("remote.state", "remote", "スマホからの操作", "ok", "使っていません（設定 remote_relay に中継サーバーの場所を書くと有効になります）", None)];
    };
    let http = relay.replacen("wss://", "https://", 1).replacen("ws://", "http://", 1);
    let http = http.trim_end_matches('/').to_string();
    let Some(c) = client() else {
        return vec![row("remote.state", "remote", "スマホからの操作", "warn", "確認する手段を作れません", None)];
    };
    // ★サインインしていれば認証情報つきで引く（relayauth.py）。していなければ今までどおり（必須の中継なら 401）。
    let signed = crate::auth::machines().await;
    let devices = match signed {
        Some(v) => Some(v),
        None => match c.get(format!("{http}/devices")).send().await {
        Ok(r) if r.status().is_success() => r.json::<serde_json::Value>().await.ok(),
        Ok(r) if r.status().as_u16() == 401 => {
            return vec![row("remote.relay", "remote", "中継サーバー", "warn", "サインインすると確認できます", None)];
        }
        Ok(r) => {
            return vec![row("remote.relay", "remote", "中継サーバー", "fail", format!("{http} が {} を返しました", r.status()), None)];
        }
        Err(e) => {
            return vec![row("remote.relay", "remote", "中継サーバー", "fail",
                format!("{http} に接続できません（{e}）。中継サーバーが動いているか確認してください。"), None)];
        }
        },
    };
    let mut out = vec![row("remote.relay", "remote", "中継サーバー", "ok", format!("{http} に接続できます"), None)];
    let id = std::fs::read_to_string(crate::app_root().join("agent").join("device-id"))
        .map(|s| s.trim().to_string())
        .unwrap_or_default();
    let me = devices.as_ref().and_then(|v| v.as_array()).and_then(|a| {
        a.iter().find(|d| d.get("id").and_then(|x| x.as_str()) == Some(id.as_str())).cloned()
    });
    let online = me.as_ref().and_then(|d| d.get("online")).and_then(|x| x.as_bool()).unwrap_or(false);
    if !id.is_empty() && online {
        out.push(row("remote.device", "remote", "このマシンの登録", "ok", "スマホのマシンの一覧に「接続中」で出ています", None));
    } else {
        let alive = crate::sidecar::remote_loop_alive();
        out.push(row("remote.device", "remote", "このマシンの登録", "fail",
            if alive {
                "中継サーバーに登録できていません（登録処理は動いていて、再接続を続けています。少し待ってからもう一度確認してください）".to_string()
            } else {
                "登録処理が動いていません。「修復」で起動します。".to_string()
            },
            if alive { None } else { Some("remote") }));
    }
    out
}

/// 会話の保存と作業フォルダ（読むだけ）。
fn data_rows() -> Vec<Row> {
    let mut out = Vec::new();
    let conv = crate::app_root().join("conversations.json");
    match std::fs::read_to_string(&conv) {
        Err(_) => out.push(row("data.convos", "data", "チャットの保存", "ok", "まだ保存されたチャットはありません", None)),
        Ok(t) => match serde_json::from_str::<serde_json::Value>(&t) {
            Ok(v) => out.push(row("data.convos", "data", "チャットの保存", "ok",
                format!("{} 件のチャットを読めます", v.as_array().map(|a| a.len()).unwrap_or(0)), None)),
            Err(e) => out.push(row("data.convos", "data", "チャットの保存", "fail",
                format!("{} が壊れています（{e}）。次に開いたとき conversations.json.broken に退避されます。", conv.display()), None)),
        },
    }
    let ws = crate::app_root().join("workspace.json");
    let cur = std::fs::read_to_string(&ws).ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|v| v.get("current").and_then(|c| c.as_str()).map(String::from));
    match cur {
        None => out.push(row("data.workspace", "data", "ワークスペース", "warn", "まだ選ばれていません（入力欄の上のボタンから選べます）", None)),
        Some(p) if std::path::Path::new(&p).is_dir() => out.push(row("data.workspace", "data", "ワークスペース", "ok", p, None)),
        Some(p) => out.push(row("data.workspace", "data", "ワークスペース", "fail",
            format!("{p} がありません（消えた・ドライブが外れた）。入力欄の上のボタンから選び直してください。"), None)),
    }
    let sessions = crate::app_root().join("agent").join("sessions");
    out.push(row("data.sessions", "data", "実行ログ", if sessions.is_dir() || !sessions.exists() { "ok" } else { "fail" },
        if sessions.is_dir() {
            format!("{} 件（{}）", std::fs::read_dir(&sessions).map(|r| r.count()).unwrap_or(0), sessions.display())
        } else {
            "まだありません".to_string()
        }, None));
    out
}

#[tauri::command]
pub async fn integrity_check() -> Vec<Row> {
    let mut out = python_rows().await;
    out.extend(config_rows().await);
    out.extend(remote_rows().await);
    out.extend(data_rows());
    out
}

/// 「直す」。fix の名前ごとに 1 つの手。結果の一言を返す（失敗は Err）。
#[tauri::command]
pub async fn integrity_fix(app: AppHandle, fix: String) -> Result<String, String> {
    match fix.as_str() {
        "restage" => {
            let install = crate::sidecar::install_pyruntime_dir().map(|d| d.display().to_string()).unwrap_or_default();
            let (code, out, err) = crate::sidecar::run_python_module(
                "laps.integrity", &["--repair-staged"], &[("LAPS_INSTALL_PYRUNTIME", install)], 300).await?;
            let v: serde_json::Value = serde_json::from_str(out.lines().last().unwrap_or("{}"))
                .map_err(|_| format!("コピーし直しの結果を読めません: {}", err.lines().last().unwrap_or("")))?;
            let n = v.get("repaired").and_then(|x| x.as_u64()).unwrap_or(0);
            let bad: Vec<String> = v.get("failed").and_then(|x| x.as_array())
                .map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect()).unwrap_or_default();
            if code == 0 {
                Ok(format!("{n} 個のファイルをコピーし直しました"))
            } else {
                Err(format!("{n} 個はコピーし直しましたが、{} 個はできませんでした: {}（使用中のファイルはアプリを開き直すと写し直されます）",
                            bad.len(), bad.join("・")))
            }
        }
        "parts" => {
            let (code, out, err) = crate::sidecar::run_python_module("laps.oc.parts", &[], &[], 3600).await?;
            let tail: Vec<&str> = out.lines().chain(err.lines()).filter(|l| !l.trim().is_empty()).collect();
            let last = tail.iter().rev().take(3).rev().cloned().collect::<Vec<_>>().join(" / ");
            if code == 0 { Ok(format!("コンポーネントをそろえました: {last}")) } else { Err(format!("コンポーネントをそろえられませんでした: {last}")) }
        }
        "voice" => {
            // ★声の道具（oc/voice.py）の環境を入れ直す（PyTorch とモデルで 5〜7GB・時間がかかる）。
            let (code, out, err) = crate::sidecar::run_python_module("laps.oc.voice", &["--prepare"], &[], 7200).await?;
            let last = out.lines().chain(err.lines()).filter(|l| !l.trim().is_empty()).last().unwrap_or("").to_string();
            if code == 0 { Ok(last) } else { Err(format!("音声ツールをインストールできませんでした: {last}")) }
        }
        "config" => {
            crate::ensure_config_keys(&crate::config_path()).map_err(|e| format!("設定ファイルに書けません: {e}"))?;
            Ok("不足していた設定項目を説明付きで追加しました".into())
        }
        "localgpu" => {
            crate::localgpu::local_gpu_prepare(app).await?;
            Ok("この PC の推論サーバーとモデルをそろえました".into())
        }
        "remote" => {
            crate::sidecar::start_remote_host_in_background();
            Ok("登録処理を起動しました（10 秒ほどで中継サーバーの一覧に出ます）".into())
        }
        other => Err(format!("不明な修復方法です: {other}")),
    }
}
