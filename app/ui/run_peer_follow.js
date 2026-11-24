// Remote Control（2026-10-31）: ほかの端末で走っている会話を開くと実況を追う・アプリのある機械へはその画面で走らせる・消す・/rc の窓。
process.chdir(__dirname);
const { N } = require("./domstub.js");
Object.defineProperty(N.prototype, "dataset", { get() { return (this.__ds ||= {}); } });
globalThis.requestAnimationFrame = (f) => setTimeout(f, 0); globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
N.prototype.replaceChildren = function (...c) { this.children = []; c.forEach((x) => this.appendChild(x)); };
N.prototype.prepend = function (...c) { c.reverse().forEach((x) => { x.parentNode = this; this.children.unshift(x); }); };
N.prototype.after = function () {}; N.prototype.before = function () {};
N.prototype.toggleAttribute = function () {}; N.prototype.removeAttribute = function () {};
N.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 }; };
Object.defineProperty(N.prototype, "childNodes", { get() { return this.children; } });
Object.defineProperty(N.prototype, "style", { get() { return (this.__st ||= {}); } });
const fs = require("fs"), vm = require("vm");
const H = {};
global.window.__TAURI__.event.listen = async (name, fn) => { (H[name] ||= []).push(fn); return () => { H[name] = H[name].filter((f) => f !== fn); }; };
const calls = [];
const auth = {};                  // op → 答え
const baseInvoke = global.invoke;
global.invoke = async (cmd, a) => {
  calls.push([cmd, a]);
  if (cmd === "agent_send") return null;
  if (cmd === "auth_call") {
    const k = a.req.op === "ctl" ? `ctl:${a.req.ctl}` : a.req.op;
    const v = auth[k];
    if (typeof v === "function") return v(a.req);
    return v ?? { ok: false, message: "no " + k };
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
const authOps = (k) => calls.filter((c) => c[0] === "auth_call" && (c[1].req.ctl === k || c[1].req.op === k));
(async () => {
  await ticks(3);
  run(`state.convos = []; state.cur = null; state.ws = null;`);
  const running = { device: "dev9", sid: "s_run", title: "長い仕事", first: "長い仕事", status: "running", online: true, updated: 100, device_name: "remote-a" };
  auth.__peers = { me: "me", items: [running] };
  auth.history = { ok: true, runs: [
    { prompt: "前の依頼", finished: true, events: [], outcome: "accepted", text: "前の答え" },
    { prompt: "長い仕事", finished: false, events: [] } ] };
  // ── 走っている会話を開くと追う
  await run(`loadPeers()`); await ticks();
  run(`openPeerConvo(state.peers.items[0])`); await ticks(6);
  let s = sends();
  t("走っている会話を開くと、依頼を送らずに追う", s.length === 1 && s[0].follow === "s_run" && s[0].remote === "dev9");
  const lane = s[0]?.lane;
  t("走行の器に入り、入力欄は走行中になる", run(`state.runs.length`) === 1 && run(`state.busy`) === true);
  const msgs0 = run(`state.cur.msgs.map((m) => m.role)`);
  t("終わった走行の依頼と答え、走っている走行の依頼が並ぶ", JSON.stringify(msgs0) === JSON.stringify(["user", "bot", "user"]) || msgs0.length === 3);
  fire("chat-token", { text: "向こうの実況", lane });
  fire("chat-done", { lane, session: "s_run" }); await ticks(6);
  const m = run(`state.cur.msgs`);
  t("実況が会話に残る", String(m[m.length - 1]?.text) === "向こうの実況");
  t("終わると走行の器を出る", run(`state.runs.length`) === 0 && run(`state.busy`) === false);
  // 終わった目録が来ても読み直さない（見終わった）
  const hist0 = authOps("history").length;
  auth.__peers = { me: "me", items: [{ ...running, status: "done", updated: 101 }] };
  await run(`loadPeers()`); await ticks();
  t("見終わった会話を読み直さない", authOps("history").length === hist0 && sends().length === 1);

  // ── 向こうのアプリが受ける機械へはその画面で走らせる
  auth["ctl:machine.info"] = { ok: true, result: { app: { alive: true, ops: ["chat.send"] } } };
  auth["ctl:chat.send"] = { ok: true, result: { accepted: true } };
  const ta = run(`ta`), form = run(`$("#ask")`);
  ta.value = "続けて"; form.dispatchEvent({ type: "submit", preventDefault() {} }); await ticks(6);
  const cs = authOps("chat.send");
  t("向こうのアプリへ chat.send で渡す（本体を別に立てない）", cs.length === 1 && cs[0][1].req.args.session === "s_run" && cs[0][1].req.args.text === "続けて" && sends().length === 1);
  t("送った依頼が見える", String(run(`state.cur.msgs.at(-1).text`)).includes("続けて"));
  auth.__peers = { me: "me", items: [{ ...running, status: "running", updated: 200 }] };
  auth.history = { ok: true, runs: [{ prompt: "続けて", finished: false, events: [] }] };
  await run(`loadPeers()`); await ticks(6);
  t("走り出したら実況を追う", sends().length === 2 && sends()[1].follow === "s_run");
  fire("chat-done", { lane: sends()[1].lane, session: "s_run" }); await ticks(6);

  // ── アプリの無い機械は今までどおり向こうの本体で
  auth["ctl:machine.info"] = { ok: true, result: { app: { alive: false } } };
  auth.__peers = { me: "me", items: [{ ...running, device: "devX", sid: "s_x", status: "done", updated: 300, device_name: "linux" }] };
  await run(`loadPeers()`); await tick();
  run(`openPeerConvo(state.peers.items[0])`); await ticks();
  ta.value = "こっちも"; form.dispatchEvent({ type: "submit", preventDefault() {} }); await ticks(6);
  const last = sends().at(-1);
  t("アプリの無い機械は向こうの本体で走る", last.remote === "devX" && !last.follow && last.session === "s_x");
  fire("chat-done", { lane: last.lane, session: "s_x" }); await ticks(6);

  // ── 消す
  auth["ctl:sessions.delete"] = { ok: true, result: { deleted: 1, failed: [], ids: ["s_x"] } };
  const okDel = await run(`deletePeerConvo("devX", "s_x", "linux")`); await ticks();
  t("向こうの会話を消すと一覧からも消える", okDel === true && run(`state.convos.some((c) => c.remote?.sid === "s_x")`) === false && run(`state.peers.items.length`) === 0);
  auth["ctl:sessions.delete"] = { ok: true, result: { deleted: 0, failed: ["s_y: 走っているので消しません"], ids: [] } };
  run(`state.convos.push({ id: 9, title: "y", session: "s_y", remote: { device: "devX", sid: "s_y", name: "linux" }, msgs: [] })`);
  const ngDel = await run(`deletePeerConvo("devX", "s_y", "linux")`); await ticks();
  t("断られたら消さず理由を出す", ngDel === false && run(`state.convos.some((c) => c.remote?.sid === "s_y")`) === true
    && String(run(`$("#composer-note")?.textContent`)).includes("走っているので"));

  // ── この機械の会話をほかの端末が消した
  run(`state.convos.push({ id: 10, title: "z", session: "s_z", msgs: [] }, { id: 11, title: "w", session: "s_w", msgs: [] })`);
  fire("remote-chat", { action: "delete", ids: ["s_z"] }); await ticks();
  t("ほかの端末が消した会話が一覧から消える", run(`state.convos.some((c) => c.session === "s_z")`) === false && run(`state.convos.some((c) => c.session === "s_w")`) === true);

  // ── /rc
  auth.rc = { ok: true, connected: true, name: "PC", device: "me", app: "blackwall://open?d=me", web: "https://h/#dev=me", qr: ["101", "010", "101"] };
  ta.value = "/rc"; form.dispatchEvent({ type: "submit", preventDefault() {} }); await ticks(6);
  t("/rc で窓が開き、依頼は送らない", authOps("rc").length === 1 && String(ta.value) === "");
  t("/remote-control でも開く", (ta.value = "/remote-control", form.dispatchEvent({ type: "submit", preventDefault() {} }), await ticks(6), authOps("rc").length === 2));
  console.log(ng ? `★NG ${ng} 件` : "全部 OK");
  process.exit(ng ? 1 : 0);
})().catch((e) => { console.log("NG", e); process.exit(1); });
