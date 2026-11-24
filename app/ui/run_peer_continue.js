// ほかの端末の会話を Windows から続ける（2026-10-30・走行の器の 4 段目）: 一覧に載り、その機械（remote）とその会話の名札で送り、答えが届く。
process.chdir(__dirname);
const { N } = require("./domstub.js");
// この確かめの器だけの足し（偽の DOM に dataset と value・style が無く、起動が途中で止まる）。
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
const baseInvoke = global.invoke;
global.invoke = async (cmd, a) => { calls.push([cmd, a]); if (cmd === "agent_send") return null; return baseInvoke(cmd, a); };
const fire = (name, payload) => (H[name] || []).slice().forEach((f) => f({ payload }));
try { vm.runInThisContext(fs.readFileSync("app.js", "utf8"), { filename: "app.js" }); } catch (e) { console.log("(boot:", e.message, ")"); }
const run = (js) => vm.runInThisContext(js);
run(`state.model = { name: "m" }; state.models = [{ name: "m" }]; thinkLevel = () => "low"; currentMode = () => "chat";`);
let ng = 0; const t = (name, cond) => { console.log((cond ? "ok  " : "NG  ") + name); if (!cond) ng++; };
const tick = () => new Promise((r) => setTimeout(r, 30));
(async () => {
  await tick(); await tick(); await tick();
  run(`state.convos = []; state.cur = null; state.ws = null; currentMode = () => "agent";`);
  // ほかの端末（.9）の会話を「続ける」
  run(`openPeerConvo({ device: "dev9", sid: "20261030_010101_abcd", title: "ログを見て", first: "ログを見て", last: "見ました。", device_name: "remote-b", online: true, status: "done" })`);
  t("向こうの会話が一覧に載り開いた", run(`state.cur?.remote?.device`) === "dev9" && run(`state.cur.msgs.length`) === 2);
  const ta = run(`ta`), form = run(`$("#ask")`);
  ta.value = "続きをお願い"; form.dispatchEvent({ type: "submit", preventDefault() {} }); await tick(); await tick();
  const s = calls.filter((c) => c[0] === "agent_send").map((c) => c[1]);
  t("向こうの機械で送る（作業フォルダ無しでも道具あり・その会話の名札）",
    s.length === 1 && s[0].remote === "dev9" && s[0].session === "20261030_010101_abcd" && s[0].useTools === true && s[0].engine === null, s);
  fire("chat-token", { text: "続けました", lane: s[0]?.lane });
  fire("chat-done", { lane: s[0]?.lane, session: "20261030_010101_abcd" }); await tick(); await tick();
  const m = run(`state.cur.msgs`);
  t("答えが届く", String(m[m.length - 1]?.text) === "続けました");
  t("同じ会話をもう一度「続ける」と同じ物を開く",
    (run(`openPeerConvo({ device: "dev9", sid: "20261030_010101_abcd", title: "x" }); state.convos.length`) === 1));
  console.log(ng ? `★NG ${ng} 件` : "全部 OK");
  process.exit(ng ? 1 : 0);
})().catch((e) => { console.log("NG", e); process.exit(1); });
