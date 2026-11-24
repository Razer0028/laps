//! agent.rs — ツールを使う生成ループ（作業ディレクトリを読み書きしながら答える）
//!
//! 位置づけ: llama.rs の `chat_send`（素の生成）はそのまま残し、その横に
//! 「ツール呼び出しの往復」だけを足した別経路として置く。生成の中核に手を入れずに
//! 機能を足せる形にしておく＝新機能の不具合が既存のチャットを巻き込まない。
//!
//! 安全について:
//! - ファイル操作の実体は `workspace.rs` が持つ。封じ込め（作業ディレクトリの外へ出さない）も
//!   あちら側の責務で、ここは呼ぶだけ。**パスの判断を自前で持たない**＝判定が2箇所に分かれて
//!   食い違う事故を作らない。
//! - ★ツールが返した中身は「情報」であって「指示」ではない。結果は必ず role="tool" の
//!   メッセージとしてだけ会話へ戻し、system 指示に混ぜない（間接プロンプト注入への備え）。
//! - 拒否・失敗は握り潰さずモデルにも画面にも理由を渡す。空の結果と失敗を区別できる形にする。

#[path = "../blackwall-core/src/engine_ws.rs"]
mod engine_ws;

use std::collections::BTreeMap;
use std::sync::OnceLock;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};

// ---------------------------------------------------------------------------
// 定数
// ---------------------------------------------------------------------------

/// 推論器のポートが取れなかったときに試す既定値（llama.rs の DEFAULT_PORT と同じ）。
const FALLBACK_PORT: u16 = 8760;

/// ツール往復の上限。超えたら止める。
/// 無制限にすると、モデルが延々と読み直すだけで返ってこない形が実際に起きる
/// （この種のエージェントの主要な失敗型は「間違える」ではなく「終わらない」）。
const MAX_ROUNDS: usize = 8;

/// 同じ (ツール名, 引数) がこの回数続いたら堂々巡りとみなして打ち切る。
const REPEAT_LIMIT: usize = 3;

/// 画面に出す結果の抜粋の長さ（文字数。バイトではない）。
const PREVIEW_CHARS: usize = 200;

/// 会話へ戻すツール結果の上限（文字数）。
/// workspace 側にも上限がある前提だが、ここでも歯止めを置く。1回の読み取りで文脈を
/// 使い切ると、以後の往復が全部無駄になる。★切ったときは黙らず本文に印を残す。
const TOOL_RESULT_MAX_CHARS: usize = 24_000;

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

/// 生成用クライアント。全体タイムアウトは付けない（長い生成が途中で切られるため）。
/// `no_proxy()` が要点: Windows ではシステムプロキシ設定を拾って 127.0.0.1 宛まで
/// プロキシに投げてしまうことがある（llama.rs と同じ理由）。
fn stream_client() -> &'static reqwest::Client {
    static C: OnceLock<reqwest::Client> = OnceLock::new();
    C.get_or_init(|| {
        reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(5))
            .build()
            .expect("reqwest streaming client")
    })
}

/// 推論器の場所。ポートは動的（塞がっていれば llama.rs が上へずらす）で、
/// `current_port()` は非公開なので、公開コマンド `llama_status()` から貰う。
/// 取れなければ既定値で試す＝外れれば接続エラーとして表に出るので、黙って間違えない。
async fn base_url() -> String {
    // ★LAN の推論機が有効ならそちら。ここを忘れると「道具なし」の既定経路だけが
    //   手元へ行き、画面は LAN を表示したまま別モデルが答える（敵対レビューで検出）。
    if let Some(root) = crate::inference_root() {
        return root;
    }
    let port = crate::llama::llama_status()
        .await
        .map(|s| s.port)
        .unwrap_or(FALLBACK_PORT);
    format!("http://127.0.0.1:{port}")
}

// ---------------------------------------------------------------------------
// ツール定義
// ---------------------------------------------------------------------------

/// モデルに見せるツール。**3つだけ**。
///
/// ★増やさないこと。ツール数は選択精度に最も効く（50個で 84〜95%、200個で 41%、
/// 740個でランダムという実測がある）。足したくなったら、まず1つ削れるか考える。
/// 渡している道具の名前。画面の一覧はここから作る（固定の一覧を持たせない）。
pub fn tool_names() -> Vec<String> {
    tools_schema()
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|t| t["function"]["name"].as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

fn tools_schema() -> Value {
    json!([
        {
            "type": "function",
            "function": {
                "name": "ws_list",
                "description": "作業ディレクトリの中身を一覧する。rel を省くと直下。まず何があるかを見るために使う。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel": {
                            "type": "string",
                            "description": "作業ディレクトリからの相対パス（フォルダ）。省略可。絶対パスや .. は拒否される。"
                        }
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "ws_read",
                "description": "作業ディレクトリ内のファイルを読む。中身を推測せず、必ず読んでから答えるために使う。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel": {
                            "type": "string",
                            "description": "作業ディレクトリからの相対パス。絶対パスや .. は拒否される。"
                        }
                    },
                    "required": ["rel"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "ws_edit",
                "description": "既存ファイルの一部を置き換える。old_string は前後の行を含めて一意にすること。同じ文字列が複数あるときは occurrence=N（1始まり）で場所を選ぶ。構文を壊す編集と、別々の場所を巻き込む一括置換は拒否される。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel": {"type": "string", "description": "作業ディレクトリからの相対パス。"},
                        "old_string": {"type": "string", "description": "置き換える前の文字列。ファイル内で一意になるように前後を含める。"},
                        "new_string": {"type": "string", "description": "置き換えた後の文字列。"},
                        "occurrence": {"type": "integer", "description": "複数一致するときに何番目を直すか（1始まり）。"},
                        "replace_all": {"type": "boolean", "description": "全部の一致を同じに変える場合のみ true。"}
                    },
                    "required": ["rel", "old_string", "new_string"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "ws_write",
                "description": "新しいファイルを作る。★既存ファイルの変更には使わないこと（全文を書き直すと中身を失う）。既存の変更は ws_edit を使う。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel": {
                            "type": "string",
                            "description": "作業ディレクトリからの相対パス。絶対パスや .. は拒否される。"
                        },
                        "content": {
                            "type": "string",
                            "description": "ファイル全体の新しい内容。"
                        }
                    },
                    "required": ["rel", "content"]
                }
            }
        }
    ])
}

// ---------------------------------------------------------------------------
// ストリームから組み立てるもの
// ---------------------------------------------------------------------------

/// 1件のツール呼び出し。delta では id / name / arguments が分割して届くので、
/// index をキーに継ぎ足していく。
#[derive(Debug, Default, Clone, PartialEq)]
struct PendingCall {
    id: String,
    name: String,
    /// JSON 文字列のまま持つ。分割到着を継ぎ足すので、途中では壊れた JSON になっている。
    args: String,
}

/// 1往復の結果。
#[derive(Debug, Default)]
struct Round {
    /// この往復でモデルが述べた本文（画面には逐次流し済み。ここでは履歴に残すために保持）。
    text: String,
    calls: Vec<PendingCall>,
    usage: Option<Value>,
    /// 利用者が止めたか。
    stopped: bool,
    /// 上限に当たって切られたか（finish_reason=length）。
    /// ★思考の途中で切られた場合に、思考を本文へ移さないための判断材料。
    truncated: bool,
}

/// delta（または非ストリームの message）1つぶんを取り込む。
///
/// ★name の扱いに癖がある: 実装によって「最初の1回だけ完全な名前」を送るものと、
/// 断片で送るものがある。素直に push_str すると前者で "ws_readws_read" になり、
/// 素直に代入すると後者が壊れる。**既に入っている文字列と同一なら重複送信とみなす**
/// ことで両方を通す。
fn merge_tool_delta(acc: &mut BTreeMap<i64, PendingCall>, node: &Value) {
    let Some(list) = node.get("tool_calls").and_then(|t| t.as_array()) else {
        return;
    };
    for (i, tc) in list.iter().enumerate() {
        let idx = tc
            .get("index")
            .and_then(|v| v.as_i64())
            .unwrap_or(i as i64);
        let slot = acc.entry(idx).or_default();

        if let Some(id) = tc.get("id").and_then(|v| v.as_str()) {
            if !id.is_empty() {
                slot.id = id.to_string();
            }
        }
        let Some(f) = tc.get("function") else { continue };

        if let Some(n) = f.get("name").and_then(|v| v.as_str()) {
            if !n.is_empty() && slot.name != n {
                slot.name.push_str(n);
            }
        }
        match f.get("arguments") {
            // 断片で届く通常の形。
            Some(Value::String(s)) => slot.args.push_str(s),
            // 一部の実装は完成したオブジェクトのまま返す。継ぎ足さず置き換える。
            Some(v) if !v.is_null() => slot.args = v.to_string(),
            _ => {}
        }
    }
}

/// 1往復ぶんの本体を組み立てる。`tools` が None なら既存の素の生成と同じ形になる。
fn build_body(model: &str, convo: &[Value], tools: Option<&Value>, thinking: bool) -> Value {
    let mut body = json!({
        "model": model,
        "messages": convo,
        "stream": true,
        "stream_options": { "include_usage": true },
        "timings_per_token": true,
    });
    crate::sampling::apply(&mut body);
    crate::sampling::set_thinking(&mut body, thinking);
    if let Some(t) = tools {
        // tool_choice は指定しない。ツールを使うかどうかはモデルに委ねる
        // （required にすると、答えるだけで済む問いにも無理やり読みに行く）。
        body["tools"] = t.clone();
    }
    body
}

// ---------------------------------------------------------------------------
// 1往復（SSE）
// ---------------------------------------------------------------------------

/// 1回分の生成を流し、本文をイベントで送りつつ、ツール呼び出しを組み立てて返す。
/// 読み方は llama.rs の `stream_chat` に倣う（`chunk()` で逐次・全体を溜めない）。
async fn stream_round(
    app: &AppHandle,
    base: &str,
    model: &str,
    convo: &[Value],
    tools: Option<&Value>,
    round: usize,
    meter: &mut crate::think::Meter,
    thinking: bool,
) -> Result<Round, String> {
    let url = format!("{base}/v1/chat/completions");
    let mut resp = stream_client()
        .post(url)
        .json(&build_body(model, convo, tools, thinking))
        .send()
        .await
        .map_err(|e| format!("推論器に届きません: {e}"))?;

    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("推論器が HTTP {code} を返しました: {}", text.trim()));
    }

    let mut buf: Vec<u8> = Vec::with_capacity(8192);
    let mut out = Round::default();
    let mut acc: BTreeMap<i64, PendingCall> = BTreeMap::new();
    // ★思考の分離はラウンドごとに新しく始める（1ラウンド＝1回の生成）。
    let mut split = crate::think::Splitter::default();
    let gen = crate::cancel::current();
    // Promote（思考でなかったと分かった）に備えて、思考ぶんも控えておく。
    let mut think_buf = String::new();
    let mut done = false;

    loop {
        if crate::cancel::is_stopped(gen) {
            out.stopped = true;
            break;
        }
        let chunk = match resp.chunk().await {
            Ok(Some(c)) => c,
            Ok(None) => break, // 接続が正常に閉じた
            Err(e) => return Err(format!("受信が途切れました: {e}")),
        };
        buf.extend_from_slice(&chunk);

        // 行単位で取り出す。\n で切るので UTF-8 の途中で割れることはない。
        while let Some(pos) = buf.iter().position(|b| *b == b'\n') {
            let line_bytes: Vec<u8> = buf.drain(..=pos).collect();
            let decoded = String::from_utf8_lossy(&line_bytes);
            let line = decoded.trim_end_matches(|c| c == '\r' || c == '\n');

            if line.is_empty() {
                continue;
            }
            // SSE のコメント行（": ping" 等）と data 以外のフィールドは読み飛ばす。
            let payload = match line.strip_prefix("data:") {
                Some(p) => p.trim(),
                None => continue,
            };
            if payload == "[DONE]" {
                done = true;
                break;
            }
            let v: Value = match serde_json::from_str(payload) {
                Ok(v) => v,
                // 壊れた1行で会話全体を落とさない。
                Err(_) => continue,
            };
            if let Some(u) = v.get("usage") {
                if !u.is_null() {
                    out.usage = Some(u.clone());
                }
            }

            // delta が本筋。ツールを使う構成では、最後の1件だけ message 形で返す
            // 実装もあるため、無ければ message を見る（どちらか一方だけを読む＝二重計上しない）。
            let Some(node) = v
                .get("choices")
                .and_then(|c| c.get(0))
                .and_then(|c| c.get("delta").or_else(|| c.get("message")))
            else {
                continue;
            };

            if v
                .get("choices")
                .and_then(|c| c.get(0))
                .and_then(|c| c.get("finish_reason"))
                .and_then(|f| f.as_str())
                == Some("length")
            {
                out.truncated = true;
            }
            if let Some(r) = node.get("reasoning_content").and_then(|c| c.as_str()) {
                if !r.is_empty() {
                    meter.tick(true);
                    let _ = app.emit("chat-think", json!({ "text": r }));
                }
            }
            if let Some(text) = node.get("content").and_then(|c| c.as_str()) {
                if !text.is_empty() {
                    // 本文と思考を分ける。out.text（次の観測に戻す履歴）にも
                    // **本文だけ**を積む＝思考を履歴に混ぜない。
                    for piece in split.push(text) {
                        route_piece(&piece, &mut out.text, &mut think_buf);
                        crate::llama::emit_piece(app, meter, piece);
                    }
                }
            }
            merge_tool_delta(&mut acc, node);
            if meter.should_report() {
                let _ = app.emit("chat-stat", meter.stat());
            }
        }
        if done {
            break;
        }
    }

    // id を持たない実装があるので、無ければこちらで振る（tool_call_id の突き合わせに要る）。
    out.calls = acc
        .into_iter()
        .filter(|(_, c)| !c.name.is_empty())
        .enumerate()
        .map(|(i, (idx, mut c))| {
            if c.id.is_empty() {
                c.id = format!("call_{round}_{idx}_{i}");
            }
            c
        })
        .collect();

    // ★ツールを呼んだラウンドの思考は本文でない。戻すと履歴に入って次に渡る。
    let allow_promote = !out.truncated && !out.stopped && out.calls.is_empty();
    for piece in split.finish(allow_promote) {
        route_piece(&piece, &mut out.text, &mut think_buf);
        crate::llama::emit_piece(app, meter, piece);
    }
    // ★経路A の対策。タグが一切無いまま「思考 → ツール呼び出し」となる形では、
    //   タグを見る仕組みは思考だと気づけない。だが**ツールを呼んだラウンドの本文は
    //   最終回答ではない**（最終回答はツールを呼ばないラウンドに出る）ので、
    //   ラウンドの終わりに構造で判断して思考欄へ落とす。
    //   ★履歴からも外す。残すと、モデルが**自分の思考を自分の前回の発言として**
    //   読み返し、同じことを言い直す（実機で英語の思考が3ブロック並んだ形）。
    //   このラウンドの実体はツール呼び出しとその結果で、そちらは履歴に残る。
    if !out.calls.is_empty() {
        if let Some(piece) = split.demote_answer() {
            route_piece(&piece, &mut out.text, &mut think_buf);
            crate::llama::emit_piece(app, meter, piece);
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// ツール実行
// ---------------------------------------------------------------------------

/// 引数から必須の文字列を取る。無ければ**理由を返す**（黙って空文字で続けない）。
fn need_str(args: &Value, key: &str) -> Result<String, String> {
    match args.get(key) {
        Some(Value::String(s)) => Ok(s.clone()),
        Some(other) => Err(format!(
            "引数 {key} は文字列である必要があります（受け取った形: {}）",
            type_name(other)
        )),
        None => Err(format!("引数 {key} が要ります")),
    }
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "真偽値",
        Value::Number(_) => "数値",
        Value::String(_) => "文字列",
        Value::Array(_) => "配列",
        Value::Object(_) => "オブジェクト",
    }
}

/// ツールの返り値を、モデルに読ませる文字列にする。
/// 文字列ならそのまま（ファイル本文を JSON エスケープすると読みにくく、無駄にトークンを食う）。
fn render_result(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => serde_json::to_string_pretty(other).unwrap_or_else(|_| other.to_string()),
    }
}

/// ツールを1つ実行する。`Ok` は成功、`Err` は拒否・失敗の理由。
/// **どちらもモデルへ返す**（失敗を黙らせると、モデルは同じ呼び出しを繰り返す）。
///
/// ★実体は workspace.rs の同名コマンドをそのまま呼ぶ。HTTP を経由しないので、
///   ここで新しい入口（＝新しい攻撃面）は増えない。
async fn run_tool(name: &str, args: &Value) -> Result<String, String> {
    match name {
        "ws_list" => {
            // rel は省略可。null と未指定を同じに扱う。
            let rel = match args.get("rel") {
                Some(Value::String(s)) => Some(s.clone()),
                Some(Value::Null) | None => None,
                Some(other) => {
                    return Err(format!(
                        "引数 rel は文字列である必要があります（受け取った形: {}）",
                        type_name(other)
                    ))
                }
            };
            let v = crate::workspace::ws_list(rel).await?;
            let v = serde_json::to_value(v).map_err(|e| format!("結果を読めません: {e}"))?;
            Ok(render_result(&v))
        }
        "ws_read" => {
            let rel = need_str(args, "rel")?;
            let v = crate::workspace::ws_read(rel).await?;
            let v = serde_json::to_value(v).map_err(|e| format!("結果を読めません: {e}"))?;
            Ok(render_result(&v))
        }
        "ws_edit" => {
            let rel = need_str(args, "rel")?;
            let old = need_str(args, "old_string")?;
            let new = need_str(args, "new_string")?;
            let occ = args.get("occurrence").and_then(|v| v.as_u64()).map(|n| n as usize);
            let all = args.get("replace_all").and_then(|v| v.as_bool());
            // 拒否理由（曖昧一致・構文破壊など）はそのままモデルに返す。黙らせると同じ編集を繰り返す。
            let v = crate::workspace::ws_edit(rel, old, new, all, occ).await?;
            let v = serde_json::to_value(v).map_err(|e| format!("結果を読めません: {e}"))?;
            Ok(render_result(&v))
        }
        "ws_write" => {
            let rel = need_str(args, "rel")?;
            let content = need_str(args, "content")?;
            let v = crate::workspace::ws_write(rel.clone(), content).await?;
            let v = serde_json::to_value(v).map_err(|e| format!("結果を読めません: {e}"))?;
            // 書き込みは「何も返さない」ことがある。空を成功と読み違えないよう、
            // 空なら書けた事実を明示して返す。
            let s = render_result(&v);
            if s.trim().is_empty() || s == "null" {
                Ok(format!("{rel} を書きました。"))
            } else {
                Ok(s)
            }
        }
        other => Err(format!(
            "そのようなツールはありません: {other}（使えるのは ws_list / ws_read / ws_edit / ws_write）"
        )),
    }
}

// ---------------------------------------------------------------------------
// 堂々巡りの検知
// ---------------------------------------------------------------------------

/// 直前と同じ呼び出しが何回続いたかを数える。
#[derive(Default)]
struct RepeatGuard {
    last: Option<String>,
    count: usize,
}

impl RepeatGuard {
    /// 連続回数を返す（初回・別物なら 1）。
    fn observe(&mut self, sig: String) -> usize {
        if self.last.as_deref() == Some(sig.as_str()) {
            self.count += 1;
        } else {
            self.count = 1;
            self.last = Some(sig);
        }
        self.count
    }
}

/// 呼び出しの同一性。引数は文字列のまま比べる（空白の差で別物にならない程度に trim する）。
fn call_signature(name: &str, args: &str) -> String {
    format!("{name}\u{1}{}", args.trim())
}

// ---------------------------------------------------------------------------
// 文字列の整形
// ---------------------------------------------------------------------------

/// 文字数（バイトではない）で切る。切ったかどうかも返す。
/// ★バイトで切ると日本語の途中で割れて panic する。
fn clamp_chars(s: &str, max: usize) -> (String, bool) {
    let mut out = String::new();
    let mut n = 0usize;
    for c in s.chars() {
        if n == max {
            return (out, true);
        }
        out.push(c);
        n += 1;
    }
    (out, false)
}

/// 画面に出す1行ぶんの抜粋。改行はつぶすが、全角空白などはそのまま残す
/// （`split_whitespace` で潰すと U+3000 まで消えて、日本語のパスや文が変形する）。
fn preview(s: &str) -> String {
    let flat: String = s
        .chars()
        .map(|c| if c == '\n' || c == '\r' || c == '\t' { ' ' } else { c })
        .collect();
    let (mut p, cut) = clamp_chars(flat.trim(), PREVIEW_CHARS);
    if cut {
        p.push('…');
    }
    p
}

/// 打ち切りなどの経緯を本文に混ぜて見せる。
/// ★エラーとして投げない: そこまでの答えを捨てずに、理由だけ足す。
fn notice(app: &AppHandle, text: &str) {
    // ★断り書きを本文チャンネル（chat-token）に流していた。すると画面の本文に混ざり、
    //   保存され、**次の送信でモデル自身の発言として送り返される**（レビュー・経路E）。
    //   ハーネスの言葉をモデルの言葉に混ぜない。llama.rs は同じ用途に chat-notice を
    //   使っており、そちらが正しい作法だった。
    let _ = app.emit("chat-notice", json!({ "message": text }));
}

// ---------------------------------------------------------------------------
// 本体
// ---------------------------------------------------------------------------

/// ツールを使える生成。
///
/// 失敗は**イベント `chat-error` で報せ、戻り値は Ok にする**（llama.rs の `chat_send` と同じ作法。
/// 戻り値でも投げると画面が二重に出す）。終端イベントは `chat-done` か `chat-error` の
/// **どちらか一方だけ**が必ず1回出る＝画面が待ちっぱなしにならない。
/// 1回のやり取りを記録に残す。
///
/// ★2026-08-11: journal.rs は書かれ、起動もし、コマンドも登録されていたのに、
///   **`journal::write` を呼ぶ場所が1つも無かった**（実機から1件も届かなかった原因）。
///   「書いたが繋いでいない」は今日4回目なので、呼び出しを試験で固定する。
fn record_turn(
    model: &str,
    engine: &str,
    prompt: &str,
    answer: &str,
    think: &str,
    tools: Vec<Value>,
    secs: f64,
    outcome: &str,
) {
    let mut t = crate::journal::Turn::default();
    t.client = json!({ "app_version": env!("CARGO_PKG_VERSION") });
    t.engine = json!({ "kind": engine, "model": model });
    t.input = json!({ "text": prompt });
    t.output = json!({ "text": answer });
    t.think = json!({ "text": think });
    t.tools = tools;
    t.cost = json!({ "secs": secs });
    t.stop = json!({ "outcome": outcome });
    crate::journal::write(t);
}

#[tauri::command]
pub async fn agent_send(
    app: AppHandle,
    model: String,
    messages: Vec<Value>,
    use_tools: bool,
    thinking: Option<bool>,
    // ★画面のモード（2026-09-21）。"chat" で Ask モード＝質問に答えるために調べる（2026-11-11 から読む道具も渡す・書く道具は渡さない）。
    toolset: Option<String>,
    session: Option<String>,
    // ★思考の深さ（"low"＝浅く・"high"＝深く・2026-10-11）。無ければ本体の既定（浅く）。
    effort: Option<String>,
    // ★作業のモード（2026-10-15 オーナー「解析・開発・3D モデリングで分けましょう」）。
    mode: Option<String>,
    // ★依頼を出した画面（2026-10-30・全端末で 1 つの会話の一覧）。無ければ "app"。リモート（chat.send）は "android"・"web"。
    source: Option<String>,
    // ★この会話の宛先の種類（"lan" / "local" / "claude"・2026-10-30 会話ごとのモデル）。無ければいつもの宛先。
    engine: Option<String>,
    // ★走行の器の札（会話・2026-10-30）。渡すと会話ごとに同時に走り、知らせに同じ札が付く。
    lane: Option<String>,
    // ★ほかの機械（中継の名札）で走らせる（2026-10-30）。session がその機械の会話の名札。
    remote: Option<String>,
    // ★依頼を送らず、ほかの機械で走っている会話（名札）を追う（2026-10-31 Remote Control）。remote と一緒に渡す。
    follow: Option<String>,
) -> Result<(), String> {
    let lane = lane.map(|l| l.trim().to_string()).filter(|l| !l.is_empty() && l.len() <= 64);
    let out = crate::sidecar::LaneOut { app: &app, lane: lane.as_deref() };
    use crate::sidecar::Out as _;
    let backend = match engine.as_deref() {
        Some(k) => crate::sidecar::backend_for(k).await,
        None => None,
    };
    // ★router の一覧に合わせた ID にしてから使う（llama.rs と同じ理由）。★宛先を会話で替えたとき（Claude・LAN）は
    //   この PC の推論機の一覧で読み替えない（Claude のモデル名を手元のモデルの名前に取り違えない）。
    let model = if backend.is_some() && engine.as_deref() != Some("local") {
        model
    } else {
        crate::llama::resolve_model_id(&model).await
    };
    let thinking = thinking.unwrap_or(true);
    let effort_now = effort.clone();
    crate::sampling::set_effort(effort);

    // ★2026-08-11 オーナー指示「完全にアプリに本体を乗せていい。中途半端では使えない」。
    //   道具を使う会話は**本体だけ**を通す。アプリ内蔵の実装（道具5個）は、
    //   本体が起動できないときに**そうと分かる形で**動く控えに退く。
    //   黙って能力の低い側へ落ちると、同じ質問の答えが日によって変わり、
    //   何が原因か誰にも分からなくなる。
    if use_tools {
        // ★会話モード（toolset="chat"）は**作業場を要らない**（2026-09-21）。
        //   ★2026-11-11 から読む道具（Read・Grep・Glob・見るだけの Bash）も渡すが、書く道具は渡さない（本体の engine/tools）。
        //   作業場が未設定でも会話できるべきで、ここで弾くと「未設定だから話せない」
        //   という、利用者から見て理由の分からない拒否になる。
        //   ★本体側は作業場の文字列を受け取る作りなので、**在るなら渡し、
        //     無ければ今のフォルダ**を渡す（書く道具が無いので変わらない。読むのはそこになる）。
        // ★チャットモード（toolset="talk"・2026-11-23）は選んでいる作業フォルダーを使わず、データ根の下の chat/ を渡す。
        //   判定は blackwall-core/src/engine_ws.rs（ホストで試験が走る）。ほかの機械で走る会話はデータ根。
        let ws = engine_ws::engine_workspace(toolset.as_deref(), crate::workspace::current_root(), remote.is_some(),
                                             &crate::app_root(), std::env::current_dir().ok());
        match (crate::sidecar::core_available().await, ws) {
            (true, Ok(ws)) => {
                match crate::sidecar::core_send(&app, model.clone(), messages.clone(), ws,
                                                thinking, toolset.clone(), session.clone(),
                                                mode.clone(), source.clone(), backend.clone(),
                                                lane.clone(), Some(effort_now.clone()), remote.clone(), follow.clone()).await {
                    Ok(()) => return Ok(()),
                    // ★**忙しいときは控えへ落ちない**（2026-08-22）。
                    //   忙しい（前の依頼が終わっていない）のと、壊れている
                    //   （本体が起動できない）のは別のことなのに、同じ扱いで
                    //   道具5個の控えへ黙って落ちていた。
                    //   実機で退行した答えを返した走行がこれだった——利用者は
                    //   22個の道具で動いているつもりで、5個の別物と話していた。
                    //   作業の上限を撤去したので走行は自分では終わらない＝
                    //   この経路は今後もっと踏まれる。**待つか、止めるか**を言う。
                    Err(msg) if msg.starts_with("BUSY:") => {
                        out.out(
                            "chat-notice",
                            json!({ "message":
                                "前の依頼がまだ終わっていません。終わるまで待つか、\
                                 「止める」を押してください（待っている依頼もまとめて取り消します）。\
                                 ★作業の上限は外してあるので、放っておいても終わりません。" }),
                        );
                        return Ok(());
                    }
                    Err(msg) if remote.is_some() => {
                        // ほかの機械へ繋がらない（中継・その機械のゲートウェイ）。控えはこの PC の物なので落ちない。
                        out.out("chat-error", json!({ "message": format!("ほかの機械で続けられませんでした（{msg}）") }));
                        return Ok(());
                    }
                    Err(msg) => {
                        out.out(
                            "chat-notice",
                            json!({ "message": format!("本体が応答しませんでした（{msg}）。控えの道具で続けます。") }),
                        );
                    }
                }
            }
            (false, _) => {
                out.out(
                    "chat-notice",
                    json!({ "message": format!(
                        "本体を起動できません（{}）。控えの道具 {} 個で動きます。設定→自己診断で詳しく分かります。",
                        crate::sidecar::last_unavailable_reason().unwrap_or_else(|| "理由不明".into()),
                        tool_names().len()
                    ) }),
                );
            }
            (true, Err(e)) => {
                out.out(
                    "chat-notice",
                    json!({ "message": format!("作業ディレクトリが決まっていません（{e}）。左上から選んでください。") }),
                );
            }
        }
    }

    // ★札つき（会話ごとに同時に走る）の依頼は、ほかの会話が走っている間は控えの内蔵の道具へ落とさない
    //   （内蔵の控えは止める・道具の状態が 1 つしか無く、知らせに札も付かない＝ほかの会話の走行と混ざる）。
    //   ほかに走っていなければ今までどおり控えで動く（画面は札の無い知らせを、その会話の物として拾う）。
    if remote.is_some() {
        out.out("chat-error", json!({ "message": "ほかの機械の会話は、この PC の本体（Python）が動かないと続けられません。" }));
        return Ok(());
    }
    if lane.is_some() && !crate::sidecar::lanes_running().is_empty() {
        out.out("chat-error", json!({ "message": "本体が使えず、ほかの会話が走っているので、この会話は今は走らせられません。" }));
        return Ok(());
    }
    if let Err(msg) = run_agent(&app, model, messages, use_tools, thinking).await {
        out.out("chat-error", json!({ "message": msg }));
    }
    Ok(())
}

/// 履歴（次の観測に戻す本文）へ、断片を振り分ける。
/// ★思考は履歴に混ぜない。ただし Promote＝思考ではなかったと分かった場合は戻す
///   （戻さないと本文が空のまま次のラウンドに入り、同じ生成を繰り返す）。
fn route_piece(piece: &crate::think::Piece, text: &mut String, think: &mut String) {
    use crate::think::Piece;
    match piece {
        Piece::Answer(t) => text.push_str(t),
        Piece::Think(t) => think.push_str(t),
        Piece::Reclassify(m) => {
            if let Some(rest) = text.strip_suffix(m.as_str()) {
                *text = rest.to_string();
            }
            think.push_str(m);
        }
        Piece::Promote(m) => {
            if let Some(rest) = think.strip_suffix(m.as_str()) {
                *think = rest.to_string();
            }
            text.push_str(m);
        }
    }
}

/// 終了時にまとめて渡す数値。画面の「消費」表示はこれを読む。
fn done_payload(meter: &crate::think::Meter, usage: &Option<Value>) -> Value {
    let mut v = meter.stat();
    if let Some(o) = v.as_object_mut() {
        o.insert("usage".into(), usage.clone().unwrap_or(Value::Null));
        o.insert(
            "prompt_tokens".into(),
            json!(usage.as_ref().and_then(|u| u.get("prompt_tokens").and_then(|p| p.as_u64()))),
        );
    }
    v
}

async fn run_agent(
    app: &AppHandle,
    model: String,
    messages: Vec<Value>,
    use_tools: bool,
    thinking: bool,
) -> Result<(), String> {
    let base = base_url().await;
    let tools = if use_tools { Some(tools_schema()) } else { None };

    // system プロンプトは画面側が組み立てて渡す。ここでは足さない
    // （2箇所で足すと、画面に出ていない指示が紛れて、挙動の説明がつかなくなる）。
    let mut convo = messages;
    let mut guard = RepeatGuard::default();
    let mut usage: Option<Value> = None;
    // 計測はエージェント全体で1つ。ツールを何度呼んでも合計が出るようにする。
    let mut meter = crate::think::Meter::default();
    let gen = crate::cancel::begin();
    // ★控えの走行も journal に残す（record_turn は書かれたまま呼ばれていなかった・2026-10-19）。
    //   控えに落ちた日の会話だけ記録が欠けると、「控えで何が起きたか」を後から確かめられない。
    let started = std::time::Instant::now();
    let prompt = last_user_text(&convo);
    let mut answer = String::new();
    let mut tool_log: Vec<Value> = Vec::new();
    macro_rules! finish {
        ($outcome:expr) => {{
            let _ = app.emit("chat-done", done_payload(&meter, &usage));
            record_turn(&model, "builtin", &prompt, &answer, "", tool_log.clone(),
                        started.elapsed().as_secs_f64(), $outcome);
            return Ok(());
        }};
    }

    for round in 1..=MAX_ROUNDS {
        if crate::cancel::is_stopped(gen) {
            let _ = app.emit("chat-notice", json!({ "message": "止めました。" }));
            break;
        }
        let out = stream_round(app, &base, &model, &convo, tools.as_ref(), round, &mut meter, thinking).await?;
        if out.stopped {
            let _ = app.emit("chat-notice", json!({ "message": "止めました。" }));
            answer.push_str(&out.text);
            finish!("stopped");
        }
        if out.usage.is_some() {
            usage = out.usage.clone();
        }

        // ツールを渡していないのに tool_calls が来たら無視する（テンプレート由来の事故対策）。
        answer.push_str(&out.text);
        if !use_tools || out.calls.is_empty() {
            finish!("done");
        }

        if round == MAX_ROUNDS {
            notice(
                app,
                &format!("ツールの往復が上限（{MAX_ROUNDS} 回）に達したので、ここで止めました。答えは途中です。"),
            );
            break;
        }

        // モデルの手番を履歴に戻す。content は null にせず必ず文字列で置く
        // （テンプレートによっては null を "None" と描いてしまう）。
        let tool_calls: Vec<Value> = out
            .calls
            .iter()
            .map(|c| {
                json!({
                    "id": c.id,
                    "type": "function",
                    "function": { "name": c.name, "arguments": c.args }
                })
            })
            .collect();
        convo.push(json!({
            "role": "assistant",
            "content": out.text,
            "tool_calls": tool_calls,
        }));

        for call in &out.calls {
            // ★堂々巡りの検知は実行の前。同じ呼び出しを3回目に及んで実行しても、
            //   結果は同じで文脈を食うだけ。
            if guard.observe(call_signature(&call.name, &call.args)) >= REPEAT_LIMIT {
                notice(
                    app,
                    &format!(
                        "同じ呼び出し（{}）が {REPEAT_LIMIT} 回続いたので、堂々巡りとみなして止めました。答えは途中です。",
                        call.name
                    ),
                );
                finish!("repeat_guard");
            }

            // 引数は JSON 文字列で届く。壊れていたら、それ自体を結果として返して直させる。
            let parsed: Value = if call.args.trim().is_empty() {
                json!({})
            } else {
                serde_json::from_str(&call.args).unwrap_or(Value::Null)
            };

            let result = if parsed.is_null() {
                Err(format!(
                    "引数を JSON として読めません: {}",
                    preview(&call.args)
                ))
            } else if !parsed.is_object() {
                Err(format!(
                    "引数はオブジェクトである必要があります（受け取った形: {}）",
                    type_name(&parsed)
                ))
            } else {
                run_tool(&call.name, &parsed).await
            };

            let (ok, body) = match result {
                Ok(s) => (true, s),
                // 拒否・失敗はモデルにも見せる。理由が分かれば別の手を打てる。
                Err(e) => (false, format!("エラー: {e}")),
            };

            // 何が起きたかを画面へ。ツールが黙って走らないこと自体が要件。
            let _ = app.emit(
                "agent-tool",
                json!({
                    "name": call.name,
                    "args": if parsed.is_object() { parsed.clone() } else { json!({ "raw": call.args }) },
                    "result_preview": preview(&body),
                    "ok": ok,
                }),
            );

            tool_log.push(json!({ "name": call.name, "ok": ok, "result_preview": preview(&body) }));
            let (mut content, cut) = clamp_chars(&body, TOOL_RESULT_MAX_CHARS);
            if cut {
                // 黙って切らない。モデルが「全部読んだ」前提で答えるのを防ぐ。
                content.push_str("\n…（ここで切りました。続きは範囲を指定して読み直してください）");
            }
            convo.push(json!({
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": content,
            }));
        }
    }

    let outcome = if crate::cancel::is_stopped(gen) { "stopped" } else { "max_rounds" };
    finish!(outcome);
}

/// 履歴の最後の利用者発言の本文（content が配列＝画像つきのときは text の部分だけ）。
fn last_user_text(convo: &[Value]) -> String {
    let Some(m) = convo.iter().rev().find(|m| m.get("role").and_then(|r| r.as_str()) == Some("user")) else {
        return String::new();
    };
    match m.get("content") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

// ---------------------------------------------------------------------------
// テスト（純粋関数のみ。推論器も workspace も要らない＝どの OS でも走る）
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_count_is_four() {
        // ★規律をテストで固定する。増やすときは、この数字を変える理由を書くこと。
        //
        // 3 → 4（2026-08-11）: ws_edit を足した。理由は「増やしたかった」ではなく、
        // ws_write だけだと**既存ファイルの変更が全文差し替えになる**ためで、これは
        // 中身を失う既知の壊し方（設計で採らないと決めた形）。部分編集の道を用意しない限り
        // 安全機構（曖昧一致の拒否・構文ゲート）を通せない。ツール数は精度に効くので、
        // これ以上は増やさない。
        assert_eq!(tools_schema().as_array().map(|a| a.len()), Some(4));
    }

    #[test]
    fn fragmented_arguments_are_joined() {
        let mut acc = BTreeMap::new();
        merge_tool_delta(
            &mut acc,
            &json!({"tool_calls":[{"index":0,"id":"c1","function":{"name":"ws_read","arguments":"{\"rel\":"}}]}),
        );
        merge_tool_delta(
            &mut acc,
            &json!({"tool_calls":[{"index":0,"function":{"arguments":"\"a.txt\"}"}}]}),
        );
        let c = &acc[&0];
        assert_eq!(c.id, "c1");
        assert_eq!(c.name, "ws_read");
        assert_eq!(c.args, "{\"rel\":\"a.txt\"}");
    }

    #[test]
    fn repeated_full_name_is_not_doubled() {
        // 名前を毎チャンク送ってくる実装でも "ws_listws_list" にならない。
        let mut acc = BTreeMap::new();
        let d = json!({"tool_calls":[{"index":0,"function":{"name":"ws_list","arguments":""}}]});
        merge_tool_delta(&mut acc, &d);
        merge_tool_delta(&mut acc, &d);
        assert_eq!(acc[&0].name, "ws_list");
    }

    #[test]
    fn object_arguments_replace_instead_of_append() {
        let mut acc = BTreeMap::new();
        merge_tool_delta(
            &mut acc,
            &json!({"tool_calls":[{"index":0,"function":{"name":"ws_read","arguments":{"rel":"a.txt"}}}]}),
        );
        assert_eq!(
            serde_json::from_str::<Value>(&acc[&0].args).unwrap(),
            json!({"rel":"a.txt"})
        );
    }

    #[test]
    fn parallel_calls_keep_separate_slots() {
        let mut acc = BTreeMap::new();
        merge_tool_delta(
            &mut acc,
            &json!({"tool_calls":[
                {"index":0,"id":"a","function":{"name":"ws_read","arguments":"{}"}},
                {"index":1,"id":"b","function":{"name":"ws_list","arguments":"{}"}}
            ]}),
        );
        assert_eq!(acc.len(), 2);
        assert_eq!(acc[&1].name, "ws_list");
    }

    #[test]
    fn tools_are_absent_when_not_requested() {
        let body = build_body("m", &[], None, true);
        assert!(body.get("tools").is_none());
        assert_eq!(body["stream"], json!(true));
        let body = build_body("m", &[], Some(&tools_schema()), true);
        assert_eq!(body["tools"].as_array().map(|a| a.len()), Some(3));
        // tool_choice は付けない（使うかどうかはモデルに委ねる）。
        assert!(body.get("tool_choice").is_none());
    }

    #[test]
    fn repeat_guard_counts_consecutive_only() {
        let mut g = RepeatGuard::default();
        let a = call_signature("ws_read", " {\"rel\":\"x\"} ");
        let b = call_signature("ws_read", "{\"rel\":\"y\"}");
        assert_eq!(g.observe(a.clone()), 1);
        // 前後の空白は同一視する。
        assert_eq!(g.observe(call_signature("ws_read", "{\"rel\":\"x\"}")), 2);
        assert_eq!(g.observe(b), 1); // 別物で仕切り直し
        assert_eq!(g.observe(a.clone()), 1);
        assert_eq!(g.observe(a), 2);
    }

    #[test]
    fn clamp_is_char_safe_for_japanese() {
        let (s, cut) = clamp_chars("作業ディレクトリ", 4);
        assert_eq!((s.as_str(), cut), ("作業ディ", true));
        let (s, cut) = clamp_chars("短い", 10);
        assert_eq!((s.as_str(), cut), ("短い", false));
    }

    #[test]
    fn preview_flattens_newlines_but_keeps_ideographic_space() {
        // 実機のユーザー名 C:\Users\田中　優輝 に全角空白が入る。潰さないこと。
        let p = preview("田中　優輝\nの\tメモ");
        assert_eq!(p, "田中　優輝 の メモ");
    }

    #[test]
    fn missing_argument_is_reported_not_defaulted() {
        let e = need_str(&json!({}), "rel").unwrap_err();
        assert!(e.contains("rel"));
        let e = need_str(&json!({"rel": 3}), "rel").unwrap_err();
        assert!(e.contains("文字列"));
    }

    #[test]
    fn string_results_are_not_json_escaped() {
        assert_eq!(render_result(&json!("a\nb")), "a\nb");
        assert!(render_result(&json!({"x":1})).contains("\"x\""));
    }
}
