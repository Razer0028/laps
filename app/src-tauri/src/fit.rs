//! 「そのモデルは載るか」の**ダウンロード前**見積もり。
//!
//! ★これは見積もりであって判定ではない。
//! 権威ある判定は llama.cpp 自身の fitter（`-fit`・既定 ON、`no_alloc=true` の dry-run load）で、
//! それは**ファイルが手元にある**ことが前提になる。つまり 18.6GB を落とし終えるまで動かない。
//! 本モジュールはその空白——「落とす前に載らないと分かりたい」——だけを埋める。
//! ファイルが揃ったら、必ず llama.cpp 側の値で上書きすること。
//!
//! 入力は HF の API からほぼ無料で得られる:
//!   `GET /api/models/{repo}/tree/{rev}?recursive=true` → size
//!   `GET /api/models/{repo}`                           → gguf.total(params) / architecture / context_length
//! 層数は HF が返さないので、分かれば渡す・分からなければパラメータ数から粗く推定する。
//!
//! 見積もりは**必ず過大側に倒す**。「載ると言って載らない」は 18GB のダウンロードを無駄にするが、
//! 「載らないと言って実は載る」は llama.cpp の fitter が後で訂正できる。損失が対称ではない。

use serde::{Deserialize, Serialize};

// main.rs に mod gguf; を登録済み。クレート直下のものを使う（二重取り込みを避ける）。
use crate::gguf::{GgufMeta, KvGeometry};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FitVerdict {
    /// "fits" | "no_fit" | "fits_with_cpu_offload"
    pub verdict: String,
    pub needed_mb: u64,
    pub weights_mb: u64,
    pub kv_mb: u64,
    /// 空き - 必要。不足しているときは 0（不足量は note に書く）。
    pub headroom_mb: u64,
    /// UI がそのまま表示できる日本語1文。
    pub note: String,
    /// CPU 分担時の推定生成速度（下限側）。**仮定に基づく概算で実測ではない。**
    pub est_tok_s: Option<f32>,

    // -- 以下は追加分。既存フィールドは画面が使っているので消さない ------------
    /// この空きに収まる最大の文脈長。**「載りません」で行き止まりにしないための値。**
    /// 要求 ctx より小さければ「ここまで下げれば載る」、大きければ「まだ伸ばせる」。
    /// どうやっても入らない（＝重みだけで空きを超える）ときだけ None。
    pub max_ctx: Option<u32>,
    /// KV の内訳と、q8_0 にした場合の伸びしろ。UI が2行目として出せる粒度。
    pub kv_note: Option<String>,
}

const MIB: u64 = 1024 * 1024;

/// 計算バッファ（logits・KV 以外の一時領域）の固定見積もり。
/// モデルと文脈で変わるが、数百 MiB という桁は変わらないので固定値で押さえる。
const COMPUTE_BUFFER_MB: u64 = 384;

/// llama.cpp の fitter が既定で空けておくデバイスあたりの余白（`-fitt`、既定 1024 MiB）。
/// fitter が最終判定者である以上、こちらの見積もりも同じ余白を積んでおかないと
/// 「アプリは載ると言ったのに llama.cpp が層を CPU に落とした」というズレが出る。
const FITTER_MARGIN_MB: u64 = 1024;

// -- CPU 分担時の速度見積もりに使う仮定（★実測ではない） ---------------------
// 端点はどちらも「桁を外さない」ことだけを狙った仮定値。
// 手元での実測（30B-A3B が full-offload で約 60 tok/s）は GPU 側の上限であって
// この式の端点そのものではない。数字を実測として引用しないこと。
const ASSUMED_GPU_TOK_S: f32 = 30.0;
const ASSUMED_CPU_TOK_S: f32 = 4.0;

/// ダウンロード前の可否見積もり。
///
/// 引数は JS からは camelCase（`sizeBytes`, `nParams`, `nLayers`, `vramFreeMb`, `isMoe`）で渡る。
///
/// - `size_bytes`  : GGUF ファイルのサイズ。**これがほぼそのまま重みの VRAM 消費**になる。
/// - `n_params`    : 総パラメータ数（HF の `gguf.total`）。層数・次元の推定に使う。
/// - `n_layers`    : 分かっていれば渡す。KV 見積もりの精度がここで決まる。
/// - `ctx`         : 使いたい文脈長。
/// - `vram_free_mb`: **空き**（総量ではない）。gpu.rs の `vram_free_mb` を渡すこと。
/// - `is_moe`      : MoE なら experts を CPU に降ろす逃げ道がある（`-ot` / `--n-cpu-moe`）。
#[cfg_attr(not(feature = "no_tauri"), tauri::command)]
pub fn estimate_fit(
    size_bytes: u64,
    n_params: Option<u64>,
    n_layers: Option<u32>,
    ctx: u32,
    vram_free_mb: u64,
    is_moe: bool,
) -> FitVerdict {
    // GGUF は量子化済みの重みそのもの。ヘッダとメタデータの分は誤差なので切り分けない。
    // 端数は切り上げる（過大側に倒す方針）。
    let weights_mb = size_bytes.div_ceil(MIB);

    let params = n_params.unwrap_or_else(|| infer_params_from_size(size_bytes));
    let layers = n_layers.map(u64::from).unwrap_or_else(|| infer_layers(params));
    let (kv_mb, kv_is_rough) = estimate_kv_mb(layers, ctx, params, n_layers.is_some());

    let overhead_mb = COMPUTE_BUFFER_MB + FITTER_MARGIN_MB;
    let needed_mb = weights_mb + kv_mb + overhead_mb;

    let fits = needed_mb <= vram_free_mb;
    let headroom_mb = vram_free_mb.saturating_sub(needed_mb);
    let shortfall_mb = needed_mb.saturating_sub(vram_free_mb);

    let rough_suffix = if kv_is_rough {
        "（層数が不明なため KV は粗い上限）"
    } else {
        ""
    };

    if fits {
        return FitVerdict {
            verdict: "fits".to_string(),
            needed_mb,
            weights_mb,
            kv_mb,
            headroom_mb,
            note: format!(
                "載ります。必要 {} MB（重み {} MB＋KV {} MB＋余白 {} MB）に対し空き {} MB、余裕 {} MB。{}最終判定はファイル取得後の llama.cpp の --fit です。",
                fmt(needed_mb),
                fmt(weights_mb),
                fmt(kv_mb),
                fmt(overhead_mb),
                fmt(vram_free_mb),
                fmt(headroom_mb),
                rough_suffix
            ),
            // 生成速度はメモリ帯域に支配され、ここでは帯域を知らない。
            // 全 GPU 常駐なら「速い」以上のことを数字で言う根拠が無いので None を返す。
            est_tok_s: None,
            // ここは落とす前の推測なので、伸ばせる ctx の上限まで言い切らない。
            // 具体値はファイルが手元に来てから fit_local が出す。
            max_ctx: None,
            kv_note: None,
        };
    }

    if is_moe {
        // MoE は expert の重みが層ごとに巨大で、しかも1トークンあたり一部しか使われない。
        // よって experts だけを CPU に置く（-ot '.ffn_.*_exps=CPU' / --n-cpu-moe）と、
        // 減る VRAM の割に速度低下が小さい。dense モデルではこの逃げ道は成立しない。
        let est = estimate_offload_tok_s(shortfall_mb, weights_mb);
        let est_high = est * 2.0;
        return FitVerdict {
            verdict: "fits_with_cpu_offload".to_string(),
            needed_mb,
            weights_mb,
            kv_mb,
            headroom_mb: 0,
            note: format!(
                "そのままでは {} MB 足りません（必要 {} MB／空き {} MB）が、MoE なので experts を CPU に降ろせば動きます（-ot '.ffn_.*_exps=CPU'）。推定 {:.0}〜{:.0} tok/s ですが、これは仮定に基づく概算で実測ではありません。{}",
                fmt(shortfall_mb),
                fmt(needed_mb),
                fmt(vram_free_mb),
                est,
                est_high,
                rough_suffix
            ),
            est_tok_s: Some(est),
            max_ctx: None,
            kv_note: None,
        };
    }

    // 逃げ道を必ず添える。「載りません」で終える画面は作らない（DESIGN.md §7）。
    let smaller_ctx = suggest_smaller_ctx(ctx, shortfall_mb, kv_mb);
    let ctx_hint = match smaller_ctx {
        Some(c) => format!("ctx を {} まで下げるか、", c),
        None => String::new(),
    };
    FitVerdict {
        verdict: "no_fit".to_string(),
        needed_mb,
        weights_mb,
        kv_mb,
        headroom_mb: 0,
        note: format!(
            "載りません。必要 {} MB（重み {} MB＋KV {} MB＋余白 {} MB）に対し空き {} MB、{} MB 不足です。{}より小さい量子化を選ぶか、LAN の推論機を使ってください。{}",
            fmt(needed_mb),
            fmt(weights_mb),
            fmt(kv_mb),
            fmt(overhead_mb),
            fmt(vram_free_mb),
            fmt(shortfall_mb),
            ctx_hint,
            rough_suffix
        ),
        est_tok_s: None,
        max_ctx: smaller_ctx,
        kv_note: None,
    }
}

// ---------------------------------------------------------------------------
// ここから下は**手元にファイルがある**場合。推測せず GGUF の実値で計算する。
// ---------------------------------------------------------------------------

/// ctx を刻む単位。512 の倍数で決定論的に探すので、同じ入力なら常に同じ答えになる。
const CTX_STEP: u32 = 512;
/// 探索の上限。これ以上の文脈長を勧めても実用にならないし、ループを有界にしておきたい。
const MAX_PROBE_CTX: u32 = 1 << 20;

/// KV キャッシュの**実寸**（MiB）。
///
///   bytes = n_layers × ctx × n_head_kv × (key_length + value_length) × (kv_bits / 8)
///
/// K と V を別々に足しているのは、両者で head_dim が違う型（MLA など）があるため。
/// 通常は key_length == value_length なので「× 2」と同じ値になる。
///
/// `kv_bits`: 16 = f16（既定）／8 = q8_0（`-ctk q8_0 -ctv q8_0`）。
/// ★q8_0 の実体は 32 要素ブロックに f16 のスケールが付く（34/32 バイト＝約 6% 増）が、
///   ここでは 1 バイト/要素で計算している。その差は余白 1,024 MiB に十分埋もれる。
pub fn kv_mb_exact(
    n_layers: u32,
    n_head_kv: u32,
    key_len: u32,
    val_len: u32,
    ctx: u32,
    kv_bits: u8,
) -> u64 {
    let elems = u64::from(n_layers)
        .saturating_mul(u64::from(ctx))
        .saturating_mul(u64::from(n_head_kv))
        .saturating_mul(u64::from(key_len) + u64::from(val_len));
    let bits = elems.saturating_mul(u64::from(kv_bits));
    bits.div_ceil(8).div_ceil(MIB)
}

/// この空き VRAM に収まる**最大の文脈長**（512 の倍数）。入らないなら 0。
///
/// ★余白の扱い: ここでは compute バッファ（COMPUTE_BUFFER_MB = 384 MiB）だけを引き、
/// llama.cpp の fitter 既定余白（1,024 MiB）は引かない。あれは「空けておく」推奨であって
/// 絶対要件ではなく、この関数の役目は「どこまで押せるか」を答えることだから。
/// 推奨余白込みの値が要るときは呼び出し側で `FITTER_MARGIN_MB` を足した予算で考える。
pub fn max_ctx_fitting(
    weights_mb: u64,
    vram_free_mb: u64,
    n_layers: u32,
    n_head_kv: u32,
    key_len: u32,
    val_len: u32,
    kv_bits: u8,
) -> u32 {
    max_ctx_with_overhead(
        weights_mb,
        vram_free_mb,
        KvGeometry {
            n_layers,
            n_head_kv,
            key_length: key_len,
            value_length: val_len,
        },
        kv_bits,
        COMPUTE_BUFFER_MB,
    )
}

fn max_ctx_with_overhead(
    weights_mb: u64,
    vram_free_mb: u64,
    g: KvGeometry,
    kv_bits: u8,
    overhead_mb: u64,
) -> u32 {
    let budget = vram_free_mb
        .saturating_sub(weights_mb)
        .saturating_sub(overhead_mb);
    if budget == 0 || g.n_layers == 0 || g.n_head_kv == 0 {
        return 0;
    }

    // 1 段（512 トークン）あたりの KV から当たりを付ける。kv_mb_exact は切り上げるので
    // この見積もりは必ず真の上限以下に出る＝下の検算ループは高々数回しか回らない。
    let per_step = kv_mb_exact(
        g.n_layers,
        g.n_head_kv,
        g.key_length,
        g.value_length,
        CTX_STEP,
        kv_bits,
    )
    .max(1);
    let max_steps = u64::from(MAX_PROBE_CTX / CTX_STEP);
    let mut ctx = (budget / per_step).min(max_steps) as u32 * CTX_STEP;

    // 切り上げ誤差の分だけ実計算で詰める。決定論的に下へ降りるだけなので必ず止まる。
    while ctx >= CTX_STEP
        && kv_mb_exact(
            g.n_layers,
            g.n_head_kv,
            g.key_length,
            g.value_length,
            ctx,
            kv_bits,
        ) > budget
    {
        ctx -= CTX_STEP;
    }
    if ctx < CTX_STEP {
        0
    } else {
        ctx
    }
}

/// 手元の .gguf を読んで、実値で可否を出す。
///
/// ★行き止まりにしない。「載りません」で終える代わりに、
///   ・そのままで載るなら余裕を言う
///   ・載らないなら **ctx をいくつにすれば載るか**（＋ KV を q8_0 にした場合の値）を言う
///   ・重みだけで超えているときだけ、CPU 分担／LAN 推論機へ案内する
///  これは llama.cpp 側の既定の振る舞い（自動 fit は文脈を縮めて載せる）とも一致する。
#[cfg_attr(not(feature = "no_tauri"), tauri::command)]
pub async fn fit_local(path: String, ctx: u32, vram_free_mb: u64) -> Result<FitVerdict, String> {
    let p = std::path::PathBuf::from(path);
    // GGUF ヘッダ読みは同期 I/O。Tauri の非同期ランタイムのワーカーを塞がないよう逃がす。
    let (size_bytes, meta) = tokio::task::spawn_blocking(move || {
        let size = std::fs::metadata(&p)
            .map_err(|e| format!("{} を読めません: {e}", p.display()))?
            .len();
        let meta = crate::gguf::read_meta(&p)?;
        Ok::<_, String>((size, meta))
    })
    .await
    .map_err(|e| format!("GGUF の読み取りが異常終了しました: {e}"))??;

    Ok(verdict_from_meta(size_bytes, &meta, ctx, vram_free_mb))
}

/// `fit_local` の中身（純粋関数）。I/O を切り離してあるので試験できる。
fn verdict_from_meta(
    size_bytes: u64,
    meta: &GgufMeta,
    ctx: u32,
    vram_free_mb: u64,
) -> FitVerdict {
    let weights_mb = size_bytes.div_ceil(MIB);

    // 0 が来たらモデルの学習文脈長、それも無ければ llama.cpp の自動 fit の既定に合わせる。
    let ctx = if ctx == 0 {
        meta.context_length.unwrap_or(4096)
    } else {
        ctx
    };

    // 実値が揃わないファイル（層数やヘッド数を書いていない GGUF）は、
    // 実値のふりをせず従来の推測に落とす。どちらで出した数字かは kv_note で必ず言う。
    let Some(g) = meta.kv_geometry() else {
        let mut v = estimate_fit(size_bytes, None, meta.n_layers, ctx, vram_free_mb, false);
        v.kv_note = Some(
            "この GGUF は層数かヘッド数を持っていないため、KV は推測値です（実値ではありません）。"
                .to_string(),
        );
        return v;
    };

    let kv_mb = kv_mb_exact(g.n_layers, g.n_head_kv, g.key_length, g.value_length, ctx, 16);
    let overhead_mb = COMPUTE_BUFFER_MB + FITTER_MARGIN_MB;
    let needed_mb = weights_mb + kv_mb + overhead_mb; // 推奨（fitter の余白込み）
    let needed_min_mb = weights_mb + kv_mb + COMPUTE_BUFFER_MB; // 実効下限

    // 伸ばせる／下げるべき ctx。モデルの学習文脈長を超える提案はしない
    // （超えても rope 拡張で動きはするが、それはこの画面が勧めることではない）。
    let train_ctx = meta.context_length.unwrap_or(MAX_PROBE_CTX);
    let cap = |c: u32| c.min(train_ctx);
    let max_f16 = cap(max_ctx_fitting(
        weights_mb,
        vram_free_mb,
        g.n_layers,
        g.n_head_kv,
        g.key_length,
        g.value_length,
        16,
    ));
    let max_q8 = cap(max_ctx_fitting(
        weights_mb,
        vram_free_mb,
        g.n_layers,
        g.n_head_kv,
        g.key_length,
        g.value_length,
        8,
    ));

    let geom_note = format!(
        "KV は GGUF の実値から計算しています（{} 層／KV ヘッド {}／head_dim {}{}）。",
        g.n_layers,
        g.n_head_kv,
        g.key_length,
        if g.value_length == g.key_length {
            String::new()
        } else {
            format!("・V 側 {}", g.value_length)
        }
    );
    let ceiling_note = if max_f16 >= CTX_STEP {
        let mut t = format!(
            "{}この空きで届く上限は ctx {}",
            geom_note,
            fmt(u64::from(max_f16))
        );
        // q8_0 を勧めるのは実際に伸びるときだけ。学習文脈長で頭打ちなら同じ値が並ぶだけで、
        // 「効きそうに見えて効かない助言」になる。
        if max_q8 > max_f16 {
            t.push_str(&format!(
                "、KV を q8_0（-ctk q8_0 -ctv q8_0）にすれば ctx {} まで",
                fmt(u64::from(max_q8))
            ));
        } else if max_f16 == train_ctx {
            t.push_str("（モデルの学習文脈長で頭打ち）");
        }
        t.push('。');
        t
    } else {
        geom_note.clone()
    };
    let margin_note = "余白のうち 1,024 MB は llama.cpp の fitter の既定（-fitt）で、絶対要件ではありません。";

    // (1) 推奨余白まで含めて収まる。
    if needed_mb <= vram_free_mb {
        return FitVerdict {
            verdict: "fits".to_string(),
            needed_mb,
            weights_mb,
            kv_mb,
            headroom_mb: vram_free_mb - needed_mb,
            note: format!(
                "載ります。ctx {} で必要 {} MB（重み {} MB＋KV {} MB＋余白 {} MB）に対し空き {} MB、余裕 {} MB。",
                fmt(u64::from(ctx)),
                fmt(needed_mb),
                fmt(weights_mb),
                fmt(kv_mb),
                fmt(overhead_mb),
                fmt(vram_free_mb),
                fmt(vram_free_mb - needed_mb),
            ),
            est_tok_s: None,
            max_ctx: Some(max_f16),
            kv_note: Some(ceiling_note),
        };
    }

    // (2) 推奨余白には届かないが、実際には収まる。
    //     ここを「載りません」にしていたのが元の設計の誤り。fitter の余白は安全策であって
    //     ハードな要件ではないので、事実（薄い）を言ったうえで載ると答える。
    if needed_min_mb <= vram_free_mb {
        let short = needed_mb - vram_free_mb;
        // 推奨余白まで確保したいなら ctx をいくつにすべきか。
        let comfy = cap(max_ctx_with_overhead(
            weights_mb,
            vram_free_mb,
            g,
            16,
            COMPUTE_BUFFER_MB + FITTER_MARGIN_MB,
        ));
        let comfy_q8 = cap(max_ctx_with_overhead(
            weights_mb,
            vram_free_mb,
            g,
            8,
            COMPUTE_BUFFER_MB + FITTER_MARGIN_MB,
        ));
        let comfy_hint = if comfy >= CTX_STEP {
            format!(
                "推奨余白まで空けるなら ctx {}（KV を q8_0 にすれば ctx {}）まで下げることになります。",
                fmt(u64::from(comfy)),
                fmt(u64::from(comfy_q8))
            )
        } else if comfy_q8 >= CTX_STEP {
            format!(
                "推奨余白まで空けるなら KV を q8_0（-ctk q8_0 -ctv q8_0）にして ctx {} です。",
                fmt(u64::from(comfy_q8))
            )
        } else {
            "推奨余白まで空ける余地はありません。".to_string()
        };
        return FitVerdict {
            verdict: "fits".to_string(),
            needed_mb,
            weights_mb,
            kv_mb,
            headroom_mb: 0,
            note: format!(
                "載ります（ただし余裕は薄い）。ctx {} で重み {} MB＋KV {} MB＋compute {} MB＝{} MB に対し空き {} MB。推奨余白まで積むと {} MB 足りませんが、{}{}",
                fmt(u64::from(ctx)),
                fmt(weights_mb),
                fmt(kv_mb),
                fmt(COMPUTE_BUFFER_MB),
                fmt(needed_min_mb),
                fmt(vram_free_mb),
                fmt(short),
                margin_note,
                comfy_hint,
            ),
            est_tok_s: None,
            max_ctx: Some(max_f16),
            kv_note: Some(ceiling_note),
        };
    }

    // (3) この ctx では入らない。★ここで止めない——入る ctx を具体値で出す。
    let shortfall = needed_min_mb - vram_free_mb;
    if max_f16 >= CTX_STEP {
        let q8_hint = if max_q8 > max_f16 {
            format!(
                "KV を q8_0（-ctk q8_0 -ctv q8_0）にすれば ctx {} まで伸ばせます。",
                fmt(u64::from(max_q8))
            )
        } else {
            String::new()
        };
        return FitVerdict {
            verdict: "no_fit".to_string(),
            needed_mb,
            weights_mb,
            kv_mb,
            headroom_mb: 0,
            note: format!(
                "ctx {} では {} MB 足りませんが、ctx を {} にすれば載ります（KV {} MB → {} MB）。{}{}",
                fmt(u64::from(ctx)),
                fmt(shortfall),
                fmt(u64::from(max_f16)),
                fmt(kv_mb),
                fmt(kv_mb_exact(g.n_layers, g.n_head_kv, g.key_length, g.value_length, max_f16, 16)),
                q8_hint,
                margin_note,
            ),
            est_tok_s: None,
            max_ctx: Some(max_f16),
            kv_note: Some(geom_note),
        };
    }

    // (4) f16 の KV ではどの ctx でも入らない。まだ q8_0 という手が残っている。
    if max_q8 >= CTX_STEP {
        return FitVerdict {
            verdict: "no_fit".to_string(),
            needed_mb,
            weights_mb,
            kv_mb,
            headroom_mb: 0,
            note: format!(
                "f16 の KV ではどの文脈長でも入りません（{} MB 不足）。ただし KV を q8_0（-ctk q8_0 -ctv q8_0）にすれば ctx {} で載ります。{}",
                fmt(shortfall),
                fmt(u64::from(max_q8)),
                margin_note,
            ),
            est_tok_s: None,
            max_ctx: Some(max_q8),
            kv_note: Some(geom_note),
        };
    }

    // (5) 重みだけで空きを超えている。文脈長では解決しないので、別の手を案内する。
    FitVerdict {
        verdict: "no_fit".to_string(),
        needed_mb,
        weights_mb,
        kv_mb,
        headroom_mb: 0,
        // ここでは fitter の余白の話をしない。余白を削っても足りない状況なので、
        // 出しても「効きそうに見えて効かない助言」になる。
        note: format!(
            "重みだけで {} MB あり、空き {} MB に収まりません（文脈長を下げても解決しません）。一部の層を CPU に置く（-ngl を下げる）か、より小さい量子化を選ぶか、LAN の推論機を使ってください。",
            fmt(weights_mb),
            fmt(vram_free_mb),
        ),
        est_tok_s: None,
        max_ctx: None,
        kv_note: Some(geom_note),
    }
}

// ---------------------------------------------------------------------------
// 推定のための下請け
// ---------------------------------------------------------------------------

/// KV キャッシュの見積もり（MiB）と、それが粗い上限かどうか。
///
/// 式:
///   kv_bytes = 2 (K と V) × n_layers × ctx × kv_dim × 2 バイト (f16)
///
/// - f16 を仮定する。`-ctk/-ctv q8_0` を使えば実際は約半分になるが、
///   既定は f16 なので既定で見積もる（小さく出して外すより良い）。
/// - `kv_dim` は本来 n_head_kv × head_dim だが、GGUF を読む前は分からない。
///   ★以前は **d_model / 4** を仮定していた。これは head_dim × n_head == d_model かつ
///   n_head / n_head_kv == 4 のときしか合わず、実際には外れる:
///     Qwen3-14B  d_model 5120・n_head_kv 8・head_dim 128 → 1024（d_model/4 なら 1280 で 1.25 倍）
///     Qwen3-4B   d_model 2560・n_head_kv 8・head_dim 128 → 1024（d_model/4 なら 640 で**過小**）
///   近年の GQA は「KV ヘッド 8 × head_dim 128 = 1024」に収束していて、d_model にはほとんど
///   連動しない。そこで **1024 を基準**にし、d_model/4 でクリップする形に改める。
///   （Gemma 系のように head_dim 256 の例外は 2048 になるので、上限を 2048 に置いて拾う）
/// - ファイルが手元にあるなら推測する理由は無い。`kv_mb_exact` を使うこと。
fn estimate_kv_mb(layers: u64, ctx: u32, params: u64, layers_known: bool) -> (u64, bool) {
    let d_model = infer_d_model(params);
    let kv_dim = (d_model / 4).clamp(1024, 2048);
    let bytes = 2u64
        .saturating_mul(layers)
        .saturating_mul(u64::from(ctx))
        .saturating_mul(kv_dim)
        .saturating_mul(2); // f16
    (bytes.div_ceil(MIB), !layers_known)
}

/// パラメータ数から d_model を粗く当てる。既存モデルの実値から作った段階表で、
/// 補間はしない（境界付近で滑らかに間違えるより、段で外したほうが後から気づける）。
fn infer_d_model(params: u64) -> u64 {
    const B: u64 = 1_000_000_000;
    match params {
        p if p <= 2 * B => 2048,
        p if p <= 4 * B => 2560,
        p if p <= 9 * B => 4096,
        p if p <= 16 * B => 5120,
        p if p <= 40 * B => 5120,
        p if p <= 80 * B => 8192,
        _ => 8192,
    }
}

/// 層数が不明なときの推定。同上、粗い段階表。
/// ★MoE は総パラメータ数に対して層が少ない傾向があるため、この表は MoE では過大になりがち。
///   過大＝安全側なのでそのままにしている。
fn infer_layers(params: u64) -> u64 {
    const B: u64 = 1_000_000_000;
    match params {
        p if p <= 2 * B => 24,
        p if p <= 4 * B => 32,
        p if p <= 9 * B => 32,
        p if p <= 16 * B => 40,
        p if p <= 40 * B => 48,
        p if p <= 80 * B => 64,
        _ => 80,
    }
}

/// パラメータ数が分からないときに、ファイルサイズから逆算する。
/// 4.5 bit/param は Q4_K_M の実効ビット数の目安（Q4_K_M は層ごとに混在するため一定ではない）。
/// **量子化が違えば当然ずれる**。あくまで層数・次元を段階表に食わせるための粗い入力。
fn infer_params_from_size(size_bytes: u64) -> u64 {
    let bits = size_bytes.saturating_mul(8);
    ((bits as f64) / 4.5) as u64
}

/// experts を CPU に降ろしたときの生成速度（下限側）の概算。
///
/// 時間は足し算になるので、速度は調和平均で合成する:
///   t = (1-r)/GPU + r/CPU 、 tok/s = 1/t
/// r は「VRAM に入りきらなかった割合」で近似する。
/// ★端点は仮定値であり、これは実測の代用にならない。UI 側でも「推定」と明示すること。
fn estimate_offload_tok_s(shortfall_mb: u64, weights_mb: u64) -> f32 {
    if weights_mb == 0 {
        return ASSUMED_CPU_TOK_S;
    }
    let r = ((shortfall_mb as f32) / (weights_mb as f32)).clamp(0.0, 1.0);
    let t = (1.0 - r) / ASSUMED_GPU_TOK_S + r / ASSUMED_CPU_TOK_S;
    if t <= 0.0 {
        return ASSUMED_GPU_TOK_S;
    }
    (1.0 / t).max(0.5)
}

/// 不足分が KV だけで吸収できるなら、下げるべき ctx を提案する。
/// KV は ctx に比例するので、必要な削減率をそのまま ctx に掛ける。
/// 2048 未満になるなら「ctx を下げれば載る」とは言わない（実用にならないため）。
fn suggest_smaller_ctx(ctx: u32, shortfall_mb: u64, kv_mb: u64) -> Option<u32> {
    if kv_mb == 0 || shortfall_mb >= kv_mb {
        return None;
    }
    let remaining = kv_mb - shortfall_mb;
    let ratio = (remaining as f64) / (kv_mb as f64);
    let proposed = ((f64::from(ctx) * ratio) as u64) & !1023; // 1024 の倍数に丸める
    if proposed >= 2048 {
        Some(proposed as u32)
    } else {
        None
    }
}

/// 3桁区切り。UI は tabular-nums で出す前提なので、桁を揃えられるようにする。
fn fmt(n: u64) -> String {
    let s = n.to_string();
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len() + s.len() / 3);
    for (i, b) in bytes.iter().enumerate() {
        if i > 0 && (bytes.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(*b as char);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const GB: u64 = 1024 * 1024 * 1024;

    #[test]
    fn fmt_groups_digits() {
        assert_eq!(fmt(0), "0");
        assert_eq!(fmt(999), "999");
        assert_eq!(fmt(1234), "1,234");
        assert_eq!(fmt(18600), "18,600");
    }

    #[test]
    fn small_model_fits_on_12gb() {
        // ★この試験は元々失敗していた（cargo check は #[cfg(test)] を組み立てないので誰も気づいていない）。
        //   元の想定「9GB（= 9,216MB）を空き 12,000MB に」は、余白 1,408MB を積むと
        //   9,216 + 1,600 + 1,408 = 12,224MB で**本当に入らない**。コードではなく想定のほうが誤り。
        //   14B の Q4_K_M の実物は 8.4GiB = 8,585MB なので、実寸に直す。
        let v = estimate_fit(8_585 * MIB, Some(14_000_000_000), Some(40), 8192, 12_000, false);
        assert_eq!(v.verdict, "fits");
        assert!(v.headroom_mb > 0);
        assert!(v.est_tok_s.is_none());
    }

    #[test]
    fn dense_model_too_big_is_no_fit() {
        let v = estimate_fit(18 * GB, Some(32_000_000_000), Some(64), 32768, 8_000, false);
        assert_eq!(v.verdict, "no_fit");
        assert_eq!(v.headroom_mb, 0);
    }

    #[test]
    fn moe_too_big_offers_cpu_offload() {
        // 30B-A3B Q4_K_M ≒ 18.6GB を 12GB に載せようとした場合。
        let v = estimate_fit(18_600 * MIB, Some(30_000_000_000), Some(48), 32768, 12_000, true);
        assert_eq!(v.verdict, "fits_with_cpu_offload");
        let est = v.est_tok_s.expect("MoE の逃げ道には推定速度を付ける");
        assert!(est > 0.0 && est < ASSUMED_GPU_TOK_S);
    }

    #[test]
    fn kv_scales_with_ctx() {
        let a = estimate_fit(4 * GB, Some(8_000_000_000), Some(32), 4096, 24_000, false);
        let b = estimate_fit(4 * GB, Some(8_000_000_000), Some(32), 32768, 24_000, false);
        assert!(b.kv_mb > a.kv_mb);
        assert_eq!(b.weights_mb, a.weights_mb);
    }

    #[test]
    fn unknown_layers_still_produces_a_verdict() {
        let v = estimate_fit(5 * GB, None, None, 8192, 12_000, false);
        assert!(!v.verdict.is_empty());
        assert!(v.needed_mb >= v.weights_mb + v.kv_mb);
    }

    // -- 実値による計算（オーナー機で実際に起きた場面の数字） ------------------
    //
    //   RTX 4070: 総 12,282 MB / 空き 10,339 MB（デスクトップ合成が約 1.9GB）
    //   Qwen3-14B-abliterated Q4_K_M: 8.4 GiB = 8,585 MB
    //   40 層 / n_head 40 / n_head_kv 8 / key=value=128
    //   → KV(ctx 8192, f16) = 1,280 MiB。アプリは 1,920 MB と見ていた（1.5 倍）。

    fn qwen3_14b_meta() -> GgufMeta {
        GgufMeta {
            arch: Some("qwen3".to_string()),
            n_layers: Some(40),
            n_embd: Some(5120),
            n_head: Some(40),
            n_head_kv: Some(8),
            key_length: Some(128),
            value_length: Some(128),
            context_length: Some(40960),
        }
    }

    #[test]
    fn kv_exact_matches_measured_values() {
        assert_eq!(kv_mb_exact(40, 8, 128, 128, 8192, 16), 1280);
        assert_eq!(kv_mb_exact(40, 8, 128, 128, 4096, 16), 640);
        // q8_0 は半分。
        assert_eq!(kv_mb_exact(40, 8, 128, 128, 8192, 8), 640);
        assert_eq!(kv_mb_exact(40, 8, 128, 128, 4096, 8), 320);
    }

    #[test]
    fn kv_exact_is_linear_in_ctx_and_layers() {
        let a = kv_mb_exact(40, 8, 128, 128, 4096, 16);
        assert_eq!(kv_mb_exact(40, 8, 128, 128, 8192, 16), a * 2);
        assert_eq!(kv_mb_exact(80, 8, 128, 128, 4096, 16), a * 2);
        assert_eq!(kv_mb_exact(40, 16, 128, 128, 4096, 16), a * 2);
    }

    #[test]
    fn max_ctx_on_the_owners_4070() {
        let c = max_ctx_fitting(8_585, 10_339, 40, 8, 128, 128, 16);
        assert!(c >= 4096, "4096 すら出ないなら計算が間違っている: {c}");
        assert_eq!(c % 512, 0, "512 の倍数であること: {c}");
        // その ctx の KV が本当に予算に収まっていること（＝答えが自己整合）。
        let budget = 10_339 - 8_585 - COMPUTE_BUFFER_MB;
        assert!(kv_mb_exact(40, 8, 128, 128, c, 16) <= budget);
        // 1 段でも上げたら溢れること（＝最大であること）。
        assert!(kv_mb_exact(40, 8, 128, 128, c + 512, 16) > budget);
        // q8_0 なら f16 より伸びる。
        assert!(max_ctx_fitting(8_585, 10_339, 40, 8, 128, 128, 8) > c);
    }

    #[test]
    fn max_ctx_returns_zero_when_weights_alone_overflow() {
        assert_eq!(max_ctx_fitting(12_000, 10_339, 40, 8, 128, 128, 16), 0);
    }

    #[test]
    fn local_verdict_uses_exact_kv_not_the_old_guess() {
        // 8.4 GiB
        let size = 8_585 * MIB;
        let v = verdict_from_meta(size, &qwen3_14b_meta(), 8192, 10_339);
        assert_eq!(v.kv_mb, 1280, "旧推測の 1,920 MB に戻っていないこと");
        // 推奨余白込みでは足りないが、実際には載る側に倒れること（行き止まりにしない）。
        assert_eq!(v.verdict, "fits");
        assert!(v.note.contains("薄い"), "{}", v.note);
        assert!(v.max_ctx.unwrap() >= 8192);
    }

    #[test]
    fn local_verdict_offers_a_smaller_ctx_instead_of_a_dead_end() {
        // 空きを削って、8192 では入らない状況を作る。
        let v = verdict_from_meta(8_585 * MIB, &qwen3_14b_meta(), 8192, 9_600);
        assert_eq!(v.verdict, "no_fit");
        let c = v.max_ctx.expect("下げれば載る ctx を必ず出す");
        assert!(c >= 512 && c < 8192, "{c}");
        assert!(v.note.contains("にすれば載ります"), "{}", v.note);
        assert!(v.note.contains("q8_0"), "逃げ道を必ず添える: {}", v.note);
    }

    #[test]
    fn local_verdict_caps_at_the_models_training_context() {
        // 空きが潤沢でも、学習文脈長（ここでは 4096）を超える提案はしない。
        let mut meta = qwen3_14b_meta();
        meta.context_length = Some(4096);
        let v = verdict_from_meta(8_585 * MIB, &meta, 2048, 24_000);
        assert_eq!(v.verdict, "fits");
        assert_eq!(v.max_ctx, Some(4096));
    }

    #[test]
    fn local_verdict_falls_back_when_the_header_lacks_geometry() {
        let meta = GgufMeta {
            arch: Some("mystery".to_string()),
            ..Default::default()
        };
        let v = verdict_from_meta(4 * GB, &meta, 8192, 12_000);
        assert!(!v.verdict.is_empty());
        // 推測を実値のように見せない。
        assert!(v.kv_note.unwrap().contains("推測値"));
    }

    #[test]
    fn local_verdict_says_weights_alone_are_too_big() {
        let v = verdict_from_meta(20 * GB, &qwen3_14b_meta(), 8192, 10_339);
        assert_eq!(v.verdict, "no_fit");
        assert_eq!(v.max_ctx, None);
        assert!(v.note.contains("-ngl"), "{}", v.note);
    }

    #[test]
    fn rough_estimate_no_longer_overshoots_kv_by_half() {
        // 落とす前の推測でも、実値 1,280 MB に対して 1.5 倍（1,920）は出さないこと。
        let (kv, rough) = estimate_kv_mb(40, 8192, 14_000_000_000, true);
        assert!(!rough);
        let exact = kv_mb_exact(40, 8, 128, 128, 8192, 16);
        assert!(kv >= exact, "推測は過大側に倒す方針を保つ: {kv} < {exact}");
        assert!(kv <= exact * 5 / 4, "1.25 倍を超えて外さないこと: {kv}");
    }
}
