/* BLACKWALL — チャット背景のシナプスの球（2026-10-09 オーナー「入力欄の上の波をなくして、チャット画面の背景に
 * オーディオスペクトラムのようなものを。モデルの思考に合わせて動かす。赤・黒を基調のアメーバ型シナプス」
 * ＋「デジタル要素も」「Out は Think と違う動き」「Tool の時も」「ジャービスみたいな感じ」「立体の球状で」）。
 *
 * 見た目（JARVIS の球を、赤と黒の神経網で）:
 *   ・球 … 球面に神経細胞（点）を並べ、近い細胞を球面に沿う糸でつなぐ。ゆっくり回る（奥は暗く・手前は明るい）。
 *     届いた思考・本文の文字を 48 の帯に振り分けて溜め、細胞ごとに受け持つ帯の強さで球面が外へ盛り上がる
 *     （＝球そのものがスペクトラム。ゆらぎも足してアメーバのように波打つ）。
 *   ・周り … 円形の LED スペクトラムの輪（48 帯）・回る破線の HUD の輪・目盛りの弧・傾いた 2 本の軌道の輪・
 *     16 進の読み出し・中心の光る芯。信号は四角い粒（データの粒）で走る。
 *   ・段階ごとに動きを変える:
 *       Think … 球面が細かくばらばらに波打ち、粒が球の上を細胞から細胞へ走る（探っている）。
 *               ★思考の量では強めない決まった動き（2026-10-19 オーナー「重くみえるので固定のアニメーションでいいかも」）。
 *       Out   … 球が文字の勢いで脈打ち、粒が球面から外の輪へ放たれ、波紋が広がる（まとめて出している）。
 *       Tool  … 走査の輪が球を上下になぞり、通った細胞が光る。輪の端に道具の名前（読みに行っている）。
 *       待ち  … ゆっくり呼吸して回る。 終わり … 暗くして止める。
 * ★OS の「動きを減らす」（Windows のアニメーション効果オフ）でも止めない（オーナー指示 2026-10-09）。
 * ★負荷: 動かすのは走行中と、終わってから静まるまで・マウスを動かした間だけ（静まったら 1 枚描いて止める）。
 *   画素密度は 1.5 で頭打ち、20 fps（点の背景と同じ刻み・2026-11-21）。窓が隠れている間は requestAnimationFrame 自体が止まる。
 * ★本文の読みやすさ: 線は細く、面はほとんど塗らない。中央（会話の列）は CSS の mask で少し薄くする（styles.css .synapse）。
 */
"use strict";
(() => {
  if (typeof window === "undefined" || typeof document === "undefined") return;

  const BANDS = 48;
  // Think の決まった動き（2026-10-19）: 細胞と輪の明るさの中心と振れ幅、粒の出る割合（細胞 1 つ・1 秒あたり）と上限。
  const THINK_E = 0.34, THINK_AMP = 0.18, THINK_RATE = 0.06, THINK_PACKETS = 24;
  // ★色はテーマから読む（2026-11-17 ガラスの意匠: 濃い灰の窓は赤・白い窓は青）。styles.css の --spec・--spec-hot・--spec-dim
  //   （R G B を空白で）・--spec-blend（重ね方）・--spec-boost（濃さ）。白い面に加算（lighter）で重ねると見えないので source-over。
  let RED = [208, 52, 44], HOT = [255, 92, 72], DIM = [142, 36, 30], BLEND = "lighter", BOOST = 1;
  function readTheme() {
    const cs = getComputedStyle(document.documentElement);
    const rgb = (name, d) => {
      const v = cs.getPropertyValue(name).trim().split(/[\s,]+/).map(Number);
      return v.length === 3 && v.every((x) => Number.isFinite(x) && x >= 0 && x <= 255) ? v : d;
    };
    RED = rgb("--spec", [208, 52, 44]); HOT = rgb("--spec-hot", [255, 92, 72]); DIM = rgb("--spec-dim", [142, 36, 30]);
    const b = cs.getPropertyValue("--spec-blend").trim();
    BLEND = b === "source-over" ? "source-over" : "lighter";
    BOOST = Number(cs.getPropertyValue("--spec-boost")) || 1;
  }
  const MONO = '"IBM Plex Mono","Cascadia Mono",Consolas,monospace';
  const TAU = Math.PI * 2;
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${Math.max(0, Math.min(1, a * BOOST)).toFixed(3)})`;
  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
  const rand = (a, b) => a + Math.random() * (b - a);

  let canvas = null, ctx = null, host = null;
  const LABEL_A = 0.3;   // 背景の文字の濃さ（会話の文字の邪魔をしない）
  let W = 0, H = 0, dpr = 1, R = 100, cx = 0, cy = 0;
  let verts = [], edges = [], adj = [];
  let packets = [], rings = [], glyphs = [];
  const band = new Float32Array(BANDS);
  const recent = [];                   // 最近届いた文字の符号（16 進で出す）
  let phase = "idle";                  // idle | wait | think | write | tool | compact
  let prog = 0, progShown = 0;         // compact の進み具合（0〜1）
  let toolName = "";
  let level = 0, pulse = 0;            // 勢い（文字の量）・Out の脈
  let yaw = 0.6, pitch = 0.32, spin = 0;
  let raf = 0, lastT = 0, acc = 0, t = 0, phaseAt = 0;
  let mouse = null;                    // { x, y, at }（球を少し傾ける・近くの細胞を光らせる）

  // ── 球面に細胞を並べる（フィボナッチ格子）・近い 3 つとつなぐ ─────────────
  function build() {
    const N = 220, ga = Math.PI * (3 - Math.sqrt(5));
    verts = [];
    for (let i = 0; i < N; i++) {
      const y = 1 - (2 * (i + 0.5)) / N, r = Math.sqrt(1 - y * y), th = i * ga;
      const x = Math.cos(th) * r, z = Math.sin(th) * r;
      const lon = (Math.atan2(z, x) + Math.PI) / TAU, lat = (y + 1) / 2;
      verts.push({ u: [x, y, z], b: (((lon * 16) | 0) + ((lat * 3) | 0) * 16) % BANDS,
                   e: 0, glow: 0, p: null, d: 1 });
    }
    const seen = new Set();
    edges = []; adj = verts.map(() => []);
    verts.forEach((a, i) => {
      const near = verts.map((b, j) => ({ j, d: a.u[0] * b.u[0] + a.u[1] * b.u[1] + a.u[2] * b.u[2] }))
        .filter((o) => o.j !== i).sort((p, q) => q.d - p.d).slice(0, 3);
      for (const { j } of near) {
        const key = i < j ? i + ":" + j : j + ":" + i;
        if (seen.has(key)) continue;
        seen.add(key);
        adj[i].push(edges.length); adj[j].push(edges.length);
        edges.push({ a: i, b: j });
      }
    });
  }

  function size() {
    if (!canvas || !host) return false;
    const w = Math.max(1, host.clientWidth), h = Math.max(1, host.clientHeight);
    const d = Math.min(window.devicePixelRatio || 1, 1.5);
    if (w === W && h === H && d === dpr) return false;
    W = w; H = h; dpr = d;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    R = clamp(Math.min(W * 0.2, H * 0.19), 60, 220);   // 外側の帯の番号（2.16R）まで窓に収める
    cx = W / 2; cy = H * 0.44;
    return true;
  }

  // ── 3D: 回して映す ────────────────────────────────────────────────
  function rot(v) {
    const cy_ = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    const x1 = v[0] * cy_ + v[2] * sy, z1 = -v[0] * sy + v[2] * cy_;
    return [x1, v[1] * cp - z1 * sp, v[1] * sp + z1 * cp];
  }
  const CAM = 3.4;
  function proj(v, r) {                // v: 回した単位ベクトル、r: 半径の倍率 → 画面の点と奥行き（0 奥〜1 手前）
    const z = v[2] * r, s = CAM / (CAM - z);
    return { x: cx + v[0] * r * R * s, y: cy - v[1] * r * R * s, k: (z / Math.max(0.6, r) + 1) / 2, s };
  }

  // 球面のゆらぎ（アメーバ）: 方向ごとの波の重ね。Think は細かく速く、Out はなめらかに大きく。
  const WAVES = [[0.8, 0.3, 0.5], [-0.2, 0.9, 0.4], [0.5, -0.4, 0.77], [-0.6, -0.2, -0.77]];
  function wobble(u) {
    const f = phase === "think" ? 4.2 : phase === "write" ? 2.0 : 2.6;
    const a = phase === "think" ? 0.045 : phase === "idle" ? 0.012 : 0.028;
    let s = 0;
    WAVES.forEach((w, k) => { s += Math.sin(f * (u[0] * w[0] + u[1] * w[1] + u[2] * w[2]) * (1 + k * 0.35) + t * (0.9 + k * 0.4)); });
    return 1 + a * s;
  }

  // ── 1 コマ進める ──────────────────────────────────────────────────
  function step(dt) {
    t += dt;
    for (let i = 0; i < BANDS; i++) band[i] *= Math.exp(-dt / (phase === "write" ? 0.8 : 0.55));
    level *= Math.exp(-dt / 1.6);
    pulse *= Math.exp(-dt / 0.25);
    // 回る速さ: Tool は速く、Think・Out はふつう、待ちはゆっくり、終わりは止まる
    const target = phase === "tool" ? 0.65 : phase === "think" ? 0.28 : phase === "write" ? 0.22 : phase === "wait" ? 0.12
      : phase === "compact" ? 0.08 : 0;
    spin += (target - spin) * (1 - Math.exp(-dt * 2));
    yaw += spin * dt;
    const tilt = mouse && t - mouse.at < 2 ? clamp((mouse.y - cy) / H, -0.5, 0.5) * 0.5 : 0;
    pitch += (0.32 + Math.sin(t * 0.17) * 0.08 + tilt - pitch) * (1 - Math.exp(-dt * 1.5));

    // ★2026-10-10 オーナー「ツール使用中の…球状をスキャンするような円盤…上下していますがそれなくしたい」→ 走査をやめた。
    // ★compact（会話の要約）: 球を少し縮め、進み具合に合わせて上から下へ細胞を灯す＝畳み込んでいる。
    const comp = phase === "compact";
    if (comp) progShown += (prog - progShown) * (1 - Math.exp(-dt * 3));
    verts.forEach((v, i) => {
      let tgt = band[v.b] * (phase === "write" ? 0.7 : 1);
      if (phase === "wait") tgt = Math.max(tgt, 0.08 + 0.07 * Math.sin(t * 1.5 + v.u[1] * 3));
      // ★Think は決まった波で動かす（2026-10-19 オーナー「Think の量に応じて背景のスペクトラムの演出を増やすとアプリが
      //   重くみえるので固定のアニメーションでいいかも」）。届いた思考の量では強さを変えない（feed も Think を溜めない）。
      if (phase === "think") tgt = THINK_E + THINK_AMP * Math.sin(t * 1.1 + v.u[0] * 2.3 + v.u[1] * 1.7 - v.u[2] * 1.3);
      const ru = rot(v.u);
      if (comp) {
        // 上（+1）から下（-1）へ、進んだ分の細胞を灯す。境目は少し明るく。
        const edge = 1 - 2 * progShown;
        tgt = ru[1] >= edge ? 0.55 : 0.04;
        if (Math.abs(ru[1] - edge) < 0.06) tgt = 1.0;
      }
      v.e += (clamp(tgt, 0, 1.4) - v.e) * (1 - Math.exp(-dt * 7));
      v.glow *= Math.exp(-dt / 0.35);
      v.d = (wobble(v.u) + 0.20 * v.e + pulse * 0.06) * (comp ? 0.86 + 0.02 * Math.sin(t * 2.2) : 1);
      v.r = ru;
      v.p = proj(ru, v.d);
      if (mouse && t - mouse.at < 1.5 && Math.hypot(v.p.x - mouse.x, v.p.y - mouse.y) < 60) v.glow = Math.max(v.glow, 0.5);
      // Think: 強く光った細胞から、隣の細胞へ粒が走る（球の上をさまよう）
      if (phase === "think" && v.e > 0.3 && packets.length < THINK_PACKETS && Math.random() < dt * THINK_RATE) {
        const k = adj[i][(Math.random() * adj[i].length) | 0];
        if (k !== undefined) { packets.push({ kind: "edge", e: edges[k], from: i, u: 0, sp: rand(1.4, 2.4) }); v.glow = 1; }
        if (Math.random() < 0.2) glyph(i);
      }
    });
    // Out: 文字の勢いに合わせて、手前の細胞から外の輪へ粒を放つ
    if (phase === "write" && Math.random() < dt * (3 + level * 8) && packets.length < 60) {
      const front = verts.map((v, i) => i).filter((i) => verts[i].p.k > 0.55);
      const i = front[(Math.random() * front.length) | 0];
      if (i !== undefined) { packets.push({ kind: "ray", from: i, u: 0, sp: rand(0.9, 1.3) }); verts[i].glow = 1; }
    }
    for (const p of packets) p.u += p.sp * dt;
    packets.forEach((p) => {
      if (p.u < 1) return;
      if (p.kind === "edge") {                                   // 届いた先も少し光る（伝わる）
        const to = p.e.a === p.from ? p.e.b : p.e.a;
        verts[to].glow = Math.min(1, verts[to].glow + 0.6);
        band[verts[to].b] = Math.min(1.4, band[verts[to].b] + 0.06);
      } else if (Math.random() < 0.35) {                         // 外の輪に着いた: 波紋と符号
        rings.push({ r: 1.45, a: 0.7 });
        glyph(p.from, true);
      }
    });
    packets = packets.filter((p) => p.u < 1);
    for (const r of rings) { r.r += dt * 0.55; r.a -= dt * 0.8; }
    rings = rings.filter((r) => r.a > 0.02);
    for (const g of glyphs) { g.a -= dt * 0.8; g.y -= dt * 10; }
    glyphs = glyphs.filter((g) => g.a > 0.02);
  }
  function glyph(i, outer) {
    if (glyphs.length >= 16 || !recent.length) return;
    const v = verts[i]; if (!v || !v.p) return;
    const c = recent[(Math.random() * recent.length) | 0];
    const txt = c.toString(16).toUpperCase().padStart(c < 256 ? 2 : 4, "0");
    let x = v.p.x, y = v.p.y;
    if (outer) { const a = Math.atan2(y - cy, x - cx); x = cx + Math.cos(a) * R * 1.62; y = cy + Math.sin(a) * R * 1.62; }
    glyphs.push({ x: x + rand(-8, 8), y: y - 8, txt, a: 1, hot: !!outer });
  }

  // ── 描く ──────────────────────────────────────────────────────────
  function draw() {
    ctx.clearRect(0, 0, W, H);
    if (!verts[0] || !verts[0].p) verts.forEach((v) => { v.r = rot(v.u); v.d = wobble(v.u); v.p = proj(v.r, v.d); });
    const base = phase === "idle" ? 0.55 : 1;
    const e0 = clamp(level / 3, 0, 1);

    drawCore(base, e0);
    drawOrbits(base, false);

    // 糸（球面に沿って 3 つに折る）。奥 → 手前の順に 2 回に分けて描く。
    ctx.lineCap = "round";
    for (const front of [false, true]) {
      for (const ed of edges) {
        const a = verts[ed.a], b = verts[ed.b];
        const kz = (a.p.k + b.p.k) / 2;
        if ((kz > 0.5) !== front) continue;
        const en = (a.e + b.e) / 2 + (a.glow + b.glow) * 0.3;
        ctx.strokeStyle = rgba(RED, (0.05 + 0.30 * kz + 0.35 * clamp(en, 0, 1)) * base * (front ? 1 : 0.55));
        ctx.lineWidth = front ? 0.8 + en * 0.8 : 0.6;
        ctx.beginPath();
        for (let s = 0; s <= 3; s++) {
          const w = s / 3;
          const m = [a.r[0] * (1 - w) + b.r[0] * w, a.r[1] * (1 - w) + b.r[1] * w, a.r[2] * (1 - w) + b.r[2] * w];
          const n = Math.hypot(m[0], m[1], m[2]) || 1;
          const p = proj([m[0] / n, m[1] / n, m[2] / n], a.d * (1 - w) + b.d * w);
          s ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y);
        }
        ctx.stroke();
      }
      // 細胞（四角い点）。光っている細胞は芯の色で大きめに。
      for (const v of verts) {
        if ((v.p.k > 0.5) !== front) continue;
        const en = clamp(v.e + v.glow * 0.8, 0, 1.4);
        const z = (1.1 + 1.6 * v.p.k + en * 1.6) * v.p.s;
        ctx.fillStyle = rgba(en > 0.55 ? HOT : RED, (0.12 + 0.55 * v.p.k + 0.4 * en) * base * (front ? 1 : 0.6));
        ctx.fillRect(v.p.x - z / 2, v.p.y - z / 2, z, z);
      }
    }

    drawOrbits(base, true);
    drawHud(base);
    if (phase === "tool") drawToolName();

    // データの粒（四角・加算で重ねる）
    if (packets.length) {
      ctx.globalCompositeOperation = BLEND;
      for (const p of packets) {
        for (let k = 4; k >= 0; k--) {
          const u = Math.max(0, p.u - k * 0.05);
          const q = packetAt(p, u);
          if (!q) continue;
          const z = k ? 2.2 - k * 0.3 : 3.2;
          ctx.fillStyle = rgba(k ? RED : HOT, (k ? 0.45 - k * 0.08 : 0.95) * (q.k ?? 1));
          ctx.fillRect(q.x - z / 2, q.y - z / 2, z, z);
        }
      }
      ctx.globalCompositeOperation = "source-over";
    }

    // 波紋（Out）
    ctx.setLineDash([3, 4]); ctx.lineWidth = 1;
    for (const r of rings) {
      ctx.strokeStyle = rgba(HOT, r.a * 0.5);
      ctx.beginPath(); ctx.arc(cx, cy, R * r.r, 0, TAU); ctx.stroke();
    }
    ctx.setLineDash([]);

    // 16 進の読み出し
    if (glyphs.length) {
      ctx.font = `10px ${MONO}`; ctx.textAlign = "center";
      // ★文字は線より薄く（2026-11-05 Windows の実機で「BASHOUTPUT」「202.15」が会話の行に透けて、行の「1/6」に重なっていた）。
      for (const g of glyphs) { ctx.fillStyle = rgba(g.hot ? HOT : RED, g.a * LABEL_A); ctx.fillText(g.txt, g.x, g.y); }
    }
  }

  function packetAt(p, u) {
    const a = verts[p.from];
    if (p.kind === "edge") {
      const b = verts[p.e.a === p.from ? p.e.b : p.e.a];
      const m = [a.r[0] * (1 - u) + b.r[0] * u, a.r[1] * (1 - u) + b.r[1] * u, a.r[2] * (1 - u) + b.r[2] * u];
      const n = Math.hypot(m[0], m[1], m[2]) || 1;
      return proj([m[0] / n, m[1] / n, m[2] / n], a.d * (1 - u) + b.d * u);
    }
    // ray: 球面から外の輪（半径 1.45）へまっすぐ
    const ang = Math.atan2(a.p.y - cy, a.p.x - cx);
    const r0 = Math.hypot(a.p.x - cx, a.p.y - cy), r1 = R * 1.45;
    const r = r0 + (r1 - r0) * u;
    return { x: cx + Math.cos(ang) * r, y: cy + Math.sin(ang) * r, k: 1 };
  }

  // 中心の芯（勢いで明るく・Out は脈打つ）
  function drawCore(base, e0) {
    const rr = R * (0.30 + 0.06 * pulse);
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rr);
    g.addColorStop(0, rgba(HOT, (0.10 + 0.22 * e0 + 0.25 * pulse) * base));
    g.addColorStop(0.5, rgba(RED, (0.05 + 0.10 * e0) * base));
    g.addColorStop(1, rgba(RED, 0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, rr, 0, TAU); ctx.fill();
  }

  // 傾いた 2 本の軌道の輪（球の奥側 → 手前側の順に描くため、front で半分ずつ）。Tool の間は粒が回る。
  const ORBITS = [{ tilt: 1.15, roll: 0.35, r: 1.28, sp: 0.7 }, { tilt: -0.9, roll: -0.55, r: 1.36, sp: -0.45 }];
  function orbitPt(o, a) {
    const v = [Math.cos(a) * o.r, 0, Math.sin(a) * o.r];
    const ct = Math.cos(o.tilt), st = Math.sin(o.tilt), cr = Math.cos(o.roll), sr = Math.sin(o.roll);
    const y1 = v[1] * ct - v[2] * st, z1 = v[1] * st + v[2] * ct;
    const x2 = v[0] * cr - y1 * sr, y2 = v[0] * sr + y1 * cr;
    const w = rot([x2, y2, z1]);
    return proj([w[0] / o.r, w[1] / o.r, w[2] / o.r], o.r);
  }
  function drawOrbits(base, front) {
    ctx.lineWidth = 1;
    for (const o of ORBITS) {
      for (let s = 0; s < 96; s += 2) {
        const p = orbitPt(o, (s / 96) * TAU);
        if ((p.k > 0.5) !== front) continue;
        ctx.fillStyle = rgba(RED, (0.10 + 0.25 * p.k) * base);
        ctx.fillRect(p.x - 0.8, p.y - 0.8, 1.6, 1.6);
      }
      if (phase === "tool" || phase === "think") {
        for (let k = 0; k < 2; k++) {
          const p = orbitPt(o, t * o.sp * (phase === "tool" ? 2.2 : 1) + k * Math.PI);
          if ((p.k > 0.5) !== front) continue;
          ctx.fillStyle = rgba(HOT, 0.85 * base); ctx.fillRect(p.x - 2, p.y - 2, 4, 4);
        }
      }
    }
  }

  // HUD: 円形の LED スペクトラム・回る破線の輪・目盛りの弧・帯の番号
  function drawHud(base) {
    const r0 = R * 1.47, seg = 7, sl = 3.2, gap = 2;
    const rotA = t * 0.05;
    // ★Tool の間はスペクトラムに変化を付ける（2026-10-09 オーナー「tool 使用中はスペクトラムに変化を加えるのでいい」
    //   「（回る掃引）以外で」）: 棒の向きを内側（球の方）へ返し、デジタルのノイズのように細かく明滅させる
    //   ＝道具の結果を球が取り込んでいる。高さは 1 秒に 12 回だけ刻んで変える（滑らかにしない＝データらしく）。
    const tool = phase === "tool";
    const comp = phase === "compact";
    const tick = tool ? Math.floor((t - phaseAt) * 12) : 0;
    const noise = (i) => {                       // 刻みごとに決まる 0〜1（同じ刻みの間は同じ値）
      const x = Math.sin((i * 12.9898 + tick * 78.233) * 1.7) * 43758.5453;
      return x - Math.floor(x);
    };
    for (let i = 0; i < BANDS; i++) {
      // compact は回さず真上から時計回りに、進んだ分の帯を満たす（残りは暗く）
      const a = (i / BANDS) * TAU + (comp ? 0 : rotA) - Math.PI / 2;
      const v = comp ? ((i + 0.5) / BANDS <= progShown ? 1 : 0.06)
        : tool ? clamp(Math.pow(noise(i), 1.8) * 0.85 + band[i] * 0.2, 0, 1)
        : phase === "think" ? clamp(THINK_E + THINK_AMP * Math.sin(t * 1.6 + i * 0.39), 0, 1)
        : clamp(band[i] * 0.9 + (phase === "write" ? pulse * 0.4 : 0), 0, 1);
      const lit = Math.round(v * seg);
      const ca = Math.cos(a), sa = Math.sin(a);
      for (let s = 0; s < seg; s++) {
        // Tool は内側へ伸ばす（輪の少し外から球へ向かって）・ふだんは外側へ
        // Tool と compact は内側へ伸ばす（輪の少し外から球へ向かって＝取り込む・畳み込む）・ふだんは外側へ
        const ra = (tool || comp) ? r0 + sl * 2 - s * (sl + gap) - sl : r0 + s * (sl + gap), rb = ra + sl;
        ctx.strokeStyle = s < lit ? rgba(s >= 5 ? HOT : RED, (0.35 + s * 0.07) * base) : rgba(DIM, 0.10 * base);
        ctx.lineWidth = 2.6;
        ctx.beginPath(); ctx.moveTo(cx + ca * ra, cy + sa * ra); ctx.lineTo(cx + ca * rb, cy + sa * rb); ctx.stroke();
      }
    }
    // 回る破線の輪
    ctx.save(); ctx.translate(cx, cy);
    ctx.rotate(t * (phase === "tool" ? 0.5 : phase === "compact" ? 0.04 : 0.15));
    ctx.setLineDash([1.5, 6]); ctx.lineWidth = 1;
    ctx.strokeStyle = rgba(RED, 0.35 * base);
    ctx.beginPath(); ctx.arc(0, 0, R * 1.86, 0, TAU); ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
    // 目盛りの弧（3 つ・逆回り）
    ctx.save(); ctx.translate(cx, cy);
    ctx.rotate(-t * (phase === "tool" ? 0.35 : 0.08));
    ctx.strokeStyle = rgba(RED, 0.40 * base); ctx.lineWidth = 1.2;
    const ra = R * 1.98;
    for (let k = 0; k < 3; k++) {
      const a0 = k * (TAU / 3), a1 = a0 + 0.75;
      ctx.beginPath(); ctx.arc(0, 0, ra, a0, a1); ctx.stroke();
      ctx.beginPath();
      for (let m = 0; m <= 8; m++) {
        const a = a0 + (a1 - a0) * (m / 8), l = m % 4 === 0 ? 7 : 3;
        ctx.moveTo(Math.cos(a) * ra, Math.sin(a) * ra); ctx.lineTo(Math.cos(a) * (ra + l), Math.sin(a) * (ra + l));
      }
      ctx.stroke();
    }
    ctx.restore();
    // 帯の番号（目盛りの弧の外・4 か所）
    ctx.font = `9px ${MONO}`; ctx.textAlign = "center";
    for (let k = 0; k < 4; k++) {
      const a = -t * 0.08 + k * (TAU / 4) + 0.4;
      const i = (k * 12 + ((t * 2) | 0)) % BANDS;
      ctx.fillStyle = rgba(RED, (0.30 + 0.5 * clamp(band[i], 0, 1)) * base);
      ctx.fillText(`B${String(i).padStart(2, "0")}:${Math.round(clamp(band[i], 0, 1) * 255).toString(16).toUpperCase().padStart(2, "0")}`,
                   cx + Math.cos(a) * R * 2.16, cy + Math.sin(a) * R * 2.16 + 3);
    }
  }

  // Tool: 道具の名前だけを球の右上に出す（2026-10-10 に上下する走査の輪をやめた）
  function drawToolName() {
    if (!toolName) return;
    const x = cx + R * 1.12, y = cy - R * 0.98;
    ctx.strokeStyle = rgba(HOT, 0.55); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x - 10, y + 10); ctx.lineTo(x + 6, y - 4); ctx.lineTo(x + 58, y - 4); ctx.stroke();
    ctx.font = `10px ${MONO}`; ctx.textAlign = "left"; ctx.fillStyle = rgba(HOT, LABEL_A);
    ctx.fillText("▸ " + toolName.toUpperCase(), x + 8, y - 8);
  }

  // ── 回す・止める ──────────────────────────────────────────────────
  const settled = () => phase === "idle" && !packets.length && !rings.length && !glyphs.length && spin < 0.002
    && verts.every((v) => v.e < 0.01 && v.glow < 0.01) && (!mouse || t - mouse.at > 2);
  // ★GPU（2026-11-21）: 旧は毎フレーム起きて 30fps で描いていた。点の背景（bgfield.js）と同じ刻み（window.BW_ANIM_MS・既定 50ms＝20fps）に
  //   そろえて同じフレームで描き（上に重なる入力欄のぼかしの作り直しを 1 回にまとめる）、合間は setTimeout で寝る。
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
  function frame(now) {
    raf = 0;
    const dt = Math.min(0.1, lastT ? (now - lastT) / 1000 : 0.016);
    lastT = now;
    size();
    step(dt);
    draw();
    if (!settled()) { raf = 1; CLOCK.next(frame); }
    else { lastT = 0; }                    // 静まったら止める（いま描いた 1 枚が残る）
  }
  const wake = () => { if (!raf && canvas) { lastT = 0; raf = 1; CLOCK.now(frame); } };

  // ── 外から呼ぶもの（app.js の beginLive が呼ぶ）──────────────────────
  const hash = (c) => (Math.imul(c, 2654435761) >>> 0) % BANDS;
  window.BWSynapse = {
    mount(el) {
      if (canvas || !el) return;
      host = el;
      canvas = document.createElement("canvas");
      canvas.className = "synapse";
      canvas.setAttribute("aria-hidden", "true");
      el.prepend(canvas);
      ctx = canvas.getContext("2d");
      if (!ctx) { canvas.remove(); canvas = null; return; }
      readTheme(); build(); size(); draw();
      if (typeof ResizeObserver === "function") new ResizeObserver(() => {
        // ★映した位置は大きさごとに作り直す（2026-10-31 UI レビュー: 止まっている間は作り直されないので、窓を変えると球が元の位置に
        //   残り、隠れた状態で始まるウェブ版では左上に点の塊として固まっていた）。
        if (size()) { if (verts[0]) verts[0].p = null; draw(); }
      }).observe(el);
      el.addEventListener("pointermove", (ev) => {
        const b = el.getBoundingClientRect();
        mouse = { x: ev.clientX - b.left, y: ev.clientY - b.top, at: t }; wake();
      }, { passive: true });
      el.addEventListener("pointerleave", () => { mouse = null; });
    },
    /* 思考（"think"）・本文（"answer"）の文字。文字ごとに帯を決めて溜める（同じ字は同じ帯＝語の並びが模様になる）。 */
    feed(kind, text) {
      if (!canvas || !text) return;
      const s = String(text);
      // ★Think は強さに足さない（決まった動き・2026-10-19 オーナー）。16 進の読み出しに使う符号だけ取る。
      const grow = kind === "answer";
      const n = Math.min(s.length, 96);
      for (let i = 0; i < n; i++) {
        const c = s.charCodeAt(i);
        if (grow) { const b = hash(c); band[b] = Math.min(1.4, band[b] + 0.11); }
        if (c > 32 && i % 3 === 0) { recent.push(c); if (recent.length > 24) recent.shift(); }
      }
      if (!grow) return;
      level = Math.min(3, level + n * 0.01);
      if (kind === "answer") pulse = Math.min(1, pulse + 0.25 + n * 0.02);
      wake();
    },
    /* 声（音声会話の係が話している音）。bins は 0〜255 の周波数の強さ（Web Audio の AnalyserNode）。
     * ★2026-10-11 オーナー「スペクトラムもモデルが会話しているときは反応するように」。低い音から高い音を 48 帯へ対数で割り当て、
     *   帯の強さを声で上書きする（文字の符号で決める feed とは別）。話している間は "write"（Out）の見た目で回す。 */
    speak(bins) {
      if (!canvas || !bins || !bins.length) return;
      const n = bins.length;
      let sum = 0;
      for (let i = 0; i < BANDS; i++) {
        const a = Math.floor(Math.pow(n, i / BANDS)), b = Math.max(a + 1, Math.floor(Math.pow(n, (i + 1) / BANDS)));
        let m = 0;
        for (let k = a; k < Math.min(b, n); k++) m = Math.max(m, bins[k]);
        const v = m / 255;
        band[i] = Math.max(band[i] * 0.6, v * 1.3);
        sum += v;
      }
      const avg = sum / BANDS;
      level = Math.min(3, level * 0.9 + avg * 2.5);
      pulse = Math.min(1, Math.max(pulse, avg * 1.6));
      if (phase !== "write") { phase = "write"; phaseAt = t; }
      wake();
    },
    /* 段階: "wait"（最初の応答待ち）・"think"・"write"（Out）・"tool"（name＝道具名）・"idle"（終わった・止めた） */
    phase(p, name) {
      if (!canvas) return;
      const next = p || "idle";
      if (next !== phase) { phaseAt = t; if (next !== "write") rings = []; }
      phase = next;
      toolName = next === "tool" ? String(name || "").slice(0, 24) : "";
      if (next !== "compact") { prog = 0; progShown = 0; }
      wake();
    },
    /* テーマが変わった（app.js の applyTheme）: 色を読み直して 1 枚描く。 */
    retheme() {
      readTheme();
      if (canvas) { draw(); wake(); }
    },
    /* compact の進み具合（0〜1）。会話の要約の心拍から（app.js の heartbeat）。 */
    progress(f) {
      if (!canvas) return;
      prog = clamp(Number(f) || 0, 0, 1);
      wake();
    },
  };
})();
