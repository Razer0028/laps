/* BLACKWALL Remote — 会話の描画（2026-10-10 オーナー「デスクトップアプリの方のUIです。ウェブもこれに合わせて」）。
 *
 * 部品はデスクトップアプリのものをそのまま使う（desk.js＝app/ui/app.js から scripts/extract_desk.py で抜き出す）:
 *   依頼＝.msg--user の吹き出し／答え＝.msg--bot（BLACKWALL の名札・Thinking・本文の Markdown・道具の束・meter）。
 *   色と形は app/ui/styles.css（同じ物を配る）。ここに新しい見た目を足さない。
 * 状態は core.js の bwReduce（webui・Android と同じ約束）。ここは「状態 → デスクトップの DOM」の写しだけ。
 */
"use strict";

/* desk.js の paraNode が引用チップの押下で呼ぶ。ウェブ版に Sources の窓は無い（出典の数 0 で描く＝チップは作らない）。 */
function focusEvidence() { /* 何もしない */ }

const BW = {
  state: bwInitState(), dom: {}, groups: new Map(), ws: null, runId: null, pendingPrompt: null,
  conn: "接続しています…", connBad: false, frame: 0, booted: false, timer: null,
  syn: { key: "", len: 0, phase: "" },
};

/* ── 描き直し ─────────────────────────────────────────────────────
 * 1 フレームに 1 回（走行中は 1 秒に数十の事象が来る）。すぐ要るとき（開き直しの直後に下へ送る）は bwRenderNow。 */
function bwRender() {
  if (BW.frame) return;
  const raf = globalThis.requestAnimationFrame || ((f) => setTimeout(f, 16));
  BW.frame = raf(() => { BW.frame = 0; bwRenderNow(); });
}
function bwRenderNow() {
  if (BW.frame) { try { (globalThis.cancelAnimationFrame || clearTimeout)(BW.frame); } catch (e) { /* 無い */ } BW.frame = 0; }
  const s = BW.state, d = BW.dom;
  if (!d.thread) return;
  const stick = bwNearBottom();
  bwRenderThread(s, d);
  bwPaintRunbar();
  bwRenderAsk(s, d);
  bwPaintSend();
  bwFeedSynapse(s);
  if (d.note) {
    const text = s.note || (s.queue ? s.queue.message : "") || "";
    d.note.textContent = text;
    d.note.classList.toggle("is-bad", !!s.note && s.noteBad);
  }
  try { document.body.classList.toggle("is-busy", isRunning(s)); } catch (e) { /* 偽 DOM */ }
  if (typeof rmAfterRender === "function") rmAfterRender();
  if (stick) bwToBottom();
}
/* 下まで読んでいる人だけ追従させる（さかのぼっている最中に引き戻さない）。デスクトップの nearBottom と同じ 120px。 */
function bwNearBottom() {
  const t = BW.dom.thread;
  if (!t) return true;
  return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
}
function bwToBottom() { const t = BW.dom.thread; if (t) t.scrollTop = t.scrollHeight; }

/* ── 会話: 依頼ごとに区切る ────────────────────────────────────────
 * デスクトップと同じく、依頼（user）の後の物は 1 つの BLACKWALL の吹き出しに入る。
 * 走行中の割り込み（inject）は吹き出しの中の .injected（デスクトップの「interjected: …」）。 */
function bwGroups(timeline) {
  const groups = [];
  let cur = null;
  for (const it of timeline) {
    if (it.kind === "user") { groups.push({ kind: "user", key: "u:" + it.key, item: it }); cur = null; continue; }
    if (!cur) { cur = { kind: "bot", key: "b:" + it.key, items: [] }; groups.push(cur); }
    cur.items.push(it);
  }
  return groups;
}

function bwRenderThread(s, d) {
  const groups = bwGroups(s.timeline);
  const keep = new Set(groups.map((g) => g.key));
  for (const [k, g] of Array.from(BW.groups)) {
    if (!keep.has(k)) { g.el.remove(); BW.groups.delete(k); }
  }
  const last = groups[groups.length - 1];
  let prev = null;
  for (const g of groups) {
    let ent = BW.groups.get(g.key);
    if (!ent) {
      ent = g.kind === "user" ? bwUserNode(g.item) : bwBotNode();
      BW.groups.set(g.key, ent);
    }
    // 並びを保つ（前の吹き出しのすぐ後ろに置く）。
    const want = prev ? prev.el.nextSibling : d.thread.firstChild;
    if (ent.el !== want) d.thread.insertBefore(ent.el, want);
    if (g.kind === "user") {
      if (ent.item !== g.item) { ent.el.querySelector(".bubble").textContent = g.item.text; ent.item = g.item; }
    } else {
      // ★1 つの吹き出しで例外が出ても、ほかの吹き出しと画面の残り（走行の行・送る札・追う線）は描く（2026-10-13）。
      try { bwPaintBot(ent, g.items, g === last && isRunning(s), s); }
      catch (e) {
        if (!ent.el.querySelector(":scope > .notice.is-render-error")) {
          const n = document.createElement("p"); n.className = "notice is-gate is-render-error";
          n.textContent = `この返事の一部を描けませんでした（${e.message}）`;
          ent.el.appendChild(n);
        }
        try { console.error(e); } catch (e2) { /* 無い */ }
      }
    }
    prev = ent;
  }
  if (s.timeline.length && d.hello && d.hello.parentNode) { d.hello.remove(); d.hello = null; }
}

function bwUserNode(item) {
  const n = document.createElement("div");
  n.className = "msg msg--user";
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = item.text;
  n.appendChild(b);
  return { el: n, item };
}

/* デスクトップの beginLive と同じ骨組み。 */
function bwBotNode() {
  const n = document.createElement("div");
  n.className = "msg msg--bot";
  n.innerHTML =
    '<div class="who">L.A.P.S<span class="pulse" aria-hidden="true" hidden></span><span class="who__sum num"></span></div>' +
    '<div class="meter num" aria-live="off"></div>';
  return {
    el: n, who: n.querySelector(".who"), pulse: n.querySelector(".pulse"), sum: n.querySelector(".who__sum"), meter: n.querySelector(".meter"),
    parts: new Map(), thinkText: "", fold: { t: null, open: false }, planEl: null, planKey: "",
  };
}

/* 本文に混ざった思考（<think>…</think>）を思考の箱へ移す。
 * ★デスクトップは本体（Rust）が流している途中で分けるので本文に札が来ないが、会話の記録（assistant の本文）には残っている
 *   （Gemma が札を本文に書く走行が実在する）。開き直しでは記録から描くので、ここで分ける。閉じが来ていない札は生成中の思考。 */
function bwSplitThink(text) {
  let think = "";
  const add = (t) => { t = String(t || "").trim(); if (t) think += (think ? "\n" : "") + t; };
  let s = String(text || "").replace(/<think>([\s\S]*?)<\/think>/g, (m, t) => { add(t); return ""; });
  const c = s.lastIndexOf("</think>");                 // 開きの無い閉じ（デスクトップの stripThinking と同じ形）
  if (c !== -1) { add(s.slice(0, c)); s = s.slice(c + 8); }
  const o = s.indexOf("<think>");                       // 閉じがまだ来ていない
  if (o !== -1) { add(s.slice(o + 7)); s = s.slice(0, o); }
  return { think, text: s.replace(/^\s+/, "") };
}

/* 走っている行の秒を進める（帯の時計と一緒に）。 */
const bwLiveTurns = new Set();

/* 吹き出しの中身を、いまの区切りの並びに合わせる（同じ物は作り直さない）。
 * ★走行の線（2026-11-02・デスクトップと同じ部品）: 1 手番＝1 行。思考は同じ手番の行に入れ、道具の無い手番だけ「Thought」の行になる。 */
function bwPaintBot(ent, items, live, s) {
  ent.el.classList.toggle("is-live", live);
  ent.pulse.hidden = !live;
  const split = new Map();
  for (const i of items) if (i.kind === "answer") split.set(i.key, bwSplitThink(i.text));
  // 手番ごとの思考。本文に混ざった思考（記録の <think>）も、その手番の思考として数える。
  const thinkOf = new Map(), startOf = new Map(), toolIters = new Set();
  const addThink = (iter, text, at) => {
    if (!text) return;
    thinkOf.set(iter, (thinkOf.has(iter) ? thinkOf.get(iter) + "\n" : "") + text);
    if (at && !startOf.has(iter)) startOf.set(iter, at);
  };
  for (const i of items) {
    if (i.kind === "tools") i.tools.forEach((t) => toolIters.add(t.iter || 0));
    if (i.kind === "think") addThink(i.iter || 0, i.text, i.startedMs);
    if (i.kind === "answer") addThink(i.iter || 0, split.get(i.key).think, i.startedMs);
  }
  ent.thinkText = [...thinkOf.values()].join("\n");
  // サブエージェントの動き（「子: Read パス」「子: ✓ Read」）は、任せている手番の行に出す。
  const kidOf = new Map();
  let taskIter = null;
  for (const i of items) {
    if (i.kind === "tools") i.tools.forEach((t) => { if (String(t.name || "") === "Task") taskIter = t.iter || 0; });
    if (i.kind === "notice" && taskIter != null && String(i.text || "").startsWith("子: ")) {
      if (!kidOf.has(taskIter)) kidOf.set(taskIter, []);
      kidOf.get(taskIter).push(String(i.text).slice(3));
    }
  }

  // 本文・行・割り込み・注記を、届いた順に（デスクトップの parts と同じ並び）。
  const keep = new Set();
  let prevNode = ent.planEl || ent.who;
  let outcome = null;
  const place = (key, make, paint) => {
    keep.add(key);
    let p = ent.parts.get(key);
    if (!p) { p = make(); ent.parts.set(key, p); }
    paint(p);
    if (prevNode.nextSibling !== p.el) ent.el.insertBefore(p.el, prevNode.nextSibling);
    prevNode = p.el;
  };
  const thoughtRow = (key, iter, streaming, at) => place(key, () => bwRailPart(), (p) => {
    const t = p.turn || (p.turn = bwTurnIn(p));
    t.thought = thinkOf.get(iter) || "";
    t.t0 = at || t.t0;
    t.t1 = streaming && live ? null : (t.t1 ?? Date.now());
    paintRailTurn(t);
    if (t.t1 == null) bwLiveTurns.add(t); else bwLiveTurns.delete(t);
  });
  const seen = new Set();
  for (const it of items) {
    if (it.kind === "think") {
      const iter = it.iter || 0;
      if (toolIters.has(iter) || seen.has(iter)) continue;    // 道具のある手番の思考はその行の中へ
      seen.add(iter);
      thoughtRow(it.key, iter, it.streaming, it.startedMs);
      continue;
    }
    if (it.kind === "outcome") { outcome = it; continue; }
    if (it.kind === "notice" && it.level === "dim") continue;    // 内部の進み（phase_start 等）は数えるだけ
    if (it.kind === "notice" && taskIter != null && String(it.text || "").startsWith("子: ")) continue;
    if (it.kind === "answer") {
      const iter = it.iter || 0;
      if (split.get(it.key).think && !toolIters.has(iter) && !seen.has(iter)) { seen.add(iter); thoughtRow(it.key + "t", iter, false, it.startedMs); }
    }
    place(it.key, () => bwPartNode(it), (p) => {
      if (it.kind === "tools") { bwPaintBundle(p, it, s, thinkOf, startOf, kidOf); p.item = it; return; }
      if (p.item !== it) { bwPaintPart(p, it, s, split.get(it.key)); p.item = it; }
    });
  }
  for (const [k, p] of Array.from(ent.parts)) {
    if (!keep.has(k)) { p.el.remove(); (p.turns ? [...p.turns.values()] : p.turn ? [p.turn] : []).forEach((t) => bwLiveTurns.delete(t)); ent.parts.delete(k); }
  }
  bwRailAfter(ent, items);
  // 終わり: 未完了なら赤の注記、止めたなら素の注記。数字は meter（デスクトップの fmtStat と同じ書き方）。
  bwPaintOutcome(ent, outcome, items);
}

function bwRailPart() {
  const n = document.createElement("section");
  n.className = "rail";
  return { el: n, item: null, turns: new Map(), nodes: new Map(), turn: null };
}
function bwTurnIn(p) {
  const t = railTurnNode();
  p.el.appendChild(t.root);
  return t;
}
function bwPartNode(it) {
  if (it.kind === "answer") { const n = document.createElement("div"); n.className = "live"; return { el: n, item: null }; }
  if (it.kind === "tools" || it.kind === "inject") return bwRailPart();
  const n = document.createElement("p");
  n.className = "notice";
  return { el: n, item: null };
}

function bwPaintPart(p, it, s, sp) {
  if (it.kind === "answer") {
    const text = sp ? sp.text : it.text;
    p.el.innerHTML = "";
    try { renderMarkdown(text, (n) => p.el.appendChild(n), 0, 0); }
    catch (e) { p.el.textContent = text; }      // 途中の壊れた形なら素のまま出す（デスクトップと同じ）
    return;
  }
  if (it.kind === "inject") {
    const t = p.turn || (p.turn = bwTurnIn(p));
    t.kind = "you"; t.say = it.text; t.v = "You"; t.res = it.force ? "interrupted" : "interjected";
    paintRailTurn(t);
    return;
  }
  // 注記: bad＝門・失敗（赤の左線）。それ以外は素。
  p.el.className = "notice" + (it.level === "bad" ? " is-gate" : "");
  p.el.textContent = it.text;
}

/* 道具の区切り。reducer の道具（status/input/result）をデスクトップの entry（done/ok/args/result）へ写し、手番ごとの行にまとめる。 */
function bwDeskEntry(t) {
  const done = t.status !== "running";
  const secs = (done && t.endedMs && t.startedMs) ? Math.max(0, Math.round((t.endedMs - t.startedMs) / 1000)) : undefined;
  return {
    name: t.name, args: t.input || {}, done,
    ok: t.status !== "failed", orphan: t.status === "orphan",
    kind: t.resultKind || (t.status === "denied" ? "denied" : t.status === "empty" ? "empty" : ""),
    result: t.result, summary: t.summary, truncated: !!t.truncated,
    chars: t.chars > 0 ? t.chars : undefined, secs,
  };
}
function bwPaintBundle(p, it, s, thinkOf, startOf, kidOf) {
  const restoring = !isRunning(s) && !p.painted;
  const groups = new Map();
  it.tools.forEach((t, i) => {
    const k = t.iter || 0;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push([t, i]);
  });
  for (const [iter, list] of groups) {
    let turn = p.turns.get(iter);
    if (!turn) { turn = bwTurnIn(p); p.turns.set(iter, turn); }
    const entries = [];
    list.forEach(([t, i]) => {
      const key = `${t.call}#${i}`;
      let node = p.nodes.get(key);
      if (!node) {
        node = toolNode();
        // 開き直した会話の畳まれた道具は、開いたときに描く（デスクトップの __lazy と同じ）。
        if (restoring) node.__lazy = true;
        p.nodes.set(key, node);
        turn.toolsEl.appendChild(node);
        turn.nodes.push(node);
      }
      const e = bwDeskEntry(t);
      if (node.__src !== t) { paintTool(node, e); node.__src = t; }
      node.hidden = String(t.name || "") === "TodoWrite" && !toolIsBad(e);   // 予定の更新の中身は帯が見せる
      entries.push(e);
    });
    turn.entries = entries;
    const says = list.map(([t]) => String(t.input?.progress || "").trim()).filter(Boolean);
    turn.say = says[0] || "";
    turn.says = [...new Set(says.slice(1))].filter((x) => x !== turn.say);
    turn.thought = thinkOf.get(iter) || "";
    const kl = kidOf.get(iter) || [];
    const started = kl.filter((x) => !/^[▶✓✗] /.test(x) || x.startsWith("▶ ")).map((x) => x.replace(/^▶ /, ""));
    turn.kids = started.length;
    turn.note = started.length ? clip(started[started.length - 1], 160) : "";
    turn.kidLines = kl.filter((x) => !x.startsWith("✓ ")).slice(-60);
    const t0 = Math.min(startOf.get(iter) || Infinity, ...list.map(([t]) => t.startedMs || Infinity));
    const allDone = entries.every((e) => e.done);
    const t1 = list.reduce((m, [t]) => Math.max(m, t.endedMs || 0), 0);
    turn.t0 = Number.isFinite(t0) ? t0 : turn.t0;
    turn.t1 = allDone && t1 ? t1 : null;
    turn.fixedSecs = allDone && t1 && Number.isFinite(t0) ? Math.max(0, Math.round((t1 - t0) / 1000)) : null;
    const first = list.findIndex(([t]) => String(t.name || "") !== "TodoWrite");
    const f = first >= 0 ? list[first][0] : null;
    turn.thinkSecs = f && Number.isFinite(t0) && f.startedMs && turn.thought.trim() ? Math.max(0, Math.round((f.startedMs - t0) / 1000)) : null;
    paintRailTurn(turn);
    if (turn.t1 == null && isRunning(s)) bwLiveTurns.add(turn); else bwLiveTurns.delete(turn);
  }
  p.painted = true;
}

/* 吹き出し全体でそろえる物: エラーの 1 行は最後の失敗だけ・古い手番を畳む・予定の帯・道具の数。 */
function bwRailAfter(ent, items) {
  const turns = [];
  // 画面の順（行は区切りの中で手番の順に足してある）
  ent.el.querySelectorAll(":scope > .rail").forEach((sec) => {
    for (const p of ent.parts.values()) {
      if (p.el !== sec) continue;
      if (p.turn) turns.push(p.turn);
      if (p.turns) turns.push(...p.turns.values());
    }
  });
  let last = null, lastE = null;
  for (const t of turns) for (const e of t.entries) if (toolIsBad(e) && e.kind !== "denied" && !e.stopped) { last = t; lastE = e; }
  for (const t of turns) {
    const want = t === last ? railErrLine(lastE) : "";
    if (t.errText !== want) { t.errText = want; paintRailTurn(t); }
  }
  applyRailFold(turns, ent.fold);
  const all = turns.flatMap((t) => t.entries).filter((e) => String(e.name || "") !== "TodoWrite");
  const bad = all.filter((e) => toolIsBad(e) && e.kind !== "denied" && !e.stopped).length;
  if (ent.sum) ent.sum.textContent = all.length ? `${all.length} ${all.length === 1 ? "tool" : "tools"}${bad ? ` · ${bad} failed` : ""}` : "";
  // 予定の帯（TodoWrite の一番新しい物）。段ごとの内訳は手番を順に見て数える。
  let plan = null, now = "";
  for (const t of turns) for (const e of t.entries) {
    if (String(e.name || "") === "TodoWrite") {
      const got = planTodos(e);
      if (got.length) { plan = got; const c = got.find((x) => x.status === "in_progress"); if (c) now = c.content; }
    } else if (now) t.step = now;
  }
  if (now) turns.forEach((t) => { if (!t.step) t.step = now; });
  if (plan) {
    if (!ent.planEl) {
      ent.planEl = document.createElement("section");
      ent.planEl.className = "plan";
      ent.planEl.setAttribute("aria-label", "Plan");
      ent.who.after(ent.planEl);
    }
    const stat = (content) => {
      const v = { tools: 0, secs: 0, failed: 0, first: null };
      turns.forEach((t) => {
        if (t.step !== content) return;
        if (!v.first) v.first = t;
        v.tools += t.entries.filter((e) => String(e.name || "") !== "TodoWrite").length;
        v.failed += t.entries.filter((e) => toolIsBad(e) && e.kind !== "denied" && !e.stopped).length;
        v.secs += railSecs(t) || 0;
      });
      return v;
    };
    const jump = (content) => {
      const t = stat(content).first;
      if (!t) return;
      if (t.root.hidden) { ent.fold.open = true; applyRailFold(turns, ent.fold); }
      t.root.scrollIntoView?.({ block: "start", behavior: "smooth" });
    };
    const key = JSON.stringify(plan);
    if (key !== ent.planKey || ent.planEl.__open) { ent.planKey = key; paintPlan(ent.planEl, plan, { stat, jump }); }
  }
}

function bwPaintOutcome(ent, outcome, items) {
  let tail = ent.el.querySelector(":scope > .notice.is-outcome");
  if (!outcome) {
    if (tail) tail.remove();
    ent.meter.textContent = "";
    return;
  }
  const i = outcome.info;
  const label = i.accepted ? "" : (i.cancelled ? "止めました"
    : (i.outcome === "lost" ? "途中で終わりました（本体が入れ替わった）" : `未完了（${i.outcome}）`));
  if (label) {
    if (!tail) { tail = document.createElement("p"); ent.el.insertBefore(tail, ent.meter); }
    tail.className = "notice is-outcome" + (i.cancelled ? "" : " is-gate");
    tail.textContent = label;
  } else if (tail) tail.remove();
  // ★空の返答を黙って出さない（デスクトップの finish と同じ言い方）。
  const hasText = items.some((x) => x.kind === "answer" && bwSplitThink(x.text).text.trim());
  let empty = ent.el.querySelector(":scope > .live.is-empty");
  if (!hasText && i.accepted) {
    if (!empty) { empty = document.createElement("div"); empty.className = "live is-empty"; ent.el.insertBefore(empty, tail || ent.meter); }
    empty.textContent = items.some((x) => x.kind === "tools")
      ? "（道具は動きましたが、答えの本文が返りませんでした。何を実行して何が返ったかは、上の道具の記録にあります。）"
      : (ent.thinkText ? "（考えはしましたが、答えを出しませんでした。上の Thought を開くと、どこまで進んだかが分かります。）"
                       : "（返事が空でした。推論機が途中で止まった可能性があります。）");
  } else if (empty) empty.remove();
  const st = i.stats && (i.stats.answer_tokens || i.stats.secs) ? fmtStat(i.stats) : "";
  const bits = [];
  if (st) bits.push(st);
  else if (i.secs) bits.push(`${Math.round(i.secs)}s`);
  if (i.iters) bits.push(`${i.iters} turns`);
  if (i.tools) bits.push(`${i.tools} tools`);
  ent.meter.textContent = bits.join(" · ");
}

/* ── 走行状態の 1 行（入力欄の上・フォルダの札の横）─────────────────── */
function bwPaintRunbar() {
  bwLiveTurns.forEach((t) => paintRailTurn(t));
  const s = BW.state, d = BW.dom;
  if (!d.runbar) return;
  if (!isRunning(s)) { d.runbar.hidden = true; return; }
  d.runbar.hidden = false;
  const now = Date.now();
  const started = s.startedMs || now;
  const elapsed = Math.max(0, ((s.endedMs || now) - started) / 1000);
  const running = runningToolName(s);
  let what, mark;
  if (s.phase === "stopping") { what = "Stopping"; mark = "·"; }
  else if (s.phase === "asking") { what = "Waiting for you"; mark = "·"; }
  else if (s.phase === "queued") { what = "Queued"; mark = "·"; }
  else if (running) { what = runDoing(running); mark = "▸"; }   // 道具の動き（デスクトップと同じ語・desk.js）・印は静止
  else if (lastStreaming(s) === "answer") { what = "Writing"; mark = markNow(now, started); }
  else if (lastStreaming(s) === "think") { what = gerund(s, elapsed); mark = markNow(now, started); }
  else if (s.runEvents === 0 && !s.chars) { what = "Starting"; mark = "·"; }
  else { what = gerund(s, elapsed); mark = markNow(now, started); }
  d.runbarWhat.textContent = what;
  d.runbarMark.textContent = mark;
  d.runbarTime.textContent = fmtRunElapsed(elapsed);
  d.runbarTokens.textContent = s.chars ? fmtRunTokens(Math.ceil(s.tok || 0)) : "";
  d.runbarHint.textContent = s.heartbeat ? s.heartbeat.text : (s.queue ? s.queue.message : "");
  d.runbarHint.classList.toggle("is-quiet", !!s.heartbeat);
}
function markNow(now, started) {
  if (reducedMotion()) return "✳";
  return runMarkAt(now - started);
}
// ★2026-10-30: 気まぐれな Gerund をやめ、デスクトップと同じく「Thinking…」（オーナー「推論中の文言」）。
function gerund() {
  return "Thinking…";
}
function runningToolName(s) {
  for (let i = s.timeline.length - 1; i >= 0; i--) {
    const b = s.timeline[i];
    if (b.kind !== "tools") continue;
    const t = b.tools.filter((x) => x.status === "running");
    if (t.length) return t[t.length - 1].name;
  }
  return "";
}
function lastStreaming(s) {
  const last = s.timeline[s.timeline.length - 1];
  return last && last.streaming ? last.kind : "";
}

/* ── 背景のシナプス（デスクトップと同じ synapse.js）。届いた分の文字だけを流す。 ── */
function bwFeedSynapse(s) {
  const syn = globalThis.BWSynapse;
  if (!syn) return;
  const last = s.timeline[s.timeline.length - 1];
  if (isRunning(s) && last && last.streaming && (last.kind === "think" || last.kind === "answer")) {
    if (BW.syn.key !== last.key) { BW.syn.key = last.key; BW.syn.len = 0; }
    const delta = String(last.text || "").slice(BW.syn.len);
    if (delta) { syn.feed(last.kind, delta); BW.syn.len += delta.length; }
  }
  let ph = "idle", name = "";
  if (isRunning(s)) {
    const tool = runningToolName(s);
    if (tool) { ph = "tool"; name = tool; }
    else if (lastStreaming(s) === "answer") ph = "write";
    else if (lastStreaming(s) === "think") ph = "think";
    else ph = "wait";
  }
  const sig = ph + "|" + name;
  if (sig !== BW.syn.phase) { BW.syn.phase = sig; syn.phase(ph, name); }
}

/* ── 送るボタン（デスクトップの setSending と同じ切り替え）────────────────
 *   止まっている: Send ／ 走行中で文字が無い: Stop ／ 走行中で文字がある: Inject（＋ Interrupt）。 */
function bwPaintSend() {
  const d = BW.dom;
  if (!d.send) return;
  const on = isRunning(BW.state);
  const has = !!(d.q && d.q.value.trim());
  const dv = typeof curDev === "function" ? curDev() : null;
  if (d.now) d.now.hidden = !(on && has);
  if (on && has) { d.send.classList.remove("is-stop"); d.send.textContent = "Inject"; d.send.disabled = false; return; }
  d.send.classList.toggle("is-stop", on);
  d.send.textContent = on ? "Stop" : "Send";
  d.send.setAttribute("aria-label", on ? "stop generating" : "Send");
  d.send.disabled = !on && !(dv && dv.online);
}

/* ── 質問（ask）: デスクトップの showAsk と同じ箱（.askbox）を入力欄の直前に ── */
function bwRenderAsk(s, d) {
  const host = d.composer && d.composer.parentNode;
  if (!host) return;
  let box = document.getElementById("ask-box");
  if (!s.ask) { if (box) box.remove(); return; }
  const wantKey = s.ask.id + "|" + s.ask.options.join("|") + "|" + (s.ask.expired ? "x" : "");
  if (box && box.__key === wantKey) return;
  if (box) box.remove();
  box = document.createElement("div");
  box.className = "askbox"; box.id = "ask-box"; box.__key = wantKey;
  const q = document.createElement("p");
  q.className = "askbox__q"; q.textContent = s.ask.question;
  box.appendChild(q);
  const row = document.createElement("div");
  row.className = "askbox__row";
  s.ask.options.forEach((o, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "askbox__opt"; b.disabled = !!s.ask.expired;
    const label = document.createElement("span");
    label.className = "askbox__optlabel"; label.textContent = String(o);
    b.appendChild(label);
    const desc = String(s.ask.descriptions[i] || "").trim();
    if (desc) {
      const dd = document.createElement("span");
      dd.className = "askbox__optdesc"; dd.textContent = desc;
      b.appendChild(dd);
      b.title = `${o} — ${desc}`;
    }
    b.addEventListener("click", () => bwAnswer(o));
    row.appendChild(b);
  });
  const free = document.createElement("input");
  free.type = "text"; free.className = "askbox__free"; free.placeholder = "type your own";
  free.disabled = !!s.ask.expired;
  free.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.isComposing && free.value.trim()) bwAnswer(free.value.trim());
  });
  row.appendChild(free);
  box.appendChild(row);
  host.insertBefore(box, d.composer);
}
