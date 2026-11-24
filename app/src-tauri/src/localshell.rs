//! 会話のシェル（2026-11-11 オーナー「シェルを会話の画面から開けるようにしましょうかね」）。
//!
//! 画面（ui/shell.js の xterm.js）← イベント `local-shell` ← ここ ← この PC の擬似端末（Windows は ConPTY・Linux は pty）
//! の中の利用者の既定の殻。こちらは**この PC の手元の殻**。
//!
//! ★画面から渡せるのは「どのフォルダで始めるか」と端末の大きさだけ。動かすプログラムと引数はここで決める
//!   （画面に任意のプログラムを起動させる口を作らない・tauri-plugin-shell を入れていないのと同じ理由）。
//! ★1 つの会話＝1 つの殻。会話を移っても殻は動き続け、閉じる（またはアプリを閉じる）と止まる。
//! ★出力は base64 で渡す。読んだ塊の切れ目が UTF-8 の文字の途中に来ても、xterm.js が続きとつなげて描ける。
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};

use base64::Engine as _;
use portable_pty::{native_pty_system, ChildKiller, CommandBuilder, MasterPty, PtySize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};

struct Sess {
    /// 打った文字を書く糸への列。★表の鍵を握ったまま端末へ書かない（2026-11-13: 読んでいないプログラムへ大きく貼り付けると
    ///   書き込みが詰まり、鍵を握ったままなので、その殻を閉じることも大きさを変えることもできず、主スレッドごと止まりえた）。
    ///   列に入れた順に書くので、打った順は変わらない。
    tx: std::sync::mpsc::Sender<Vec<u8>>,
    master: Box<dyn MasterPty + Send>,
    killer: Box<dyn ChildKiller + Send + Sync>,
}

fn sessions() -> &'static Mutex<HashMap<u64, Sess>> {
    static S: OnceLock<Mutex<HashMap<u64, Sess>>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(HashMap::new()))
}

static NEXT: AtomicU64 = AtomicU64::new(1);

fn emit(app: &AppHandle, id: u64, mut v: Value) {
    if let Some(o) = v.as_object_mut() {
        o.insert("id".into(), json!(id));
    }
    let _ = app.emit("local-shell", v);
}

fn size(cols: u32, rows: u32) -> PtySize {
    PtySize { rows: rows.clamp(4, 500) as u16, cols: cols.clamp(10, 1000) as u16, pixel_width: 0, pixel_height: 0 }
}

/// 利用者の既定の殻（名前と、画面に出す短い名）。
fn default_shell() -> (CommandBuilder, String) {
    #[cfg(windows)]
    {
        // PowerShell 7 があればそれ、無ければ Windows に入っている PowerShell 5.1。
        let pwsh = std::env::var_os("PATH").and_then(|p| {
            std::env::split_paths(&p).map(|d| d.join("pwsh.exe")).find(|f| f.is_file())
        });
        let (exe, name) = match pwsh {
            Some(f) => (f.display().to_string(), "pwsh".to_string()),
            None => ("powershell.exe".to_string(), "powershell".to_string()),
        };
        let mut c = CommandBuilder::new(exe);
        c.arg("-NoLogo");
        (c, name)
    }
    #[cfg(not(windows))]
    {
        let sh = std::env::var("SHELL").ok().filter(|s| Path::new(s).is_file()).unwrap_or_else(|| "/bin/bash".into());
        let name = Path::new(&sh).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_else(|| "sh".into());
        let mut c = CommandBuilder::new(&sh);
        c.arg("-i");
        (c, name)
    }
}

/// 殻を開く。返すのは番号と殻の名（以後の入力・大きさ・閉じるに使う）。
/// `cwd` は会話のワークスペースのフォルダ。無い・フォルダでないときは利用者のホームで始める。
#[tauri::command]
pub async fn local_shell_open(app: AppHandle, cols: u32, rows: u32, cwd: Option<String>) -> Result<Value, String> {
    let pty = native_pty_system();
    let pair = pty.openpty(size(cols, rows)).map_err(|e| format!("擬似端末を作れません: {e}"))?;
    let (mut cmd, name) = default_shell();
    let dir = cwd
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty() && Path::new(s).is_dir())
        .or_else(|| {
            std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
                .map(|h| h.to_string_lossy().to_string())
        });
    if let Some(d) = &dir {
        cmd.cwd(d);
    }
    cmd.env("TERM", "xterm-256color");
    cmd.env("COLORTERM", "truecolor");
    cmd.env("LAPS_SHELL", "1");
    let mut child = pair.slave.spawn_command(cmd).map_err(|e| format!("殻を起こせません: {e}"))?;
    drop(pair.slave);
    let mut reader = pair.master.try_clone_reader().map_err(|e| format!("端末を読めません: {e}"))?;
    let mut writer = pair.master.take_writer().map_err(|e| format!("端末へ書けません: {e}"))?;
    let (tx, rx) = std::sync::mpsc::channel::<Vec<u8>>();
    // 書き手: 列に入った順に端末へ書く。表から外れる（送り手が消える）か、書けなくなったら終わる。
    std::thread::spawn(move || {
        for data in rx {
            if writer.write_all(&data).is_err() || writer.flush().is_err() {
                break;
            }
        }
    });
    let killer = child.clone_killer();
    let id = NEXT.fetch_add(1, Ordering::Relaxed);
    sessions()
        .lock()
        .map_err(|_| "シェルの表が壊れています")?
        .insert(id, Sess { tx, master: pair.master, killer });

    // 読み手: 殻の出力をそのまま画面へ。表から外れて端末が閉じると読み終わる。
    let app_r = app.clone();
    std::thread::spawn(move || {
        let mut buf = vec![0u8; 16 * 1024];
        loop {
            match reader.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let d = base64::engine::general_purpose::STANDARD.encode(&buf[..n]);
                    emit(&app_r, id, json!({ "t": "o", "d": d }));
                }
            }
        }
    });
    // 見張り: 殻が終わったら知らせ、表から外す（端末を閉じる＝Windows で読み手が止まらないのを防ぐ）。
    let app_w = app.clone();
    std::thread::spawn(move || {
        let code = child.wait().ok().map(|s| s.exit_code());
        if let Ok(mut m) = sessions().lock() {
            m.remove(&id);
        }
        emit(&app_w, id, json!({ "t": "exit", "code": code }));
    });
    Ok(json!({ "id": id, "shell": name, "cwd": dir }))
}

/// 打った文字（xterm.js の onData・貼り付けもここ）。
#[tauri::command]
pub fn local_shell_input(id: u64, data: String) -> Result<(), String> {
    let m = sessions().lock().map_err(|_| "シェルの表が壊れています")?;
    let s = m.get(&id).ok_or("そのシェルは閉じています")?;
    s.tx.send(data.into_bytes()).map_err(|_| "シェルへ送れません（閉じています）".to_string())
}

/// 端末の大きさが変わった（枠の大きさ・文字の大きさ）。
#[tauri::command]
pub fn local_shell_resize(id: u64, cols: u32, rows: u32) -> Result<(), String> {
    let m = sessions().lock().map_err(|_| "シェルの表が壊れています")?;
    let s = m.get(&id).ok_or("そのシェルは閉じています")?;
    s.master.resize(size(cols, rows)).map_err(|e| format!("大きさを変えられません: {e}"))
}

/// 閉じる。殻を止めて表から外す（端末も閉じる）。
#[tauri::command]
pub fn local_shell_close(id: u64) -> Result<(), String> {
    let ent = sessions().lock().map_err(|_| "シェルの表が壊れています")?.remove(&id);
    if let Some(mut s) = ent {
        let _ = s.killer.kill();
    }
    Ok(())
}

// ── 別の窓のシェル（2026-11-17 オーナー「シェル機能はアプリと分離して表示できるようにしておくといいかもしれませんね」）──
// 窓ごとに新しい殻（ui/shellwin.html が local_shell_open で開く・閉じると殻も止める）。始めるフォルダは窓の名前で預け、
// 窓が開いたら shell_window_cwd で取りに来る（窓が出来上がる前に送った事象は届かない＝filepreview の viewer_pending と同じ）。
// ★画面から渡せるのはフォルダだけ（local_shell_open と同じ）。動かすプログラムはここでは決めない。
static WIN_N: AtomicU64 = AtomicU64::new(1);

fn win_cwd() -> &'static Mutex<HashMap<String, String>> {
    static S: OnceLock<Mutex<HashMap<String, String>>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(HashMap::new()))
}

#[tauri::command]
pub async fn shell_window_open(app: AppHandle, cwd: Option<String>) -> Result<(), String> {
    let label = format!("shell-{}", WIN_N.fetch_add(1, Ordering::Relaxed));
    if let Some(c) = cwd.filter(|c| !c.trim().is_empty()) {
        win_cwd().lock().map_err(|_| "シェルの窓の表が壊れています")?.insert(label.clone(), c);
    }
    // ★非同期の命令の中で作る（同期の命令で窓を作ると Windows で固まる＝Tauri の注意書き）。
    tauri::WebviewWindowBuilder::new(&app, label, tauri::WebviewUrl::App("shellwin.html".into()))
        .title("L.A.P.S — シェル")
        .inner_size(960.0, 600.0)
        .min_inner_size(480.0, 280.0)
        .build()
        .map_err(|e| format!("窓を開けません: {e}"))?;
    Ok(())
}

/// 別の窓が開いたときに、預けてある始めるフォルダを取りに来る（1 回きり）。
#[tauri::command]
pub fn shell_window_cwd(window: tauri::WebviewWindow) -> Option<String> {
    win_cwd().lock().ok().and_then(|mut m| m.remove(window.label()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn size_is_clamped() {
        let s = size(0, 100_000);
        assert_eq!((s.cols, s.rows), (10, 500));
    }

    /// 本物の擬似端末で殻を起こし、打った命令の出力が読めること（Linux の試験機で）。
    #[cfg(not(windows))]
    #[test]
    fn real_pty_echoes() {
        let pty = native_pty_system();
        let pair = pty.openpty(size(80, 24)).unwrap();
        let mut c = CommandBuilder::new("/bin/sh");
        c.arg("-c");
        c.arg("echo bw-shell-ok");
        let mut child = pair.slave.spawn_command(c).unwrap();
        drop(pair.slave);
        let mut r = pair.master.try_clone_reader().unwrap();
        child.wait().unwrap();
        let mut out = String::new();
        let mut buf = [0u8; 256];
        while let Ok(n) = r.read(&mut buf) {
            if n == 0 {
                break;
            }
            out.push_str(&String::from_utf8_lossy(&buf[..n]));
            if out.contains("bw-shell-ok") {
                break;
            }
        }
        assert!(out.contains("bw-shell-ok"), "{out:?}");
    }
}
