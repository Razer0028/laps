//! ふだんの Chrome をつなぐ口（2026-11-18 オーナー「L.A.P.S の Chrome 連携を強化したい。もっと Chrome を操作しやすく」）。
//!
//! L.A.P.S の Chrome 拡張機能（app/chrome-ext）が 127.0.0.1:<port>/laps-bridge へ WebSocket でつなぐ。
//! ★口はアプリに 1 つだけ（本体は会話ごとに最大 3 つ動くので、本体に持たせると口を持たない本体から使えない）。
//! 本体は app_call {op:"chrome", args:{op:"open"|"attach"|"cdp"|"tabs"|"activate"|"detach"|"events"|"status", …}} で頼む
//! （アプリの横の窓の browser_cdp と同じ形）。拡張機能からの知らせはタブごとに貯め、本体が events で取りに来る。
//! 守り: 待ち受けは 127.0.0.1 だけ・Origin は chrome-extension:// だけ・最初に合言葉（agent/chrome-bridge.json の token）。

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tokio_tungstenite::tungstenite::Message;

pub const DEFAULT_PORT: u16 = 18765;
const EVENTS_KEEP: usize = 500;

static TX: Mutex<Option<mpsc::UnboundedSender<String>>> = Mutex::new(None);
static PENDING: Mutex<Option<HashMap<u64, oneshot::Sender<Value>>>> = Mutex::new(None);
static EVENTS: Mutex<Option<HashMap<i64, VecDeque<Value>>>> = Mutex::new(None);
static GONE: Mutex<Option<HashMap<i64, String>>> = Mutex::new(None);
static INFO: Mutex<Option<Value>> = Mutex::new(None);
static LISTEN: Mutex<Option<String>> = Mutex::new(None);   // 待ち受けの様子（"ok" か、失敗の理由）
static SEQ: AtomicU64 = AtomicU64::new(1);
static CONN: AtomicU64 = AtomicU64::new(0);

fn cfg_path() -> std::path::PathBuf {
    crate::app_root().join("agent").join("chrome-bridge.json")
}

fn random_token() -> String {
    // 外の乱数の部品を足さずに、OS の乱数で鍵を決める RandomState から 3 つ引く（この PC の中だけの合言葉）。
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    let mut out = String::new();
    for i in 0..3u64 {
        let mut h = RandomState::new().build_hasher();
        h.write_u64(i ^ std::process::id() as u64);
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out
}

/// 設定（{"port","token","sites"}）。無ければ作る。本体（oc/chromebridge.py）と同じファイル。
pub fn config() -> Value {
    let p = cfg_path();
    if let Ok(t) = std::fs::read_to_string(&p) {
        if let Ok(v) = serde_json::from_str::<Value>(&t) {
            if v.get("token").and_then(|x| x.as_str()).map(|s| !s.is_empty()).unwrap_or(false) {
                return v;
            }
        }
    }
    let v = json!({ "port": DEFAULT_PORT, "token": random_token(), "sites": {} });
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let tmp = p.with_extension("tmp");
    if std::fs::write(&tmp, serde_json::to_string_pretty(&v).unwrap_or_default()).is_ok() {
        let _ = std::fs::rename(&tmp, &p);
    }
    v
}

fn lock<T>(m: &'static Mutex<Option<T>>) -> std::sync::MutexGuard<'static, Option<T>> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// アプリの起動時に 1 回。LAPS_CHROME_BRIDGE=0 で起こさない。
pub fn start() {
    if std::env::var("LAPS_CHROME_BRIDGE").map(|v| v == "0").unwrap_or(false) {
        *lock(&LISTEN) = Some("turned off (LAPS_CHROME_BRIDGE=0)".into());
        return;
    }
    tauri::async_runtime::spawn(async {
        let cfg = config();
        let port = cfg.get("port").and_then(|x| x.as_u64()).unwrap_or(DEFAULT_PORT as u64) as u16;
        let token = cfg.get("token").and_then(|x| x.as_str()).unwrap_or("").to_string();
        let listener = match tokio::net::TcpListener::bind(("127.0.0.1", port)).await {
            Ok(l) => l,
            Err(e) => {
                *lock(&LISTEN) = Some(format!("port {port} is in use or not allowed: {e}"));
                return;
            }
        };
        *lock(&LISTEN) = Some("ok".into());
        loop {
            let Ok((stream, _)) = listener.accept().await else { continue };
            let token = token.clone();
            tauri::async_runtime::spawn(async move { handle(stream, token).await });
        }
    });
}

async fn handle(stream: tokio::net::TcpStream, token: String) {
    // 拡張機能だけを受ける（Origin が chrome-extension://）。
    let check = |req: &Request, resp: Response| -> Result<Response, ErrorResponse> {
        let origin = req.headers().get("origin").and_then(|v| v.to_str().ok()).unwrap_or("");
        if origin.starts_with("chrome-extension://") && req.uri().path() == "/laps-bridge" {
            Ok(resp)
        } else {
            let mut r = ErrorResponse::new(Some("not an extension".into()));
            *r.status_mut() = tokio_tungstenite::tungstenite::http::StatusCode::FORBIDDEN;
            Err(r)
        }
    };
    let Ok(ws) = tokio_tungstenite::accept_hdr_async(stream, check).await else { return };
    let (mut write, mut read) = ws.split();
    // 最初の 1 通は合言葉（10 秒まで）。
    let hello = match tokio::time::timeout(std::time::Duration::from_secs(10), read.next()).await {
        Ok(Some(Ok(Message::Text(t)))) => serde_json::from_str::<Value>(&t).unwrap_or(Value::Null),
        _ => return,
    };
    let given = hello.get("token").and_then(|x| x.as_str()).unwrap_or("");
    let same = given.len() == token.len() && given.bytes().zip(token.bytes()).fold(0u8, |a, (x, y)| a | (x ^ y)) == 0;
    if hello.get("type").and_then(|x| x.as_str()) != Some("hello") || !same || token.is_empty() {
        let _ = write.send(Message::Text(json!({"type": "denied", "reason": "ペアリングキーが違います"}).to_string().into())).await;
        return;
    }
    let me = CONN.fetch_add(1, Ordering::SeqCst) + 1;
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    *lock(&TX) = Some(tx);
    *lock(&INFO) = Some(json!({ "version": hello.get("version"), "ua": hello.get("ua"),
                                "since": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) }));
    if write.send(Message::Text(json!({"type": "welcome"}).to_string().into())).await.is_err() {
        return;
    }
    let writer = tauri::async_runtime::spawn(async move {
        while let Some(s) = rx.recv().await {
            if write.send(Message::Text(s.into())).await.is_err() {
                break;
            }
        }
    });
    while let Some(msg) = read.next().await {
        let Ok(Message::Text(t)) = msg else {
            if matches!(msg, Ok(Message::Close(_)) | Err(_)) { break; }
            continue;
        };
        let Ok(m) = serde_json::from_str::<Value>(&t) else { continue };
        if let Some(re) = m.get("re").and_then(|x| x.as_u64()) {
            if let Some(tx) = lock(&PENDING).get_or_insert_with(Default::default).remove(&re) {
                let _ = tx.send(m);
            }
            continue;
        }
        let tab = m.get("tabId").and_then(|x| x.as_i64()).unwrap_or(0);
        match m.get("type").and_then(|x| x.as_str()) {
            Some("event") => {
                let mut g = lock(&EVENTS);
                let q = g.get_or_insert_with(Default::default).entry(tab).or_default();
                if q.len() >= EVENTS_KEEP {
                    q.pop_front();
                }
                q.push_back(json!({ "method": m.get("method"), "params": m.get("params").cloned().unwrap_or(json!({})),
                                    "session": m.get("sessionId").cloned().unwrap_or(json!("")) }));
            }
            Some("detached") | Some("closed") => {
                let why = m.get("reason").and_then(|x| x.as_str()).or(m.get("type").and_then(|x| x.as_str())).unwrap_or("closed");
                lock(&GONE).get_or_insert_with(Default::default).insert(tab, why.to_string());
            }
            _ => {}
        }
    }
    writer.abort();
    // 新しいつなぎに入れ替わっていなければ、口を閉じたことにする（待っている頼みは失敗で返る）。
    if CONN.load(Ordering::SeqCst) == me {
        *lock(&TX) = None;
        *lock(&INFO) = None;
        lock(&PENDING).get_or_insert_with(Default::default).clear();
    }
}

/// 本体の頼み（app_call op="chrome"）。答えは必ず {ok, …}。
pub async fn call(args: &Value) -> Value {
    let op = args.get("op").and_then(|x| x.as_str()).unwrap_or("");
    let tab = args.get("tabId").and_then(|x| x.as_i64()).unwrap_or(0);
    match op {
        "status" => {
            let connected = lock(&TX).is_some();
            let cfg = config();
            return json!({ "ok": true, "connected": connected, "port": cfg.get("port"),
                           "listen": lock(&LISTEN).clone().unwrap_or_default(), "extension": lock(&INFO).clone() });
        }
        "events" => {
            let events: Vec<Value> = lock(&EVENTS).get_or_insert_with(Default::default)
                .get_mut(&tab).map(|q| q.drain(..).collect()).unwrap_or_default();
            let gone = lock(&GONE).get_or_insert_with(Default::default).get(&tab).cloned();
            return json!({ "ok": true, "events": events, "gone": gone });
        }
        _ => {}
    }
    let Some(tx) = lock(&TX).clone() else {
        let why = lock(&LISTEN).clone().unwrap_or_default();
        return json!({ "ok": false, "error": format!("the L.A.P.S Chrome extension is not connected{}",
                                                    if why.is_empty() || why == "ok" { String::new() } else { format!(" ({why})") }) });
    };
    if op == "open" || op == "attach" {
        if tab != 0 {
            lock(&GONE).get_or_insert_with(Default::default).remove(&tab);
        }
    }
    let id = SEQ.fetch_add(1, Ordering::SeqCst);
    let (otx, orx) = oneshot::channel::<Value>();
    lock(&PENDING).get_or_insert_with(Default::default).insert(id, otx);
    let mut msg = args.clone();
    if let Some(o) = msg.as_object_mut() {
        o.remove("timeout");
        o.insert("id".into(), json!(id));
    }
    if tx.send(msg.to_string()).is_err() {
        lock(&PENDING).get_or_insert_with(Default::default).remove(&id);
        return json!({ "ok": false, "error": "the Chrome extension disconnected" });
    }
    let secs = args.get("timeout").and_then(|x| x.as_f64()).unwrap_or(15.0).clamp(1.0, 120.0);
    match tokio::time::timeout(std::time::Duration::from_secs_f64(secs), orx).await {
        Ok(Ok(r)) => {
            if let Some(e) = r.get("error") {
                let e = e.as_str().map(|s| s.to_string()).unwrap_or_else(|| e.to_string());
                if e.contains("not attached") || e.contains("No tab with") {
                    lock(&GONE).get_or_insert_with(Default::default)
                        .insert(tab, if e.contains("No tab with") { "closed".into() } else { "canceled_by_user".into() });
                }
                json!({ "ok": false, "error": e })
            } else {
                json!({ "ok": true, "result": r.get("result").cloned().unwrap_or(json!({})) })
            }
        }
        Ok(Err(_)) => json!({ "ok": false, "error": "the Chrome extension disconnected" }),
        Err(_) => {
            lock(&PENDING).get_or_insert_with(Default::default).remove(&id);
            json!({ "ok": false, "error": format!("{} timed out after {secs} s",
                                                  args.get("method").and_then(|x| x.as_str()).unwrap_or(op)), "timeout": true })
        }
    }
}

fn ext_dir(app: &tauri::AppHandle) -> std::path::PathBuf {
    use tauri::Manager;
    app.path().resource_dir().map(|d| d.join("chrome-ext")).unwrap_or_else(|_| crate::app_root().join("chrome-ext"))
}

/// 画面の「Chrome とつなぐ」用（合言葉・ポート・つながっているか・拡張機能のフォルダ）。
#[tauri::command]
pub fn chrome_bridge_info(app: tauri::AppHandle) -> Value {
    let cfg = config();
    let dir = ext_dir(&app);
    json!({ "connected": lock(&TX).is_some(), "port": cfg.get("port"), "token": cfg.get("token"),
            "listen": lock(&LISTEN).clone().unwrap_or_default(), "extension": lock(&INFO).clone(),
            "path": cfg_path().to_string_lossy(), "ext_dir": dir.to_string_lossy(), "ext_present": dir.join("manifest.json").is_file() })
}

/// 拡張機能のフォルダをエクスプローラーで開く（Chrome の「読み込む」で選ぶため）。
#[tauri::command]
pub fn chrome_ext_open(app: tauri::AppHandle) -> Result<String, String> {
    let dir = ext_dir(&app);
    if !dir.join("manifest.json").is_file() {
        return Err(format!("拡張機能のフォルダが見つかりません: {}", dir.display()));
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        std::process::Command::new("explorer.exe").arg(&dir).creation_flags(0x0800_0000).spawn()
            .map_err(|e| format!("エクスプローラを開けません: {e}"))?;
    }
    #[cfg(not(windows))]
    {
        std::process::Command::new("xdg-open").arg(&dir).spawn().map_err(|e| format!("フォルダを開けません: {e}"))?;
    }
    Ok(dir.to_string_lossy().into_owned())
}
