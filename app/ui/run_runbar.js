// 推論中のステータス行（runbar）を**実際に動かして**読む（設計 src/REPORTS/uiux-redesign-20260906.md §B）。
// run_heartbeat.js と同じ作法（domstub + vm）。単独でも動く: node app/ui/run_runbar.js
// ★run_heartbeat.js の末尾から require されて preflight に相乗りする（「書いたが繋いでいない」を避ける）。
//   同じ文脈で app.js を 2 度評価すると const が衝突するので、既に読まれていればそのまま使う。
// 見るもの: 経過が増えて `3m 53s` の形になる／トークンが `↓ ~N tokens` で出る／印が 120ms で相を替える／
//   道具中は道具名＋静止 ▸／純推論だけ Gerund（7 秒で替わる・毎秒は替えない）／finish で消える／
//   halt で時計が止まる／reduced-motion で印だけ止まる／出しうる文字列が CSS の min-width に収まる（幅不動）。
const fs = require("fs"), vm = require("vm");
if (typeof globalThis.beginLive !== "function") {
  require("./domstub.js");
  const src = fs.readFileSync(__dirname + "/app.js", "utf8");
  try { vm.runInThisContext(src, { filename: "app.js" }); } catch (e) { console.log("(boot 中の例外は無視:", e.message + ")"); }
}
let bad = 0;
const ok = (name, cond, got) => {
  if (cond) console.log("ok  " + name);
  else { bad++; console.log("★NG " + name + (got !== undefined ? "  得: " + JSON.stringify(got) : "")); }
};
const q = (id) => document.querySelector("#" + id);
const txt = (id) => q(id)?.textContent ?? "";
const isThinking = (s) => s === "Thinking…";

// 時計を握る。印の相・経過・Gerund の差し替えは壁時計から出るので、時計を進めて描き直す。
// （domstub の setInterval は何もしないので、ticker/marker は動かない＝repaint で駆動する）
const realNow = Date.now;
let clock = 1_700_000_000_000;
Date.now = () => clock;
const tick = (ms) => { clock += ms; };

try {
  const live = beginLive();
  ok("開始直後: 主ラベルは Starting（まだ何も届いていない事実）", txt("runbar-what") === "Starting", txt("runbar-what"));
  ok("開始直後: 経過は 0s", txt("runbar-time") === "0s", txt("runbar-time"));
  ok("開始直後: トークン欄は空（~0 を出さない）", txt("runbar-tokens") === "", txt("runbar-tokens"));
  ok("開始直後: 印は谷の ·", txt("runbar-mark") === "·", txt("runbar-mark"));
  ok("runbar は見えている", q("runbar").hidden === false);

  tick(233_000); live.repaint();
  ok("233 秒後: 経過が 3m 53s（増える・分と秒に空白）", txt("runbar-time") === "3m 53s", txt("runbar-time"));
  ok("233 秒後: 印は 1941 相目＝✢（往復の途中）", txt("runbar-mark") === "✢", txt("runbar-mark"));
  const m1 = txt("runbar-mark"); tick(120); live.repaint(); const m2 = txt("runbar-mark");
  ok("120ms で印が次の相へ（✢→✳）", m1 === "✢" && m2 === "✳", [m1, m2]);

  // 純推論（思考が来た）→ Thinking…（2026-10-30: 気まぐれな Gerund をやめた）。
  live.think("abcdefg");                    // ★2026-11-16 英字は 0.27 トークン/字（estTok）＝7 字で ~2
  const g1 = txt("runbar-what");
  ok("思考中: Thinking…", isThinking(g1), g1);
  ok("思考中: 英字 7 字 → ↓ ~2 tokens（推定・~ 付き）", txt("runbar-tokens") === "↓ ~2 tokens", txt("runbar-tokens"));
  tick(1000); live.repaint();
  ok("1 秒後は同じ語（毎秒替えない）", txt("runbar-what") === g1, txt("runbar-what"));
  tick(7000); live.repaint();
  ok("7 秒後も同じ語（語を替えない）", txt("runbar-what") === g1, txt("runbar-what"));

  // chat-stat（Rust の Meter）が来たら、その数を優先。~ は残す（本物か見積りか画面には分からない）。
  live.stat({ answer_tokens: 12, think_tokens: 30, tok_s: 41.8, secs: 13.6 });
  ok("stat 後: ↓ ~42 tokens（answer+think）", txt("runbar-tokens") === "↓ ~42 tokens", txt("runbar-tokens"));
  live.stat({ answer_tokens: 10700, think_tokens: 0 });
  ok("stat 後: ↓ ~10.7k tokens", txt("runbar-tokens") === "↓ ~10.7k tokens", txt("runbar-tokens"));

  // 道具が走る → 道具名が主役・印は静止した ▸（実際に起きていることを Gerund で隠さない）。
  live.tool({ name: "Bash", id: "rb1", args: { command: "ls" }, phase: "start" });
  ok("道具中: 主ラベルは道具の動き（Bash → Running command）", txt("runbar-what") === "Running command", txt("runbar-what"));
  ok("道具中: 印は ▸", txt("runbar-mark") === "▸", txt("runbar-mark"));
  tick(120); live.repaint();
  ok("道具中: 120ms 経っても ▸ のまま（脈動しない）", txt("runbar-mark") === "▸", txt("runbar-mark"));
  live.tool({ name: "Bash", id: "rb1", ok: true, result: "a.txt", phase: "end" });
  const g3 = txt("runbar-what");
  ok("道具が終わると純推論へ戻り Thinking…", isThinking(g3), g3);
  ok("道具の後は印が脈動へ戻る", RUN_MARK_SEQ.includes(txt("runbar-mark")), txt("runbar-mark"));
  ok("道具をまたいでもトークンは減らない", txt("runbar-tokens") === "↓ ~10.7k tokens", txt("runbar-tokens"));

  // 本文が来た → Writing（実際に起きていることが主役）。
  live.answer("hello");
  ok("本文中: Writing", txt("runbar-what") === "Writing", txt("runbar-what"));

  // 心拍の注記は主ラベルを奪わず末尾（hint）に添う。
  live.heartbeat({ stage: "推論機", quiet_secs: 45 });
  ok("心拍: 主ラベルは Writing のまま", txt("runbar-what") === "Writing", txt("runbar-what"));
  ok("心拍: 注記は hint に出る", txt("runbar-hint").includes("Waiting for the model (45s)"), txt("runbar-hint"));

  // 幅: 出しうる文字列が CSS の min-width（ch）に収まる。styles.css を読んで突き合わせる。
  const css = fs.readFileSync(__dirname + "/styles.css", "utf8");
  // ★行頭の基底の規則だけを読む（2026-10-08: 札の横の `.ctx .runbar__time{…}` を先に拾って誤判定した）。
  const chOf = (cls, pre = "") => Number((css.match(new RegExp("(?:^|\\n)" + pre + "\\." + cls + "\\{[^}]*min-width:(\\d+)ch")) || [])[1] || 0);
  const whatCh = chOf("runbar__what"), timeCh = chOf("runbar__time"), tokCh = chOf("runbar__tokens");
  const longestLabel = Math.max("Thinking…".length, ...Object.values(RUN_DOING).map((v) => v.length));
  ok(`幅: 主ラベル min-width ${whatCh}ch ≥ 最長の語 ${longestLabel}`, whatCh > 0 && whatCh >= longestLabel);
  ok(`幅: 経過 min-width ${timeCh}ch ≥ "59m 59s"＋区切り 2`, timeCh > 0 && timeCh >= fmtRunElapsed(3599).length + 2);
  // 札の横（.ctx）では経過を先頭に置き、区切りを付けない＝"59m 59s" が収まれば幅は動かない。
  const ctxTimeCh = chOf("runbar__time", "\\.ctx ");
  ok(`幅: 札の横の経過 min-width ${ctxTimeCh}ch ≥ "59m 59s"`, ctxTimeCh > 0 && ctxTimeCh >= fmtRunElapsed(3599).length);
  ok(`幅: トークン min-width ${tokCh}ch ≥ "↓ ~99.9k tokens"＋区切り 2`,
     tokCh > 0 && tokCh >= fmtRunTokens(99949).length + 2 && tokCh >= fmtRunTokens(99950).length + 2);
  ok("幅: 印の箱は固定幅（width を持つ）", /\.runbar__mark\{[^}]*\bwidth:/.test(css));
  /* ★2026-09-21 置き換え: 旧はここで「**窓の幅**の規則が在ること」を要求していた。
     実測で踏んだ——袖（会話一覧と Sources）を両方開くと、窓が 820px でも中央の欄は
     **250px** しか無い。`@media (max-width:760px)` は窓を見るので一度も効かず、
     数値の行が欄からはみ出していた。★**この検査は、効かない規則の存在を確かめて緑にしていた。**
     判定を `@container`（欄の幅）へ移し、検査もそちらを見る。 */
  ok("狭いとき: tokens → 経過 の順に落とす（★欄の幅で）",
     /@container\s+runbar\s*\(max-width:\s*420px\)\{\s*\.runbar__tokens\{ display:none/.test(css)
     && /@container\s+runbar\s*\(max-width:\s*320px\)\{\s*\.runbar__time\{ display:none/.test(css));
  ok(".runbar が欄として宣言されている（これが無いと @container は黙って効かない）",
     /container-type\s*:\s*inline-size/.test(css) && /container-name\s*:\s*runbar/.test(css));
  ok("窓の幅で runbar を落とす規則が復活していない",
     !/@media[^{]*max-width[^{]*\)\s*\{[^}]*\.runbar__/.test(css));

  // 終わり → 帯が消える。
  live.finish("done", [], false, null);
  ok("finish 後: runbar が消える", q("runbar").hidden === true);
  const tokAfter = txt("runbar-tokens");
  tick(5000); live.repaint();
  ok("finish 後: repaint しても何も描かない", txt("runbar-tokens") === tokAfter);

  // 止めた → 主ラベルに理由・印は谷の ·・時計は止まる。
  const live2 = beginLive();
  live2.think("x");
  tick(4000); live2.repaint();
  live2.halt("止めました");
  ok("halt 後: 主ラベルは理由", txt("runbar-what") === "止めました", txt("runbar-what"));
  ok("halt 後: 印は ·", txt("runbar-mark") === "·", txt("runbar-mark"));
  const tAfter = txt("runbar-time"); tick(9000); live2.repaint();
  ok("halt 後: 時計が進まない", txt("runbar-time") === tAfter, [tAfter, txt("runbar-time")]);
  live2.finish("", [], false, null);

  // OS が「動きを減らす」→ 印は ✳ で固定。経過・トークンは動く（幅は動かないので）。
  globalThis.matchMedia = () => ({ matches: true });
  const live3 = beginLive();
  live3.think("abcdefgh");
  ok("reduced: 印は ✳ 固定", txt("runbar-mark") === "✳", txt("runbar-mark"));
  tick(120); live3.repaint();
  ok("reduced: 120ms 後も ✳", txt("runbar-mark") === "✳", txt("runbar-mark"));
  tick(61_000); live3.repaint();
  ok("reduced: 経過は動く（1m 01s）", txt("runbar-time") === "1m 01s", txt("runbar-time"));
  ok("reduced: トークンは出る", /^↓ ~\d+ tokens$/.test(txt("runbar-tokens")), txt("runbar-tokens"));
  ok("reduced: Thinking… が出る", isThinking(txt("runbar-what")), txt("runbar-what"));
  live3.finish("", [], false, null);
  delete globalThis.matchMedia;
} finally {
  Date.now = realNow;
}

console.log(bad ? `★NG 推論中のステータス行 ${bad} 件` : "OK  推論中のステータス行（runbar）");
if (bad) process.exit(1);
// 単独で動かしたとき: app.js の起動で立つ定期の見回りが残るので、自分で終わる。
if (require.main === module) process.exit(0);
