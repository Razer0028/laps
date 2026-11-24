//! llama-server のログから「**実際にどう載ったか**」を読む。純粋（I/O を一切しない）。
//!
//! ## なぜ要るか（2026-09-02 実測）
//! 実利用者（Windows 機）の記録は、手元のローカル推論で **成功 7／無音 18**、
//! 別の推論サーバーで **成功 6／無音 0**。無音は全部ローカル側だった。
//!
//! GUI は `-ngl` / `-c` を渡さない（llama.rs の設計コメント参照）。auto-fitter は
//! 空き VRAM が足りないと **一部の層だけ GPU・残りを CPU** に置く。9.0GB のモデルで
//! これが起きると最初のトークンまで数百秒かかり、利用者からは「ただ返ってこない」
//! ようにしか見えない。**この分割は今どこにも表示されていない。**
//!
//! 予測（`fit.rs`）は既にある。抜けていたのは **載った後の実測**で、それはログの
//!   `load_tensors: offloaded 22/41 layers to GPU`
//!   `load_tensors:      Vulkan0 model buffer size =  5033.10 MiB`
//!   `load_tensors:   CPU_Mapped model buffer size =  3546.02 MiB`
//! に生で出ている。予測と違って「他アプリが後から VRAM を掴んだ」場合も捕まえられる。
//!
//! ## 書式の出所（推測ではない）
//! 同梱している `resources/llama/*.dll` から printf 書式を直接抽出した:
//!   llama.dll        `%s: offloaded %d/%d layers to GPU`
//!                    `%s: %12s model buffer size = %8.2f MiB`
//!                    `%s: %10s KV buffer size = %8.2f MiB`
//!                    `%s: %10s compute buffer size = %8.2f MiB`
//!   ggml-vulkan.dll  `ggml_vulkan: Found %zu Vulkan devices:`
//!                    `ggml_vulkan: %zu = %s (%s) | uma: %d | fp16: %s | ...`
//! ★同梱の GPU バックエンドは **Vulkan だけ**（`ggml-cuda.dll` は入っていない）。
//!   ただし解析は名前で判定するので CUDA/ROCm/Metal でもそのまま動く。
//!
//! ## 方針
//! - 出すのは**数字だけ**。助言は書かない（間違った案内文は無いより悪い）。
//! - 読めない・書式が変わった → **`None`**。推測で埋めない。
//! - 「取れない」と「0」を区別する（`Option<f64>` と `0.0` は別物）。
//! - **最後の**読み込みブロックを採る。router は 600 秒アイドルでモデルを降ろし、
//!   次の依頼で載せ直す＝ログには読み込みブロックが**何度も追記される**。
//!   最初の一致を採ると、いま使っているのと違うモデルの分割を表示してしまう。
#![allow(dead_code)] // 本体アプリ側は Scanner だけを使う（parse_log は試験と一発解析用）。

use serde::Serialize;

/// 1回の読み込みで観測された実測値。**すべて計測値で、助言は含まない。**
///
/// JSON の欄名（画面側が読む契約）:
/// `gpu_device` / `offloaded_layers` / `total_layers` /
/// `gpu_weight_mib` / `cpu_weight_mib` / `gpu_kv_mib` / `cpu_kv_mib` /
/// `gpu_compute_mib` / `cpu_compute_mib` / `n_ctx`
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct LoadFacts {
    /// Vulkan が列挙した先頭デバイスの名前（例 `NVIDIA GeForce RTX 4070 (NVIDIA)`）。
    /// llama.cpp の既定 `main_gpu` は 0 なので、先頭＝実際に使うデバイス。
    pub gpu_device: Option<String>,
    /// GPU に載った層の数。**0 は「全部 CPU」という測定結果**で、欠測ではない。
    pub offloaded_layers: u32,
    /// モデルの全層数（+1 は出力層）。
    pub total_layers: u32,
    /// 重みのうち GPU 常駐分（MiB）。
    pub gpu_weight_mib: Option<f64>,
    /// 重みのうち CPU 常駐分（MiB）。**ここに数字が出ている＝黙って CPU に落ちた証拠。**
    pub cpu_weight_mib: Option<f64>,
    pub gpu_kv_mib: Option<f64>,
    pub cpu_kv_mib: Option<f64>,
    pub gpu_compute_mib: Option<f64>,
    pub cpu_compute_mib: Option<f64>,
    /// auto-fitter が実際に選んだ文脈長。
    pub n_ctx: Option<u32>,
}

// ---------------------------------------------------------------------------
// 1行ずつの読み取り（どれも純粋・失敗は None）
// ---------------------------------------------------------------------------

/// `... offloaded 22/41 layers to GPU` → `(22, 41)`。
///
/// ★同じ行の近くに `offloading %d repeating layers to GPU` があり、これは
/// **層の総数ではない**（繰り返し層だけの数）。取り違えないよう
/// ` offloaded ` を必須にしている。
pub fn offload_counts(line: &str) -> Option<(u32, u32)> {
    let i = line.find(" offloaded ")?;
    let rest = &line[i + " offloaded ".len()..];
    let j = rest.find(" layers to GPU")?;
    let (n, m) = rest[..j].split_once('/')?;
    Some((n.trim().parse().ok()?, m.trim().parse().ok()?))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Weight,
    Kv,
    Compute,
}

impl Kind {
    fn idx(self) -> usize {
        match self {
            Kind::Weight => 0,
            Kind::Kv => 1,
            Kind::Compute => 2,
        }
    }
}

/// `load_tensors:      Vulkan0 model buffer size =  5033.10 MiB`
/// → `("Vulkan0", Kind::Weight, 5033.10)`
///
/// 接頭辞（`load_tensors` / `llama_kv_cache` / `llama_context`）には依存しない。
/// llama.cpp は `__func__` を出しているだけで、版によって変わりうるため。
fn buffer_line(line: &str) -> Option<(&str, Kind, f64)> {
    let (head, val) = line.split_once(" buffer size = ")?;
    let mib: f64 = val.trim().strip_suffix(" MiB")?.trim().parse().ok()?;
    let mut it = head.split_whitespace().rev();
    let kind = match it.next()? {
        "model" => Kind::Weight,
        "KV" => Kind::Kv,
        "compute" => Kind::Compute,
        // output / LoRA / RS / state などは意図的に無視する（分割の判断に効かない）。
        _ => return None,
    };
    let name = it.next()?;
    // 名前の欄が無い行（`foo: model buffer size = ...`）を接頭辞ごと拾わない。
    if name.ends_with(':') {
        return None;
    }
    Some((name, kind, mib))
}

/// バッファ名から「GPU に常駐しているか」を判定する。
///
/// `Vulkan0` / `CUDA0` / `ROCm0` … は GPU。
/// `CPU` / `CPU_Mapped` / `CPU_Repack` は CPU。
/// ★`Vulkan_Host` / `CUDA_Host` は**名前に GPU が入っているがホスト側のピン留めメモリ**。
///   ここを取り違えると「GPU に載っている」と嘘をつくので、`_Host` を明示的に外す。
fn is_gpu_buffer(name: &str) -> bool {
    !(name.starts_with("CPU") || name.contains("_Host"))
}

/// `llama_context: n_ctx                 = 4096` → `4096`。
///
/// `n_ctx_train` / `n_ctx_seq` / `n_ctx_orig_yarn` とは**完全一致で**区別する。
fn n_ctx_value(line: &str) -> Option<u32> {
    let (head, val) = line.split_once('=')?;
    let key = head.split(':').nth(1)?.trim();
    if key != "n_ctx" {
        return None;
    }
    val.trim().parse().ok()
}

/// `ggml_vulkan: 0 = NVIDIA GeForce RTX 4070 (NVIDIA) | uma: 0 | ...`
/// → `(0, "NVIDIA GeForce RTX 4070 (NVIDIA)")`
fn vulkan_device(line: &str) -> Option<(usize, String)> {
    let i = line.find("ggml_vulkan: ")?;
    let rest = &line[i + "ggml_vulkan: ".len()..];
    let (idx, rest) = rest.split_once(" = ")?;
    let idx: usize = idx.trim().parse().ok()?;
    let name = rest.split(" | ").next()?.trim();
    if name.is_empty() {
        return None;
    }
    Some((idx, name.to_string()))
}

// ---------------------------------------------------------------------------
// ブロック（= 最後の `offloaded` 行から始まる範囲）の解析
// ---------------------------------------------------------------------------

/// 同じ種別の行が**離れて2度目**に出てきたら、それは別の文脈生成の分。
/// 足すと二重計上になるので、最初のひとかたまりだけを採る。
/// 間に警告行が1〜2行挟まる程度は同じかたまりとみなす（実機のログは
/// stdout と stderr が同じファイルに落ちるので、行が挟まりうる）。
const RUN_GAP_TOLERANCE: usize = 3;

/// ブロック本文（**1行目が `offloaded` 行**）から実測値を組み立てる。
/// 1行目がそれでなければ `None`（＝嘘を出さない）。
pub fn parse_block(block: &str) -> Option<LoadFacts> {
    let mut facts: Option<LoadFacts> = None;
    let mut gpu: [Option<f64>; 3] = [None; 3];
    let mut cpu: [Option<f64>; 3] = [None; 3];
    let mut last_idx: [Option<usize>; 3] = [None; 3];
    let mut closed = [false; 3];
    let mut n_ctx: Option<u32> = None;

    for (i, line) in block.lines().enumerate() {
        if i == 0 {
            let (n, m) = offload_counts(line)?;
            facts = Some(LoadFacts {
                offloaded_layers: n,
                total_layers: m,
                ..Default::default()
            });
            continue;
        }
        if let Some((name, kind, mib)) = buffer_line(line) {
            let k = kind.idx();
            if closed[k] {
                continue;
            }
            if let Some(prev) = last_idx[k] {
                if i > prev + RUN_GAP_TOLERANCE {
                    closed[k] = true;
                    continue;
                }
            }
            last_idx[k] = Some(i);
            let slot = if is_gpu_buffer(name) {
                &mut gpu[k]
            } else {
                &mut cpu[k]
            };
            *slot = Some(slot.unwrap_or(0.0) + mib);
            continue;
        }
        if n_ctx.is_none() {
            n_ctx = n_ctx_value(line);
        }
    }

    let mut f = facts?;
    f.gpu_weight_mib = gpu[Kind::Weight.idx()];
    f.cpu_weight_mib = cpu[Kind::Weight.idx()];
    f.gpu_kv_mib = gpu[Kind::Kv.idx()];
    f.cpu_kv_mib = cpu[Kind::Kv.idx()];
    f.gpu_compute_mib = gpu[Kind::Compute.idx()];
    f.cpu_compute_mib = cpu[Kind::Compute.idx()];
    f.n_ctx = n_ctx;
    Some(f)
}

// ---------------------------------------------------------------------------
// 追記を読み進める走査器
// ---------------------------------------------------------------------------

/// 読み込みブロックとして溜める上限（行）。llama.cpp の1回分は 200 行に満たない。
/// ★上限に達したら**先頭を捨てずに追記を止める**。先頭を捨てると `offloaded` 行が
///   落ちてブロックが無効になる（依頼ログが何千行も後続するので必ず起きる）。
const MAX_BLOCK_LINES: usize = 400;
/// 改行の来ない中途半端な行を溜める上限。これを超えたら捨てる（我々の読む行ではない）。
const MAX_CARRY: usize = 64 * 1024;

/// ログの**追記分だけ**を食べて、最後の読み込みブロックの実測値を保つ。
///
/// なぜ全文を読み直さないか: `llama_status()` は画面のポーリングだけでなく
/// `agent.rs::base_url()` から**依頼のたび**に呼ばれる。全文走査を入れると
/// 会話1往復ごとに数 MB を読むことになる。追記分だけなら数百バイトで済む。
///
/// なぜ末尾 N バイトだけを見ないか: 読み込みのあとに依頼ログが積もると
/// `offloaded` 行が窓の外へ出る。「窓から出たら消える」表示は、
/// 一番知りたい長時間走行のときに限って何も出ない。
pub struct LoadLogScanner {
    carry: Vec<u8>,
    block: String,
    block_lines: usize,
    devices: Vec<(usize, String)>,
    facts: Option<LoadFacts>,
}

impl Default for LoadLogScanner {
    fn default() -> Self {
        Self::new()
    }
}

impl LoadLogScanner {
    pub fn new() -> Self {
        Self {
            carry: Vec::new(),
            block: String::new(),
            block_lines: 0,
            devices: Vec::new(),
            facts: None,
        }
    }

    /// ログの続きを与える。**境界はどこで切れていてもよい**（行の途中でも可）。
    pub fn feed(&mut self, chunk: &[u8]) {
        self.carry.extend_from_slice(chunk);
        let mut start = 0usize;
        while let Some(nl) = self.carry[start..].iter().position(|b| *b == b'\n') {
            let end = start + nl;
            let mut line = &self.carry[start..end];
            if line.last() == Some(&b'\r') {
                line = &line[..line.len() - 1];
            }
            let line = String::from_utf8_lossy(line).into_owned();
            self.push_line(&line);
            start = end + 1;
        }
        self.carry.drain(..start);
        if self.carry.len() > MAX_CARRY {
            self.carry.clear();
        }
    }

    fn push_line(&mut self, line: &str) {
        // デバイス一覧はブロックの外（バックエンド初期化時）に出るので独立に持つ。
        if line.contains("Vulkan devices:") {
            self.devices.clear();
        } else if let Some((idx, name)) = vulkan_device(line) {
            self.devices.retain(|(i, _)| *i != idx);
            self.devices.push((idx, name));
            self.devices.sort_by_key(|(i, _)| *i);
        }

        if offload_counts(line).is_some() {
            // 新しい読み込みが始まった。前のブロックは捨てる（最後の一致を採る）。
            self.block.clear();
            self.block.push_str(line);
            self.block.push('\n');
            self.block_lines = 1;
            self.facts = parse_block(&self.block);
            return;
        }
        if self.block_lines == 0 || self.block_lines >= MAX_BLOCK_LINES {
            return;
        }
        self.block.push_str(line);
        self.block.push('\n');
        self.block_lines += 1;
        // 値が増えうる行のときだけ組み直す（依頼ログ1行ごとに解析し直さない）。
        if line.contains(" buffer size = ") || line.contains("n_ctx") {
            self.facts = parse_block(&self.block);
        }
    }

    /// いまの実測値。読み込みをまだ観測していなければ `None`。
    pub fn facts(&self) -> Option<LoadFacts> {
        let mut f = self.facts.clone()?;
        f.gpu_device = self.devices.first().map(|(_, n)| n.clone());
        Some(f)
    }

    /// ログが差し替わった（切り詰め・別ファイル）ときに最初から読み直す。
    pub fn reset(&mut self) {
        *self = Self::new();
    }
}

/// 一発解析。全文を走査器へ流すだけ＝**本番と同じ1本の経路**を通る。
pub fn parse_log(text: &str) -> Option<LoadFacts> {
    let mut s = LoadLogScanner::new();
    s.feed(text.as_bytes());
    // 末尾に改行が無い場合の最後の1行も読ませる。
    s.feed(b"\n");
    s.facts()
}

// ---------------------------------------------------------------------------
// 試験
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// 実機が印字する形そのもの（書式は同梱 DLL の printf 文字列から起こした）。
    /// 12GB 級の GPU に 9.0GB のモデルを載せて**溢れた**ときの姿。
    const PARTIAL: &str = "\
ggml_vulkan: Found 1 Vulkan devices:
ggml_vulkan: 0 = NVIDIA GeForce RTX 4070 Laptop GPU (NVIDIA) | uma: 0 | fp16: 1 | bf16: 0 | fp4: 0 | warp size: 32 | shared memory: 49152 | int dot: 1 | matrix cores: KHR_coopmat
load_tensors: offloading 22 repeating layers to GPU
load_tensors: offloaded 22/41 layers to GPU
load_tensors:      Vulkan0 model buffer size =  5033.10 MiB
load_tensors:   CPU_Mapped model buffer size =  3546.02 MiB
llama_context: n_ctx                 = 4096
llama_context: n_ctx_train           = 40960
llama_kv_cache:    Vulkan0 KV buffer size =   440.00 MiB
llama_kv_cache:        CPU KV buffer size =   360.00 MiB
llama_context:    Vulkan0 compute buffer size =   304.00 MiB
llama_context:  Vulkan_Host compute buffer size =    16.01 MiB
srv  log_server_r: request: GET /health 127.0.0.1 200
";

    #[test]
    fn reads_a_partial_offload_the_way_the_user_actually_sees_it() {
        // なぜ: 実利用者の無音 18 件はこの形（層が半分だけ GPU）だと見ている。
        // 重みが CPU 側に 3.5GB 残っていることが、いま画面のどこにも出ていない。
        let f = parse_log(PARTIAL).expect("読めるはず");
        assert_eq!((f.offloaded_layers, f.total_layers), (22, 41));
        assert_eq!(f.gpu_weight_mib, Some(5033.10));
        assert_eq!(f.cpu_weight_mib, Some(3546.02));
        assert_eq!(f.gpu_kv_mib, Some(440.00));
        assert_eq!(f.cpu_kv_mib, Some(360.00));
        assert_eq!(f.gpu_compute_mib, Some(304.00));
        // Vulkan_Host は名前に Vulkan が入っているが**ホスト側**。GPU に数えない。
        assert_eq!(f.cpu_compute_mib, Some(16.01));
        assert_eq!(f.n_ctx, Some(4096)); // n_ctx_train(40960) と取り違えない
        assert_eq!(
            f.gpu_device.as_deref(),
            Some("NVIDIA GeForce RTX 4070 Laptop GPU (NVIDIA)")
        );
    }

    #[test]
    fn a_full_offload_has_no_cpu_weight_at_all() {
        // なぜ: 「取れない(None)」と「0」を混ぜない。CPU 側の行が無いのが正常な姿。
        let log = "\
load_tensors: offloaded 41/41 layers to GPU
load_tensors:      Vulkan0 model buffer size =  8579.12 MiB
";
        let f = parse_log(log).unwrap();
        assert_eq!((f.offloaded_layers, f.total_layers), (41, 41));
        assert_eq!(f.gpu_weight_mib, Some(8579.12));
        assert_eq!(f.cpu_weight_mib, None);
    }

    #[test]
    fn zero_layers_is_a_measurement_not_a_blank() {
        // なぜ: 全部 CPU に落ちたときこそ表示したい。0 を欠測として消さない。
        let log = "\
load_tensors: offloaded 0/41 layers to GPU
load_tensors:   CPU_Mapped model buffer size =  8579.12 MiB
";
        let f = parse_log(log).unwrap();
        assert_eq!(f.offloaded_layers, 0);
        assert_eq!(f.gpu_weight_mib, None);
        assert_eq!(f.cpu_weight_mib, Some(8579.12));
    }

    #[test]
    fn the_last_load_wins_not_the_first() {
        // なぜ: router は 600 秒アイドルでモデルを降ろし、次の依頼で載せ直す。
        // ログには読み込みブロックが積み重なる。最初の一致を採ると
        // **いま使っていないモデルの分割**を表示してしまう。
        let log = "\
load_tensors: offloaded 41/41 layers to GPU
load_tensors:      Vulkan0 model buffer size =  1000.00 MiB
srv  log_server_r: request: POST /v1/chat/completions 127.0.0.1 200
load_tensors: offloaded 10/41 layers to GPU
load_tensors:      Vulkan0 model buffer size =  2000.00 MiB
load_tensors:   CPU_Mapped model buffer size =  6000.00 MiB
";
        let f = parse_log(log).unwrap();
        assert_eq!(f.offloaded_layers, 10);
        assert_eq!(f.gpu_weight_mib, Some(2000.00));
        assert_eq!(f.cpu_weight_mib, Some(6000.00));
    }

    #[test]
    fn a_second_context_does_not_double_count_compute() {
        // なぜ: 同じ読み込みの中で文脈が2度作られると compute 行がもう一度出る。
        // 足すと実際の倍を表示する（＝嘘）。離れた2度目のかたまりは採らない。
        let log = "\
load_tensors: offloaded 41/41 layers to GPU
load_tensors:      Vulkan0 model buffer size =  1000.00 MiB
llama_context:    Vulkan0 compute buffer size =   300.00 MiB
srv    load_model: loaded
srv  log_server_r: request: GET /props 127.0.0.1 200
srv  log_server_r: request: GET /health 127.0.0.1 200
llama_context:    Vulkan0 compute buffer size =   300.00 MiB
";
        let f = parse_log(log).unwrap();
        assert_eq!(f.gpu_compute_mib, Some(300.00));
    }

    #[test]
    fn an_interleaved_line_does_not_split_one_run() {
        // なぜ: stdout と stderr が同じファイルに落ちるので、かたまりの中に
        // 別の行が1行挟まりうる。挟まっただけで CPU 側を取りこぼすと、
        // 「GPU にしか無い」という**逆の嘘**になる。
        let log = "\
load_tensors: offloaded 22/41 layers to GPU
load_tensors:      Vulkan0 model buffer size =  5000.00 MiB
srv    load_model: warming up
load_tensors:   CPU_Mapped model buffer size =  3500.00 MiB
";
        let f = parse_log(log).unwrap();
        assert_eq!(f.gpu_weight_mib, Some(5000.00));
        assert_eq!(f.cpu_weight_mib, Some(3500.00));
    }

    #[test]
    fn offloading_repeating_layers_is_not_the_total() {
        // なぜ: `offloading 22 repeating layers to GPU` は**総数ではない**。
        // これを拾うと層数を取り違える。
        assert_eq!(
            offload_counts("load_tensors: offloading 22 repeating layers to GPU"),
            None
        );
        // 古い版（llama.cpp 公式ドキュメントに残っている形）も総数を持たないので拾わない。
        assert_eq!(
            offload_counts("llama_model_load_internal: [cublas] offloading 60 layers to GPU"),
            None
        );
        assert_eq!(
            offload_counts("load_tensors: offloaded 22/41 layers to GPU"),
            Some((22, 41))
        );
    }

    #[test]
    fn nothing_is_reported_when_the_format_changes() {
        // なぜ: 版が上がって書式が変わったら、**何も出さない**のが正しい。
        // それらしい数字を出すほうが害が大きい。
        assert_eq!(parse_log("load_tensors: put 22 of 41 layers on the GPU\n"), None);
        assert_eq!(parse_log(""), None);
        assert_eq!(parse_log("srv  log_server_r: request: GET /health 200\n"), None);
    }

    #[test]
    fn buffer_lines_that_are_not_about_placement_are_ignored() {
        // output / LoRA / state は分割の判断に効かないので拾わない。
        // 「= の無い」比較メッセージも拾わない。
        assert!(buffer_line("llama_context:   Vulkan0  output buffer size =     0.58 MiB").is_none());
        assert!(buffer_line("llama_context:   Vulkan0 LoRA buffer size =     1.00 MiB").is_none());
        assert!(
            buffer_line("llama_context: Vulkan0 compute buffer size is 304.0000 MiB, matches expectation of 304.0000 MiB").is_none()
        );
        assert_eq!(
            buffer_line("load_tensors:      Vulkan0 model buffer size =  5033.10 MiB").map(|(n, _, v)| (n, v)),
            Some(("Vulkan0", 5033.10))
        );
    }

    #[test]
    fn host_pinned_memory_is_counted_as_cpu() {
        // なぜ: `Vulkan_Host` / `CUDA_Host` は名前に GPU が入っているが実体はホスト。
        // ここを間違えると「GPU に載っている」と嘘をつく。
        assert!(is_gpu_buffer("Vulkan0"));
        assert!(is_gpu_buffer("CUDA0"));
        assert!(is_gpu_buffer("ROCm0"));
        assert!(!is_gpu_buffer("Vulkan_Host"));
        assert!(!is_gpu_buffer("CUDA_Host"));
        assert!(!is_gpu_buffer("CPU"));
        assert!(!is_gpu_buffer("CPU_Mapped"));
        assert!(!is_gpu_buffer("CPU_Repack"));
    }

    #[test]
    fn the_first_vulkan_device_is_the_one_llama_uses() {
        // なぜ: 既定の main_gpu は 0。最後に列挙された行を採ると、2枚挿しの機械で
        // 使っていない GPU の名前を出す。
        let log = "\
ggml_vulkan: Found 2 Vulkan devices:
ggml_vulkan: 0 = NVIDIA GeForce RTX 4070 (NVIDIA) | uma: 0 | fp16: 1 | matrix cores: KHR_coopmat
ggml_vulkan: 1 = Intel(R) UHD Graphics (Intel) | uma: 1 | fp16: 1 | matrix cores: none
load_tensors: offloaded 41/41 layers to GPU
";
        let f = parse_log(log).unwrap();
        assert_eq!(f.gpu_device.as_deref(), Some("NVIDIA GeForce RTX 4070 (NVIDIA)"));
    }

    #[test]
    fn feeding_one_byte_at_a_time_gives_the_same_answer() {
        // なぜ: 本番は**ログの追記分**を切れ目の分からない塊で読む。
        // 行の途中で切れても結果が変わってはいけない（ここが壊れると
        // 「たまにしか出ない表示」になり、原因を追えなくなる）。
        let want = parse_log(PARTIAL).unwrap();
        let mut s = LoadLogScanner::new();
        for b in PARTIAL.as_bytes() {
            s.feed(&[*b]);
        }
        assert_eq!(s.facts().unwrap(), want);

        // 3 バイトずつでも同じ。
        let mut s3 = LoadLogScanner::new();
        for c in PARTIAL.as_bytes().chunks(3) {
            s3.feed(c);
        }
        assert_eq!(s3.facts().unwrap(), want);
    }

    #[test]
    fn every_possible_split_point_gives_the_same_answer() {
        // なぜ: 1バイトずつと3バイトずつが通っても、「たまたまその刻み方だけ通る」
        // 可能性が残る。実際の読みは OS が返した長さで切れるので、**あらゆる**
        // 切れ目で同じ答えになることを総当たりで固定する（672 通り）。
        let want = parse_log(PARTIAL).unwrap();
        let bytes = PARTIAL.as_bytes();
        for cut in 0..bytes.len() {
            let mut s = LoadLogScanner::new();
            s.feed(&bytes[..cut]);
            s.feed(&bytes[cut..]);
            assert_eq!(s.facts().unwrap(), want, "切れ目 {cut} で答えが変わった");
        }
    }

    #[test]
    fn crlf_is_handled() {
        // Windows のログ。行末の \r を名前や数値に混ぜない。
        let log = "load_tensors: offloaded 22/41 layers to GPU\r\nload_tensors:      Vulkan0 model buffer size =  5033.10 MiB\r\n";
        let f = parse_log(log).unwrap();
        assert_eq!(f.offloaded_layers, 22);
        assert_eq!(f.gpu_weight_mib, Some(5033.10));
    }

    #[test]
    fn a_long_tail_of_request_logs_does_not_lose_the_block() {
        // なぜ: 読み込みのあとに依頼ログが何千行も積もる。上限で**先頭を捨てる**
        // 実装にすると `offloaded` 行が落ちて、長く使うほど表示が消える。
        let mut log = String::from(PARTIAL);
        for i in 0..5000 {
            log.push_str(&format!(
                "srv  log_server_r: request: POST /v1/chat/completions 127.0.0.1 200 #{i}\n"
            ));
        }
        let f = parse_log(&log).unwrap();
        assert_eq!((f.offloaded_layers, f.total_layers), (22, 41));
        assert_eq!(f.cpu_weight_mib, Some(3546.02));
    }

    #[test]
    fn the_json_field_names_are_the_contract_with_the_screen() {
        // なぜ: 画面側（app.js / sidecar）はこの欄名で読む。名前を変えると
        // 表示だけが静かに消える（＝一番気づきにくい壊れ方）。
        let f = parse_log(PARTIAL).unwrap();
        let v: serde_json::Value = serde_json::to_value(&f).unwrap();
        for key in [
            "gpu_device",
            "offloaded_layers",
            "total_layers",
            "gpu_weight_mib",
            "cpu_weight_mib",
            "gpu_kv_mib",
            "cpu_kv_mib",
            "gpu_compute_mib",
            "cpu_compute_mib",
            "n_ctx",
        ] {
            assert!(v.get(key).is_some(), "欄 {key} が消えている");
        }
        assert_eq!(v["offloaded_layers"], 22);
        assert_eq!(v["total_layers"], 41);
    }

    #[test]
    fn a_reset_forgets_everything() {
        // ログが切り詰め・差し替えされたとき、古い読み込みを残さない。
        let mut s = LoadLogScanner::new();
        s.feed(PARTIAL.as_bytes());
        assert!(s.facts().is_some());
        s.reset();
        assert!(s.facts().is_none());
    }
}
