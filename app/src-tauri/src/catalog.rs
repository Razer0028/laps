//! モデル候補の厳選カタログ。
//!
//! 目的（オーナー要望）: 「アプリでウェブからダウンロードできる候補を一覧で表示したい」。
//! `hf_search` は Hugging Face の検索結果をそのまま返すが、GGUF リポジトリは同名・同量子化が
//! 何十も並び、初見では選べない。ここでは**コードに焼いた固定リスト**だけを見せる。
//! 検索の海を見せないことがこのモジュールの価値で、件数を増やすことではない。
//!
//! ラボの方針: 無検閲(abliterated)版を優先。用途はインフラ・開発・コーディング。
//!
//! ★固定リストの repo / file / size は 2026-08-11 に HF の実 API で実在とサイズを確認した値。
//!   **確認していないものをここに書かない。** 存在しない repo 名を書くと、利用者には
//!   「アプリが壊れている」としか見えず、しかも一覧の他の行まで信用できなくなる。
//!
//! 起動時に同じ値を HF へ問い合わせ直して上書きする（量子化のやり直しでサイズが変わる、
//! リポジトリが消える、といったことが実際に起きるため）。ネットワークが無い場合は
//! 固定値のまま `verified=false` で返す —— 一覧が出ないより、出所の怪しさを明示して出す方がよい。
//!
//! 使う HF エンドポイント（hf.rs が使う4本とは別・これ1本だけ）:
//!   POST /api/models/{repo}/paths-info/{rev}  body {"paths":[...]}
//!     → [{ path, size, lfs:{size,oid}, ... }] の配列。**存在しないパスは配列から落ちるだけで
//!       エラーにはならない**（2026-08-11 実測）。だから「答えが返ったのに居ない＝無い」と読める。

use std::sync::OnceLock;
use std::time::Duration;

use serde::{Deserialize, Serialize};

// hf.rs にも同名の定数があるが、あちらでは private で、hf.rs はこの作業では改変しない。
// 3 つの &str を共有するために公開範囲を広げるのは割に合わないので、ここで持つ。
const HF_HOST: &str = "https://huggingface.co";
const REVISION: &str = "main";
const USER_AGENT: &str = "blackwall-console/0.1";

/// 実サイズの確認にかける時間の上限。ここで長く待つと起動が止まって見える。
/// 落とすのは 8〜18GB の本体で、この確認は数百バイトの JSON。届かないならすぐ諦めてよい。
const PROBE_TIMEOUT: Duration = Duration::from_secs(12);

/// 「概ね載る」の判定に足す余白（MB）。KV と計算バッファと llama.cpp の fitter 余白の合計を
/// 小さめの文脈で見積もった概数。**厳密な判定は fit.rs の仕事**で、ここは並べ替えの
/// ためだけに使う。fit.rs の定数は private なので参照できないが、参照できたとしても
/// 借りるべきではない —— あちらは文脈長を受け取って正確に出す関数で、ここは受け取らない。
const ROUGH_OVERHEAD_MB: u64 = 1400;

// ---------------------------------------------------------------------------
// 公開型（画面が依存する固定インターフェース）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CatalogEntry {
    /// 例 mradermacher/Qwen3-14B-abliterated-GGUF
    pub repo: String,
    /// 例 Qwen3-14B-abliterated.Q4_K_M.gguf
    pub file: String,
    /// 画面に出す名前（人間向け）
    pub label: String,
    pub size_bytes: u64,
    /// 14.0 など。MoE は総パラメータ数（実際に動くのは一部）。
    pub params_b: f32,
    /// 無検閲(abliterated)版か
    pub uncensored: bool,
    /// コード特化か
    pub coder: bool,
    /// 一言（何に向くか・代償）
    pub note: String,
    /// HF で実在とサイズを確認できたか。false は「無い」ではなく「確かめられていない」。
    pub verified: bool,
}

// ---------------------------------------------------------------------------
// 固定リスト
// ---------------------------------------------------------------------------

/// 固定リストの素。`CatalogEntry` を const で持てない（String を含む）ので、
/// &'static str の表として持って起動時に一度だけ組み立てる。
struct Seed {
    repo: &'static str,
    file: &'static str,
    label: &'static str,
    /// 2026-08-11 に HF で確認したバイト数。起動時の照会が通れば上書きされる。
    size_bytes: u64,
    params_b: f32,
    uncensored: bool,
    coder: bool,
    note: &'static str,
}

/// 並び順はここでは意味を持たない（返す前に載る順・無検閲優先・小さい順で並べ替える）。
/// 大きい順に書いてあるのは人が読んで確認しやすいから。
const SEEDS: &[Seed] = &[
    // --- 30B MoE（12GB には載らない。CPU 分担が要る） ---------------------
    Seed {
        repo: "mradermacher/Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated-GGUF",
        file: "Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated.Q4_K_M.gguf",
        label: "Qwen3-Coder 30B-A3B 無検閲 (Q4_K_M)",
        size_bytes: 18_556_689_600,
        params_b: 30.0,
        uncensored: true,
        coder: true,
        note: "コード用の最上位。MoE なので動くのは一部だけ＝速いが、12GB には載らず experts を CPU に降ろす必要がある。",
    },
    Seed {
        repo: "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF",
        file: "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        label: "Qwen3-Coder 30B-A3B (Q4_K_M)",
        size_bytes: 18_556_689_568,
        params_b: 30.0,
        uncensored: false,
        coder: true,
        note: "上と同じ土台の無改変版。検閲は残るが、素の挙動と比べたいときの対照になる。",
    },
    // --- 14B（12GB 級の本命） ---------------------------------------------
    Seed {
        repo: "mradermacher/Qwen3-14B-abliterated-GGUF",
        file: "Qwen3-14B-abliterated.Q4_K_M.gguf",
        label: "Qwen3 14B 無検閲 (Q4_K_M)",
        size_bytes: 9_001_753_408,
        params_b: 14.0,
        uncensored: true,
        coder: false,
        note: "12GB 級 GPU の本命。土台は mlabonne/Qwen3-14B-abliterated。文脈を 4096 まで縮めれば 12GB に収まる。",
    },
    Seed {
        repo: "unsloth/Qwen3-14B-GGUF",
        file: "Qwen3-14B-Q4_K_M.gguf",
        label: "Qwen3 14B (Q4_K_M)",
        size_bytes: 9_001_753_984,
        params_b: 14.0,
        uncensored: false,
        coder: false,
        note: "同じ 14B の無改変版。無検閲版の応答が崩れているか判断するときの比較対象。",
    },
    // --- 4〜8B（8GB 以下の GPU 向け） --------------------------------------
    Seed {
        repo: "mradermacher/Huihui-Qwen3-8B-abliterated-v2-GGUF",
        file: "Huihui-Qwen3-8B-abliterated-v2.Q4_K_M.gguf",
        label: "Qwen3 8B 無検閲 v2 (Q4_K_M)",
        size_bytes: 5_027_780_352,
        params_b: 8.0,
        uncensored: true,
        coder: false,
        note: "8GB GPU 向けの無検閲。14B より素直に速いが、長い手順を最後までやり切る力は落ちる。",
    },
    Seed {
        repo: "bartowski/Qwen2.5-Coder-7B-Instruct-abliterated-GGUF",
        file: "Qwen2.5-Coder-7B-Instruct-abliterated-Q4_K_M.gguf",
        label: "Qwen2.5-Coder 7B 無検閲 (Q4_K_M)",
        size_bytes: 4_683_074_400,
        params_b: 7.0,
        uncensored: true,
        coder: true,
        note: "小さいコード特化。世代は一つ古い(Qwen2.5)ぶん、道具呼び出しの書式は Qwen3 系より弱い。",
    },
    Seed {
        repo: "unsloth/Qwen3-8B-GGUF",
        file: "Qwen3-8B-Q4_K_M.gguf",
        label: "Qwen3 8B (Q4_K_M)",
        size_bytes: 5_027_784_512,
        params_b: 8.0,
        uncensored: false,
        coder: false,
        note: "8B の無改変版。まず動かして経路を確かめる用途に向く。",
    },
    Seed {
        repo: "mradermacher/Huihui-Qwen3-4B-Instruct-2507-abliterated-GGUF",
        file: "Huihui-Qwen3-4B-Instruct-2507-abliterated.Q4_K_M.gguf",
        label: "Qwen3 4B Instruct 2507 無検閲 (Q4_K_M)",
        size_bytes: 2_497_281_312,
        params_b: 4.0,
        uncensored: true,
        coder: false,
        note: "一覧で最小。6GB 以下でも動く代わりに、複数手順の作業は途中で崩れやすい。",
    },
    Seed {
        repo: "unsloth/Qwen3-4B-Instruct-2507-GGUF",
        file: "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        label: "Qwen3 4B Instruct 2507 (Q4_K_M)",
        size_bytes: 2_497_281_120,
        params_b: 4.0,
        uncensored: false,
        coder: false,
        note: "最小の無改変版。GPU が非力なとき、まず経路を通すための入口。",
    },
];

fn curated() -> Vec<CatalogEntry> {
    SEEDS
        .iter()
        // 固定リストなので普通は全て通る。表に打ち間違いが入ったときに、その行を
        // URL 組み立てまで運ばないための保険（後段でエスケープせず format! するため）。
        .filter(|s| is_safe_repo(s.repo) && is_safe_file(s.file))
        .map(|s| CatalogEntry {
            repo: s.repo.to_string(),
            file: s.file.to_string(),
            label: s.label.to_string(),
            size_bytes: s.size_bytes,
            params_b: s.params_b,
            uncensored: s.uncensored,
            coder: s.coder,
            note: s.note.to_string(),
            // 照会が通るまでは「確かめていない」。ここを true 始まりにすると、
            // オフライン時に固定値が確認済みとして画面に出てしまう。
            verified: false,
        })
        .collect()
}

/// URL のパス片としてそのまま埋めてよい文字だけか。hf.rs の `encode_path` は private で、
/// ここでは固定リストしか扱わない＝エスケープではなく**通さない**方が正しい。
fn is_safe_segment(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 200
        && !s.contains("..")
        && s
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
}

fn is_safe_repo(repo: &str) -> bool {
    let mut parts = repo.split('/');
    match (parts.next(), parts.next(), parts.next()) {
        (Some(owner), Some(name), None) => is_safe_segment(owner) && is_safe_segment(name),
        _ => false,
    }
}

/// 固定リストの GGUF は今のところ全てリポジトリ直下にある。将来 `Q4_K_M/xxx.gguf` の形を
/// 足すときは、ここと paths-info の投げ方を両方見直すこと。
fn is_safe_file(file: &str) -> bool {
    is_safe_segment(file) && file.to_ascii_lowercase().ends_with(".gguf")
}

// ---------------------------------------------------------------------------
// HF への実在確認
// ---------------------------------------------------------------------------

static CATALOG_CLIENT: OnceLock<reqwest::Client> = OnceLock::new();

fn client() -> Result<&'static reqwest::Client, String> {
    if let Some(c) = CATALOG_CLIENT.get() {
        return Ok(c);
    }
    let built = reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .connect_timeout(Duration::from_secs(6))
        .timeout(PROBE_TIMEOUT)
        .build()
        .map_err(|e| format!("HTTP クライアントを作れません: {e}"))?;
    // 競合しても余分な Client が捨てられるだけ（hf.rs と同じ扱い）。
    let _ = CATALOG_CLIENT.set(built);
    CATALOG_CLIENT
        .get()
        .ok_or_else(|| "HTTP クライアントの初期化に失敗しました".to_string())
}

#[derive(Deserialize)]
struct PathInfo {
    #[serde(default)]
    path: String,
    #[serde(default)]
    size: u64,
    #[serde(default)]
    lfs: Option<PathLfs>,
}

#[derive(Deserialize)]
struct PathLfs {
    #[serde(default)]
    size: Option<u64>,
}

/// 1 リポジトリぶんの照会結果。
///
/// ★この2つを混ぜてはいけない。「HF が答えた上で居ない」は存在の否定だが、
/// 「届かなかった」は何の否定でもない。同じ扱いにすると、社内 LAN が切れているだけで
/// 一覧が空になる（オフラインでも一覧は出す、という要件に真っ向から反する）。
#[derive(Debug, Clone, PartialEq)]
enum RepoProbe {
    /// HF が答えた。(path, size) の一覧。ここに無いパスは「本当に無い」。
    Answered(Vec<(String, u64)>),
    /// 届かなかった / HF 側の一時障害。固定値のまま verified=false で残す。
    Unreachable,
}

async fn probe_repo(repo: String, paths: Vec<String>) -> RepoProbe {
    let c = match client() {
        Ok(c) => c,
        Err(_) => return RepoProbe::Unreachable,
    };
    let url = format!("{HF_HOST}/api/models/{repo}/paths-info/{REVISION}");
    let resp = match c
        .post(&url)
        .json(&serde_json::json!({ "paths": paths }))
        .send()
        .await
    {
        Ok(r) => r,
        Err(_) => return RepoProbe::Unreachable,
    };

    let status = resp.status();
    if !status.is_success() {
        // 404 は「そのリポジトリは無い」という HF の答えなので、存在の否定として扱う
        // （消えたリポジトリを一覧に残さないため）。それ以外の失敗は向こう側の都合。
        return if status == reqwest::StatusCode::NOT_FOUND {
            RepoProbe::Answered(Vec::new())
        } else {
            RepoProbe::Unreachable
        };
    }

    match resp.json::<Vec<PathInfo>>().await {
        Ok(items) => RepoProbe::Answered(
            items
                .into_iter()
                .filter(|i| !i.path.is_empty())
                .map(|i| {
                    // LFS 実体のサイズを優先。GGUF は必ず LFS なので通常こちらが入る
                    // （top-level の size はポインタ側の値になることがある）。
                    let size = i.lfs.as_ref().and_then(|l| l.size).unwrap_or(i.size);
                    (i.path, size)
                })
                .collect(),
        ),
        // 形が変わったときに、古い固定値を「確認済み」として出さない。
        Err(_) => RepoProbe::Unreachable,
    }
}

/// 照会結果を固定リストへ畳み込む。**純粋関数**にしてあるのは、この判断（残す/落とす/
/// サイズを上書きする）がこのモジュールで一番間違えやすく、網無しでは直せないから。
fn merge(entries: Vec<CatalogEntry>, probes: &[(String, RepoProbe)]) -> Vec<CatalogEntry> {
    entries
        .into_iter()
        .filter_map(|mut e| {
            let probe = probes.iter().find(|(r, _)| *r == e.repo).map(|(_, p)| p);
            match probe {
                Some(RepoProbe::Answered(found)) => {
                    match found.iter().find(|(p, _)| *p == e.file) {
                        Some((_, size)) => {
                            // 実サイズで上書きする。量子化のやり直しでバイト数は動く。
                            if *size > 0 {
                                e.size_bytes = *size;
                            }
                            e.verified = true;
                            Some(e)
                        }
                        // HF が答えたのに居ない＝消えた/名前が変わった。見せない。
                        None => None,
                    }
                }
                // 照会できなかったものは概算のまま残す（オフラインでも一覧は出す）。
                Some(RepoProbe::Unreachable) | None => Some(e),
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// 並べ替え
// ---------------------------------------------------------------------------

/// 「概ね載る」か。素朴でよい（厳密な判定は fit.rs の仕事）。
/// `vram_free_mb == 0` は GPU を見られなかった場合＝**載らない扱いにはせず、
/// 判定材料が無いものとして全件を同じ扱いにする**（後段で無検閲・小さい順だけが効く）。
fn roughly_fits(size_bytes: u64, vram_free_mb: u64) -> bool {
    if vram_free_mb == 0 {
        return false;
    }
    let weights_mb = size_bytes / (1024 * 1024);
    weights_mb + ROUGH_OVERHEAD_MB <= vram_free_mb
}

fn sort_entries(entries: &mut [CatalogEntry], vram_free_mb: u64) {
    entries.sort_by(|a, b| {
        let key = |e: &CatalogEntry| {
            (
                // 1. その GPU に載るものを先に
                !roughly_fits(e.size_bytes, vram_free_mb),
                // 2. 無検閲を優先（ラボの方針）
                !e.uncensored,
                // 3. 小さい順
                e.size_bytes,
            )
        };
        key(a).cmp(&key(b))
    });
}

// ---------------------------------------------------------------------------
// コマンド
// ---------------------------------------------------------------------------

#[cfg_attr(not(feature = "no_tauri"), tauri::command)]
pub async fn catalog_list(vram_free_mb: u64) -> Result<Vec<CatalogEntry>, String> {
    let entries = curated();

    // リポジトリ単位にまとめてから投げる。paths-info は複数パスを1回で受けるので、
    // 同じリポジトリの別量子化を将来足しても要求数は増えない。
    let mut grouped: Vec<(String, Vec<String>)> = Vec::new();
    for e in &entries {
        match grouped.iter_mut().find(|(r, _)| *r == e.repo) {
            Some((_, paths)) => paths.push(e.file.clone()),
            None => grouped.push((e.repo.clone(), vec![e.file.clone()])),
        }
    }

    // 直列に投げると 9 リポジトリ × タイムアウトで最悪 100 秒級になる。
    // 起動時に一覧が出ないのと同じなので並行に投げる（tokio のみ・futures は足さない）。
    let mut jobs = Vec::with_capacity(grouped.len());
    for (repo, paths) in grouped {
        let handle = tokio::spawn(probe_repo(repo.clone(), paths));
        jobs.push((repo, handle));
    }

    let mut probes: Vec<(String, RepoProbe)> = Vec::with_capacity(jobs.len());
    for (repo, handle) in jobs {
        // join の失敗（panic・キャンセル）は「届かなかった」と同じに畳む。
        // ここで Err を返すと、1件の事故で一覧全体が消える。
        let probe = handle.await.unwrap_or(RepoProbe::Unreachable);
        probes.push((repo, probe));
    }

    let mut out = merge(entries, &probes);
    sort_entries(&mut out, vram_free_mb);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 固定リストの打ち間違いは URL 組み立てまで運ばれる（エスケープしていない）。
    /// 表を触ったときに気づけるよう、形だけは機械で見張る。
    #[test]
    fn seeds_are_well_formed() {
        assert!(SEEDS.len() >= 7 && SEEDS.len() <= 10, "厳選である意味が消える件数");
        for s in SEEDS {
            assert!(is_safe_repo(s.repo), "repo の形が不正: {}", s.repo);
            assert!(is_safe_file(s.file), "file の形が不正: {}", s.file);
            assert!(!s.label.is_empty() && !s.note.is_empty(), "{}", s.repo);
            assert!(s.size_bytes > 0, "{}", s.repo);
            assert!(s.params_b > 0.0, "{}", s.repo);
        }
        // curated() が1行も落とさない＝表が全て健全であること。
        assert_eq!(curated().len(), SEEDS.len());
    }

    #[test]
    fn repo_and_file_pairs_are_unique() {
        let mut seen: Vec<(&str, &str)> = Vec::new();
        for s in SEEDS {
            assert!(!seen.contains(&(s.repo, s.file)), "重複: {}/{}", s.repo, s.file);
            seen.push((s.repo, s.file));
        }
    }

    #[test]
    fn traversal_and_odd_names_are_rejected() {
        assert!(!is_safe_repo("owner"));
        assert!(!is_safe_repo("owner/name/extra"));
        assert!(!is_safe_repo("owner/../name"));
        assert!(!is_safe_repo("owner/na me"));
        assert!(is_safe_repo("unsloth/Qwen3-14B-GGUF"));
        // 直下の .gguf のみ。サブフォルダ付きは今の投げ方では扱わない。
        assert!(!is_safe_file("Q4_K_M/x.gguf"));
        assert!(!is_safe_file("x.safetensors"));
        assert!(is_safe_file("Qwen3-14B-Q4_K_M.gguf"));
    }

    fn entry(repo: &str, file: &str, size: u64, uncensored: bool) -> CatalogEntry {
        CatalogEntry {
            repo: repo.to_string(),
            file: file.to_string(),
            label: "x".to_string(),
            size_bytes: size,
            params_b: 1.0,
            uncensored,
            coder: false,
            note: "x".to_string(),
            verified: false,
        }
    }

    #[test]
    fn answered_but_absent_is_dropped() {
        let entries = vec![entry("a/b", "gone.gguf", 100, false)];
        let probes = vec![("a/b".to_string(), RepoProbe::Answered(Vec::new()))];
        assert!(merge(entries, &probes).is_empty());
    }

    #[test]
    fn answered_size_overwrites_and_marks_verified() {
        let entries = vec![entry("a/b", "x.gguf", 100, false)];
        let probes = vec![(
            "a/b".to_string(),
            RepoProbe::Answered(vec![("x.gguf".to_string(), 999)]),
        )];
        let out = merge(entries, &probes);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].size_bytes, 999);
        assert!(out[0].verified);
    }

    /// 一番守りたい性質: 回線が無いだけで一覧が空にならないこと。
    #[test]
    fn unreachable_keeps_the_row_unverified() {
        let entries = vec![entry("a/b", "x.gguf", 100, false)];
        let probes = vec![("a/b".to_string(), RepoProbe::Unreachable)];
        let out = merge(entries, &probes);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].size_bytes, 100, "概算のまま残す");
        assert!(!out[0].verified);
    }

    #[test]
    fn fitting_rows_come_first_then_uncensored_then_small() {
        const GIB: u64 = 1024 * 1024 * 1024;
        let mut v = vec![
            entry("a/big-unc", "1.gguf", 17 * GIB, true),
            entry("a/small-plain", "2.gguf", 2 * GIB, false),
            entry("a/small-unc", "3.gguf", 4 * GIB, true),
            entry("a/mid-unc", "4.gguf", 8 * GIB, true),
        ];
        // RTX 4070 の実測に近い空き
        sort_entries(&mut v, 10_339);
        let order: Vec<&str> = v.iter().map(|e| e.repo.as_str()).collect();
        assert_eq!(
            order,
            vec!["a/small-unc", "a/mid-unc", "a/small-plain", "a/big-unc"],
            "載る無検閲→載る無改変→載らないもの、の順"
        );
    }

    /// GPU が読めなかったとき（0）は載る/載らないで並べ替えない。
    #[test]
    fn unknown_vram_falls_back_to_uncensored_then_size() {
        const GIB: u64 = 1024 * 1024 * 1024;
        let mut v = vec![
            entry("a/plain-small", "1.gguf", 2 * GIB, false),
            entry("a/unc-big", "2.gguf", 17 * GIB, true),
            entry("a/unc-small", "3.gguf", 4 * GIB, true),
        ];
        sort_entries(&mut v, 0);
        let order: Vec<&str> = v.iter().map(|e| e.repo.as_str()).collect();
        assert_eq!(order, vec!["a/unc-small", "a/unc-big", "a/plain-small"]);
    }
}
