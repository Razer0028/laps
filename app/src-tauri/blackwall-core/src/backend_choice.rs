//! 推論の宛先を決める**純粋な**論理（2026-09-06）。
//!
//! ★なぜ切り出したか（実機で踏んだ 2026-09-06 の障害）:
//!   ある実機が 0.1.194 に更新した直後から、
//!   「推論機の応答を待っています（N秒）→ エラー」になり、
//!   LAN の推論機には**要求が1件も届かなくなった**。
//!   ＝宛先が黙って `http://127.0.0.1:8760/v1`（同梱の llama-server）へ落ちていた。
//!
//!   それまでの決まりは `sidecar.rs::backend_url()` と `main.rs::lan_backend()` に
//!   分かれて書かれた素の分岐で、**試験が1本も無かった**（本体 `blackwall` は
//!   Tauri のクレートなのでホストで `cargo test` がリンクできない＝
//!   あちらに書いても一度も走らない試験になる）。ここへ実体を置き、
//!   `src/main.rs` が `#[path]` で参照する。二重管理は起きない。
//!
//! ★決め方の骨格（3値）:
//!     use_lan_backend = true    … LAN 固定（従来どおり）
//!     use_lan_backend = false   … この機械のローカル固定（利用者が選んだ OFF）
//!     use_lan_backend = "auto"  … **ローカルが実際に使えるときだけローカル**（新設・既定）
//!     鍵そのものが無い          … auto と同じ
//!
//!   `auto` を既定にしたのは、「設定が消えた／既定に戻った」ときに
//!   **黙って使えない宛先へ落ちない**ようにするため。逆に、
//!   「値があれば LAN を使う」にはしない——それは全ての既存インストールが
//!   黙って別のマシンへ話しに行く、向きを逆にしただけの同じ事故になる
//!   （`main.rs::lan_backend()` のコメントが名指しで警告している）。

use std::path::{Path, PathBuf};

/// `use_lan_backend` の 3 値。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LanChoice {
    /// LAN 固定。
    Lan,
    /// この機械のローカル固定。
    Local,
    /// ローカルが実際に使えるときだけローカル。使えないなら LAN。
    Auto,
}

/// 設定ファイルから読んだ、宛先に関わる値だけ。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LanConfig {
    /// `None` = 鍵そのものが無い（auto と同じに扱う）。
    pub choice: Option<LanChoice>,
    /// `lan_backend_url`（`/v1` を含まない根）。
    pub url: String,
    /// `lan_model`。
    pub model: String,
    /// `lan_choice_pinned = true`。**利用者が画面で選んだ**印。
    /// これが立っていない `false` は「アプリが既定として書いた値」であって
    /// 利用者の意思ではない——移行（`migrate_machine_default_false`）はそこだけ触る。
    pub pinned: bool,
}

/// 宛先がどう決まったか。画面と記録に**そのまま**出す。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// 環境変数 `LAPS_SIDECAR_BACKEND`。
    Env,
    /// `use_lan_backend = true`。
    ExplicitLan,
    /// `use_lan_backend = false`。
    ExplicitLocal,
    /// auto ＋ ローカルが使えない → LAN。
    AutoLan,
    /// auto ＋ ローカルが使える → ローカル。
    AutoLocal,
    /// LAN にしたいのに宛先が空 → ローカルへ落ちた（設定の誤り）。
    LanNoUrl,
}

impl Source {
    /// 画面に出す短い日本語。★「なぜそこへ送っているか」を利用者に読ませる。
    pub fn label(self) -> &'static str {
        match self {
            Source::Env => "環境変数の指定",
            Source::ExplicitLan => "設定で LAN",
            Source::ExplicitLocal => "設定でローカル固定",
            Source::AutoLan => "既定（別の推論サーバー）",
            Source::AutoLocal => "既定だが接続先が空なのでローカル",
            Source::LanNoUrl => "LAN を選んでいるが接続先が空",
        }
    }
    /// 記録に残す機械可読な語。
    pub fn code(self) -> &'static str {
        match self {
            Source::Env => "env",
            Source::ExplicitLan => "explicit_lan",
            Source::ExplicitLocal => "explicit_local",
            Source::AutoLan => "auto_lan",
            Source::AutoLocal => "auto_local",
            Source::LanNoUrl => "lan_no_url",
        }
    }
}

/// 決まった宛先。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decision {
    /// true = LAN の推論機（手元の llama-server は起こさない）。
    pub remote: bool,
    /// LAN の根（`/v1` を含まない）。ローカルのときは空。
    pub root: String,
    pub source: Source,
}

/// `key = value` の行を1つ読む。`#` から先はコメント。値の前後の空白と引用符を落とす。
///
/// ★TOML のパーサは足さない。読むのは数行だけで、行単位の読み書き
/// （`lan_settings_set`）と**同じ読み方**でなければ食い違いが生まれる。
fn kv(line: &str) -> Option<(&str, &str)> {
    let bare = line.split('#').next().unwrap_or("");
    let (k, v) = bare.split_once('=')?;
    Some((k.trim(), v.trim().trim_matches('"').trim()))
}

/// 3 値の読み取り。知らない綴りは `None`（＝鍵が無いのと同じ＝auto）にする。
///
/// ★`true`/`false` 以外を「false」に丸めない。丸めると、綴りを間違えただけで
///   黙ってローカル固定になり、いま直している障害と同じ見え方になる。
pub fn parse_choice(v: &str) -> Option<LanChoice> {
    match v.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" | "lan" => Some(LanChoice::Lan),
        "false" | "0" | "no" | "off" | "local" => Some(LanChoice::Local),
        "auto" => Some(LanChoice::Auto),
        _ => None,
    }
}

/// 設定ファイルの本文から宛先に関わる値を読む。
///
/// ★**同じ鍵が2度出てきたら後ろが勝つ**（従来の `lan_backend()` と同じ）。
///   追記式の `ensure_config_keys` がある以上、重複はいつでも起こりうる。
pub fn parse_config(body: &str) -> LanConfig {
    let mut c = LanConfig::default();
    for line in body.lines() {
        let Some((k, v)) = kv(line) else { continue };
        match k {
            "use_lan_backend" => c.choice = parse_choice(v),
            "lan_backend_url" => c.url = v.trim_end_matches('/').to_string(),
            "lan_model" => c.model = v.to_string(),
            PIN_KEY => c.pinned = matches!(v.to_ascii_lowercase().as_str(), "true" | "1" | "yes"),
            _ => {}
        }
    }
    c
}

/// **唯一の決定点。** `local_usable` は「この機械の llama-server が実際に
/// 答えられる見込みがあるか」（動いていてモデルが載っている／載せられる gguf がある）。
pub fn decide(cfg: &LanConfig, local_usable: bool) -> Decision {
    let _ = local_usable; // 2026-09-06: 既定の判断には使わない（下記）
    let url = cfg.url.trim().trim_end_matches('/').to_string();
    let has_url = !url.is_empty();
    let lan = |source| Decision { remote: true, root: url.clone(), source };
    let local = |source| Decision { remote: false, root: String::new(), source };

    match cfg.choice.unwrap_or(LanChoice::Auto) {
        LanChoice::Lan if has_url => lan(Source::ExplicitLan),
        // ★宛先が無いのに LAN を選んでいる。ローカルへ落とすが、**理由を残す**
        //   （黙って落ちるのが、いま直している障害そのもの）。
        LanChoice::Lan => local(Source::LanNoUrl),
        LanChoice::Local => local(Source::ExplicitLocal),
        // ★2026-09-06 オーナー決定「デフォルトで 別の推論サーバーを使う仕様に。その方がミスがない」:
        //   既定（鍵が無い／auto）は **宛先があるかぎり常に LAN**。ローカルが使えるか
        //   どうかで宛先を変えない（自動判定が手元の止まった推論機へ送って P0 になった）。
        //   ローカルは利用者が設定で明示 OFF（false）にしたときだけ。`local_usable` は
        //   Auto の判断に使わない（互換のため引数は残す）。
        LanChoice::Auto if has_url => lan(Source::AutoLan),
        LanChoice::Auto => local(Source::AutoLocal),
    }
}

/// 実際に本体（serve）へ渡す `LAPS_BACKEND` の文字列を組み立てる。
///
/// ★`/v1` を付けるのはここ1箇所。設定には根だけを書かせる——利用者が
///   付けたり付けなかったりすると「設定は正しく見えるのに 404」になる。
pub fn backend_env(d: &Decision, local_port: u16) -> String {
    if d.remote {
        format!("{}/v1", d.root.trim_end_matches('/'))
    } else {
        format!("http://127.0.0.1:{local_port}/v1")
    }
}

// ---------------------------------------------------------------------------
// 設定ファイルの移行（アプリが書いた既定の false → auto）
// ---------------------------------------------------------------------------

/// 「利用者が画面で選んだ」印の鍵。
pub const PIN_KEY: &str = "lan_choice_pinned";

/// アプリが既定として書いた `use_lan_backend = false` を `"auto"` へ1度だけ直す。
///
/// ★なぜ触ってよいか: 0.1.194 以前の既定の設定ファイルと `ensure_config_keys` は
///   **利用者に聞かずに** `use_lan_backend = false` を書いていた。これは
///   「利用者がローカルを選んだ」ではなく「アプリの既定」であって、
///   設定が既定に戻った機械はこの1行のせいで永久にローカル固定になる。
///   同じ形の直しは既に前例がある（`lan_model = "omega-j-prime"` ＝
///   行き先の無い旧既定を1度だけ書き換えている）。
///
/// ★触らない条件（利用者の意思を消さないための境界）:
///   - `lan_choice_pinned = true` がある＝画面から保存した値なので**そのまま**。
///   - `true` や `"auto"` は元から触らない。
///
/// ★安全な理由: `auto` はローカルが使えるなら**ローカルを選ぶ**。
///   手で false と書いた人がローカルの推論機を持っているなら、動きは変わらない。
///
/// 変更が要らなければ `None`。
pub fn migrate_machine_default_false(body: &str) -> Option<String> {
    let cfg = parse_config(body);
    if cfg.pinned || cfg.choice != Some(LanChoice::Local) {
        return None;
    }
    let mut changed = false;
    let out: Vec<String> = body
        .lines()
        .map(|line| {
            if changed {
                return line.to_string();
            }
            match kv(line) {
                Some(("use_lan_backend", v)) if parse_choice(v) == Some(LanChoice::Local) => {
                    changed = true;
                    // ★コメントは落とす（値の意味が変わるので、古い説明を残すと嘘になる）。
                    "use_lan_backend = \"auto\"   # 0.1.195 で既定の false から移行".to_string()
                }
                _ => line.to_string(),
            }
        })
        .collect();
    if !changed {
        return None;
    }
    let mut text = out.join("\n");
    if !text.ends_with('\n') {
        text.push('\n');
    }
    Some(text)
}

// ---------------------------------------------------------------------------
// 設定ファイルの引き継ぎ（データ根が動いたとき）
// ---------------------------------------------------------------------------

/// いまのデータ根に設定ファイルが無いとき、**どこを探すか**を決める。
///
/// ★なぜ要るか: Windows のデータ根は `platform_root()`（main.rs）が
///   `%LOCALAPPDATA%\Blackwall` と `%PUBLIC%\Blackwall` を出し分ける
///   （利用者名に非 ASCII が混ざると後者へ逃がす）。根が動いた回は
///   設定ファイルが**新しい根に無い**＝既定が書かれ、LAN の設定が消える。
///   インストーラは `%LOCALAPPDATA%` を触らないので、上書きではなく
///   **見に行く場所が変わった**ほうが起きうる。
///
/// 返すのは「現在の根と異なる候補の `config.toml`」の並び（重複は落とす）。
pub fn adoption_candidates(current_root: &Path, others: &[PathBuf]) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    for o in others {
        if o == current_root || out.contains(o) {
            continue;
        }
        out.push(o.clone());
    }
    out.into_iter().map(|p| p.join("config.toml")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(body: &str) -> LanConfig {
        parse_config(body)
    }

    // ── 読み取り ────────────────────────────────────────────────
    #[test]
    fn 鍵が無ければ_choice_は_none() {
        let c = cfg("some_key = \"http://x\"\n");
        assert_eq!(c.choice, None);
        assert_eq!(c.url, "");
    }

    #[test]
    fn 三値を読む() {
        assert_eq!(cfg("use_lan_backend = true\n").choice, Some(LanChoice::Lan));
        assert_eq!(cfg("use_lan_backend = false\n").choice, Some(LanChoice::Local));
        assert_eq!(cfg("use_lan_backend = \"auto\"\n").choice, Some(LanChoice::Auto));
        assert_eq!(cfg("use_lan_backend = auto\n").choice, Some(LanChoice::Auto));
    }

    #[test]
    fn 知らない綴りは_false_に丸めない() {
        // ★丸めると、綴り違いが黙ってローカル固定になり、いま直した障害と同じ形になる。
        assert_eq!(cfg("use_lan_backend = yeah\n").choice, None);
        assert_eq!(decide(&cfg("use_lan_backend = yeah\nlan_backend_url = \"http://h:1\"\n"), false).remote, true);
    }

    #[test]
    fn 行末のコメントと引用符を落とす() {
        let c = cfg("use_lan_backend = true   # LAN を使う\nlan_backend_url = \"http://127.0.0.1:8080/\"  # 根だけ\n");
        assert_eq!(c.choice, Some(LanChoice::Lan));
        assert_eq!(c.url, "http://127.0.0.1:8080"); // 末尾の / は落ちる
    }

    #[test]
    fn 同じ鍵が二度あれば後ろが勝つ() {
        // ★追記式の ensure_config_keys がある以上、重複はいつでも起こりうる。
        let c = cfg("use_lan_backend = true\nuse_lan_backend = false\n");
        assert_eq!(c.choice, Some(LanChoice::Local));
    }

    #[test]
    fn コメント行の中の鍵は読まない() {
        let c = cfg("# use_lan_backend = true\nuse_lan_backend = false\n");
        assert_eq!(c.choice, Some(LanChoice::Local));
    }

    // ── 判定表（設定 4 形 × ローカル 2 形）─────────────────────
    const URL: &str = "lan_backend_url = \"http://127.0.0.1:8080\"\n";

    #[test]
    fn 表_設定ファイルが無い() {
        // 設定ごと無い＝空文字。宛先も無いのでローカル（LAN の当て先が無い）。
        let d = decide(&cfg(""), false);
        assert_eq!((d.remote, d.source), (false, Source::AutoLocal));
    }

    #[test]
    fn 表_鍵が欠落_ローカル使える() {
        // ★2026-09-06: ローカルが使えても既定は LAN。自動判定で手元へ送らない。
        let d = decide(&cfg(URL), true);
        assert_eq!((d.remote, d.source), (true, Source::AutoLan));
        assert_eq!(d.root, "http://127.0.0.1:8080");
    }

    #[test]
    fn 表_鍵が欠落_ローカル使えない() {
        let d = decide(&cfg(URL), false);
        assert_eq!((d.remote, d.source), (true, Source::AutoLan));
        assert_eq!(d.root, "http://127.0.0.1:8080");
    }

    #[test]
    fn 表_古い設定_明示_false_ローカル使えない() {
        // ★明示 false は**ローカル固定**（仕様どおり）。auto へ寄せるのは
        //   migrate_machine_default_false の仕事で、判定は意思を尊重する。
        let d = decide(&cfg(&format!("{URL}use_lan_backend = false\n")), false);
        assert_eq!((d.remote, d.source), (false, Source::ExplicitLocal));
    }

    #[test]
    fn 表_明示_true_ローカル使える() {
        let d = decide(&cfg(&format!("{URL}use_lan_backend = true\n")), true);
        assert_eq!((d.remote, d.source), (true, Source::ExplicitLan));
    }

    #[test]
    fn 表_明示_true_だが宛先が空() {
        let d = decide(&cfg("use_lan_backend = true\nlan_backend_url = \"\"\n"), false);
        assert_eq!((d.remote, d.source), (false, Source::LanNoUrl));
    }

    #[test]
    fn 表_auto_ローカル使える_使えない() {
        let body = format!("{URL}use_lan_backend = \"auto\"\n");
        // ★2026-09-06: auto は local_usable に依らず LAN。
        assert_eq!(decide(&cfg(&body), true).remote, true);
        assert_eq!(decide(&cfg(&body), false).remote, true);
    }

    // ── env の組み立て ──────────────────────────────────────────
    #[test]
    fn backend_env_は_v1_を一度だけ足す() {
        let d = decide(&cfg(&format!("{URL}use_lan_backend = true\n")), true);
        assert_eq!(backend_env(&d, 8760), "http://127.0.0.1:8080/v1");
        let l = decide(&cfg(""), true);
        assert_eq!(backend_env(&l, 8761), "http://127.0.0.1:8761/v1");
    }

    #[test]
    fn 設定の末尾スラッシュで_v1_が二重にならない() {
        let d = decide(&cfg("use_lan_backend = true\nlan_backend_url = \"http://h:1/\"\n"), true);
        assert_eq!(backend_env(&d, 8760), "http://h:1/v1");
    }

    // ── 移行 ────────────────────────────────────────────────────
    #[test]
    fn 移行_既定の_false_は_auto_になる() {
        let body = "# 説明\nuse_lan_backend = false\nlan_backend_url = \"http://h:1\"\n";
        let out = migrate_machine_default_false(body).expect("移行されるはず");
        assert_eq!(parse_config(&out).choice, Some(LanChoice::Auto));
        assert!(out.contains("lan_backend_url"), "他の行は残る");
        assert!(out.contains("# 説明"), "コメントも残る");
    }

    #[test]
    fn 移行_利用者が選んだ_false_は触らない() {
        let body = format!("use_lan_backend = false\n{PIN_KEY} = true\n");
        assert_eq!(migrate_machine_default_false(&body), None);
    }

    #[test]
    fn 移行_true_と_auto_は触らない() {
        assert_eq!(migrate_machine_default_false("use_lan_backend = true\n"), None);
        assert_eq!(migrate_machine_default_false("use_lan_backend = \"auto\"\n"), None);
        assert_eq!(migrate_machine_default_false(""), None);
    }

    #[test]
    fn 移行は一度きり_二度目は_none() {
        let body = "use_lan_backend = false\n";
        let once = migrate_machine_default_false(body).unwrap();
        assert_eq!(migrate_machine_default_false(&once), None);
    }

    #[test]
    fn 移行後に判定が_auto_として効く() {
        let body = format!("{URL}use_lan_backend = false\n");
        let out = migrate_machine_default_false(&body).unwrap();
        let c = parse_config(&out);
        // ★2026-09-06: 移行後の auto も既定＝LAN。手元が使えても手元へは送らない。
        assert_eq!(decide(&c, false).remote, true, "既定は LAN へ");
        assert_eq!(decide(&c, true).remote, true, "手元が使えても既定は LAN へ");
    }

    // ── 実際に出荷された設定ファイルで確かめる ──────────────────
    //    ★合成した文字列だけで確かめると、本物の書式（コメントの中に true が
    //      入っている・値が引用符つき・行末に説明がある）を1つも通らない。
    //      0.1.194 までが書いていた本文をそのまま貼って測る。
    const SHIPPED_0_1_194: &str = "\
# L.A.P.S Console — 設定

# LAN の推論機（任意）。手元 GPU に載らないモデルの逃げ道。
# ★true にすると、手元の GPU を起こさずこの機械へ推論を投げる（アプリは
#   llama-server を起動しない）。届かないときは黙って手元へ戻らず、
#   画面にそう出す——「気づかないうちに別のマシンで動いていた」を作らないため。
use_lan_backend = false
lan_backend_url = \"http://127.0.0.1:8080\"
lan_model = \"default\"

policy = \"trusted\"
ethics = \"raw\"
max_iters = 0
";

    #[test]
    fn 出荷済みの既定は_false_と読める() {
        let c = parse_config(SHIPPED_0_1_194);
        assert_eq!(c.choice, Some(LanChoice::Local));
        assert_eq!(c.url, "http://127.0.0.1:8080");
        assert_eq!(c.model, "default");
        assert!(!c.pinned);
        // ★これが実機の症状そのもの: 手元に模型が無くても手元へ行く。
        assert_eq!(decide(&c, false).remote, false);
    }

    #[test]
    fn 出荷済みの既定を移行すると常に_lan_へ行く() {
        let fixed = migrate_machine_default_false(SHIPPED_0_1_194).expect("移行されるはず");
        let c = parse_config(&fixed);
        assert_eq!(c.choice, Some(LanChoice::Auto));
        assert_eq!(decide(&c, false).source, Source::AutoLan);
        assert_eq!(decide(&c, true).source, Source::AutoLan); // 2026-09-06: 手元が使えても LAN
        // 他の設定は1文字も動かさない。
        for keep in ["policy", "ethics", "max_iters", "lan_model"] {
            assert!(fixed.contains(keep), "{keep} が消えた");
        }
        assert!(fixed.contains("# ★true にすると"), "コメントも残る");
    }

    #[test]
    fn 画面から_on_にした形は_lan_固定になる() {
        // lan_settings_set は行を丸ごと置き換え、印を末尾に足す。
        let after = SHIPPED_0_1_194
            .replace("use_lan_backend = false", "use_lan_backend = true")
            + &format!("\n{PIN_KEY} = true\n");
        let c = parse_config(&after);
        assert_eq!(c.choice, Some(LanChoice::Lan));
        assert!(c.pinned);
        assert_eq!(decide(&c, true).source, Source::ExplicitLan, "手元が使えても LAN 固定");
        assert_eq!(migrate_machine_default_false(&after), None);
    }

    #[test]
    fn 画面から_off_にした形は移行されない() {
        let after = SHIPPED_0_1_194.to_string() + &format!("\n{PIN_KEY} = true\n");
        assert_eq!(migrate_machine_default_false(&after), None, "意図した OFF は触らない");
        assert_eq!(decide(&parse_config(&after), false).source, Source::ExplicitLocal);
    }

    // ── 引き継ぎ候補 ────────────────────────────────────────────
    #[test]
    fn 引き継ぎ候補は現在の根を除く() {
        let cur = PathBuf::from("/a/Blackwall");
        let got = adoption_candidates(&cur, &[cur.clone(), PathBuf::from("/b/Blackwall")]);
        assert_eq!(got, vec![PathBuf::from("/b/Blackwall/config.toml")]);
    }

    #[test]
    fn 引き継ぎ候補は重複を落とす() {
        let cur = PathBuf::from("/a");
        let b = PathBuf::from("/b");
        let got = adoption_candidates(&cur, &[b.clone(), b.clone()]);
        assert_eq!(got.len(), 1);
    }
}
