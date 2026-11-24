// 推論の宛先が**画面に出るか**を実際に動かして読む（2026-09-06）。
//
// ★なぜ要るか: ある実機が 0.1.194 更新後に「推論機の応答を待っています（N秒）
//   → エラー」になり、LAN の推論サーバーへは1件も届いていなかった。
//   画面のどこにも**実際の宛先**が出ていなかったので、切り分けに丸1日かかった。
//   ここで見るのは「宛先が状態行と RIG と設定欄に、渡した値のまま出るか」。
//
// ★domstub + vm。run_heartbeat.js から require されて preflight に相乗りする
//   （＝「書いたが繋いでいない」を避ける）。
require("./domstub.js");
const fs = require("fs"), vm = require("vm");
if (typeof beginLive !== "function") {
  const src = fs.readFileSync(require("path").join(__dirname, "app.js"), "utf8");
  try { vm.runInThisContext(src, { filename: "app.js" }); } catch (e) { console.log("(boot 中の例外は無視:", e.message + ")"); }
}

let bad = 0;
const ok = (name, cond, got) => {
  console.log((cond ? "OK  " : "★NG ") + name + (got === undefined ? "" : "  = " + JSON.stringify(got)));
  if (!cond) bad++;
};
const hint = () => document.querySelector("#runbar-hint")?.textContent || "";
const hintTitle = () => document.querySelector("#runbar-hint")?.title || "";

const LOCAL = {
  remote: false, url: "", model: "",
  effective: "http://127.0.0.1:8760/v1", source: "auto_local",
  source_label: "自動（ローカルが使える）", local_ok: true, local_port: 8760,
};
const LAN = {
  remote: true, url: "http://127.0.0.1:8080/v1", model: "default",
  effective: "http://127.0.0.1:8080/v1", source: "auto_lan",
  source_label: "自動（ローカルが使えないので LAN）", local_ok: false, local_port: 8760,
};

/* ★同期部分と非同期部分を1つの Promise にまとめて返す。
 *   run_heartbeat.js は require の直後に process.exit(0) するので、
 *   非同期のまま置くと**最後の4件が一度も走らない**（＝書いたが繋いでいない）。
 *   呼び出し側は返り値を await すること。 */
async function run() {
// ── 1) 1行の言葉づかい ──────────────────────────────────────────────
state.backend = LOCAL;
ok("ローカルの宛先は種類で出る（住所は帯の title・2026-10-30）", backendLine() === "This PC", backendLine());
state.backend = LAN;
ok("LAN の宛先は種類とモデルで出る", backendLine() === "Server · default", backendLine());
ok("scheme は落として住所だけ", backendAddr() === "127.0.0.1:8080/v1", backendAddr());

// ★**設定ではなく渡した値**を出す。effective と url が食い違う（設定は LAN なのに
//   実際は手元へ行っている）状況こそ、この行が要る場面。
state.backend = { remote: false, url: "http://127.0.0.1:8080/v1", effective: "http://127.0.0.1:8760/v1" };
ok("食い違ったら effective が勝つ", backendAddr() === "127.0.0.1:8760/v1", backendAddr());

state.backend = null;
ok("宛先が取れないときは黙る", backendLine() === "", backendLine());

// ── 2) 状態行に出るか ───────────────────────────────────────────────
state.backend = LAN;
{
  const live = beginLive();
  live.repaint();
  ok("走行中の状態行に宛先の種類が出る", hint().includes("Server"), hint());
  ok("住所は帯の title で読める", hintTitle().includes("127.0.0.1:8080/v1"), hintTitle());
  // 心拍が来たら、そちらが優先（宛先は押しのけてよい＝より新しい観測だから）
  live.heartbeat({ stage: "推論機", quiet_secs: 45 });
  ok("心拍は宛先より優先", hint().includes("Waiting for the model (45s)"), hint());
  live.halt();
}

// ── 3) 20秒たったら「まだ来ていません」＋宛先 ──────────────────────
{
  const real = Date.now;
  const t0 = real();
  Date.now = () => t0;
  const live = beginLive();
  Date.now = () => t0 + 30000;          // 30 秒たったことにする
  live.repaint();
  const h = hint();
  Date.now = real;
  ok("20秒で「まだ来ていません」", h.includes("Waiting for the model (30s)"), h);
  ok("その時も title に宛先が入る", hintTitle().includes("127.0.0.1:8080/v1"), hintTitle());
  live.halt();
}

// ── 4) 設定欄が 3 値（auto）を言葉で出す ────────────────────────────
{
  const saved = global.invoke;
  const reply = { enabled: false, url: "http://127.0.0.1:8080", model: "default", mode: "auto" };
  global.invoke = async (cmd) => (cmd === "lan_settings_get" ? reply : null);

  const noteEl = document.querySelector("#lan-note");
  noteEl.textContent = "保存しましたが **届きません**。";   // 保存の結果（消えてはいけない）

  state.backend = LAN;
  await refreshLanForm();
  const mode = document.querySelector("#lan-mode")?.textContent || "";
  ok("auto は言葉で説明される", mode.startsWith("サーバー固定："), mode);
  ok("auto の説明に実際の宛先が入る", mode.includes("127.0.0.1:8080/v1"), mode);
  // ★チェックが外れていても LAN で動くのが auto。だから説明が要る。
  // ★2026-09-06: チェックボックスは撤去（推論機はサーバー側に固定）。#lan-on は存在しないのが正。
  // （偽 DOM は querySelector が null を返さないので、要素の有無ではなく**保存の振る舞い**で確かめる）
  {
    let seen = null;
    const inner = global.invoke;
    global.invoke = async (cmd, args) => {
      if (cmd === "lan_settings_set") { seen = args; return { ...reply, enabled: true, mode: "on" }; }
      return inner(cmd, args);
    };
    try { await saveLanSettings(); } catch (e) { /* 保存後の再描画は偽 DOM で落ちてよい */ }
    global.invoke = inner;
    ok("保存は常に enabled=true（サーバー固定・チェックボックス無し）", !!(seen && seen.enabled === true), seen);
  }
  // ★保存の結果を潰さない（#lan-note と #lan-mode を分けた理由そのもの）。
  ok("保存の結果は消えない", noteEl.textContent.includes("届きません"), noteEl.textContent);

  reply.mode = "on";  await refreshLanForm();
  ok("on でもサーバー固定の一文", (document.querySelector("#lan-mode").textContent || "").startsWith("サーバー固定："));
  reply.mode = "off"; await refreshLanForm();
  ok("off でもサーバー固定の一文（3 値は廃止）", (document.querySelector("#lan-mode").textContent || "").startsWith("サーバー固定："));

  global.invoke = saved;
  console.log(bad ? "★NG 宛先の表示 " + bad + " 件" : "OK  宛先の表示 15/15");
  if (bad) process.exit(1);
}
}

module.exports = run;
// 単体で `node ui/run_backend.js` したときも同じ経路で走る。
if (require.main === module) run().then(() => process.exit(bad ? 1 : 0));
