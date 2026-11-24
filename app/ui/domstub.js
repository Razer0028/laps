// 最小の DOM 見立て。app.js が beginLive で触る分だけ。
class N {
  constructor(tag="div"){ this.tagName=tag.toUpperCase(); this.children=[]; this.parentNode=null;
    this.className=""; this._text=""; this.hidden=false; this.open=false; this.classList={
      _s:new Set(), add:(...c)=>c.forEach(x=>this.classList._s.add(x)),
      remove:(...c)=>c.forEach(x=>this.classList._s.delete(x)),
      contains:(c)=>this.classList._s.has(c),
      toggle:(c,on)=>{ const has=this.classList._s.has(c);
        const want = on===undefined ? !has : !!on;
        if (want) this.classList._s.add(c); else this.classList._s.delete(c); return want; } }; }
  get textContent(){ return this._text !== "" ? this._text : this.children.map(c=>c.textContent).join(""); }
  set textContent(v){ this._text=String(v); this.children=[]; }
  /* ★クラス名を固定で持たない。持っていたせいで、新しく足した要素（状態表示）が
     見えず、検査が静かに盲目になった。**実際の HTML から読み取る。** */
  set innerHTML(h){
    this.children=[];
    const src=String(h);
    for (const m of src.matchAll(/<(\w+)[^>]*class="([^"]+)"/g)) {
      const n=new N(m[1]); n.className=m[2]; n.parentNode=this; this.children.push(n);
    }
  }
  get innerHTML(){ return ""; }
  append(...cs){ cs.forEach(c=>this.appendChild(c)); }
  appendChild(c){ c.parentNode=this; this.children.push(c); return c; }
  insertBefore(c,ref){ const i=this.children.indexOf(ref); this.children.splice(i<0?this.children.length:i,0,c); c.parentNode=this; return c; }
  removeChild(c){ const i=this.children.indexOf(c); if(i>=0) this.children.splice(i,1); c.parentNode=null; return c; }
  remove(){ this.parentNode?.removeChild(this); }
  replaceWith(n){ const p=this.parentNode; if(!p) return; const i=p.children.indexOf(this);
    if(i>=0){ p.children[i]=n; n.parentNode=p; this.parentNode=null; } }
  _all(){ return this.children.flatMap(c=>[c,...c._all()]); }
  querySelector(sel){ const k=sel.replace(/^[.#]/,""); return this._all().find(n=>n.className.split(" ").includes(k)) || null; }
  querySelectorAll(sel){ const k=sel.replace(/^[.#]/,""); return this._all().filter(n=>n.className.split(" ").includes(k)); }
  /* ★心電図の描画そのものを検査で通すための最小の canvas。
     これが無いと fallback 側しか走らず、描画コードの誤りを見逃す。 */
  getContext(kind){ if(kind!=="2d") return null;
    /* ★2026-09-21: `setTransform` を足した。波を入力欄の幅まで広げた回で、
       実機には在る API がこの偽物に無く、**本番のコードが落ちた**（煙試験が捕まえた）。
       ★偽物を本番側で避けない（`typeof ctx.setTransform === "function"` のような
         防具を app.js に足すと、検収の器の都合が本番のコードに残る）。足りない側を足す。
       ⚠ この偽物が**測っていないもの**: 実際に描かれた画（座標・色・本数）は見ていない。
         ここで見ているのは「描画コードが例外を出さずに最後まで通るか」だけ。 */
    const noop=()=>{}; return { clearRect:noop, beginPath:noop, moveTo:noop, lineTo:noop,
      stroke:noop, setTransform:noop, save:noop, restore:noop,
      set lineWidth(v){}, set strokeStyle(v){} }; }
  scrollIntoView(){} 
  get scrollHeight(){return 0} get scrollTop(){return 0} set scrollTop(v){} get clientHeight(){return 0}
  /* ★属性を**実際に持つ**（2026-09-02）。no-op だったせいで、DOM に付ける属性は
     どの検査からも見えなかった＝`<ol start="3">`（2026-08-18 のオーナー報告
     「同じ出力が何度も出ているように見える」の修正）が**一度も試験されていなかった**。
     実測: 実機の返答 1,793 通のうち、1 で始まらない番号つきの並びは
     257 本中 91 本（35.4%）・35 通（2.0%）＝落とすと実際に見えなくなる。
     ⚠ app.js は getAttribute を1度も読まない（描画は代入のみ）ので、
       持たせても本番の経路は変わらない。 */
  setAttribute(k, v){ (this.__attrs ||= {})[k] = String(v); }
  getAttribute(k){ const v = (this.__attrs || {})[k]; return v === undefined ? null : v; }
  // ★handler を**実際に持つ**。空実装だと、登録された経路が一度も走らないまま
  //   「検査は緑」になる。2026-08-18 の敵対レビューで、停止ボタンの経路が
  //   **一度も実行されたことがない**と分かった（grep のゲートは通っていた）。
  cloneNode(deep){ const c = new N(this.tag || "div"); c.className = this.className; return c; }
  closest(){ return null } contains(){ return false } focus(){} blur(){} click(){}
  addEventListener(type, fn){ (this.__h ||= {})[type] = [...(this.__h?.[type]||[]), fn]; }
  dispatchEvent(ev){ for (const fn of (this.__h?.[ev?.type] || [])) fn(ev); return true; }
}
const thread = new N("div"); thread.className="thread";
const byId = {};
global.document = {
  createElement:(t)=>new N(t),
  createTextNode:(t)=>{ const n=new N("#text"); n.textContent=String(t); return n; },
  /* ★id で引くものは、実物と同じ形の器を返す。null を返すと
     ガードで素通りし、**描画コードが検査を通らない**（心電図がまさにそれだった）。 */
  querySelector:(s)=>{
    if (s==="#thread") return thread;
    if (typeof s==="string" && s.startsWith("#")) {
      const k=s.slice(1);
      byId[k] = byId[k] || Object.assign(new N(k==="runbar-wave"?"canvas":"div"), {className:k});
      return byId[k];
    }
    return null;
  },
  // ★木を実際にたどる。常に [] を返していたので、画面の中身を見る検査が
  //   **何を書いても緑**になっていた（2026-08-18: 道具行の対称性を見ようとして発覚）。
  querySelectorAll(sel){ return thread.querySelectorAll(sel); },
  getElementById(id){ return this.querySelector("#" + id); },   // voiceassist.js が使う（run_va_supervise.js）
  addEventListener(){},
  body: new N("body"),
};
global.window = { addEventListener(){}, localStorage:{ getItem:()=>null, setItem(){}, removeItem(){} } };
global.localStorage = global.window.localStorage;
module.exports = { thread, N };

// 状態表示のタイマー（本物の window が無いので最小限）
globalThis.setInterval = globalThis.setInterval || ((f,t)=>0);
globalThis.clearInterval = globalThis.clearInterval || (()=>{});
// ★2026-11-16: app.js が 11-04 から requestAnimationFrame を使い、ここに無くて run_runbar.js などが途中で落ちていた。
//   画面を描かない試験なので、次の空き時間に呼ぶだけ（描画の時刻は要らない）。
globalThis.requestAnimationFrame = globalThis.requestAnimationFrame || ((f)=>setTimeout(()=>f(0), 0));
globalThis.cancelAnimationFrame = globalThis.cancelAnimationFrame || ((id)=>clearTimeout(id));

// ★Tauri の橋。**存在しないと、押下経路が非同期に落ちて検査が「押せていない」ことに
//   気づけない**（2026-08-18: 停止の検査が invoke 未定義で halt に届いていなかった）。
//   呼ばれたコマンド名を記録して、検査が「何を撃ったか」を確かめられるようにする。
global.__invoked = [];
global.invoke = async (cmd, args) => {
  global.__invoked.push(cmd);
  if (typeof global.__invokeSpy === "function") global.__invokeSpy(cmd, args);
  // 画面が値を読む口はそれらしい既定を返す（null を返すと分岐が変わる）。
  // ★実物と同じ形にする（2026-09-06 で欄が増えた）。欠けていると、
  //   画面が新しい欄を読む経路が検査から**見えないまま**通る。
  if (cmd === "backend_info") return {
    remote: false, url: "", model: "",
    effective: "http://127.0.0.1:8760/v1", source: "auto_local",
    source_label: "自動（ローカルが使える）", local_ok: true, local_port: 8760,
  };
  if (cmd === "lan_settings_get") return {
    enabled: false, url: "http://127.0.0.1:8080", model: "default", mode: "auto",
  };
  if (cmd === "backend_probe") return false;
  return null;
};
global.listen = async () => (() => {});
// ★app.js は `const invoke = TAURI ? TAURI.core.invoke : null`（app.js:17）で
//   **自分の束縛**を作るので、global.invoke を置くだけでは届かない。
//   window.__TAURI__ を用意して、その道を通す。
global.window.__TAURI__ = {
  core: { invoke: (...a) => global.invoke(...a) },
  event: { listen: global.listen },
};
