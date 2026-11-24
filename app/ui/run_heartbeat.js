// 心拍の画面側（app.js）を実際に動かす。run_finish.js と同じ作法（domstub + vm）。
require("./domstub.js");
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(require("path").join(__dirname, "app.js"), "utf8");
try { vm.runInThisContext(src, { filename: "app.js" }); } catch (e) { console.log("(boot 中の例外は無視:", e.message + ")"); }
let bad = 0;
const live = beginLive();
const hint = () => document.querySelector("#runbar-hint")?.textContent || "";
console.log("typeof live.heartbeat =", typeof live.heartbeat); if (typeof live.heartbeat !== "function") bad++;
live.heartbeat({ stage: "推論機", quiet_secs: 45 });
let h = hint(); console.log("心拍(推論機 45s)   =", JSON.stringify(h)); if (!h.includes("Waiting for the model (45s)")) bad++;
live.heartbeat({ stage: "道具 Bash", quiet_secs: 120 });
h = hint(); console.log("心拍(道具 Bash)     =", JSON.stringify(h)); if (!h.includes("Bash running (120s silent)")) bad++;
live.answer("alpha");
h = hint(); console.log("本文が来たあと      =", JSON.stringify(h)); if (h.includes("Waiting") || h.includes("silent")) bad++;
live.note("差し戻し: 裏付けのない数値");
live.heartbeat({ stage: "推論機", quiet_secs: 30 });
h = hint(); console.log("一言が優先されるか  =", JSON.stringify(h)); if (!h.includes("差し戻し")) bad++;
console.log(bad ? "★NG " + bad + " 件" : "OK  心拍の画面側 4/4");
if (bad) process.exit(1);

// ── 実測欄（0.1.166）: paintChipMeta が load を数字だけで添え、load 無しなら何も足さない
{
  let bad2 = 0;
  state.gpu = { vram_free_mb: 5120 };
  state.llama = { running: true, load: { offloaded_layers: 22, total_layers: 41, gpu_weight_mib: 5033, cpu_weight_mib: 3546 } };
  let el = document.querySelector("#chip-meta");
  if (!el) { el = document.createElement("span"); el.id = "chip-meta"; document.body.appendChild(el); }
  paintChipMeta();
  let t = el.textContent; console.log("実測欄(load あり)   =", JSON.stringify(t)); if (!t.includes("GPU層 22/41") || !t.includes("CPU 3.5GB")) bad2++;
  state.llama = { running: true, load: null }; paintChipMeta();
  t = el.textContent; console.log("実測欄(load 無し)   =", JSON.stringify(t)); if (t.includes("GPU層")) bad2++;
  console.log(bad2 ? "★NG 実測欄 " + bad2 + " 件" : "OK  実測欄 2/2");
  if (bad2) process.exit(1);
}

// ── 推論中のステータス行（2026-09-06）: 経過 `3m 53s`・↓トークン・脈動する印・Gerund を実際に動かして読む。
//   ★ここから呼ぶのは preflight（scripts/preflight.sh）に相乗りするため＝書いたが繋いでいない、を避ける。
require("./run_runbar.js");

// ── 推論の宛先の表示（2026-09-06）: 状態行・RIG・設定欄が**実際に渡した値**を出すか。
//   ★`await` する。require しただけで process.exit(0) すると、非同期の4件が
//     一度も走らないまま「緑」になる（この台本自身が踏みかけた形）。
require("./run_backend.js")()
  .then(() => process.exit(0))   // beginLive の setInterval が残るので明示的に終える
  .catch((e) => { console.log("★NG run_backend.js 中の例外:", e); process.exit(1); });
