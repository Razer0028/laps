//! 編集の安全機構 — Python 側の `laps/tools/edit.py` からの移植。
//!
//! ★これは新規設計ではない。本体エージェントで実測して直してきたものを、そのまま持ってくる。
//! 発明し直すと、同じ事故をもう一度踏むことになる（実際に踏んだ順に列挙する）:
//!
//!   1. **全文上書きはファイルを壊す**。設計で「採らない」と決めた形（他実装が
//!      `[original content here]` のような置き換えで中身を消した事例）。だから編集は
//!      「古い文字列 → 新しい文字列」で行い、全文の書き換えは編集の手段にしない。
//!   2. **曖昧一致に `replace_all` で答えると、直す気のない箇所まで変わる**。
//!      実測: `add` と `mul` の本体が同じ `return a + b` で、`mul` を直すつもりが
//!      両方変わって `add` が壊れた。案内文だけでは防げない（モデルが選ぶため）ので、
//!      **その形を構造的に拒否**し、`occurrence` という安全な道を出す。
//!   3. **見つからないときは今の中身を返す**。素の「not found」だけ返すと、モデルは
//!      自分が覚えている古い内容に合わせ続けて堂々巡りになる。
//!   4. **改行の種類を保つ**。読み書きで CRLF が LF に潰れると、1行の編集が全行の差分になる。
//!   5. **書き込みは原子的に**。途中で落ちたとき、半端なファイルを残さない。
//!
//! Python 版との違い（正直に書く）: 構文検査は JSON しか本当の意味で検査できない。
//! Python や YAML の構文解析器を Rust に持ち込むと依存が増えるため、それ以外は
//! **括弧と引用符の釣り合い**という弱い検査に留める。弱いと分かる名前にしてある。

use std::path::Path;

/// 一致した各位置（バイト単位の先頭 index）。
fn match_sites(body: &str, old: &str) -> Vec<usize> {
    let mut out = Vec::new();
    if old.is_empty() {
        return out;
    }
    let mut from = 0usize;
    while let Some(i) = body[from..].find(old) {
        let at = from + i;
        out.push(at);
        from = at + 1; // 重なりも数える（Python 版と同じ）
    }
    out
}

fn line_bounds(body: &str, pos: usize) -> (usize, usize) {
    let start = body[..pos].rfind('\n').map(|i| i + 1).unwrap_or(0);
    let end = body[pos..].find('\n').map(|i| pos + i).unwrap_or(body.len());
    (start, end)
}

/// その行より前の、空でない最も近い行（前後関係の指紋）。
fn prev_nonempty(body: &str, line_start: usize) -> &str {
    let mut end = line_start;
    while end > 0 {
        let pe = end - 1; // 直前の行を終える改行
        let ps = body[..pe].rfind('\n').map(|i| i + 1).unwrap_or(0);
        let prev = body[ps..pe].trim();
        if !prev.is_empty() {
            return prev;
        }
        end = ps;
    }
    ""
}

/// 事故の形（別々の場所が、たまたま同じ1行を共有している）かどうか。
///
/// `old` が行まるごとの文で、かつ各出現の**前後が違う**とき true。
/// トークンの改名（`foo_bar` → `baz`）は行全体と一致しないので false のまま＝掃引できる。
fn heterogeneous_full_line(body: &str, old: &str, sites: &[usize]) -> bool {
    if old.contains('\n') {
        return false;
    }
    let target = old.trim();
    let mut sigs = std::collections::BTreeSet::new();
    for &pos in sites {
        let (ls, le) = line_bounds(body, pos);
        if body[ls..le].trim() != target {
            return false; // 行の一部＝改名などの正当な掃引
        }
        sigs.insert(prev_nonempty(body, ls).to_string());
    }
    sigs.len() > 1
}

/// 1つの編集の結果。
#[derive(Debug)]
pub struct Applied {
    pub body: String,
    pub count: usize,
}

/// 編集を1つ適用する。`occurrence` は 1 始まり。`replace_all` とは排他。
pub fn apply_one(
    body: &str,
    old: &str,
    new: &str,
    replace_all: bool,
    occurrence: Option<usize>,
) -> Result<Applied, String> {
    if old.is_empty() {
        return Err("old_string が空です".to_string());
    }
    let sites = match_sites(body, old);
    if sites.is_empty() {
        // 今の中身を返す。これが無いと、モデルは覚えている古い内容に合わせ続ける。
        let snippet: String = if body.chars().count() <= 2000 {
            body.to_string()
        } else {
            body.chars().take(2000).collect::<String>() + "\n…(切り詰め)"
        };
        return Err(format!(
            "old_string が見つかりません: {}\n--- ファイルの現在の内容（これに合わせること）---\n{}",
            preview(old, 60),
            snippet
        ));
    }
    let cnt = sites.len();

    if let Some(n) = occurrence {
        if replace_all {
            return Err("occurrence と replace_all は同時に指定できません（1箇所なら occurrence、全部なら replace_all）".into());
        }
        if n == 0 || n > cnt {
            return Err(format!("occurrence={n} は範囲外です（一致は {cnt} 箇所、1〜{cnt} で指定）"));
        }
        let pos = sites[n - 1];
        let mut out = String::with_capacity(body.len() + new.len());
        out.push_str(&body[..pos]);
        out.push_str(new);
        out.push_str(&body[pos + old.len()..]);
        return Ok(Applied { body: out, count: 1 });
    }

    if replace_all {
        if cnt > 1 && heterogeneous_full_line(body, old, &sites) {
            let mut where_ = String::new();
            for (i, &pos) in sites.iter().take(6).enumerate() {
                let line = body[..pos].matches('\n').count() + 1;
                let (ls, _) = line_bounds(body, pos);
                where_.push_str(&format!(
                    "  occurrence={}: 行 {}（直前: {}）\n",
                    i + 1,
                    line,
                    preview(prev_nonempty(body, ls), 40)
                ));
            }
            return Err(format!(
                "replace_all は拒否しました: {} は {} 箇所すべてで“行まるごと”一致し、しかも前後が異なります\
                 （別々の場所が同じ行をたまたま共有しているだけ）。\n\
                 全部変えると、直すつもりのない箇所まで壊れます。\n\
                 1箇所だけ直すには occurrence=N を指定してください:\n{}",
                preview(old, 50),
                cnt,
                where_
            ));
        }
        return Ok(Applied { body: body.replace(old, new), count: cnt });
    }

    if cnt > 1 {
        let lines: Vec<String> = sites
            .iter()
            .take(3)
            .map(|&p| (body[..p].matches('\n').count() + 1).to_string())
            .collect();
        return Err(format!(
            "old_string が {} 箇所に一致します（行 {}{}）: {}\n\
             1箇所だけ直すには occurrence=N（1〜{}）を指定するか、前後の行を old_string に足して\
             一意にしてください。全部を同じに変えるのが本当に意図なら replace_all を指定してください。",
            cnt,
            lines.join(", "),
            if cnt > 3 { " ほか" } else { "" },
            preview(old, 50),
            cnt
        ));
    }
    Ok(Applied { body: body.replacen(old, new, 1), count: 1 })
}

fn preview(s: &str, n: usize) -> String {
    let t: String = s.chars().take(n).collect();
    format!("{:?}", t)
}

// ---------------------------------------------------------------------------
// 構文の検査（弱い）
// ---------------------------------------------------------------------------

/// 編集が構文を壊したかを見る。**JSON 以外は弱い検査**であることを名前で示す。
///
/// 「有効だったものが無効になった」ときだけ拒否する。もともと壊れているファイルを
/// 直す途中では邪魔をしない（Python 版と同じ判定）。
pub fn breaks_syntax(path: &Path, before: &str, after: &str) -> Option<String> {
    let name = path.to_string_lossy().to_lowercase();
    if name.ends_with(".json") {
        let ok_before = serde_json::from_str::<serde_json::Value>(before.trim_start_matches('\u{feff}')).is_ok();
        let err_after = serde_json::from_str::<serde_json::Value>(after.trim_start_matches('\u{feff}')).err();
        return match (ok_before, err_after) {
            (true, Some(e)) => Some(format!("JSON として壊れます: {e}")),
            _ => None,
        };
    }
    // それ以外は括弧と引用符の釣り合いだけ見る。構文解析ではないので、
    // 「壊れていないのに壊れたと言う」ことがないよう、**釣り合いが取れていたものが
    // 取れなくなった場合に限って**拒否する。
    if balanced(before) && !balanced(after) {
        return Some("括弧か引用符の釣り合いが取れなくなります".to_string());
    }
    None
}

fn balanced(s: &str) -> bool {
    let (mut round, mut square, mut curly) = (0i32, 0i32, 0i32);
    let (mut sq, mut dq, mut esc) = (false, false, false);
    for c in s.chars() {
        if esc {
            esc = false;
            continue;
        }
        match c {
            '\\' if sq || dq => esc = true,
            '\'' if !dq => sq = !sq,
            '"' if !sq => dq = !dq,
            _ if sq || dq => {}
            '(' => round += 1,
            ')' => round -= 1,
            '[' => square += 1,
            ']' => square -= 1,
            '{' => curly += 1,
            '}' => curly -= 1,
            _ => {}
        }
        if round < 0 || square < 0 || curly < 0 {
            return false;
        }
    }
    round == 0 && square == 0 && curly == 0 && !sq && !dq
}

// ---------------------------------------------------------------------------
// 読み書き（改行を保ち、原子的に置き換える）
// ---------------------------------------------------------------------------

/// 読み込み結果。編集は LF 空間で行い、書き戻すときに元の改行へ戻す。
pub struct Loaded {
    pub body: String,
    pub crlf: bool,
}

pub fn load(path: &Path) -> Result<Loaded, String> {
    let raw = std::fs::read(path).map_err(|e| format!("{} を読めません: {e}", path.display()))?;
    let crlf = {
        let n_crlf = raw.windows(2).filter(|w| w == b"\r\n").count();
        let n_lf = raw.iter().filter(|&&b| b == b'\n').count();
        n_crlf > 0 && n_crlf * 2 >= n_lf
    };
    let text = String::from_utf8_lossy(&raw).replace("\r\n", "\n");
    Ok(Loaded { body: text, crlf })
}

/// temp → rename で置き換える。途中で落ちても元のファイルは無傷。
pub fn save_atomic(path: &Path, body: &str, crlf: bool) -> Result<(), String> {
    let data = if crlf { body.replace('\n', "\r\n") } else { body.to_string() };
    let dir = path.parent().ok_or_else(|| "親フォルダがありません".to_string())?;
    std::fs::create_dir_all(dir).map_err(|e| format!("{} を作れません: {e}", dir.display()))?;
    let tmp = dir.join(format!(
        ".bw_{}.tmp",
        path.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default()
    ));
    std::fs::write(&tmp, data.as_bytes()).map_err(|e| format!("一時ファイルを書けません: {e}"))?;
    std::fs::rename(&tmp, path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("{} を置き換えられません: {e}", path.display())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const ADD_MUL: &str = "def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a + b\n";

    #[test]
    fn the_add_mul_shape_refuses_replace_all() {
        let e = apply_one(ADD_MUL, "return a + b", "return a * b", true, None).unwrap_err();
        assert!(e.contains("occurrence"), "occurrence の案内が無い: {e}");
    }

    #[test]
    fn occurrence_edits_exactly_one_site() {
        let r = apply_one(ADD_MUL, "return a + b", "return a * b", false, Some(2)).unwrap();
        assert_eq!(r.count, 1);
        assert_eq!(r.body, "def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a * b\n");
        assert!(apply_one(ADD_MUL, "return a + b", "x", false, Some(3)).is_err());
    }

    #[test]
    fn token_rename_still_sweeps() {
        let r = apply_one("foo_bar = 1\nprint(foo_bar)\n", "foo_bar", "baz", true, None).unwrap();
        assert_eq!(r.count, 2);
        assert!(!r.body.contains("foo_bar"));
    }

    #[test]
    fn ambiguous_single_edit_is_refused_with_line_numbers() {
        let e = apply_one(ADD_MUL, "return a + b", "x", false, None).unwrap_err();
        assert!(e.contains("2 箇所") && e.contains("行"), "{e}");
    }

    #[test]
    fn not_found_hands_back_the_current_content() {
        let e = apply_one("x = 1\n", "y = 2", "z", false, None).unwrap_err();
        assert!(e.contains("現在の内容") && e.contains("x = 1"), "{e}");
    }

    #[test]
    fn occurrence_and_replace_all_conflict() {
        assert!(apply_one(ADD_MUL, "return a + b", "x", true, Some(1)).is_err());
    }

    #[test]
    fn json_break_is_caught_but_already_broken_is_not_blocked() {
        let p = Path::new("c.json");
        assert!(breaks_syntax(p, r#"{"a":1}"#, r#"{"a":1,}"#).is_some());
        assert!(breaks_syntax(p, r#"{"a":1,}"#, r#"{"a":2,}"#).is_none()); // 元から壊れている
        assert!(breaks_syntax(p, r#"{"a":1}"#, r#"{"a":2}"#).is_none());
    }

    #[test]
    fn balance_check_only_fires_on_newly_unbalanced() {
        let p = Path::new("m.py");
        assert!(breaks_syntax(p, "f(1)\n", "f(1\n").is_some());
        assert!(breaks_syntax(p, "f(1\n", "f(1 + 2\n").is_none()); // 元から釣り合っていない
        assert!(breaks_syntax(p, "s = \"(\"\n", "s = \"()\"\n").is_none()); // 文字列の中は数えない
    }

    #[test]
    fn crlf_is_preserved_through_a_round_trip() {
        let d = std::env::temp_dir().join(format!("bw_edit_test_{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let f = d.join("w.txt");
        std::fs::write(&f, b"alpha\r\nbeta\r\n").unwrap();
        let l = load(&f).unwrap();
        assert!(l.crlf && l.body == "alpha\nbeta\n");
        let r = apply_one(&l.body, "beta", "BETA", false, None).unwrap();
        save_atomic(&f, &r.body, l.crlf).unwrap();
        assert_eq!(std::fs::read(&f).unwrap(), b"alpha\r\nBETA\r\n");
        let _ = std::fs::remove_dir_all(&d);
    }
}
