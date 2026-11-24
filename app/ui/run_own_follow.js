// この機械のゲートウェイで走った会話（2026-11-01 オーナー「スマホから送った指示が Windows アプリから見えない」）:
// 目録から見つけて実況を追う・止める・割り込む・手元の中身を残す・終わった走行は記録から足す・名指しの無い依頼は新しい会話
process.chdir(__dirname);
const { N } = require("./domstub.js");
Object.defineProperty(N.prototype, "dataset", { get() { return (this.__ds ||= {}); } });
globalThis.requestAnimationFrame = (f) => setTimeout(f, 0); globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
N.prototype.replaceChildren = function (...c) { this.children = []; c.forEach((x) => this.appendChild(x)); };
N.prototype.prepend = function (...c) { c.reverse().forEach((x) => { x.parentNode = this; this.children.unshift(x); }); };
N.prototype.after = function () {}; N.prototype.before = function () {};
N.prototype.click = function () { this.dispatchEvent({ type: "click", preventDefault() {}, stopPropagation() {} }); };   // ＋ボタンを本当に押す
N.prototype.toggleAttribute = function () {}; N.prototype.removeAttribute = function () {};
N.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 }; };
Object.defineProperty(N.prototype, "childNodes", { get() { return this.children; } });
Object.defineProperty(N.prototype, "style", { get() { return (this.__st ||= {}); } });
const fs = require("fs"), vm = require("vm");
const H = {};
global.window.__TAURI__.event.listen = async (name, fn) => { (H[name] ||= []).push(fn); return () => { H[name] = H[name].filter((f) => f !== fn); }; };
const calls = [];
const auth = {};
const baseInvoke = global.invoke;
global.invoke = async (cmd, a) => {
  calls.push([cmd, a]);
  if (cmd === "agent_send" || cmd === "chat_stop" || cmd === "core_inject") return null;
  if (cmd === "auth_call") {
    const v = auth[a.req.op];
    return v ?? { ok: false, message: "no " + a.req.op };
  }
  if (cmd === "peers_load") return JSON.stringify(auth.__peers || { me: "me", items: [] });
  if (cmd === "remote_status") return { enabled: true, connected: true };
  return baseInvoke(cmd, a);
};
const fire = (name, payload) => (H[name] || []).slice().forEach((f) => f({ payload }));
try { vm.runInThisContext(fs.readFileSync("app.js", "utf8"), { filename: "app.js" }); } catch (e) { console.log("(boot:", e.message); }
const run = (js) => vm.runInThisContext(js);
run(`state.model = { name: "m" }; state.models = [{ name: "m" }]; thinkLevel = () => "low"; currentMode = () => "agent";`);
let ng = 0; const t = (name, cond) => { console.log((cond ? "ok  " : "NG  ") + name); if (!cond) ng++; };
const tick = () => new Promise((r) => setTimeout(r, 30));
const ticks = async (n = 4) => { for (let i = 0; i < n; i++) await tick(); };
const sends = () => calls.filter((c) => c[0] === "agent_send").map((c) => c[1]);
const opCount = (op) => calls.filter((c) => c[0] === "auth_call" && c[1].req.op === op).length;
const now = () => Math.floor(Date.now() / 1000);
const item = (o) => ({ device: "me", device_name: "PC", online: true, updated: now(), ...o });
const reset = (convos = []) => { run(`state.runs = []; state.cur = null; state.convosReady = true; bindFg(null); setSending(false); ta.value = "";`); globalThis.__c = convos; run(`state.convos = globalThis.__c;`); };
(async () => {
  await ticks(3);

  // ── 会話が読めるまでは何も作らない
  reset(); run(`state.convosReady = false;`);
  auth.__peers = { me: "me", items: [item({ sid: "s_early", status: "running", source: "android", via: "gateway", first: "早い" })] };
  await run(`loadPeers()`); await ticks();
  t("会話を読み終えるまでは会話を作らない", run(`state.convos.length`) === 0);

  // ── 表の会話がゲートウェイで走り出した
  const start = sends().length;
  reset([{ id: 1, title: "前から", ws: "", engine: "", session: "s_a", sessionWs: "", msgs: [{ role: "user", text: "前の依頼" }, { role: "bot", text: "前の答え", tools: [] }] }]);
  run(`openConvo(state.convos[0])`); await ticks();
  auth.history = { ok: true, runs: [{ prompt: "前の依頼", finished: true, events: [], outcome: "accepted", text: "前の答え" }, { prompt: "スマホの依頼", finished: false, events: [] }] };
  auth.__peers = { me: "me", items: [item({ sid: "s_a", status: "running", source: "android", via: "gateway", first: "前の依頼" })] };
  await run(`loadPeers()`); await ticks(6);
  let s = sends().slice(start);
  t("表の会話がスマホから動くと、依頼を送らずに追う", s.length === 1 && s[0].follow === "s_a" && s[0].remote === "me" && s[0].session === "s_a");
  t("手元の中身を残して、スマホの依頼が見える", JSON.stringify(run(`state.cur.msgs.map((m) => m.text)`)) === JSON.stringify(["前の依頼", "前の答え", "スマホの依頼"]));
  t("会話はこの機械の物のまま", !run(`state.cur.remote`) && run(`state.runs[0].via.device`) === "me");
  t("入力欄が走行中になる", run(`state.busy`) === true);
  // 止める・割り込むは追っている線の札へ
  const lane = s[0].lane;
  run(`ta.value = "方向を変えて"`); run(`$("#ask")`).dispatchEvent({ type: "submit", preventDefault() {} }); await ticks();
  const inj = calls.filter((c) => c[0] === "core_inject").at(-1);
  t("Windows から打つと割り込みとして同じ線へ", !!inj && inj[1].lane === lane && inj[1].text === "方向を変えて");
  run(`ta.value = ""`); run(`$("#ask")`).dispatchEvent({ type: "submit", preventDefault() {} }); await ticks();
  const stop = calls.filter((c) => c[0] === "chat_stop").at(-1);
  t("Windows の止めるが同じ線へ", !!stop && stop[1].lane === lane);
  fire("chat-token", { text: "スマホの答え", lane });
  fire("chat-done", { lane, session: "s_a" }); await ticks(6);
  t("実況が会話に残り、走行の器を出る", run(`state.runs.length`) === 0 && String(run(`state.convos[0].msgs.at(-1).text`)) === "スマホの答え");
  // 終わった目録では読み直さない
  const h0 = opCount("history");
  auth.__peers = { me: "me", items: [item({ sid: "s_a", status: "done", source: "android", via: "gateway", first: "前の依頼", updated: now() - 2 })] };
  await run(`loadPeers()`); await ticks();
  t("見終わった走行を読み直さない", opCount("history") === h0 && run(`state.convos[0].msgs.length`) === 4);
  // Windows から続けると、この機械の本体で走る
  run(`state.ws = { path: "" }`);
  const before = sends().length;
  run(`ta.value = "続きはこちらで"`); run(`$("#ask")`).dispatchEvent({ type: "submit", preventDefault() {} }); await ticks(6);
  const next = sends()[before];
  t("終わった後に Windows から続けると、この機械の本体で走る", !!next && !next.remote && !next.follow);
  if (next) { fire("chat-done", { lane: next.lane, session: "s_a" }); await ticks(6); }

  // ── スマホで始めた知らない会話: 手が空いていれば開く
  reset();
  run(`state.cur = null`);
  const b2 = sends().length;
  auth.history = { ok: true, runs: [{ prompt: "新しい依頼", finished: false, events: [] }] };
  auth.__peers = { me: "me", items: [item({ sid: "s_b", status: "running", source: "android", via: "gateway", first: "新しい依頼" })] };
  await run(`loadPeers()`); await ticks(8);
  t("スマホで始めた会話が一覧に出る", run(`state.convos.some((c) => c.session === "s_b" && !c.remote)`) === true);
  t("手が空いていれば開いて追う", run(`state.cur?.session`) === "s_b" && sends().length === b2 + 1 && sends().at(-1).follow === "s_b");
  fire("chat-done", { lane: sends().at(-1).lane, session: "s_b" }); await ticks(6);

  // ── 打ちかけなら奪わない。一覧に印。開くと追う
  reset([{ id: 2, title: "作業中", ws: "", engine: "", msgs: [] }]);
  run(`openConvo(state.convos[0]); ta.value = "打ちかけ"`); await ticks();
  const b3 = sends().length;
  auth.__peers = { me: "me", items: [item({ sid: "s_c", status: "running", source: "android", via: "gateway", first: "裏の依頼" })] };
  await run(`loadPeers()`); await ticks(6);
  const cc = run(`state.convos.find((c) => c.session === "s_c")`);
  t("打ちかけの間は表の会話を奪わない", !!cc && run(`state.cur.title`) === "作業中" && sends().length === b3 && run(`ta.value`) === "打ちかけ");
  t("一覧で走っているのが分かる", run(`ownRunning(state.convos.find((c) => c.session === "s_c"))`) === true);
  run(`ta.value = ""`);
  auth.history = { ok: true, runs: [{ prompt: "裏の依頼", finished: false, events: [] }] };
  run(`openConvo(state.convos.find((c) => c.session === "s_c"))`); await ticks(8);
  t("開くと追う", sends().length === b3 + 1 && sends().at(-1).follow === "s_c");
  fire("chat-done", { lane: sends().at(-1).lane, session: "s_c" }); await ticks(6);

  // ── アプリ自身の走行は追わない
  reset();
  const b4 = sends().length;
  auth.__peers = { me: "me", items: [item({ sid: "s_app", status: "running", source: "android", via: "app", first: "アプリで" }), item({ sid: "s_app2", status: "running", source: "app", via: "app" })] };
  await run(`loadPeers()`); await ticks();
  t("アプリの走行は追わず、会話も増やさない", sends().length === b4 && run(`state.convos.length`) === 0);

  // ── 見ていない間に終わった走行は足す（1 回だけ）
  reset([{ id: 3, title: "見ていない", ws: "", engine: "", session: "s_d", msgs: [{ role: "user", text: "前" }, { role: "bot", text: "答え", tools: [] }] }]);
  auth.history = { ok: true, runs: [{ prompt: "前", finished: true, events: [] }, { prompt: "外で頼んだ", finished: true, events: [], outcome: "accepted", text: "外の答え" }] };
  auth.__peers = { me: "me", items: [item({ sid: "s_d", status: "done", source: "android" })] };
  const h1 = opCount("history");
  await run(`loadPeers()`); await ticks(6);
  t("印を付ける前の走行も記録から足す", JSON.stringify(run(`state.convos[0].msgs.map((m) => m.role)`)) === JSON.stringify(["user", "bot", "user", "bot"]) && run(`state.convos[0].msgs[2].text`) === "外で頼んだ");
  await run(`loadPeers()`); await ticks(6);
  t("同じ走行を 2 度足さず、読み直しもしない", run(`state.convos[0].msgs.length`) === 4 && opCount("history") === h1 + 1);
  // 記録と手元が同じなら足さない
  reset([{ id: 4, title: "同じ", ws: "", engine: "", session: "s_e", msgs: [{ role: "user", text: "前" }, { role: "bot", text: "答え", tools: [] }] }]);
  auth.history = { ok: true, runs: [{ prompt: "前", finished: true, events: [] }] };
  auth.__peers = { me: "me", items: [item({ sid: "s_e", status: "done", source: "android" })] };
  await run(`loadPeers()`); await ticks(6);
  t("手元と同じなら足さない", run(`state.convos[0].msgs.length`) === 2);
  // アプリの走行・古い走行
  reset();
  auth.__peers = { me: "me", items: [item({ sid: "s_f", status: "done", source: "app" }), item({ sid: "s_g", status: "done", source: "android", via: "app" }),
                                     item({ sid: "s_h", status: "done", source: "android", via: "gateway", updated: now() - 8 * 86400 })] };
  await run(`loadPeers()`); await ticks(6);
  t("アプリの走行と古い走行では会話を作らない", run(`state.convos.length`) === 0);
  // ほかの機械は今までどおり一覧の「ほかの端末」
  auth.__peers = { me: "me", items: [item({ device: "dev9", sid: "s_o", status: "running", via: "gateway", source: "android" })] };
  await run(`loadPeers()`); await ticks();
  t("ほかの機械の走行では、この機械の会話を作らない", run(`state.convos.length`) === 0);

  // ── 名指しの無い依頼は、走っている別の会話へ混ぜない
  reset([{ id: 5, title: "走っている", ws: "", engine: "", session: "s_r", msgs: [] }]);
  run(`state.ws = { path: "D:/w" }; state.convos[0].ws = "D:/w"; state.convos[0].sessionWs = "D:/w"`);
  run(`openConvo(state.convos[0])`); await ticks();
  run(`ta.value = "長い仕事"`); run(`$("#ask")`).dispatchEvent({ type: "submit", preventDefault() {} }); await ticks(6);
  const injBefore = calls.filter((c) => c[0] === "core_inject").length, sBefore = sends().length;
  const runsBefore = run(`state.runs.length`);
  fire("remote-chat", { action: "send", text: "スマホの新しい依頼", session: "", source: "android" }); await ticks(8);
  t("割り込みにならない", calls.filter((c) => c[0] === "core_inject").length === injBefore);
  t("新しい会話として走る", sends().length === sBefore + 1 && run(`state.runs.length`) === runsBefore + 1 && run(`state.cur.title`).includes("スマホの新しい依頼"));
  t("走っていた会話はそのまま", run(`state.runs.some((r) => r.convo.session === "s_r")`) === true);
  // 名指しがあれば今までどおりその走行へ
  fire("remote-chat", { action: "send", text: "名指しの割り込み", session: "s_r", source: "android" }); await ticks();
  const inj2 = calls.filter((c) => c[0] === "core_inject").at(-1);
  t("名指しの依頼はその走行への割り込み", !!inj2 && inj2[1].text === "名指しの割り込み");
  // 止めるだけは、名指しが無ければ表の会話の走行へ
  const st0 = calls.filter((c) => c[0] === "chat_stop").length;
  fire("remote-chat", { action: "stop", session: "" }); await ticks();
  t("名指しの無い止めるは今までどおり", calls.filter((c) => c[0] === "chat_stop").length === st0 + 1);

  console.log(ng ? `NG ${ng} 件` : "全部 ok");
  process.exit(ng ? 1 : 0);
})().catch((e) => { console.log("ERR", e); process.exit(2); });
