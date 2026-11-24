/* ── 背景の点の格子（2026-11-17 オーナー「背景をシンプルだけど面白いデザインにしたい」「ガラスデザインに合うやつ」「任せます」）──
 * 窓全体（左の板の下も）に細かい点の格子を敷く。ガラスの板（左右の板・入力欄）の後ろに点があるので、ぼかしが目に見える。
 *   ・止まっているときは 1 回描くだけ（動かない＝負荷なし）。
 *   ・指の近くの点だけ少し明るくなる。
 *   ・エージェントが動いている間（body.is-busy）は、真ん中のスペクトラムの球から輪がゆっくり広がる（色はスペクトラムと同じ赤／青）。
 *     動きは状態を知らせるためなので、動きを減らす設定でも止めない（字の光と同じ扱い・オーナー 2026-10-09）。
 * 色は CSS の変数（--dot・--dot-a・--spec）から読む。テーマが替わったら（bw-theme）読み直す。
 * 手本: React Bits の Dot Grid・Magic UI の Flickering Grid・Aceternity の点の背景（形だけ・コードは使っていない）。 */
(() => {
  if (typeof document === "undefined") return;
  const canvas = document.createElement("canvas");
  canvas.className = "bgfield";
  canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);
  const ctx = canvas.getContext("2d");
  if (!ctx) { canvas.remove(); return; }

  const GAP = 24, SIZE = 1.6, GLOW_R = 150, RING_EVERY = 2600, RING_LIFE = 3400, RING_W = 40, FRAME_MS = 33;
  let W = 0, H = 0, dpr = 1, dot = [255, 255, 255], dotA = 0.075, spec = [208, 52, 44];
  let pointer = null, rings = [], lastSpawn = 0, raf = 0, lastFrame = 0, busy = false;

  const triple = (v, fb) => {
    const p = String(v || "").trim().split(/[\s,]+/).map(Number);
    return p.length === 3 && p.every((x) => Number.isFinite(x)) ? p : fb;
  };
  function readTheme() {
    const cs = getComputedStyle(document.documentElement);
    dot = triple(cs.getPropertyValue("--dot"), dot);
    const a = parseFloat(cs.getPropertyValue("--dot-a"));
    if (Number.isFinite(a)) dotA = a;
    spec = triple(cs.getPropertyValue("--spec"), spec);
  }
  function size() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
  }
  /* 波の始まり＝スペクトラムの球の中心（synapse.js と同じ: 本文の枠の幅の中央・高さの 44%）。 */
  function origin() {
    const m = document.querySelector(".main");
    const r = m ? m.getBoundingClientRect() : { left: 0, top: 0, width: W, height: H };
    const R = Math.max(60, Math.min(220, Math.min(r.width * 0.2, r.height * 0.19)));
    return { x: r.left + r.width / 2, y: r.top + r.height * 0.44, R };
  }
  const ease = (p) => (p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2);   // ★外へ一気に出ない（ease-out だと大半の時間が窓の外だった）

  function draw(now) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const o = origin();
    const far = Math.hypot(Math.max(o.x, W - o.x), Math.max(o.y, H - o.y));
    const live = rings.map((t0) => {
      const p = Math.min(1, (now - t0) / RING_LIFE);
      return { r: o.R * 1.15 + (far - o.R * 1.15) * ease(p), k: Math.pow(1 - p, 1.4) * 0.75 };
    });
    const x0 = ((W % GAP) / 2) || GAP / 2, y0 = ((H % GAP) / 2) || GAP / 2;
    const base = `rgba(${dot[0]},${dot[1]},${dot[2]},${dotA})`;
    const half = SIZE / 2;
    ctx.fillStyle = base;
    for (let y = y0; y < H; y += GAP) for (let x = x0; x < W; x += GAP) ctx.fillRect(x - half, y - half, SIZE, SIZE);
    // 指の近く・波の輪の上の点だけ、上から重ねて明るくする（全部の点を描き直さない）。
    if (pointer) {
      const gx0 = Math.max(x0, x0 + Math.ceil((pointer.x - GLOW_R - x0) / GAP) * GAP);
      const gy0 = Math.max(y0, y0 + Math.ceil((pointer.y - GLOW_R - y0) / GAP) * GAP);
      for (let y = gy0; y <= pointer.y + GLOW_R && y < H; y += GAP) {
        for (let x = gx0; x <= pointer.x + GLOW_R && x < W; x += GAP) {
          const d = Math.hypot(x - pointer.x, y - pointer.y);
          if (d >= GLOW_R) continue;
          const k = Math.pow(1 - d / GLOW_R, 2);
          ctx.fillStyle = `rgba(${dot[0]},${dot[1]},${dot[2]},${(0.22 * k).toFixed(3)})`;
          ctx.fillRect(x - half - 0.25, y - half - 0.25, SIZE + 0.5, SIZE + 0.5);
        }
      }
    }
    if (live.length) {
      for (let y = y0; y < H; y += GAP) {
        for (let x = x0; x < W; x += GAP) {
          const d = Math.hypot(x - o.x, y - o.y);
          let b = 0;
          for (const g of live) { const t = 1 - Math.abs(d - g.r) / RING_W; if (t > 0) b += g.k * t; }
          if (b < 0.02) continue;
          ctx.fillStyle = `rgba(${spec[0]},${spec[1]},${spec[2]},${Math.min(0.85, b).toFixed(3)})`;
          ctx.fillRect(x - half - 0.8, y - half - 0.8, SIZE + 1.6, SIZE + 1.6);
        }
      }
    }
  }

  // ★GPU（2026-11-21 オーナー「UI/UX の GPU 使用率が下がるように」）: 走行中は毎フレーム（60 回/秒）起きて 30fps で窓全体を描いていた。
  //   窓全体の下には左の板・入力欄のぼかし（backdrop-filter）があり、描くたびにぼかしも作り直しになる。球（synapse.js）とずれた時刻に
  //   描くと作り直しが倍になるので、両方を同じ刻み（window.BW_ANIM_MS・既定 50ms＝20fps）にそろえ、合間は setTimeout で寝る。
  //   指を動かした時（輪が無い時）はその場で 1 回描く。
  // 共有の時計（bgfield.js と synapse.js の両方に同じ物・先に読まれた方が作る）: 刻みごとに 1 回だけ requestAnimationFrame を頼み、
  //   その 1 フレームで頼まれた描画を全部呼ぶ＝2 つの絵がいつも同じフレームで描かれ、上のぼかしの作り直しが 1 回で済む（2026-11-21）。
  const CLOCK = window.BWAnimClock || (window.BWAnimClock = (() => {
    const want = new Set(); let timer = 0, raf = 0;
    const run = (ts) => { raf = 0; const fs = [...want]; want.clear(); fs.forEach((f) => { try { f(ts); } catch (e) { console.error(e); } }); };
    const arm = () => {
      if (timer || raf) return;
      const P = window.BW_ANIM_MS || 50, wait = Math.max(0, (Math.floor(performance.now() / P) + 1) * P - performance.now());
      timer = setTimeout(() => { timer = 0; raf = requestAnimationFrame(run); }, wait);
    };
    return { next(f) { want.add(f); arm(); }, now(f) { want.add(f); if (!raf) { clearTimeout(timer); timer = 0; raf = requestAnimationFrame(run); } } };
  })());
  function tick(now) {
    raf = 0;
    if (document.hidden) return;
    if (busy && now - lastSpawn >= RING_EVERY) { rings.push(now); lastSpawn = now; }
    rings = rings.filter((t0) => now - t0 < RING_LIFE);
    draw(now); lastFrame = now;
    if (busy || rings.length) { raf = 1; CLOCK.next(tick); }
  }
  const wake = () => { if (!raf && !document.hidden) { raf = 1; CLOCK.now(tick); } };

  readTheme(); size(); draw(performance.now());
  window.addEventListener("resize", () => { size(); wake(); });
  window.addEventListener("bw-theme", () => { requestAnimationFrame(() => { readTheme(); wake(); }); });
  window.addEventListener("pointermove", (e) => { pointer = { x: e.clientX, y: e.clientY }; wake(); }, { passive: true });
  document.addEventListener("pointerleave", () => { pointer = null; wake(); });
  window.addEventListener("blur", () => { pointer = null; wake(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) wake(); });
  const sync = () => {
    const b = document.body.classList.contains("is-busy");
    if (b !== busy) { busy = b; if (busy) lastSpawn = 0; wake(); }
  };
  new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  // 左の板の開閉・右の板の開閉で本文の枠が動くと、波の中心も動く
  if (typeof ResizeObserver === "function") { const m = document.querySelector(".main"); if (m) new ResizeObserver(wake).observe(m); }
  sync();
  window.BWBgField = { redraw: wake, rethemed: () => { readTheme(); wake(); }, state: () => ({ busy, rings: rings.length, raf: !!raf, W, H, dotA, spec }) };
})();
