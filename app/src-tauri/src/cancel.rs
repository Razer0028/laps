//! 生成の中止。
//!
//! ★2026-08-11 オーナー要望。止める手段が無く、思考に入り込んだ生成を
//! 最後まで待つしかなかった（実機で 1,080 トークンの思考を待たされている）。
//!
//! 世代（generation）で管理する。単なる真偽値にすると、
//! **止めた直後に始めた生成が、前の中止指示を引き継いで即死する**。
//! 送信のたびに世代を進め、中止は「いまの世代を止める」と記録する。

use std::sync::atomic::{AtomicU64, Ordering};

/// いまの世代。送信のたびに1つ進む。
static GEN: AtomicU64 = AtomicU64::new(0);
/// 中止を指示された世代。
static STOPPED: AtomicU64 = AtomicU64::new(u64::MAX);

/// 既定の器（札の無い走行）のいまの世代。★札つきの走行（下）が世代を進めても、既定の「止める」がそちらへ効かないように分ける。
static DEFAULT_GEN: AtomicU64 = AtomicU64::new(0);

/// ★走行の器（レーン・2026-10-30）。オーナー「api を使っている間は別チャットで gpu の推論に切り替えて使えるように」
///   ＝会話ごとに同時に走る。1 つの「いまの世代」だけでは、別の会話の「止める」が**最後に始めた会話**を止めてしまう。
///   札（会話）ごとにいまの世代を覚え、止めた世代を短い列に置く。
static LANE_GENS: std::sync::Mutex<Vec<(String, u64)>> = std::sync::Mutex::new(Vec::new());
static STOPPED_LANES: std::sync::Mutex<Vec<u64>> = std::sync::Mutex::new(Vec::new());

fn guard<T>(m: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// 新しい生成を始める。返った値を、その生成の間だけ持ち歩く。
pub fn begin() -> u64 {
    let g = GEN.fetch_add(1, Ordering::SeqCst) + 1;
    DEFAULT_GEN.store(g, Ordering::SeqCst);
    g
}

/// いま走っている生成を止める。
pub fn stop() {
    STOPPED.store(DEFAULT_GEN.load(Ordering::SeqCst), Ordering::SeqCst);
}

/// いま走っている世代。ラウンドをまたぐ処理が、同じ世代を見るために使う。
pub fn current() -> u64 {
    DEFAULT_GEN.load(Ordering::SeqCst)
}

/// 札（会話）つきの走行を始める。札が空なら既定の器（begin と同じ）。
pub fn begin_lane(lane: &str) -> u64 {
    if lane.is_empty() {
        return begin();
    }
    let g = GEN.fetch_add(1, Ordering::SeqCst) + 1;
    let mut v = guard(&LANE_GENS);
    v.retain(|(l, _)| l != lane);
    v.push((lane.to_string(), g));
    if v.len() > 32 {
        v.remove(0);
    }
    g
}

/// その札の走行を止める。札が空なら既定の器（stop と同じ）。★札の走行がまだ一度も始まっていなければ既定の器を止める
/// （本体が使えず内蔵の控えで走っている会話は、札つきで頼んでも既定の器で走る・2026-10-30）。
pub fn stop_lane(lane: &str) {
    if lane.is_empty() {
        stop();
        return;
    }
    let g = guard(&LANE_GENS).iter().find(|(l, _)| l == lane).map(|(_, g)| *g);
    if g.is_none() {
        stop();
        return;
    }
    if let Some(g) = g {
        let mut s = guard(&STOPPED_LANES);
        if !s.contains(&g) {
            s.push(g);
        }
        if s.len() > 64 {
            s.remove(0);
        }
    }
}

/// この世代は止められたか。
pub fn is_stopped(gen: u64) -> bool {
    STOPPED.load(Ordering::SeqCst) == gen || guard(&STOPPED_LANES).contains(&gen)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// ★状態がプロセス全体で1つなので、試験は並列に走ると互いを壊す。
    /// 実装の問題ではなく試験の問題なので、ここで直列化する。
    static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());
    fn serial() -> std::sync::MutexGuard<'static, ()> {
        SERIAL.lock().unwrap_or_else(|e| e.into_inner())
    }

    #[test]
    fn stopping_affects_the_running_generation() {
        let _g = serial();
        let g = begin();
        assert!(!is_stopped(g));
        stop();
        assert!(is_stopped(g));
    }

    #[test]
    fn a_new_generation_is_not_killed_by_the_previous_stop() {
        let _g = serial();
        // ★真偽値1つで作ると、ここが壊れる。止めた直後の送信が即死する。
        let g1 = begin();
        stop();
        assert!(is_stopped(g1));
        let g2 = begin();
        assert!(!is_stopped(g2), "次の生成が前の中止を引き継いだ");
    }

    #[test]
    fn lanes_stop_only_themselves() {
        let _g = serial();
        let a = begin();                    // 既定の器
        let b = begin_lane("c1");            // 会話 1
        let c = begin_lane("c2");            // 会話 2（最後に始めた）
        stop_lane("c1");
        assert!(is_stopped(b) && !is_stopped(c) && !is_stopped(a), "会話 1 の「止める」が別の走行に効いた");
        stop();                             // 既定の「止める」は最後に始めた会話 2 ではなく既定の器へ
        assert!(is_stopped(a) && !is_stopped(c));
        let c2 = begin_lane("c2");          // 同じ会話の次の走行は前の中止を引き継がない
        let d = begin();                    // 内蔵の控え（既定の器）で走っている会話
        stop_lane("c9");                    // 一度も始まっていない札＝既定の器を止める（札の走行には効かない）
        assert!(!is_stopped(c2) && is_stopped(d));
    }

    #[test]
    fn stopping_twice_is_harmless() {
        let _g = serial();
        let g = begin();
        stop();
        stop();
        assert!(is_stopped(g));
    }
}
