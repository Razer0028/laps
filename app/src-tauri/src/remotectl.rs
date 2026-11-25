//! スマホ（Android）からこの機械を操作する橋（2026-10-10）。
//!
//! オーナー（2026-10-10）: 「モバイル版はリモートセッション管理用に機能を絞ります。その代わりリモート環境でできる操作は
//! すべてできるように」→ 範囲は「会話の操作＋機械の管理まで全部」。
//!
//! 道のり: Android → 中継 → この機械のゲートウェイ（Python・`laps/remotectl.py`）→ 受け箱 → ここ。
//! ★アプリには待ち受けの口を作らない（sidecar.rs の冒頭の約束）。ゲートウェイとはファイルで渡す:
//!   `<app_root>/agent/remote/ctl/req-<id>.json` … ゲートウェイが置く {"id","op","args","ts"}。ここが読んで消す。
//!   `<app_root>/agent/remote/ctl/res-<id>.json` … ここが置く {"id","ok","result"|"error"}。ゲートウェイが読んで消す。
//!   `<app_root>/agent/remote/ctl/app.json`      … 心拍（2 秒ごと）。受ける操作の一覧 [`OPS`] を載せる。
//! ★操作は画面（app.js の invoke）が呼ぶのと**同じ関数**を呼ぶ。中身を写さない（片方だけ直す型を作らない）。
//! ★受けるのは [`OPS`] に書いた物だけ。画面の外へ窓を開く物（フォルダを選ぶ窓・エクスプローラ）は入れない。

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::AppHandle;

/// 受ける操作（ゲートウェイはこの一覧に無い物を送らない・スマホの画面もこの一覧で出す物を決める）。
pub const OPS: &[&str] = &[
    "app.version", "engine.status", "tools.list",
    "backend.info", "backend.probe", "lan.get", "lan.set",
    "llama.status", "llama.start", "llama.stop", "llama.load",
    "models.local", "models.catalog", "hf.search", "hf.files", "hf.download",
    "gpu.probe", "gpu.local.status", "gpu.local.prepare",
    "integrity.check", "integrity.fix", "selftest",
    "ws.get", "ws.set", "ws.recent",
    "convos.load", "memory.stats", "memory.delete", "memory.purge", "memory.trash_restore",
    "metrics", "journal.status",
    // ★ウェブ版・スマホから送った依頼を、この画面で打ったのと同じ道で走らせる（2026-10-10 オーナー「ウェブから送った内容や状態は
    //   接続先のアプリ内でも動作するように。ClaudeCodeのRemote-controlと同じ」）。画面（app.js の remoteChat）が受ける。
    "chat.send", "chat.stop", "chat.delete",
];

/// これより古い要求は捨てる（アプリが落ちていた間に置かれた物を、起動した瞬間に実行しない）。
const STALE_REQ_SECS: f64 = 120.0;

fn dir() -> PathBuf {
    crate::app_root().join("agent").join("remote").join("ctl")
}

fn now_secs() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// 一時名で書いてから名前を替える（読み手に書きかけを掴ませない）。
fn write_atomic(path: &Path, body: &str) {
    let tmp = path.with_extension("tmp");
    if std::fs::write(&tmp, body).is_ok() && std::fs::rename(&tmp, path).is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
}

fn beat(d: &Path) {
    let body = json!({
        "pid": std::process::id(),
        "version": env!("CARGO_PKG_VERSION"),
        "ts": now_secs(),
        "ops": OPS,
    });
    write_atomic(&d.join("app.json"), &body.to_string());
}

/// 橋を立てる（アプリの起動時に 1 回）。リモート操作を切っている機械では立てない。
pub fn start(app: AppHandle) {
    if !crate::sidecar::remote_control_enabled() {
        return;
    }
    tauri::async_runtime::spawn(async move {
        let d = dir();
        let _ = std::fs::create_dir_all(&d);
        let mut last_beat = 0.0f64;
        let mut peer_tail = crate::peers::Tail::new();
        loop {
            // ほかの端末の会話の変わり目（名乗り役が書く peer-events.ndjson）を画面と通知へ。
            crate::peers::poll(&app, &mut peer_tail);
            let now = now_secs();
            if now - last_beat >= 2.0 {
                beat(&d);
                last_beat = now;
            }
            if let Ok(rd) = std::fs::read_dir(&d) {
                for e in rd.flatten() {
                    let name = e.file_name().to_string_lossy().to_string();
                    if !(name.starts_with("req-") && name.ends_with(".json")) {
                        continue;
                    }
                    let p = e.path();
                    let Ok(text) = std::fs::read_to_string(&p) else { continue };
                    // ★読んだら先に消す（同じ要求を 2 度実行しない）。
                    let _ = std::fs::remove_file(&p);
                    let Ok(req) = serde_json::from_str::<Value>(&text) else { continue };
                    let id = req.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    // 答えの名前に使うので、英数字だけ（道のりの記号を通さない）。
                    if id.is_empty() || id.len() > 64 || !id.chars().all(|c| c.is_ascii_alphanumeric()) {
                        continue;
                    }
                    let ts = req.get("ts").and_then(|v| v.as_f64()).unwrap_or(0.0);
                    let op = req.get("op").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    let args = req.get("args").cloned().unwrap_or(Value::Null);
                    let d2 = d.clone();
                    if now - ts > STALE_REQ_SECS {
                        write_res(&d2, &id, Err("リクエストが古いため実行しませんでした（アプリが止まっていた間に届いた物）".into()));
                        continue;
                    }
                    let app2 = app.clone();
                    tauri::async_runtime::spawn(async move {
                        let r = dispatch(&app2, &op, &args).await;
                        write_res(&d2, &id, r);
                    });
                }
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
    });
}

fn write_res(d: &Path, id: &str, r: Result<Value, String>) {
    let body = match r {
        Ok(v) => json!({ "id": id, "ok": true, "result": v }),
        Err(e) => json!({ "id": id, "ok": false, "error": e }),
    };
    write_atomic(&d.join(format!("res-{id}.json")), &body.to_string());
}

fn s(a: &Value, k: &str) -> String {
    a.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

fn b(a: &Value, k: &str) -> bool {
    a.get(k).and_then(|v| v.as_bool()).unwrap_or(false)
}

fn strs(a: &Value, k: &str) -> Vec<String> {
    a.get(k)
        .and_then(|v| v.as_array())
        .map(|xs| xs.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

fn val<T: serde::Serialize>(x: T) -> Result<Value, String> {
    serde_json::to_value(x).map_err(|e| e.to_string())
}

/// JSON の文字列を返す関数（convos・memory）の答えを、そのまま JSON に戻す。
fn parsed(text: String) -> Value {
    serde_json::from_str::<Value>(&text).unwrap_or(Value::String(text))
}

/// 会話の宛先の形（app.js の convEngine と同じ）: "lan" / "local" / "claude:claude-…"。
fn engine_ok(e: &str) -> bool {
    match e {
        "lan" | "local" => true,
        _ => e.strip_prefix("claude:claude-").map(|m| !m.is_empty() && m.len() <= 40
            && m.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')).unwrap_or(false),
    }
}

async fn dispatch(app: &AppHandle, op: &str, a: &Value) -> Result<Value, String> {
    use crate::{catalog, convos, gpu, hf, integrity, llama, localgpu, metrics, selftest, sidecar, workspace};
    match op {
        // 画面へ渡して、打ったのと同じ道（送信・走行中なら割り込み・止める）で動かす。待たない（走行は画面の側で続く）。
        "chat.send" | "chat.stop" => {
            use tauri::Emitter;
            let text = s(a, "text");
            if op == "chat.send" && text.trim().is_empty() {
                return Err("送る文がありません".into());
            }
            let session = s(a, "session");
            let action = if op == "chat.send" { "send" } else { "stop" };
            // ★Think の深さ（"off" / "low" / "high"・2026-10-11）。画面がその 1 回だけ使う。知らない値は渡さない。
            let think_level = s(a, "think_level");
            let think_level = if ["off", "low", "high"].contains(&think_level.as_str()) { think_level } else { String::new() };
            // ★送った端末（"android"・"web"）。画面がその 1 回の走行に付ける（2026-10-30・ほかの端末の一覧に出す）。
            let source = s(a, "source");
            let source = if !source.is_empty() && source.len() <= 16 && source.chars().all(|c| c.is_ascii_alphanumeric()) {
                source
            } else {
                "remote".to_string()
            };
            // ★会話の宛先（2026-10-30・スマホの入力欄の札）。知らない形は渡さない（画面は空ならその会話の今の宛先のまま）。
            let engine = s(a, "engine");
            let engine = if engine_ok(&engine) { engine } else { String::new() };
            // ★チャットモード（2026-11-24 Android）。新しい会話をどちらの種類で始めるか。知らない値は渡さない（画面は今のモードのまま）。
            let mode = s(a, "mode");
            let mode = if mode == "talk" || mode == "agent" { mode } else { String::new() };
            app.emit("remote-chat", json!({ "action": action, "text": text, "session": session, "force": b(a, "force"),
                                            "think_level": think_level, "source": source, "engine": engine, "mode": mode }))
                .map_err(|e| format!("画面へ渡せません: {e}"))?;
            Ok(json!({ "accepted": true, "session": session }))
        }
        // ほかの端末が記録を消した会話を、この画面の一覧からも消す（2026-10-31 オーナー「他の機器のチャットを削除できるように」）。
        "chat.delete" => {
            use tauri::Emitter;
            app.emit("remote-chat", json!({ "action": "delete", "ids": a.get("ids").cloned().unwrap_or_else(|| json!([])) }))
                .map_err(|e| format!("画面へ渡せません: {e}"))?;
            Ok(json!({ "accepted": true }))
        }
        "app.version" => Ok(json!(env!("CARGO_PKG_VERSION"))),
        "engine.status" => Ok(crate::engine_status().await),
        "tools.list" => val(crate::agent_tools().await),
        "backend.info" => val(sidecar::backend_info().await),
        "backend.probe" => Ok(json!(sidecar::backend_probe().await)),
        "lan.get" => val(sidecar::lan_settings_get()),
        "lan.set" => val(sidecar::lan_settings_set(b(a, "enabled"), s(a, "url"), s(a, "model"))?),
        "llama.status" => val(llama::llama_status().await?),
        "llama.start" => val(llama::llama_start(app.clone()).await?),
        "llama.stop" => llama::llama_stop().await.map(|_| json!(true)),
        "llama.load" => llama::llama_load(s(a, "model")).await.map(|_| json!(true)),
        "models.local" => val(hf::list_local_models().await?),
        "models.catalog" => {
            let free = a.get("vram_free_mb").and_then(|v| v.as_u64()).unwrap_or(0);
            val(catalog::catalog_list(free).await?)
        }
        "hf.search" => val(hf::hf_search(s(a, "query")).await?),
        "hf.files" => val(hf::hf_repo_files(s(a, "repo")).await?),
        "hf.download" => val(hf::hf_download(app.clone(), s(a, "repo"), s(a, "path")).await?),
        "gpu.probe" => val(gpu::probe_gpu().await?),
        "gpu.local.status" => val(localgpu::local_gpu_status().await),
        "gpu.local.prepare" => val(localgpu::local_gpu_prepare(app.clone()).await?),
        "integrity.check" => val(integrity::integrity_check().await),
        "integrity.fix" => val(integrity::integrity_fix(app.clone(), s(a, "fix")).await?),
        // ★書き込みを伴う試し（write=true）はスマホからは受けない（画面の前に人が居るときだけにする）。
        "selftest" => val(selftest::self_test(false).await?),
        "ws.get" => val(workspace::ws_get().await?),
        "ws.set" => val(workspace::ws_set(s(a, "path")).await?),
        "ws.recent" => val(workspace::ws_recent().await?),
        "convos.load" => Ok(parsed(convos::convos_load()?)),
        "memory.stats" => Ok(parsed(convos::memory_stats(strs(a, "referenced")))),
        // ★走行中は消さない（2026-11-13: この門は画面にしか無く、スマホからの依頼は、本体が書いている最中の記録のフォルダも消せた）。
        "memory.delete" | "memory.purge" if !crate::sidecar::lanes_running().is_empty() => {
            Err("実行中はメモリを消去できません。実行が終わってからもう一度お試しください。".into())
        }
        "memory.delete" => Ok(parsed(convos::memory_delete_sessions(strs(a, "ids")))),
        // ★2026-11-23: 一覧に頼る消し方は一覧が ok のときだけ・記録はゴミ箱へ（convos::memory_purge の注）。
        //   離れた所からは「すべて」（all＝一覧の確かめを通らない）を受けない（外部レビューの指摘）。
        "memory.purge" => Ok(parsed(convos::memory_purge(strs(a, "keep"), b(a, "with_tool_output"),
                                                         Some(b(a, "keep_external")), Some(false)))),
        // ゴミ箱から戻す（離れた所からも戻せるように）。本当に消す memory_trash_empty は画面だけ（取り消せない）。
        "memory.trash_restore" => Ok(parsed(convos::memory_trash_restore())),
        "metrics" => {
            let since = a.get("since").and_then(|v| v.as_f64()).unwrap_or(0.0);
            Ok(parsed(metrics::server_metrics(since).await?))
        }
        // 走行記録の送信の状態（読むだけ・2026-11-14: 実機から記録が届かない理由を、離れた所から読めなかった）。
        "journal.status" => Ok(crate::journal::journal_status()),
        _ => Err(format!("不明な操作: {op}")),
    }
}
