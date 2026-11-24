// 走行の器（2026-10-30）: 2 つの会話を同時に走らせ、知らせが札で振り分けられること・裏の会話にも届くこと・
// 片方が終わっても表の会話の走行が残ること・止めるが表の会話の札へ行くことを確かめる。
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
  await tick(); await tick(); await tick();          // 起動の非同期（会話の読み込み）が済んでから
  run(`state.convos = [{ id: 1, title: "A", msgs: [] }, { id: 2, title: "B", msgs: [] }]; openConvo(state.convos[0]);`);
  const ta = run(`ta`);
  const form = run(`$("#ask")`);
  const submit = () => form.dispatchEvent({ type: "submit", preventDefault() {} });
  ta.value = "hello A"; submit(); await tick(); await tick();
  const sends = () => calls.filter((c) => c[0] === "agent_send").map((c) => c[1].lane);
  t("A を送った（札 c1）", JSON.stringify(sends()) === '["c1"]');
  run(`openConvo(state.convos[1])`);
  t("A を裏へ預けたら入力欄は空く", run(`state.busy`) === false && run(`state.runs.length`) === 1);
  ta.value = "hello B"; submit(); await tick(); await tick();
  t("B も送れた（札 c2）", JSON.stringify(sends()) === '["c1","c2"]' && run(`state.runs.length`) === 2);
  fire("chat-token", { text: "tokA", lane: "c1" });
  fire("chat-token", { text: "tokB", lane: "c2" });
  fire("agent-tool", { id: "x1", name: "Read", args: { file_path: "a" }, lane: "c1", phase: "start" });
  fire("agent-ask", { id: "q1", question: "A に聞く", lane: "c1" });
  t("裏の A の問いは A の記録に（表の B には出さない）", run(`state.runs.find(r => r.lane === "c1").ask?.id`) === "q1"
    && !run(`$("#ask-box")?.parentNode`));
  fire("chat-done", { lane: "c1" }); await tick(); await tick();
  const a = run(`state.convos[0].msgs`);
  t("A の答えは A に（tokA だけ）", a.length >= 2 && String(a[a.length - 1].text) === "tokA");
  const toasts = calls.filter((c) => c[0] === "conv_toast").map((c) => [c[1].title.split(":")[0], c[1].key]);
  t("裏の A が聞いてきた・終わったら知らせる（押すと A を開く）",
    JSON.stringify(toasts) === JSON.stringify([["聞かれています", "local:1"], ["終わりました", "local:1"]]), toasts);
  t("A が終わっても表の B は走ったまま", run(`state.busy`) === true && run(`state.runConvo === state.convos[1]`) && run(`state.runs.length`) === 1);
  ta.value = ""; submit(); await tick();
  const stops = calls.filter((c) => c[0] === "chat_stop").map((c) => c[1]?.lane);
  t("止めるは表の B の札へ", JSON.stringify(stops) === '["c2"]');
  fire("chat-token", { text: "late", lane: "c2" });
  fire("chat-done", { lane: "c2" }); await tick(); await tick();
  const b = run(`state.convos[1].msgs`);
  t("表に出ている B の終わりは知らせない（窓が見えている）", calls.filter((c) => c[0] === "conv_toast").length === 2);
  t("B の答えは B に（tokB で始まる）", b.length >= 2 && String(b[b.length - 1].text).startsWith("tokB"));
  t("全部終わったら空", run(`state.runs.length`) === 0 && run(`state.busy`) === false);
  // 道具なしの会話（作業フォルダ無し・会話モードでない）は chat_send＝札の無い知らせ。それでも答えが届き、終わる。
  run(`currentMode = () => "agent"; state.ws = null; state.convos.push({ id: 3, title: "C", msgs: [] }); openConvo(state.convos[2]);`);
  ta.value = "hello C"; submit(); await tick(); await tick();
  t("道具なしは chat_send で送る", calls.some((c) => c[0] === "chat_send"));
  fire("chat-token", { text: "plain" });
  fire("chat-done", {}); await tick(); await tick();
  const c = run(`state.convos[2].msgs`);
  t("札の無い知らせでも答えが届く", c.length >= 2 && String(c[c.length - 1].text) === "plain");
  t("終わったら空（道具なし）", run(`state.runs.length`) === 0);
  console.log(ng ? `★NG ${ng} 件` : "全部 OK");
  process.exit(ng ? 1 : 0);
})().catch((e) => { console.log("NG", e); process.exit(1); });
