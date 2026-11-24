// monaco の作業用 worker（差分の計算など）。CSP（script-src 'self'）に合わせて、同じ所のファイルから起こす。
// ★場所は読み込んだ時に覚える（呼ばれる時には document.currentScript が無い）。
(() => {
  const base = document.currentScript ? document.currentScript.src : location.href;
  self.MonacoEnvironment = { getWorker: () => new Worker(new URL("editor.worker.js", base)) };
})();
