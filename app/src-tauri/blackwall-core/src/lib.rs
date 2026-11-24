//! 本体の**純粋な**モジュールを、Tauri 抜きで試験するための入口。
//!
//! ★ここに載せてよいのは `tauri` を一切参照しないモジュール、または
//!   `#[tauri::command]` **属性だけ**が壁のモジュール（属性は
//!   `#[cfg_attr(not(feature = "no_tauri"), tauri::command)]` に置き換えてあり、
//!   ここでは no_tauri を立てて属性を外す）。それ以外の `tauri::` 参照が
//!   入った瞬間にビルドが落ちるので、**純粋さがコンパイルで守られる**。
#[path = "../../src/cancel.rs"]
pub mod cancel;
#[path = "../../src/edit_guard.rs"]
pub mod edit_guard;
#[path = "../../src/gguf.rs"]
pub mod gguf;
#[path = "../../src/sampling.rs"]
pub mod sampling;
#[path = "../../src/think.rs"]
pub mod think;

// ★属性だけが壁のモジュール（2026-09-02）。
//   fit は gguf に依存（上で取り込み済み）＋ tokio。
//   catalog は reqwest ＋ tokio。どちらも crate 内の他モジュールには依存しない。
#[path = "../../src/fit.rs"]
pub mod fit;
#[path = "../../src/catalog.rs"]
pub mod catalog;

// ★純粋な解析だけを持つ新しいモジュール（2026-09-02）。
//   本体 `src/llama.rs` は tauri を参照するのでここへは入れられない。そこで
//   **解析だけを切り出してこちらに置き**、`llama.rs` 側が `#[path]` で参照する
//   （向きは逆だが、実体は1本＝二重管理は起きない。試験はここで実際に走る）。
pub mod llama_log;

// ★線（NDJSON）の翻訳表（2026-09-04・W1）。実体はここ、`src/sidecar.rs` が
//   `#[path]` で参照する。sidecar.rs は tauri を参照するのでホストでは
//   `cargo test` がリンクできず、あちらに書くと**また一度も走らない試験**になる。
pub mod wire;

// ★線の 1 行を画面の操作へ翻訳する純粋関数（2026-09-04・W1）。実体はここ、
//   `src/sidecar.rs` が `#[path]` で参照する。移す前、この 21 本は
//   **一度も実行されたことがなかった**（tauri crate はホストでリンクできない）。
pub mod translate;

// ★作業ディレクトリの封じ込め（resolve_in_ws）と一覧・読み・書き（2026-09-05・
//   r4/tests-never-run）。実体はここ、`src/workspace.rs` が `#[path]` で参照する。
//   移す前、シンボリックリンク/8.3短縮名/ADS/予約デバイス名などパストラバーサル
//   防御を検査する試験 33 本が**一度も実行されたことがなかった**（workspace.rs は
//   tauri::AppHandle を参照するのでホストで cargo test がリンクできない）。
pub mod workspace_guard;

// ★journal.rs の封筒組み立て・ladder・時刻書式・素性のハッシュ・送信バッチ計画・
//   古い日の削除計画・gzip・行の切り出し（2026-09-05・r4/tests-never-run）。
//   実体はここ、`src/journal.rs` が `#[path]` で参照する。移す前、29 本の試験の
//   うち 28 本が**一度も実行されたことがなかった**（journal.rs は
//   tauri::async_runtime::spawn と #[tauri::command] を使うのでホストで
//   cargo test がリンクできない）。
pub mod journal_format;

// ★推論の宛先を決める純粋な論理（2026-09-06）。実体はここ、`src/main.rs` が
//   `#[path]` で参照する。決め方は `sidecar.rs::backend_url()` と
//   `main.rs::lan_backend()` に分かれた素の分岐で、**試験が1本も無かった**
//   （ある実機が 0.1.194 で LAN へ1件も送らなくなった障害の現場）。
pub mod backend_choice;

// ★会話の一覧と記録の守り（2026-11-23）。実体はここ、`src/convos.rs` が `#[path]` で参照する。
//   convos.rs は `crate::app_root()` と `#[tauri::command]` を使うのでホストでは試験が走らない
//   （あちらの試験 4 本は一度も走っていなかった）。一覧が信用できないときに記録を消さない判定と、ゴミ箱をここに置く。
pub mod convos_guard;

// ★本体に渡す作業場の決め方（2026-11-23・チャットモード）。`src/agent.rs` が `#[path]` で参照する。
pub mod engine_ws;
