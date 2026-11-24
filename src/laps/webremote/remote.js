/* BLACKWALL Remote — ブラウザから全ての機械の会話を見て、送る（2026-10-10）
 *
 * オーナー「Blackwallのウェブ版を公開してほしいです。全てのセッションを管理できるように。UI/UXはデスクトップアプリ版と同じ感じで」
 *       「全てのセッションとリアルタイムで連動して、ウェブから送った内容や状態は接続先のアプリ内でも動作するように。
 *         ClaudeCodeのRemote-controlと同じです」「常にライブで」「Linux版の管理もできるように」「モバイル用のUI/UXも」
 *
 * 作り:
 *   - 中継（推論機:18400・LAN の中だけ）が配る。機械は外向きに中継へつないでいる（受け口を開けない＝Remote Control と同じ形）。
 *   - 会話の畳み方は core.js（webui/app.js と同じ・Android の EventReducer と同じ約束）。描画は view.js が
 *     デスクトップアプリの部品（desk.js）で描く（2026-10-10 オーナー「ウェブもこれ（デスクトップ）に合わせて」）。ここは外枠と線だけ。
 *   - ★走っている会話を開いたときは、いまの走行の写しを頭から受け取るが、**追いつくまで描かない**。時刻は記録の時刻（ts）を使う
 *     （2026-10-10 オーナー「ウェブから会話を開いたときに会話履歴トークンの読み込みが１から走ってしまう」＝写しを生の流れとして
 *      受け取り、経過とトークンの数が 0 から数え直しになり、本文も頭から流れ直していた。この会話で写しは 22,294 行あった）。
 *   - 開いた会話は**いつも追う**（/d/<機械>/ws?follow=<会話>）。アプリで次の走行が始まっても流れてくる。
 *   - 送る: 走行中なら割り込み（inject）。止まっていれば、アプリが受けるなら（操作の線 chat.send）アプリの会話で走らせ、
 *     受けない機械（Linux 版・ゲートウェイ）ではその機械の serve で会話の続きとして走らせる。
 */
"use strict";

const RM = {
  token: "", auth: true, register: false, devices: [], dev: null, rows: [], sid: null, query: "",
  follow: null, followSid: null, followTries: 0, followRetry: null, followClosing: false,
  run: null, runClosing: false,
  ctl: null, ctlWaiters: new Map(), ctlSeq: 0, info: {},          // 機械ごとの machine.info
  gwSid: "", relayVer: "", openSeq: 0, listTimer: null, devTimer: null,
  epoch: 0, rawSeq: 0,      // ★走行ごとに seq は 1 から。描画の鍵が前の走行と重ならないよう、世代ごとに 1e9 ずらす。
};
/* 事象を状態へ。seq を世代でずらし、つなぎ直し用の生の seq は別に覚える。 */
function rmEpochEv(ev) {
  const raw = Number(ev.seq || 0);
  if (raw > 0) { RM.rawSeq = Math.max(RM.rawSeq, raw); return { ...ev, seq: raw + RM.epoch * 1e9 }; }
  return ev;
}

/* 事象の時刻（ミリ秒）。★受け取った時刻ではなく、本体が書いた時刻（ts）。写しを頭から受け取り直しても経過が狂わない。 */
const rmEvMs = (ev) => (Number(ev && ev.ts) > 0 ? Number(ev.ts) * 1000 : Date.now());

/* ── 小道具 ──────────────────────────────────────────────────── */
function rmStore(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) { localStorage.removeItem(key); return null; }
    localStorage.setItem(key, value); return value;
  } catch (e) { return null; }
}
const rmQ = (id) => document.getElementById(id);
function rmHttp(path, extra) {
  const q = new URLSearchParams(extra || {});
  if (RM.token) q.set("token", RM.token);
  const qs = q.toString();
  return `${location.protocol}//${location.host}${path}${qs ? "?" + qs : ""}`;
}
function rmWs(path, extra) {
  const q = new URLSearchParams(extra || {});
  if (RM.token) q.set("token", RM.token);
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}?${q.toString()}`;
}
async function rmGet(path, extra, asText) {
  const r = await fetch(rmHttp(path, extra), { headers: RM.token ? { "X-Blackwall-Token": RM.token } : {} });
  if (r.status === 401) {
    // ★取り消された認証情報を持ち続けない（次の起動でもサインインの画面へ）
    if (RM.token) { rmStore("bwr.token", null); RM.token = ""; }
    throw Object.assign(new Error("サインインし直してください"), { status: 401 });
  }
  if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
  return asText ? r.text() : r.json();
}
const devById = (id) => RM.devices.find((d) => d.id === id) || null;
const curDev = () => devById(RM.dev);
function rmNote(text, bad) { BW.state = { ...BW.state, note: text, noteBad: !!bad }; bwRender(); }

/* 記録の形（{type, ts, seq, payload}）を生の走行の形へ（webui/app.js の bwUnwrapRecorded と同じ）。 */
function rmUnwrap(ev) {
  if (!ev || typeof ev !== "object") return ev;
  const p = (ev.payload && typeof ev.payload === "object") ? ev.payload : null;
  const out = p ? Object.assign({}, p, { type: ev.type, ts: ev.ts, seq: ev.seq }) : Object.assign({}, ev);
  if (!out.id && out.session) out.id = out.session;
  switch (out.type) {
    case "tool_end": out.type = "tool_result"; if (out.text == null && out.result != null) out.text = String(out.result);
      if (out.ok == null) out.ok = !/exit=[1-9]/.test(String(out.result || "")); break;
    case "assistant": out.type = "token"; break;
    case "result": out.type = "done"; if (!out.id && out.session_id) out.id = out.session_id; break;
    case "run_spec": out.type = "run_start"; break;
    default: break;
  }
  return out;
}
function rmTitle(first) {
  const lines = String(first || "").split("\n").map((l) => l.trim()).filter(Boolean);
  const skip = /^(\[|作業ディレクトリ:|OS:|実行の制限:|使える道具:|推論機:|作業場:|Working directory:|Execution policy:|Shell:|Model:|Inference:|<)/;
  const body = lines.filter((l) => !skip.test(l));
  const pick = body.length ? body[0] : (lines[0] || "");
  return pick.length > 60 ? pick.slice(0, 60) + "…" : pick;
}
function rmWhen(sec) {
  const d = new Date(Number(sec) * 1000), now = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? `${p(d.getHours())}:${p(d.getMinutes())}` : `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ── 機械 ────────────────────────────────────────────────────── */
async function rmLoadDevices() {
  try {
    const list = await rmGet("/devices");
    RM.devices = Array.isArray(list) ? list : [];
    if (!RM.dev || !devById(RM.dev)) {
      const want = rmHashGet();
      if (want.dev && want.sid) rmStore(`bwr.sid.${want.dev}`, want.sid);
      const saved = rmStore("bwr.dev");
      const pick = devById(want.dev) || devById(saved) || RM.devices.find((d) => d.online) || RM.devices[0];
      if (pick) rmSelectDev(pick.id, true);
    }
    rmSetConn("接続中", false);
  } catch (e) {
    if (e.status === 401) { rmShowGate("サインインし直してください"); return; }
    rmSetConn(`中継に届きません（${e.message}）`, true);
  }
  rmRenderDevs();
}
function rmKindLabel(k) { return { windows: "Windows", gateway: "Linux", linux: "Linux", term: "Linux" }[k] || (k || "?"); }
function rmRenderDevs() {
  const box = rmQ("devs"); if (!box) return;
  box.textContent = "";
  for (const d of RM.devices) {
    const b = el("button", "chip dev"); b.type = "button";
    b.classList.toggle("is-active", d.id === RM.dev);
    b.classList.toggle("is-online", !!d.online);
    b.setAttribute("role", "tab"); b.setAttribute("aria-selected", d.id === RM.dev ? "true" : "false");
    const name = el("span", "chip__name");
    name.appendChild(el("span", "", d.name || d.id));
    name.appendChild(el("span", "dev__kind", rmKindLabel(d.kind)));
    b.appendChild(name);
    b.appendChild(el("span", "chip__meta num", [d.version ? `v${d.version}` : "", d.workspace || ""].filter(Boolean).join(" · ") || "—"));
    b.title = `${d.os || ""} · v${d.version || "?"}${d.online ? "" : " · つながっていません"}`;
    b.addEventListener("click", () => { rmSelectDev(d.id); });
    box.appendChild(b);
  }
  const d = curDev(), mi = (d && RM.info[d.id]) || {};
  // 入力欄の上の札: 送り先の機械とその作業フォルダ。
  const dn = rmQ("dev-name"); if (dn) dn.textContent = d ? (d.name || d.id) : (RM.devices.length ? "機械を選ぶ" : "つながっている機械がありません");
  const wc = rmQ("ws-chip"), wp = rmQ("ws-path");
  if (wc && wp) { wc.hidden = !(d && d.workspace); wp.textContent = d && d.workspace ? d.workspace : ""; }
  // ランプ: 中継・機械・アプリ（○ 未確認 / ● つながっている / × 不通。形でも分ける＝デスクトップの .lamp）。
  const lamp = (key, up, down) => {
    const n = document.querySelector(`.lamp[data-key="${key}"]`); if (!n) return;
    n.classList.toggle("is-up", !!up); n.classList.toggle("is-down", !!down);
  };
  lamp("relay", !BW.connBad && RM.devices.length >= 0 && RM.relayOk, BW.connBad);
  lamp("device", d && d.online, d && !d.online);
  lamp("app", mi.app === true, d && d.kind === "windows" && mi.app === false);
  const eng = rmQ("side-engine");
  if (eng) {
    if (!d) eng.textContent = "";
    else if (!d.online) eng.textContent = "この機械はつながっていません";
    else if (mi.app === true) eng.textContent = appTakes("chat.send") ? "送るとアプリの会話で走ります" : "アプリは起動中（この版のアプリは受けないので、機械の本体で走ります）";
    else if (d.kind === "windows") eng.textContent = "アプリは閉じています（送ると機械の本体で走ります）";
    else eng.textContent = "送るとこの機械の本体で走ります";
  }
  const ver = rmQ("side-version"); if (ver) ver.textContent = RM.relayVer || "—";      // /health の version は「relay 1.2」の形
  const wh = rmQ("where"); if (wh) wh.textContent = eng ? eng.textContent : "";
  const hw = rmQ("hello-where");
  if (hw) hw.textContent = d ? `${d.name || d.id} の ${d.workspace || "作業フォルダ"} で走ります。` : "左の一覧から機械を選んでください。";
}
/* 開いている機械と会話を URL に残す（#dev=…&sid=…）。ブックマークや別の端末から同じ会話を開ける。 */
function rmHashSet() {
  const h = new URLSearchParams();
  if (RM.dev) h.set("dev", RM.dev);
  if (RM.sid) h.set("sid", RM.sid);
  try { history.replaceState(null, "", "#" + h.toString()); } catch (e) { /* 埋め込みなど */ }
}
function rmHashGet() {
  try { const h = new URLSearchParams(String(location.hash || "").replace(/^#/, "")); return { dev: h.get("dev"), sid: h.get("sid") }; }
  catch (e) { return {}; }
}
function rmSelectDev(id, quiet) {
  if (RM.dev === id && !quiet) { rmToggleSide(false); return; }
  RM.dev = id; rmStore("bwr.dev", id);
  rmCtlClose();
  RM.rows = [];
  rmRenderDevs(); rmRenderConvos();
  rmRefreshList();
  rmMachineInfo();
  const saved = rmStore(`bwr.sid.${id}`);
  if (saved) rmOpen(saved); else rmNewChat();
}

/* ── 操作の線（/ws?ctl=1）─────────────────────────────────────── */
function rmCtlClose() {
  if (RM.ctl) { try { RM.ctl.close(); } catch (e) { /* 閉じている */ } }
  RM.ctl = null;
  for (const [, w] of RM.ctlWaiters) w.reject(new Error("線が切れました"));
  RM.ctlWaiters.clear();
}
function rmCtlOpen() {
  if (RM.ctl && RM.ctl.readyState <= 1) return RM.ctl;
  const ws = new WebSocket(rmWs(`/d/${encodeURIComponent(RM.dev)}/ws`, { ctl: "1" }));
  RM.ctl = ws;
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(String(e.data)); } catch (err) { return; }
    if (!m || m.type !== "ctl_result") return;
    const w = RM.ctlWaiters.get(String(m.id)); if (!w) return;
    RM.ctlWaiters.delete(String(m.id));
    if (m.ok) w.resolve(m.result); else w.reject(new Error(m.message || m.error || "失敗しました"));
  };
  ws.onclose = () => { if (RM.ctl === ws) RM.ctl = null; for (const [, w] of RM.ctlWaiters) w.reject(new Error("線が切れました")); RM.ctlWaiters.clear(); };
  return ws;
}
function rmCtl(op, args, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    const ws = rmCtlOpen();
    const id = `c${++RM.ctlSeq}`;
    const t = setTimeout(() => { RM.ctlWaiters.delete(id); reject(new Error("返事がありません")); }, timeoutMs);
    RM.ctlWaiters.set(id, { resolve: (v) => { clearTimeout(t); resolve(v); }, reject: (e) => { clearTimeout(t); reject(e); } });
    const send = () => ws.send(JSON.stringify({ type: "ctl", id, op, args: args || {} }));
    if (ws.readyState === 1) send(); else ws.addEventListener("open", send, { once: true });
  });
}
async function rmMachineInfo() {
  const id = RM.dev; if (!id) return;
  try {
    const info = await rmCtl("machine.info", {});
    const ops = Array.isArray(info && info.ops) ? info.ops : [];
    const app = (info && typeof info.app === "object" && info.app) || {};
    RM.info[id] = { ...(info || {}), ops, app: !!app.alive, appOps: Array.isArray(app.ops) ? app.ops : [] };
  } catch (e) {
    RM.info[id] = { ops: [], error: String(e.message || e) };
  }
  rmRenderDevs();
}
// アプリが起動していて、その操作を受けるときだけ（閉じていればゲートウェイが app_not_running を返す＝機械の本体で走らせる）。
const appTakes = (op) => { const mi = RM.info[RM.dev] || {}; return !!mi.app && (mi.appOps || []).includes(op); };

/* ── 会話の一覧（いつも新しく）─────────────────────────────────── */
async function rmRefreshList() {
  const id = RM.dev; if (!id) return;
  const d = curDev();
  if (d && !d.online) {
    // ★機械が寝ていても、中継の写し（relay 1.8・2026-11-11 オーナー「Remote Control と同じ仕組み」）がある会話は読める。
    //   一覧は中継の目録から（送るのは繋がってから）。
    const rows = await rmGet("/conversations", { device: id, limit: "200" }).catch(() => []);
    if (RM.dev !== id) return;
    RM.rows = (Array.isArray(rows) ? rows : []).filter((r) => r.copy)
      .map((r) => ({ id: r.sid, mtime: Number(r.updated || 0), title: r.title || rmTitle(r.first) || r.sid, running: false, copy: true }));
    rmRenderConvos(RM.rows.length ? undefined : "この機械はつながっていません");
    rmPaintHead();
    return;
  }
  try {
    const [rows, live] = await Promise.all([
      rmGet(`/d/${encodeURIComponent(id)}/sessions`, { has_events: "1", limit: "80", order: "desc" }).catch(() => []),
      rmGet(`/d/${encodeURIComponent(id)}/live`).catch(() => []),
    ]);
    if (RM.dev !== id) return;
    const byId = new Map();
    for (const r of (Array.isArray(rows) ? rows : [])) byId.set(r.id, { id: r.id, mtime: r.mtime, title: rmTitle(r.first) || r.id, running: false });
    for (const l of (Array.isArray(live) ? live : [])) {
      const cur = byId.get(l.id) || { id: l.id, mtime: l.mtime, title: rmTitle(l.prompt) || l.id };
      cur.running = !!l.running; cur.liveSeq = Number(l.last_seq || 0); cur.mtime = Math.max(cur.mtime || 0, l.mtime || 0);
      if (!cur.title || cur.title === cur.id) cur.title = rmTitle(l.prompt) || cur.id;
      byId.set(l.id, cur);
    }
    RM.rows = Array.from(byId.values()).sort((a, b) => (b.running - a.running) || ((b.mtime || 0) - (a.mtime || 0)));
    rmRenderConvos();
    rmPaintHead();
  } catch (e) {
    rmRenderConvos(`一覧を取れません（${e.message}）`);
  }
}
function rmRenderConvos(msg) {
  const box = rmQ("convos"); if (!box) return;
  box.textContent = "";
  if (msg) { box.appendChild(el("p", "convos__empty", msg)); return; }
  const q = RM.query.trim().toLowerCase();
  const rows = RM.rows.filter((r) => !q || String(r.title).toLowerCase().includes(q) || r.id.includes(q));
  if (!rows.length) {
    box.appendChild(el("p", "convos__empty", q ? `「${RM.query.trim()}」に合う会話はありません。語を減らしてみてください。`
      : (RM.dev ? "まだ会話がありません" : "機械を選んでください")));
    return;
  }
  const today = new Date().toDateString(), yest = new Date(Date.now() - 864e5).toDateString();
  let group = "";
  for (const r of rows) {
    const day = new Date(r.mtime * 1000).toDateString();
    const g = r.running ? "Running" : (day === today ? "Today" : (day === yest ? "Yesterday" : "Earlier"));
    if (g !== group) { box.appendChild(el("div", "convo-group", g)); group = g; }
    const b = el("button", "convo"); b.type = "button";
    b.classList.toggle("is-active", r.id === RM.sid);
    b.classList.toggle("is-running", !!r.running);
    b.appendChild(el("span", "convo__t", r.title || r.id));
    b.appendChild(el("span", "convo__m", rmWhen(r.mtime)));
    b.title = r.id;
    b.addEventListener("click", () => { rmOpen(r.id); rmToggleSide(false); });
    // ★この機械の会話の記録を消す（2026-10-31 オーナー「他の機器のチャットを削除できるように」）。走っている会話は機械の側が断る。
    const row = el("div", "convo-row");
    row.appendChild(b);
    if (!r.running) {
      const del = el("button", "convo__x", "×"); del.type = "button"; del.title = "削除";
      del.setAttribute("aria-label", `「${r.title || r.id}」を削除`);
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (del.dataset.armed !== "1") {
          del.dataset.armed = "1"; del.textContent = "削除";
          setTimeout(() => { if (del.dataset.armed === "1") { del.dataset.armed = ""; del.textContent = "×"; } }, 4000);
          return;
        }
        del.disabled = true;
        try {
          // 機械が寝ていれば機械には頼めない＝中継の写しだけ消す（同じ会話が機械から来ても中継は戻さない）
          if (curDev()?.online) {
            const res = await rmCtl("sessions.delete", { ids: [r.id] });
            if (res?.failed?.length) { rmNote(`消せませんでした: ${res.failed[0]}`, true); del.disabled = false; return; }
          }
          // ★中継の写しも消す（relay 1.8・オーナー 11-11「消さないで残しておいてアプリから消せるようにしておく」）
          const cd = await rmAuth({ op: "conv_delete", device: RM.dev, sid: r.id }).catch((e) => ({ ok: false, message: String(e?.message || e) }));
          if (!cd?.ok) rmNote(`中継の写しを消せませんでした: ${cd?.message || cd?.error || ""}`, true);
          RM.rows = RM.rows.filter((x) => x.id !== r.id);
          if (RM.sid === r.id) rmNewChat();
          rmRenderConvos();
        } catch (e) { rmNote(`消せませんでした: ${e?.message || e}`, true); del.disabled = false; }
      });
      row.appendChild(del);
    }
    box.appendChild(row);
  }
}
/* この会話の作業フォルダの変更（2026-10-31・Claude の Remote Control の差分の欄）。 */
async function rmShowChanges() {
  let box = rmQ("changes-box");
  if (!box) {
    box = el("div", "changes"); box.id = "changes-box";
    box.addEventListener("click", (ev) => { if (ev.target === box) box.hidden = true; });
    document.body.appendChild(box);
  }
  box.hidden = false;
  box.textContent = "";
  const card = el("div", "changes__card");
  const head = el("div", "changes__head");
  const title = el("b", "", "変更");
  const x = el("button", "ghost", "×"); x.type = "button"; x.setAttribute("aria-label", "閉じる");
  x.addEventListener("click", () => { box.hidden = true; });
  head.append(title, x);
  const body = el("pre", "changes__body", "読んでいます…");
  card.append(head, body);
  box.appendChild(card);
  try {
    const r = await rmCtl("git.changes", { session: RM.sid || "" });
    if (!r.git) { body.textContent = `${r.workspace} は git の管理下ではありません`; return; }
    title.textContent = `変更 · ${r.branch}${r.status?.length ? ` · ${r.status.length} 件` : ""}`;
    const untracked = (r.status || []).filter((s) => s.startsWith("??")).map((s) => `新規 ${s.slice(3)}`);
    const lines = [...untracked, ...String(r.diff || "").split("\n")];
    body.textContent = "";
    if (!r.diff && !untracked.length) { body.textContent = "変更はありません"; return; }
    for (const ln of lines) {
      const cls = ln.startsWith("+") && !ln.startsWith("+++") ? "is-add" : ln.startsWith("-") && !ln.startsWith("---") ? "is-del" : ln.startsWith("@@") ? "is-hunk" : "";
      body.appendChild(el("span", `changes__ln ${cls}`, ln || " "));
    }
    if (r.truncated) body.appendChild(el("span", "changes__ln is-hunk", "（長いので途中まで）"));
  } catch (e) { body.textContent = `読めませんでした: ${e?.message || e}`; }
}
function rmPaintHead() {
  const ch = rmQ("changes");
  if (ch) ch.hidden = !RM.dev;
  const d = curDev(), row = RM.rows.find((r) => r.id === RM.sid);
  const title = RM.sid ? (row ? row.title : RM.sid) : "New chat";
  const tt = rmQ("top-title"); if (tt) tt.textContent = d ? `${title}  ·  ${d.name || d.id}` : title;
  const nb = rmQ("new-chat"); if (nb) nb.disabled = !d || !d.online;
}

/* ── 会話を開く: 記録を読み、いつも追う ─────────────────────────── */
function rmResetThread(showHello) {
  const d = BW.dom;
  for (const [, ent] of BW.groups) ent.el.remove();
  BW.groups = new Map();
  d.thread.textContent = "";
  const b = document.getElementById("ask-box"); if (b) b.remove();
  if (showHello && BW.helloTpl) {
    const h = BW.helloTpl.cloneNode(true);
    d.thread.appendChild(h); d.hello = h;
    rmWireHello(h);
    rmRenderDevs();
  } else { d.hello = null; }
  BW.state = bwInitState(); BW.runId = null; BW.syn = { key: "", len: 0, phase: "" };
  rmCatchupEnd(true);
}
function rmWireHello(h) {
  for (const c of h.querySelectorAll(".hcard")) {
    c.addEventListener("click", () => { const q = BW.dom.q; q.value = c.textContent; rmGrow(q); q.focus(); bwPaintSend(); });
  }
}
function rmNewChat() {
  rmFollowClose(); rmRunClose();
  RM.sid = null; if (RM.dev) rmStore(`bwr.sid.${RM.dev}`, null);
  rmResetThread(true); rmRenderConvos(); rmPaintHead(); bwRender(); rmHashSet();
}
async function rmOpen(sid) {
  const dev = RM.dev; if (!dev || !sid) return;
  const my = ++RM.openSeq;
  rmFollowClose(); rmRunClose();
  RM.sid = sid; rmStore(`bwr.sid.${dev}`, sid); rmHashSet();
  rmResetThread(false); rmRenderConvos(); rmPaintHead();
  rmNote("読み込んでいます…", false);
  let row = RM.rows.find((r) => r.id === sid);
  let text = "", liveRows = null;
  // ★走っているかは開くたびに /live で確かめる（2026-10-13）。ページを開いた直後は会話の一覧がまだ届いておらず（row が無い）、
  //   走っている会話を「止まっている」と読んで、記録の全部を描いた上にライブの写しを頭から足し、いまの走行が二重に出ていた。
  const [t, lv] = await Promise.all([
    rmGet(`/d/${encodeURIComponent(dev)}/sessions/${encodeURIComponent(sid)}/events`, {}, true)
      .catch((e) => { if (e.status !== 404) rmNote(`記録を読めません（${e.message}）`, true); return ""; }),
    rmGet(`/d/${encodeURIComponent(dev)}/live`).catch(() => null),
  ]);
  text = t;
  if (my !== RM.openSeq) return;
  if (Array.isArray(lv)) {
    liveRows = lv;
    const l = lv.find((x) => x.id === sid);
    row = { ...(row || { id: sid }), running: !!(l && l.running), liveSeq: l ? Number(l.last_seq || 0) : (row ? row.liveSeq : 0) };
  }
  // ★走っている会話は、いまの走行を live の写しから頭から流してもらう。記録は「最後に終わった走行」までで切る（二重に描かない）。
  const lines = String(text || "").split("\n").filter((l) => l.trim());
  const evs = []; for (const l of lines) { const e = bwParse(l); if (e) evs.push(e); }
  let cut = evs.length;
  if (row && row.running) {
    cut = 0;
    for (let i = evs.length - 1; i >= 0; i--) { if (evs[i].type === "result" || evs[i].type === "done") { cut = i + 1; break; } }
  }
  RM.epoch = 1; RM.rawSeq = 0;
  let s = bwInitState(), sawToken = false;
  for (let i = 0; i < cut; i++) {
    const ev = evs[i];
    if (ev.type === "run_spec") sawToken = false;
    if (ev.type === "token") sawToken = true;
    if (ev.type === "assistant" && sawToken) continue;
    if (ev.type === "run_spec" && ev.payload && ev.payload.prompt) {
      s = { ...s, timeline: s.timeline.concat([{ kind: "user", key: `ur${ev.seq || i}`, text: rmTitleFull(ev.payload.prompt) }]) };
    }
    const rec = rmEpochEv(rmUnwrap(ev));
    s = bwReduce(s, rec, rmEvMs(rec));
  }
  RM.epoch = 2; RM.rawSeq = 0;              // ここから先（ライブ）は新しい世代
  BW.state = { ...s, phase: "idle", runId: null, note: null, noteBad: false };
  if (s.timeline.length && BW.dom.hello) { BW.dom.hello.remove(); BW.dom.hello = null; }
  // ★走っている会話は、いまの写しの最後の番号まで「追いつき」として受け取る（描かない）。番号は開く直前に取り直す。
  let upTo = 0;
  if (row && row.running) {
    const r = (liveRows || []).find((x) => x.id === sid);
    if (r && r.running) upTo = Number(r.last_seq || 0);
    if (!upTo) upTo = 1;
  }
  if (!upTo) rmNote(null);
  // ★描けなくても追う線は開く（2026-10-13: 部品の 1 つが例外を出し、ここで止まって「読み込んでいます…」のままになっていた）。
  try { bwRenderNow(); } catch (e) { rmNote(`描けない所がありました（${e.message}）`, true); try { console.error(e); } catch (e2) { /* 無い */ } }
  bwToBottom();
  rmFollowOpen(sid, (row && !row.running && row.liveSeq) ? row.liveSeq : 0, upTo);
}
// 依頼の前置き（[作業場]・OS: 等）を除いた、利用者が書いた部分。
function rmTitleFull(prompt) {
  const lines = String(prompt || "").split("\n");
  const skip = /^(\[|作業ディレクトリ:|OS:|実行の制限:|使える道具:|推論機:|作業場:|Working directory:|Execution policy:|Shell:|Model:|Inference:)/;
  const kept = lines.filter((l) => !skip.test(l.trim()));
  return (kept.join("\n").trim() || String(prompt || "")).slice(0, 4000);
}

/* 追う線。★切れたら since_seq から埋め直す。走行が入れ替わると seq が戻るので run_start で数え直す。 */
function rmFollowOpen(sid, sinceSeq, catchUpTo) {
  rmFollowClose();
  RM.followClosing = false; RM.followSid = sid;
  if (catchUpTo > 0) rmCatchupBegin(catchUpTo);
  const extra = { follow: sid };
  if (sinceSeq > 0) extra.since_seq = String(sinceSeq);
  let ws;
  try { ws = new WebSocket(rmWs(`/d/${encodeURIComponent(RM.dev)}/ws`, extra)); }
  catch (e) { return rmFollowRetry(sid); }
  RM.follow = ws; BW.ws = ws;
  ws.onopen = () => { RM.followTries = 0; rmSetConn("接続中・ライブ", false); bwRender(); };
  ws.onmessage = (e) => rmFollowFeed(String(e.data));
  ws.onclose = () => {
    if (RM.follow === ws) { RM.follow = null; if (BW.ws === ws) BW.ws = null; }
    if (RM.followClosing || RM.followSid !== sid) return;
    rmSetConn("切れました——つなぎ直します", true);
    rmFollowRetry(sid);
  };
}
function rmFollowRetry(sid) {
  if (RM.followRetry) return;
  const wait = [1, 2, 4, 8, 15][Math.min(RM.followTries, 4)] * 1000;
  RM.followTries += 1;
  RM.followRetry = setTimeout(() => {
    RM.followRetry = null;
    if (RM.sid === sid && !RM.run) rmFollowOpen(sid, RM.rawSeq || 0);
  }, wait);
}
function rmFollowClose() {
  RM.followClosing = true;
  if (RM.followRetry) { clearTimeout(RM.followRetry); RM.followRetry = null; }
  if (RM.follow) { try { RM.follow.close(); } catch (e) { /* 閉じている */ } }
  if (BW.ws === RM.follow) BW.ws = null;
  RM.follow = null; RM.followSid = null;
}
function rmFollowFeed(line) {
  const ev = bwParse(line);
  if (!ev) return;
  if (ev.type === "notice" && ev.kind === "run_start") {
    // 次の走行（アプリ・スマホ・ここ、どこから始まった物でも）。seq は本体ごとに 1 から＝数え直す。
    const prompt = rmTitleFull(ev.prompt || "");
    const already = BW.pendingPrompt && prompt && BW.pendingPrompt.trim() === prompt.trim();
    RM.epoch += 1; RM.rawSeq = 0;
    BW.state = { ...BW.state, runId: ev.id ? String(ev.id) : null, phase: "generating", startedMs: rmEvMs(ev), endedMs: null, chars: 0,
                 runEvents: 0, outcome: null, ask: null, note: null, noteBad: false,
                 timeline: (prompt && !already) ? BW.state.timeline.concat([{ kind: "user", key: `ul${ev.seq}-${Date.now()}`, text: prompt }]) : BW.state.timeline };
    BW.pendingPrompt = null;
    if (BW.dom.hello) { BW.dom.hello.remove(); BW.dom.hello = null; }
  }
  if (ev.id) BW.runId = String(ev.id);
  const raw = Number(ev.seq || 0);
  bwFeedEvent(rmEpochEv(ev));
  if (BW.catchup) {
    if (raw >= BW.catchup.to || ev.type === "done") rmCatchupEnd();
    else rmCatchupQuiet();
  }
  if (ev.type === "done") { setTimeout(rmRefreshList, 500); }
}
function bwFeedEvent(ev) {
  BW.state = bwReduce(BW.state, ev, rmEvMs(ev));
  if (BW.catchup) return;                 // 追いつくまでは描かない（状態だけ作る）
  bwRender();
  rmPaintHead();
}

/* ── 追いつき（走っている会話を開いたとき）────────────────────────────
 * 写しは走行の頭からの全行。生の流れとして描くと「頭から流れ直す」ように見えるので、最後の番号（開く直前の last_seq）に
 * 届くまでは状態だけ作り、届いたら 1 回で描いて下へ送る。番号が取れない・写しが止まっているときは 0.6 秒静まったら終える。 */
function rmCatchupBegin(to) {
  rmCatchupEnd(true);
  BW.catchup = { to, quiet: null, guard: setTimeout(() => rmCatchupEnd(), 15000) };
  BW.state = { ...BW.state, note: "いまの走行に追いついています…", noteBad: false };
  bwRenderNow();
}
function rmCatchupQuiet() {
  const c = BW.catchup; if (!c) return;
  if (c.quiet) clearTimeout(c.quiet);
  c.quiet = setTimeout(() => rmCatchupEnd(), 600);
}
function rmCatchupEnd(silent) {
  const c = BW.catchup; if (!c) return;
  if (c.quiet) clearTimeout(c.quiet);
  if (c.guard) clearTimeout(c.guard);
  BW.catchup = null;
  if (silent) return;
  if (BW.state.note === "いまの走行に追いついています…") BW.state = { ...BW.state, note: null };
  bwRenderNow();
  bwToBottom();
  rmPaintHead();
}

/* ── 送る ─────────────────────────────────────────────────────── */
/* ── Think の深さ（Off / Low / High・2026-10-11 オーナー「深さを選べるように」）。アプリと同じ札・端末に覚える ── */
const RM_THINK_KEY = "bwr.think";
const RM_THINK_LEVELS = ["off", "low", "high"];
const RM_THINK_LABEL = { off: "Off", low: "Low", high: "High" };
function rmThinkLevel() {
  const v = document.getElementById("think-level")?.dataset.level;
  return RM_THINK_LEVELS.includes(v) ? v : "low";
}
function rmSetThinkLevel(v, save = true) {
  const lv = RM_THINK_LEVELS.includes(v) ? v : "low";
  const b = document.getElementById("think-level");
  if (!b) return;
  b.dataset.level = lv;
  const t = document.getElementById("think-level-text");
  if (t) t.textContent = RM_THINK_LABEL[lv];
  b.setAttribute("aria-label", `Thinking depth: ${RM_THINK_LABEL[lv]}`);
  if (save) { try { localStorage.setItem(RM_THINK_KEY, lv); } catch (e) { /* 保存できなくても動く */ } }
}
(() => {
  let saved = null;
  try { saved = localStorage.getItem(RM_THINK_KEY); } catch (e) { /* 無視 */ }
  rmSetThinkLevel(saved || "low", false);
  document.getElementById("think-level")?.addEventListener("click", () => {
    const i = RM_THINK_LEVELS.indexOf(rmThinkLevel());
    rmSetThinkLevel(RM_THINK_LEVELS[(i + 1) % RM_THINK_LEVELS.length]);
  });
})();

async function rmSend(text, force) {
  const t = String(text || "").trim();
  if (!t) return false;
  const d = curDev();
  // ★寝ている機械へは送らない（Remote Control と同じ・オーナー 11-11）。文は入力欄に残る（呼び出し側が ok のときだけ消す）。
  if (!d || !d.online) { rmNote("この機械はいまオフラインです。繋がったら送れます（文は入力欄に残しています）", true); return false; }
  // 走行中: 割り込み（作業を捨てない）。追う線か、ここで始めた走行の線で送る。
  //   force＝強制割り込み（いまの生成と Bash を止めてすぐ渡す・デスクトップの Interrupt と同じ）。
  //   ★画面には足さない: serve が injected の通知で返し、それが吹き出しの中の「interjected: …」になる（二重に出さない）。
  if (isRunning(BW.state)) {
    const ws = RM.run || RM.follow;
    if (!ws || ws.readyState !== 1) { rmNote("送れませんでした（つながっていません）", true); return false; }
    ws.send(JSON.stringify({ type: "inject", id: BW.runId || "", text: t, source: "web", force: !!force }));
    rmNote(force ? "いまの生成を止めて渡しています…" : "次の区切りで渡します…", false);
    return true;
  }
  // ★送る直前に機械の状態を取り直す（2026-10-11: 01:02 に取った古い状態のまま 03:25 に送り、起動中のアプリではなく
  //   機械の本体で走らせていた）。返事が無ければ手元の控えで決める。
  await Promise.race([rmMachineInfo(), new Promise((r) => setTimeout(r, 4000))]);
  // アプリが受けるなら、アプリの会話で走らせる（アプリの画面にも出る・Remote Control と同じ）。
  if (appTakes("chat.send")) {
    try {
      BW.pendingPrompt = t;
      BW.state = { ...BW.state, phase: "queued", startedMs: Date.now(), note: "アプリへ送りました", noteBad: false,
                   timeline: BW.state.timeline.concat([{ kind: "user", key: `ua${Date.now()}`, text: t }]) };
      if (BW.dom.hello) { BW.dom.hello.remove(); BW.dom.hello = null; }
      bwRender();
      const res = await rmCtl("chat.send", { session: RM.sid || "", text: t, think_level: rmThinkLevel() }, 30000);
      const sid = res && res.session ? String(res.session) : RM.sid;
      if (sid) {
        if (sid !== RM.followSid) { RM.sid = sid; rmStore(`bwr.sid.${RM.dev}`, sid); rmHashSet(); rmFollowOpen(sid, 0); }
      } else {
        // 新しい会話: アプリが会話の id を決める。走り出した会話（/live の先頭）を探して追う（最大 20 秒）。
        rmFindNewRun(Date.now());
      }
      setTimeout(rmRefreshList, 800);
      return true;
    } catch (e) {
      BW.pendingPrompt = null;
      BW.state = { ...BW.state, phase: "idle", note: `アプリへ送れませんでした（${e.message}）——この機械の本体で走らせます`, noteBad: true };
      bwRender();
    }
  }
  return rmRunOnDevice(t);
}

async function rmFindNewRun(t0) {
  const dev = RM.dev;
  for (let i = 0; i < 20 && RM.dev === dev && !RM.sid; i++) {
    try {
      const live = await rmGet(`/d/${encodeURIComponent(dev)}/live`);
      const row = (Array.isArray(live) ? live : []).find((r) => r.running && (Number(r.mtime) * 1000 >= t0 - 5000));
      if (row) { RM.sid = row.id; rmStore(`bwr.sid.${dev}`, row.id); rmHashSet(); rmFollowOpen(row.id, 0); rmRefreshList(); return; }
    } catch (e) { /* 次の周で */ }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

/* その機械の serve で走らせる（Linux 版・ゲートウェイ・アプリが閉じているとき）。会話の続きとして名乗る。 */
function rmRunOnDevice(t) {
  rmFollowClose();
  const sid = RM.sid;
  const id = `w${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  BW.runId = id;
  RM.epoch += 1; RM.rawSeq = 0;
  BW.state = { ...BW.state, runId: id, phase: "generating", startedMs: Date.now(), endedMs: null, chars: 0,
               runEvents: 0, outcome: null, ask: null, note: null, noteBad: false, verbSeed: Math.floor(Math.random() * RUN_VERBS.length),
               timeline: BW.state.timeline.concat([{ kind: "user", key: `u${id}`, text: t }]) };
  if (BW.dom.hello) { BW.dom.hello.remove(); BW.dom.hello = null; }
  bwRender();
  const ws = new WebSocket(rmWs(`/d/${encodeURIComponent(RM.dev)}/ws`, { session: RM.gwSid }));
  RM.run = ws; BW.ws = ws; RM.runClosing = false;
  ws.onopen = () => {
    const msg = { type: "run", id, prompt: t, max_iters: 0, source: "web" };
    if (sid) msg.session = sid;
    const lv = rmThinkLevel();                     // Think の深さ（serve の think / effort）
    msg.think = lv !== "off";
    if (lv !== "off") msg.effort = lv;
    ws.send(JSON.stringify(msg));
    rmSetConn("接続中・この機械で実行", false);
  };
  ws.onmessage = (e) => {
    const ev0 = bwParse(String(e.data));
    if (!ev0) return;
    const ev = rmEpochEv(ev0);
    bwFeedEvent(ev);
    if (ev.type === "done") {
      const newSid = ev.session ? String(ev.session) : sid;
      rmRunClose();
      if (newSid) { RM.sid = newSid; rmStore(`bwr.sid.${RM.dev}`, newSid); }
      setTimeout(async () => {
        await rmRefreshList();
        const row = RM.rows.find((r) => r.id === RM.sid);
        if (RM.sid) rmFollowOpen(RM.sid, row && row.liveSeq ? row.liveSeq : (RM.rawSeq || 0));
      }, 600);
    }
  };
  ws.onclose = () => {
    if (RM.run === ws) { RM.run = null; if (BW.ws === ws) BW.ws = null; }
    if (RM.runClosing) return;
    if (isRunning(BW.state)) {
      rmNote("線が切れました。走行はその機械で続いています——ライブで追い直します", true);
      if (RM.sid) rmFollowOpen(RM.sid, 0);
    }
  };
  return true;
}
function rmRunClose() {
  RM.runClosing = true;
  if (RM.run) { try { RM.run.close(); } catch (e) { /* 閉じている */ } }
  if (BW.ws === RM.run) BW.ws = null;
  RM.run = null;
}
function rmStop() {
  if (!isRunning(BW.state)) return;
  const ws = RM.run || RM.follow;
  if (!ws || ws.readyState !== 1) { rmNote("止められません（つながっていません）", true); return; }
  ws.send(JSON.stringify({ type: "cancel", id: BW.runId || "" }));
  BW.state = { ...BW.state, phase: "stopping", stopRequestedMs: Date.now() };
  bwRender();
}
// core.js の質問の選択肢が呼ぶ（bwAnswer）。追う線でもここで始めた線でも同じ形。
function bwAnswer(text) {
  const s = BW.state; if (!s.ask) return false;
  const ws = RM.run || RM.follow;
  if (!ws || ws.readyState !== 1) { rmNote("答えを送れません（つながっていません）", true); return false; }
  ws.send(JSON.stringify({ type: "answer", id: BW.runId || "", ask: s.ask.id, text: String(text) }));
  BW.state = { ...s, ask: null, phase: "generating" };
  bwRender();
  return true;
}

/* view.js の描き直しの後に呼ばれる（題と札）。 */
function rmAfterRender() { rmPaintHead(); }

/* ── 画面の配線 ───────────────────────────────────────────────── */
function rmSetConn(text, bad) {
  BW.conn = text; BW.connBad = !!bad; RM.relayOk = !bad;
  const c = rmQ("conn"); if (c) { c.textContent = text; c.title = text; c.classList.toggle("is-bad", !!bad); }
  rmRenderDevs();
}
function rmToggleSide(open) {
  const app = rmQ("app"), m = rmQ("menu"), sc = rmQ("scrim");
  const on = open === undefined ? !app.classList.contains("side-open") : !!open;
  app.classList.toggle("side-open", on);
  if (m) m.setAttribute("aria-expanded", on ? "true" : "false");
  if (sc) sc.hidden = !on;
}
function rmShowGate(err) {
  rmQ("gate").hidden = false; rmQ("app").hidden = true;
  const ge = rmQ("gate-err"); ge.hidden = !err; ge.textContent = err || "";
}
function rmShowApp() { rmQ("gate").hidden = true; rmQ("app").hidden = false; }
/* サインインの線（WebSocket /auth に 1 通送って 1 通受ける・本文は JSON）。★認証情報は TLS でしか中継が受けない。 */
function rmAuth(req) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(rmWs("/auth", {}));
    const t = setTimeout(() => { ws.close(); reject(new Error("答えがありません")); }, 20000);
    ws.onopen = () => ws.send(JSON.stringify(req));
    ws.onmessage = (ev) => { clearTimeout(t); ws.close(); resolve(JSON.parse(ev.data)); };
    ws.onerror = () => { clearTimeout(t); reject(new Error("線を開けません")); };
  });
}
function rmGrow(t) { try { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 220) + "px"; } catch (e) { /* 偽 DOM */ } }

/* ── Server（推論機の機械の GPU）────────────────────────────────────────
 * ★2026-10-18 オーナー「各アプリの UI に 2080super の使用率を表示する必要がありますね」。デスクトップ版（app/ui の srv*）と
 *   同じ形: GPU 1 枚に 1 本・直近 5 分・0〜100% の同じ目盛り。値は中継の /server/metrics（推論機 の bw-metrics をそのまま）。
 *   中継が古い（1.3 より前）と 404 になるので、そのときは欄ごと出さない。 */
const SRV_WIN = 300, SRV_W = 240, SRV_H = 40;
const SRV = { samples: [], last: 0, names: [], timer: null, err: "", built: -1 };
const srvShort = (n, i) => String(n || "").replace(/^NVIDIA\s+/, "").replace(/^(GeForce|Tesla|Quadro)\s+/, "") || `GPU ${i}`;
function rmSrvRows() {
  const last = SRV.samples[SRV.samples.length - 1];
  const n = Math.max(SRV.names.length, ((last && last.gpus) || []).length);
  return Array.from({ length: n }, (_, i) => ({ i, name: srvShort(SRV.names[i], i) }));
}
function rmSrvRender() {
  const panel = rmQ("srv"), box = rmQ("srv-charts");
  if (!panel || !box) return;
  const rows = rmSrvRows();
  if (SRV.built !== rows.length) {
    box.replaceChildren(); SRV.built = rows.length;
    for (const k of rows) {
      const row = document.createElement("div"); row.className = "srv__row"; row.dataset.gpu = String(k.i);
      row.innerHTML = `<div class="srv__head"><span class="srv__name"></span><span class="srv__val">—</span></div>` +
        `<svg class="srv__plot" viewBox="0 0 ${SRV_W} ${SRV_H}" preserveAspectRatio="none" role="img">` +
        [0.25, 0.5, 0.75].map((f) => `<line class="grid" x1="0" x2="${SRV_W}" y1="${SRV_H * f}" y2="${SRV_H * f}"/>`).join("") +
        `<path class="area"/><path class="line"/></svg><div class="srv__sub"></div>`;
      box.appendChild(row);
    }
  }
  const S = SRV.samples, last = S[S.length - 1];
  const end = last ? last.t : Date.now() / 1000, start = end - SRV_WIN;
  const win = S.filter((s) => s.t >= start);
  for (const k of rows) {
    const row = box.querySelector(`.srv__row[data-gpu="${k.i}"]`); if (!row) continue;
    const pts = win.map((s) => { const g = (s.gpus || [])[k.i]; return g ? [(s.t - start) / SRV_WIN * SRV_W, SRV_H - Math.max(0, Math.min(100, g.util || 0)) / 100 * SRV_H] : null; })
      .filter(Boolean);
    const d = pts.map((p, j) => `${j ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    row.querySelector(".line").setAttribute("d", d);
    row.querySelector(".area").setAttribute("d", pts.length > 1 ? `${d} L${pts[pts.length - 1][0].toFixed(1)},${SRV_H} L${pts[0][0].toFixed(1)},${SRV_H} Z` : "");
    const g = last && (last.gpus || [])[k.i];
    row.querySelector(".srv__name").textContent = k.name;
    row.querySelector(".srv__val").textContent = g ? `${Math.round(g.util || 0)}%` + (g.temp == null ? "" : ` · ${Math.round(g.temp)}℃`) : "—";
    const vram = g && g.vram_total ? `VRAM ${(g.vram_used / 1024).toFixed(1)} / ${(g.vram_total / 1024).toFixed(1)} GB` : "";
    const llm = k.i === 0 && last && last.llm && typeof last.llm.busy === "number" ? ` · ${last.llm.busy} running` : "";
    row.querySelector(".srv__sub").textContent = vram + llm;
    row.querySelector(".srv__plot").setAttribute("aria-label", `GPU utilization (${k.name}) ${g ? Math.round(g.util || 0) + "%" : ""}`);
  }
  const ax = document.querySelectorAll("#srv-axis span");
  const hm = (t) => { const x = new Date(t * 1000); return `${String(x.getHours()).padStart(2, "0")}:${String(x.getMinutes()).padStart(2, "0")}`; };
  if (ax.length === 3) { ax[0].textContent = hm(start); ax[1].textContent = hm(start + SRV_WIN / 2); ax[2].textContent = hm(end); }
  rmQ("srv-host").textContent = rows.length > 1 ? `${rows.length} GPUs` : "";
  const stale = last && Date.now() / 1000 - last.t > 15;
  rmQ("srv-msg").textContent = SRV.err ? `${SRV.err} (retrying)` : stale ? "No fresh measurements from the server" : "";
  panel.classList.toggle("is-down", !!SRV.err);
}
async function rmSrvPoll() {
  clearTimeout(SRV.timer);
  const panel = rmQ("srv");
  if (!panel) return;
  if (document.hidden || panel.dataset.collapsed === "1") { SRV.timer = setTimeout(rmSrvPoll, 5000); return; }
  try {
    const r = await rmGet("/server/metrics", { since: SRV.last || 0 });
    const add = Array.isArray(r.samples) ? r.samples : [];
    if (Array.isArray(r.gpu_names)) SRV.names = r.gpu_names;
    SRV.samples = SRV.samples.concat(add).filter((s) => s.t >= (add.length ? add[add.length - 1].t : 0) - SRV_WIN - 10);
    if (add.length) SRV.last = add[add.length - 1].t;
    SRV.err = ""; panel.hidden = false;
  } catch (e) {
    if (e.status === 404) { panel.hidden = true; SRV.timer = setTimeout(rmSrvPoll, 60000); return; }   // 古い中継
    SRV.err = String(e.message || e); panel.hidden = false;
  }
  rmSrvRender();
  SRV.timer = setTimeout(rmSrvPoll, SRV.err ? 10000 : 3000);
}
function rmSrvStart() {
  const panel = rmQ("srv"), tg = rmQ("srv-toggle");
  if (!panel || !tg || SRV.started) { if (SRV.started) rmSrvPoll(); return; }
  SRV.started = true;
  panel.dataset.collapsed = rmStore("bwr.srv.collapsed") === "1" ? "1" : "";
  tg.setAttribute("aria-expanded", String(panel.dataset.collapsed !== "1"));
  tg.addEventListener("click", () => {
    const now = panel.dataset.collapsed === "1" ? "" : "1";
    panel.dataset.collapsed = now; tg.setAttribute("aria-expanded", String(now !== "1"));
    rmStore("bwr.srv.collapsed", now || null);
    if (!now) rmSrvPoll();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) rmSrvPoll(); });
  rmSrvPoll();
}

function rmStart() {
  rmQ("changes")?.addEventListener("click", rmShowChanges);
  rmShowApp();
  rmSrvStart();
  rmLoadDevices().then(() => rmRefreshList());
  clearInterval(RM.devTimer); clearInterval(RM.listTimer);
  RM.devTimer = setInterval(rmLoadDevices, 10000);          // 機械の出入り
  clearInterval(RM.infoTimer);
  RM.infoTimer = setInterval(() => { if (RM.dev && !document.hidden) rmMachineInfo(); }, 30000);   // アプリが起きているか（ランプと送り先）
  RM.listTimer = setInterval(() => { if (!document.hidden) rmRefreshList(); }, 4000);   // 走っている会話の印・新しい会話（隠れている間は取らない）
}

function rmBoot() {
  const d = BW.dom;
  d.thread = rmQ("thread"); d.hello = rmQ("hello"); d.main = rmQ("main");
  d.runbar = rmQ("runbar"); d.runbarMark = rmQ("runbar-mark"); d.runbarWhat = rmQ("runbar-what");
  d.runbarTime = rmQ("runbar-time"); d.runbarTokens = rmQ("runbar-tokens"); d.runbarHint = rmQ("runbar-hint");
  d.composer = rmQ("ask"); d.q = rmQ("q"); d.send = rmQ("ask-send"); d.now = rmQ("ask-now"); d.note = rmQ("composer-note");
  d.conn = rmQ("conn");
  BW.helloTpl = d.hello ? d.hello.cloneNode(true) : null;
  if (d.hello) rmWireHello(d.hello);
  RM.gwSid = rmStore("bwr.gwsid") || `web-${Math.random().toString(36).slice(2, 10)}`; rmStore("bwr.gwsid", RM.gwSid);
  RM.token = rmStore("bwr.token") || "";
  try { globalThis.BWSynapse && globalThis.BWSynapse.mount(document.querySelector(".main")); } catch (e) { /* 描けない環境 */ }

  const submit = (force) => {
    const v = d.q.value;
    if (isRunning(BW.state) && !v.trim()) { rmStop(); return; }      // 文字が無ければ Stop（デスクトップと同じ）
    rmSend(v, force).then((ok) => { if (ok) { d.q.value = ""; rmGrow(d.q); bwPaintSend(); } });
  };
  d.composer.addEventListener("submit", (e) => { e.preventDefault(); submit(false); });
  d.now.addEventListener("click", () => { submit(true); d.q.focus(); });
  d.q.addEventListener("input", () => { rmGrow(d.q); bwPaintSend(); });
  d.q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing && isRunning(BW.state)) { e.preventDefault(); submit(true); return; }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(false); }
  });
  rmQ("new-chat").addEventListener("click", () => { rmNewChat(); rmToggleSide(false); d.q.focus(); });
  rmQ("convo-q").addEventListener("input", (e) => { RM.query = e.target.value || ""; rmRenderConvos(); });
  rmQ("menu").addEventListener("click", () => rmToggleSide());
  rmQ("dev-chip").addEventListener("click", () => rmToggleSide(true));
  rmQ("scrim").addEventListener("click", () => rmToggleSide(false));
  document.addEventListener("keydown", (e) => {                      // 引き出し（スマホの一覧）は Esc で閉じる
    if (e.key === "Escape" && rmQ("app").classList.contains("side-open")) { rmToggleSide(false); rmQ("menu").focus(); }
  });
  rmQ("forget").addEventListener("click", async () => {
    await rmAuth({ op: "logout" }).catch(() => null);     // ★届かなくても手元は消す
    rmStore("bwr.token", null); RM.token = ""; location.reload();
  });
  rmQ("gate-switch").addEventListener("click", () => {
    RM.register = !RM.register;
    rmQ("gate-pw2").hidden = !RM.register;
    rmQ("gate-go").textContent = RM.register ? "登録" : "サインイン";
    rmQ("gate-switch").textContent = RM.register ? "アカウントをお持ちの場合はサインイン" : "アカウントを作る";
    rmQ("gate-err").hidden = true;
  });
  rmQ("gate-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const pw = rmQ("gate-pw").value;
    if (RM.register && pw !== rmQ("gate-pw2").value) { rmShowGate("パスワードが一致しません"); return; }
    rmQ("gate-go").disabled = true;
    try {
      const r = await rmAuth({ op: RM.register ? "register" : "login", email: rmQ("gate-email").value.trim(), password: pw,
                               device: { name: `ブラウザ（${navigator.platform || "?"}）`, kind: "web" } });
      if (!r.ok) { rmShowGate(r.message || r.error || "失敗しました"); return; }
      RM.token = r.token;
      rmStore("bwr.token", RM.token);
      rmQ("gate-pw").value = ""; rmQ("gate-pw2").value = "";
      rmQ("forget").hidden = false;
      rmStart();
    } catch (err) { rmShowGate(`中継に届きません（${err.message}）`); }
    finally { rmQ("gate-go").disabled = false; }
  });
  // 走行中だけ印を動かす（デスクトップと同じ間隔）。
  BW.timer = setInterval(() => { if (isRunning(BW.state) && !BW.catchup) bwPaintRunbar(); }, RUN_MARK_MS);
  BW.booted = true;

  fetch(`${location.protocol}//${location.host}/health`).then((r) => r.json()).then((h) => {
    RM.relayVer = h && h.version ? String(h.version) : "";
    // ★サインインなしでは入らない（2026-10-31 オーナー「利用にはアカウント登録が必要に」）。アカウントの無い中継（off）だけ素通り。
    RM.auth = !!(h && (h.auth || (h.auth_mode && h.auth_mode !== "off")));
    rmQ("forget").hidden = !RM.token;
    if (!RM.auth || RM.token) rmStart(); else rmShowGate("");
  }).catch(() => { rmShowGate("中継に届きません"); });
  bwRender();
}

/* ★貼った・書き換えた #dev=…&sid=… を開く（2026-10-13: 開いている画面に別の会話の URL を貼っても何も起きなかった）。 */
window.addEventListener("hashchange", () => {
  const want = rmHashGet();
  if (want.dev && want.dev !== RM.dev && devById(want.dev)) {
    if (want.sid) rmStore(`bwr.sid.${want.dev}`, want.sid);
    rmSelectDev(want.dev, true);
  } else if (want.sid && want.sid !== RM.sid) rmOpen(want.sid);
});

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", rmBoot);
else rmBoot();
