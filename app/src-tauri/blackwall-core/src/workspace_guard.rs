//! 作業ディレクトリの封じ込め（[`resolve_in_ws`]）と、その上に乗る一覧・読み・書き。
//!
//! ★なぜ本体（`src/workspace.rs`）ではなくここに置くか（2026-09-05・r4/tests-never-run）:
//!   `workspace.rs` は `tauri`（`AppHandle`・`#[tauri::command]`）を参照するので
//!   ホスト（Linux）では `cargo test` が**リンクできない**（GTK 依存 pango が無い）。
//!   つまりここにあった 38 本の試験のうち、封じ込め・一覧・読み・書きに関わる 33 本は
//!   preflight でも `cargo check --tests`＝**コンパイルされるだけで一度も実行
//!   されていなかった**。0.1.191 で直した workspace guard の隣接コード――
//!   シンボリックリンク・8.3 短縮名・ADS・予約デバイス名・末尾ドット/空白の回避を
//!   実際に検査する分がこれである。
//!   `workspace.rs` は `#[path]` で参照するだけ＝実体は1本・二重管理は起きない
//!   （`wire.rs` / `translate.rs` と同じ作法）。
//!
//!   作業ディレクトリの**選択・保存**（`WsState`・`config_path`・`adopt_root` 等）は
//!   `crate::app_root()`（Tauri 側のアプリ設定ディレクトリ解決）に依存するので、
//!   こちらには移していない。ここに置くのは「`root: &Path` を明示的に受け取り、
//!   自分では永続状態を持たない」部分だけ――そしてそここそが、パス脱出を防ぐ
//!   本体（封じ込め）である。
//!
//! ★ 残る穴を正直に書いておく（元の doc comment から）: 検査してから開くまでの間に
//! 相手を差し替える競合（TOCTOU）はここでは塞げない。ハンドルを握ったまま検査する
//! API が要る。実害を出すには作業ディレクトリ内に既に書き込み権が要るので、当面は許容する。

use std::ffi::{OsStr, OsString};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::Serialize;

// ---------------------------------------------------------------------------
// 定数
// ---------------------------------------------------------------------------

/// ws_read が返す最大バイト数。
///
/// 上限を置く理由は2つ。①数 MB のログを丸ごと文脈に流し込むと、モデルの窓が一撃で埋まる。
/// ②巨大ファイルの読み込みで画面が固まる。256KB は「ソースファイルなら確実に全部入る」大きさ。
pub const READ_LIMIT: usize = 256 * 1024;

/// ws_list が返す最大件数。超えた分は捨てて、捨てたことを末尾のお知らせ行で伝える。
pub const LIST_LIMIT: usize = 500;

/// read_dir を舐める上限。数十万件のフォルダを選ばれても戻ってこられるようにする保険。
pub const SCAN_CAP: usize = 20_000;

/// 一覧から隠す名前。数千件を返して画面と文脈を潰さないため。
///
/// 種別（ファイル/フォルダ）を見ずに名前だけで判定する。`.git` は worktree だとファイルなので、
/// 「フォルダなら隠す」にすると片方が漏れる。
pub const HIDDEN_NAMES: &[&str] = &[".git", "node_modules", "target", "__pycache__", ".venv"];

/// お知らせ行と、切り詰め/文字化けの注記に付ける印。
///
/// ★これはこちらが足した文字列で、**ファイルの中身が同じ行を騙ることは可能**。
/// 真偽の判定に使わないこと（使えるのは「人が状況を察する」ためだけ）。
pub const NOTICE_TAG: &str = "[BLACKWALL]";

// ---------------------------------------------------------------------------
// 画面へ返す形
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct WsEntry {
    pub name: String,
    /// 作業ディレクトリからの相対パス。区切りは `/` に正規化してある。
    ///
    /// ★**空文字はお知らせ行**（切り詰めた・除外したの通知）であって実体ではない。
    /// 画面側は `rel.is_empty()` で見分けて、開ける項目として扱わないこと。
    pub rel: String,
    pub is_dir: bool,
    pub size: u64,
}

pub fn notice_entry(text: String) -> WsEntry {
    WsEntry { name: text, rel: String::new(), is_dir: false, size: 0 }
}

// ---------------------------------------------------------------------------
// パスの表示
// ---------------------------------------------------------------------------

/// 人に見せる形。`canonicalize` が付ける Windows の verbatim 接頭辞を落とす。
///
/// `\\?\C:\x` のまま画面に出すと「壊れている」と読まれるし、利用者がそれをコピーして別の場所に
/// 貼ったときに動かない。内部で使うパスは正準形のまま持ち回る（長いパスに強いので落とさない）。
pub fn display_path(p: &Path) -> String {
    let s = p.to_string_lossy().into_owned();
    #[cfg(windows)]
    {
        if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
            return format!(r"\\{rest}");
        }
        if let Some(rest) = s.strip_prefix(r"\\?\") {
            return rest.to_string();
        }
    }
    s
}

// ---------------------------------------------------------------------------
// ★封じ込め
// ---------------------------------------------------------------------------

/// パスの構成要素を比べる。
///
/// Windows はファイルシステムが大文字小文字を区別しないので、比較も区別しない。ここを区別すると
/// 「同じフォルダなのに外だと判定する」誤拒否が出る。逆に Linux では区別しないと `/a/B` の中だと
/// 言って `/a/b` を開いてしまう。
///
/// 比較は `to_string_lossy()` ではなく `as_encoded_bytes()` で行う。Windows の名前は UTF-16 で、
/// lossy 変換は不正なサロゲートを U+FFFD に潰す——別々の名前が同じ文字列になり、そこが誤許可になる。
pub fn component_eq(a: &OsStr, b: &OsStr) -> bool {
    let (x, y) = (a.as_encoded_bytes(), b.as_encoded_bytes());
    if cfg!(windows) {
        x.eq_ignore_ascii_case(y)
    } else {
        x == y
    }
}

/// `p` が `root` と同じか、その下にあるか。**両方が正準形であることを前提にする。**
///
/// 文字列の `starts_with` は使えない: `/home/user/ws` に対して `/home/user/ws-evil` が
/// 前方一致してしまう。構成要素の単位で比べる。
pub fn is_within(root: &Path, p: &Path) -> bool {
    let mut rc = root.components();
    let mut pc = p.components();
    loop {
        match rc.next() {
            None => return true, // root を全部使い切った＝p は root 以下
            Some(r) => match pc.next() {
                None => return false, // p の方が短い
                Some(c) => {
                    if !component_eq(r.as_os_str(), c.as_os_str()) {
                        return false;
                    }
                }
            },
        }
    }
}

/// 相対パスとして受け付けられる形か検査し、構成要素に割る。
///
/// OS に触れずに落とせるものはここで落とす。実体を見る前に落とした方が、理由をはっきり書けるし、
/// 存在しないパスに対しても同じ判定になる（「存在するかどうかで挙動が変わる」を作らない）。
///
/// 区切りは `/` と `\` の両方を受ける。Windows はどちらも区切りとして扱うので、`\` を素通しすると
/// Linux 上の試験だけ通って実機で破られる。
pub fn split_relative(rel: &str) -> Result<Vec<&str>, String> {
    if rel.contains('\0') {
        return Err("パスに NUL 文字が入っています（受け付けません）".to_string());
    }
    // ★入力を trim しない。ここで前後の空白を落とすと、末尾が空白の名前（Windows が黙って
    //   別名に落とす形）を下の検査が見られなくなる——実際にそれで検査をすり抜けた。
    //   代わりに制御文字を全部拒否する。改行入りのパスを受けて黙って直すより、
    //   何が悪いか言って呼び出し側に直させる方が、後で辿れる。
    if let Some(c) = rel.chars().find(|c| c.is_control()) {
        return Err(format!(
            "パスに制御文字（U+{:04X}・改行やタブなど）が入っています。前後の余分な文字を落としてから渡してください",
            c as u32
        ));
    }
    let t = rel;

    if t.starts_with('/') || t.starts_with('\\') {
        return Err(format!(
            "`{t}` は絶対パス（または UNC）です。作業ディレクトリからの相対パスで指定してください"
        ));
    }
    // `C:\x` も `C:x`（ドライブ相対）も、作業ディレクトリの外を指し得る。
    let b = t.as_bytes();
    if b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':' {
        return Err(format!(
            "`{t}` はドライブ名から始まっています。作業ディレクトリからの相対パスで指定してください"
        ));
    }

    let mut out = Vec::new();
    for seg in t.split(['/', '\\']) {
        if seg.is_empty() || seg == "." {
            continue; // `a//b` や `./a` や末尾の `/` は無害なので詰めるだけ
        }
        if seg == ".." {
            return Err(format!(
                "`{t}` に `..` が入っています。作業ディレクトリの外は読み書きできません"
            ));
        }
        if seg.contains(':') {
            // Windows の代替データストリーム（`a.txt:hidden`）。実体はファイルの外側に書ける。
            return Err(format!("`{seg}` に `:` が入っています（受け付けません）"));
        }
        if seg.ends_with('.') || seg.ends_with(' ') {
            // Windows は末尾のドットと空白を黙って落とす＝要求した名前と別の場所に着地する。
            return Err(format!("`{seg}` は末尾がドットまたは空白です（別の名前になるため拒否します）"));
        }
        if is_reserved_device_name(seg) {
            return Err(format!(
                "`{seg}` は Windows の予約デバイス名です（ファイルではなく装置を開いてしまいます）"
            ));
        }
        out.push(seg);
    }
    Ok(out)
}

/// `CON` `PRN` `AUX` `NUL` `COM0`〜`COM9` `LPT0`〜`LPT9`。拡張子が付いていても予約は生きている。
///
/// Windows 以外でも同じ判定をする。片方だけ緩いと、Linux 上の試験が実機の危険を見逃す。
pub fn is_reserved_device_name(seg: &str) -> bool {
    let stem = seg.split('.').next().unwrap_or(seg);
    let up = stem.to_ascii_uppercase();
    if matches!(up.as_str(), "CON" | "PRN" | "AUX" | "NUL" | "CLOCK$") {
        return true;
    }
    let bytes = up.as_bytes();
    bytes.len() == 4
        && (up.starts_with("COM") || up.starts_with("LPT"))
        && bytes[3].is_ascii_digit()
}

/// 作業ディレクトリを正準化する。比較の土台なので、必ずここを通してから使う。
pub fn canonicalize_root(root: &Path) -> Result<PathBuf, String> {
    let canon = std::fs::canonicalize(root)
        .map_err(|e| format!("作業ディレクトリを開けません {}: {e}", display_path(root)))?;
    if !canon.is_dir() {
        return Err(format!("{} はフォルダではありません", display_path(&canon)));
    }
    Ok(canon)
}

/// ★本体。`root` の中に閉じた実パスを返す。外に出る経路はここで全部塞ぐ。
///
/// 手順:
///   1. 文字列として不正なものを落とす（[`split_relative`]）
///   2. `root` を正準化する
///   3. 存在する一番深い祖先まで遡って正準化し、`root` の下にいるか確かめる
///   4. まだ存在しない残りの構成要素を継ぎ足して返す
///
/// 3 が要点。まだ無いファイルは canonicalize できないが、**親までは実体があるので追跡できる**。
/// 親が作業ディレクトリの外を指すリンクなら、その時点で分かる。
/// また存在するものについては canonicalize がリンクも 8.3 名も解決するので、
/// 「見た目は中、実体は外」がここで必ず露見する。
///
/// 返すのは正準形を土台にしたパス。呼び出し側が元の文字列を組み直すと、検査したものと
/// 開くものが食い違う（そこが穴になる）。
pub fn resolve_in_ws(root: &Path, rel: &str) -> Result<PathBuf, String> {
    let segments = split_relative(rel)?;
    let root_canon = canonicalize_root(root)?;

    let mut candidate = root_canon.clone();
    for s in &segments {
        candidate.push(s);
    }

    // 存在する一番深い祖先を探す。symlink_metadata を使うのは、壊れたリンク（先が無い）も
    // 「存在する」と見なして次の canonicalize で明示的に失敗させたいため。
    let mut existing = candidate.clone();
    let mut pending: Vec<OsString> = Vec::new();
    while std::fs::symlink_metadata(&existing).is_err() {
        let Some(name) = existing.file_name().map(|n| n.to_os_string()) else {
            // 根まで遡っても見つからない＝作業ディレクトリごと消えている。
            return Err(format!(
                "作業ディレクトリが見つかりません（{}）。選び直してください",
                display_path(&root_canon)
            ));
        };
        pending.push(name);
        match existing.parent() {
            Some(p) => existing = p.to_path_buf(),
            None => {
                return Err(format!(
                    "作業ディレクトリが見つかりません（{}）。選び直してください",
                    display_path(&root_canon)
                ))
            }
        }
    }

    let existing_canon = std::fs::canonicalize(&existing).map_err(|e| {
        // 壊れたリンクはここに来る。「無い」と言い切らずに理由を出す。
        format!("{} の実体をたどれません: {e}", display_path(&existing))
    })?;

    if !is_within(&root_canon, &existing_canon) {
        return Err(format!(
            "`{rel}` は作業ディレクトリ（{}）の外を指しています（実体は {}）。読み書きできません",
            display_path(&root_canon),
            display_path(&existing_canon)
        ));
    }

    // まだ存在しない部分を継ぎ足す。これらは split_relative を通った素の名前なので、
    // 継ぎ足しても外へは出られない（`..` も区切りも含まない）。
    let mut out = existing_canon;
    for name in pending.iter().rev() {
        out.push(name);
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// 原子的書き込み
// ---------------------------------------------------------------------------

/// temp へ書いてから rename で置き換える。
///
/// 途中で電源が落ちても、元のファイルが「中身が半分だけ」になることは無い。
/// ★宛先が既にあるときに宛先を先に消す実装にはしない（hf.rs はダウンロード中間物なので消しているが、
/// こちらは利用者のファイルを扱う。rename に失敗したら元を残したまま報告する方が損害が小さい）。
pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("{} の置き場所が決まりません", display_path(path)))?;
    std::fs::create_dir_all(parent)
        .map_err(|e| format!("{} を作成できません: {e}", display_path(parent)))?;

    // 同じファイルへの書き込みが同時に走っても temp がぶつからないようにする
    // （コマンドは invoke ごとに別タスクで動くので、同時に来ること自体はあり得る）。
    static TMP_SEQ: AtomicU64 = AtomicU64::new(0);
    let stem = path.file_name().unwrap_or_else(|| OsStr::new("ws")).to_string_lossy().into_owned();
    let tmp = parent.join(format!(
        ".{stem}.blackwall-tmp{}-{}",
        std::process::id(),
        TMP_SEQ.fetch_add(1, Ordering::Relaxed)
    ));

    // sync_all まで行う。rename は「temp の中身が確定した後」でなければ意味が無い。
    let write = (|| -> std::io::Result<()> {
        use std::io::Write;
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()
    })();
    if let Err(e) = write {
        let _ = std::fs::remove_file(&tmp);
        return Err(format!("{} に書けません: {e}", display_path(&tmp)));
    }

    if let Err(e) = std::fs::rename(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(format!(
            "{} を置き換えられません: {e}（他のプログラムが開いている可能性があります。元のファイルはそのままです）",
            display_path(path)
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// 一覧・読み・書き（root を引数で受ける形。commands は薄い皮）
// ---------------------------------------------------------------------------

pub fn list_in(root: &Path, rel: &str) -> Result<Vec<WsEntry>, String> {
    let root_canon = canonicalize_root(root)?;
    let segments = split_relative(rel)?;
    let dir = resolve_in_ws(&root_canon, rel)?;

    let md = std::fs::metadata(&dir)
        .map_err(|e| format!("{} を開けません: {e}", display_path(&dir)))?;
    if !md.is_dir() {
        return Err(format!("`{rel}` はフォルダではありません（ファイルは ws_read で読みます）"));
    }

    let prefix = segments.join("/");
    let rd = std::fs::read_dir(&dir)
        .map_err(|e| format!("{} の中を読めません: {e}", display_path(&dir)))?;

    let mut entries: Vec<WsEntry> = Vec::new();
    let mut scanned = 0usize;
    let mut hit_scan_cap = false;
    let mut excluded = 0usize; // 作業ディレクトリの外を指していたので外したもの

    for e in rd.flatten() {
        scanned += 1;
        if scanned > SCAN_CAP {
            hit_scan_cap = true;
            break;
        }
        let os_name = e.file_name();
        if HIDDEN_NAMES.iter().any(|h| component_eq(os_name.as_os_str(), OsStr::new(h))) {
            continue;
        }
        let name = os_name.to_string_lossy().into_owned();
        let child_rel = if prefix.is_empty() { name.clone() } else { format!("{prefix}/{name}") };

        // ★ここでもう一度封じ込めを通す。フォルダの中に外を指すリンクが混ざっていても、
        //   一覧に出した以上は開けると読まれる。開けないものは出さない（出した件数は下で伝える）。
        if resolve_in_ws(&root_canon, &child_rel).is_err() {
            excluded += 1;
            continue;
        }

        // 封じ込めを通った＝リンク先も中なので、追跡して大きさと種別を取る。
        let (is_dir, size) = match std::fs::metadata(e.path()) {
            Ok(m) => (m.is_dir(), m.len()),
            Err(_) => {
                excluded += 1;
                continue;
            }
        };
        entries.push(WsEntry { name, rel: child_rel, is_dir, size });
    }

    // フォルダを先に、次に名前順。大文字小文字は無視する（Windows の一覧と揃える）。
    entries.sort_by(|a, b| {
        b.is_dir
            .cmp(&a.is_dir)
            .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
            .then_with(|| a.name.cmp(&b.name))
    });

    let total = entries.len();
    let mut truncated = 0usize;
    if total > LIST_LIMIT {
        truncated = total - LIST_LIMIT;
        entries.truncate(LIST_LIMIT);
    }

    // 起きたことは全部言う。空の結果と「多すぎて出せなかった」を混同させない。
    let mut notes: Vec<String> = Vec::new();
    if truncated > 0 {
        notes.push(format!("他 {truncated} 件は表示上限 {LIST_LIMIT} 件を超えたので省きました"));
    }
    if hit_scan_cap {
        notes.push(format!("項目が {SCAN_CAP} 件を超えたので途中で読み止めました"));
    }
    if excluded > 0 {
        notes.push(format!("{excluded} 件は作業ディレクトリの外を指すか開けないため除きました"));
    }
    if !notes.is_empty() {
        entries.push(notice_entry(format!("{NOTICE_TAG} {}", notes.join(" / "))));
    }
    Ok(entries)
}

/// 切り詰めた末尾が UTF-8 の文字の途中なら、直前の境界まで戻す。
///
/// これをしないと、正しい UTF-8 のファイルを切っただけで「文字化けしている」と報告してしまう。
pub fn trim_to_char_boundary(b: &[u8]) -> &[u8] {
    let mut end = b.len();
    for _ in 0..3 {
        if std::str::from_utf8(&b[..end]).is_ok() {
            return &b[..end];
        }
        if end == 0 {
            break;
        }
        end -= 1;
    }
    b // 3 バイト戻して駄目なら、そもそも UTF-8 ではない。lossy に任せる。
}

pub fn read_text_in(root: &Path, rel: &str) -> Result<String, String> {
    let root_canon = canonicalize_root(root)?;
    if split_relative(rel)?.is_empty() {
        return Err("読むファイルを指定してください（作業ディレクトリ自身は読めません）".to_string());
    }
    let path = resolve_in_ws(&root_canon, rel)?;

    let md = std::fs::metadata(&path)
        .map_err(|e| format!("`{rel}` を開けません: {e}"))?;
    if md.is_dir() {
        return Err(format!("`{rel}` はフォルダです（中身の一覧は ws_list で見ます）"));
    }
    let total = md.len();

    // 上限 +1 バイトだけ読む。+1 が読めたら「まだ先がある」と判る。
    let cap = std::cmp::min(total.saturating_add(1), (READ_LIMIT + 1) as u64) as usize;
    let mut buf = Vec::with_capacity(cap);
    std::fs::File::open(&path)
        .map_err(|e| format!("`{rel}` を開けません: {e}"))?
        .take((READ_LIMIT + 1) as u64)
        .read_to_end(&mut buf)
        .map_err(|e| format!("`{rel}` を読めません: {e}"))?;

    let truncated = buf.len() > READ_LIMIT;
    let body: &[u8] = if truncated { trim_to_char_boundary(&buf[..READ_LIMIT]) } else { &buf };

    // NUL を含むものはテキストとして扱わない。文字化けした塊を返すと、上の層が
    // 「読めた」と誤解して、その化けた文字列を根拠に次の判断をしてしまう。
    if body.contains(&0) {
        return Err(format!(
            "`{rel}` はバイナリのようです（NUL バイトを含む・{total} バイト）。テキストファイルだけ読めます"
        ));
    }

    let (text, lossy) = match std::str::from_utf8(body) {
        Ok(s) => (s.to_string(), false),
        Err(_) => (String::from_utf8_lossy(body).into_owned(), true),
    };

    if !truncated && !lossy {
        return Ok(text); // 普通のファイルは中身をそのまま返す。余計な行を混ぜない。
    }

    let mut head = String::new();
    if truncated {
        head.push_str(&format!(
            "{NOTICE_TAG} 先頭 {} バイトだけを読み込みました（全体 {total} バイト）\n",
            body.len()
        ));
    }
    if lossy {
        head.push_str(&format!(
            "{NOTICE_TAG} UTF-8 として読めないバイトがあり、U+FFFD に置き換えました（元の符号化は不明です）\n"
        ));
    }
    let mut out = head;
    out.push_str(&text);
    if truncated {
        out.push_str(&format!("\n{NOTICE_TAG} ここで切り詰めました（この先は読み込んでいません）\n"));
    }
    Ok(out)
}

pub fn write_text_in(root: &Path, rel: &str, content: &str) -> Result<String, String> {
    let root_canon = canonicalize_root(root)?;
    if split_relative(rel)?.is_empty() {
        return Err("書き込むファイルを指定してください".to_string());
    }
    let path = resolve_in_ws(&root_canon, rel)?;

    if let Ok(md) = std::fs::symlink_metadata(&path) {
        if md.is_dir() {
            return Err(format!("`{rel}` はフォルダです。上書きできません"));
        }
    }
    // 親が無ければ作る。resolve_in_ws を通っているので、作られる場所は必ず作業ディレクトリの中。
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("`{rel}` の置き場所を作れません: {e}"))?;
    }
    atomic_write(&path, content.as_bytes())?;
    Ok(display_path(&path))
}

// ---------------------------------------------------------------------------
// 誤操作の歯止め・選択直後の要約
//
// ★2026-09-05（r4b/tests-never-run・反証 must_fix #5）: `reject_dangerous_root` と
//   `info_of` は `crate::app_root()` に依存しない純粋関数だった（前回の移設は
//   「config/state で app_root に依存する 5 本は残す」としたが、この 2 本は
//   その5本には当たらない——`workspace.rs` 側の説明が誤っていた）。安全側の試験
//   `the_filesystem_root_cannot_be_the_workspace` を含め、ここへ移して実行対象にする。
// ---------------------------------------------------------------------------

/// ws_list/ws_read で数える対象の直下ファイル数の上限（表示用の目安であって正確な総数ではない）。
pub const FILES_COUNT_CAP: usize = 10_000;

/// 選択直後に見せる要約。
#[derive(Debug, Clone, Serialize)]
pub struct WsInfo {
    /// 人が読める絶対パス（Windows の `\\?\` 接頭辞は落としてある）。
    pub path: String,
    /// 末尾のフォルダ名。画面の見出し用。
    pub name: String,
    /// 直下のファイル数。**再帰しない**——数万件のツリーを歩いて選択直後に固まるのを避ける。
    /// 「選ぶフォルダを間違えていないか」を見るための目安。
    pub files: usize,
    /// `.git` があるか（worktree ではファイルなので種別は見ない）。
    pub git: bool,
    /// いまのブランチ名（detached なら先頭 7 桁）。git でない・読めないなら None。
    /// ★2026-10-08 入力欄の上の札に出す（オーナー「Claude Code みたいにセッションごとにワークスペースを」）。
    ///   git を起動せず `.git/HEAD` を読むだけ（選択のたびに数ミリ秒）。
    pub branch: Option<String>,
}

/// `.git/HEAD` からブランチ名を読む。worktree（`.git` がファイルで `gitdir: …` を指す）にも対応する。
pub fn git_branch(root: &Path) -> Option<String> {
    let dot = root.join(".git");
    let gitdir = if dot.is_dir() {
        dot
    } else {
        let t = std::fs::read_to_string(&dot).ok()?;
        let p = PathBuf::from(t.trim().strip_prefix("gitdir:")?.trim());
        if p.is_absolute() { p } else { root.join(p) }
    };
    let head = std::fs::read_to_string(gitdir.join("HEAD")).ok()?;
    let head = head.trim();
    if let Some(name) = head.strip_prefix("ref: refs/heads/") {
        return Some(name.to_string());
    }
    if head.len() >= 7 && head.chars().all(|c| c.is_ascii_hexdigit()) {
        return Some(head[..7].to_string());
    }
    None
}

/// 作業ディレクトリとして受け付けられない場所を落とす。
///
/// ★これは権限の境界ではなく**誤操作の歯止め**。ドライブ直下や Windows フォルダを選ぶと、
/// 以後このアプリの読み書きが事実上そのマシン全体に開く。選び直しは1クリックなので、
/// 疑わしいときは断る方を採る。
pub fn reject_dangerous_root(canon: &Path) -> Result<(), String> {
    if canon.parent().is_none() {
        return Err(format!(
            "{} はドライブ（またはファイルシステム）の直下です。もう一段下のフォルダを選んでください",
            display_path(canon)
        ));
    }
    #[cfg(windows)]
    {
        for key in ["SystemRoot", "windir"] {
            if let Some(v) = std::env::var_os(key) {
                if let Ok(sys) = std::fs::canonicalize(PathBuf::from(v)) {
                    if is_within(&sys, canon) {
                        return Err(format!(
                            "{} は Windows のシステムフォルダです。作業用のフォルダを選んでください",
                            display_path(canon)
                        ));
                    }
                }
            }
        }
    }
    Ok(())
}

/// 選択直後に見せる要約を作る。
pub fn info_of(canon: &Path) -> WsInfo {
    let name = canon
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| display_path(canon));

    let mut files = 0usize;
    if let Ok(rd) = std::fs::read_dir(canon) {
        for e in rd.flatten() {
            if files >= FILES_COUNT_CAP {
                break;
            }
            let os_name = e.file_name();
            if HIDDEN_NAMES.iter().any(|h| component_eq(os_name.as_os_str(), OsStr::new(h))) {
                continue;
            }
            // 種別が取れないものは数えない。リンク切れなどで、開けないものを数えても嘘になる。
            if e.file_type().map(|t| !t.is_dir()).unwrap_or(false) {
                files += 1;
            }
        }
    }

    WsInfo {
        path: display_path(canon),
        name,
        files,
        git: canon.join(".git").symlink_metadata().is_ok(),
        branch: git_branch(canon),
    }
}

// ---------------------------------------------------------------------------
// 試験
//
// 依存を増やさないために tempdir を自作する。試験は Linux 上で回る形にしてあるが、
// 判定そのもの（予約デバイス名・末尾ドット・ADS）は Windows 前提のものも含めて全 OS で
// 同じ結果になるようにしてある——片方だけ緩いと、Linux の緑が実機の危険を隠す。
//
// ★この 33 本は移設前、`src/workspace.rs` の `#[cfg(test)] mod tests` にあり、
//   `cargo check --tests --target x86_64-pc-windows-gnu` で**コンパイルされるだけ**
//   だった（Windows バイナリは Linux ホストで実行不能）。ここに移してはじめて、
//   `cargo test --manifest-path blackwall-core/Cargo.toml` で実際に走る。
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static SEQ: AtomicUsize = AtomicUsize::new(0);

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            let n = SEQ.fetch_add(1, Ordering::SeqCst);
            let p = std::env::temp_dir()
                .join(format!("blackwall-wsguard-{}-{}-{}", std::process::id(), tag, n));
            let _ = std::fs::remove_dir_all(&p);
            std::fs::create_dir_all(&p).expect("試験用フォルダを作れません");
            // 実体で比べたいので、ここで正準化しておく（/tmp が symlink の環境がある）。
            TempDir(std::fs::canonicalize(&p).expect("正準化できません"))
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn write(p: &Path, s: &str) {
        if let Some(d) = p.parent() {
            std::fs::create_dir_all(d).unwrap();
        }
        std::fs::write(p, s).unwrap();
    }

    // --- 文字列としての拒否 -------------------------------------------------

    #[test]
    fn absolute_and_unc_are_rejected() {
        let td = TempDir::new("abs");
        for bad in ["/etc/passwd", "\\\\server\\share\\x", "\\windows\\system32"] {
            assert!(resolve_in_ws(td.path(), bad).is_err(), "通ってはいけない: {bad}");
        }
    }

    #[test]
    fn drive_letters_are_rejected() {
        let td = TempDir::new("drive");
        for bad in ["C:\\Windows", "c:/windows", "D:secret.txt"] {
            assert!(resolve_in_ws(td.path(), bad).is_err(), "通ってはいけない: {bad}");
        }
    }

    #[test]
    fn parent_traversal_is_rejected_anywhere_in_the_path() {
        let td = TempDir::new("dotdot");
        std::fs::create_dir_all(td.path().join("a/b")).unwrap();
        for bad in ["..", "../x", "a/../../x", "a/b/../../../etc", "a\\..\\..\\x", "./../x"] {
            assert!(resolve_in_ws(td.path(), bad).is_err(), "通ってはいけない: {bad}");
        }
    }

    #[test]
    fn nul_byte_is_rejected() {
        let td = TempDir::new("nul");
        assert!(resolve_in_ws(td.path(), "a\0b").is_err());
    }

    #[test]
    fn alternate_data_stream_is_rejected() {
        let td = TempDir::new("ads");
        assert!(resolve_in_ws(td.path(), "notes.txt:hidden").is_err());
    }

    #[test]
    fn reserved_device_names_are_rejected_on_every_platform() {
        let td = TempDir::new("dev");
        for bad in ["CON", "nul", "Aux.txt", "COM1", "lpt9.log", "sub/CON"] {
            assert!(resolve_in_ws(td.path(), bad).is_err(), "通ってはいけない: {bad}");
        }
        // 紛らわしいが予約ではないもの。
        for ok in ["console.log", "com.txt", "COM10", "lpt.rs"] {
            assert!(!is_reserved_device_name(ok.split('/').next_back().unwrap()), "拒否しすぎ: {ok}");
        }
    }

    #[test]
    fn trailing_dot_or_space_is_rejected() {
        let td = TempDir::new("trail");
        // ★末尾の空白は入力全体を trim すると見えなくなる。ここは実際にすり抜けた形。
        for bad in ["a.", "b ", "dir./x", "x/y ", " ", "sub/ "] {
            assert!(resolve_in_ws(td.path(), bad).is_err(), "通ってはいけない: {bad:?}");
        }
    }

    #[test]
    fn control_characters_are_rejected_rather_than_silently_trimmed() {
        let td = TempDir::new("ctrl");
        write(&td.path().join("a.txt"), "x");
        for bad in ["a.txt\n", "\ta.txt", "a\rb.txt"] {
            let e = resolve_in_ws(td.path(), bad).unwrap_err();
            assert!(e.contains("制御文字"), "理由を言うこと: {e}");
        }
    }

    // --- 正常系 -------------------------------------------------------------

    #[test]
    fn plain_relative_paths_resolve_under_the_root() {
        let td = TempDir::new("plain");
        write(&td.path().join("src/main.rs"), "fn main() {}");
        let got = resolve_in_ws(td.path(), "src/main.rs").unwrap();
        assert_eq!(got, td.path().join("src").join("main.rs"));
        // 区切りは `\` でも同じ結果になる。
        assert_eq!(resolve_in_ws(td.path(), "src\\main.rs").unwrap(), got);
        // 冗長な区切りと `.` は詰められる。
        assert_eq!(resolve_in_ws(td.path(), "./src//main.rs").unwrap(), got);
    }

    #[test]
    fn empty_relative_path_means_the_root_itself() {
        let td = TempDir::new("empty");
        assert_eq!(resolve_in_ws(td.path(), "").unwrap(), td.path());
        assert_eq!(resolve_in_ws(td.path(), ".").unwrap(), td.path());
    }

    #[test]
    fn missing_files_are_allowed_when_their_place_is_inside() {
        let td = TempDir::new("missing");
        std::fs::create_dir_all(td.path().join("src")).unwrap();
        // 親はある
        assert!(resolve_in_ws(td.path(), "src/new.rs").is_ok());
        // 親も祖父も無い（ws_write が作る場合）
        let deep = resolve_in_ws(td.path(), "a/b/c/new.txt").unwrap();
        assert!(is_within(td.path(), &deep));
        assert!(deep.ends_with("a/b/c/new.txt"));
    }

    // --- リンクによる脱出 ---------------------------------------------------

    #[cfg(unix)]
    #[test]
    fn symlinks_pointing_outside_are_rejected() {
        let td = TempDir::new("link-in");
        let out = TempDir::new("link-out");
        write(&out.path().join("secret.txt"), "とられてはいけない");

        std::os::unix::fs::symlink(out.path(), td.path().join("escape")).unwrap();
        std::os::unix::fs::symlink(out.path().join("secret.txt"), td.path().join("f.txt")).unwrap();

        // フォルダへのリンク（先が外）
        assert!(resolve_in_ws(td.path(), "escape").is_err());
        // その下の既存ファイル
        assert!(resolve_in_ws(td.path(), "escape/secret.txt").is_err());
        // その下の**まだ無い**ファイル（親を正準化して判定する経路）
        assert!(resolve_in_ws(td.path(), "escape/new.txt").is_err());
        // ファイルへのリンク
        assert!(resolve_in_ws(td.path(), "f.txt").is_err());
    }

    #[cfg(unix)]
    #[test]
    fn symlinks_staying_inside_are_allowed_and_resolved() {
        let td = TempDir::new("link-inside");
        write(&td.path().join("real/data.txt"), "中身");
        std::os::unix::fs::symlink(td.path().join("real"), td.path().join("alias")).unwrap();

        let got = resolve_in_ws(td.path(), "alias/data.txt").unwrap();
        // 返るのは実体側。検査したものと開くものを一致させるため。
        assert_eq!(got, td.path().join("real").join("data.txt"));
    }

    #[cfg(unix)]
    #[test]
    fn a_root_that_is_itself_a_symlink_still_works() {
        let td = TempDir::new("root-link-real");
        let holder = TempDir::new("root-link-holder");
        write(&td.path().join("a.txt"), "x");
        let link = holder.path().join("ws");
        std::os::unix::fs::symlink(td.path(), &link).unwrap();

        // root 側も正準化してから比べるので、リンク越しでも中だと判定できる。
        assert!(resolve_in_ws(&link, "a.txt").is_ok());
    }

    // --- 接頭辞の比較 -------------------------------------------------------

    #[test]
    fn sibling_with_a_shared_prefix_is_not_inside() {
        // 文字列の starts_with だと通ってしまう形。
        assert!(!is_within(Path::new("/home/u/ws"), Path::new("/home/u/ws-evil/x")));
        assert!(is_within(Path::new("/home/u/ws"), Path::new("/home/u/ws/x")));
        assert!(is_within(Path::new("/home/u/ws"), Path::new("/home/u/ws")));
        assert!(!is_within(Path::new("/home/u/ws"), Path::new("/home/u")));
    }

    #[test]
    fn case_folding_follows_the_platform() {
        let inside = is_within(Path::new("/home/U/WS"), Path::new("/home/u/ws/x"));
        if cfg!(windows) {
            assert!(inside, "Windows は大文字小文字を区別しないので中と見なす");
        } else {
            assert!(!inside, "Linux では別のフォルダ。中と見なしてはいけない");
        }
    }

    #[test]
    fn non_ascii_paths_work() {
        // 実機の利用者名が「田中　優輝」（全角空白入り）。ここで壊れないこと。
        let td = TempDir::new("非ASCII　名前");
        write(&td.path().join("メモ/日誌.md"), "# 日誌\n");
        let got = resolve_in_ws(td.path(), "メモ/日誌.md").unwrap();
        assert!(is_within(td.path(), &got));
        assert_eq!(read_text_in(td.path(), "メモ/日誌.md").unwrap(), "# 日誌\n");
    }

    // --- 一覧 ---------------------------------------------------------------

    #[test]
    fn list_hides_noise_and_puts_folders_first() {
        let td = TempDir::new("list");
        write(&td.path().join("b.txt"), "b");
        write(&td.path().join("a.txt"), "a");
        write(&td.path().join("node_modules/x.js"), "x");
        write(&td.path().join(".git/HEAD"), "ref");
        std::fs::create_dir_all(td.path().join("src")).unwrap();

        let got = list_in(td.path(), "").unwrap();
        let names: Vec<&str> = got.iter().map(|e| e.name.as_str()).collect();
        assert_eq!(names, vec!["src", "a.txt", "b.txt"]);
        assert!(got[0].is_dir);
        assert_eq!(got[1].size, 1);
        assert!(got.iter().all(|e| !e.rel.is_empty()), "お知らせ行は出ないはず");
    }

    #[test]
    fn list_of_a_subfolder_keeps_relative_paths_joinable() {
        let td = TempDir::new("list-sub");
        write(&td.path().join("src/lib/mod.rs"), "x");
        let got = list_in(td.path(), "src").unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].rel, "src/lib");
        // 返った rel をそのまま次の呼び出しに使えること。
        assert!(list_in(td.path(), &got[0].rel).is_ok());
    }

    #[test]
    fn list_reports_truncation_instead_of_silently_dropping() {
        let td = TempDir::new("list-many");
        for i in 0..(LIST_LIMIT + 7) {
            write(&td.path().join(format!("f{i:05}.txt")), "x");
        }
        let got = list_in(td.path(), "").unwrap();
        assert_eq!(got.len(), LIST_LIMIT + 1, "上限 + お知らせ行1件");
        let last = got.last().unwrap();
        assert!(last.rel.is_empty(), "お知らせ行は rel が空");
        assert!(last.name.contains("他 7 件"), "実際の件数を書くこと: {}", last.name);
    }

    #[cfg(unix)]
    #[test]
    fn list_excludes_escaping_links_and_says_so() {
        let td = TempDir::new("list-esc");
        let out = TempDir::new("list-esc-out");
        write(&out.path().join("secret.txt"), "s");
        write(&td.path().join("ok.txt"), "o");
        std::os::unix::fs::symlink(out.path().join("secret.txt"), td.path().join("bad.txt")).unwrap();

        let got = list_in(td.path(), "").unwrap();
        assert!(got.iter().any(|e| e.name == "ok.txt"));
        assert!(!got.iter().any(|e| e.name == "bad.txt"), "外を指すものを一覧に出さない");
        let note = got.last().unwrap();
        assert!(note.rel.is_empty() && note.name.contains("1 件"), "除外を黙らせない: {}", note.name);
    }

    #[test]
    fn list_of_a_file_is_an_error_not_an_empty_list() {
        let td = TempDir::new("list-file");
        write(&td.path().join("a.txt"), "a");
        assert!(list_in(td.path(), "a.txt").is_err());
    }

    // --- 読み ---------------------------------------------------------------

    #[test]
    fn read_returns_the_file_verbatim() {
        let td = TempDir::new("read");
        write(&td.path().join("a.txt"), "line1\nline2\n");
        assert_eq!(read_text_in(td.path(), "a.txt").unwrap(), "line1\nline2\n");
    }

    #[test]
    fn read_marks_truncation() {
        let td = TempDir::new("read-big");
        let body = "あ".repeat(READ_LIMIT); // 3 バイト文字なので確実に超える
        write(&td.path().join("big.txt"), &body);

        let got = read_text_in(td.path(), "big.txt").unwrap();
        assert!(got.starts_with(NOTICE_TAG), "切り詰めたことを先頭で言う");
        assert!(got.contains("ここで切り詰めました"));
        // 文字の途中で切っていないこと（U+FFFD を出していない）。
        assert!(!got.contains('\u{FFFD}'), "境界で戻していない");
        assert!(!got.contains("読めないバイト"), "切り詰めを文字化けと誤報している");
    }

    #[test]
    fn read_marks_non_utf8_instead_of_returning_garbage_silently() {
        let td = TempDir::new("read-sjis");
        // Shift_JIS の「日本語」。NUL は含まない。
        std::fs::write(td.path().join("s.txt"), [0x93u8, 0xFA, 0x96, 0x7B, 0x8C, 0xEA]).unwrap();
        let got = read_text_in(td.path(), "s.txt").unwrap();
        assert!(got.contains("UTF-8 として読めない"));
        assert!(got.contains('\u{FFFD}'));
    }

    #[test]
    fn read_refuses_binary() {
        let td = TempDir::new("read-bin");
        std::fs::write(td.path().join("a.bin"), [0x7Fu8, b'E', b'L', b'F', 0x00, 0x01]).unwrap();
        let e = read_text_in(td.path(), "a.bin").unwrap_err();
        assert!(e.contains("バイナリ"), "理由を言うこと: {e}");
    }

    #[test]
    fn read_of_a_folder_is_an_error() {
        let td = TempDir::new("read-dir");
        std::fs::create_dir_all(td.path().join("src")).unwrap();
        assert!(read_text_in(td.path(), "src").is_err());
        assert!(read_text_in(td.path(), "").is_err());
    }

    #[test]
    fn read_of_a_missing_file_says_so() {
        let td = TempDir::new("read-miss");
        let e = read_text_in(td.path(), "nope.txt").unwrap_err();
        assert!(e.contains("nope.txt"), "どれが無いのか言うこと: {e}");
    }

    // --- 書き ---------------------------------------------------------------

    #[test]
    fn write_creates_parents_and_round_trips() {
        let td = TempDir::new("write");
        write_text_in(td.path(), "a/b/c.txt", "内容\n").unwrap();
        assert_eq!(std::fs::read_to_string(td.path().join("a/b/c.txt")).unwrap(), "内容\n");
        assert_eq!(read_text_in(td.path(), "a/b/c.txt").unwrap(), "内容\n");
    }

    #[test]
    fn write_replaces_an_existing_file_without_leaving_temp_files() {
        let td = TempDir::new("write-over");
        write(&td.path().join("a.txt"), "古い");
        write_text_in(td.path(), "a.txt", "新しい").unwrap();
        assert_eq!(std::fs::read_to_string(td.path().join("a.txt")).unwrap(), "新しい");

        let leftovers: Vec<String> = std::fs::read_dir(td.path())
            .unwrap()
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.contains("blackwall-tmp"))
            .collect();
        assert!(leftovers.is_empty(), "temp が残っている: {leftovers:?}");
    }

    #[test]
    fn write_outside_the_workspace_is_refused() {
        let td = TempDir::new("write-esc");
        let out = TempDir::new("write-esc-out");
        assert!(write_text_in(td.path(), "../escaped.txt", "x").is_err());
        assert!(!out.path().join("escaped.txt").exists());
    }

    #[cfg(unix)]
    #[test]
    fn write_through_an_escaping_link_is_refused() {
        let td = TempDir::new("write-link");
        let out = TempDir::new("write-link-out");
        std::os::unix::fs::symlink(out.path(), td.path().join("escape")).unwrap();
        assert!(write_text_in(td.path(), "escape/x.txt", "x").is_err());
        assert!(!out.path().join("x.txt").exists(), "外に書けてしまった");
    }

    #[test]
    fn write_to_a_folder_is_refused() {
        let td = TempDir::new("write-dir");
        std::fs::create_dir_all(td.path().join("src")).unwrap();
        assert!(write_text_in(td.path(), "src", "x").is_err());
        assert!(write_text_in(td.path(), "", "x").is_err());
    }

    // --- 選択時の歯止め（2026-09-05・r4b/tests-never-run で移設）---------------
    //
    // ★元は `src/workspace.rs` の `#[cfg(test)] mod tests` にあり、`reject_dangerous_root`
    //   本体を移した後もこの2本だけ取り残されていた（tauri 依存の workspace.rs は
    //   cargo test がリンクできないので、moved 前の33本と同じ「コンパイルされるだけ」の
    //   まま）。反証(idx17) must_fix #5「安全側の試験は実行される場所へ動かす」の対象。

    #[test]
    fn the_filesystem_root_cannot_be_the_workspace() {
        let e = reject_dangerous_root(Path::new("/")).unwrap_err();
        assert!(e.contains("直下"), "理由を言うこと: {e}");
        let td = TempDir::new("ok-root");
        assert!(reject_dangerous_root(td.path()).is_ok());
    }

    #[test]
    fn info_counts_only_visible_files_at_the_top_level() {
        let td = TempDir::new("info");
        write(&td.path().join("a.txt"), "a");
        write(&td.path().join("b.txt"), "b");
        write(&td.path().join("src/deep.rs"), "x"); // 再帰しないので数えない
        write(&td.path().join("node_modules/x.js"), "x"); // 隠すので数えない
        write(&td.path().join(".git/HEAD"), "ref");

        let i = info_of(td.path());
        assert_eq!(i.files, 2);
        assert!(i.git, ".git があれば git=true");
        assert_eq!(i.name, td.path().file_name().unwrap().to_string_lossy());
    }

    #[test]
    fn branch_is_read_from_head_and_worktree_file() {
        let base = std::env::temp_dir().join(format!("bw-branch-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        // 普通のリポジトリ
        let repo = base.join("repo");
        std::fs::create_dir_all(repo.join(".git")).unwrap();
        std::fs::write(repo.join(".git").join("HEAD"), "ref: refs/heads/feature/x\n").unwrap();
        assert_eq!(git_branch(&repo).as_deref(), Some("feature/x"));
        // detached
        std::fs::write(repo.join(".git").join("HEAD"), "0123456789abcdef0123456789abcdef01234567\n").unwrap();
        assert_eq!(git_branch(&repo).as_deref(), Some("0123456"));
        // worktree（.git がファイル）
        let wt = base.join("wt");
        let gd = base.join("gitdir-wt");
        std::fs::create_dir_all(&wt).unwrap();
        std::fs::create_dir_all(&gd).unwrap();
        std::fs::write(gd.join("HEAD"), "ref: refs/heads/main\n").unwrap();
        std::fs::write(wt.join(".git"), format!("gitdir: {}\n", gd.display())).unwrap();
        assert_eq!(git_branch(&wt).as_deref(), Some("main"));
        // git でない
        assert_eq!(git_branch(&base), None);
        let _ = std::fs::remove_dir_all(&base);
    }

}
