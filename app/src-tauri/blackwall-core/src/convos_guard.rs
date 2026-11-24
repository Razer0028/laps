//! 会話の一覧と記録の守り（2026-11-23・オーナー「LAPS改善頼みます」）。
//!
//! ★2026-11-19 夜、ある実機で会話の履歴が消えた（conversations.json がファイルごと無く、記録も外の端末の 2 件だけ）。
//!   原因は確定していないが、次の形と合う: 一覧が読めない・壊れている・無い → 画面は「チャット 0 件」として開く →
//!   記録の画面が「残っている記録を消す」（keep＝空＝全部）を出す → 押すと全部消える。
//!   一覧に頼る消し方は、一覧が信用できないときに走らせてはいけない。
//! ★ここに置くのは Tauri を参照しない部品だけ（`blackwall-core` で試験が実際に走る）。
//!   `src/convos.rs` が `#[path]` で参照する（実体は 1 本）。
//!
//! 守りは 3 段:
//! 1. 一覧の状態（ok・無い・読めない・壊れていた）を出し、ok 以外では一覧に頼る消し方を断る。
//! 2. 一覧の横へ避けたファイル（.broken・.unread-<秒>・.tmp）に書かれた記録の id は、一覧に頼る消し方では消さない
//!    （壊れた JSON でも文字列から拾う）。
//! 3. まとめて消すときは、すぐに消さずにゴミ箱（agent/trash/<秒>/）へ移す。7 日たったら本当に消す。戻せる。

use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// ゴミ箱に残す日数。
pub const TRASH_DAYS: u64 = 7;

/// 記録の id の形（本体の engine/session.py と同じ規則・`..` と区切り文字を拒む）。
pub fn valid_sid(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 128
        && b[0].is_ascii_alphanumeric()
        && b.iter().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-'))
        && !s.contains("..")
}

/// フォルダの中のファイルの大きさの合計（リンクはたどらない）。
pub fn dir_bytes(p: &Path) -> u64 {
    let mut total = 0u64;
    let mut stack = vec![p.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        for e in rd.flatten() {
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                stack.push(e.path());
            } else if let Ok(m) = e.metadata() {
                total += m.len();
            }
        }
    }
    total
}

// ── 一覧の状態 ─────────────────────────────────────────────────────

/// 会話の一覧（conversations.json）の状態。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ListState {
    /// 読めた（空の一覧 `[]` を含む）。
    Ok,
    /// ファイルが無い。新しく入れたばかりか、消えた。記録が残っているなら後者を疑う。
    Missing,
    /// この起動で、ファイルは在るのに読めなかった（ウイルス対策の掴み・ドライブが外れた直後など）。
    Unreadable,
    /// この起動で、中身が JSON ではなかった（別の名前へ避けた）。
    Broken,
}

impl ListState {
    pub fn as_str(self) -> &'static str {
        match self {
            ListState::Ok => "ok",
            ListState::Missing => "missing",
            ListState::Unreadable => "unreadable",
            ListState::Broken => "broken",
        }
    }
}

/// 起動のときの読み込みで起きたこと（`trouble`・起動の間は消さない）と、いまファイルが在るかから状態を決める。
/// ★読めなかった・壊れていたは、その後の保存で新しいファイルができても起動の間は残す
///   （画面が持っている一覧は、読めなかった一覧の代わりではない）。
pub fn list_state(trouble: Option<ListState>, file_exists: bool) -> ListState {
    match trouble {
        Some(t) if t != ListState::Ok => t,
        _ if !file_exists => ListState::Missing,
        _ => ListState::Ok,
    }
}

/// 一覧に頼る消し方（「一覧にない記録だけ消す」「残っている記録を消す」）を、いまの状態で断るか。
/// 断るなら理由（画面にそのまま出す文）。「すべてのチャットと記録を消す」はこれを通らない（利用者が全部と決めている）。
pub fn purge_refusal(state: ListState) -> Option<String> {
    let why = match state {
        ListState::Ok => return None,
        ListState::Missing => "チャットの一覧のファイルが見つかりません",
        ListState::Unreadable => "チャットの一覧のファイルを読めませんでした",
        ListState::Broken => "チャットの一覧のファイルが壊れていました（別の名前で残してあります）",
    };
    Some(format!(
        "{why}。どのチャットの記録かを確かめられないので、記録は消しません。先に「記録からチャットを組み直す」で一覧を戻してください。"
    ))
}

// ── 避けたファイル ─────────────────────────────────────────────────

/// 文字列から `"session": "<id>"` の id を拾う。壊れた JSON・途中で切れた JSON でも拾える分は拾う。
/// ★`"sessionWs"` のような別の鍵は拾わない（鍵の閉じ引用符まで合わせる）。
pub fn ids_in_text(text: &str) -> BTreeSet<String> {
    const KEY: &str = "\"session\"";
    let mut out = BTreeSet::new();
    let mut rest = text;
    while let Some(i) = rest.find(KEY) {
        rest = &rest[i + KEY.len()..];
        let t = rest.trim_start();
        let Some(t) = t.strip_prefix(':') else { continue };
        let t = t.trim_start();
        let Some(t) = t.strip_prefix('"') else { continue };
        let Some(end) = t.find('"') else { break };
        let id = &t[..end];
        if valid_sid(id) {
            out.insert(id.to_string());
        }
    }
    out
}

/// 一覧の横へ避けたファイル（`conversations.json.broken`・`.unread-<秒>`・`.tmp` など、一覧の名前で始まる物）。
pub fn aside_files(list_path: &Path) -> Vec<PathBuf> {
    let (Some(dir), Some(name)) = (list_path.parent(), list_path.file_name().and_then(|n| n.to_str())) else {
        return vec![];
    };
    let prefix = format!("{name}.");
    let Ok(rd) = std::fs::read_dir(dir) else { return vec![] };
    let mut out: Vec<PathBuf> = rd
        .flatten()
        .filter(|e| e.file_type().map(|t| t.is_file()).unwrap_or(false))
        .filter(|e| e.file_name().to_str().map(|n| n.starts_with(&prefix)).unwrap_or(false))
        .map(|e| e.path())
        .collect();
    out.sort();
    out
}

/// 避けたファイルの一覧（名前・大きさ・拾えた記録の id の数）と、拾えた id の全部。
/// 1 つが 256MB を超えるファイルは読まない（数えるだけ）。
pub fn aside_report(list_path: &Path) -> (Vec<Value>, BTreeSet<String>) {
    let mut all = BTreeSet::new();
    let mut rows = Vec::new();
    for p in aside_files(list_path) {
        let bytes = std::fs::metadata(&p).map(|m| m.len()).unwrap_or(0);
        let ids = if bytes <= 256 * 1024 * 1024 {
            std::fs::read(&p).map(|b| ids_in_text(&String::from_utf8_lossy(&b))).unwrap_or_default()
        } else {
            BTreeSet::new()
        };
        rows.push(json!({
            "name": p.file_name().and_then(|n| n.to_str()).unwrap_or(""),
            "bytes": bytes,
            "sessions": ids.len(),
        }));
        all.extend(ids);
    }
    (rows, all)
}

// ── ゴミ箱 ─────────────────────────────────────────────────────────

/// ゴミ箱の 1 回分のフォルダの名前から、作った時刻（UNIX 秒）を読む。`<秒>` か `<秒>-<番号>`。
pub fn batch_secs(name: &str) -> Option<u64> {
    name.split('-').next()?.parse().ok()
}

/// ゴミ箱に新しい 1 回分のフォルダを作る（同じ秒に 2 回でも重ならない）。
pub fn new_trash_batch(trash_root: &Path, now_secs: u64) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(trash_root)?;
    for n in 0..1000u32 {
        let name = if n == 0 { now_secs.to_string() } else { format!("{now_secs}-{n}") };
        let p = trash_root.join(name);
        match std::fs::create_dir(&p) {
            Ok(()) => return Ok(p),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
    Err(std::io::Error::other("ゴミ箱のフォルダを作れません"))
}

/// ゴミ箱の 1 回分のフォルダ（古い順）。
fn batches(trash_root: &Path) -> Vec<(u64, PathBuf)> {
    let Ok(rd) = std::fs::read_dir(trash_root) else { return vec![] };
    let mut out: Vec<(u64, PathBuf)> = rd
        .flatten()
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .filter_map(|e| e.file_name().to_str().and_then(batch_secs).map(|s| (s, e.path())))
        .collect();
    out.sort();
    out
}

/// `days` 日より古い回のフォルダを消す。消した回の数。
pub fn sweep_trash(trash_root: &Path, now_secs: u64, days: u64) -> usize {
    let limit = days * 86_400;
    let mut n = 0;
    for (secs, p) in batches(trash_root) {
        if now_secs.saturating_sub(secs) > limit && std::fs::remove_dir_all(&p).is_ok() {
            n += 1;
        }
    }
    n
}

/// ゴミ箱の中身（回の数・記録の数・大きさ・いちばん古い回の時刻）。
pub fn trash_stats(trash_root: &Path) -> Value {
    let bs = batches(trash_root);
    let mut sessions = 0u64;
    let mut bytes = 0u64;
    for (_, p) in &bs {
        bytes += dir_bytes(p);
        if let Ok(rd) = std::fs::read_dir(p) {
            sessions += rd.flatten().filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false)).count() as u64;
        }
    }
    json!({
        "batches": bs.len(),
        "sessions": sessions,
        "bytes": bytes,
        "oldest": bs.first().map(|(s, _)| *s),
        "days": TRASH_DAYS,
    })
}

/// 記録 1 件をゴミ箱の回のフォルダへ移す。移した大きさ（無ければ 0）。
/// ★実際の場所が記録の場所の下にあること（リンクで外へ出ていないこと）を確かめてから移す。
pub fn move_to_trash(sessions_root: &Path, batch: &Path, id: &str) -> Result<u64, String> {
    if !valid_sid(id) {
        return Err(format!("記録の名前が不正です: {id}"));
    }
    let p = sessions_root.join(id);
    if !p.exists() {
        return Ok(0);
    }
    let (Ok(real), Ok(base)) = (p.canonicalize(), sessions_root.canonicalize()) else {
        return Err(format!("場所を確かめられません: {id}"));
    };
    if !real.starts_with(&base) || real == base {
        return Err(format!("記録の場所の外を指しています: {id}"));
    }
    let bytes = dir_bytes(&p);
    std::fs::rename(&p, batch.join(id)).map_err(|e| format!("{id}: ゴミ箱へ移せません: {e}"))?;
    Ok(bytes)
}

/// ゴミ箱の記録を全部、記録の場所へ戻す。同じ名前の記録がもう在るものは戻さない（上書きしない）。
/// 戻した後の回のフォルダは、中に一覧の控え（conversations.json）が残っていれば残す。
/// 返り値: （戻した数, 戻さなかった数, 失敗, いちばん新しい一覧の控えの中身）
pub fn restore_trash(trash_root: &Path, sessions_root: &Path) -> (u64, u64, Vec<String>, Option<String>) {
    let (mut restored, mut skipped, mut failed) = (0u64, 0u64, Vec::new());
    let mut list = None;
    let _ = std::fs::create_dir_all(sessions_root);
    for (_, b) in batches(trash_root) {
        if let Ok(text) = std::fs::read_to_string(b.join("conversations.json")) {
            if serde_json::from_str::<Value>(&text).is_ok() {
                list = Some(text);          // 新しい回ほど後に来る＝最後に読んだ物が新しい
            }
        }
        let Ok(rd) = std::fs::read_dir(&b) else { continue };
        for e in rd.flatten() {
            if !e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let Some(id) = e.file_name().to_str().map(String::from) else { continue };
            if !valid_sid(&id) {
                continue;
            }
            let to = sessions_root.join(&id);
            if to.exists() {
                skipped += 1;
                continue;
            }
            match std::fs::rename(e.path(), &to) {
                Ok(()) => restored += 1,
                Err(err) => failed.push(format!("{id}: {err}")),
            }
        }
        // 空になった回は片付ける（一覧の控えだけ残った回も、控えは読んだので片付けてよい）。
        let left_dirs = std::fs::read_dir(&b)
            .map(|rd| rd.flatten().any(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false)))
            .unwrap_or(true);
        if !left_dirs {
            let _ = std::fs::remove_dir_all(&b);
        }
    }
    (restored, skipped, failed, list)
}

// ── 一覧の日ごとの控え ─────────────────────────────────────────────
// ★外部レビューの勧め（2026-11-23）: 一覧のファイルそのものが消えた・空で上書きされた（その実機の形）ときは、記録からの組み直し
//   （依頼・道具・答えだけ）より、前の日の一覧を戻すほうが元どおりに戻る。保存のたびに、その日の最初の 1 回だけ写しを残す。
//   空の一覧（`[]`）は写さない（消えた後の空で、前の日の良い写しを押し出さない）。

/// 控えの名前 `conversations-<UNIX 日>.json` から日を読む。
pub fn history_day(name: &str) -> Option<u64> {
    name.strip_prefix("conversations-")?.strip_suffix(".json")?.parse().ok()
}

/// 控えの一覧（新しい順）。`(日, パス, 大きさ)`。
pub fn history_list(hist_dir: &Path) -> Vec<(u64, PathBuf, u64)> {
    let Ok(rd) = std::fs::read_dir(hist_dir) else { return vec![] };
    let mut out: Vec<(u64, PathBuf, u64)> = rd
        .flatten()
        .filter_map(|e| {
            let day = e.file_name().to_str().and_then(history_day)?;
            let bytes = e.metadata().ok()?.len();
            Some((day, e.path(), bytes))
        })
        .collect();
    out.sort_by(|a, b| b.0.cmp(&a.0));
    out
}

/// その日の控えがまだ無く、一覧が空でなければ写す。古い控えは `keep` 件を残して消す。写したら true。
pub fn snapshot_daily(list_path: &Path, hist_dir: &Path, day: u64, keep: usize) -> bool {
    let target = hist_dir.join(format!("conversations-{day}.json"));
    if target.exists() {
        return false;
    }
    let Ok(text) = std::fs::read_to_string(list_path) else { return false };
    let t = text.trim();
    if !t.starts_with('[') || t == "[]" || serde_json::from_str::<Value>(t).is_err() {
        return false;
    }
    if std::fs::create_dir_all(hist_dir).is_err() {
        return false;
    }
    let tmp = hist_dir.join(format!("conversations-{day}.json.tmp"));
    if std::fs::write(&tmp, text.as_bytes()).is_err() || std::fs::rename(&tmp, &target).is_err() {
        let _ = std::fs::remove_file(&tmp);
        return false;
    }
    for (_, p, _) in history_list(hist_dir).into_iter().skip(keep) {
        let _ = std::fs::remove_file(p);
    }
    true
}

// ── 記録から会話を組み直す ─────────────────────────────────────────

/// 記録（events.jsonl）の 1 回の依頼。
pub struct Run {
    pub prompt: String,
    pub workspace: String,
    pub source: String,
    pub started: f64,
    /// 画面へ渡す形 `{started, ended, finished, outcome, events}`。
    pub value: Value,
}

fn clip_chars(s: &str, n: usize) -> String {
    if s.chars().count() <= n { s.to_string() } else { s.chars().take(n).collect::<String>() + "…" }
}

/// events.jsonl の中身を、依頼（run_spec）ごとに区切る。返すのは画面の組み直しに要る事象だけ
/// （道具の始まり・終わり・本文・締め）。道具の結果は 4,000 字で切る（会話の保存と同じ上限）。
/// ★事象の無い依頼も返す（呼ぶ側が捨てるか決める）。
pub fn parse_runs(text: &str) -> Vec<Run> {
    let mut out: Vec<Run> = Vec::new();
    // (prompt, workspace, source, 始まり, 事象, 締めがあるか, outcome, 最後の時刻)
    let mut cur: Option<(String, String, String, f64, Vec<Value>, bool, Option<String>, f64)> = None;
    let flush = |c: Option<(String, String, String, f64, Vec<Value>, bool, Option<String>, f64)>, out: &mut Vec<Run>| {
        if let Some((prompt, workspace, source, started, events, finished, outcome, ended)) = c {
            out.push(Run {
                prompt,
                workspace,
                source,
                started,
                value: json!({"started": started, "ended": ended, "finished": finished, "outcome": outcome, "events": events}),
            });
        }
    };
    for line in text.lines() {
        let Ok(o) = serde_json::from_str::<Value>(line) else { continue };
        let ty = o.get("type").and_then(|v| v.as_str()).unwrap_or("");
        let ts = o.get("ts").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let p = o.get("payload").cloned().unwrap_or(Value::Null);
        let s = |k: &str| p.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        if ty == "run_spec" {
            flush(cur.take(), &mut out);
            cur = Some((s("prompt"), s("workspace"), s("source"), ts, Vec::new(), false, None, ts));
            continue;
        }
        let Some(r) = cur.as_mut() else { continue };
        r.7 = r.7.max(ts);
        let ev = match ty {
            "tool_start" => json!({"type": ty, "ts": ts, "name": p.get("name"), "input": p.get("input"), "call": p.get("call")}),
            "tool_end" => json!({"type": ty, "ts": ts, "name": p.get("name"), "call": p.get("call"), "ok": p.get("ok"),
                                  "kind": p.get("kind"),
                                  "result": clip_chars(&match p.get("result") { Some(Value::String(s)) => s.clone(), Some(v) => v.to_string(), None => String::new() }, 4000)}),
            "assistant" => json!({"type": ty, "ts": ts, "text": p.get("text")}),
            "result" => {
                r.5 = true;
                r.6 = p.get("outcome").and_then(|v| v.as_str()).map(String::from);
                json!({"type": ty, "ts": ts, "text": p.get("text"), "outcome": p.get("outcome")})
            }
            _ => continue,
        };
        r.4.push(ev);
    }
    flush(cur.take(), &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "bw-convos-guard-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn session_ids_are_checked_like_the_core_does() {
        assert!(valid_sid("20261006_205840_c3ce72"));
        for bad in ["", "..", "../x", "a/b", "a\\b", ".hidden", "a..b", "-x"] {
            assert!(!valid_sid(bad), "{bad}");
        }
    }

    #[test]
    fn a_list_that_could_not_be_trusted_refuses_the_list_based_purges() {
        // ★事故の形: 一覧が無い・読めない・壊れていた → 「残っている記録を消す」が全部を消した。
        assert_eq!(list_state(None, true), ListState::Ok);
        assert_eq!(list_state(None, false), ListState::Missing);
        assert_eq!(list_state(Some(ListState::Broken), true), ListState::Broken, "壊れていたことは、後で新しい一覧を書いても起動の間は残す");
        assert_eq!(list_state(Some(ListState::Unreadable), true), ListState::Unreadable);
        assert!(purge_refusal(ListState::Ok).is_none());
        for s in [ListState::Missing, ListState::Unreadable, ListState::Broken] {
            let why = purge_refusal(s).expect("ok 以外は断る");
            assert!(why.contains("消しません"), "{why}");
        }
    }

    #[test]
    fn ids_are_picked_from_broken_and_truncated_lists() {
        let full = r#"[{"id":1,"session":"20261119_165215_ab12","sessionWs":"C:\\w"},{"id":2,"session" : "20261118_103401"}]"#;
        let got: Vec<String> = ids_in_text(full).into_iter().collect();
        assert_eq!(got, vec!["20261118_103401", "20261119_165215_ab12"]);
        // 途中で切れた（書きかけ・壊れた）一覧からも、切れる前の分は拾う。
        let cut = &full[..full.find(",{\"id\":2").unwrap() + 20];
        assert_eq!(ids_in_text(cut).len(), 1);
        // 別の鍵（sessionWs）と、id の形でない物は拾わない。
        assert!(ids_in_text(r#"{"sessionWs":"20261119_x","session":"../etc"}"#).is_empty());
    }

    #[test]
    fn aside_files_are_found_next_to_the_list() {
        let d = tmpdir("aside");
        let list = d.join("conversations.json");
        std::fs::write(&list, "[]").unwrap();
        std::fs::write(d.join("conversations.json.broken"), r#"[{"session":"s1"},{"session":"s2""#).unwrap();
        std::fs::write(d.join("conversations.json.unread-1763800000"), r#"[{"session":"s3"}]"#).unwrap();
        std::fs::write(d.join("config.json"), r#"{"session":"nope"}"#).unwrap();
        let (rows, ids) = aside_report(&list);
        assert_eq!(rows.len(), 2);
        assert_eq!(ids.into_iter().collect::<Vec<_>>(), vec!["s1", "s2", "s3"]);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn trash_moves_restores_and_expires() {
        let d = tmpdir("trash");
        let sessions = d.join("sessions");
        let trash = d.join("trash");
        for id in ["a1", "b2"] {
            std::fs::create_dir_all(sessions.join(id)).unwrap();
            std::fs::write(sessions.join(id).join("events.jsonl"), "x".repeat(10)).unwrap();
        }
        let batch = new_trash_batch(&trash, 1_000_000).unwrap();
        let again = new_trash_batch(&trash, 1_000_000).unwrap();
        assert_ne!(batch, again, "同じ秒の 2 回目は別のフォルダ");
        let _ = std::fs::remove_dir(&again);
        assert_eq!(move_to_trash(&sessions, &batch, "a1").unwrap(), 10);
        assert_eq!(move_to_trash(&sessions, &batch, "zz").unwrap(), 0, "無い記録は 0");
        assert!(move_to_trash(&sessions, &batch, "../x").is_err());
        std::fs::write(batch.join("conversations.json"), r#"[{"session":"a1"}]"#).unwrap();
        assert!(!sessions.join("a1").exists());
        assert_eq!(trash_stats(&trash)["sessions"], 1);
        // 戻す。同じ名前がもう在る記録は上書きしない。
        std::fs::create_dir_all(batch.join("b2")).unwrap();
        let (restored, skipped, failed, list) = restore_trash(&trash, &sessions);
        assert_eq!((restored, skipped), (1, 1));
        assert!(failed.is_empty());
        assert!(sessions.join("a1").join("events.jsonl").exists());
        assert_eq!(list.as_deref(), Some(r#"[{"session":"a1"}]"#));
        // 7 日を過ぎた回は消える。過ぎていない回は残る。
        let old = new_trash_batch(&trash, 1_000_000).unwrap();
        let fresh = new_trash_batch(&trash, 1_000_000 + 8 * 86_400).unwrap();
        assert_eq!(sweep_trash(&trash, 1_000_000 + 8 * 86_400, TRASH_DAYS), 2, "古い 2 回（戻して b2 だけ残った回と old）");
        assert!(!old.exists() && fresh.exists());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn the_list_is_copied_once_a_day_and_empty_lists_are_not() {
        let d = tmpdir("hist");
        let list = d.join("conversations.json");
        let hist = d.join("hist");
        std::fs::write(&list, "[]").unwrap();
        assert!(!snapshot_daily(&list, &hist, 100, 7), "空の一覧は写さない");
        std::fs::write(&list, r#"[{"id":1,"session":"s1"}]"#).unwrap();
        assert!(snapshot_daily(&list, &hist, 100, 7));
        std::fs::write(&list, r#"[{"id":1,"session":"s1"},{"id":2,"session":"s2"}]"#).unwrap();
        assert!(!snapshot_daily(&list, &hist, 100, 7), "同じ日の 2 回目は写さない（その日の最初の形を残す）");
        assert_eq!(std::fs::read_to_string(hist.join("conversations-100.json")).unwrap(), r#"[{"id":1,"session":"s1"}]"#);
        std::fs::write(&list, "{壊れた").unwrap();
        assert!(!snapshot_daily(&list, &hist, 101, 7), "壊れた一覧は写さない");
        std::fs::write(&list, r#"[{"id":3}]"#).unwrap();
        for day in 101..110 {
            snapshot_daily(&list, &hist, day, 7);
        }
        let days: Vec<u64> = history_list(&hist).into_iter().map(|x| x.0).collect();
        assert_eq!(days, vec![109, 108, 107, 106, 105, 104, 103], "新しい 7 日分だけ残す");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn runs_are_split_per_request() {
        let text = [
            r#"{"type":"run_spec","ts":10,"payload":{"prompt":"一つ目","workspace":"C:\\w","source":"app"}}"#,
            r#"{"type":"tool_start","ts":11,"payload":{"name":"Bash","input":{"command":"ls"},"call":"c1"}}"#,
            r#"{"type":"tool_end","ts":12,"payload":{"name":"Bash","call":"c1","ok":true,"result":"a"}}"#,
            r#"{"type":"assistant","ts":13,"payload":{"text":"答え"}}"#,
            r#"{"type":"result","ts":14,"payload":{"text":"答え","outcome":"done"}}"#,
            "壊れた行",
            r#"{"type":"run_spec","ts":20,"payload":{"prompt":"二つ目"}}"#,
        ]
        .join("\n");
        let runs = parse_runs(&text);
        assert_eq!(runs.len(), 2);
        assert_eq!(runs[0].prompt, "一つ目");
        assert_eq!(runs[0].workspace, "C:\\w");
        assert_eq!(runs[0].value["events"].as_array().unwrap().len(), 4);
        assert_eq!(runs[0].value["finished"], true);
        assert_eq!(runs[0].value["ended"], 14.0);
        assert_eq!(runs[1].value["events"].as_array().unwrap().len(), 0);
        assert_eq!(runs[1].value["finished"], false);
    }
}
