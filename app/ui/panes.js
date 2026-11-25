/* ── 分割表示（2026-11-11 オーナー「Claudeを参考にして分割表示とか取り入れましょうかね」）。
 * 会話を横に 2 つ・2×2 で 4 つまで並べる。枠ごとに会話の画面（.thread）を持ち、選んでいる枠の .thread が id="thread" になる
 * （app.js は #thread を毎回引き直すので、id を移すだけで描く先が変わる）。入力欄（#ask）とシェル（#cshell）は選んでいる枠の下へ動く。
 * ・選んでいない枠の会話が走っていれば、描いた物はその枠に置いたまま裏で続く（r.park.inPane）。描き手は自分の要素を握って
 *   書き続けるので、画面にそのまま流れて見える。押すとその枠を選び、入力欄が来る。
 * ・左の一覧の会話を Ctrl+クリック（または中ボタン）で新しい枠に開く。上の帯の分割のボタンで空の枠を足す。枠の × で閉じる
 *   （走っている会話は止めずに裏へ預ける）。同じ会話を 2 つの枠に出さない（出ていればその枠を選ぶ）。
 * ・並びは覚えておき、次に開いたときに戻す（bw.panes）。 */
const PANES_MAX = 4;
const panes = { list: [], focus: 0 };

function paneOfConvo(c) {
  if (!c) return -1;
  return panes.list.findIndex((p, i) => (i === panes.focus ? state.cur === c : p.convo === c));
}

function paneTitle(p, i) {
  const c = i === panes.focus ? state.cur : p.convo;
  return c ? c.title : "新しいチャット";
}

function paintPanes() {
  const box = $("#panes");
  if (!box) return;
  const n = panes.list.length;
  box.dataset.n = String(n);
  panes.list.forEach((p, i) => {
    p.el.classList.toggle("is-focus", i === panes.focus);
    p.el.style.order = String(i);
    const t = p.el.querySelector(".cpane__title"); if (t) { t.textContent = paneTitle(p, i); t.title = t.textContent; }
    const g = p.el.querySelector(".cpane__ghost"); if (g) g.hidden = i === panes.focus;
  });
  const sp = $("#split-add");
  if (sp) { sp.disabled = n >= PANES_MAX; sp.title = n >= PANES_MAX ? `ペインは ${PANES_MAX} つまで` : "ペインを追加して横に並べる（Ctrl+\\）"; }
}

function savePanes() {
  try {
    localStorage.setItem("bw.panes", JSON.stringify({
      ids: panes.list.map((p, i) => ((i === panes.focus ? state.cur : p.convo)?.id ?? null)), focus: panes.focus }));
  } catch {}
}

/* 枠の器を作る（最初の枠は index.html にある物を使う）。 */
function makePane(el) {
  if (!el) {
    el = document.createElement("div");
    el.className = "cpane";
    el.innerHTML =
      '<div class="cpane__head"><button class="cpane__title" type="button"></button>' +
      '<button class="cpane__x" type="button" aria-label="このペインを閉じる" title="ペインを閉じる（チャットは停止しない）">×</button></div>' +
      '<div class="thread"></div>' +
      '<button class="cpane__ghost" type="button" hidden>ここに返信…</button>';
    if (!panes.restoring) enterOnce(el, "is-entering");   // 開いた枠だけ動かす（起動時に並びを戻すときは動かさない）
    $("#panes")?.appendChild(el);
  }
  const p = { el, th: el.querySelector(".thread"), convo: null, evidence: [] };
  el.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".cpane__x")) return;
    const i = panes.list.indexOf(p);
    if (i >= 0 && i !== panes.focus) focusPane(i);
  }, true);
  // ★キー（Tab）で枠の中に入っても選ぶ（2026-11-16 全体のレビュー: 選んでいない枠の Rewind・Retry が、選んでいる枠の会話に効いていた）。
  el.addEventListener("focusin", (e) => {
    if (e.target.closest(".cpane__x")) return;
    const i = panes.list.indexOf(p);
    if (i >= 0 && i !== panes.focus) focusPane(i);
  });
  el.querySelector(".cpane__x")?.addEventListener("click", () => closePane(panes.list.indexOf(p)));
  el.querySelector(".cpane__ghost")?.addEventListener("click", () => queueMicrotask(() => $("#q")?.focus()));
  bindThreadStick(p.th);
  panes.list.push(p);
  return p;
}

/* 選んでいる枠を替える。今の枠の走行はその場に置いたまま裏へ（DOM は動かさない）。 */
function focusPane(j) {
  if (j < 0 || j >= panes.list.length || j === panes.focus) return;
  const A = panes.list[panes.focus], B = panes.list[j];
  A.convo = state.cur;
  const rA = runOf(state.cur);
  if (rA && !rA.park) {
    rA.park = { inPane: true, nodes: null, evidence: state.evidence, scroll: 0, atEnd: true };
    $("#ask-box")?.remove();
    rA.live?.toBack?.();
    bindFg(null);
    setSending(false);
  } else A.evidence = state.evidence;
  A.th.removeAttribute("id");
  B.th.id = "thread";
  B.el.appendChild($("#cshell"));
  B.el.appendChild($("#ask"));
  panes.focus = j;
  state.cur = B.convo;
  const rB = runOf(B.convo);
  if (rB?.park?.inPane) {
    state.evidence = rB.park.evidence || [];
    rB.park = null;
    rB.live?.toFront?.();
    bindFg(rB);
    setSending(true);
    if (rB.ask) showAsk(rB.ask);
  } else state.evidence = B.evidence || [];
  renderEvidence();
  filesReset?.();
  if (B.convo) enterConvoWs(B.convo);
  const t = $("#top-title"); if (t) t.textContent = B.convo?.title || "新しいチャット";
  renderConvos();
  paintConvModel();
  paintPanes();
  savePanes();
}

/* 枠を足す。c があればその会話を、無ければ新しい会話の画面を出す。 */
function splitPane(c = null) {
  if (panes.list.length >= PANES_MAX) return false;
  if (c && paneOfConvo(c) >= 0) { focusPane(paneOfConvo(c)); return true; }
  const p = makePane(null);
  p.convo = null;
  focusPane(panes.list.length - 1);
  if (c) openConvo(c);
  else {
    p.th.innerHTML = "";
    if (HELLO) { p.th.appendChild(HELLO.cloneNode(true)); bindHelloCards(); }
  }
  paintPanes(); savePanes();
  queueMicrotask(() => $("#q")?.focus());
  return true;
}

/* 枠を閉じる。走っている会話は止めず、描いた物を記録へ預ける（左の一覧から開けば付け直す）。 */
function closePane(i) {
  if (i < 0 || panes.list.length <= 1) return;
  if (i === panes.focus) focusPane(i === 0 ? 1 : i - 1);
  const p = panes.list[i];
  const r = runOf(p.convo);
  if (r?.park?.inPane) r.park = { nodes: Array.from(p.th.childNodes), evidence: r.park.evidence || [], scroll: p.th.scrollTop, atEnd: true };
  p.el.remove();
  panes.list.splice(i, 1);
  if (panes.focus > i) panes.focus--;
  paintPanes(); savePanes();
}

/* 開いている会話が替わったとき（paintConvModel から）。枠の題を描き直して覚える。 */
function panesFollow() {
  if (!panes.list.length) return;
  panes.list[panes.focus].convo = state.cur;
  paintPanes(); savePanes();
}

/* 消えた会話を出している枠を閉じる（左の一覧を描き直すたびに・選んでいない枠だけ）。 */
function panesPrune() {
  for (let i = panes.list.length - 1; i >= 0; i--) {
    const c = panes.list[i].convo;
    if (i !== panes.focus && c && !state.convos.includes(c)) closePane(i);
  }
}

/* 最新を見ていたら、会話の欄の大きさが変わっても最新を見たままにする（2026-10-31 UI レビュー: Sources を開くと
 * 折り返しが増えて、答えの最後の数行が入力欄の下へ隠れていた）。読み返している間は動かさない。枠ごとに付ける。 */
function bindThreadStick(t) {
  if (!t || t.__stick || !("ResizeObserver" in window)) return;
  t.__stick = true;
  let atEnd = true;
  t.addEventListener("scroll", () => { atEnd = t.scrollHeight - t.scrollTop - t.clientHeight < 40; }, { passive: true });
  new ResizeObserver(() => { if (atEnd && t.clientHeight > 0) t.scrollTop = t.scrollHeight; }).observe(t);
}

/* 前回の並びを戻す（会話を読み込んだ後に 1 回）。 */
function restorePanes() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("bw.panes") || "null"); } catch {}
  const ids = Array.isArray(saved?.ids) ? saved.ids.slice(0, PANES_MAX) : [];
  if (ids.length < 2) return;
  const find = (id) => (id == null ? null : state.convos.find((x) => x.id === id) || null);
  const first = find(ids[0]);
  if (first) openConvo(first);
  panes.restoring = true;
  try { for (const id of ids.slice(1)) splitPane(find(id)); } finally { panes.restoring = false; }
  focusPane(Math.min(Math.max(0, saved.focus | 0), panes.list.length - 1));
}

(() => {
  const first = document.querySelector("#panes > .cpane");
  if (!first) return;
  makePane(first);
  $("#split-add")?.addEventListener("click", () => splitPane(null));
  paintPanes();
})();
