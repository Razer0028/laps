//! ローカルの `.gguf` から**ヘッダ（メタデータ）だけ**を読む。
//!
//! なぜ要るか: fit.rs はこれまで「パラメータ数からの段階表」で層数と KV 次元を推測していた。
//! 手元にファイルがあるなら推測する理由が無い。実際、RTX 4070（空き 10,339 MB）に
//! Qwen3-14B-abliterated Q4_K_M を載せる場面で、推測は KV を 1,920 MB と見たが
//! 実値は 1,280 MB（ctx 8192）で、**1.5 倍外していた**。GQA（head_count 40 に対し
//! head_count_kv 8）を見ていなかったのが原因。ヘッダは数百 KB 手前にあり、読めば確定する。
//!
//! 方針:
//! - **全部は読まない。** 語彙配列（tokenizer.ggml.tokens）だけで数 MB あるので、
//!   欲しい鍵が揃った時点で打ち切る。揃わなくても kv_count を読み切ったら終わる。
//! - **panic しない。** 壊れたファイル・途中で切れたファイルは Err を返す。
//!   長さ欄が壊れている場合に備えて、確保する前に上限で弾く（悪意あるファイルで
//!   数 GB の Vec を確保しないため）。
//!
//! 形式（実測で確認済み・リトルエンディアン）:
//!   "GGUF"(4B) → version u32 → tensor_count u64 → kv_count u64
//!   以降 kv_count 回: key(u64 長 + UTF-8) → value_type u32 → 値
//!   型: 0..=12 = u8,i8,u16,i16,u32,i32,f32,bool,string,array,u64,i64,f64
//!   array = element_type u32 + len u64 + 要素列

use std::fs::File;
use std::io::{BufReader, Read, Seek};
use std::path::Path;

use serde::Serialize;

/// 見積もりに要る鍵だけ。ここに無いものは読み飛ばす。
///
/// 鍵の接頭辞はアーキテクチャ名（`qwen3.block_count` など）なので**末尾一致**で拾う。
#[derive(Debug, Clone, Default, Serialize)]
pub struct GgufMeta {
    pub arch: Option<String>,
    pub n_layers: Option<u32>,       // *.block_count
    pub n_embd: Option<u32>,         // *.embedding_length
    pub n_head: Option<u32>,         // *.attention.head_count
    pub n_head_kv: Option<u32>,      // *.attention.head_count_kv
    pub key_length: Option<u32>,     // *.attention.key_length
    pub value_length: Option<u32>,   // *.attention.value_length
    pub context_length: Option<u32>, // *.context_length
}

/// KV キャッシュの実寸を出すのに要る4値。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct KvGeometry {
    pub n_layers: u32,
    pub n_head_kv: u32,
    pub key_length: u32,
    pub value_length: u32,
}

impl GgufMeta {
    /// KV ヘッド数。`head_count_kv` が無いモデル（GQA 以前）は MHA なので head_count と等しい
    /// ——これは llama.cpp の既定と同じ解釈。
    pub fn n_head_kv_eff(&self) -> Option<u32> {
        self.n_head_kv.or(self.n_head)
    }

    /// K の head_dim。省略時の既定は embedding_length / head_count（llama.cpp と同じ）。
    pub fn key_length_eff(&self) -> Option<u32> {
        if let Some(k) = self.key_length {
            return Some(k);
        }
        let (embd, head) = (self.n_embd?, self.n_head?);
        if head == 0 {
            return None;
        }
        Some(embd / head)
    }

    /// V の head_dim。省略時は K と同じ（MLA のように K と V で幅が違う型があるので分けてある）。
    pub fn value_length_eff(&self) -> Option<u32> {
        self.value_length.or_else(|| self.key_length_eff())
    }

    /// 4値が揃ったときだけ Some。**1つでも欠けたら推測に落とすかどうかは呼び出し側の判断**なので、
    /// ここで勝手に既定値を埋めない（埋めると「実値で計算した」という顔をした推測になる）。
    pub fn kv_geometry(&self) -> Option<KvGeometry> {
        let g = KvGeometry {
            n_layers: self.n_layers?,
            n_head_kv: self.n_head_kv_eff()?,
            key_length: self.key_length_eff()?,
            value_length: self.value_length_eff()?,
        };
        // 0 が混じると以降の計算が黙って 0 になる。壊れた値は無いものとして扱う。
        if g.n_layers == 0 || g.n_head_kv == 0 || g.key_length == 0 || g.value_length == 0 {
            return None;
        }
        Some(g)
    }

    fn is_complete(&self) -> bool {
        self.arch.is_some()
            && self.n_layers.is_some()
            && self.n_embd.is_some()
            && self.n_head.is_some()
            && self.n_head_kv.is_some()
            && self.key_length.is_some()
            && self.value_length.is_some()
            && self.context_length.is_some()
    }
}

// ---------------------------------------------------------------------------
// 上限。壊れた／悪意ある長さ欄で確保も無限ループもしないための門。
// ---------------------------------------------------------------------------

/// メタデータの鍵の個数。実物は数十〜数百。
const MAX_KV_COUNT: u64 = 1 << 20;
/// 1つの文字列の長さ。chat template が数十 KB あるので余裕を見て 16 MiB。
const MAX_STR_LEN: u64 = 16 << 20;
/// 配列の要素数。語彙は数十万なので 16M で十分に上。
const MAX_ARRAY_LEN: u64 = 16 << 20;

/// ローカルの .gguf からメタデータを読む。
pub fn read_meta(path: &Path) -> Result<GgufMeta, String> {
    let f = File::open(path).map_err(|e| format!("{} を開けません: {e}", path.display()))?;
    // 64 KiB。語彙配列を読み飛ばすとき seek_relative がバッファ内で収まりやすい大きさ。
    let mut r = BufReader::with_capacity(64 * 1024, f);
    read_meta_from(&mut r).map_err(|e| format!("{}: {e}", path.display()))
}

fn read_meta_from<R: Read + Seek>(r: &mut BufReader<R>) -> Result<GgufMeta, String> {
    let mut magic = [0u8; 4];
    r.read_exact(&mut magic)
        .map_err(|e| format!("先頭を読めません（GGUF ではない可能性）: {e}"))?;
    if &magic != b"GGUF" {
        return Err(format!(
            "GGUF ではありません（先頭 4 バイトが {:02X?}）",
            magic
        ));
    }

    let version = read_u32(r)?;
    // v1 は長さ欄が u32 で、この実装では読めない。llama.cpp 本体も既に落としている。
    // 未知の将来版を「たぶん同じ」で読むと静かに誤読するので、素直に断る。
    if !(2..=3).contains(&version) {
        return Err(format!("未対応の GGUF version {version}（対応は 2 と 3）"));
    }

    let _tensor_count = read_u64(r)?;
    let kv_count = read_u64(r)?;
    if kv_count > MAX_KV_COUNT {
        return Err(format!("メタデータの個数が異常です（{kv_count}）"));
    }

    let mut meta = GgufMeta::default();
    for _ in 0..kv_count {
        let key = read_string(r)?;
        let vtype = read_u32(r)?;
        match classify(&key) {
            // 同じ鍵が二度出たら先勝ち。後勝ちにすると、mmproj 同梱などで
            // 別の塔（vision 側）の値に静かに上書きされうる。
            Some(field) if !filled(&meta, field) => {
                let val = read_value(r, vtype, true)?;
                assign(&mut meta, field, val);
            }
            _ => {
                read_value(r, vtype, false)?;
            }
        }
        if meta.is_complete() {
            // 語彙配列まで読み進める理由が無い。ここで打ち切るのがこの関数の速さの本体。
            break;
        }
    }
    Ok(meta)
}

// ---------------------------------------------------------------------------
// 鍵の対応付け
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Field {
    Arch,
    Layers,
    Embd,
    Head,
    HeadKv,
    KeyLen,
    ValLen,
    CtxLen,
}

fn classify(key: &str) -> Option<Field> {
    // head_count_kv を head_count より先に見る。ends_with なので取り違えは起きないが、
    // 読む人が誤読しやすいので順序でも意図を示しておく。
    if key == "general.architecture" {
        Some(Field::Arch)
    } else if key.ends_with(".attention.head_count_kv") {
        Some(Field::HeadKv)
    } else if key.ends_with(".attention.head_count") {
        Some(Field::Head)
    } else if key.ends_with(".attention.key_length") {
        Some(Field::KeyLen)
    } else if key.ends_with(".attention.value_length") {
        Some(Field::ValLen)
    } else if key.ends_with(".block_count") {
        Some(Field::Layers)
    } else if key.ends_with(".embedding_length") {
        Some(Field::Embd)
    } else if key.ends_with(".context_length") {
        Some(Field::CtxLen)
    } else {
        None
    }
}

fn filled(meta: &GgufMeta, field: Field) -> bool {
    match field {
        Field::Arch => meta.arch.is_some(),
        Field::Layers => meta.n_layers.is_some(),
        Field::Embd => meta.n_embd.is_some(),
        Field::Head => meta.n_head.is_some(),
        Field::HeadKv => meta.n_head_kv.is_some(),
        Field::KeyLen => meta.key_length.is_some(),
        Field::ValLen => meta.value_length.is_some(),
        Field::CtxLen => meta.context_length.is_some(),
    }
}

fn assign(meta: &mut GgufMeta, field: Field, val: Option<Val>) {
    match (field, val) {
        (Field::Arch, Some(Val::Str(s))) => meta.arch = Some(s),
        (Field::Layers, Some(Val::Int(n))) => meta.n_layers = to_u32(n),
        (Field::Embd, Some(Val::Int(n))) => meta.n_embd = to_u32(n),
        (Field::Head, Some(Val::Int(n))) => meta.n_head = to_u32(n),
        (Field::HeadKv, Some(Val::Int(n))) => meta.n_head_kv = to_u32(n),
        (Field::KeyLen, Some(Val::Int(n))) => meta.key_length = to_u32(n),
        (Field::ValLen, Some(Val::Int(n))) => meta.value_length = to_u32(n),
        (Field::CtxLen, Some(Val::Int(n))) => meta.context_length = to_u32(n),
        // 型が想定と違う（例: block_count が文字列）ときは黙って無視する。
        // 埋めないので、呼び出し側は「不明」として扱える＝嘘の実値を返さない。
        _ => {}
    }
}

/// 負値と u32 を超える値は捨てる。層数やヘッド数が u32 を超えることは無く、
/// 超えているならそのファイルの値のほうが壊れている。
fn to_u32(n: i128) -> Option<u32> {
    u32::try_from(n).ok()
}

// ---------------------------------------------------------------------------
// 値の読み取り／読み飛ばし
// ---------------------------------------------------------------------------

enum Val {
    Int(i128),
    Str(String),
}

/// 値を1つ消費する。`capture` が false なら中身を捨てる（＝読み飛ばし）が、
/// **構造は必ず解析する**。長さが分からないと次の鍵の位置が決まらないため。
fn read_value<R: Read + Seek>(
    r: &mut BufReader<R>,
    vtype: u32,
    capture: bool,
) -> Result<Option<Val>, String> {
    match vtype {
        8 => {
            if capture {
                Ok(Some(Val::Str(read_string(r)?)))
            } else {
                skip_string(r)?;
                Ok(None)
            }
        }
        9 => {
            skip_array(r)?;
            Ok(None) // 欲しい鍵に配列型のものは無い。
        }
        t => {
            let n = read_scalar(r, t)?;
            Ok(if capture { n.map(Val::Int) } else { None })
        }
    }
}

/// 数値・真偽の1個。f32/f64 は消費だけして None を返す（整数として扱えないため）。
fn read_scalar<R: Read>(r: &mut BufReader<R>, t: u32) -> Result<Option<i128>, String> {
    Ok(match t {
        0 => Some(i128::from(read_n::<_, 1>(r)?[0])),
        1 => Some(i128::from(read_n::<_, 1>(r)?[0] as i8)),
        2 => Some(i128::from(u16::from_le_bytes(read_n(r)?))),
        3 => Some(i128::from(i16::from_le_bytes(read_n(r)?))),
        4 => Some(i128::from(u32::from_le_bytes(read_n(r)?))),
        5 => Some(i128::from(i32::from_le_bytes(read_n(r)?))),
        6 => {
            read_n::<_, 4>(r)?; // f32
            None
        }
        7 => Some(i128::from(read_n::<_, 1>(r)?[0] != 0)), // bool は 1 バイト
        10 => Some(i128::from(u64::from_le_bytes(read_n(r)?))),
        11 => Some(i128::from(i64::from_le_bytes(read_n(r)?))),
        12 => {
            read_n::<_, 8>(r)?; // f64
            None
        }
        other => return Err(format!("未知の値型 {other}")),
    })
}

/// 配列を読み飛ばす。
/// ★文字列配列は要素ごとに長さが違うので幅で飛ばせず、1つずつ読み進めるしかない。
///   数値配列は要素幅×個数で一度に飛ばす。
fn skip_array<R: Read + Seek>(r: &mut BufReader<R>) -> Result<(), String> {
    let elem_type = read_u32(r)?;
    let len = read_u64(r)?;
    if len > MAX_ARRAY_LEN {
        return Err(format!("配列の要素数が異常です（{len}）"));
    }
    match elem_type {
        8 => {
            for _ in 0..len {
                skip_string(r)?;
            }
            Ok(())
        }
        // 入れ子配列は GGUF に実例が無い。読めるふりをして誤読するより断る。
        9 => Err("入れ子の配列には対応していません".to_string()),
        t => {
            let w = scalar_width(t).ok_or_else(|| format!("未知の配列要素型 {t}"))?;
            let bytes = w
                .checked_mul(len)
                .ok_or_else(|| "配列の大きさが桁あふれしています".to_string())?;
            skip(r, bytes)
        }
    }
}

fn scalar_width(t: u32) -> Option<u64> {
    Some(match t {
        0 | 1 | 7 => 1,
        2 | 3 => 2,
        4 | 5 | 6 => 4,
        10 | 11 | 12 => 8,
        _ => return None,
    })
}

fn read_string<R: Read>(r: &mut BufReader<R>) -> Result<String, String> {
    let len = read_u64(r)?;
    if len > MAX_STR_LEN {
        return Err(format!("文字列の長さが異常です（{len}）"));
    }
    // 長さは上で押さえてあるので確保してよい。
    let mut buf = vec![0u8; len as usize];
    r.read_exact(&mut buf)
        .map_err(|e| format!("文字列を読めません: {e}"))?;
    String::from_utf8(buf).map_err(|_| "文字列が UTF-8 ではありません".to_string())
}

fn skip_string<R: Read + Seek>(r: &mut BufReader<R>) -> Result<(), String> {
    let len = read_u64(r)?;
    if len > MAX_STR_LEN {
        return Err(format!("文字列の長さが異常です（{len}）"));
    }
    skip(r, len)
}

/// 前方へ n バイト飛ばす。
/// EOF を越えても seek 自体は成功しうるが、その場合は次の read_exact が失敗するので
/// 「壊れたファイルが Err になる」性質は保たれる。
fn skip<R: Read + Seek>(r: &mut BufReader<R>, n: u64) -> Result<(), String> {
    let n = i64::try_from(n).map_err(|_| "読み飛ばし量が大きすぎます".to_string())?;
    r.seek_relative(n)
        .map_err(|e| format!("読み飛ばしに失敗しました: {e}"))
}

fn read_n<R: Read, const N: usize>(r: &mut BufReader<R>) -> Result<[u8; N], String> {
    let mut b = [0u8; N];
    r.read_exact(&mut b)
        .map_err(|e| format!("{N} バイトを読めません（ファイルが途中で切れています）: {e}"))?;
    Ok(b)
}

fn read_u32<R: Read>(r: &mut BufReader<R>) -> Result<u32, String> {
    Ok(u32::from_le_bytes(read_n(r)?))
}

fn read_u64<R: Read>(r: &mut BufReader<R>) -> Result<u64, String> {
    Ok(u64::from_le_bytes(read_n(r)?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    // -- 合成 GGUF の組み立て（実ファイルを置かずに壊れ方も試せるようにする） ----

    fn s(v: &str) -> Vec<u8> {
        let mut o = (v.len() as u64).to_le_bytes().to_vec();
        o.extend_from_slice(v.as_bytes());
        o
    }

    fn kv_u32(key: &str, val: u32) -> Vec<u8> {
        let mut o = s(key);
        o.extend_from_slice(&4u32.to_le_bytes()); // type=UINT32
        o.extend_from_slice(&val.to_le_bytes());
        o
    }

    fn kv_str(key: &str, val: &str) -> Vec<u8> {
        let mut o = s(key);
        o.extend_from_slice(&8u32.to_le_bytes()); // type=STRING
        o.extend_from_slice(&s(val));
        o
    }

    fn kv_str_array(key: &str, vals: &[&str]) -> Vec<u8> {
        let mut o = s(key);
        o.extend_from_slice(&9u32.to_le_bytes()); // type=ARRAY
        o.extend_from_slice(&8u32.to_le_bytes()); // elem=STRING
        o.extend_from_slice(&(vals.len() as u64).to_le_bytes());
        for v in vals {
            o.extend_from_slice(&s(v));
        }
        o
    }

    fn kv_i32_array(key: &str, vals: &[i32]) -> Vec<u8> {
        let mut o = s(key);
        o.extend_from_slice(&9u32.to_le_bytes()); // type=ARRAY
        o.extend_from_slice(&5u32.to_le_bytes()); // elem=INT32
        o.extend_from_slice(&(vals.len() as u64).to_le_bytes());
        for v in vals {
            o.extend_from_slice(&v.to_le_bytes());
        }
        o
    }

    fn build(kvs: Vec<Vec<u8>>) -> Vec<u8> {
        let mut o = b"GGUF".to_vec();
        o.extend_from_slice(&3u32.to_le_bytes()); // version
        o.extend_from_slice(&0u64.to_le_bytes()); // tensor_count
        o.extend_from_slice(&(kvs.len() as u64).to_le_bytes());
        for kv in kvs {
            o.extend_from_slice(&kv);
        }
        o
    }

    fn parse(bytes: Vec<u8>) -> Result<GgufMeta, String> {
        read_meta_from(&mut BufReader::new(Cursor::new(bytes)))
    }

    /// オーナー機で問題になった Qwen3-14B の実値。
    fn qwen3_14b() -> Vec<Vec<u8>> {
        vec![
            kv_str("general.architecture", "qwen3"),
            kv_str("general.name", "Qwen3-14B-abliterated"),
            kv_u32("qwen3.block_count", 40),
            kv_u32("qwen3.context_length", 40960),
            kv_u32("qwen3.embedding_length", 5120),
            kv_u32("qwen3.attention.head_count", 40),
            kv_u32("qwen3.attention.head_count_kv", 8),
            kv_u32("qwen3.attention.key_length", 128),
            kv_u32("qwen3.attention.value_length", 128),
        ]
    }

    #[test]
    fn reads_real_world_keys() {
        let m = parse(build(qwen3_14b())).expect("読めること");
        assert_eq!(m.arch.as_deref(), Some("qwen3"));
        assert_eq!(m.n_layers, Some(40));
        assert_eq!(m.n_embd, Some(5120));
        assert_eq!(m.n_head, Some(40));
        assert_eq!(m.n_head_kv, Some(8));
        assert_eq!(m.key_length, Some(128));
        assert_eq!(m.value_length, Some(128));
        assert_eq!(m.context_length, Some(40960));

        let g = m.kv_geometry().expect("4値が揃う");
        assert_eq!(g.n_layers, 40);
        assert_eq!(g.n_head_kv, 8);
        assert_eq!(g.key_length, 128);
    }

    #[test]
    fn skips_string_array_with_varying_lengths() {
        // 語彙配列（要素ごとに長さが違う）を挟んでも、その後ろの鍵が読めること。
        let mut kvs = vec![
            kv_str("general.architecture", "qwen3"),
            kv_str_array("tokenizer.ggml.tokens", &["a", "bbbb", "", "ccccccccc", "dd"]),
            kv_i32_array("tokenizer.ggml.token_type", &[1, 1, 3, 1, 1]),
        ];
        kvs.extend(qwen3_14b().into_iter().skip(1));
        let m = parse(build(kvs)).expect("読めること");
        assert_eq!(m.n_layers, Some(40));
        assert_eq!(m.n_head_kv, Some(8));
    }

    #[test]
    fn stops_early_once_everything_is_found() {
        // 全部揃った後ろに壊れた鍵を置く。打ち切っていれば読まないので成功する。
        let mut kvs = qwen3_14b();
        let mut broken = s("tokenizer.ggml.tokens");
        broken.extend_from_slice(&99u32.to_le_bytes()); // 未知の型＝読めば Err
        kvs.push(broken);
        let m = parse(build(kvs)).expect("打ち切っているので壊れた鍵に到達しない");
        assert_eq!(m.n_layers, Some(40));
    }

    #[test]
    fn missing_optional_keys_fall_back_like_llama_cpp() {
        // key_length / value_length / head_count_kv を書かない古めの GGUF。
        let kvs = vec![
            kv_str("general.architecture", "llama"),
            kv_u32("llama.block_count", 32),
            kv_u32("llama.embedding_length", 4096),
            kv_u32("llama.attention.head_count", 32),
        ];
        let m = parse(build(kvs)).expect("読めること");
        assert_eq!(m.n_head_kv, None);
        assert_eq!(m.n_head_kv_eff(), Some(32)); // MHA
        assert_eq!(m.key_length_eff(), Some(128)); // 4096/32
        let g = m.kv_geometry().expect("既定で補える");
        assert_eq!(g.n_head_kv, 32);
        assert_eq!(g.value_length, 128);
    }

    #[test]
    fn rejects_non_gguf() {
        let e = parse(b"NOTAGGUFFILE............".to_vec()).unwrap_err();
        assert!(e.contains("GGUF ではありません"), "{e}");
    }

    #[test]
    fn rejects_unsupported_version() {
        let mut b = b"GGUF".to_vec();
        b.extend_from_slice(&1u32.to_le_bytes());
        b.extend_from_slice(&0u64.to_le_bytes());
        b.extend_from_slice(&0u64.to_le_bytes());
        let e = parse(b).unwrap_err();
        assert!(e.contains("version"), "{e}");
    }

    #[test]
    fn truncated_file_errors_without_panic() {
        let full = build(qwen3_14b());
        for cut in [4, 10, 24, 40, 60] {
            assert!(parse(full[..cut.min(full.len())].to_vec()).is_err());
        }
    }

    #[test]
    fn absurd_lengths_are_rejected_not_allocated() {
        // 長さ欄が壊れた文字列。ここで vec![0; huge] を確保しないこと。
        let mut b = b"GGUF".to_vec();
        b.extend_from_slice(&3u32.to_le_bytes());
        b.extend_from_slice(&0u64.to_le_bytes());
        b.extend_from_slice(&1u64.to_le_bytes());
        b.extend_from_slice(&u64::MAX.to_le_bytes()); // 鍵の長さ
        let e = parse(b).unwrap_err();
        assert!(e.contains("異常"), "{e}");
    }

    #[test]
    fn zero_geometry_is_treated_as_unknown() {
        // 0 を実値として通すと KV が 0 MB になり「載る」と嘘をつく。
        let kvs = vec![
            kv_str("general.architecture", "x"),
            kv_u32("x.block_count", 0),
            kv_u32("x.embedding_length", 4096),
            kv_u32("x.attention.head_count", 32),
            kv_u32("x.attention.head_count_kv", 8),
        ];
        let m = parse(build(kvs)).expect("読めること");
        assert!(m.kv_geometry().is_none());
    }
}
