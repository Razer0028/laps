//! ほかの端末の会話（2026-10-30 オーナー「Claude のアプリのような使いかた」・全端末で 1 つの会話の一覧と、閉じていても届く通知）。
//!
//! 道のり: 各機械の名乗り役（`laps/remotehost.py`）が自分の会話の目録を中継へ送り、中継（`relay.py` 1.5）が全機械ぶんを
//! 覚えて機械へも配る。この機械の名乗り役が受けた物を置き場に書く:
//!   `<app_root>/agent/remote/peers.json`        … 全機械の会話（新しい順・1 件は convindex.py の形＋device・device_name・online）
//!   `<app_root>/agent/remote/peer-events.ndjson` … 変わるたびに 1 行（追記）
//! ここはそれを読むだけ（★アプリに待ち受けの口を作らない約束のまま）。この機械の会話は画面の一覧が既に出しているので、
//! 知らせはほかの機械の物だけ。
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use tauri::{AppHandle, Emitter, Manager};

fn dir() -> PathBuf {
    crate::app_root().join("agent").join("remote")
}

/// この機械の名札（名乗り役が `agent/device-id` に書いた物）。無ければ空。
pub fn my_device() -> String {
    std::fs::read_to_string(crate::app_root().join("agent").join("device-id"))
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// 画面へ: {"me": この機械の名札, "items": 全機械の会話}。
#[tauri::command]
pub fn peers_load() -> String {
    let items = std::fs::read_to_string(dir().join("peers.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(|v| v.is_array())
        .unwrap_or_else(|| json!([]));
    json!({ "me": my_device(), "items": items }).to_string()
}

/// この機械が中継に繋がっているか（名乗り役の status.json・状態の札と /remote-control の窓）。
/// 名乗り役は 30 秒ごとに書き直すので、90 秒より古ければ止まっている。
#[tauri::command]
pub fn remote_status() -> Value {
    let f = dir().join("status.json");
    let fresh = std::fs::metadata(&f).ok().and_then(|m| m.modified().ok())
        .and_then(|t| t.elapsed().ok()).map(|d| d.as_secs() < 90).unwrap_or(false);
    let st = std::fs::read_to_string(&f).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()).unwrap_or_else(|| json!({}));
    let up = fresh && st.get("connected").and_then(Value::as_bool).unwrap_or(false);
    // device: 中継でのこの機械の名札（会話を消すとき中継の写しも消すのに使う・relay 1.8）
    json!({ "enabled": crate::sidecar::remote_control_enabled(), "connected": up,
            "device": st.get("device").cloned().unwrap_or(Value::Null) })
}

/// 追記の読み位置と、会話ごとの前の状態（変わったときだけ知らせる）。
pub struct Tail {
    pos: u64,
    status: HashMap<String, String>,
    started: bool,
    /// 「機械の前に人が居る」印を最後に書いた時
    presence_at: Option<std::time::Instant>,
}

impl Tail {
    pub fn new() -> Self {
        Tail { pos: 0, status: HashMap::new(), started: false, presence_at: None }
    }
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}

/// remotectl の見回り（0.3 秒ごと）から呼ぶ。増えた行を画面へ `peer-conv` で渡し、ほかの機械の会話が
/// 答え待ちになった・終わった・止まったときは Windows の通知を出す（窓が前に出ていれば画面の側に任せる）。
pub fn poll(app: &AppHandle, t: &mut Tail) {
    // ★窓が前にある間は「機械の前に人が居る」印を新しくし続ける。名乗り役（convindex.py）が読み、ほかの端末へ鳴らさない
    //   （2026-10-31・Claude の Remote Control と同じ・触っている間はスマホへ知らせない）。
    if t.presence_at.map_or(true, |at| at.elapsed().as_secs() >= 5)
        && app.get_webview_window("main").and_then(|w| w.is_focused().ok()).unwrap_or(false)
    {
        t.presence_at = Some(std::time::Instant::now());
        let _ = std::fs::write(dir().join("presence"), b"");
    }
    let f = dir().join("peer-events.ndjson");
    let Ok(mut fh) = std::fs::File::open(&f) else {
        // ★まだ無い＝これから書かれる行は全部新しい。初めて入れた端末の最初の一覧を読み飛ばさない（2026-10-31・
        //   読み飛ばすと、ほかの会話が動くかアプリを起動し直すまで「ほかの端末」の欄が空のままだった）。
        t.started = true;
        return;
    };
    let len = fh.metadata().map(|m| m.len()).unwrap_or(0);
    if !t.started {
        // ★起動した時点までの行は知らせない（古い終わりを鳴らさない）。前の状態だけ覚える。
        t.started = true;
        t.pos = len;
        if let Ok(txt) = std::fs::read_to_string(dir().join("peers.json")) {
            if let Ok(Value::Array(xs)) = serde_json::from_str::<Value>(&txt) {
                for x in xs {
                    t.status.insert(format!("{}/{}", s(&x, "device"), s(&x, "sid")), s(&x, "status"));
                }
            }
        }
        return;
    }
    if len < t.pos {
        t.pos = 0; // 名乗り役が縮めた
    }
    if len == t.pos {
        return;
    }
    if fh.seek(SeekFrom::Start(t.pos)).is_err() {
        return;
    }
    let mut buf = String::new();
    if fh.read_to_string(&mut buf).is_err() {
        return;
    }
    // 書きかけの最後の行は次の回に読む
    let complete = match buf.rfind('\n') {
        Some(i) => i + 1,
        None => return,
    };
    t.pos += complete as u64;
    let me = my_device();
    let focused = app.get_webview_window("main").and_then(|w| w.is_focused().ok()).unwrap_or(false);
    for line in buf[..complete].lines() {
        let Ok(it) = serde_json::from_str::<Value>(line) else { continue };
        let key = format!("{}/{}", s(&it, "device"), s(&it, "sid"));
        let now = s(&it, "status");
        let prev = t.status.insert(key.clone(), now.clone()).unwrap_or_default();
        let _ = app.emit("peer-conv", &it);
        if s(&it, "device") == me || focused || prev == now || it.get("quiet").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        let was_active = prev == "running" || prev == "asking";
        let title = {
            let t = s(&it, "title");
            if t.is_empty() { "L.A.P.S".to_string() } else { t }
        };
        let dev = s(&it, "device_name");
        let body = match now.as_str() {
            "asking" => format!("{dev}: {}", { let a = s(&it, "ask"); if a.is_empty() { "答えを待っています".into() } else { a } }),
            "done" if was_active => format!("{dev}: {}", { let l = s(&it, "last"); if l.is_empty() { "終わりました".into() } else { l } }),
            "failed" if was_active => format!("{dev}: 止まりました"),
            _ => continue,
        };
        crate::calltoast::note(app, title, body, key);
    }
}
