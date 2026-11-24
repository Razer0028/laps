/* BLACKWALL Remote — 会話の畳み方（webui/app.js の 24〜450 行の写し・2026-10-10）。
 * ★描画は view.js（デスクトップの部品＝desk.js で描く）。道具の分類（toolKind・toolWhat）と clip は desk.js が持つ。
 * ★畳み方を 2 本にしない: 直すときは webui/app.js と同じ所を同じように直す（ここは写し）。 */
"use strict";
/* ── 小道具 ────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
/* ── 走行状態の 1 行（runbar）の部品 ────────────────────────────
 * app/ui/app.js（Tauri 版）と**同じ値**を使う。語が替わっても右の列が動かないよう、
 * 幅は styles.css の min-width と対にしてある（run_smoke.js が突き合わせる）。 */
const RUN_VERBS = [
  "Nebulizing", "Ruminating", "Percolating", "Untangling", "Marshalling", "Divining",
  "Forging", "Reticulating", "Coalescing", "Triangulating", "Distilling", "Extrapolating",
  "Tempering", "Annealing", "Kindling", "Smelting", "Surveying", "Sifting",
  "Threading", "Unspooling", "Tracing", "Calibrating", "Deliberating", "Mulling",
  "Pondering", "Puzzling", "Musing", "Cogitating", "Synthesizing", "Deducing",
];
const RUN_MARK_SEQ = ["·", "✢", "✳", "✶", "✻", "✽"];
const RUN_MARK_MS = 120;
const RUN_VERB_MS = 7000;
function runMarkAt(ms) {
  const L = RUN_MARK_SEQ.length, period = 2 * L - 2;
  const p = Math.floor(Math.max(0, Number(ms) || 0) / RUN_MARK_MS) % period;
  return RUN_MARK_SEQ[p < L ? p : period - p];
}
function fmtRunElapsed(secs) {
  const s = Math.max(0, Math.floor(Number(secs) || 0));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}
function fmtRunTokens(n, approx = true) {
  const v = Math.max(0, Math.floor(Number(n) || 0));
  let k = String(v);
  if (v >= 1000) { const t = v / 1000; k = t >= 99.95 ? `${Math.round(t)}k` : `${t.toFixed(1)}k`; }
  return `↓ ${approx ? "~" : ""}${k} tokens`;
}
function reducedMotion() {
  try { return !!(globalThis.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches); }
  catch (e) { return false; }
}

/* ── 通知の分類 ────────────────────────────────────────────────
 * ★内部の識別子は画面に出さない（term/render.py 項目 17）。文言が無い kind は
 *   「数えるだけ」にして、`kind` の文字列そのものを利用者に見せない。
 * ★表は term/render.py `_NOTICE` と同じ分類に揃える（片方だけ直すと必ず食い違う）。 */
const NOTICE_LEVEL = {
  phase_start: "dim", phase_done: "info", phase_retry: "bad", phase_failed: "bad",
  phase_skip: "dim", phase_plan: "dim", phase_guidance_dropped: "bad",
  phase_hold_restored: "bad", harness: "dim",
  hook_error: "bad", handoff_gap: "bad", cancel_error: "bad", checkpoint_error: "bad",
  answer_guard: "bad", user_stop: "info", verifier: "bad", red_tests: "bad",
  backend_proxy: "bad", ws_guard: "bad", host_scope: "bad", invent: "bad", contradiction: "bad", offline: "bad",
  engine_retry: "info", gen_truncated: "info", child_compacted: "info", compacting: "info", doom_loop: "info", compaction_failed: "bad",
  session: "info", ask_expired: "bad", injected: "info",
  inject_ignored: "bad", inject_failed: "bad", queue_cleared: "info", request: "info",
  context_warn: "info", context_compacted: "dim", context_reset: "bad", context_exhausted: "bad",
  degraded: "bad", crash: "bad", fatal: "bad", rejected: "bad",
  bad_request: "bad", unknown_command: "bad", cancel_detail: "dim", cancel_applied: "dim",
  subagent: "dim", changes: "info",
};
/* 画面に出さない kind。状態は runbar と meta が持つ（会話の中に二重に書かない）。 */
const NOTICE_HIDDEN = new Set(["ready", "pong", "run_start", "queued", "status", "shutdown",
                               "heartbeat", "cancel", "cancelled", "session_id"]);
/* この印で始まる通知は「いま書いた下書きは捨てて書き直す」の合図（本文を捨ててよいのはここだけ）。 */
const RESTART_MARKS = ["[fabrication ", "[pushback ", "[reset ", "[verifier ",
                       "[red_tests ", "[rerun ", "[invent ", "[contradiction "];
/* `harness` でくくられた通知のうち、ハーネスが割り込んだ部類（目立たせる）。 */
const GATE_MARKS = ["[fabrication ", "[pushback ", "[reset ", "[verifier ", "[red_tests ",
                    "[rerun ", "[host_scope ", "[invent ", "[contradiction ", "[offline ", "[search_limit ",
                    "[loop]", "[Ω-", "[予算]", "[検査]"];
const startsWithAny = (s, marks) => marks.some((m) => String(s || "").startsWith(m));

/* ── 状態 ────────────────────────────────────────────────────── */
/* 文字からトークン数を見積もる（think.rs の est_tokens と同じ式）。★2026-11-16 Qwen3.8 の tokenizer で測り直し:
 * 英語 4.0 字/トークン・コード 3.2・日本語 1.7。前は全部 2.2 で割り、英語の思考を 1.8 倍に数えていた。
 * かな・漢字・全角・ハングルは 1 字 0.7、それ以外は 0.27。 */
function estTok(text) {
  let wide = 0, other = 0;
  for (const ch of String(text || "")) {
    const u = ch.codePointAt(0);
    if ((u >= 0x3000 && u <= 0x30ff) || (u >= 0x3400 && u <= 0x9fff) || (u >= 0xff00 && u <= 0xffef) || (u >= 0xac00 && u <= 0xd7af)) wide++;
    else other++;
  }
  return wide * 0.7 + other * 0.27;
}

function bwInitState() {
  return {
    runId: null, lastSeq: 0, phase: "idle",
    timeline: [], openBundleKey: null,
    ask: null, note: null, noteBad: false,
    startedMs: 0, endedMs: null, chars: 0, verbSeed: 0, runEvents: 0,
    heartbeat: null, ready: null, queue: null, stopRequestedMs: null,
    outcome: null,
    // 数える物（画面の帯に出す。握り潰さない）
    duplicates: 0, dropped: 0, gaps: 0, malformed: 0, unknown: {}, events: 0,
  };
}
const isRunning = (s) => ["queued", "generating", "tooling", "asking", "stopping"].includes(s.phase);

function bwParse(line) {
  try { const o = JSON.parse(line); return (o && typeof o === "object") ? o : null; }
  catch (e) { return null; }
}

/* ── 事象 1 つ → 状態 1 つ（純関数）─────────────────────────── */
function bwReduce(state, ev, nowMs) {
  const type = String(ev.type || "");
  const kind = String(ev.kind || "");
  const seq = Number(ev.seq || 0);
  const isReady = (type === "ready") || (type === "notice" && kind === "ready");

  // 別の依頼 id が**明示されている**行だけ捨てる（id の無い行は必ず通す）。
  if (ev.id && state.runId && ev.id !== state.runId) {
    return { ...state, dropped: state.dropped + 1 };
  }
  let s = { ...state, events: state.events + 1 };
  // ★この走行に属する事象だけを数える（状態行の "Starting" は「まだ**この依頼**について
  //   何も届いていない」という事実の表示。全体の事象数で判定すると、前の走行の ready を
  //   数えてしまい、送った直後から Gerund が回り出す＝**嘘の忙しさ**になる）。
  if (ev.id && s.runId && ev.id === s.runId) s.runEvents = state.runEvents + 1;
  if (isReady) {
    s.lastSeq = 0;                       // serve が入れ替わると seq は 1 に戻る
  } else if (seq > 0 && s.lastSeq > 0 && seq <= s.lastSeq) {
    return { ...state, duplicates: state.duplicates + 1 };
  }
  if (seq > 0) {
    if (s.lastSeq > 0 && seq > s.lastSeq + 1) s.gaps += 1;
    s.lastSeq = Math.max(s.lastSeq, seq);
  }
  const isHeartbeat = (type === "notice" && kind === "heartbeat");
  if (!isHeartbeat) s.heartbeat = null;  // 本物の事象が来たら心拍の行は消える

  switch (type) {
    case "token": return onBody(s, ev, "answer", nowMs);
    case "think": return onBody(s, ev, "think", nowMs);
    // 手番の番号を数える（走行の線: 1 手番＝1 行・思考と道具を同じ行にまとめる・2026-11-02）
    case "iter_start": return { ...closeThink(closeAnswer(s)), iterN: (s.iterN || 0) + 1 };
    case "tool_call": case "tool_start":
      return onToolCall(s, ev.call, String(ev.name || ""), ev.input, seq, nowMs);
    case "tool_result": return onToolResult(s, ev, nowMs);
    case "ask": return onAsk(s, ev, nowMs);
    case "done": return onDone(s, ev, nowMs);
    case "error": return onError(s, ev);
    case "notice": return onNotice(s, ev, kind, nowMs);
    case "ready": return { ...closeAll(s), phase: s.phase === "idle" ? "idle" : s.phase };
    default:
      s.unknown = { ...s.unknown, [type]: (s.unknown[type] || 0) + 1 };
      return s;
  }
}

/* 本文と思考。★反復の頭（iter_start）でも**本文は消さない**——段落を切るだけ
 *   （消していたせいで、道具を呼ぶたびに報告が消えていた前歴がある）。 */
function onBody(s, ev, kindName, nowMs) {
  const text = String(ev.text || "");
  // ★道具の束を閉じるのは「本文が来たとき」だけ（デスクトップの answer() と同じ・2026-10-11 オーナー「デスクトップアプリ版だと
  //   まとまって表示されているけど、ウェブ版だとひとつづつになってしまっている」）。旧: 思考が来るたびに束を閉じていたので、
  //   道具の間に考える Gemma では 1 本ずつの束になっていた。中身の無い本文（記録の assistant が空・空白だけの断片）も閉じない。
  const lastNow = s.timeline[s.timeline.length - 1];
  const streamingAnswer = lastNow && lastNow.kind === "answer" && lastNow.streaming;
  if (kindName === "answer" && !text.trim() && !streamingAnswer) return s;
  let out = kindName === "answer" ? closeThink(closeBundle(s)) : closeAnswer(s);
  const last = out.timeline[out.timeline.length - 1];
  if (last && last.kind === kindName && last.streaming) {
    out = replaceLast(out, { ...last, text: last.text + text });
  } else {
    out = append(out, { kind: kindName, key: `${kindName[0]}${ev.seq}`, text, streaming: true, iter: out.iterN || 0, startedMs: nowMs });
  }
  out.chars += text.length;
  out.tok = (out.tok || 0) + estTok(text);
  out.note = null;
  if (out.phase !== "stopping") out.phase = "generating";
  if (!out.startedMs) out.startedMs = nowMs;
  return out;
}

function onToolCall(s, call, name, input, seq, nowMs) {
  let out = closeThink(closeAnswer(s));
  out.note = null;
  const item = {
    call: call || `seq-${seq}`, name, input: input || {}, status: "running", iter: out.iterN || 0,
    resultKind: "", chars: 0, truncated: false, result: "", summary: null,
    startedMs: nowMs, endedMs: null,
  };
  const open = openBundle(out);
  if (open) {
    out = replaceItem(out, open.key, { ...open, tools: open.tools.concat([item]) });
  } else {
    out = append(out, { kind: "tools", key: `b${seq}`, tools: [item] });
    out.openBundleKey = `b${seq}`;
  }
  out.phase = "tooling";
  if (!out.startedMs) out.startedMs = nowMs;
  return out;
}

/* `kind=empty` は**失敗ではない**（該当なし）。赤い失敗として出すと事故に見える。 */
function statusOf(ok, kind) {
  if (kind === "empty") return "empty";
  if (kind === "denied") return "denied";
  return ok ? "ok" : "failed";
}

function onToolResult(s, ev, nowMs) {
  const call = ev.call || null, name = String(ev.name || "");
  const status = statusOf(ev.ok !== false, String(ev.kind || ""));
  const patch = {
    status, resultKind: String(ev.kind || ""), chars: Number(ev.chars || 0),
    truncated: !!ev.truncated, result: String(ev.result == null ? "" : ev.result),
    summary: ev.summary || null, endedMs: nowMs,
  };
  let out = { ...s };
  const found = findRunningTool(out, call, name);
  if (!found) {
    // 呼び出しを見ていない結果（再接続で取りこぼした等）。**捨てない**
    // ——結果だけ消えると「道具が動いていない」に見える。
    const item = { call: call || `seq-${ev.seq}`, name, input: {}, startedMs: nowMs, iter: out.iterN || 0, ...patch };
    const open = openBundle(out);
    if (open) out = replaceItem(out, open.key, { ...open, tools: open.tools.concat([item]) });
    else {
      out = append(out, { kind: "tools", key: `b${ev.seq}`, tools: [item] });
      out.openBundleKey = `b${ev.seq}`;
    }
  } else {
    const [bundle, tool] = found;
    const updated = { ...tool, ...patch };
    out = replaceItem(out, bundle.key, {
      ...bundle,
      tools: bundle.tools.map((t) => (t.call === updated.call && t.status === "running" ? updated : t)),
    });
  }
  const open = openBundle(out);
  const stillRunning = !!(open && open.tools.some((t) => t.status === "running"));
  if (out.phase === "tooling" && !stillRunning) out.phase = "generating";
  return out;
}

function onAsk(s, ev, nowMs) {
  return {
    ...s, phase: "asking",
    ask: {
      id: String(ev.ask || ev.id || ""), question: String(ev.question || ""),
      options: Array.isArray(ev.options) ? ev.options.map(String) : [],
      descriptions: Array.isArray(ev.descriptions) ? ev.descriptions.map(String) : [],
      receivedMs: nowMs, expired: false, sent: false,
    },
  };
}

function onDone(s, ev, nowMs) {
  let out = orphanRunningTools(closeAll(s));
  const text = String(ev.text || "");
  // 本文が 1 文字も流れていないときだけ `text` を使う（二重に出さない）。
  if (!out.timeline.some((i) => i.kind === "answer") && text.trim()) {
    out = append(out, { kind: "answer", key: `a${ev.seq}`, text, streaming: false });
  }
  const outcome = String(ev.outcome || "unknown");
  // ★2026-09-08 監査（clients high）: `accepted_after_pushback`（差し戻しを経て受理）が無く、
  //   成功した走行を「✗ 未完了」と描いていた。term（render._ACCEPTED）/Windows（wire.rs ACCEPTED）と同じ集合にする。
  const ACCEPTED = ["accepted", "accepted_after_pushback", "user_stop"];
  const accepted = ACCEPTED.includes(outcome) || ["done", "ok", "answered"].includes(outcome);
  const cancelled = outcome === "cancelled";
  const info = {
    outcome, accepted, cancelled,
    secs: Number(ev.secs || 0), iters: Number(ev.iters || 0),
    // ★tool_calls は serve では道具ごとの数の表（dict）。Number にすると NaN になるので、数でなければ数え直す。
    tools: typeof ev.tool_calls === "number" ? ev.tool_calls : countTools(out), tokS: Number(ev.tok_s || 0),
    model: String(ev.model || ""), files: Array.isArray(ev.files_written) ? ev.files_written : [],
    // デスクトップの meter（fmtStat）と同じ材料。serve の done にそのまま載っている。
    stats: { prompt_tokens: Number(ev.prompt_tokens || 0), answer_tokens: Number(ev.answer_tokens || 0),
             think_tokens: Number(ev.think_tokens || 0), tok_s: Number(ev.tok_s || 0), secs: Number(ev.secs || 0) },
  };
  out = append(out, { kind: "outcome", key: `o${ev.seq}`, info });
  out.phase = (accepted || cancelled) ? "done" : "failed";
  out.outcome = info;
  out.ask = null; out.queue = null; out.endedMs = nowMs; out.stopRequestedMs = null;
  return out;
}

/* ★`error` で画面を閉じない。`done` は必ず 1 つ出る（serve.py の契約）。 */
function onError(s, ev) {
  const text = errorLine(String(ev.kind || ""), String(ev.message || ""));
  const out = append(s, { kind: "notice", key: `n${ev.seq}`, level: "bad", text });
  out.note = text; out.noteBad = true;
  return out;
}
function errorLine(kind, message) {
  if (kind === "rejected") return `受け付けられませんでした: ${message}`;
  if (kind === "bad_request") return `送った行が読めませんでした: ${message}`;
  if (kind === "fatal" || kind === "crash") return `本体が落ちました: ${message}`;
  return message || "エラーが起きました";
}

function onNotice(s, ev, kind, nowMs) {
  const msg = String(ev.message || ev.text || "");
  if (kind === "heartbeat") {
    const stage = String(ev.stage || "推論機");
    const q = Math.max(0, Number(ev.quiet_secs || 0));
    // 「背景の仕事」は本体が手番を閉じずに背景の仕事の終わりを待っている（推論機は待っていない・2026-11-12）。
    const text = stage.startsWith("道具 ") ? `${stage.slice(3)} running (${q}s silent)`
      : stage === "背景の仕事" ? `Waiting for background jobs (${q}s)` : `Waiting for the model (${q}s)`;
    return { ...s, heartbeat: { text, quietSecs: q, stage } };
  }
  if (kind === "ready") {
    // ★2026-09-08 監査（serve/gateway high）: ready は「新しい serve が立った＝いまの走行はもう存在しない」の合図。
    //   旧は phase を触らず、猶予切れ→再接続→新 serve の ready の後も「走行中」のまま送信ボタンが押せなかった
    //   （journal に実例: 19:22 切断 → 19:32 猶予切れ → 4001 → 再接続 → ready）。走行中なら失われた締めを 1 行入れ idle に戻す。
    let base = s;
    if (isRunning(s)) {
      base = append(closeAll(s), { kind: "outcome", key: `lost${ev.seq || 0}`, info: {
        outcome: "lost", accepted: false, cancelled: false, secs: 0, iters: 0, tools: countTools(s), tokS: 0, model: "", files: [] } });
      base.phase = "idle"; base.runId = null;
      base.note = "本体が入れ替わったので、この走行は途中で終わりました（送り直せます）"; base.noteBad = true;
    }
    const out = closeAll(base);
    out.ready = {
      model: String(ev.model || ""), backend: String(ev.backend || ""),
      workspace: String(ev.workspace || ""), policy: String(ev.policy || ""),
      tools: Array.isArray(ev.tools) ? ev.tools.length : 0,
      busy: ev.busy || null, queued: Number(ev.queued || 0),
    };
    return out;
  }
  if (kind === "run_start") {
    return { ...s, phase: s.phase === "stopping" ? s.phase : "generating",
             queue: null, outcome: null, endedMs: null,
             startedMs: s.startedMs || nowMs };
  }
  if (kind === "queued") {
    return { ...s, phase: "queued",
             queue: { position: Number(ev.position || 0), message: msg || "順番を待っています" } };
  }
  if (kind === "cancel" || kind === "cancelled") {
    return { ...s, phase: (s.phase === "done" || s.phase === "failed") ? s.phase : "stopping",
             note: msg || "止めています…", noteBad: false,
             stopRequestedMs: s.stopRequestedMs || nowMs };
  }
  if (kind === "ask_expired") {
    const out = appendNotice(s, ev, "bad", msg || "この質問はもう時間切れです");
    if (out.ask) out.ask = { ...out.ask, expired: true };
    return out;
  }
  // 差し戻し系は kind ではなく**本文の印**で来る（`[pushback 1] …`）。
  if (startsWithAny(msg, RESTART_MARKS)) {
    const out = appendNotice(discardDraft(s), ev, "bad", msg);
    out.note = msg; out.noteBad = true;
    return out;
  }
  if (kind === "ws_guard" && !msg) {
    // ★payload に message が無い（tool/target/n だけ）。ここで組み立てないと、
    //   関門が働いた 1 行が画面に出ない（実機で実際にそうなっていた）。
    const text = `作業場の外を変える ${ev.tool || "?"} を止めました（${ev.n || 1} 回目）: ${ev.target || ""}`;
    const out = appendNotice(s, ev, "bad", text);
    out.note = text; out.noteBad = true;
    return out;
  }
  if (kind === "inject_taken") return s;   // 本体が差し込みを読んだ（2026-11-16・差し込みは別に出ているので重ねない）
  if (kind === "injected" && msg) {
    // ★割り込み（アプリ・スマホ・ウェブのどこから送った物でも serve がこの通知で返す）。
    //   ウェブ版はデスクトップと同じく、吹き出しの中のいまの位置に「interjected: …」で出す（view.js）。
    return append(closeAll(s), { kind: "inject", key: `n${ev.seq}`, text: msg, force: !!ev.force });
  }
  if (NOTICE_HIDDEN.has(kind)) return s;
  let level = NOTICE_LEVEL[kind] || "dim";
  if (kind === "harness" && startsWithAny(msg, GATE_MARKS)) level = "bad";
  if (kind === "subagent" && msg.includes("✗")) level = "bad";
  if (!msg) {
    // 文言の無い知らない kind は**握り潰さず数える**（内部の識別子は出さない）。
    return { ...s, unknown: { ...s.unknown, [`notice:${kind}`]: (s.unknown[`notice:${kind}`] || 0) + 1 } };
  }
  const out = appendNotice(s, ev, level, msg);
  if (level === "bad") { out.note = msg; out.noteBad = true; }
  return out;
}

/* ── 状態の小道具（どれも新しい配列／物を返す）───────────────── */
function append(s, item) { return { ...s, timeline: s.timeline.concat([item]) }; }
function appendNotice(s, ev, level, text) {
  return append(s, { kind: "notice", key: `n${ev.seq}`, level, text });
}
function replaceLast(s, item) {
  return { ...s, timeline: s.timeline.slice(0, -1).concat([item]) };
}
function replaceItem(s, key, item) {
  return { ...s, timeline: s.timeline.map((i) => (i.key === key ? item : i)) };
}
function openBundle(s) {
  if (!s.openBundleKey) return null;
  const found = s.timeline.filter((i) => i.key === s.openBundleKey);
  return found.length ? found[found.length - 1] : null;
}
function closeBundle(s) { return s.openBundleKey ? { ...s, openBundleKey: null } : s; }
function closeStream(s, kindName) {
  const last = s.timeline[s.timeline.length - 1];
  if (!last || last.kind !== kindName || !last.streaming) return s;
  if (!last.text.trim()) return { ...s, timeline: s.timeline.slice(0, -1) };  // 空の箱は残さない
  return replaceLast(s, { ...last, streaming: false });
}
const closeAnswer = (s) => closeStream(s, "answer");
const closeThink = (s) => closeStream(s, "think");
const closeAll = (s) => closeBundle(closeThink(closeAnswer(s)));
function discardDraft(s) {
  const last = s.timeline[s.timeline.length - 1];
  if (last && last.kind === "answer" && last.streaming && last.text.trim()) {
    return { ...s, timeline: s.timeline.slice(0, -1) };
  }
  return closeAnswer(s);
}
function findRunningTool(s, call, name) {
  for (let i = s.timeline.length - 1; i >= 0; i--) {
    const b = s.timeline[i];
    if (!b || b.kind !== "tools") continue;
    for (let j = b.tools.length - 1; j >= 0; j--) {
      const t = b.tools[j];
      if (t.status !== "running") continue;
      if (call ? t.call === call : t.name === name) return [b, t];
    }
  }
  return null;
}
/* 結果が来ないまま締めが来た道具。**実行中のまま永久に残さない。** */
function orphanRunningTools(s) {
  return {
    ...s,
    timeline: s.timeline.map((i) => {
      if (i.kind !== "tools" || !i.tools.some((t) => t.status === "running")) return i;
      return { ...i, tools: i.tools.map((t) => (t.status === "running" ? { ...t, status: "orphan" } : t)) };
    }),
  };
}
function countTools(s) {
  return s.timeline.reduce((n, i) => n + (i.kind === "tools" ? i.tools.length : 0), 0);
}
