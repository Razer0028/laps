/* 別の窓のシェル（2026-11-17 オーナー「シェル機能はアプリと分離して表示できるようにしておくといいかもしれませんね」）。
 * 道のり: ここ（xterm.js）→ invoke local_shell_* → localshell.rs → この PC の擬似端末の既定の殻（本体の窓のシェルと同じ口）。
 * ・窓ごとに新しい殻。始めるフォルダは、窓を開いた会話のフォルダ（Rust の shell_window_cwd がこの窓の名前で預かっている）。
 * ・窓を閉じると殻も止める。殻が終わったら Enter でもう一度開く。
 * ・コピーと貼り付けは本体のシェルと同じ（選んでいるときの Ctrl+C はコピー・Ctrl+V は貼り付け・右クリックで選んだ所をコピー）。
 * ・色はアプリの設定（localStorage の bw.theme＝system/dark/light）に合わせ、ほかの窓で替えたら追う（storage の事象）。 */
(() => {
  const T = window.__TAURI__ ?? null;
  const call = T ? T.core.invoke : null;
  const stage = document.getElementById("stage");
  const nameEl = document.getElementById("name"), cwdEl = document.getElementById("cwd");
  if (!call || typeof window.Terminal !== "function" || !window.FitAddon) {
    stage.innerHTML = "";
    const p = document.createElement("p"); p.className = "err"; p.textContent = "シェルを開けません（このウィンドウではターミナルの部品が読めませんでした）";
    stage.appendChild(p);
    return;
  }

  const THEMES = {
    dark: { background: "#18181B", foreground: "#ECECF1", cursor: "#ECECF1", cursorAccent: "#18181B",
            selectionBackground: "rgba(255,255,255,.22)", selectionInactiveBackground: "rgba(255,255,255,.12)", black: "#2C2C31", red: "#FF7B72", green: "#7DDA8B", yellow: "#E3C27A", blue: "#8AB4F8", magenta: "#D2A8FF", cyan: "#7FD4D4", white: "#D0D0D6", brightBlack: "#8F8F99", brightRed: "#FFA198", brightGreen: "#A6E9B1", brightYellow: "#F0D79A", brightBlue: "#B1CCFB", brightMagenta: "#E2C5FF", brightCyan: "#A7E5E5", brightWhite: "#FFFFFF" },
    light: { background: "#E6E6EA", foreground: "#16161C", cursor: "#16161C", cursorAccent: "#E6E6EA",
             selectionBackground: "rgba(0,0,0,.16)", selectionInactiveBackground: "rgba(0,0,0,.08)", black: "#16161C", red: "#B42A20", green: "#1E7F3A", yellow: "#8A6D00", blue: "#1F5FBF", magenta: "#8E3FB0", cyan: "#0B7A7A", white: "#6E6E78", brightBlack: "#686872", brightRed: "#D23B30", brightGreen: "#25984A", brightYellow: "#A88400", brightBlue: "#2F74DA", brightMagenta: "#A653CB", brightCyan: "#0F9393", brightWhite: "#2C2C31" },
  };
  const themeName = () => {
    let pref = "system";
    try { const v = localStorage.getItem("bw.theme"); if (v === "dark" || v === "light") pref = v; } catch {}
    if (pref !== "system") return pref;
    return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  };
  const FONT = '"Cascadia Code","Cascadia Mono",Consolas,"BIZ UDGothic","Noto Sans Mono CJK JP",monospace';

  const term = new window.Terminal({
    fontFamily: FONT, fontSize: 13, lineHeight: 1.15, cursorBlink: true, cursorStyle: "bar", scrollback: 10000,
    theme: THEMES[themeName()], allowProposedApi: true, rightClickSelectsWord: false, drawBoldTextInBrightColors: false,
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  if (window.Unicode11Addon) { term.loadAddon(new window.Unicode11Addon.Unicode11Addon()); term.unicode.activeVersion = "11"; }
  term.open(stage);
  if (window.WebglAddon) {
    try { const gl = new window.WebglAddon.WebglAddon(); gl.onContextLoss(() => gl.dispose()); term.loadAddon(gl); } catch {}
  }

  const applyTheme = () => {
    const t = themeName();
    document.documentElement.dataset.theme = t;
    term.options.theme = THEMES[t];
  };
  applyTheme();
  window.addEventListener("storage", (e) => { if (e.key === "bw.theme") applyTheme(); });
  try { window.matchMedia?.("(prefers-color-scheme: light)").addEventListener("change", applyTheme); } catch {}

  let sid = null, ended = false, cwd = "", early = [];
  const copy = (text) => { try { navigator.clipboard?.writeText(text); } catch {} };
  const fitNow = () => {
    try { fit.fit(); } catch { return; }
    if (sid != null) call("local_shell_resize", { id: sid, cols: term.cols, rows: term.rows }).catch(() => {});
  };

  async function start() {
    ended = false; sid = null; early = [];
    fitNow();
    try {
      const r = await call("local_shell_open", { cols: term.cols || 100, rows: term.rows || 24, cwd: cwd || null });
      sid = r.id; cwd = r.cwd || cwd;
      nameEl.textContent = r.shell || "シェル";
      cwdEl.textContent = cwd; cwdEl.title = cwd;
      document.title = `${r.shell || "シェル"} — ${cwd || "L.A.P.S"}`;
      for (const p of early.splice(0)) onEvent(p);
      fitNow();
    } catch (e) {
      ended = true;
      term.write(`\x1b[38;2;255;156;142m✕ シェルを開けません: ${String(e)}\x1b[0m\r\n\x1b[2m── Enter でもう一度 ──\x1b[0m\r\n`);
    }
  }
  function onEvent(p) {
    if (sid == null) { if (!ended) early.push(p); return; }
    if (p.id !== sid) return;                    // ほかの窓・本体の殻の出力（事象は全部の窓へ届く）
    if (p.t === "o") {
      const bin = atob(p.d || ""); const u = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
      term.write(u);
    } else if (p.t === "exit") {
      sid = null; ended = true;
      term.write(`\r\n\x1b[2m── 終了しました${p.code != null ? `（コード ${p.code}）` : ""}。Enter でもう一度開きます ──\x1b[0m\r\n`);
    }
  }
  T.event.listen("local-shell", (ev) => onEvent(ev?.payload || {}));

  term.onData((d) => {
    if (ended) { if (d === "\r") { term.write("\r\n"); start(); } return; }
    if (sid != null) call("local_shell_input", { id: sid, data: d }).catch(() => {});
  });
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    const k = e.key.toLowerCase();
    if (e.ctrlKey && !e.altKey && k === "c" && (e.shiftKey || term.hasSelection())) {
      const t = term.getSelection(); if (t) { copy(t); term.clearSelection(); }
      e.preventDefault(); return false;
    }
    if (e.ctrlKey && !e.altKey && k === "v") return false;        // 貼り付けはブラウザに任せる（xterm が受ける）
    return true;
  });
  stage.addEventListener("contextmenu", (e) => {
    if (term.hasSelection()) { copy(term.getSelection()); term.clearSelection(); e.preventDefault(); }
  });
  document.getElementById("clear")?.addEventListener("click", () => { term.clear(); term.focus(); });
  new ResizeObserver(() => fitNow()).observe(stage);
  window.addEventListener("beforeunload", () => { if (sid != null) call("local_shell_close", { id: sid }).catch(() => {}); });

  (async () => {
    try { cwd = (await call("shell_window_cwd")) || ""; } catch { cwd = ""; }
    await start();
    term.focus();
  })();
})();
