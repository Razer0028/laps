/* ── 会話のシェル（2026-11-11 オーナー「シェルを会話の画面から開けるようにしましょうかね」）。
 * 道のり: ここ（xterm.js）→ invoke local_shell_* → localshell.rs → この PC の擬似端末の既定の殻
 *   （Windows は PowerShell 7 か 5.1・Linux は $SHELL）。こちらは手元の殻。
 * ・会話の下に開く。会話のワークスペースのフォルダで始まる（エージェントの道具が動くのと同じ場所）。
 * ・会話ごとに 1 本。会話を移ると、その会話の殻に切り替わる（前の殻は動き続ける）。
 * ・上の帯の >_ か Ctrl+J で開閉。上の縁を引いて高さを変える。閉じる（×）と殻も止まる。
 * ・コピーと貼り付けは Console と同じ（選んでいるときの Ctrl+C はコピー・Ctrl+V は貼り付け・右クリックで選んだ所をコピー）。
 * ★別の機械の会話（state.cur.remote）では開かない（殻はこの PC のもの）。 */
(() => {
  if (typeof document === "undefined") return;
  const dock = document.getElementById("cshell");
  if (!dock) return;
  const T = typeof window !== "undefined" ? (window.__TAURI__ ?? null) : null;
  const call = T ? T.core.invoke : null;
  const q = (s) => dock.querySelector(s);
  const stage = q(".cshell__stage"), nameEl = q(".cshell__name"), cwdEl = q(".cshell__cwd"), note = q(".cshell__note");
  const toggleBtn = document.getElementById("shell-toggle");

  const LS_H = "bw.shell.height", LS_OPEN = "bw.shell.open";
  const ls = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
               set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
  const THEME = {
    background: "#131011", foreground: "#EAE4E3", cursor: "#D0342C", cursorAccent: "#131011",
    selectionBackground: "rgba(208,52,44,.38)", selectionInactiveBackground: "rgba(208,52,44,.22)",
    black: "#221B1D", red: "#E0453D", green: "#7FA08E", yellow: "#D8B26A", blue: "#7C9CC4", magenta: "#C28BB8",
    cyan: "#78AFAF", white: "#CFC6C4", brightBlack: "#6E6362", brightRed: "#FF6B62", brightGreen: "#9CC2AD",
    brightYellow: "#EDCB86", brightBlue: "#9DBBE0", brightMagenta: "#DBA6D2", brightCyan: "#98CDCD", brightWhite: "#F4EFEE",
  };
  const FONT = '"Cascadia Code","Cascadia Mono",Consolas,"BIZ UDGothic","Noto Sans Mono CJK JP","MS Gothic",monospace';
  const termTheme = () => (typeof window.bwTermTheme === "function" ? window.bwTermTheme() : THEME);   // 2026-11-17 テーマに合わせる
  const libOK = () => typeof window.Terminal === "function" && window.FitAddon && window.Unicode11Addon;

  const byConvo = new Map();   // 会話の id → { sid, term, fit, host, shell, cwd, ended, early }
  const bySid = new Map();     // Rust の番号 → 同じ物
  let open = ls.get(LS_OPEN) === "1";
  let cur = null;              // いま出している殻

  const curConvo = () => { try { return state.cur || null; } catch { return null; } };
  const convoKey = (c) => (c ? String(c.id) : "");
  const convoDir = (c) => { try { return c?.ws || (typeof wsPath === "function" ? wsPath() : "") || ""; } catch { return ""; } };

  function say(msg) { note.textContent = msg || ""; note.hidden = !msg; }

  function setHeight(px) {
    const max = Math.max(160, (dock.parentElement?.clientHeight || 800) - 220);
    const h = Math.round(Math.min(max, Math.max(120, px)));
    dock.style.height = h + "px";
    return h;
  }

  function paintHead(s) {
    nameEl.textContent = s?.shell || "シェル";
    cwdEl.textContent = s?.cwd || "";
    cwdEl.title = s?.cwd || "";
    dock.classList.toggle("is-ended", !!s?.ended);
  }

  function fitNow(s) {
    if (!s || !open || dock.hidden) return;
    try { s.fit.fit(); } catch { return; }
    const { cols, rows } = s.term;
    if (s.sid != null && (cols !== s.cols || rows !== s.rows)) {
      s.cols = cols; s.rows = rows;
      call("local_shell_resize", { id: s.sid, cols, rows }).catch(() => {});
    }
  }

  function copy(text) { try { navigator.clipboard?.writeText(text); } catch {} }

  function make(c) {
    const host = document.createElement("div");
    host.className = "cshell__term";
    stage.appendChild(host);
    const term = new window.Terminal({
      fontFamily: FONT, fontSize: 13, lineHeight: 1.15, cursorBlink: true, cursorStyle: "bar", scrollback: 5000,
      theme: termTheme(), allowProposedApi: true, rightClickSelectsWord: false, drawBoldTextInBrightColors: false,
    });
    const fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new window.Unicode11Addon.Unicode11Addon());
    term.unicode.activeVersion = "11";
    term.open(host);
    if (window.WebglAddon) {
      try { const gl = new window.WebglAddon.WebglAddon(); gl.onContextLoss(() => gl.dispose()); term.loadAddon(gl); } catch {}
    }
    const s = { key: convoKey(c), sid: null, term, fit, host, shell: "", cwd: convoDir(c), ended: false, early: [], cols: 0, rows: 0 };
    term.onData((d) => {
      if (s.ended) { if (d === "\r") { term.write("\r\n"); start(s); } return; }
      if (s.sid != null) call("local_shell_input", { id: s.sid, data: d }).catch(() => {});
    });
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== "keydown") return true;
      const k = e.key.toLowerCase();
      if (e.ctrlKey && !e.altKey && k === "c" && (e.shiftKey || term.hasSelection())) {
        const t = term.getSelection(); if (t) { copy(t); term.clearSelection(); }
        e.preventDefault(); return false;
      }
      if (e.ctrlKey && !e.altKey && k === "v") return false;          // 貼り付けはブラウザに任せる（xterm が受ける）
      if (e.ctrlKey && !e.shiftKey && !e.altKey && k === "j") return false;   // 開閉は app.js のショートカット表（ここで閉じると二重に効く）
      return true;
    });
    host.addEventListener("contextmenu", (e) => {
      if (term.hasSelection()) { copy(term.getSelection()); term.clearSelection(); e.preventDefault(); }
    });
    byConvo.set(s.key, s);
    return s;
  }

  async function start(s) {
    s.ended = false; s.sid = null; paintHead(s);
    fitNow(s);
    try {
      const r = await call("local_shell_open", { cols: s.term.cols || 100, rows: s.term.rows || 20, cwd: s.cwd || null });
      s.sid = r.id; s.shell = r.shell || ""; s.cwd = r.cwd || s.cwd;
      bySid.set(s.sid, s);
      for (const p of s.early.splice(0)) onEvent(p);
      s.cols = 0; fitNow(s);
    } catch (e) {
      s.ended = true;
      s.term.write(`\x1b[38;2;232;99;92m✕ シェルを開けません: ${String(e)}\x1b[0m\r\n\x1b[2m── Enter でもう一度 ──\x1b[0m\r\n`);
    }
    if (s === cur) paintHead(s);
  }

  function onEvent(p) {
    const s = bySid.get(p.id);
    if (!s) { for (const x of byConvo.values()) if (x.sid == null && !x.ended) { x.early.push(p); return; } return; }
    if (p.t === "o") {
      const bin = atob(p.d || ""); const u = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
      s.term.write(u);
    } else if (p.t === "exit") {
      bySid.delete(p.id); s.sid = null; s.ended = true;
      s.term.write(`\r\n\x1b[2m── 終了しました${p.code != null ? `（コード ${p.code}）` : ""}。Enter でもう一度開きます ──\x1b[0m\r\n`);
      if (s === cur) paintHead(s);
    }
  }

  function show(s) {
    for (const x of byConvo.values()) x.host.hidden = x !== s;
    cur = s;
    paintHead(s);
    if (s) requestAnimationFrame(() => { fitNow(s); s.term.focus(); });
  }

  /** いま開いている会話の殻を出す（無ければ作る）。開いていないときは何もしない。 */
  function follow() {
    if (!open) return;
    const c = curConvo();
    if (c?.remote) {
      for (const x of byConvo.values()) x.host.hidden = true;
      cur = null; paintHead(null);
      say(`このチャットは ${c.remote.name || "別のマシン"} で動いています。シェルはこの PC の物なので、ここでは開きません。`);
      return;
    }
    say("");
    const key = convoKey(c) || "_";
    if (cur && cur.key === key) return;
    // ★会話の無い画面（新しい会話の前）で開いた殻は、最初の送信でできた会話がそのまま引き継ぐ（置き去りの殻を作らない）。
    const loose = byConvo.get("_");
    if (key !== "_" && loose && !byConvo.has(key) && loose.cwd === convoDir(c)) {
      byConvo.delete("_"); loose.key = key; byConvo.set(key, loose);
    }
    const s = byConvo.get(key) || make(c || { id: "_" });
    show(s);
    if (s.sid == null && !s.ended) start(s);
  }

  function setOpen(on) {
    if (on && !call) { open = true; dock.hidden = false; say("この画面ではシェルを使えません（アプリの中で開いてください）。"); return; }
    if (on && !libOK()) { open = true; dock.hidden = false; say("ターミナルの部品（vendor/xterm.js）を読めません。アプリを入れ直してください。"); return; }
    open = on;
    ls.set(LS_OPEN, on ? "1" : "0");
    dock.hidden = !on;
    toggleBtn?.setAttribute("aria-pressed", String(on));
    if (on) {
      setHeight(+ls.get(LS_H) || 260);
      follow();
    } else {
      cur = null;
      document.getElementById("q")?.focus?.();
    }
  }

  /** 閉じる（×）: この会話の殻を止めて欄を畳む。 */
  function closeCur() {
    const s = cur;
    if (s) {
      if (s.sid != null) { call("local_shell_close", { id: s.sid }).catch(() => {}); bySid.delete(s.sid); }
      s.term.dispose(); s.host.remove(); byConvo.delete(s.key);
    }
    cur = null;
    setOpen(false);
  }

  /** 別の窓で開く（2026-11-17 オーナー「シェル機能はアプリと分離して表示できるようにしておくといいかもしれませんね」）。
   *  窓ごとに新しい殻（この会話のフォルダで始まる・中身は shellwin.js）。この欄は畳む（殻は動いたまま・Ctrl+J で戻せる）。 */
  async function popOut() {
    const c = curConvo();
    if (c?.remote) { setOpen(true); say("別のマシンのチャットのシェルは、新しいウィンドウでは開けません（シェルはこの PC のもの）。"); return; }
    if (!call) { setOpen(true); say("この画面ではシェルを使えません（アプリの中で開いてください）。"); return; }
    try {
      await call("shell_window_open", { cwd: convoDir(c) || null });
      if (open) setOpen(false);
    } catch (e) { setOpen(true); say(`新しいウィンドウを開けません: ${String(e?.message || e)}`); }
  }
  q(".cshell__pop")?.addEventListener("click", popOut);
  window.addEventListener("bw-theme", () => { for (const s of byConvo.values()) { try { s.term.options.theme = termTheme(); } catch {} } });
  q(".cshell__close")?.addEventListener("click", closeCur);
  q(".cshell__hide")?.addEventListener("click", () => setOpen(false));
  q(".cshell__clear")?.addEventListener("click", () => { cur?.term.clear(); cur?.term.focus(); });
  toggleBtn?.addEventListener("click", () => setOpen(!open));

  // 上の縁を引いて高さを変える
  const grip = q(".cshell__grip");
  grip?.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    const y0 = e.clientY, h0 = dock.getBoundingClientRect().height;
    const move = (ev) => setHeight(h0 + (y0 - ev.clientY));
    const up = () => {
      grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up);
      ls.set(LS_H, String(Math.round(dock.getBoundingClientRect().height)));
      fitNow(cur);
    };
    grip.addEventListener("pointermove", move); grip.addEventListener("pointerup", up);
  });
  grip?.addEventListener("keydown", (e) => {                          // 縁に Tab で来たら ↑↓ で 24px ずつ
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    const h = setHeight(dock.getBoundingClientRect().height + (e.key === "ArrowUp" ? 24 : -24));
    ls.set(LS_H, String(h)); fitNow(cur);
  });

  if (T?.event?.listen) T.event.listen("local-shell", (ev) => onEvent(ev?.payload || {}));
  new ResizeObserver(() => { clearTimeout(fitNow._t); fitNow._t = setTimeout(() => fitNow(cur), 60); }).observe(stage);

  window.bwShell = { follow, open: () => setOpen(true), close: () => setOpen(false), toggle: () => setOpen(!open),
                     isOpen: () => open, popOut };
  // 前回開いていたら、最初に会話が決まったとき（paintConvModel → follow）に開く。ここでは欄だけ出しておく。
  if (open) { open = false; queueMicrotask(() => setOpen(true)); }
})();
