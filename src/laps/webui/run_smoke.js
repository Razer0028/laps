/* Web クライアントの煙試験（2026-09-07）。`node run_smoke.js` で単独に走り、
 * `tests/test_webui_smoke.py` から呼ばれて preflight（`pytest src/tests`）に相乗りする。
 *
 * ★何を見るか: **本番と同じ 1 本**（app.js の bwBoot → bwFeed → bwRender）を偽 DOM で通す。
 *   状態を試験の中で組み立て直さない——写しを測ると「本番だけ直っていないのに緑」になる
 *   （`エージェントの検収は端から端まで通す.md` 2026-08-30）。
 * ★DOM は index.html を**読んで**組む（固定の id 表を持たない）。持つと、新しく足した
 *   要素が検査から見えないまま「緑」になる（app/ui/domstub.js が同じ理由でそうしている）。
 */
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const HERE = __dirname;
let bad = 0;
const ok = (name, cond, got) => {
  if (cond) console.log("ok  " + name);
  else { bad++; console.log("★NG " + name + (got !== undefined ? "  得: " + JSON.stringify(got) : "")); }
};

/* ── 偽 DOM ──────────────────────────────────────────────── */
class N {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.parentNode = null;
    this.className = ""; this._text = ""; this.hidden = false; this.disabled = false;
    this.value = ""; this.style = {}; this.__attrs = {}; this.__h = {};
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0;
    const set = new Set();
    this.classList = {
      _s: set,
      add: (...c) => c.forEach((x) => set.add(x)),
      remove: (...c) => c.forEach((x) => set.delete(x)),
      contains: (c) => set.has(c),
      toggle: (c, on) => { const want = on === undefined ? !set.has(c) : !!on;
        if (want) set.add(c); else set.delete(c); return want; },
    };
  }
  get textContent() { return this._text !== "" ? this._text : this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceWith(n) {
    const p = this.parentNode; if (!p) return;
    const i = p.children.indexOf(this);
    if (i >= 0) { p.children[i] = n; n.parentNode = p; this.parentNode = null; }
  }
  _all() { return this.children.flatMap((c) => [c, ...c._all()]); }
  querySelector(sel) { const k = sel.replace(/^[.#]/, ""); return this._all().find((n) => n.className.split(" ").includes(k)) || null; }
  querySelectorAll(sel) { const k = sel.replace(/^[.#]/, ""); return this._all().filter((n) => n.className.split(" ").includes(k)); }
  setAttribute(k, v) { this.__attrs[k] = String(v); }
  getAttribute(k) { return k in this.__attrs ? this.__attrs[k] : null; }
  addEventListener(t, fn) { (this.__h[t] = this.__h[t] || []).push(fn); }
  dispatchEvent(ev) { for (const fn of (this.__h[(ev && ev.type) || ""] || [])) fn(ev); return true; }
  scrollIntoView() {}
  focus() {} blur() {} click() { this.dispatchEvent({ type: "click", preventDefault() {} }); }
  get firstChild() { return this.children[0] || null; }
}

// index.html から id を読み取って、その要素だけを持つ registry を作る。
const HTML = fs.readFileSync(path.join(HERE, "index.html"), "utf8");
const HTML_IDS = [...HTML.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
const byId = {};
for (const id of HTML_IDS) {
  const tag = (HTML.match(new RegExp("<(\\w+)[^>]*\\sid=\"" + id + "\"")) || [])[1] || "div";
  byId[id] = new N(tag);
}
// index.html の入れ子（読み書きするのは親子関係だけ）。hello は thread の中に居る。
if (byId.thread && byId.hello) byId.thread.appendChild(byId.hello);

global.document = {
  readyState: "complete",
  getElementById: (id) => byId[id] || null,
  createElement: (t) => new N(t),
  addEventListener() {},
  body: new N("body"),
};
const store = new Map();
global.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
global.location = { protocol: "http:", host: "127.0.0.1:18400", search: "" };
global.window = { addEventListener() {}, localStorage: global.localStorage, location: global.location };
global.matchMedia = () => ({ matches: false });

let FETCHES = [];
let SESSIONS_STATUS = 200;
global.fetch = async (url) => {
  FETCHES.push(String(url));
  if (String(url).includes("/health")) {
    return { status: 200, json: async () => ({ ok: true, version: "0.1.201", auth: true, web: true }) };
  }
  if (String(url).includes("/events")) {
    return { ok: true, status: 200, text: async () => (global.RECORDED_LINES || []).join("\n") };
  }
  return { status: SESSIONS_STATUS, json: async () => [] };
};

const SOCKETS = [];
class FakeWS {
  constructor(url) {
    this.url = url; this.readyState = 0; this.sent = [];
    this.onopen = null; this.onmessage = null; this.onclose = null; this.onerror = null;
    SOCKETS.push(this);
  }
  send(s) { this.sent.push(s); }
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
  open() { this.readyState = 1; if (this.onopen) this.onopen({}); }
  feed(obj) { if (this.onmessage) this.onmessage({ data: typeof obj === "string" ? obj : JSON.stringify(obj) }); }
}
global.WebSocket = FakeWS;
const TIMERS = [];
global.setInterval = (f, t) => { TIMERS.push(f); return TIMERS.length; };
global.clearInterval = () => {};

/* ── 時計を握る（印の相・経過・Gerund は壁時計から出る）── */
const realNow = Date.now;
let clock = 1_700_000_000_000;
Date.now = () => clock;
const tick = (ms) => { clock += ms; };
const settle = () => new Promise((r) => setImmediate(r));

/* ── app.js を読む（本番と同じ 1 本）── */
vm.runInThisContext(fs.readFileSync(path.join(HERE, "app.js"), "utf8"), { filename: "app.js" });

const txt = (id) => (byId[id] ? byId[id].textContent : "(no such id)");
const lastWS = () => SOCKETS[SOCKETS.length - 1];
const sentObjs = () => (lastWS() ? lastWS().sent.map((s) => JSON.parse(s)) : []);
let seq = 0;
const feed = (o) => { seq += 1; lastWS().feed(Object.assign({ v: 1, seq, ts: 0 }, o)); };

(async () => {
  try {
    /* ① 起動: 合言葉が無いので入口が出る（本文と入力欄は隠れる） */
    ok("起動した（bwBoot が走った）", BW.booted === true);
    await settle(); await settle();
    ok("合言葉が無い → 入口が出ている", byId.gate.hidden === false && byId.main.hidden === true);
    ok("/health を引いた", FETCHES.some((u) => u.includes("/health")));

    /* ② 違う合言葉は断られる（401 を画面の言葉にする） */
    SESSIONS_STATUS = 401;
    byId["gate-token"].value = "wrong";
    byId["gate-form"].dispatchEvent({ type: "submit", preventDefault() {} });
    await settle(); await settle();
    ok("合言葉が違う → 入口のまま・理由が出る",
       byId.gate.hidden === false && byId["gate-err"].hidden === false, txt("gate-err"));
    ok("合言葉が違う → 線を張らない", SOCKETS.length === 0, SOCKETS.length);

    /* ③ 正しい合言葉 → 保存して線を張る */
    SESSIONS_STATUS = 200;
    byId["gate-token"].value = "s3cret";
    byId["gate-form"].dispatchEvent({ type: "submit", preventDefault() {} });
    await settle(); await settle();
    ok("合言葉が通る → 本文と入力欄が出る", byId.gate.hidden === true && byId.main.hidden === false);
    ok("合言葉を控えた", store.get("bw.token") === "s3cret");
    ok("WebSocket を張った", SOCKETS.length === 1);
    ok("URL に session と token が乗る",
       /\/ws\?session=web-[a-z0-9]+&token=s3cret$/.test(lastWS().url), lastWS() && lastWS().url);
    lastWS().open();
    ok("つながったと表示", txt("conn") === "つながっています", txt("conn"));

    /* ④ ready → 何につながっているかが上の帯に出る（内部の識別子は出さない） */
    feed({ type: "notice", id: null, kind: "ready", model: "blackwall-coder",
           workspace: "/root/bw-mobile", tools: ["Bash", "Read"], policy: "trusted",
           backend: "http://192.0.2.160:8741/v1", busy: null, queued: 0 });
    ok("帯にモデルと作業場と道具数", txt("meta").includes("blackwall-coder")
       && txt("meta").includes("/root/bw-mobile") && txt("meta").includes("道具 2"), txt("meta"));
    ok("ready 自体は会話に出ない", byId.thread.children.length === 1);   // hello だけ

    /* ⑤ 送る */
    byId.q.value = "1+1 は？";
    byId.composer.dispatchEvent({ type: "submit", preventDefault() {} });
    const first = sentObjs()[0];
    ok("run を送った", first && first.type === "run" && first.prompt === "1+1 は？", first);
    ok("入力欄を空にした", byId.q.value === "");
    ok("自分の依頼が会話に出る", byId.thread.textContent.includes("1+1 は？"));
    ok("送信ボタンは走行中は押せない", byId.send.disabled === true);
    ok("Stop が出る", byId.stop.hidden === false);
    ok("状態行が出て Starting", byId.runbar.hidden === false && txt("runbar-what") === "Starting",
       txt("runbar-what"));

    /* ⑥ 思考 → Gerund・経過・トークン */
    feed({ type: "think", id: first.id, text: "1+1" });
    tick(233_000);
    bwPaintRunbar();
    ok("思考中: Gerund…", /…$/.test(txt("runbar-what"))
       && RUN_VERBS.includes(txt("runbar-what").slice(0, -1)), txt("runbar-what"));
    ok("経過が 3m 53s", txt("runbar-time") === "3m 53s", txt("runbar-time"));
    ok("トークンは ↓ ~2 tokens（3 字の推定）", txt("runbar-tokens") === "↓ ~2 tokens", txt("runbar-tokens"));
    const m1 = txt("runbar-mark"); tick(120); bwPaintRunbar();
    ok("120ms で印が次の相へ", m1 !== txt("runbar-mark") && RUN_MARK_SEQ.includes(txt("runbar-mark")),
       [m1, txt("runbar-mark")]);
    ok("思考は畳んだ箱に入る（本文と同じ見た目にしない）",
       byId.thread.querySelectorAll("think").length === 1);

    /* ⑦ 道具 */
    feed({ type: "tool_call", id: first.id, call: "c1", name: "Bash", input: { command: "echo 2" } });
    bwPaintRunbar();
    ok("道具中: 主ラベルは道具名", txt("runbar-what") === "Bash", txt("runbar-what"));
    ok("道具中: 印は静止の ▸", txt("runbar-mark") === "▸", txt("runbar-mark"));
    const tool = byId.thread.querySelector(".tool");
    ok("道具の行にコマンド全文が出る", tool && tool.textContent.includes("echo 2"), tool && tool.textContent);
    ok("実行系の札は run", tool && tool.textContent.includes("run"));
    feed({ type: "tool_result", id: first.id, call: "c1", name: "Bash", result: "2\n",
           ok: true, kind: "exec", chars: 2, truncated: false });
    ok("道具が終わると ✓", byId.thread.querySelector(".tool").textContent.includes("✓"));
    bwPaintRunbar();
    ok("道具の後は純推論へ戻る", /…$/.test(txt("runbar-what")), txt("runbar-what"));

    /* ⑧ 本文（と重複の抑止） */
    feed({ type: "token", id: first.id, text: "答えは " });
    const beforeSeq = seq;
    lastWS().feed({ v: 1, seq: beforeSeq, ts: 0, type: "token", id: first.id, text: "答えは " });
    feed({ type: "token", id: first.id, text: "2 です。" });
    ok("本文が 1 つの段落に流れる", byId.thread.textContent.includes("答えは 2 です。"));
    ok("同じ seq は二重に描かない",
       BW.state.duplicates === 1 && !byId.thread.textContent.includes("答えは 答えは"), BW.state.duplicates);
    bwPaintRunbar();
    ok("本文中は Writing", txt("runbar-what") === "Writing", txt("runbar-what"));

    /* ⑨ 心拍は主ラベルを奪わず注記に添う */
    feed({ type: "notice", id: first.id, kind: "heartbeat", stage: "推論機", quiet_secs: 45 });
    bwPaintRunbar();
    ok("心拍: 主ラベルは Writing のまま", txt("runbar-what") === "Writing", txt("runbar-what"));
    ok("心拍: 注記に出る", txt("runbar-hint") === "推論機の応答を待っています（45 秒）", txt("runbar-hint"));

    /* ⑩ 関門（message を持たない ws_guard を画面の言葉にする） */
    feed({ type: "notice", id: first.id, kind: "ws_guard", tool: "Write", target: "/etc/hosts", n: 1 });
    ok("ws_guard が 1 行になる",
       byId.thread.textContent.includes("作業場の外を変える Write を止めました"), BW.state.note);
    ok("ws_guard は失敗として描く",
       byId.thread.querySelectorAll("note--bad").length === 1);

    /* ⑪ 文言の無い知らない kind は数えるだけ（内部の識別子を出さない） */
    const beforeLen = byId.thread.children.length;
    feed({ type: "notice", id: first.id, kind: "totally_new_kind" });
    ok("知らない kind は画面に出さない", byId.thread.children.length === beforeLen);
    ok("知らない kind は数える", (BW.state.unknown["notice:totally_new_kind"] || 0) === 1, BW.state.unknown);
    ok("pong は出さない", (() => { const n = byId.thread.children.length;
      feed({ type: "notice", id: first.id, kind: "pong" }); return byId.thread.children.length === n; })());

    /* ⑫ 質問 → 選択肢を押すと answer が飛ぶ */
    feed({ type: "ask", id: first.id, ask: "a1", question: "どちらにしますか",
           options: ["進める", "やめる"], descriptions: ["そのまま", "ここで止める"] });
    ok("質問が出る", byId.ask.hidden === false && txt("ask-q") === "どちらにしますか");
    ok("選択肢が 2 つ", byId["ask-opts"].children.length === 2);
    ok("説明も出す", byId["ask-opts"].textContent.includes("ここで止める"));
    byId["ask-opts"].children[0].click();
    const ans = sentObjs().find((o) => o.type === "answer");
    ok("answer を送った（ask の id を名指す）", ans && ans.ask === "a1" && ans.text === "進める", ans);
    ok("答えたら質問は閉じる", byId.ask.hidden === true);

    /* ⑬ 止める */
    ok("Stop で cancel が飛ぶ", (() => { byId.stop.click();
      const c = sentObjs().find((o) => o.type === "cancel"); return !!c && c.id === first.id; })());
    ok("2 度押しても cancel は 1 通だけ", (() => { byId.stop.click();
      return sentObjs().filter((o) => o.type === "cancel").length === 1; })());

    /* ⑭ 締め */
    feed({ type: "done", id: first.id, outcome: "accepted", text: "", session: "20260907_120000_ab",
           secs: 3.9, iters: 2, tok_s: 41.8, model: "blackwall-coder", files_written: [] });
    ok("締めで状態行が消える", byId.runbar.hidden === true);
    ok("締めの行が出る", byId.thread.textContent.includes("完了") && byId.thread.textContent.includes("3.9 秒"));
    ok("また送れる", byId.send.disabled === false && byId.stop.hidden === true);
    ok("次の依頼は同じ session を名乗る（会話が続く）", BW.session === "20260907_120000_ab", BW.session);

    /* ⑮ 壊れた行は数えるだけ（落ちない） */
    lastWS().feed("これは JSON ではない");
    ok("壊れた行で落ちない", BW.state.malformed === 1, BW.state.malformed);

    /* ⑯ 切れたら since_seq で戻る */
    const seqBefore = BW.state.lastSeq;
    lastWS().onclose({ code: 1006 });
    ok("切れたら理由を出す", txt("conn").includes("つなぎ直します"), txt("conn"));
    ok("繋ぎ直しの URL に since_seq が乗る",
       bwWsUrl().includes(`&since_seq=${seqBefore}`), bwWsUrl());
    ok("控えの再送は二重に描かない（seq が古い行は捨てる）", (() => {
      const before = BW.state.duplicates;
      BW.state = bwReduce(BW.state, { type: "token", id: first.id, seq: 3, text: "x" }, Date.now());
      return BW.state.duplicates === before + 1;
    })());

    /* ⑰ 幅（出しうる文字列が CSS の min-width に収まる＝字が替わっても列が動かない） */
    const css = fs.readFileSync(path.join(HERE, "styles.css"), "utf8");
    const chOf = (cls) => Number((css.match(new RegExp("\\." + cls + "\\{[^}]*min-width:(\\d+)ch")) || [])[1] || 0);
    const whatCh = chOf("runbar__what"), timeCh = chOf("runbar__time"), tokCh = chOf("runbar__tokens");
    const longestVerb = Math.max(...RUN_VERBS.map((v) => v.length + 1));
    ok(`幅: 主ラベル ${whatCh}ch ≥ 最長の Gerund ${longestVerb}`, whatCh >= longestVerb);
    ok(`幅: 経過 ${timeCh}ch ≥ "59m 59s"＋区切り`, timeCh >= fmtRunElapsed(3599).length + 2);
    ok(`幅: トークン ${tokCh}ch ≥ "↓ ~99.9k tokens"＋区切り`,
       tokCh >= fmtRunTokens(99949).length + 2 && tokCh >= fmtRunTokens(99950).length + 2);
    ok("幅: 印の箱は固定幅", /\.runbar__mark\{[^}]*\bwidth:/.test(css));

    /* ⑱ 画面が指している id が index.html に実在するか（「書いたが繋いでいない」の門） */
    const appSrc = fs.readFileSync(path.join(HERE, "app.js"), "utf8");
    const wanted = [...appSrc.matchAll(/\$\("([^"]+)"\)/g)].map((m) => m[1]);
    const missing = wanted.filter((id) => !HTML_IDS.includes(id));
    ok("app.js が触る id は全部 index.html に在る", missing.length === 0, missing);

    /* ⑲ CSS に未定義の var() が無い（未定義は黙って初期値に落ちる＝線も色も消える） */
    const defined = new Set([...css.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
    const used = new Set([...css.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1]));
    const undef = [...used].filter((v) => !defined.has(v));
    ok("CSS に未定義の var() が無い", undef.length === 0, undef);

    /* ⑳ 使っているクラスが CSS に在る（描いたのに素のまま、を防ぐ） */
    const cssClasses = new Set([...css.matchAll(/\.([A-Za-z][\w-]*)/g)].map((m) => m[1]));
    const usedClasses = new Set();
    for (const m of appSrc.matchAll(/el\("[\w]+",\s*"([^"]+)"/g)) {
      for (const c of m[1].split(/\s+/)) if (c && !c.includes("$")) usedClasses.add(c);
    }
    const noStyle = [...usedClasses].filter((c) => !cssClasses.has(c));
    ok("画面が付けるクラスは CSS に在る", noStyle.length === 0, noStyle);
  } catch (e) {
    bad++;
    console.log("★NG 例外: " + (e && e.stack ? e.stack : e));
  } finally {
    Date.now = realNow;
  }
  await bwSmokeHistory();   // ★合否の前に（履歴の検査の NG も件数に入れる）
  console.log(bad ? `★NG Web クライアント ${bad} 件` : "OK  Web クライアント（煙試験）");
  process.exit(bad ? 1 : 0);
})();

// ── 接頭辞（逆プロキシ /bw/web/ でも API は /bw/ws へ）────────
{
  const saved = global.location;
  global.location = { protocol: "http:", host: "192.0.2.161", pathname: "/bw/web/", search: "" };
  const u = (typeof bwWsUrl === "function") ? bwWsUrl() : "";
  ok("接頭辞つきで /bw/ws を打つ", u.includes("/bw/ws"), u);
  ok("接頭辞つきの HTTP 基底", bwHttpBase().endsWith("/bw"), bwHttpBase());
  global.location = { protocol: "http:", host: "192.0.2.9:18400", pathname: "/web/", search: "" };
  ok("接頭辞なしは従来どおり", bwWsUrl().includes("192.0.2.9:18400/ws"), bwWsUrl());
  global.location = saved;
}

// ── 履歴（一覧の題・時刻・読み返しの畳み） ────────────────
async function bwSmokeHistory() {
  // ★2026-09-08: 走行中に新しい serve の ready が来たら idle に戻り、失われた締めが 1 行入る
  {
    let s = bwInitState();
    s = bwReduce(s, { type: "notice", seq: 1, kind: "run_start", id: "r9" }, 1000);
    s = bwReduce(s, { type: "token", seq: 2, id: "r9", text: "途中" }, 1001);
    const before = isRunning(s);
    s = bwReduce(s, { type: "notice", seq: 3, kind: "ready", model: "m", tools: ["Read"] }, 1002);
    const outs = (s.timeline || []).filter((x) => x.kind === "outcome").map((x) => x.info && x.info.outcome);
    ok("走行中の ready で idle に戻り lost の締めが入る", before && !isRunning(s) && outs.includes("lost"), { before, phase: s.phase, outs });
  }   // ★履歴の検査は本物の bwReplay（async）を通すので関数にし、main の末尾で await する
  const first = "[アプリからの指示]\n[この実行での事実]\n作業ディレクトリ: /root/bw-mobile\nOS: Linux\n実行の制限: policy=trusted\n\n現在の作業フォルダ内にあるゲームを調べてください";
  ok("題は前置きを飛ばして利用者の文", bwHistoryTitle(first) === "現在の作業フォルダ内にあるゲームを調べてください", bwHistoryTitle(first));
  ok("時刻は MM/DD HH:MM", /^\d\d\/\d\d \d\d:\d\d$/.test(bwHistoryTime(1788736047)), bwHistoryTime(1788736047));
  let s = bwInitState();
  const lines = [
    '{"seq":1,"type":"run_start","id":"r1","ts":1}',
    '{"seq":2,"type":"tool_call","id":"r1","call":"c1","name":"Glob","input":{"pattern":"**"},"ts":2}',
    '{"seq":3,"type":"tool_result","id":"r1","call":"c1","name":"Glob","ok":true,"text":"a\nb","ts":3}',
    '{"seq":4,"type":"token","id":"r1","text":"答え","ts":4}',
    '{"seq":5,"type":"done","id":"r1","outcome":"accepted","text":"答え","ts":5}',
  ];
  for (const l of lines) { const ev = bwParse(l); if (ev) s = bwReduce(s, ev, (ev.ts || 0) * 1000); }
  ok("読み返しは同じ畳みで timeline が出来る", Array.isArray(s.timeline) && s.timeline.length >= 2, s.timeline && s.timeline.length);
  // ★本番の記録そのものの形（gateway の events.jsonl・2026-09-07 12:40 の実物から）
  let rs = bwInitState();
  const recorded = [
    '{"type": "run_spec", "ts": 1, "seq": 1, "payload": {"id": "w1", "session": "s1", "prompt": "1+1 は？"}}',
    '{"type": "iter_start", "ts": 2, "seq": 2, "payload": {"iter": 0}}',
    // ★本番の形を写す（/root/.blackwall-mobile/sessions/*/events.jsonl・2026-09-08）: token と assistant の
    //   payload に id は無い（run_spec だけが id を持つ）。旧 fixture は token に id を付けていて本番と違った。
    //   本文は token が**複数の断片**で流れ、assistant がその**連結**を 1 回持つ（実物: token 4・assistant 3）。
    //   さらに**反復が 2 回**あると（実物は 3 回）、2 回目の assistant は「ここまでの本文」と一致しないので
    //   onBody の畳みが効かず本文が二重になる（本番記録 878→1746 字で実測・2026-09-08）。その形で書く。
    '{"type": "token", "ts": 3, "seq": 3, "payload": {"text": "まず確かめます。"}}',
    '{"type": "assistant", "ts": 3, "seq": 4, "payload": {"text": "まず確かめます。", "iter": 0}}',
    '{"type": "token", "ts": 3, "seq": 5, "payload": {"text": "1+1 は"}}',
    '{"type": "token", "ts": 3, "seq": 6, "payload": {"text": " 2 です。"}}',
    '{"type": "assistant", "ts": 3, "seq": 7, "payload": {"text": "1+1 は 2 です。\n", "iter": 1}}',
    '{"type": "tool_start", "ts": 4, "seq": 8, "payload": {"name": "SystemState", "input": {"verb": "os"}, "call": "c1"}}',
    '{"type": "tool_end", "ts": 5, "seq": 9, "payload": {"name": "SystemState", "call": "c1", "result": "[SystemState os] exit=0\nLinux 7.0.0-30-generic"}}',
    '{"type": "result", "ts": 6, "seq": 10, "payload": {"session_id": "s1", "outcome": "accepted", "text": "1+1 は 2 です。"}}',
  ];
  // ★本番と同じ 1 本（bwReplay）を通す（試験がロジックを写すと本番だけ壊れていても緑になる・2026-08-30）。
  global.RECORDED_LINES = recorded;
  await bwReplay("s1");
  rs = BW.state;
  const rtools = (rs.timeline || []).filter((x) => x.kind === "tools").flatMap((x) => x.tools || x.items || []);
  const rnames = rtools.map((x) => x.name);
  ok("記録の形でも道具名が出る", rnames.includes("SystemState"), rnames);
  const rtext = JSON.stringify(rs.timeline || []);
  ok("記録の形でも本文が出る", rtext.includes("1+1 は 2 です"), rtext.slice(0, 200));
  const answers = (rs.timeline || []).filter((x) => x.kind === "answer").map((x) => String(x.text || ""));
  const times = answers.join("").split("1+1 は 2 です").length - 1;
  const times0 = answers.join("").split("まず確かめます").length - 1;
  ok("記録の形でも本文は 1 回だけ（token と assistant の二重を畳む）", times === 1 && times0 === 1, { times, times0, answers });
  ok("記録の形でも結末が出る", (rs.timeline || []).some((x) => x.kind === "outcome"), (rs.timeline || []).map((x) => x.kind));
}

