//! 思考ブロックの分離と、生成量の計測。
//!
//! ★2026-08-11 実機で、本文に `</think>` と思考の断片（「笔者」）がそのまま出た。
//! 原因は Qwen3 系のテンプレートが**プロンプトの末尾に `<think>` を入れる**こと。
//! モデルの出力は**閉じタグから始まる**ので、「開きタグを探して思考に入る」実装では
//! 永久に思考だと気づけず、思考がそのまま答えとして流れる。
//!
//! ここでは開きタグの無い閉じタグ（孤児）を「ここまでは全部思考だった」と解釈し、
//! 画面に遡って移させる（`Reclassify`）。一度その形を見たら以後は最初から思考として
//! 扱うので、2回目からは遡りが起きない（同じモデルなら毎回同じ形になる）。
//!
//! タグは chunk の境界で割れる（`</th` + `ink>`）。**保留（hold）**を持つのはそのため。

use std::sync::atomic::{AtomicBool, Ordering};

/// このモデルは「思考が開いた状態」で始まる、と分かったかどうか。
/// 一度分かれば会話をまたいで覚えておく（毎回1回目だけ遡るのを避ける）。
static FORCED_OPEN: AtomicBool = AtomicBool::new(false);

pub fn forced_open() -> bool {
    FORCED_OPEN.load(Ordering::Relaxed)
}
fn remember_forced_open() {
    FORCED_OPEN.store(true, Ordering::Relaxed);
}

#[derive(Debug, PartialEq, Eq)]
pub enum Piece {
    /// 画面に出す本文
    Answer(String),
    /// 思考欄に出す文字列
    Think(String),
    /// 本文として出した**この文字列**は、実は思考だった。思考欄へ移す。
    ///
    /// ★文字列を持たせる理由: 「移せ」とだけ伝えると、受け側は**どこまで移すか**を
    /// 知らない。画面と履歴は複数ラウンドぶんを繋げて持っているので、
    /// **前のラウンドの本文まで持って行かれる**（レビューで再現・経路D）。
    Reclassify(String),
    /// 逆。思考として出した**この文字列**を本文へ戻す。
    /// 同じ理由で文字列を持つ（経路B: 最後の救済が全ラウンドの思考を落とした）。
    Promote(String),
}

const OPEN: &str = "<think>";
const CLOSE: &str = "</think>";

pub struct Splitter {
    /// この Splitter が本文として出した分（＝このラウンドぶん）。移す量の根拠。
    answer_emitted: String,
    /// 同、思考として出した分。
    think_emitted: String,
    in_think: bool,
    /// 閉じタグを一度でも見たか。★見ないまま終わったら、思考だという前提が
    /// 外れている可能性がある（`FORCED_OPEN` はモデル全体で覚えるので、
    /// 思考しない返答が1つ混ざると**答えを丸ごと思考欄に飲み込む**）。
    saw_close: bool,
    hold: String,
    /// 本文をまだ1文字も出していない間は、先頭の改行を落とす（`</think>\n\n本文` 対策）
    answer_started: bool,
}

impl Default for Splitter {
    fn default() -> Self {
        Self::new(forced_open())
    }
}

impl Splitter {
    pub fn new(start_in_think: bool) -> Self {
        Self {
            answer_emitted: String::new(),
            think_emitted: String::new(),
            in_think: start_in_think,
            saw_close: false,
            hold: String::new(),
            answer_started: false,
        }
    }

    /// 受け取った差分を、本文と思考に振り分ける。
    pub fn push(&mut self, s: &str) -> Vec<Piece> {
        self.hold.push_str(s);
        let mut out = Vec::new();
        loop {
            if self.in_think {
                // ★**重複した開きタグを捨てる**（2026-09-02・試験を初めて走らせて判明）。
                //   `start_in_think=true`（前の手番で「開きタグ無しで思考する」と
                //   学習した場合）のまま `<think>` が来ると、旧実装はそのタグ自体を
                //   **思考の中身として**出していた＝画面の思考欄に生タグが出る。
                //   `the_rescue_does_not_fire_when_there_is_already_an_answer` が
                //   これを咎めていたが、**この製品では Rust の試験 195 本が一度も
                //   実行されたことがなく**（ホストに GTK 依存が無く `cargo test` が
                //   ビルドできない）、少なくとも 2026-08-16 から赤いまま気づかれなかった。
                if self.hold.starts_with(OPEN) {
                    self.hold = self.hold[OPEN.len()..].to_string();
                    continue;
                }
                match self.hold.find(CLOSE) {
                    Some(i) => {
                        let pre = self.hold[..i].to_string();
                        if !pre.is_empty() {
                            self.note_think(&pre);
                            out.push(Piece::Think(pre));
                        }
                        self.hold = self.hold[i + CLOSE.len()..].to_string();
                        self.in_think = false;
                        self.saw_close = true;
                    }
                    None => break,
                }
            } else {
                // 本文の途中では、開きタグと**孤児の閉じタグ**の早い方を探す。
                let o = self.hold.find(OPEN);
                let c = self.hold.find(CLOSE);
                match (o, c) {
                    (Some(i), _) if c.map_or(true, |j| i < j) => {
                        let pre = self.hold[..i].to_string();
                        if !pre.is_empty() {
                            out.push(self.answer(pre));
                        }
                        self.hold = self.hold[i + OPEN.len()..].to_string();
                        self.in_think = true;
                    }
                    (_, Some(j)) => {
                        // 孤児。開いていないのに閉じた＝**最初から思考だった**。
                        remember_forced_open();
                        if !self.answer_emitted.is_empty() {
                            let moved = std::mem::take(&mut self.answer_emitted);
                            self.think_emitted.push_str(&moved);
                            out.push(Piece::Reclassify(moved));
                        }
                        self.answer_started = false;
                        let pre = self.hold[..j].to_string();
                        if !pre.is_empty() {
                            self.note_think(&pre);
                            out.push(Piece::Think(pre));
                        }
                        self.hold = self.hold[j + CLOSE.len()..].to_string();
                        self.in_think = false;
                        self.saw_close = true;
                    }
                    _ => break,
                }
            }
        }
        // タグの途中で切れている分だけ残し、確定した分を出す。
        let keep = partial_tail(&self.hold);
        if keep < self.hold.len() {
            let ready: String = self.hold[..self.hold.len() - keep].to_string();
            self.hold = self.hold[self.hold.len() - keep..].to_string();
            if !ready.is_empty() {
                out.push(if self.in_think {
                    self.note_think(&ready);
                    Piece::Think(ready)
                } else {
                    self.answer(ready)
                });
            }
        }
        out.into_iter().filter(|p| !is_empty_piece(p)).collect()
    }

    /// 打ち切り。保留していた分（タグになりかけて、ならなかったもの）を吐く。
    ///
    /// `allow_promote` = 思考欄に入れた分を本文へ戻してよいか。
    ///
    /// 閉じタグを見ないまま終わる形は**複数あり、扱いが違う**:
    ///   A. モデルがそもそも思考しなかった → 思考欄の中身は本文。戻す（allow_promote=true）
    ///   B. 思考の途中で上限に当たった     → それは思考。戻さない
    ///   C. **思考のあとツールを呼んだ**   → それは思考。戻さない
    ///
    /// ★2026-08-11、B と C を A として扱って**思考が回答に混ざった**。
    ///   とくに C は害が二重で、戻した思考が**履歴に入って次のラウンドに渡る**ため、
    ///   モデルが自分の思考を「前回の自分の発言」として読み、同じことを繰り返す
    ///   （実機で英語の思考が3回ほぼ同じ内容で並んだのはこれ）。
    ///
    /// 戻すのは「本文も無く、ツールも呼んでいない＝画面が空になる」場合だけに絞る。
    pub fn finish(&mut self, allow_promote: bool) -> Vec<Piece> {
        let rest = std::mem::take(&mut self.hold);
        let mut out = Vec::new();
        if !rest.is_empty() {
            out.push(if self.in_think {
                self.note_think(&rest);
                Piece::Think(rest)
            } else {
                self.answer(rest)
            });
        }
        // ★このラウンドで思考として出した分だけを戻す。空なら何もしない
        //   （content が一度も来ていないのに救済が飛ぶ、というのが経路C）。
        //
        // ★2026-08-16 に条件をひとつ緩めた。実機40件のうち**8件が本文ゼロ**で、
        //   そのうち4件は**思考の末尾が完成した最終報告**だった
        //   （「**Note**: 本報告では…」で終わる等）。つまり実際に起きていたのは
        //
        //     D. **閉じタグは出した。そのあと本文を1文字も書かなかった**
        //
        //   という形で、`!self.saw_close` の条件に当たらず救済されなかった。
        //   利用者から見れば「推論はしているのに何も表示されない」。
        //   ★**空白を見せるより、思考を見せるほうが必ずまし。**
        //
        //   B（思考の途中で上限）と C（思考のあとツール）は従来どおり守られる:
        //   どちらも `allow_promote=false` で呼ばれる経路だからで、
        //   ここで見ているのは「自然に終わった」ときだけ。
        if allow_promote && !self.answer_started {
            let moved = std::mem::take(&mut self.think_emitted);
            if !moved.is_empty() {
                out.push(Piece::Promote(moved));
            }
        }
        out.into_iter().filter(|p| !is_empty_piece(p)).collect()
    }

    /// 思考の途中で終わったか（本文が1文字も出ていない）。画面の断り書きに使う。
    pub fn stuck_in_thought(&self) -> bool {
        self.in_think && !self.saw_close
    }

    fn answer(&mut self, s: String) -> Piece {
        let t = if self.answer_started {
            s
        } else {
            let t = s.trim_start_matches(['\n', '\r', ' ']).to_string();
            if !t.is_empty() {
                self.answer_started = true;
            }
            t
        };
        self.answer_emitted.push_str(&t);
        Piece::Answer(t)
    }

    fn note_think(&mut self, s: &str) {
        self.think_emitted.push_str(s);
    }
}

fn is_empty_piece(p: &Piece) -> bool {
    match p {
        Piece::Answer(s) | Piece::Think(s) | Piece::Reclassify(s) | Piece::Promote(s) => {
            s.is_empty()
        }
    }
}

/// このラウンドで本文として出した分を、まとめて思考欄へ移す指示を作る。
///
/// ★経路A の対策。タグが一切無いまま「思考 → ツール呼び出し」となる形では、
/// 分離の仕組みは思考だと気づけない。しかし**ツールを呼んだラウンドの本文は
/// 最終回答ではない**（最終回答はツールを呼ばないラウンドに出る）ので、
/// ラウンドの終わりに構造で判断できる。
impl Splitter {
    pub fn demote_answer(&mut self) -> Option<Piece> {
        if self.answer_emitted.is_empty() {
            return None;
        }
        let moved = std::mem::take(&mut self.answer_emitted);
        self.think_emitted.push_str(&moved);
        self.answer_started = false;
        Some(Piece::Reclassify(moved))
    }
}

/// 末尾がタグの一部になりかけている長さ。`</th` なら 4、`abc` なら 0。
fn partial_tail(s: &str) -> usize {
    let max = CLOSE.len() - 1;
    let n = s.len();
    let start = n.saturating_sub(max);
    for i in start..n {
        if !s.is_char_boundary(i) {
            continue;
        }
        let tail = &s[i..];
        if OPEN.starts_with(tail) || CLOSE.starts_with(tail) {
            return n - i;
        }
    }
    0
}

/// 生成量の計測。llama.cpp は 1 チャンク＝1 トークンで送ってくるので、
/// チャンクを数えれば実トークン数になる（最後に usage が来ればそちらを優先）。
pub struct Meter {
    /// 1断片＝1トークンとして数えるか。
    ///
    /// ★llama.cpp の逐次配信は 1 チャンク＝1 トークンなので、数えれば実数になる。
    /// しかし**本体（Python）経由では逐次で来ない**ことがあり、9,760 字の本文が
    /// 「4 トークン」と表示された（実機の記録で発覚）。その経路では文字数から見積もる。
    by_chars: bool,
    pub answer_tokens: u64,
    pub think_tokens: u64,
    started: Option<std::time::Instant>,
    last_report: u64,
    /// ★生成が流れていた時間（2026-11-11 オーナー「20tok/sとか表示されますが、実際に思考とか見ていると明らかにもっと早い」）。
    /// tok_s は走行の全時間（道具・読み込み・待ち）で割るので遅く見える。断片の間が FLOW_GAP 秒未満の間だけ足す。
    flow_secs: f64,
    last_at: Option<std::time::Instant>,
}

/// 文字からトークン数を見積もる（Meter::tick_text の説明を参照）。1 以上。
pub fn est_tokens(text: &str) -> u64 {
    let mut wide = 0usize;
    let mut other = 0usize;
    for c in text.chars() {
        let u = c as u32;
        if (0x3000..=0x30ff).contains(&u) || (0x3400..=0x9fff).contains(&u) || (0xff00..=0xffef).contains(&u)
            || (0xac00..=0xd7af).contains(&u)
        {
            wide += 1;
        } else {
            other += 1;
        }
    }
    ((wide as f64) * 0.7 + (other as f64) * 0.27).ceil().max(1.0) as u64
}

/// これより長い間は「生成が止まっていた」（道具・読み込み・待ち）として生成の時間に入れない。
const FLOW_GAP: f64 = 3.0;

impl Default for Meter {
    fn default() -> Self {
        Self { by_chars: false, answer_tokens: 0, think_tokens: 0, started: None, last_report: 0, flow_secs: 0.0, last_at: None }
    }
}

impl Meter {
    /// 断片が「1トークン」でない経路（本体経由）で使う。
    pub fn count_by_chars(&mut self) {
        self.by_chars = true;
    }

    /// 断片1つぶん進める。`by_chars` なら文字数から見積もる。
    /// ★2026-11-16 測り直し（Qwen3.8 の tokenizer で、英語の文 4,070 字・Python 6,000 字・日本語 3,000 字）:
    ///   英語 4.0 字/トークン、コード 3.2、日本語 1.7（かな・漢字は 1 字 ≒ 0.7 トークン）。
    ///   前は全部を 2.2 で割っていて、思考（ほぼ英語）を 1.8 倍に数え、走行中の「生成 tok/s」が実測の約 1.8 倍に出ていた。
    ///   → かな・漢字・全角は 0.7、それ以外は 0.27（≒ 3.7 字に 1 トークン）で数える。誤差は文の種類で ±12% ほど。
    pub fn tick_text(&mut self, thinking: bool, text: &str) {
        if !self.by_chars {
            self.tick(thinking);
            return;
        }
        let n = est_tokens(text);
        if self.started.is_none() {
            self.started = Some(std::time::Instant::now());
        }
        self.flow();
        if thinking {
            self.think_tokens += n;
        } else {
            self.answer_tokens += n;
        }
    }

    pub fn tick(&mut self, thinking: bool) {
        if self.started.is_none() {
            self.started = Some(std::time::Instant::now());
        }
        self.flow();
        if thinking {
            self.think_tokens += 1;
        } else {
            self.answer_tokens += 1;
        }
    }
    /// 思考として数えた分を本文へ付け替える（Promote と同時に呼ぶ）。
    /// ★これが無いと「出力 0（＋思考 1,080）」と出ているのに本文が表示される、
    ///   という食い違いになる（実機で出た）。
    pub fn promote(&mut self) {
        self.answer_tokens += self.think_tokens;
        self.think_tokens = 0;
    }
    fn flow(&mut self) {
        let now = std::time::Instant::now();
        if let Some(t) = self.last_at {
            let gap = now.duration_since(t).as_secs_f64();
            if gap < FLOW_GAP {
                self.flow_secs += gap;
            }
        }
        self.last_at = Some(now);
    }
    /// 生成が流れていた間の速さ（待ちを含まない）。流れた時間が短すぎるうちは 0。
    pub fn gen_tok_s(&self) -> f64 {
        if self.flow_secs < 0.5 {
            0.0
        } else {
            self.total() as f64 / self.flow_secs
        }
    }
    pub fn total(&self) -> u64 {
        self.answer_tokens + self.think_tokens
    }
    pub fn secs(&self) -> f64 {
        self.started.map(|t| t.elapsed().as_secs_f64()).unwrap_or(0.0)
    }
    pub fn tok_s(&self) -> f64 {
        let s = self.secs();
        if s <= 0.0 {
            0.0
        } else {
            self.total() as f64 / s
        }
    }
    /// 画面の更新は毎トークンだと重い。8 トークンごとに1回だけ真を返す。
    pub fn should_report(&mut self) -> bool {
        if self.total() >= self.last_report + 8 {
            self.last_report = self.total();
            true
        } else {
            false
        }
    }
    pub fn stat(&self) -> serde_json::Value {
        serde_json::json!({
            "answer_tokens": self.answer_tokens,
            "think_tokens": self.think_tokens,
            "tok_s": (self.tok_s() * 10.0).round() / 10.0,
            "gen_tok_s": (self.gen_tok_s() * 10.0).round() / 10.0,
            "secs": (self.secs() * 10.0).round() / 10.0,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// ★2026-11-16: 英語の文を 1.8 倍に数えていた（2.2 で割っていた）。tokenizer で測った数に近いこと。
    #[test]
    fn est_tokens_is_close_to_the_tokenizer() {
        let en = "The quick brown fox jumps over the lazy dog while the server reads the request. ".repeat(20);
        let n = est_tokens(&en) as f64;            // 実際は約 4 字に 1 トークン
        let real = en.chars().count() as f64 / 4.0;
        assert!((n / real) > 0.85 && (n / real) < 1.2, "en {n} vs {real}");
        let ja = "日本語の文はかな漢字が多く、一字がおよそ〇・七トークンになる。".repeat(20);
        let m = est_tokens(&ja) as f64;
        let real_ja = ja.chars().count() as f64 / 1.6;
        assert!((m / real_ja) > 0.8 && (m / real_ja) < 1.25, "ja {m} vs {real_ja}");
    }

    /// 画面と同じ意味で数える。★`Reclassify` は「本文に出した分を思考へ移す」なので、
    /// ここでも実際に移す。数えるだけにすると、移動が起きていなくても試験が通ってしまう。
    fn texts(v: Vec<Piece>) -> (String, String, usize) {
        let (mut a, mut t, mut r) = (String::new(), String::new(), 0);
        for p in v {
            match p {
                Piece::Answer(s) => a.push_str(&s),
                Piece::Think(s) => t.push_str(&s),
                // ★画面と同じ意味で動かす。**渡された文字列の分だけ**動かすのが要点
                //   （「移せ」とだけ伝えて受け側が全部動かしたのが経路B・D）。
                Piece::Reclassify(m) => {
                    r += 1;
                    if let Some(rest) = a.strip_suffix(&m) { a = rest.to_string(); }
                    t.push_str(&m);
                }
                Piece::Promote(m) => {
                    if let Some(rest) = t.strip_suffix(&m) { t = rest.to_string(); }
                    a.push_str(&m);
                }
            }
        }
        (a, t, r)
    }

    #[test]
    fn plain_text_is_all_answer() {
        let mut s = Splitter::new(false);
        let (a, t, r) = texts(s.push("こんにちは。元気です。"));
        assert_eq!(a, "こんにちは。元気です。");
        assert!(t.is_empty() && r == 0);
    }

    #[test]
    fn a_normal_think_block_is_separated() {
        let mut s = Splitter::new(false);
        let mut all = s.push("<think>まず調べる</think>結論です");
        all.extend(s.finish(true));
        let (a, t, r) = texts(all);
        assert_eq!(a, "結論です");
        assert_eq!(t, "まず調べる");
        assert_eq!(r, 0);
    }

    #[test]
    fn an_orphan_close_tag_means_it_was_thinking_all_along() {
        // ★実機で起きた形。テンプレートが `<think>` を入れるので閉じタグから始まる。
        let mut s = Splitter::new(false);
        let mut all = s.push("笔者");
        all.extend(s.push("\n\n</think>\n\n私はインフラの助手です"));
        all.extend(s.finish(true));
        let (a, t, r) = texts(all);
        assert_eq!(r, 1, "本文に出した分を思考へ移す指示が要る");
        assert_eq!(a, "私はインフラの助手です", "閉じタグ直後の改行は落とす");
        assert!(t.contains("笔者"));
        assert!(!a.contains("</think>"), "タグが本文に漏れない");
    }

    #[test]
    fn the_shape_is_remembered_so_it_only_reclassifies_once() {
        let mut s = Splitter::new(false);
        let _ = s.push("あ</think>い");
        assert!(forced_open(), "一度見たら覚える");
        let mut s2 = Splitter::default();
        let (a, t, r) = texts(s2.push("う</think>え"));
        assert_eq!(r, 0, "2回目は最初から思考として扱うので遡らない");
        assert_eq!(t, "う");
        assert_eq!(a, "え");
    }

    #[test]
    fn a_tag_split_across_chunks_is_not_leaked() {
        let mut s = Splitter::new(true);
        let mut all = s.push("考え中</th");
        // 途中でタグの断片を出さない（`</th` を思考にも本文にも混ぜない）
        assert!(all.iter().all(|p| !matches!(p, Piece::Answer(_))));
        assert!(!format!("{all:?}").contains("</th"), "割れたタグが漏れた: {all:?}");
        all.extend(s.push("ink>答え"));
        all.extend(s.finish(true));
        let (a, t, _) = texts(all);
        assert_eq!(a, "答え");
        assert_eq!(t, "考え中");
    }

    #[test]
    fn a_lone_angle_bracket_is_not_held_forever() {
        let mut s = Splitter::new(false);
        let mut all = s.push("a < b なので");
        all.extend(s.finish(true));
        let (a, _, _) = texts(all);
        assert_eq!(a, "a < b なので", "タグでない < を飲み込まない");
    }

    #[test]
    fn multibyte_at_the_boundary_does_not_panic() {
        let mut s = Splitter::new(false);
        for part in ["日本", "語の", "途中", "<thi", "nk>", "内", "</think>", "外"] {
            let _ = s.push(part);
        }
        let _ = s.finish(true);
    }

    #[test]
    fn a_reply_without_thinking_is_not_swallowed() {
        // ★退行防止。`FORCED_OPEN` を覚えた後で「思考しない返答」が来ると、
        //   閉じタグが来ないまま全部が思考欄に入り、画面が空になる。
        let mut s = Splitter::new(true);
        let mut all = s.push("こんにちは。何をしましょうか。");
        all.extend(s.finish(true));
        let (a, t, _) = texts(all);
        assert_eq!(a, "こんにちは。何をしましょうか。", "本文が空になってはいけない");
        assert!(t.is_empty());
    }

    #[test]
    fn thinking_cut_off_by_the_limit_stays_in_the_thinking_pane() {
        // ★退行防止。上限で切られた思考を本文に混ぜてはいけない
        //   （2026-08-11「内部思考がそのまま返答に混じる」として実機で出た）。
        let mut s = Splitter::new(true);
        let mut all = s.push("まず状況を整理すると、ユーザーは");
        all.extend(s.finish(false)); // 上限で切られた＝戻さない
        let (a, t, _) = texts(all);
        assert!(a.is_empty(), "上限で切られた思考が本文に混ざった: {a}");
        assert!(t.contains("まず状況を整理すると"));
        assert!(s.stuck_in_thought());
    }

    #[test]
    fn thinking_before_a_tool_call_never_becomes_the_answer() {
        // ★退行防止。ツールを呼んだラウンドの思考を本文へ戻すと、
        //   それが履歴に入り、次のラウンドでモデルが自分の思考を読み返して繰り返す
        //   （実機で英語の思考が3ブロック並んだ形）。
        let mut s = Splitter::new(true);
        let mut all = s.push("Okay, the user wants me to create a page. Let me check ws_list first.");
        all.extend(s.finish(false)); // ツールを呼んだので戻さない
        let (a, t, _) = texts(all);
        assert!(a.is_empty(), "思考が本文に混ざった: {a}");
        assert!(t.contains("Okay, the user wants"));
    }

    #[test]
    fn a_blank_screen_is_still_rescued() {
        // 本文もツールも無いときだけは戻す（画面が真っ白になるより出す）。
        let mut s = Splitter::new(true);
        let mut all = s.push("こんにちは。何をしましょうか。");
        all.extend(s.finish(true));
        let (a, _, _) = texts(all);
        assert_eq!(a, "こんにちは。何をしましょうか。");
    }

    #[test]
    fn an_answer_written_inside_the_thinking_block_is_rescued() {
        // ★実機で最も多かった「本文が空」の形（2026-08-16・40件中4件）。
        //   モデルは閉じタグまで出すが、そのあと**1文字も書かない**。
        //   答えは思考欄の中で完成している（末尾が「。」で終わる報告文）。
        //   旧条件は `!saw_close` を要求していたので、ここは救済されず画面が空だった。
        let mut s = Splitter::new(true);
        let mut all = s.push("<think>修正内容の反映を確認しました。外部CSSへ分離済みです。</think>");
        all.extend(s.finish(true)); // 自然終了・ツール無し・本文ゼロ
        let (a, _, _) = texts(all);
        assert!(
            a.contains("修正内容の反映を確認しました"),
            "閉じタグを見たあと本文ゼロで終わった回が救済されていない: {a:?}"
        );
    }

    #[test]
    fn the_rescue_does_not_fire_when_there_is_already_an_answer() {
        // ★救済が効きすぎると、思考が本文の後ろに足されて二重になる。
        let mut s = Splitter::new(true);
        let mut all = s.push("<think>下調べ</think>これが答えです。");
        all.extend(s.finish(true));
        let (a, t, _) = texts(all);
        assert_eq!(a, "これが答えです。");
        assert_eq!(t, "下調べ");
    }

    #[test]
    fn a_lump_of_text_is_not_one_token() {
        // ★実機で 9,760 字の本文が「4 トークン」と出た。断片の数を数えていたため。
        let mut m = Meter::default();
        m.count_by_chars();
        m.tick_text(false, &"あ".repeat(1000));
        assert!(m.answer_tokens > 300, "1000 字が {} トークン", m.answer_tokens);
    }

    #[test]
    fn streaming_still_counts_one_per_chunk() {
        let mut m = Meter::default();
        for _ in 0..5 {
            m.tick_text(false, "token");
        }
        assert_eq!(m.answer_tokens, 5, "逐次経路の数え方を変えない");
    }

    #[test]
    fn the_meter_counts_both_channels() {
        let mut m = Meter::default();
        for _ in 0..3 {
            m.tick(true);
        }
        for _ in 0..2 {
            m.tick(false);
        }
        assert_eq!(m.think_tokens, 3);
        assert_eq!(m.answer_tokens, 2);
        assert_eq!(m.total(), 5);
    }
}

#[cfg(test)]
mod gen_speed_tests {
    use super::*;

    /// 生成の速さは、断片の間が FLOW_GAP 秒を超えた時間（道具・待ち）を数えない。
    #[test]
    fn 待ちを生成の時間に入れない() {
        let mut m = Meter::default();
        for _ in 0..20 {
            m.tick(false);
            std::thread::sleep(std::time::Duration::from_millis(30));
        }
        let fast = m.gen_tok_s();
        assert!(fast > 15.0, "流れている間の速さ: {fast}");
        std::thread::sleep(std::time::Duration::from_millis(3200));
        m.tick(false);
        let after = m.gen_tok_s();
        assert!(after > fast * 0.8, "待ちの後も生成の速さは落ちない: {fast} → {after}");
        assert!(m.tok_s() < fast * 0.5, "全時間で割る tok_s は待ちで落ちる: {}", m.tok_s());
    }
}
