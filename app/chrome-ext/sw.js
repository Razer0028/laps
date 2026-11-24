// L.A.P.S Browser Bridge（2026-11-18 オーナー「L.A.P.S の Chrome 連携を強化したい。もっと Chrome を操作しやすく」）。
// 形: L.A.P.S の本体（同じ PC）が 127.0.0.1 で待つ WebSocket へ、この拡張機能からつなぐ。本体の頼みで
//   タブを開き・chrome.debugger で DevTools の命令を中継し・知らせを返す（Claude in Chrome と同じ形）。
//   ★外へは何も送らない（127.0.0.1 だけ）。合言葉（token）は L.A.P.S の画面か ~/.laps/chrome-bridge.json の物を、ポップアップで入れる。
//   ★Service Worker は止まる＝状態は chrome.storage に置く（変数に持たない）。止まったら alarms で起きてつなぎ直す。
//   開いたタブは「L.A.P.S」のタブグループにまとめる。Chrome は debugger を付けたタブの上に「デバッグ中」の帯を出す（利用者が × で切れる）。

const DEFAULTS = { port: 18765, token: "" };
let ws = null;                       // つないでいる間だけの物（止まったら無くなる＝起きたらつなぎ直す）
let pingTimer = 0;

async function settings() {
  const s = await chrome.storage.local.get(["port", "token"]);
  return { port: Number(s.port || DEFAULTS.port), token: String(s.token || "") };
}
async function setStatus(status, detail = "") {
  await chrome.storage.session.set({ status, detail, at: Date.now() });
  const text = status === "connected" ? "on" : "";
  try {
    await chrome.action.setBadgeText({ text });
    await chrome.action.setBadgeBackgroundColor({ color: status === "connected" ? "#2f6fe0" : "#71717a" });
  } catch (e) { /* 起動直後 */ }
}
function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const { port, token } = await settings();
  if (!token) { await setStatus("no-token", "合言葉が未設定"); return; }
  let sock;
  try { sock = new WebSocket(`ws://127.0.0.1:${port}/laps-bridge`); }
  catch (e) { await setStatus("error", String(e)); return; }
  ws = sock;
  sock.onopen = async () => {
    sock.send(JSON.stringify({ type: "hello", token, version: chrome.runtime.getManifest().version, ua: navigator.userAgent }));
  };
  sock.onmessage = (ev) => { handle(ev.data).catch((e) => console.warn("laps bridge", e)); };
  sock.onclose = async () => {
    if (ws === sock) ws = null;
    clearInterval(pingTimer);
    await setStatus("disconnected");
  };
  sock.onerror = () => { /* onclose が続く */ };
}

async function groupTab(tabId) {
  try {
    const { groupId } = await chrome.storage.session.get("groupId");
    let gid = groupId;
    if (gid != null) { try { await chrome.tabGroups.get(gid); } catch (e) { gid = null; } }
    if (gid == null) {
      gid = await chrome.tabs.group({ tabIds: [tabId] });
      await chrome.tabGroups.update(gid, { title: "L.A.P.S", color: "red" });
      await chrome.storage.session.set({ groupId: gid });
    } else {
      await chrome.tabs.group({ tabIds: [tabId], groupId: gid });
    }
  } catch (e) { /* グループに入れられなくても続ける */ }
}
async function attached() { return (await chrome.storage.session.get("attached")).attached || {}; }
async function markAttached(tabId, on) {
  const a = await attached();
  if (on) a[tabId] = Date.now(); else delete a[tabId];
  await chrome.storage.session.set({ attached: a });
}
async function attach(tabId) {
  const a = await attached();
  try { await chrome.debugger.attach({ tabId }, "1.3"); }
  catch (e) { if (!String(e).includes("Another debugger") || !a[tabId]) throw e; }   // 自分が付けた物はそのまま使う
  await markAttached(tabId, true);
}

async function handle(raw) {
  let m;
  try { m = JSON.parse(raw); } catch (e) { return; }
  if (m.type === "welcome") {
    await setStatus("connected");
    clearInterval(pingTimer);
    pingTimer = setInterval(() => send({ type: "ping" }), 20000);    // ★Service Worker を起こしておく（30 秒で止まる）
    return;
  }
  if (m.type === "denied") { await setStatus("denied", m.reason || "合言葉が違う"); try { ws.close(); } catch (e) {} return; }
  if (m.id == null) return;
  const reply = (result) => send({ re: m.id, result });
  const fail = (e) => send({ re: m.id, error: String(e && e.message || e) });
  try {
    if (m.op === "open") {
      const tab = await chrome.tabs.create({ url: m.url || "about:blank", active: m.active !== false });
      await groupTab(tab.id);
      await attach(tab.id);
      reply({ tabId: tab.id, url: tab.url || m.url || "" });
    } else if (m.op === "attach") {
      await attach(m.tabId);
      await groupTab(m.tabId);
      reply({ tabId: m.tabId });
    } else if (m.op === "cdp") {
      const target = m.sessionId ? { tabId: m.tabId, sessionId: m.sessionId } : { tabId: m.tabId };
      const r = await chrome.debugger.sendCommand(target, m.method, m.params || {});
      reply(r || {});
    } else if (m.op === "tabs") {
      const { groupId } = await chrome.storage.session.get("groupId");
      const a = await attached();
      const tabs = await chrome.tabs.query({});
      reply(tabs.map((t) => ({ tabId: t.id, title: t.title, url: t.url, active: t.active, inGroup: groupId != null && t.groupId === groupId, attached: !!a[t.id] })));
    } else if (m.op === "activate") {
      const t = await chrome.tabs.update(m.tabId, { active: true });
      await chrome.windows.update(t.windowId, { focused: true });
      reply({ ok: true });
    } else if (m.op === "detach") {
      try { await chrome.debugger.detach({ tabId: m.tabId }); } catch (e) { /* もう外れている */ }
      await markAttached(m.tabId, false);
      reply({ ok: true });
    } else {
      fail(`unknown op ${m.op}`);
    }
  } catch (e) { fail(e); }
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  send({ type: "event", tabId: source.tabId, sessionId: source.sessionId || "", method, params: params || {} });
});
chrome.debugger.onDetach.addListener(async (source, reason) => {
  await markAttached(source.tabId, false);
  send({ type: "detached", tabId: source.tabId, reason });     // 利用者が「デバッグ中」の帯で切った（canceled_by_user）など
});
chrome.tabs.onRemoved.addListener(async (tabId) => { await markAttached(tabId, false); send({ type: "closed", tabId }); });

chrome.runtime.onStartup.addListener(() => { connect(); });
chrome.runtime.onInstalled.addListener(async () => {
  await chrome.alarms.create("laps-reconnect", { periodInMinutes: 0.5 });
  connect();
});
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "laps-reconnect") connect(); });
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes.token || changes.port)) { try { ws && ws.close(); } catch (e) {} ws = null; connect(); }
});
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg && msg.type === "reconnect") { try { ws && ws.close(); } catch (e) {} ws = null; await connect(); }
    sendResponse(await chrome.storage.session.get(["status", "detail", "at"]));
  })();
  return true;
});
connect();
