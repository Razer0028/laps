// 走行中だけの知らせ（live）が答えの下の注記に残らないことを確かめる
process.chdir(__dirname);
require("./domstub.js");
const fs = require("fs"), vm = require("vm");
const handlers = {};
global.window.__TAURI__.event.listen = async (name, fn) => { handlers[name] = fn; return () => {}; };
const baseInvoke = global.invoke;
global.invoke = async (cmd, a) => {
  if (cmd === "agent_send") {
    setTimeout(() => {
      const n = (p) => handlers["chat-notice"]({ payload: p });
      n({ message: "pid 3600 ended (exit 1) after 2m 14s", level: "gate", live: true });
      n({ message: "Still running after 30 s; continues as background job J11", level: "plain", live: true });
      n({ message: "✓ Check passed: `py note.py`", level: "plain" });
      n({ message: "Servers left running (pid 42)", level: "plain" });
      handlers["chat-token"]({ payload: { text: "done" } });
      handlers["chat-done"]({ payload: {} });
    }, 10);
    return null;
  }
  return baseInvoke(cmd, a);
};
try { vm.runInThisContext(fs.readFileSync(require("path").join(__dirname, "app.js"), "utf8"), { filename: "app.js" }); } catch (e) { console.log("(boot:", e.message, ")"); }
vm.runInThisContext(`state.model = { name: "m" }; state.models = [{ name: "m" }]; thinkLevel = () => "low";`);
const notes = [];
const fakeLive = { note: (m, lv) => notes.push([m, lv]), answer() {}, think() {}, isInjected: () => false };
(async () => {
  const r = await vm.runInThisContext("ask")("q", true, fakeLive, null);
  const kept = r.notice.map((x) => x.text);
  console.log("status line:", notes.map((x) => x.join(" | ")));
  console.log("kept:", kept);
  const ok = kept.length === 2 && kept[0].startsWith("✓ Check") && kept[1].startsWith("Servers")
    && notes.length === 4 && notes[0][1] === "gate";
  console.log(ok ? "OK" : "NG");
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.log("NG", e); process.exit(1); });
