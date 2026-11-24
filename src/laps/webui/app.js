/* BLACKWALL — ブラウザだけで使う 1 ページ（2026-09-07）
 *
 * ★なぜ在るか（REPORTS/webui-chrome-plan-20260907.md）: 実機（Windows）は Python の
 *   subprocess で固まる欠陥を 1 日で 3 種踏んだ。**Windows 側の Python を通らない経路**が
 *   あれば、オーナーはブラウザだけで同じエージェントを使え、こちらは Claude in Chrome で
 *   同じ画面を実際に操作して確かめられる。走るのは 開発機 の serve、推論は 推論機。
 *
 * 作り（変えない）:
 *   - 枠組みを入れない。素の DOM だけ。CDN も外部フォントも引かない（外が落ちた日に画面が壊れる）。
 *   - 線の上を流れるのは **serve の 1 行 = 1 事象**（gateway は変換しない）。
 *     解析はここ 1 か所（`bwReduce`）で、Android の `EventReducer.kt` と同じ約束にする:
 *       * 事象 1 つで状態が 1 つ進む**純関数**（DOM も時計も持たない・時刻は引数）
 *       * `seq` で重複を捨てる（再接続の再送で二重に描かない）
 *       * `error` で画面を閉じない（`done` は必ず 1 つ出る）
 *       * 本文を捨てるのは差し戻しの印が来たときだけ
 *   - 描画（`bwRender`）は状態を見て DOM を当てるだけ。**同じ item は作り直さない**
 *     （identity 比較）＝ 1 トークンごとに全部を組み直さない。
 *
 * 試験: `node run_smoke.js`（偽 DOM で boot→受信→描画まで通す）。
 *       `tests/test_gateway_web_static.py`（配信と経路の正規化）。
 */
"use strict";

/* ── 小道具 ────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
const clip = (s, n) => (String(s).length > n ? String(s).slice(0, n) + "…" : String(s));

/* ── 道具の分類と一言（app/ui/app.js の写し。**推測で埋めない**） ── */
const TOOL_KIND = {
  Read: "read", Grep: "read", Glob: "read", WebFetch: "read",
  WebSearch: "read", RemoteRead: "read", SystemState: "read", BashOutput: "read",
  TaskList: "read", TaskGet: "read", ws_list: "read", ws_read: "read",
  Write: "write", Edit: "write", ApplyDiff: "write", NotebookEdit: "write",
  TodoWrite: "write", TaskCreate: "write", TaskUpdate: "write", TaskStop: "write",
  ws_write: "write", ws_edit: "write",
  Bash: "exec", Task: "exec", Desktop: "exec",
  AskUserQuestion: "ask",
};
const KIND_LABEL = { read: "read", write: "edit", exec: "run", ask: "ask", unknown: "?" };
const toolKind = (name) => TOOL_KIND[String(name || "")] || "unknown";

/* 「何をしたか」の 1 行。★実行系はコマンド**全文**（見えないまま実行されるのが一番わるい）。 */
function toolWhat(name, args) {
  const a = args && typeof args === "object" ? args : {};
  const pick = (...keys) => { for (const k of keys) if (typeof a[k] === "string" && a[k]) return a[k]; return ""; };
  switch (String(name || "")) {
    case "Bash": return pick("command");
    case "BashOutput": return a.pid != null ? `pid ${a.pid}` : "";
    case "Grep": { const p = pick("pattern"), w = pick("path", "glob"); return w ? `${p}  in ${w}` : p; }
    case "Glob": return pick("pattern", "path");
    case "WebFetch": return pick("url");
    case "WebSearch": return pick("query", "q");
    case "SystemState": return pick("verb");
    case "RemoteRead": return [pick("host"), pick("verb"), pick("arg")].filter(Boolean).join(" ");
    case "Task": case "TaskCreate": return pick("description", "subject");
    case "TodoWrite": return Array.isArray(a.todos) ? `${a.todos.length} items` : "";
    default: break;
  }
  const p = pick("file_path", "rel", "path", "notebook_path", "url", "pattern", "q", "query", "command");
  if (p) return p;
  const first = Object.keys(a).find((k) => typeof a[k] === "string" && a[k]);
  if (first) return a[first];
  const json = JSON.stringify(a || {});
  return json === "{}" ? "" : json;
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
  subagent: "dim",
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
    case "iter_start": return closeThink(closeAnswer(s));
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
  let out = kindName === "answer" ? closeThink(closeBundle(s)) : closeAnswer(closeBundle(s));
  const last = out.timeline[out.timeline.length - 1];
  if (last && last.kind === kindName && last.streaming) {
    out = replaceLast(out, { ...last, text: last.text + text });
  } else {
    out = append(out, { kind: kindName, key: `${kindName[0]}${ev.seq}`, text, streaming: true });
  }
  out.chars += text.length;
  out.note = null;
  if (out.phase !== "stopping") out.phase = "generating";
  if (!out.startedMs) out.startedMs = nowMs;
  return out;
}

function onToolCall(s, call, name, input, seq, nowMs) {
  let out = closeThink(closeAnswer(s));
  out.note = null;
  const item = {
    call: call || `seq-${seq}`, name, input: input || {}, status: "running",
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
    const item = { call: call || `seq-${ev.seq}`, name, input: {}, startedMs: nowMs, ...patch };
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
    tools: Number(ev.tool_calls || countTools(out)), tokS: Number(ev.tok_s || 0),
    model: String(ev.model || ""), files: Array.isArray(ev.files_written) ? ev.files_written : [],
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
    const text = stage.startsWith("道具 ")
      ? `${stage} を実行中（${q} 秒 応答なし）` : `${stage}の応答を待っています（${q} 秒）`;
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

/* ── 画面（状態 → DOM）─────────────────────────────────────── */
const BW = {
  state: bwInitState(), dom: {}, cache: new Map(), ws: null, token: "", sid: "",
  runId: null, session: null, sendSeq: 0, tries: 0, timer: null, retry: null,
  conn: "つないでいます…", connBad: false, closing: false, booted: false,
};

function bwRender() {
  const s = BW.state, d = BW.dom;
  if (!d.thread) return;
  bwRenderThread(s, d);
  bwPaintRunbar();
  bwRenderAsk(s, d);
  // 送信まわり。走行中は送れない（serve は待ち行列に積むが、押した本人には見えない）。
  if (d.send) d.send.disabled = isRunning(s) || !BW.ws;
  if (d.stop) d.stop.hidden = !isRunning(s);
  if (d.note) {
    const text = s.note || (s.queue ? s.queue.message : "") || "";
    d.note.textContent = text;
    d.note.classList.toggle("is-bad", !!s.note && s.noteBad);
  }
  if (d.conn) {
    d.conn.textContent = BW.conn;
    d.conn.classList.toggle("is-bad", BW.connBad);
  }
  if (d.meta) d.meta.textContent = bwMetaLine(s);
}

function bwMetaLine(s) {
  const bits = [];
  if (s.ready) {
    if (s.ready.model) bits.push(s.ready.model);
    if (s.ready.workspace) bits.push(s.ready.workspace);
    if (s.ready.tools) bits.push(`道具 ${s.ready.tools}`);
  }
  if (BW.version) bits.push(`v${BW.version}`);
  // 取りこぼし・重複・知らない型は**数だけでも出す**（送り手の変更に気づける）。
  const odd = [];
  if (s.gaps) odd.push(`欠 ${s.gaps}`);
  if (s.duplicates) odd.push(`重 ${s.duplicates}`);
  if (s.malformed) odd.push(`壊 ${s.malformed}`);
  const un = Object.keys(s.unknown).length;
  if (un) odd.push(`未知 ${un}`);
  if (odd.length) bits.push(odd.join(" "));
  return bits.join(" · ");
}

function bwRenderThread(s, d) {
  const keys = new Set(s.timeline.map((i) => i.key));
  for (const [k, ent] of Array.from(BW.cache)) {
    if (!keys.has(k)) { ent.el.remove(); BW.cache.delete(k); }
  }
  for (const item of s.timeline) {
    const ent = BW.cache.get(item.key);
    if (ent && ent.item === item) continue;      // 中身が変わっていない＝作り直さない
    const node = bwBuildItem(item);
    if (ent) { ent.el.replaceWith(node); BW.cache.set(item.key, { item, el: node }); }
    else { d.thread.appendChild(node); BW.cache.set(item.key, { item, el: node }); }
  }
  if (s.timeline.length && d.hello && d.hello.parentNode) { d.hello.remove(); d.hello = null; }
  bwScroll(d);
}

function bwScroll(d) {
  const box = d.main;
  if (!box) return;
  // 下まで読んでいる人だけ追従させる（さかのぼっている最中に引き戻さない）。
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  if (near) box.scrollTop = box.scrollHeight;
}

function bwBuildItem(item) {
  switch (item.kind) {
    case "user": return bwBuildUser(item);
    case "answer": return bwBuildAnswer(item);
    case "think": return bwBuildThink(item);
    case "tools": return bwBuildTools(item);
    case "notice": return bwBuildNotice(item);
    case "outcome": return bwBuildOutcome(item);
    default: return el("div", "msg", "");
  }
}

function bwBuildUser(item) {
  const n = el("div", "msg msg--me");
  n.appendChild(el("div", "who", "あなた"));
  n.appendChild(el("div", "body", item.text));
  return n;
}

function bwBuildAnswer(item) {
  const n = el("div", "msg msg--bot" + (item.streaming ? " is-live" : ""));
  n.appendChild(el("div", "who", "L.A.P.S"));
  const body = el("div", "body");
  bwMarkdownish(body, item.text);
  n.appendChild(body);
  return n;
}

function bwBuildThink(item) {
  const n = el("details", "think");
  // ★既定で開く（2026-09-21 オーナー指示）。畳んでおくと「推論しているのか
  //   どうか分からない」＝折りたたみは見えていないのと同じ。
  n.open = true;
  const head = el("summary", "think__head");
  head.appendChild(el("span", "think__label", "考えていること"));
  head.appendChild(el("span", "think__meter num", `${item.text.length} 字`));
  n.appendChild(head);
  n.appendChild(el("pre", "think__body", item.text));
  return n;
}

const TOOL_STATE_LABEL = {
  running: "…", ok: "✓", failed: "✗", empty: "該当なし", denied: "断られた", orphan: "結果なし",
};
function bwBuildTools(item) {
  const n = el("div", "wf");
  for (const t of item.tools) n.appendChild(bwBuildTool(t));
  return n;
}
function bwBuildTool(t) {
  const kind = toolKind(t.name);
  // ★赤の配分: 失敗はいつも赤。「結果が返らなかった」は**読む道具では赤くしない**
  //   （読めなかっただけ）。変える・実行・不明のときだけ赤にする。
  const bad = t.status === "failed" || (t.status === "orphan" && kind !== "read");
  const box = el("details", "tool" + (bad ? " is-bad" : ""));
  const head = el("summary", "tool__head");
  head.appendChild(el("span", `tool__kind tool__kind--${kind}`, KIND_LABEL[kind]));
  head.appendChild(el("span", "tool__name", t.name || "?"));
  const what = toolWhat(t.name, t.input);
  const whatEl = el("span", kind === "exec" ? "tool__what tool__what--full" : "tool__what", what);
  head.appendChild(whatEl);
  const state = el("span", "tool__state", TOOL_STATE_LABEL[t.status] || t.status);
  head.appendChild(state);
  box.appendChild(head);
  const body = el("div", "tool__body");
  const args = JSON.stringify(t.input || {}, null, 1);
  if (args && args !== "{}") {
    body.appendChild(el("div", "tool__seg-h", "渡したもの"));
    body.appendChild(el("pre", "tool__pre", clip(args, 4000)));
  }
  if (t.result) {
    body.appendChild(el("div", "tool__seg-h", `返ってきたもの${t.truncated ? "（切り詰め）" : ""}`));
    body.appendChild(el("pre", "tool__pre", clip(t.result, 20000)));
  }
  box.appendChild(body);
  return box;
}

function bwBuildNotice(item) {
  const n = el("div", `note note--${item.level}`);
  n.appendChild(el("span", "note__text", item.text));
  return n;
}

function bwBuildOutcome(item) {
  const i = item.info;
  const n = el("div", "outcome" + (i.accepted ? " is-ok" : (i.cancelled ? "" : " is-bad")));
  const label = i.accepted ? "完了" : (i.cancelled ? "止めました" : (i.outcome === "lost" ? "途中で終了（本体が入れ替わった）" : `未完了（${i.outcome}）`));
  n.appendChild(el("span", "outcome__mark", i.accepted ? "✓" : (i.cancelled ? "■" : "✗")));
  n.appendChild(el("span", "outcome__label", label));
  const bits = [];
  if (i.secs) bits.push(`${i.secs.toFixed(1)} 秒`);
  if (i.iters) bits.push(`${i.iters} 手番`);
  if (i.tools) bits.push(`道具 ${i.tools}`);
  if (i.tokS) bits.push(`${i.tokS.toFixed(1)} tok/s`);
  n.appendChild(el("span", "outcome__meta num", bits.join(" · ")));
  return n;
}

/* 本文の最小の飾り。★描画で内容を変えない（見出し・箇条書き・コードだけ）。 */
function bwMarkdownish(host, text) {
  const lines = String(text).split("\n");
  let pre = null;
  for (const line of lines) {
    if (/^```/.test(line)) {
      if (pre) { pre = null; } else { pre = el("pre", "code"); host.appendChild(pre); }
      continue;
    }
    if (pre) { pre.textContent += (pre.textContent ? "\n" : "") + line; continue; }
    if (/^#{1,6}\s/.test(line)) { host.appendChild(el("h3", "h3", line.replace(/^#+\s*/, ""))); continue; }
    if (/^\s*[-*]\s+/.test(line)) {
      host.appendChild(el("div", "li", "• " + line.replace(/^\s*[-*]\s+/, "")));
      continue;
    }
    host.appendChild(el("p", "p", line));
  }
}

/* ── 走行状態の 1 行 ───────────────────────────────────────── */
function bwPaintRunbar() {
  const s = BW.state, d = BW.dom;
  if (!d.runbar) return;
  if (!isRunning(s)) { d.runbar.hidden = true; return; }
  d.runbar.hidden = false;
  const now = Date.now();
  const started = s.startedMs || now;
  const elapsed = Math.max(0, ((s.endedMs || now) - started) / 1000);
  const running = runningToolName(s);
  let what, mark;
  if (s.phase === "stopping") { what = "止めています"; mark = "·"; }
  else if (s.phase === "asking") { what = "返事を待っています"; mark = "·"; }
  else if (s.phase === "queued") { what = "Queued"; mark = "·"; }
  else if (running) { what = running; mark = "▸"; }        // 道具名が主役・印は静止
  else if (lastStreaming(s) === "answer") { what = "Writing"; mark = markNow(now, started); }
  else if (lastStreaming(s) === "think") { what = gerund(s, elapsed); mark = markNow(now, started); }
  else if (s.runEvents === 0) { what = "Starting"; mark = "·"; }
  else { what = gerund(s, elapsed); mark = markNow(now, started); }
  d.runbarWhat.textContent = what;
  d.runbarMark.textContent = mark;
  d.runbarTime.textContent = fmtRunElapsed(elapsed);
  d.runbarTokens.textContent = s.chars ? fmtRunTokens(Math.floor(s.chars / 1.4)) : "";
  d.runbarHint.textContent = s.heartbeat ? s.heartbeat.text : (s.queue ? s.queue.message : "");
  d.runbarHint.classList.toggle("is-quiet", !!s.heartbeat);
}
function markNow(now, started) {
  if (reducedMotion()) return "✳";     // 動きを減らす設定なら固定（幅は動かないので他は動かす）
  return runMarkAt(now - started);
}
function gerund(s, elapsedSecs) {
  const i = (s.verbSeed + Math.floor(elapsedSecs * 1000 / RUN_VERB_MS)) % RUN_VERBS.length;
  return RUN_VERBS[i] + "…";
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

/* ── 質問（ask）───────────────────────────────────────────── */
function bwRenderAsk(s, d) {
  if (!d.ask) return;
  if (!s.ask) { d.ask.hidden = true; return; }
  d.ask.hidden = false;
  if (d.askQ.textContent !== s.ask.question) d.askQ.textContent = s.ask.question;
  const wantKey = s.ask.id + "|" + s.ask.options.join("|") + "|" + (s.ask.expired ? "x" : "");
  if (d.askOpts.__key === wantKey) return;
  d.askOpts.__key = wantKey;
  d.askOpts.textContent = "";
  s.ask.options.forEach((opt, i) => {
    const b = el("button", "opt");
    b.setAttribute("type", "button");
    b.appendChild(el("span", "opt__label", opt));
    const desc = s.ask.descriptions[i];
    if (desc) b.appendChild(el("span", "opt__desc", desc));
    b.disabled = !!s.ask.expired;
    b.addEventListener("click", () => bwAnswer(opt));
    d.askOpts.appendChild(b);
  });
}

/* ── 線 ────────────────────────────────────────────────────── */
function bwLoc() {
  if (typeof location !== "undefined" && location && location.host) return location;
  return { protocol: "http:", host: "127.0.0.1:18400", search: "" };
}
// ★接頭辞（2026-09-07）: VDIR 等の逆プロキシで `/bw/web/` のように配られたとき、`/ws` を
//   ホスト直下に打つと届かない。`pathname` の `/web/` より前を接頭辞として API にも付ける。
function bwPrefix() {
  const l = bwLoc(); const pn = (l && typeof l.pathname === "string") ? l.pathname : "";
  const i = pn.indexOf("/web");
  return i > 0 ? pn.slice(0, i) : "";
}
function bwHttpBase() { const l = bwLoc(); return `${l.protocol}//${l.host}${bwPrefix()}`; }
function bwWsUrl() {
  const l = bwLoc();
  const scheme = l.protocol === "https:" ? "wss:" : "ws:";
  const since = BW.state.lastSeq > 0 ? `&since_seq=${BW.state.lastSeq}` : "";
  return `${scheme}//${l.host}${bwPrefix()}/ws?session=${encodeURIComponent(BW.sid)}`
       + `&token=${encodeURIComponent(BW.token)}${since}`;
}

function bwConnect() {
  if (BW.ws) return;
  BW.closing = false;
  let ws;
  try { ws = new WebSocket(bwWsUrl()); }
  catch (e) { bwSetConn(`つなげません（${e && e.message ? e.message : e}）`, true); return bwRetry(); }
  BW.ws = ws;
  bwSetConn(BW.tries ? `つなぎ直しています…（${BW.tries} 回目）` : "つないでいます…", BW.tries > 0);
  ws.onopen = () => { BW.tries = 0; bwSetConn("つながっています", false); bwRender(); };
  ws.onmessage = (e) => bwFeed(typeof e.data === "string" ? e.data : String(e.data));
  ws.onclose = (e) => {
    BW.ws = null;
    if (BW.closing) return;
    // ★4001 = serve が死んだ（gateway が閉じた）。1006 は合言葉違い/回線切れの両方。
    const why = (e && e.code === 4001) ? "本体が終わりました" : "切れました";
    bwSetConn(`${why}——つなぎ直します`, true);
    bwRetry();
  };
  ws.onerror = () => { /* close が続けて来るので、ここでは何もしない */ };
}
function bwRetry() {
  if (BW.retry) return;
  const wait = [1, 2, 4, 8, 15][Math.min(BW.tries, 4)] * 1000;
  BW.tries += 1;
  BW.retry = setTimeout(() => { BW.retry = null; bwConnect(); }, wait);
}
function bwSetConn(text, bad) { BW.conn = text; BW.connBad = !!bad; bwRender(); }

function bwSendLine(obj) {
  if (!BW.ws || BW.ws.readyState !== 1) {
    // ★送れなかったら**そう言う**（黙って溜めない。溜めて後から投げると同じ作業が 2 回走る）。
    BW.state = { ...BW.state, note: "送れませんでした（つながっていません）", noteBad: true };
    bwRender();
    return false;
  }
  BW.ws.send(JSON.stringify(obj));
  return true;
}

/* 受け取った 1 行を状態へ。★ここが本番の唯一の入口（試験も同じ 1 本を呼ぶ）。 */
function bwFeed(line) {
  const ev = bwParse(line);
  if (!ev) {
    BW.state = { ...BW.state, malformed: BW.state.malformed + 1 };
    bwRender();
    return null;
  }
  BW.state = bwReduce(BW.state, ev, Date.now());
  if (String(ev.type) === "done" && ev.session) BW.session = String(ev.session);
  bwRender();
  return ev;
}

/* ── 送る ──────────────────────────────────────────────────── */
function bwSendPrompt(text) {
  if (BW.state && BW.state.replay) { BW.state = bwInitState(); }   // 読み返しの上には積まない（2026-09-07）
  const t = String(text || "").trim();
  if (!t) return false;
  BW.sendSeq += 1;
  const id = `w${BW.sendSeq}-${Math.random().toString(36).slice(2, 8)}`;
  const now = Date.now();
  const s = BW.state;
  // 会話は残す（新しい依頼のたびに消さない）。走行ごとの欄だけ入れ替える。
  BW.state = {
    ...s, runId: id, phase: "generating", startedMs: now, endedMs: null, chars: 0, runEvents: 0,
    verbSeed: Math.floor(Math.random() * RUN_VERBS.length), outcome: null, ask: null,
    note: null, noteBad: false, heartbeat: null, queue: null, stopRequestedMs: null,
    openBundleKey: null,
    timeline: s.timeline.concat([{ kind: "user", key: `u${id}`, text: t }]),
  };
  BW.runId = id;
  // ★max_iters を**明示的に**送る（2026-09-09）。省略すると serve.py:389 の
  //   ワイヤ既定 40 が黙って効く。端末クライアント（term/client.py:302）と
  //   Windows アプリ（sidecar.rs:504）は常に 0 を送るので影響を受けないが、
  //   ここと Android だけが 40 を継承していた＝同じ依頼が経路によって
  //   別の上限で打ち切られる（利用者からは「モデルが途中でやめた」に見える）。
  const msg = { type: "run", id, prompt: t, max_iters: 0 };
  // 前の走行の session を名乗ると、serve が**会話の続き**として読み込む
  //   （読めなければ `notice kind=session` を出して新しく始める＝黙って途切れない）。
  if (BW.session) msg.session = BW.session;
  const sent = bwSendLine(msg);
  if (!sent) BW.state = { ...BW.state, phase: "idle", runId: null };
  bwRender();
  return sent;
}

function bwStop() {
  const s = BW.state;
  if (!isRunning(s) || !BW.runId) return false;
  if (s.stopRequestedMs) {
    BW.state = { ...s, note: "止める指示は届いています（本体が手番を終えるまで待ちます）", noteBad: false };
    bwRender();
    return false;
  }
  const sent = bwSendLine({ type: "cancel", id: BW.runId });
  BW.state = { ...BW.state, phase: "stopping", stopRequestedMs: Date.now() };
  bwRender();
  return sent;
}

function bwAnswer(text) {
  const s = BW.state;
  if (!s.ask) return false;
  const sent = bwSendLine({ type: "answer", id: BW.runId, ask: s.ask.id, text: String(text) });
  if (sent) {
    BW.state = { ...s, ask: null, phase: "generating",
                 timeline: s.timeline.concat([{ kind: "user", key: `ua${s.ask.id}`, text: String(text) }]) };
  }
  bwRender();
  return sent;
}

/* ── 起動 ──────────────────────────────────────────────────── */
function bwStore(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) { localStorage.removeItem(key); return null; }
    localStorage.setItem(key, value); return value;
  } catch (e) { return null; }   // プライベート窓など。動かなくてよい所で落ちない。
}

async function bwCheckToken(token) {
  // ★/health は合言葉を見ないので、合言葉の可否は**合言葉が要る口**で確かめる。
  //   ここを /health でやると「通った」と言いながら /ws で 401 になる（画面からは沈黙に見える）。
  const url = `${bwHttpBase()}/sessions?limit=1&token=${encodeURIComponent(token)}`;
  const r = await fetch(url, { headers: { "X-Blackwall-Token": token } });
  return r.status;
}

async function bwHealth() {
  try {
    const r = await fetch(`${bwHttpBase()}/health`);
    const h = await r.json();
    BW.version = h && h.version ? String(h.version) : "";
    return h;
  } catch (e) { return null; }
}

function bwBoot() {
  const d = BW.dom;
  d.thread = $("thread"); d.hello = $("hello"); d.main = $("main");
  d.runbar = $("runbar"); d.runbarMark = $("runbar-mark"); d.runbarWhat = $("runbar-what");
  d.runbarTime = $("runbar-time"); d.runbarTokens = $("runbar-tokens"); d.runbarHint = $("runbar-hint");
  d.ask = $("ask"); d.askQ = $("ask-q"); d.askOpts = $("ask-opts");
  d.askForm = $("ask-form"); d.askFree = $("ask-free");
  d.composer = $("composer"); d.q = $("q"); d.send = $("send"); d.stop = $("stop"); d.note = $("note");
  d.gate = $("gate"); d.gateForm = $("gate-form"); d.gateToken = $("gate-token"); d.gateErr = $("gate-err");
  d.conn = $("conn"); d.meta = $("meta"); d.forget = $("forget");

  BW.sid = bwStore("bw.session") || `web-${Math.random().toString(36).slice(2, 10)}`;
  bwStore("bw.session", BW.sid);
  BW.token = bwStore("bw.token") || "";

  if (d.composer) {
    d.composer.addEventListener("submit", (e) => {
      if (e && e.preventDefault) e.preventDefault();
      const text = d.q ? d.q.value : "";
      if (bwSendPrompt(text) && d.q) { d.q.value = ""; bwGrow(d.q); }
    });
  }
  if (d.q) {
    d.q.addEventListener("input", () => bwGrow(d.q));
    d.q.addEventListener("keydown", (e) => {
      // Ctrl+Enter で送る（Enter は改行のまま＝長い依頼を書ける）。
      if (e && e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        if (e.preventDefault) e.preventDefault();
        if (d.composer && d.composer.dispatchEvent) {
          d.composer.dispatchEvent({ type: "submit", preventDefault() {} });
        }
      }
    });
  }
  if (d.stop) d.stop.addEventListener("click", () => bwStop());
  if (d.askForm) {
    d.askForm.addEventListener("submit", (e) => {
      if (e && e.preventDefault) e.preventDefault();
      const v = d.askFree ? d.askFree.value : "";
      if (String(v).trim() && bwAnswer(v) && d.askFree) d.askFree.value = "";
    });
  }
  if (d.gateForm) {
    d.gateForm.addEventListener("submit", (e) => {
      if (e && e.preventDefault) e.preventDefault();
      bwTryToken(d.gateToken ? d.gateToken.value : "");
    });
  }
  if (d.forget) {
    d.forget.addEventListener("click", () => {
      bwStore("bw.token", null); BW.token = "";
      BW.closing = true;
      if (BW.ws) { try { BW.ws.close(); } catch (err) { /* 既に閉じている */ } BW.ws = null; }
      bwShowGate("");
    });
  }
  // 走行中だけ 120ms で描き直す（印の脈動と経過。止まっているときは何もしない）。
  BW.timer = setInterval(() => { if (isRunning(BW.state)) bwPaintRunbar(); }, RUN_MARK_MS);

  BW.booted = true;
  bwHealth().then((h) => {
    if (h && h.web === false) bwSetConn("画面のファイルがサーバーに置かれていません", true);
    if (h && h.auth === false) { BW.token = ""; bwShowMain(); bwConnect(); return; }
    if (BW.token) bwTryToken(BW.token, true);
    else bwShowGate("");
  });
  bwRender();
  return BW;
}

function bwGrow(t) {
  // 高さだけ動かす（幅・レイアウトは動かさない）。
  try { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 220) + "px"; }
  catch (e) { /* 偽 DOM */ }
}

function bwTryToken(token, silent) {
  const t = String(token || "").trim();
  if (!t) { bwShowGate("合言葉が空です"); return; }
  bwCheckToken(t).then((status) => {
    if (status === 401) {
      bwStore("bw.token", null); BW.token = "";
      bwShowGate(silent ? "保存していた合言葉が通りませんでした" : "合言葉が違います");
      return;
    }
    BW.token = t; bwStore("bw.token", t);
    bwShowMain();
    bwConnect();
  }).catch((e) => bwShowGate(`サーバーに届きません（${e && e.message ? e.message : e}）`));
}

function bwShowGate(err) {
  const d = BW.dom;
  if (d.gate) d.gate.hidden = false;
  if (d.main) d.main.hidden = true;
  if (d.composer) d.composer.hidden = true;
  if (d.forget) d.forget.hidden = true;
  if (d.gateErr) { d.gateErr.hidden = !err; d.gateErr.textContent = err || ""; }
  bwSetConn("合言葉を待っています", false);
}
function bwShowMain() {
  const d = BW.dom;
  if (d.gate) d.gate.hidden = true;
  if (d.main) d.main.hidden = false;
  if (d.composer) d.composer.hidden = false;
  if (d.forget) d.forget.hidden = false;
    { const _h = bwQ("#hist"); if (_h) _h.hidden = false; }
  if (d.gateErr) d.gateErr.hidden = true;
  bwRender();
}

/* ブラウザで読み込まれたときだけ自動で起動する（node の試験は自分で bwBoot を呼ぶ）。 */
if (typeof document !== "undefined" && typeof window !== "undefined" && !globalThis.__BW_NO_AUTOBOOT) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bwBoot);
  else bwBoot();
}

// ── 履歴（過去の走行の一覧と読み返し・2026-09-07） ────────────────────
// ★gateway の /sessions と /sessions/<id>/events を使う。読み返しは ws を張らず、
//   事象を bwReduce に流し直して同じ描画で見せる（＝画面の描画が 1 経路のまま）。
function bwQ(sel) {
  // 偽 DOM（煙試験）には querySelector が無い。id 参照に落とせる形だけ使う。
  if (typeof document === "undefined" || !document) return null;
  if (typeof document.querySelector === "function") return document.querySelector(sel);
  if (sel.startsWith("#") && typeof document.getElementById === "function") return document.getElementById(sel.slice(1));
  return null;
}
function bwHistoryTitle(first) {
  const lines = String(first || "").split("\n").map((l) => l.trim()).filter(Boolean);
  // アプリの前置き（[..] 見出し・「作業ディレクトリ:」等）を飛ばし、利用者の文を取る
  const skip = /^(\[|作業ディレクトリ:|OS:|実行の制限:|使える道具:|推論機:|作業場:|Working directory:|Execution policy:|Shell:|Model:|Inference:)/;
  const body = lines.filter((l) => !skip.test(l));
  const pick = body.length ? body[body.length - 1] : (lines[lines.length - 1] || "");
  return pick.length > 72 ? pick.slice(0, 72) + "…" : pick;
}
function bwHistoryTime(mtime) {
  const d = new Date(Number(mtime) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
async function bwHistoryOpen() {
  const panel = bwQ("#hist-panel"), list = bwQ("#hist-list");
  if (!panel || !list) return;
  panel.hidden = false;
  list.textContent = "読み込み中…";
  try {
    const r = await fetch(`${bwHttpBase()}/sessions?limit=30&has_events=1&token=${encodeURIComponent(BW.token)}`,
                          { headers: { "X-Blackwall-Token": BW.token } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const rows = await r.json();
    list.textContent = "";
    if (!Array.isArray(rows) || !rows.length) { list.appendChild(el("div", "hist__empty", "まだ走行がありません。")); return; }
    for (const row of rows) {
      const b = el("button", "hist__row"); b.type = "button";
      b.appendChild(el("span", "t", bwHistoryTime(row.mtime)));
      b.appendChild(el("span", "q", bwHistoryTitle(row.first) || row.id));
      b.addEventListener("click", () => bwReplay(row.id));
      list.appendChild(b);
    }
  } catch (e) {
    list.textContent = `一覧を取れません（${e && e.message ? e.message : e}）`;
  }
}
// ★2026-09-07 実測（Playwright 12:40）: 履歴の読み返しが「[?] ?」と空の答えになった。
//   gateway が保存する事象は {type, ts, seq, payload:{...}} と**中身を payload に包み**、
//   型名も tool_start/tool_end/assistant/result（生の走行は tool_call/tool_result/token/done）。
//   画面は生の走行の形しか読まず、煙試験も生の形で書いていたので緑のまま本番だけ空だった
//   （＝試験が本番のロジックを写し、本番のデータを写していなかった）。ここで 1 か所に寄せる。
function bwUnwrapRecorded(ev) {
  if (!ev || typeof ev !== "object") return ev;
  const p = (ev.payload && typeof ev.payload === "object") ? ev.payload : null;
  const out = p ? Object.assign({}, p, { type: ev.type, ts: ev.ts, seq: ev.seq }) : Object.assign({}, ev);
  if (!out.id && out.session) out.id = out.session;
  switch (out.type) {
    case "tool_end": out.type = "tool_result"; if (out.text == null && out.result != null) out.text = String(out.result); if (out.ok == null) out.ok = !/exit=[1-9]/.test(String(out.result || "")); break;
    case "assistant": out.type = "token"; break;          // 本文はここに載る（token の重複は onBody が畳む）
    case "result": out.type = "done"; if (!out.id && out.session_id) out.id = out.session_id; break;
    case "run_spec": out.type = "run_start"; break;
    default: break;
  }
  return out;
}

async function bwReplay(sid) {
  try {
    const r = await fetch(`${bwHttpBase()}/sessions/${encodeURIComponent(sid)}/events?token=${encodeURIComponent(BW.token)}`,
                          { headers: { "X-Blackwall-Token": BW.token } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const text = await r.text();
    let s = bwInitState();
    let n = 0;
    // ★2026-09-08 監査（clients high）: 記録は本文を **token（流れ）と assistant（確定文）の 2 回**保存する
    //   （実物 /root/.blackwall-mobile/sessions/*/events.jsonl: token 4・assistant 3）。両方を token に
    //   畳むと本文が二重に出る。token を見た走行では assistant を捨てる（token が無い記録では assistant を使う）。
    let sawToken = false;
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      const ev = bwParse(line);
      if (!ev) continue;
      if (ev.type === "run_spec" || ev.type === "result") sawToken = (ev.type === "run_spec") ? false : sawToken;
      if (ev.type === "token") sawToken = true;
      if (ev.type === "assistant" && sawToken) { n += 1; continue; }
      const rec = bwUnwrapRecorded(ev);
      s = bwReduce(s, rec, (Number(rec.ts) || 0) * 1000 || Date.now()); n += 1;
    }
    BW.state = { ...s, phase: "idle", runId: null, replay: sid, note: `読み返し: ${sid}（${n} 事象）。新しく送ると新しい走行になります`, noteBad: false };
    const panel = bwQ("#hist-panel"); if (panel) panel.hidden = true;
    bwRender();
  } catch (e) {
    BW.state = { ...BW.state, note: `読み返せません（${e && e.message ? e.message : e}）`, noteBad: true };
    bwRender();
  }
}
(function bwHistoryWire() {
  if (typeof document === "undefined") return;
  const b = bwQ("#hist"), c = bwQ("#hist-close");
  if (b) b.addEventListener("click", bwHistoryOpen);
  if (c) c.addEventListener("click", () => { const p = bwQ("#hist-panel"); if (p) p.hidden = true; });
})();

