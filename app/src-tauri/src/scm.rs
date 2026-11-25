//! ソース管理（2026-11-17 オーナー「VSCode のやつはよりコード管理とかがしやすくなるように」→ 案の 1〜3 に「進めていいです」）。
//!
//! 右のパネルの「変更」タブ・状態バーの変更の数とブランチ・Quick Open のファイルの一覧が使う。手本は VS Code のソース管理。
//! ★相手はいまのワークスペース（`workspace::current_root`）だけ。画面から場所は受け取らない（ほかのフォルダを触らせない）。
//! ★git はこの PC に入っている物を呼ぶ（Git for Windows）。無ければ `git: false` を返し、画面はそのことだけを出す。
//! ★ファイルの名前は必ず `--` の後ろに置き、`-` で始まる名前・`..`・絶対パスは断る（git の引数として読まれないように）。
use serde_json::{json, Value};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Output, Stdio};

const MAX_ENTRIES: usize = 3000; // 変更の一覧の上限（無視されていない巨大なフォルダが 1 つあるだけで数万になる）
const MAX_DIFF: usize = 400_000; // 差分の上限（バイト）
const MAX_FILES: usize = 20_000; // Quick Open のファイルの上限

fn git_missing(e: &std::io::Error) -> bool {
    e.kind() == std::io::ErrorKind::NotFound
}

fn git_cmd(dir: &Path) -> Command {
    let mut c = Command::new("git");
    c.arg("-C")
        .arg(dir)
        .args(["-c", "core.quotepath=false", "-c", "color.ui=false", "-c", "core.pager=cat"])
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env("LC_ALL", "C")
        .stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW（黒い窓を出さない）
    }
    c
}

/// git を走らせる。起動できない（入っていない）ときは `Err(None)`、失敗したときは `Err(Some(理由))`。
fn run(dir: &Path, args: &[&str]) -> Result<Output, Option<String>> {
    let out = git_cmd(dir).args(args).output().map_err(|e| {
        if git_missing(&e) {
            None
        } else {
            Some(format!("git を起動できません: {e}"))
        }
    })?;
    if out.status.success() {
        Ok(out)
    } else {
        let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
        let msg = if msg.is_empty() { String::from_utf8_lossy(&out.stdout).trim().to_string() } else { msg };
        Err(Some(if msg.is_empty() { format!("git {} が失敗しました", args.first().unwrap_or(&"")) } else { msg }))
    }
}

fn no_git() -> String {
    "git が見つかりません（Git for Windows をインストールして、コマンドとして呼べるようにしてください）".to_string()
}

fn err_text(e: Option<String>) -> String {
    e.unwrap_or_else(no_git)
}

/// ワークスペースと、その上の git のリポジトリの一番上。リポジトリでなければ `Ok((ws, None))`。
fn repo() -> Result<(PathBuf, Option<PathBuf>), String> {
    let ws = crate::workspace::current_root()?;
    match run(&ws, &["rev-parse", "--show-toplevel"]) {
        Ok(o) => {
            let top = String::from_utf8_lossy(&o.stdout).trim().to_string();
            Ok((ws, if top.is_empty() { None } else { Some(PathBuf::from(top)) }))
        }
        Err(None) => Err(no_git()),
        Err(Some(_)) => Ok((ws, None)),
    }
}

fn top_or_err() -> Result<PathBuf, String> {
    match repo()? {
        (_, Some(top)) => Ok(top),
        (_, None) => Err("このワークスペースは Git のリポジトリではありません".into()),
    }
}

/// 画面から来たファイルの名前（リポジトリの一番上からの相対）を確かめる。
fn safe_rel(p: &str) -> Result<String, String> {
    let s = p.trim().replace('\\', "/");
    if s.is_empty() || s.starts_with('-') || s.contains('\0') {
        return Err(format!("ファイルの名前が不正です: {p}"));
    }
    let path = Path::new(&s);
    if path.is_absolute() || path.components().any(|c| !matches!(c, Component::Normal(_))) {
        return Err(format!("リポジトリの外は指せません: {p}"));
    }
    Ok(s)
}

/// `git status --porcelain=v1 -z --branch` を読む。`## main...origin/main [ahead 1, behind 2]` と `XY 名前`（R/C は元の名前が続く）。
fn parse_status(raw: &str) -> (Value, Vec<Value>, bool) {
    let mut head = json!({ "branch": "", "upstream": "", "ahead": 0, "behind": 0, "detached": false });
    let mut files = Vec::new();
    let mut truncated = false;
    let mut it = raw.split('\0').filter(|s| !s.is_empty());
    while let Some(tok) = it.next() {
        if let Some(h) = tok.strip_prefix("## ") {
            let (names, counts) = match h.find(" [") {
                Some(i) => (&h[..i], &h[i + 2..h.len().saturating_sub(1)]),
                None => (h, ""),
            };
            let (branch, up) = names.split_once("...").unwrap_or((names, ""));
            let branch = branch.strip_prefix("No commits yet on ").unwrap_or(branch);
            head["detached"] = json!(branch.starts_with("HEAD (no branch)"));
            head["branch"] = json!(branch);
            head["upstream"] = json!(up);
            for part in counts.split(", ") {
                if let Some(n) = part.strip_prefix("ahead ") {
                    head["ahead"] = json!(n.parse::<u64>().unwrap_or(0));
                } else if let Some(n) = part.strip_prefix("behind ") {
                    head["behind"] = json!(n.parse::<u64>().unwrap_or(0));
                }
            }
            continue;
        }
        if tok.len() < 4 {
            continue;
        }
        let x = &tok[0..1];
        let y = &tok[1..2];
        let path = tok[3..].to_string();
        let orig = if x == "R" || x == "C" || y == "R" || y == "C" { it.next().map(str::to_string) } else { None };
        if files.len() >= MAX_ENTRIES {
            truncated = true;
            continue;
        }
        files.push(json!({ "path": path, "orig": orig, "x": x, "y": y }));
    }
    (head, files, truncated)
}

/// 変更の一覧とブランチ。リポジトリでない・git が無いときも Ok で返す（画面がそのことを出す）。
#[tauri::command]
pub async fn scm_status() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let (ws, top) = match repo() {
            Ok(r) => r,
            Err(e) if e == no_git() => return Ok(json!({ "git": false, "error": e })),
            Err(e) => return Err(e),
        };
        let Some(top) = top else {
            return Ok(json!({ "git": true, "repo": false, "ws": ws.display().to_string() }));
        };
        let out = run(&top, &["status", "--porcelain=v1", "-z", "--branch", "--untracked-files=all"]).map_err(err_text)?;
        let (head, files, truncated) = parse_status(&String::from_utf8_lossy(&out.stdout));
        Ok(json!({
            "git": true, "repo": true, "ws": ws.display().to_string(), "top": top.display().to_string(),
            "head": head, "files": files, "truncated": truncated,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// 1 つのファイルの差分。`staged` は index と HEAD・そうでなければ作業中と index。追跡していないファイルは中身をそのまま返す。
#[tauri::command]
pub async fn scm_diff(path: String, staged: bool, untracked: bool) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let top = top_or_err()?;
        let rel = safe_rel(&path)?;
        if untracked {
            let full = top.join(&rel);
            let canon = std::fs::canonicalize(&full).map_err(|_| format!("見つかりません: {rel}"))?;
            let ctop = std::fs::canonicalize(&top).map_err(|e| e.to_string())?;
            if !canon.starts_with(&ctop) {
                return Err(format!("リポジトリの外は読めません: {rel}"));
            }
            let bytes = std::fs::read(&canon).map_err(|e| format!("{rel} を読めません: {e}"))?;
            let binary = bytes.iter().take(8000).any(|b| *b == 0);
            let truncated = bytes.len() > MAX_DIFF;
            let text = if binary { String::new() } else { String::from_utf8_lossy(&bytes[..bytes.len().min(MAX_DIFF)]).to_string() };
            return Ok(json!({ "untracked": true, "binary": binary, "truncated": truncated, "content": text }));
        }
        let mut args = vec!["diff", "--no-ext-diff", "--no-color", "-M"];
        if staged {
            args.push("--cached");
        }
        args.push("--");
        args.push(&rel);
        let out = run(&top, &args).map_err(err_text)?;
        let truncated = out.stdout.len() > MAX_DIFF;
        let text = String::from_utf8_lossy(&out.stdout[..out.stdout.len().min(MAX_DIFF)]).to_string();
        let binary = text.lines().any(|l| l.starts_with("Binary files "));
        Ok(json!({ "untracked": false, "binary": binary, "truncated": truncated, "diff": text }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// 差分の元の中身（`rev` が "HEAD" なら最後のコミット、"INDEX" ならステージ済みの中身）。無ければ exists=false（新しいファイル）。
/// エディターの差分（Monaco の差分エディター）が左側に出す。
#[tauri::command]
pub async fn scm_show(path: String, rev: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let top = top_or_err()?;
        let rel = safe_rel(&path)?;
        let spec = match rev.as_str() {
            "HEAD" => format!("HEAD:{rel}"),
            "INDEX" => format!(":{rel}"),
            _ => return Err(format!("rev が不正です: {rev}")),
        };
        match run(&top, &["show", &spec]) {
            Ok(o) => {
                let binary = o.stdout.iter().take(8000).any(|b| *b == 0);
                let truncated = o.stdout.len() > 5 * 1024 * 1024;
                let text = if binary { String::new() } else { String::from_utf8_lossy(&o.stdout[..o.stdout.len().min(5 * 1024 * 1024)]).to_string() };
                Ok(json!({ "exists": true, "binary": binary, "truncated": truncated, "text": text }))
            }
            Err(None) => Err(no_git()),
            Err(Some(_)) => Ok(json!({ "exists": false, "binary": false, "truncated": false, "text": "" })),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// ステージする（`stage`）・ステージを外す。
#[tauri::command]
pub async fn scm_stage(paths: Vec<String>, stage: bool) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let top = top_or_err()?;
        let rels = paths.iter().map(|p| safe_rel(p)).collect::<Result<Vec<_>, _>>()?;
        if rels.is_empty() {
            return Ok(());
        }
        let mut args: Vec<&str> = if stage { vec!["add", "-A", "--"] } else { vec!["restore", "--staged", "--"] };
        args.extend(rels.iter().map(String::as_str));
        match run(&top, &args) {
            Ok(_) => Ok(()),
            Err(None) => Err(no_git()),
            // ★まだ 1 度もコミットしていないリポジトリでは restore --staged が使えない（HEAD が無い）。そのときは index から外す。
            Err(Some(_)) if !stage => {
                let mut a: Vec<&str> = vec!["rm", "-q", "--cached", "--"];
                a.extend(rels.iter().map(String::as_str));
                run(&top, &a).map(|_| ()).map_err(err_text)
            }
            Err(Some(e)) => Err(e),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// 変更を破棄する。`kind` が "untracked"（追跡していない）・"added"（index に足しただけ）ならファイルを消す。
/// "worktree" は作業中の変更だけを index の中身へ戻す。それ以外は HEAD の中身へ戻す（ステージも外す）。★元に戻せないので、画面が 2 回押させてから呼ぶ。
#[tauri::command]
pub async fn scm_discard(path: String, kind: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let top = top_or_err()?;
        let rel = safe_rel(&path)?;
        let remove = |rel: &str| -> Result<(), String> {
            let full = top.join(rel);
            let canon = std::fs::canonicalize(&full).map_err(|_| format!("見つかりません: {rel}"))?;
            let ctop = std::fs::canonicalize(&top).map_err(|e| e.to_string())?;
            if !canon.starts_with(&ctop) || canon.is_dir() {
                return Err(format!("消せません: {rel}"));
            }
            std::fs::remove_file(&canon).map_err(|e| format!("{rel} を消せません: {e}"))
        };
        match kind.as_str() {
            "untracked" => remove(&rel),
            "added" => {
                run(&top, &["rm", "-q", "--cached", "--", &rel]).map_err(err_text)?;
                remove(&rel)
            }
            // 作業中の変更だけを捨てる（index の中身へ戻す＝ステージ済みの分は残る）。「変更」の欄の破棄はこれ。
            "worktree" => run(&top, &["restore", "--worktree", "--", &rel]).map(|_| ()).map_err(err_text),
            _ => run(&top, &["restore", "--source=HEAD", "--staged", "--worktree", "--", &rel]).map(|_| ()).map_err(err_text),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// コミットする。`all` なら先に全部ステージする（ステージ済みが無いときの「すべてコミット」）。
#[tauri::command]
pub async fn scm_commit(message: String, all: bool) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let top = top_or_err()?;
        let msg = message.trim().to_string();
        if msg.is_empty() {
            return Err("コミットのメッセージを入力してください".into());
        }
        if all {
            run(&top, &["add", "-A"]).map_err(err_text)?;
        }
        run(&top, &["commit", "-q", "-m", &msg]).map_err(err_text)?;
        let head = run(&top, &["rev-parse", "--short", "HEAD"]).map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
        Ok(json!({ "head": head }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// ローカルのブランチの一覧といまのブランチ。
#[tauri::command]
pub async fn scm_branches() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let top = top_or_err()?;
        let out = run(&top, &["branch", "--format=%(refname:short)"]).map_err(err_text)?;
        let list: Vec<String> = String::from_utf8_lossy(&out.stdout).lines().map(str::trim).filter(|l| !l.is_empty()).map(str::to_string).collect();
        let cur = run(&top, &["branch", "--show-current"]).map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
        Ok(json!({ "current": cur, "branches": list }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// ブランチを切り替える（git switch）。作業中の変更とぶつかるときは git が断るので、その理由をそのまま返す。
#[tauri::command]
pub async fn scm_switch(branch: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let top = top_or_err()?;
        let b = branch.trim();
        if b.is_empty() || b.starts_with('-') || b.chars().any(|c| c.is_control() || c == ' ') {
            return Err(format!("ブランチの名前が不正です: {branch}"));
        }
        run(&top, &["switch", b]).map(|_| ()).map_err(err_text)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// エクスプローラーの 1 階層（2026-11-21 オーナー「ファイルが多いので一部だけ…を改善」）。開いたフォルダだけを読む＝全体の上限が要らない。
/// 並びはフォルダが先・名前は大文字小文字を無視した自然な順（file2 < file10）。offset・limit で少しずつ返す（大きいフォルダの「さらに表示」）。
/// 各項目: name・rel・dir・size・mtime（エポック秒）・items（フォルダの中の項目の数・10,000 で打ち切り）・ignored（git が無視する物）・link。
#[tauri::command]
pub async fn ws_dir(rel: Option<String>, offset: Option<usize>, limit: Option<usize>) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let rel = rel.unwrap_or_default().trim().trim_matches('/').replace('\\', "/");
        let (base, dir) = crate::workspace::resolve_rel(&rel)?;
        let rd = std::fs::read_dir(&dir).map_err(|e| format!("{} を開けません: {e}", if rel.is_empty() { "ワークスペース" } else { &rel }))?;
        struct E { name: String, rel: String, dir: bool, size: u64, mtime: Option<u64>, link: bool }
        let mut all: Vec<E> = Vec::new();
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if name == ".git" {
                continue;
            }
            let crel = if rel.is_empty() { name.clone() } else { format!("{rel}/{name}") };
            let link = e.file_type().map(|t| t.is_symlink()).unwrap_or(false);
            // リンクの先が外ならたどらない（項目としては出す・開くときに ws の決まりが止める）
            let md = if link && crate::workspace::resolve_rel(&crel).is_err() { e.metadata().ok() } else { std::fs::metadata(e.path()).ok() };
            let (isdir, size, mtime) = match md {
                Some(m) => (m.is_dir(), if m.is_dir() { 0 } else { m.len() },
                            m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs())),
                None => (false, 0, None),
            };
            all.push(E { name, rel: crel, dir: isdir, size, mtime, link });
        }
        all.sort_by(|a, b| b.dir.cmp(&a.dir).then_with(|| natural_cmp(&a.name, &b.name)));
        let total = all.len();
        let off = offset.unwrap_or(0).min(total);
        let lim = limit.unwrap_or(500).clamp(1, 5000);
        let page: Vec<&E> = all.iter().skip(off).take(lim).collect();
        // git が無視する物（薄く出す）。リポジトリでなければ空。
        let mut ignored = std::collections::HashSet::new();
        if !page.is_empty() {
            let mut input = String::new();
            for e in &page {
                input.push_str(&e.rel);
                if e.dir { input.push('/'); }
                input.push('\0');
            }
            let mut c = git_cmd(&base);
            c.args(["check-ignore", "-z", "--stdin"]).stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null());
            if let Ok(mut ch) = c.spawn() {
                use std::io::Write;
                if let Some(mut si) = ch.stdin.take() { let _ = si.write_all(input.as_bytes()); }
                if let Ok(o) = ch.wait_with_output() {
                    for p in String::from_utf8_lossy(&o.stdout).split('\0').filter(|x| !x.is_empty()) {
                        ignored.insert(p.trim_end_matches('/').to_string());
                    }
                }
            }
        }
        let rows: Vec<Value> = page.iter().map(|e| {
            let items = if e.dir {
                std::fs::read_dir(base.join(&e.rel)).ok().map(|r| r.take(10_000).count())
            } else { None };
            json!({ "name": e.name, "rel": e.rel, "dir": e.dir, "size": if e.dir { Value::Null } else { json!(e.size) },
                    "mtime": e.mtime, "items": items, "ignored": ignored.contains(&e.rel), "link": e.link })
        }).collect();
        Ok(json!({ "rel": rel, "entries": rows, "total": total, "offset": off, "more": off + rows.len() < total }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// 大文字小文字を無視した自然な順（数字の並びは数として比べる）。
fn natural_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    let (a, b) = (a.to_lowercase(), b.to_lowercase());
    let (mut x, mut y) = (a.chars().peekable(), b.chars().peekable());
    loop {
        match (x.peek().copied(), y.peek().copied()) {
            (None, None) => return Ordering::Equal,
            (None, _) => return Ordering::Less,
            (_, None) => return Ordering::Greater,
            (Some(c1), Some(c2)) if c1.is_ascii_digit() && c2.is_ascii_digit() => {
                let mut n1 = String::new(); while let Some(c) = x.peek().copied() { if c.is_ascii_digit() { n1.push(c); x.next(); } else { break; } }
                let mut n2 = String::new(); while let Some(c) = y.peek().copied() { if c.is_ascii_digit() { n2.push(c); y.next(); } else { break; } }
                let (t1, t2) = (n1.trim_start_matches('0'), n2.trim_start_matches('0'));
                let o = t1.len().cmp(&t2.len()).then_with(|| t1.cmp(t2));
                if o != Ordering::Equal { return o; }
            }
            (Some(c1), Some(c2)) => {
                if c1 != c2 { return c1.cmp(&c2); }
                x.next(); y.next();
            }
        }
    }
}

/// エクスプローラーの大きさと更新時刻（2026-11-20 オーナー「ファイルやフォルダにタイムスタンプ情報と容量も」）。
/// files と同じ並びで [大きさ（バイト）, 更新時刻（エポック秒）]。読めない物（消えた・権限が無い）は null。
/// フォルダの値は画面の側で、中のファイルから足す（無視されたファイルは数えない）。
fn file_meta(ws: &std::path::Path, files: &[String]) -> Vec<Value> {
    files
        .iter()
        .map(|rel| match std::fs::metadata(ws.join(rel)) {
            Ok(m) => {
                let t = m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs());
                json!([m.len(), t])
            }
            Err(_) => Value::Null,
        })
        .collect()
}

/// Quick Open のためのワークスペースのファイルの一覧（ワークスペースからの相対）。リポジトリなら git の一覧（無視したファイルは出さない）、
/// そうでなければフォルダをたどる（.git・node_modules・target・.venv などは見ない）。
#[tauri::command]
pub async fn ws_files() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let ws = crate::workspace::current_root()?;
        if let Ok(o) = run(&ws, &["ls-files", "-co", "--exclude-standard", "-z"]) {
            let all: Vec<String> = String::from_utf8_lossy(&o.stdout).split('\0').filter(|s| !s.is_empty()).map(str::to_string).collect();
            let truncated = all.len() > MAX_FILES;
            let files: Vec<String> = all.into_iter().take(MAX_FILES).collect();
            let meta = file_meta(&ws, &files);
            return Ok(json!({ "ws": ws.display().to_string(), "files": files, "meta": meta, "truncated": truncated }));
        }
        const SKIP: [&str; 9] = [".git", "node_modules", "target", ".venv", "venv", "__pycache__", "dist", "build", ".next"];
        let mut out = Vec::new();
        let mut stack = vec![ws.clone()];
        let mut truncated = false;
        while let Some(d) = stack.pop() {
            let Ok(rd) = std::fs::read_dir(&d) else { continue };
            for e in rd.flatten() {
                let name = e.file_name().to_string_lossy().to_string();
                let Ok(ft) = e.file_type() else { continue };
                if ft.is_dir() {
                    if !SKIP.contains(&name.as_str()) {
                        stack.push(e.path());
                    }
                } else if ft.is_file() {
                    if out.len() >= 5000 {
                        truncated = true;
                        break;
                    }
                    if let Ok(rel) = e.path().strip_prefix(&ws) {
                        out.push(rel.to_string_lossy().replace('\\', "/"));
                    }
                }
            }
            if truncated {
                break;
            }
        }
        out.sort();
        let meta = file_meta(&ws, &out);
        Ok(json!({ "ws": ws.display().to_string(), "files": out, "meta": meta, "truncated": truncated }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// エクスプローラー（ソース管理・フォルダー）を別のウィンドウで開く（2026-11-17 オーナー「変更とファイルを統一して VSCode みたいにして、
/// ポップアウトできるように」）。1 つだけ（開いていれば前に出す）。中身は ui/explorer.html（explorer.js を独りで動かす）。
#[tauri::command]
pub async fn explorer_window_open(app: tauri::AppHandle) -> Result<(), String> {
    use tauri::Manager;
    if let Some(w) = app.get_webview_window("explorer") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        return Ok(());
    }
    // ★非同期の命令の中で作る（同期の命令で窓を作ると Windows で固まる＝Tauri の注意書き）。
    tauri::WebviewWindowBuilder::new(&app, "explorer", tauri::WebviewUrl::App("explorer.html".into()))
        .title("L.A.P.S — エクスプローラー")
        .inner_size(460.0, 820.0)
        .min_inner_size(320.0, 360.0)
        .build()
        .map_err(|e| format!("ウィンドウを開けません: {e}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_parses_branch_counts_and_renames() {
        let raw = "## main...origin/main [ahead 2, behind 1]\0 M app/a.js\0R  new.txt\0old.txt\0?? notes.md\0A  b.rs\0";
        let (head, files, truncated) = parse_status(raw);
        assert_eq!(head["branch"], "main");
        assert_eq!(head["upstream"], "origin/main");
        assert_eq!(head["ahead"], 2);
        assert_eq!(head["behind"], 1);
        assert!(!truncated);
        assert_eq!(files.len(), 4);
        assert_eq!(files[0]["path"], "app/a.js");
        assert_eq!(files[0]["y"], "M");
        assert_eq!(files[1]["path"], "new.txt");
        assert_eq!(files[1]["orig"], "old.txt");
        assert_eq!(files[2]["x"], "?");
        assert_eq!(files[3]["x"], "A");
    }

    #[test]
    fn status_without_upstream_or_commits() {
        let (head, files, _) = parse_status("## No commits yet on main\0?? a\0");
        assert_eq!(head["branch"], "main");
        assert_eq!(head["upstream"], "");
        assert_eq!(files.len(), 1);
    }

    #[test]
    fn unsafe_names_are_refused() {
        assert!(safe_rel("-rf").is_err());
        assert!(safe_rel("../x").is_err());
        assert!(safe_rel("a/../../x").is_err());
        assert!(safe_rel("/etc/passwd").is_err());
        assert!(safe_rel("").is_err());
        assert_eq!(safe_rel("app\\ui\\a.js").unwrap(), "app/ui/a.js");
        assert_eq!(safe_rel("src/main.rs").unwrap(), "src/main.rs");
    }
}
