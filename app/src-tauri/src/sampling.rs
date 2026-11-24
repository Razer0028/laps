//! 生成の設定と、繰り返しの打ち切り。
//!
//! ★2026-08-11 実機で「内部思考でずっと同じことをつぶやく」状態になった。
//! 調べたら**生成パラメータを1つも送っていなかった**（temperature も上限も無し）。
//! Qwen3 は公式に「greedy decoding は使うな。性能低下と**終わらない繰り返し**を招く」
//! と明記されているモデルで、しかも `max_tokens` が無いので、一度ループに入ると
//! 文脈が埋まるまで止まらない。既定任せにしていた私の手落ち。

use serde_json::{json, Value};

/// Qwen3 の thinking モードで推奨されている値。
/// `presence_penalty` は繰り返し対策だが、**上げすぎると言語が混ざる**
/// （実機で思考に中国語が出ている＝この傾向が既にある）ので控えめに置く。
pub fn params() -> Value {
    json!({
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.5,
        // 上限。これが無いと、繰り返しに入った生成が文脈を食い尽くすまで続く。
        // ★4096 では長いファイル（HTML 一式など）を書き切れず、モデルが勝手に
        //   途中で切って「字数制限を超えたので、続きを表示する」と書いた（実機）。
        //   暴走は LoopGuard と中止ボタンで止められるので、上限は緩める。
        "max_tokens": 16384,
    })
}

/// 思考を使うかどうかを、**モデル自身のテンプレートの仕組み**で指定する。
///
/// ★2026-08-11 に GGUF の tokenizer.chat_template を読んで分かったこと:
///   このテンプレートは `enable_thinking` が **false のときだけ** `<think>\n\n</think>\n\n`
///   をプロンプトに書き込む（＝思考を閉じた状態で始めさせて、思考させない）。
///   true や未指定のときは何も足さない。つまり「思考が開いた状態で始まる」わけではなく、
///   モデルが自分で `<think>` を出すのが本来の形。私はこれを逆に決めつけていた。
///
/// llama.cpp は `chat_template_kwargs` をテンプレートへ渡す（このビルドで対応を確認済み）。
pub fn set_thinking(body: &mut Value, enabled: bool) {
    if let Some(obj) = body.as_object_mut() {
        let mut kw = json!({ "enable_thinking": enabled });
        // ★思考の深さ（画面の Think の「浅く／深く」・2026-10-11）。思考させるときだけ書式へ渡す
        //   （Qwen3.8 の書式は low / xhigh で前置きに指示を足す。知らない書式は黙って無視する）。
        if enabled {
            if let Some(e) = effort() {
                kw["reasoning_effort"] = json!(if e == "high" { "xhigh" } else { "low" });
            }
        }
        obj.insert("chat_template_kwargs".into(), kw);
    }
}

/// この依頼の思考の深さ（"low" / "high"）。画面から 1 依頼ずつ送るので、送る入口（agent_send・chat_send）で
/// 先に入れ、本体への依頼（sidecar の "effort"）と推論機への要求（set_thinking）で読む。
static EFFORT: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

pub fn set_effort(e: Option<String>) {
    let v = e.filter(|s| s == "low" || s == "high");
    if let Ok(mut g) = EFFORT.lock() {
        *g = v;
    }
}

pub fn effort() -> Option<String> {
    EFFORT.lock().ok().and_then(|g| g.clone())
}

/// 生成の本文へ設定を混ぜる。既に指定がある鍵は上書きしない。
pub fn apply(body: &mut Value) {
    let Some(obj) = body.as_object_mut() else { return };
    if let Some(p) = params().as_object() {
        for (k, v) in p {
            obj.entry(k.clone()).or_insert(v.clone());
        }
    }
}

/// 同じ内容を繰り返し始めたことを見つける。
///
/// 設定だけでは消えない（abliterated は判断が予測不能になる、というのが本プロジェクトの
/// 研究知見）ので、**外側でも止める**。プロンプトに書いた規則は境界にならない、と同じ考え方。
pub struct LoopGuard {
    buf: String,
    checked_at: usize,
    /// 打ち切りの理由。見つかっていなければ None。
    pub verdict: Option<String>,
}

/// 直近この**文字数**を「いま言っていること」とみなす。
/// ★バイト数で測ってはいけない。日本語は1文字3バイトなので 60 バイト＝約20文字となり、
///   「テストを走らせます。」のような**普通の語尾**が一致して誤爆する（実際に踏んだ）。
const WINDOW_CHARS: usize = 100;
/// 遡って探す範囲。長すぎると重く、短すぎると周期の長いループを見逃す。
const LOOKBACK: usize = 6000;
/// 同じ窓がこの回数出たらループとみなす。
const HITS: usize = 4;
/// 検査の間隔（文字）。毎回やると重い。
const EVERY: usize = 300;

impl Default for LoopGuard {
    fn default() -> Self {
        Self { buf: String::new(), checked_at: 0, verdict: None }
    }
}

impl LoopGuard {
    /// 受け取った断片を足し、ループに入っていれば真を返す。
    pub fn push(&mut self, s: &str) -> bool {
        if self.verdict.is_some() {
            return true;
        }
        self.buf.push_str(s);
        if self.buf.len() > LOOKBACK {
            let cut = floor_boundary(&self.buf, self.buf.len() - LOOKBACK);
            self.buf.drain(..cut);
            self.checked_at = self.checked_at.saturating_sub(cut);
        }
        if self.buf.len() < self.checked_at + EVERY || self.buf.chars().count() < WINDOW_CHARS * 2 {
            return false;
        }
        self.checked_at = self.buf.len();
        if let Some(n) = self.repeat_count() {
            self.verdict = Some(format!("同じ内容を {n} 回繰り返しました"));
            return true;
        }
        false
    }

    /// 直近の窓が、遡った範囲に何回出てくるか。
    fn repeat_count(&self) -> Option<usize> {
        let start = self
            .buf
            .char_indices()
            .rev()
            .nth(WINDOW_CHARS - 1)
            .map(|(i, _)| i)
            .unwrap_or(0);
        let needle = &self.buf[start..];
        if needle.trim().is_empty() {
            return None;
        }
        let mut n = 0;
        let mut from = 0;
        while let Some(i) = self.buf[from..].find(needle) {
            n += 1;
            from += i + needle.len().max(1);
            if from >= self.buf.len() {
                break;
            }
        }
        if n >= HITS {
            Some(n)
        } else {
            None
        }
    }
}

/// `i` 以下で最も近い文字境界。多バイト文字の途中で切らないため。
fn floor_boundary(s: &str, mut i: usize) -> usize {
    if i >= s.len() {
        return s.len();
    }
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_settings_are_actually_sent() {
        let mut body = json!({"model": "m", "stream": true});
        apply(&mut body);
        // ★これが無いまま出荷して、実機で繰り返しループになった。
        assert_eq!(body["temperature"], json!(0.6));
        assert!(body["max_tokens"].as_u64().unwrap() > 0, "上限が無いと止まらない");
        assert_eq!(body["stream"], json!(true), "既にある指定は壊さない");
    }

    #[test]
    fn thinking_can_be_turned_off_through_the_template() {
        let mut body = json!({"model": "m"});
        set_thinking(&mut body, false);
        assert_eq!(body["chat_template_kwargs"]["enable_thinking"], json!(false));
        set_thinking(&mut body, true);
        assert_eq!(body["chat_template_kwargs"]["enable_thinking"], json!(true));
    }

    #[test]
    fn an_explicit_value_wins_over_the_default() {
        let mut body = json!({"max_tokens": 16});
        apply(&mut body);
        assert_eq!(body["max_tokens"], json!(16), "呼び出し側の指定を上書きしない");
    }

    #[test]
    fn ordinary_text_does_not_trip_the_guard() {
        // 中身の違う文を並べる。★同じ文を並べる試験にしてはいけない
        //   （それ自体がループなので、検知できたのに「誤爆」と読んでしまう）。
        let mut g = LoopGuard::default();
        for i in 0..200 {
            let line = format!(
                "{i} 番目の手順です。ファイル src/mod_{i}.rs を読み、{i} 行目の条件を直し、テストを走らせます。\n"
            );
            assert!(!g.push(&line), "普通の文で誤爆した（{i} 行目）");
        }
        assert!(g.verdict.is_none());
    }

    #[test]
    fn repeated_code_shape_does_not_trip_the_guard() {
        // 似た形の行が続くコードは正常。窓が完全一致しなければ止めない。
        let mut g = LoopGuard::default();
        for i in 0..120 {
            let line = format!("    assert_eq!(parse(\"case_{i}\").unwrap().len(), {i});\n");
            assert!(!g.push(&line), "コードで誤爆した（{i} 行目）");
        }
    }

    #[test]
    fn a_thinking_loop_is_caught() {
        let mut g = LoopGuard::default();
        let mut tripped = false;
        for _ in 0..60 {
            if g.push("待って、もう一度確認します。ユーザーは何を求めているのでしょうか。") {
                tripped = true;
                break;
            }
        }
        assert!(tripped, "同じつぶやきの繰り返しを止められない");
        assert!(g.verdict.unwrap().contains("繰り返"));
    }

    #[test]
    fn multibyte_input_does_not_panic() {
        let mut g = LoopGuard::default();
        for i in 0..400 {
            let _ = g.push(&format!("日本語のテキスト{i}。"));
        }
    }
}
