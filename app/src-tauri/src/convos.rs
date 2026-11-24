//! 会話の保存。
//!
//! ★2026-08-11、オーナーから「会話履歴が消えている」と報告。
//! 保存先が **WebView の localStorage** だったのが原因。localStorage は
//! ブラウザのプロファイル配下に置かれ、**再インストールで一緒に消える**。
//! この日だけで 0.1.12 から 0.1.19 まで更新を配ったので、そのたびに消えていた。
//!
//! 会話は利用者の作った物であって、アプリの一時状態ではない。
//! モデルや設定と同じ**データ根（ASCII のパス）**にファイルとして置く。
//! これで再インストールでも消えず、必要なら利用者が中を見られる。
//!
//! localStorage の 5MB 制限からも外れる（長い会話で静かに保存が失敗していた恐れがある。
//! 実際に失敗しても「容量超過は黙って諦める」実装だった＝**気づけない**壊れ方だった）。

use std::path::PathBuf;

// ★一覧が信用できないときに記録を消さない判定・ゴミ箱・記録の区切り方（2026-11-23）。実体は blackwall-core の側
//   （ホストで試験が走る）。ここは `#[path]` で参照するだけ。
#[path = "../blackwall-core/src/convos_guard.rs"]
mod convos_guard;
use convos_guard as guard;
use guard::{dir_bytes, valid_sid, ListState};

fn store_path() -> PathBuf {
    crate::app_root().join("conversations.json")
}

/// 直前の読み込みが「ファイルは在るのに読めなかった」で終わったか。
///
/// ★2026-11-13: 読めないとき（ウイルス対策の掴み・D: の SSD が外れた直後の I/O の失敗）に空の一覧を返していたので、
///   画面は「会話が無い」と受け取り、次の保存で `conversations.json` を空の一覧で上書きしていた＝全会話が消える。
///   読めなかったら画面へ失敗を返し、その後の最初の保存では、いまのファイルを消さずに横へ避けてから書く。
static LOAD_FAILED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// この起動の読み込みで起きたこと（0＝何も・1＝読めなかった・2＝壊れていた）。★起動の間は消さない。
/// LOAD_FAILED は「次の保存で避ける」ための印で、避けたら下ろす。こちらは記録を消してよいかの判定に使う
/// （避けた後に新しい一覧を書いても、画面の一覧は読めなかった一覧の代わりではない・2026-11-23）。
static TROUBLE: std::sync::atomic::AtomicU8 = std::sync::atomic::AtomicU8::new(0);

fn list_state() -> ListState {
    let t = match TROUBLE.load(std::sync::atomic::Ordering::SeqCst) {
        1 => Some(ListState::Unreadable),
        2 => Some(ListState::Broken),
        _ => None,
    };
    guard::list_state(t, store_path().exists())
}

/// 保存された会話を返す。無ければ空の配列。
///
/// **壊れていても起動を止めない**。中身が JSON でなければ空を返す（会話が1つ読めないより、
/// アプリが開かないほうが困る）。ただし壊れたファイルは捨てずに退避しておく。
/// ファイルは在るのに読めないときだけ失敗を返す（空と区別する）。
#[tauri::command]
pub fn convos_load() -> Result<String, String> {
    use std::sync::atomic::Ordering;
    let p = store_path();
    let bytes = match std::fs::read(&p) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            LOAD_FAILED.store(false, Ordering::SeqCst);
            return Ok("[]".into());
        }
        Err(e) => {
            LOAD_FAILED.store(true, Ordering::SeqCst);
            TROUBLE.store(1, Ordering::SeqCst);
            return Err(format!("会話の保存（{}）を読めません: {e}", p.display()));
        }
    };
    LOAD_FAILED.store(false, Ordering::SeqCst);
    if let Ok(text) = String::from_utf8(bytes) {
        if serde_json::from_str::<serde_json::Value>(&text).is_ok() {
            return Ok(text);
        }
    }
    // 壊れていた。消さずに横へ避ける（後から中身を救えるように）。
    // ★前に避けた .broken を上書きしない（2 回目に壊れたとき、1 回目の中身が消えていた）。
    TROUBLE.store(2, Ordering::SeqCst);
    let mut aside = p.with_extension("json.broken");
    if aside.exists() {
        let ts = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        aside = p.with_extension(format!("json.broken-{ts}"));
    }
    let _ = std::fs::rename(&p, &aside);
    Ok("[]".into())
}

/// 会話を保存する。
///
/// 一時ファイルへ書いてから置き換える。途中で落ちても、
/// **既にある会話が半端な状態で残らない**（原子的書き込み）。
///
/// ★async にして画面の主スレッドで動かさない（2026-11-04 オーナー「会話履歴が長いとすごく重くなる」）。
///   同期の命令は主スレッドで走るので、数 MB の JSON の検査と書き込みの間、画面が止まっていた。
#[tauri::command]
pub async fn convos_save(json: String, seq: Option<u64>, epoch: Option<String>) -> Result<(), String> {
    // 主スレッドでなくなった分、保存が 2 つ重なりうる（同じ一時ファイルを奪い合う）。1 つずつにする。
    // ★鍵は来た順に渡るとは限らない。画面が付けた番号（seq）を見て、もう新しい物を書いた後に届いた古い物は書かない
    //   （2026-11-13: 古い一覧が後から鍵を取ると、次の保存までファイルが古い中身に戻る）。画面を読み込み直すと番号は
    //   1 から始まるので、読み込みごとの印（epoch）が変わったら数え直す。番号の無い呼び出しは今までどおり必ず書く。
    static ONE: std::sync::Mutex<(String, u64)> = std::sync::Mutex::new((String::new(), 0));
    let mut last = ONE.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(n) = seq {
        let ep = epoch.unwrap_or_default();
        if last.0 == ep && n < last.1 {
            return Ok(());
        }
        *last = (ep, n);
    }
    save_blocking(json)
}

fn save_blocking(json: String) -> Result<(), String> {
    // 受け取ったものが JSON であることだけは確かめる。壊れた文字列で
    // 既存の保存を潰さない。
    serde_json::from_str::<serde_json::Value>(&json)
        .map_err(|e| format!("保存する内容が JSON ではありません: {e}"))?;

    let p = store_path();
    if let Some(dir) = p.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("保存先を作れません: {e}"))?;
    }
    // 読めなかったファイルを、読めないままの一覧で上書きしない。横へ避けてから書く（避けられないなら書かない）。
    if LOAD_FAILED.load(std::sync::atomic::Ordering::SeqCst) && p.exists() {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let aside = p.with_extension(format!("json.unread-{ts}"));
        std::fs::rename(&p, &aside)
            .map_err(|e| format!("読めなかった会話の保存を避けられないので、上書きしません: {e}"))?;
        LOAD_FAILED.store(false, std::sync::atomic::Ordering::SeqCst);
    }
    let tmp = p.with_extension("json.tmp");
    std::fs::write(&tmp, json.as_bytes()).map_err(|e| format!("書き込めません: {e}"))?;
    std::fs::rename(&tmp, &p).map_err(|e| format!("置き換えられません: {e}"))?;
    // ★その日の最初の保存で、一覧の控えを残す（7 日分・空の一覧は写さない・2026-11-23）。失敗しても保存は成功のまま。
    guard::snapshot_daily(&p, &history_dir(), now_secs() / 86_400, 7);
    Ok(())
}

fn history_dir() -> PathBuf {
    agent_root().join("list-history")
}

/// 一覧を日ごとの控えから戻す（2026-11-23）。`day` は memory_stats の history の日。
/// いまの一覧が空でなければ、消さずに conversations.json.replaced-<秒> へ避けてから戻す（避けた一覧の記録は消す側に入らない）。
/// 戻した一覧は信用できる一覧なので、この起動の「読めなかった・壊れていた」の印を下ろす。
#[tauri::command]
pub fn convos_restore_history(day: u64) -> Result<String, String> {
    let src = history_dir().join(format!("conversations-{day}.json"));
    let text = std::fs::read_to_string(&src).map_err(|e| format!("控えを読めません: {e}"))?;
    let v: serde_json::Value = serde_json::from_str(&text).map_err(|e| format!("控えが壊れています: {e}"))?;
    let n = v.as_array().map(|a| a.len()).ok_or_else(|| "控えが一覧の形ではありません".to_string())?;
    let p = store_path();
    let current_empty = match std::fs::read_to_string(&p) {
        Ok(t) => t.trim() == "[]",
        Err(e) => e.kind() == std::io::ErrorKind::NotFound,
    };
    if p.exists() && !current_empty && !LOAD_FAILED.load(std::sync::atomic::Ordering::SeqCst) {
        let aside = p.with_extension(format!("json.replaced-{}", now_secs()));
        std::fs::rename(&p, &aside).map_err(|e| format!("いまの一覧を避けられないので、戻しません: {e}"))?;
    }
    save_blocking(text)?;
    TROUBLE.store(0, std::sync::atomic::Ordering::SeqCst);
    Ok(serde_json::json!({ "restored": n }).to_string())
}

// ---------------------------------------------------------------------------
// 落ちた走行を記録から組み直す（2026-10-08 オーナー「Blackwall が落ちる前に指示した約 80 分間のタスクの履歴が何も残っていない」）。
//
// ★画面は依頼を送った瞬間に一覧へ保存するが、応答（道具の記録・本文）は**走行が終わった時点**でしか保存しない。
//   アプリが走行の途中で落ちると（実機: D: の SSD が外れてアプリ本体ごと読めなくなった）、一覧には依頼だけが残る。
//   本体は記録（agent/sessions/<id>/events.jsonl）を書き続けているので、そこから**その依頼の走行**を探して返す。
// ★返すのは画面の組み直しに要る事象だけ（道具の始まり・終わり・本文・締め）。道具の結果は 4,000 字で切る（保存と同じ上限）。
// ---------------------------------------------------------------------------

fn norm_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}


/// [sid] の記録から、依頼の文が [prompt] の**最後の**走行を探す。見つかれば事象の列（JSON）を返す。
/// 区切り方は guard::parse_runs。
fn recover_from(sid: &str, prompt: &str) -> Option<serde_json::Value> {
    if !valid_sid(sid) {
        return None;
    }
    let path = sessions_root().join(sid).join("events.jsonl");
    let meta = std::fs::metadata(&path).ok()?;
    if meta.len() > 64 * 1024 * 1024 {
        return None;
    }
    let text = std::fs::read_to_string(&path).ok()?;
    let want = norm_ws(prompt);
    if want.is_empty() {
        return None;
    }
    // ★見るのは最後の依頼だけ（旧い実装と同じ: 最後の run_spec が依頼と合わなければ見つからない扱い）。
    //   前の依頼まで探すと、ほかの会話で同じ文を送って終わった走行を、落ちた走行と取り違える。
    let run = guard::parse_runs(&text).into_iter().last().filter(|r| {
        let pr = norm_ws(&r.prompt);
        pr == want || pr.contains(&want)
    })?;
    if run.value["events"].as_array().map(|a| a.is_empty()).unwrap_or(true) {
        return None;
    }
    let mut v = run.value;
    v["session"] = serde_json::Value::String(sid.to_string());
    Some(v)
}

/// 画面の会話の最後が依頼のまま（応答が保存されていない）とき、その走行を記録から探す。
/// [session] が分かっていればその記録だけ、分からなければ（会話の最初の走行で落ちた）新しい順に 30 件を見る。
/// 見つからなければ "null"。
///
/// ★async（主スレッドで動かさない・2026-11-13）: 記録は 1 本で数十 MB になり、起動のたびに最大 30 本を読む。同期の命令は
///   主スレッドで走るので、その間は画面が止まっていた（convos_save と同じ理由）。
#[tauri::command]
pub async fn session_recover_run(prompt: String, session: Option<String>) -> String {
    let found = match session.filter(|s| !s.is_empty()) {
        Some(sid) => recover_from(&sid, &prompt),
        None => {
            let mut ids = session_ids();
            ids.reverse();
            ids.iter().take(30).find_map(|sid| recover_from(sid, &prompt))
        }
    };
    found.map(|v| v.to_string()).unwrap_or_else(|| "null".into())
}

/// 保存先の場所（画面で案内するため）。
#[tauri::command]
pub fn convos_path() -> String {
    store_path().display().to_string()
}

// ---------------------------------------------------------------------------
// 会話の記録（メモリ）を数える・消す（2026-10-06 オーナー「過去のメモリを削除できるように。
// チャットが存在している場合と存在していない場合の二パターンを想定してわかりやすく」）。
//
// ★画面の会話一覧（conversations.json）と、本体が会話ごとに書く記録（agent/sessions/<id>/）は別物。
//   一覧の「×」は一覧の行しか消しておらず、記録（会話の本文・推論機へ送る写し・経過）は残っていた。
//   実機（2026-10-06）: 一覧 1 件に対し記録 77 件（9.3MB）＝ 76 件はどの会話からも開けない記録。
// ★消すのは記録の場所の中だけ。id は本体と同じ規則で検査し（`..`・区切り文字を拒む）、
//   実際の場所が sessions の下にあることを確かめてから消す。
// ---------------------------------------------------------------------------

fn agent_root() -> PathBuf {
    // sidecar.rs が本体に渡す LAPS_HOME と同じ（app_root()/agent）。
    crate::app_root().join("agent")
}

fn sessions_root() -> PathBuf {
    agent_root().join("sessions")
}

/// ★この会話でエージェントが変える前のファイルの中身（2026-11-17 オーナー「ツールとハーネス強化」の案 3・エディターの差分）。
/// 本体（engine/undo.py）は依頼ごとに、書き換える前の中身を `<会話>/undo/<依頼>/<blob>` に控え、目録 manifest.json に
/// `files: {絶対パス: {existed, blob}}` を書く。この会話で一番古い（取り消していない）依頼の控えを返す＝会話の前の中身。
/// 返り値 `{found, existed, text, binary, rid}`。控えが無ければ found=false（画面はふつうに開く）。
#[tauri::command]
pub fn change_base(session: String, path: String) -> Result<serde_json::Value, String> {
    if !valid_sid(&session) {
        return Ok(serde_json::json!({ "found": false }));
    }
    let norm = |p: &str| {
        let s = p.replace('\\', "/");
        let s = s.trim_end_matches('/').to_string();
        if cfg!(windows) { s.to_lowercase() } else { s }
    };
    let want = norm(&path);
    let root = sessions_root().join(&session).join("undo");
    let Ok(rd) = std::fs::read_dir(&root) else {
        return Ok(serde_json::json!({ "found": false }));
    };
    let mut best: Option<(f64, PathBuf, serde_json::Value)> = None;
    for e in rd.flatten() {
        let dir = e.path();
        let Ok(raw) = std::fs::read_to_string(dir.join("manifest.json")) else { continue };
        let Ok(meta) = serde_json::from_str::<serde_json::Value>(&raw) else { continue };
        if meta.get("undone").and_then(|v| v.as_bool()).unwrap_or(false) {
            continue;
        }
        let Some(files) = meta.get("files").and_then(|v| v.as_object()) else { continue };
        let Some(entry) = files.iter().find(|(k, _)| norm(k) == want).map(|(_, v)| v.clone()) else { continue };
        let at = meta.get("started").or_else(|| meta.get("at")).and_then(|v| v.as_f64()).unwrap_or(0.0);
        if best.as_ref().map(|b| at < b.0).unwrap_or(true) {
            best = Some((at, dir, entry));
        }
    }
    let Some((_, dir, entry)) = best else {
        return Ok(serde_json::json!({ "found": false }));
    };
    let rid = dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    if !entry.get("existed").and_then(|v| v.as_bool()).unwrap_or(false) {
        return Ok(serde_json::json!({ "found": true, "existed": false, "text": "", "binary": false, "rid": rid }));
    }
    let blob = entry.get("blob").and_then(|v| v.as_str()).unwrap_or("");
    if blob.is_empty() || blob.contains('/') || blob.contains('\\') || blob.contains("..") {
        return Ok(serde_json::json!({ "found": false }));
    }
    let data = std::fs::read(dir.join(blob)).map_err(|e| format!("控えを読めません: {e}"))?;
    if data.len() > 5 * 1024 * 1024 || data.iter().take(8192).any(|b| *b == 0) {
        return Ok(serde_json::json!({ "found": true, "existed": true, "text": "", "binary": true, "rid": rid }));
    }
    let text = String::from_utf8_lossy(data.strip_prefix(&[0xEF, 0xBB, 0xBF][..]).unwrap_or(&data)).into_owned();
    Ok(serde_json::json!({ "found": true, "existed": true, "text": text, "binary": false, "rid": rid }))
}

/// 本体（session.py の `_SID_RE`）と同じ規則: 英数字で始まり、英数字と `_ . -` だけ・128 文字まで。


fn session_ids() -> Vec<String> {
    let Ok(rd) = std::fs::read_dir(sessions_root()) else { return vec![] };
    let mut out: Vec<String> = rd
        .flatten()
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| valid_sid(n))
        .collect();
    out.sort();
    out
}

/// その記録を始めた画面（本体の run_spec か写しの run_start の source）。分からなければ空。
/// ★2026-10-30: スマホ・ウェブから始めた会話はこのアプリの一覧に載らないことがあり、「一覧にない記録を消す」で消えていた
///   （全端末で 1 つの会話の一覧を作るときに見つけた）。ほかの端末の会話は、その消し方の対象にしない。
fn session_source(id: &str) -> String {
    use std::io::Read;
    let d = sessions_root().join(id);
    for (name, kind, key) in [("events.jsonl", "type", "run_spec"), ("live.ndjson", "kind", "run_start")] {
        let Ok(mut f) = std::fs::File::open(d.join(name)) else { continue };
        let mut buf = vec![0u8; 65536];
        let n = f.read(&mut buf).unwrap_or(0);
        for line in String::from_utf8_lossy(&buf[..n]).lines() {
            let Ok(v) = serde_json::from_str::<serde_json::Value>(line) else { continue };
            if v.get(kind).and_then(|x| x.as_str()) != Some(key) {
                continue;
            }
            let src = v.get("payload").and_then(|p| p.get("source")).or_else(|| v.get("source"));
            return src.and_then(|x| x.as_str()).unwrap_or("").to_string();
        }
    }
    String::new()
}

/// ほかの端末（スマホ・ウェブ）から始めた会話か。
fn is_external(id: &str) -> bool {
    matches!(session_source(id).as_str(), "android" | "web" | "remote")
}

fn remove_session(id: &str) -> Result<u64, String> {
    if !valid_sid(id) {
        return Err(format!("記録の名前が不正です: {id}"));
    }
    let root = sessions_root();
    let p = root.join(id);
    if !p.exists() {
        return Ok(0);
    }
    // 実際の場所が sessions の下にあること（リンクで外へ出ていないこと）を確かめる。
    let (Ok(real), Ok(base)) = (p.canonicalize(), root.canonicalize()) else {
        return Err(format!("場所を確かめられません: {id}"));
    };
    if !real.starts_with(&base) || real == base {
        return Err(format!("記録の場所の外を指しています: {id}"));
    }
    let bytes = dir_bytes(&p);
    std::fs::remove_dir_all(&p).map_err(|e| format!("{id}: {e}"))?;
    Ok(bytes)
}

fn trash_root() -> PathBuf {
    agent_root().join("trash")
}

fn now_secs() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// 記録の数と大きさ。`referenced` は画面の会話一覧が指している記録の id。
/// ★2026-11-23: 一覧の状態（list_state）・一覧の横へ避けたファイル（aside）・ゴミ箱（trash）も返す。
///   「一覧にない記録」（orphans）からは、避けたファイルが指している記録（protected）を除く＝一覧に頼る消し方では消さない。
///   `rebuildable` は記録からチャットに組み直せる数（一覧が指していない・ほかの端末のでない記録。避けた物も含む）。
///   開くたびに、7 日を過ぎたゴミ箱の回を本当に消す。
#[tauri::command]
pub fn memory_stats(referenced: Vec<String>) -> String {
    guard::sweep_trash(&trash_root(), now_secs(), guard::TRASH_DAYS);
    let ids = session_ids();
    let root = sessions_root();
    let (aside, aside_ids) = guard::aside_report(&store_path());
    let mut bytes = 0u64;
    let (mut orphans, mut orphan_bytes, mut protected, mut rebuildable) = (0u64, 0u64, 0u64, 0u64);
    for id in &ids {
        let b = dir_bytes(&root.join(id));
        bytes += b;
        if referenced.iter().any(|r| r == id) || is_external(id) {
            continue;
        }
        rebuildable += 1;
        if aside_ids.contains(id) {
            protected += 1;
        } else {
            orphans += 1;
            orphan_bytes += b;
        }
    }
    let tool_output = dir_bytes(&agent_root().join("tool-output"));
    serde_json::json!({
        "sessions": ids.len(),
        "bytes": bytes,
        "orphans": orphans,
        "orphan_bytes": orphan_bytes,
        "protected": protected,
        "rebuildable": rebuildable,
        "tool_output_bytes": tool_output,
        "path": agent_root().display().to_string(),
        "list_state": list_state().as_str(),
        "list_path": store_path().display().to_string(),
        "aside": aside,
        "trash": guard::trash_stats(&trash_root()),
        "history": guard::history_list(&history_dir())
            .into_iter()
            .map(|(day, path, bytes)| {
                let sessions = std::fs::read(&path).map(|b| guard::ids_in_text(&String::from_utf8_lossy(&b)).len()).unwrap_or(0);
                serde_json::json!({ "day": day, "bytes": bytes, "sessions": sessions })
            })
            .collect::<Vec<_>>(),
    })
    .to_string()
}

/// 記録を新しい id へ写す（一覧の「⋯」→ フォーク・2026-11-17 オーナー「Claude には一つのセッションでも削除以外に色々操作できる」）。
/// 写した会話は元の発言をすべて持ったまま、別の会話として続けられる。id の形は本体（engine/session.py の new_id）と同じ。
/// meta.json の id だけ新しい id に書き換える（次の走行でも本体が書き直す）。
#[tauri::command]
pub fn session_fork(id: String) -> Result<String, String> {
    if !valid_sid(&id) {
        return Err(format!("記録の名前が不正です: {id}"));
    }
    let root = sessions_root();
    let src = root.join(&id);
    let (Ok(real), Ok(base)) = (src.canonicalize(), root.canonicalize()) else {
        return Err(format!("記録が見つかりません: {id}"));
    };
    if !real.starts_with(&base) || real == base || !real.is_dir() {
        return Err(format!("記録の場所の外を指しています: {id}"));
    }
    let now = time::OffsetDateTime::now_utc();
    let now = time::UtcOffset::current_local_offset().map(|o| now.to_offset(o)).unwrap_or(now);
    let stamp = format!(
        "{:04}{:02}{:02}_{:02}{:02}{:02}",
        now.year(), u8::from(now.month()), now.day(), now.hour(), now.minute(), now.second()
    );
    let seed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
        ^ ((std::process::id() as u64) << 20);
    let mut new_id = String::new();
    for k in 0..64u64 {
        let r = (seed.wrapping_mul(6364136223846793005).wrapping_add(k.wrapping_mul(1442695040888963407)) >> 20) & 0xFF_FFFF;
        let cand = format!("{stamp}_{r:06x}");
        if !root.join(&cand).exists() {
            new_id = cand;
            break;
        }
    }
    if new_id.is_empty() {
        return Err("新しい記録の名前を決められません".into());
    }
    let dst = root.join(&new_id);
    // 中のファイルを写す（下のフォルダも）。途中で失敗したら写しかけを消す。
    fn copy_tree(from: &std::path::Path, to: &std::path::Path) -> std::io::Result<()> {
        std::fs::create_dir(to)?;
        for e in std::fs::read_dir(from)? {
            let e = e?;
            let ft = e.file_type()?;
            let target = to.join(e.file_name());
            if ft.is_dir() {
                copy_tree(&e.path(), &target)?;
            } else if ft.is_file() {
                std::fs::copy(e.path(), &target)?;
            }
        }
        Ok(())
    }
    if let Err(e) = copy_tree(&real, &dst) {
        let _ = std::fs::remove_dir_all(&dst);
        return Err(format!("記録を写せません: {e}"));
    }
    let meta = dst.join("meta.json");
    if let Ok(text) = std::fs::read_to_string(&meta) {
        if let Ok(mut v) = serde_json::from_str::<serde_json::Value>(&text) {
            if let Some(o) = v.as_object_mut() {
                o.insert("id".into(), serde_json::Value::String(new_id.clone()));
                o.insert("forked_from".into(), serde_json::Value::String(id.clone()));
                let _ = std::fs::write(&meta, serde_json::to_string_pretty(&v).unwrap_or(text));
            }
        }
    }
    Ok(new_id)
}

/// 名指した記録を消す（左の一覧の「×」）。無い記録は消えたものとして数えない。
#[tauri::command]
pub fn memory_delete_sessions(ids: Vec<String>) -> String {
    let (mut deleted, mut freed, mut failed) = (0u64, 0u64, Vec::<String>::new());
    for id in ids.iter().filter(|s| !s.is_empty()) {
        match remove_session(id) {
            Ok(0) => {}
            Ok(b) => {
                deleted += 1;
                freed += b;
            }
            Err(e) => failed.push(e),
        }
    }
    serde_json::json!({ "deleted": deleted, "bytes": freed, "failed": failed }).to_string()
}

/// `keep` 以外の記録を消す。`with_tool_output` なら道具の出力の控え（tool-output）も空にする。
/// 「一覧にない記録だけ」は keep＝一覧が指す id、「残っている記録」は keep＝空、「すべて」は keep＝空で `all`。
/// `keep_external` なら、ほかの端末から始めた会話も残す（「一覧にない記録だけ」・2026-10-30）。
///
/// ★2026-11-23（ある実機で履歴が消えた件・原因は未確定）:
///   - `all` でないとき（一覧に頼る消し方）は、一覧の状態が ok でなければ何も消さずに理由を返す。
///     ok でも、一覧の横へ避けたファイルが指している記録は消さない。
///   - 記録はすぐに消さず、ゴミ箱（agent/trash/<秒>/）へ移す。7 日間は「元に戻す」で戻せる。
///     `all` のときは、いまの一覧（conversations.json）の写しもその回のゴミ箱に入れる（画面はこの後で一覧を空にする）。
///   - 道具の出力の控えは控え（作り直せる物）なので、今までどおりすぐ消す。
#[tauri::command]
pub fn memory_purge(keep: Vec<String>, with_tool_output: bool, keep_external: Option<bool>, all: Option<bool>) -> String {
    let (mut deleted, mut freed, mut failed) = (0u64, 0u64, Vec::<String>::new());
    let keep_external = keep_external.unwrap_or(false);
    let all = all.unwrap_or(false);
    if !all {
        if let Some(why) = guard::purge_refusal(list_state()) {
            return serde_json::json!({ "deleted": 0, "bytes": 0, "failed": [why], "refused": true }).to_string();
        }
    }
    let protected = if all { Default::default() } else { guard::aside_report(&store_path()).1 };
    let targets: Vec<String> = session_ids()
        .into_iter()
        .filter(|id| !keep.iter().any(|k| k == id))
        .filter(|id| !(keep_external && is_external(id)))
        .filter(|id| !protected.contains(id))
        .collect();
    let mut trashed = 0u64;
    if !targets.is_empty() || all {
        match guard::new_trash_batch(&trash_root(), now_secs()) {
            Ok(batch) => {
                if all {
                    if let Ok(b) = std::fs::read(store_path()) {
                        let _ = std::fs::write(batch.join("conversations.json"), b);
                    }
                }
                for id in &targets {
                    match guard::move_to_trash(&sessions_root(), &batch, id) {
                        Ok(0) => {}
                        Ok(b) => {
                            deleted += 1;
                            trashed += 1;
                            freed += b;
                        }
                        Err(e) => failed.push(e),
                    }
                }
                // 何も入らなかった回は残さない。
                if std::fs::read_dir(&batch).map(|mut r| r.next().is_none()).unwrap_or(false) {
                    let _ = std::fs::remove_dir(&batch);
                }
            }
            // ゴミ箱を作れないなら消さない（取り消せない消し方へ黙って落ちない）。
            Err(e) => failed.push(format!("ゴミ箱を作れないので、記録は消しません: {e}")),
        }
    }
    if with_tool_output {
        let d = agent_root().join("tool-output");
        if let Ok(rd) = std::fs::read_dir(&d) {
            for e in rd.flatten() {
                let p = e.path();
                let b = if p.is_dir() { dir_bytes(&p) } else { e.metadata().map(|m| m.len()).unwrap_or(0) };
                let r = if p.is_dir() { std::fs::remove_dir_all(&p) } else { std::fs::remove_file(&p) };
                match r {
                    Ok(()) => freed += b,
                    Err(err) => failed.push(format!("{}: {err}", p.display())),
                }
            }
        }
    }
    serde_json::json!({ "deleted": deleted, "bytes": freed, "failed": failed, "trashed": trashed,
                        "protected": protected.len(), "days": guard::TRASH_DAYS }).to_string()
}

/// ゴミ箱の記録を全部戻す。いまの一覧が空（ファイルが無いか `[]`）で、ゴミ箱に一覧の控えがあれば、それも一覧に戻す。
/// 返り値 `{restored, skipped, failed, list_restored}`。画面は list_restored なら一覧を読み直す。
#[tauri::command]
pub fn memory_trash_restore() -> String {
    let (restored, skipped, mut failed, list) = guard::restore_trash(&trash_root(), &sessions_root());
    let mut list_restored = false;
    if let Some(text) = list {
        let empty_now = match std::fs::read_to_string(store_path()) {
            Ok(t) => serde_json::from_str::<serde_json::Value>(&t).map(|v| v.as_array().map(|a| a.is_empty()).unwrap_or(false)).unwrap_or(false),
            Err(e) => e.kind() == std::io::ErrorKind::NotFound,
        };
        // 読めないファイル（I/O の失敗）は empty_now にならない＝上書きしない。
        if empty_now {
            match save_blocking(text) {
                Ok(()) => list_restored = true,
                Err(e) => failed.push(e),
            }
        }
    }
    serde_json::json!({ "restored": restored, "skipped": skipped, "failed": failed, "list_restored": list_restored }).to_string()
}

/// ゴミ箱を空にする（本当に消す）。
#[tauri::command]
pub fn memory_trash_empty() -> String {
    let d = trash_root();
    let bytes = dir_bytes(&d);
    match std::fs::remove_dir_all(&d) {
        Ok(()) => serde_json::json!({ "bytes": bytes, "failed": [] }).to_string(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => serde_json::json!({ "bytes": 0, "failed": [] }).to_string(),
        Err(e) => serde_json::json!({ "bytes": 0, "failed": [e.to_string()] }).to_string(),
    }
}

/// 記録 1 件を、チャットを組み直す材料にする。`{session, workspace, started, runs: [...]}`。
/// events.jsonl が 64MB を超える物は `too_big`（runs は空）、読めない・依頼が 1 つも無い物は None。
fn rebuild_one(id: &str) -> Option<serde_json::Value> {
    if !valid_sid(id) {
        return None;
    }
    let path = sessions_root().join(id).join("events.jsonl");
    let meta = std::fs::metadata(&path).ok()?;
    if meta.len() > 64 * 1024 * 1024 {
        return Some(serde_json::json!({ "session": id, "too_big": true, "runs": [] }));
    }
    let text = std::fs::read_to_string(&path).ok()?;
    let runs = guard::parse_runs(&text);
    let first = runs.first()?;
    let (ws, started) = (first.workspace.clone(), first.started);
    let runs: Vec<serde_json::Value> = runs
        .into_iter()
        .map(|r| {
            let mut v = r.value;
            v["prompt"] = serde_json::Value::String(r.prompt);
            v["source"] = serde_json::Value::String(r.source);
            v
        })
        .collect();
    Some(serde_json::json!({ "session": id, "workspace": ws, "started": started, "runs": runs }))
}

/// 一覧が指していない記録から、チャットを組み直す材料を返す（2026-11-23）。
/// `exclude`＝いまの一覧が指している記録。ほかの端末から始めた記録は返さない（その端末の一覧に出る）。
/// 返り値は rebuild_one の配列（古い順）。
/// ★async（主スレッドで動かさない）: 記録は 1 本で数十 MB になる。
#[tauri::command]
pub async fn sessions_rebuild(exclude: Vec<String>) -> String {
    let mut out: Vec<serde_json::Value> = session_ids()
        .into_iter()
        .filter(|id| !exclude.iter().any(|x| x == id) && !is_external(id))
        .filter_map(|id| rebuild_one(&id))
        .collect();
    out.sort_by(|a, b| a["started"].as_f64().unwrap_or(0.0).total_cmp(&b["started"].as_f64().unwrap_or(0.0)));
    serde_json::Value::Array(out).to_string()
}

/// 記録 1 件の依頼の列（一覧に軽い形で残した古いチャットを開いたとき・2026-11-23）。無ければ "null"。
#[tauri::command]
pub async fn session_runs(id: String) -> String {
    rebuild_one(&id).map(|v| v.to_string()).unwrap_or_else(|| "null".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_path_sits_next_to_the_other_data() {
        // 設定やモデルと同じ根に置く（別の場所に散らさない）。
        assert_eq!(store_path().parent(), crate::config_path().parent());
        assert_eq!(store_path().file_name().unwrap(), "conversations.json");
    }

    #[test]
    fn broken_content_is_refused_before_it_overwrites_good_content() {
        // ★壊れた文字列を書かせない。書けてしまうと、次回の起動で全部消える。
        let e = save_blocking("これは JSON ではない".into()).unwrap_err();
        assert!(e.contains("JSON ではありません"), "{e}");
    }

    #[test]
    fn a_round_trip_keeps_the_content() {
        let json = r#"[{"id":1,"title":"あ","msgs":[{"role":"user","text":"日本語"}]}]"#;
        save_blocking(json.into()).unwrap();
        let back = convos_load().unwrap();     // ★2026-11-13 に Result を返すようになったのに試験が古いままで、Rust の試験が組み立てられなかった
        let v: serde_json::Value = serde_json::from_str(&back).unwrap();
        assert_eq!(v[0]["msgs"][0]["text"], "日本語");
        let _ = std::fs::remove_file(store_path());
    }
}
