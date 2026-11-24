//! 自己診断 — ★私（開発側）が実機で直接確かめるための口。
//!
//! なぜ要るか: ここまでの不具合（preset が開けない／モデル名の食い違い／非 ASCII パス）は
//! すべて **利用者にエラーを貼ってもらって初めて分かった**。1往復ずつ人手を使うのは
//! 遅いだけでなく、症状しか見えないので**原因でなく症状に合わせて直してしまう**
//! （実際に2回それをやった）。同じコード経路を headless で通せれば、壊れている環を
//! その場で特定できる。
//!
//! 設計上の約束:
//!   - **待ち受けポートを作らない。** 「エージェントを起動できるポート」は任意コード実行の口。
//!     診断は起動引数で1回走って終わる（bw-run/bw-view と同じ思想）。
//!   - **既定では壊さない。** 書き込みを伴う確認は `--write` を明示したときだけ。
//!   - **各段の結果を必ず出す。** 「動きません」ではなく、どの環で切れたかを出す。

use std::path::PathBuf;

pub struct Step {
    pub name: &'static str,
    pub ok: bool,
    pub detail: String,
}

fn step(name: &'static str, ok: bool, detail: impl Into<String>) -> Step {
    Step { name, ok, detail: detail.into() }
}

/// 診断を上から順に行う。前段が落ちても後段は続ける（どこまで壊れているかを知りたいので）。
pub async fn run(allow_write: bool) -> Vec<Step> {
    let mut out = Vec::new();

    // 1) 置き場所。★非 ASCII だと llama.cpp（CreateFileA/fopen）が開けない。
    let root: PathBuf = crate::app_root();
    let ascii = root.to_string_lossy().is_ascii();
    out.push(step("データ置き場", ascii,
        format!("{}{}", root.display(), if ascii { "" } else { "  ★非ASCII（llama.cpp が開けない）" })));

    // 1.5) ★本体（Python）が起動できるか。
    //   これが偽だと、アプリは道具5個の縮小版で動く。**そのことに気づけないのが一番困る**
    //   ので、能力が下がる条件をここで名指しする。Linux 上では確かめられない環なので、
    //   実機で押してもらう前提の段。
    let core = crate::sidecar::core_available().await;
    let tools = crate::sidecar::core_tools().await;
    out.push(step(
        "本体（Python）",
        core,
        match (core, &tools) {
            (true, Some(t)) => format!("使えます。道具 {} 個: {}", t.len(), t.join(" ")),
            (true, None) => "使えます（道具一覧は取れず）".to_string(),
            (false, _) => format!(
                "使えません → アプリ内蔵の道具 {} 個で動きます。理由: {}",
                crate::agent::tool_names().len(),
                crate::sidecar::last_unavailable_reason()
                    .unwrap_or_else(|| "（理由不明）".to_string())
            ),
        },
    ));

    // 2) 同梱バックエンド
    let models = crate::models_dir();
    out.push(step("モデル置き場", models.is_dir(), models.display().to_string()));

    // 3) 手持ちモデル
    match crate::hf::list_local_models().await {
        Ok(v) => out.push(step("手持ちモデル", !v.is_empty(),
            if v.is_empty() { "0 件（.gguf がありません）".to_string() }
            else { v.iter().map(|m| m.name.clone()).collect::<Vec<_>>().join(", ") })),
        Err(e) => out.push(step("手持ちモデル", false, e)),
    }

    // 4) GPU
    match crate::gpu::probe_gpu().await {
        Ok(g) => out.push(step("GPU", g.vram_total_mb > 0,
            format!("{} 空き {}MB / 全 {}MB ({}){}", g.name, g.vram_free_mb, g.vram_total_mb, g.source,
                if g.warnings.is_empty() { String::new() } else { format!("  注意: {}", g.warnings.join("; ")) }))),
        Err(e) => out.push(step("GPU", false, e)),
    }

    if !allow_write {
        out.push(step("推論器", true, "--write を付けると起動まで確かめます（既定では触りません）"));
        return out;
    }

    // 6) 推論器の起動 ★ここが何度も落ちてきた環
    match crate::llama::llama_start_headless().await {
        Ok(s) => {
            out.push(step("推論器の起動", s.running,
                format!("port={} backend={} 登録モデル={}", s.port, s.backend,
                    if s.models.is_empty() { "（0件）".to_string() }
                    else { s.models.iter().map(|m| format!("{}[{}]", m.id, m.state)).collect::<Vec<_>>().join(", ") })));

            // 7) 読み込み ★名前の食い違いはここで出る
            if let Some(first) = s.models.first() {
                match crate::llama::llama_load(first.id.clone()).await {
                    Ok(()) => out.push(step("モデルの読み込み", true, first.id.clone())),
                    Err(e) => out.push(step("モデルの読み込み", false, e)),
                }
                // ★読み込めたら、実際に1文だけ生成して端から端まで通す。
                //   「読み込めた」と「使える」は別。ここまで通れば実用の証明になる。
                match crate::llama::generate_probe(&first.id).await {
                    Ok(txt) => out.push(step("生成の確認", !txt.trim().is_empty(),
                        format!("応答: {}", txt.chars().take(40).collect::<String>()))),
                    Err(e) => out.push(step("生成の確認", false, e)),
                }
            } else {
                out.push(step("モデルの読み込み", false, "router に1件も登録されていません（preset を確認）"));
            }
        }
        Err(e) => out.push(step("推論器の起動", false, e)),
    }
    out
}

pub fn print(steps: &[Step]) {
    let mut bad = 0;
    println!("== L.A.P.S Console 自己診断 ==");
    for s in steps {
        if !s.ok { bad += 1; }
        println!("  [{}] {:<16} {}", if s.ok { "OK" } else { "NG" }, s.name, s.detail);
    }
    println!("-- {} 段中 {} 段が失敗 --", steps.len(), bad);
}


/// 画面からの自己診断。★アプリ側にも口を出す（利用者が自分で確かめられる）。
/// 起動を伴う確認は `write=true` のときだけ行う。
#[tauri::command]
pub async fn self_test(write: bool) -> Result<Vec<SelfTestRow>, String> {
    Ok(run(write).await.into_iter()
        .map(|s| SelfTestRow { name: s.name.to_string(), ok: s.ok, detail: s.detail })
        .collect())
}

#[derive(serde::Serialize)]
pub struct SelfTestRow {
    pub name: String,
    pub ok: bool,
    pub detail: String,
}
