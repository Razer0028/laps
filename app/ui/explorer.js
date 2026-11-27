/* ── エクスプローラー（2026-11-17 オーナー「変更とファイルを統一して VSCode みたいにして、ポップアウトできるように。VSCode の設計を見ながら」）──
 * 手本は VS Code のエクスプローラーとソース管理。右のパネルの「エクスプローラー」タブに、畳める区画を縦に並べる:
 *   ① ソース管理 … コミットの欄と「ステージ済みの変更」「変更」。行を押すと差分。行の右に ファイルを開く・変更を破棄（2 回押す）・ステージ。
 *   ② このチャットのファイル … このチャットでエージェントが書いたファイル（app.js の一覧）。別のウィンドウでは出さない。
 *   ③ フォルダー（ワークスペースの名前）… ファイルの木。変わったファイルには M・U・A・D の印と色、変わったファイルを含むフォルダーには点。
 * ファイルを押すと中身、変更を押すと差分を、パネル全体に出す（VS Code のエディターの代わり）。「←」で戻る。
 * 別のウィンドウ（explorer.html）でも同じ物が動く。そこではファイルの中身は行番号つきの文字で出す（VS Code のエディターと同じ）。
 * ★相手はいまのワークスペースだけ（Rust の scm.rs）。git はこの PC の物。無ければそのことだけを出す。 */
(() => {
  if (typeof document === "undefined") return;
  const T = window.__TAURI__ || null;
  const DOCKED = !!document.getElementById("drawer");               // メインのウィンドウ（右のパネル）か、別のウィンドウか
  const inv = (c, a) => (typeof invoke === "function" ? invoke(c, a) : T ? T.core.invoke(c, a) : Promise.reject(new Error("アプリの中で開いてください")));
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const svg = (d) => `<svg viewBox="0 0 16 16" aria-hidden="true">${d}</svg>`;
  const ICON = {
    open: svg('<path d="M4 2.5h5l3 3v8H4z"/><path d="M9 2.5v3h3"/>'),
    discard: svg('<path d="M4 6h6a3 3 0 0 1 0 6H7M4 6l2.5-2.5M4 6l2.5 2.5"/>'),
    refresh: svg('<path d="M13 3.5v3h-3M12.6 6.5A5 5 0 1 0 13 9"/>'),
    collapse: svg('<path d="M4 6.5l4-3 4 3M4 12.5l4-3 4 3"/>'),
    file: svg('<path d="M4 2.5h5l3 3v8H4z"/><path d="M9 2.5v3h3"/>'),
    folder: svg('<path d="M2 4.5h4l1.5 1.5H14v6.5H2z"/>'),
    newFile: svg('<path d="M4 2.5h5l3 3v8H4z"/><path d="M8 8v4M6 10h4"/>'),
    newDir: svg('<path d="M2 4.5h4l1.5 1.5H14v6.5H2z"/><path d="M8 7.5v3.5M6.25 9.25h3.5"/>'),
    popout: svg('<path d="M9 2.5h4.5V7M13.5 2.5 8 8M12 9.5v4H2.5V4h4"/>'),
  };
  /* ★形式別のアイコン（2026-11-27 オーナー「エクスプローラーのファイルアイコンとかを形式別に変えれますか？」→「任せます」）。
   *   VS Code のアイコンテーマと同じ考え: ファイル名 → 拡張子 → 既定 の順に引く。絵は線の記号（ほかのアイコンと同じ線の太さ）・
   *   色は種類ごと（data-k → CSS の --xk）。外の絵は取り込まない（ライセンスと容量を増やさない）。 */
  const KIND_ICON = {
    code: svg('<path d="M5.5 4.5 2 8l3.5 3.5M10.5 4.5 14 8l-3.5 3.5"/>'),
    script: svg('<path d="M2.5 3.5h11v9h-11z"/><path d="M4.5 6.5 6.5 8l-2 1.5M8 10h3"/>'),
    web: svg('<circle cx="8" cy="8" r="5.5"/><path d="M2.5 8h11M8 2.5c1.6 1.6 2.2 3.5 2.2 5.5S9.6 11.9 8 13.5M8 2.5C6.4 4.1 5.8 6 5.8 8s.6 3.9 2.2 5.5"/>'),
    style: svg('<path d="M3 12.5c1.5 0 2.5-1 2.5-2.2 0-1 .8-1.6 1.6-1.6L13 3l-.2-.2-6 5.8"/><path d="M3 12.5c0-1.5.3-2.6 1.4-3.2"/>'),
    data: svg('<path d="M6 2.5c-1.6 0-2 .8-2 2v1.6c0 .9-.6 1.6-1.5 1.9.9.3 1.5 1 1.5 1.9v1.6c0 1.2.4 2 2 2M10 2.5c1.6 0 2 .8 2 2v1.6c0 .9.6 1.6 1.5 1.9-.9.3-1.5 1-1.5 1.9v1.6c0 1.2-.4 2-2 2"/>'),
    config: svg('<circle cx="8" cy="8" r="2"/><path d="M8 1.8v2M8 12.2v2M1.8 8h2M12.2 8h2M3.6 3.6l1.4 1.4M11 11l1.4 1.4M3.6 12.4 5 11M11 5l1.4-1.4"/>'),
    doc: svg('<path d="M4 2.5h5l3 3v8H4z"/><path d="M6 8h4M6 10.5h4"/>'),
    md: svg('<path d="M2 4.5h12v7H2z"/><path d="M4 9.5v-3l1.5 1.7L7 6.5v3M10.5 6.5v3M9.2 8.3l1.3 1.2 1.3-1.2"/>'),
    pdf: svg('<path d="M4 2.5h5l3 3v8H4z"/><path d="M9 2.5v3h3M6 9.5h4"/>'),
    image: svg('<path d="M2.5 3.5h11v9h-11z"/><path d="M2.5 11 6 7.5l3 3 2-2 2.5 2.5"/><circle cx="10.5" cy="6" r="1"/>'),
    media: svg('<circle cx="8" cy="8" r="5.5"/><path d="M6.8 5.8 10.5 8l-3.7 2.2z"/>'),
    table: svg('<path d="M2.5 3.5h11v9h-11zM2.5 6.5h11M2.5 9.5h11M6.5 3.5v9"/>'),
    archive: svg('<path d="M3.5 2.5h9v11h-9z"/><path d="M8 2.5v1.5M8 5v1.5M8 7.5V9"/><path d="M7 9.5h2v2H7z"/>'),
    model: svg('<path d="M8 2 13.5 5v6L8 14 2.5 11V5z"/><path d="M2.5 5 8 8l5.5-3M8 8v6"/>'),
    binary: svg('<path d="M3.5 2.5h9v11h-9z"/><path d="M6 5.5v3M10 5.5v3M6 10.5h.01M10 10.5h.01"/>'),
    font: svg('<path d="M3.5 13 7.2 3h1.6L12.5 13M5 9.5h6"/>'),
    key: svg('<circle cx="5.5" cy="10.5" r="3"/><path d="M7.6 8.4 13 3M11 5l1.8 1.8M9.6 6.4l1.4 1.4"/>'),
    git: svg('<circle cx="5" cy="4" r="1.5"/><circle cx="5" cy="12" r="1.5"/><circle cx="11" cy="6.5" r="1.5"/><path d="M5 5.5v5M11 8c0 2-2.5 2.2-5 3"/>'),
    lock: svg('<path d="M4 7.5h8v6H4z"/><path d="M5.8 7.5V5.6a2.2 2.2 0 0 1 4.4 0v1.9"/>'),
    file: ICON.file,
  };
  const FOLDER_OPEN = svg('<path d="M2 4.5h4l1.5 1.5H13v1.5"/><path d="M2 12.5V4.5M2 12.5h10.5l2-5H4z"/>');
  // 拡張子 → [種類, 色]。色は CSS の [data-c] で決める（明るい・暗いどちらの画面でも読める中ほどの明るさ）。
  const EXT = {};
  const put = (kind, color, list) => list.split(" ").forEach((x) => { EXT[x] = [kind, color]; });
  put("code", "blue", "py pyi pyw ts tsx mts cts c h cpp cc cxx hpp hh cs");
  put("code", "yellow", "js mjs cjs jsx");
  put("code", "orange", "rs swift scala");
  put("code", "cyan", "go dart");
  put("code", "red", "rb java kt kts erl ex exs");
  put("code", "purple", "php hs ml lua r jl zig nim gd gdscript");
  put("script", "green", "sh bash zsh fish ps1 psm1 bat cmd");
  put("web", "orange", "html htm xhtml vue svelte astro");
  put("style", "purple", "css scss sass less styl");
  put("data", "yellow", "json jsonc json5 jsonl ndjson");
  put("data", "purple", "yaml yml toml xml plist proto graphql gql sql");
  put("config", "gray", "ini cfg conf env properties editorconfig service timer socket desktop");
  put("doc", "gray", "txt text log rst adoc org tex");
  put("md", "blue", "md markdown mdx");
  put("pdf", "red", "pdf");
  put("doc", "blue", "doc docx odt rtf");
  put("image", "purple", "png jpg jpeg gif webp bmp ico svg avif tif tiff psd heic");
  put("media", "pink", "mp3 wav ogg flac m4a aac opus mp4 mkv mov webm avi");
  put("table", "green", "csv tsv xls xlsx ods");
  put("archive", "orange", "zip 7z tar gz tgz xz bz2 zst rar deb rpm apk msi dmg iso whl");
  put("model", "cyan", "gguf safetensors onnx pt pth ckpt bin npz npy h5 tflite");
  put("binary", "gray", "exe dll so dylib o a lib wasm class pyc");
  put("font", "red", "ttf otf woff woff2");
  put("key", "yellow", "pem key crt cer p12 pfx pub");
  put("lock", "gray", "lock");
  const NAMES = {
    ".gitignore": ["git", "orange"], ".gitattributes": ["git", "orange"], ".gitmodules": ["git", "orange"],
    "dockerfile": ["config", "blue"], "makefile": ["script", "green"], "justfile": ["script", "green"],
    "cmakelists.txt": ["config", "green"], "license": ["doc", "yellow"], "license.md": ["doc", "yellow"],
    "readme": ["md", "blue"], "cargo.toml": ["config", "orange"], "cargo.lock": ["lock", "orange"],
    "package.json": ["data", "green"], "package-lock.json": ["lock", "green"], "pyproject.toml": ["config", "blue"],
    "requirements.txt": ["config", "blue"], "agents.md": ["md", "purple"], "claude.md": ["md", "orange"],
  };
  function fileKind(name) {
    const n = String(name || "").toLowerCase();
    if (NAMES[n]) return NAMES[n];
    if (n.startsWith(".env")) return ["config", "yellow"];
    if (n.startsWith("dockerfile")) return ["config", "blue"];
    const dot = n.lastIndexOf(".");
    const ext = dot > 0 || (dot === 0 && n.indexOf(".", 1) < 0) ? n.slice(dot + 1) : "";
    return EXT[ext] || ["file", ""];
  }
  /* 行のアイコンの器に絵と色を入れる（フォルダーは開いている時の形も） */
  function paintIcon(ic, name, dir, open) {
    if (dir) { ic.innerHTML = open ? FOLDER_OPEN : ICON.folder; ic.dataset.c = "folder"; return; }
    const [k, c] = fileKind(name);
    ic.innerHTML = KIND_ICON[k] || ICON.file;
    if (c) ic.dataset.c = c; else delete ic.dataset.c;
  }
  window.bwFileKind = fileKind;          // 試験とほかの一覧から使う

  const S = {
    st: null, err: "", loading: false, key: "", msg: "", armed: "",
    sel: null, diff: null, diffErr: "",                             // 開いている差分
    files: null, filesWs: "", filesAt: 0, tree: null, open: new Set([""]),   // フォルダーの木（files は Quick Open 用の全体の一覧）
    dirs: new Map(), focusRel: "", activeRel: "", reveal: false, edit: null,   // 2026-11-21 開いたフォルダだけを読む木
    view: null,                                                    // 別のウィンドウで開いているファイル { path, pv, err }
    collapsed: (() => { try { return JSON.parse(localStorage.getItem("bw.explorer.collapsed") || "{}") || {}; } catch { return {}; } })(),
  };
  const flash = (t) => {
    if (typeof sbFlash === "function") { sbFlash(t); return; }
    const m = document.getElementById("x-msg"); if (!m) return;
    m.textContent = t; m.hidden = false; clearTimeout(flash.t); flash.t = setTimeout(() => { m.hidden = true; }, 3000);
  };
  const busyHere = () => (typeof state !== "undefined" && (state.runs || []).some((r) => r && !r.convo?.remote)) || document.body.classList.contains("is-busy");
  const wsNow = () => (typeof wsPath === "function" ? wsPath() : S.st?.ws || "");
  const shown = () => DOCKED ? (typeof drawer !== "undefined" && drawer.open && drawer.tab === "files") : true;
  const host = () => DOCKED ? document.getElementById("files") : document.getElementById("x-root");
  const saveCollapsed = () => { try { localStorage.setItem("bw.explorer.collapsed", JSON.stringify(S.collapsed)); } catch { /* 覚えられなくても動く */ } };

  /* 描き直し。メインのウィンドウでは app.js の renderFiles（開いているファイルの表示を持つ）を通す。 */
  function rerender() {
    if (DOCKED) { if (typeof renderFiles === "function" && shown()) renderFiles(); }
    else renderStandalone();
  }

  /* ── 状態（git status）── */
  function groups(st) {
    const staged = [], changes = [];
    for (const f of st?.files || []) {
      const x = f.x, y = f.y;
      if (x === "?" && y === "?") { changes.push({ ...f, code: "U", kind: "untracked" }); continue; }
      if (x === "!") continue;
      const conflict = x === "U" || y === "U" || (x === "A" && y === "A") || (x === "D" && y === "D");
      if (conflict) { changes.push({ ...f, code: "!", kind: "worktree", conflict: true }); continue; }
      if (x !== " ") staged.push({ ...f, code: x, staged: true, kind: x === "A" ? "added" : "tracked" });
      if (y !== " ") changes.push({ ...f, code: y, kind: "worktree" });
    }
    return { staged, changes };
  }
  const changeCount = (st) => new Set((st?.files || []).filter((f) => f.x !== "!").map((f) => f.path)).size;

  function paintBadges() {
    const n = S.st && S.st.repo ? changeCount(S.st) : 0;
    const c = document.getElementById("files-count");
    if (c) c.textContent = n ? String(n) : "";
    const sb = document.getElementById("sb-scm");
    if (sb) {
      const t = sb.querySelector(".sb__t"); if (t) t.textContent = n ? String(n) : "";
      sb.hidden = !(S.st && S.st.repo && n);
      sb.title = n ? `ソース管理（変更 ${n} 件）` : "ソース管理";
    }
  }

  async function refresh() {
    if (S.loading || !T) return;
    S.loading = true;
    try { S.st = await inv("scm_status"); S.err = ""; }
    catch (e) { S.st = null; S.err = String(e?.message || e); }
    finally { S.loading = false; }
    if (S.sel && !(S.st?.files || []).some((f) => f.path === S.sel.path)) { S.sel = null; S.diff = null; }
    paintBadges();
    // ★変わっていなければ描き直さない（6 秒ごとに作り直すと、コミット メッセージを打っている途中で欄が消える）。
    const key = JSON.stringify([S.err, S.st?.repo, S.st?.git, S.st?.head, S.st?.files]);
    if (key !== S.key) { const first = !S.key; S.key = key; if (!first) refreshDirs(); if (shown()) rerender(); }
    if (S.tabs.length && editorVisible()) checkDisk();   // 開いているファイルをエージェントが書き換えたら読み直す
  }

  /* ── 区画（VS Code のビュー: 見出しを押すと畳む・右に操作）── */
  function section(k, title, badge, actions, build) {
    const sec = el("section", "xs" + (S.collapsed[k] ? " is-collapsed" : ""));
    sec.dataset.k = k;
    const head = el("div", "xs__head");
    const h = el("button", "xs__h"); h.type = "button";
    h.setAttribute("aria-expanded", String(!S.collapsed[k]));
    h.append(el("span", "xs__chev"), el("span", "xs__t", title));
    if (badge) h.append(el("span", "xs__badge num", badge));
    h.addEventListener("click", () => { S.collapsed[k] = !S.collapsed[k]; saveCollapsed(); rerender(); });
    head.append(h);
    if (actions.length) {
      const a = el("span", "xs__acts");
      for (const [icon, label, fn] of actions) {
        const b = el("button", "x-icon"); b.type = "button"; b.title = label; b.setAttribute("aria-label", label); b.innerHTML = icon;
        b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
        a.append(b);
      }
      head.append(a);
    }
    sec.append(head);
    if (!S.collapsed[k]) { const body = el("div", "xs__body"); build(body); sec.append(body); }
    return sec;
  }

  /* 区画を描く（メインのウィンドウでは renderFiles から呼ばれる。chat は「このチャットのファイル」の作り方）。 */
  function renderSections(box, chat) {
    const typing = document.activeElement?.id === "scm-msg" ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
    box.replaceChildren();
    const st = S.st;
    const n = st && st.repo ? changeCount(st) : 0;
    if (S.tabs.length) box.append(section("open", "開いているエディター", String(S.tabs.length), [], buildOpen));
    box.append(section("scm", "ソース管理", n ? String(n) : "", [[ICON.refresh, "最新の情報に更新", () => refresh()]], buildScm));
    if (chat) box.append(section("chat", "このチャットのファイル", chat.count ? String(chat.count) : "", [], chat.build));
    const wsName = (wsNow().split(/[\\/]/).filter(Boolean).pop() || "フォルダー");
    box.append(section("tree", wsName, "", [[ICON.newFile, "新しいファイル", () => startCreate(false)], [ICON.newDir, "新しいフォルダー", () => startCreate(true)],
      [ICON.refresh, "最新の情報に更新", () => { S.dirs = new Map(); S.filesAt = 0; loadFiles(true); rerender(); }],
      [ICON.collapse, "フォルダーをすべて折りたたむ", () => { S.open = new Set([""]); rerender(); }]], buildTree));
    if (typing) { const t = document.getElementById("scm-msg"); if (t) { t.focus(); t.setSelectionRange(typing[0], typing[1]); } }
  }

  /* ── ① ソース管理 ── */
  function buildScm(p) {
    const st = S.st;
    if (S.err) { p.append(el("p", "x-empty", S.err)); return; }
    if (!st) { p.append(el("p", "x-empty", S.loading ? "読み込んでいます" : "")); return; }
    if (st.git === false) { p.append(el("p", "x-empty", st.error || "git が見つかりません")); return; }
    if (!st.repo) { p.append(el("p", "x-empty", "このワークスペースは Git のリポジトリではありません")); return; }
    const h = st.head || {};
    const head = el("div", "scm__head");
    const br = el("button", "scm__branch", h.branch || "（ブランチなし）"); br.type = "button"; br.title = "ブランチの切り替え";
    br.addEventListener("click", () => branchMenu(br, false));
    head.append(br);
    if (h.ahead || h.behind) head.append(el("span", "scm__sync num", `${h.ahead ? "↑" + h.ahead : ""}${h.ahead && h.behind ? " " : ""}${h.behind ? "↓" + h.behind : ""}`));
    p.append(head);
    const { staged, changes } = groups(st);
    const box = el("div", "scm__commit");
    const ta = el("textarea", "scm__msg"); ta.rows = 2; ta.id = "scm-msg";
    ta.placeholder = `メッセージ（Ctrl+Enter で ${h.branch || "HEAD"} にコミット）`;
    ta.setAttribute("aria-label", "コミット メッセージ");
    ta.value = S.msg || "";
    ta.addEventListener("input", () => { S.msg = ta.value; });
    const cb = el("button", "btn btn--primary scm__commit-btn", staged.length ? "コミット" : "すべてコミット"); cb.type = "button";
    cb.disabled = !staged.length && !changes.length;
    const doCommit = async () => {
      if (cb.disabled) return;
      const msg = ta.value.trim();
      if (!msg) { ta.focus(); flash("コミット メッセージを入力してください"); return; }
      cb.disabled = true;
      try {
        const r = await inv("scm_commit", { message: msg, all: !staged.length });
        S.msg = ""; ta.value = ""; flash(`コミットしました${r?.head ? `（${r.head}）` : ""}`);
      } catch (e) { flash(`コミットできません: ${String(e?.message || e)}`); }
      cb.disabled = false;
      S.key = ""; await refresh();
    };
    cb.addEventListener("click", doCommit);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing) { e.preventDefault(); doCommit(); } });
    box.append(ta, cb);
    p.append(box);
    if (!staged.length && !changes.length) { p.append(el("p", "x-empty", "変更はありません")); return; }
    if (staged.length) p.append(group("ステージ済みの変更", staged, true));
    if (changes.length) p.append(group("変更", changes, false));
    if (st.truncated) p.append(el("p", "x-note", "変更が多いので、一部だけを表示しています"));
  }

  function group(title, rows, staged) {
    const g = el("div", "scm__group");
    const hd = el("div", "scm__gh");
    hd.append(el("span", "scm__gt", title), el("span", "xs__badge num", String(rows.length)), el("span", "x-gap"));
    const all = el("button", "x-icon x-icon--text", staged ? "−" : "+"); all.type = "button";
    all.title = staged ? "すべてステージ解除" : "すべてステージ"; all.setAttribute("aria-label", all.title);
    all.addEventListener("click", () => stage(rows.map((r) => r.path), !staged));
    hd.append(all);
    g.append(hd);
    const list = el("div", "scm__list"); list.setAttribute("role", "list");
    for (const r of rows) list.append(row(r));
    g.append(list);
    return g;
  }

  function row(r) {
    const li = el("div", "scm__row"); li.setAttribute("role", "listitem");
    const name = r.path.split("/").pop();
    const dir = r.path.slice(0, Math.max(0, r.path.length - name.length - 1));
    const b = el("button", "scm__file"); b.type = "button";
    b.title = r.orig ? `${r.orig} → ${r.path}` : r.path;
    const nm = el("span", "scm__name" + (r.code === "D" ? " is-deleted" : ""), name);
    b.append(nm, el("span", "scm__dir", dir));
    b.addEventListener("click", () => openDiffTab(r));
    const acts = el("span", "scm__acts");
    const open = el("button", "x-icon"); open.type = "button"; open.title = "ファイルを開く"; open.setAttribute("aria-label", `${name} を開く`); open.innerHTML = ICON.open;
    open.addEventListener("click", () => openRepoFile(r));
    acts.append(open);
    if (!r.staged) {
      const d = el("button", "x-icon scm__discard"); d.type = "button";
      d.title = r.kind === "untracked" ? "ファイルを削除" : "変更を破棄"; d.setAttribute("aria-label", `${name}: ${d.title}`); d.innerHTML = ICON.discard;
      const key = r.path + "|w";
      if (S.armed === key) { d.classList.add("is-armed"); d.title = "もう一度押して" + (r.kind === "untracked" ? "削除" : "破棄"); }
      d.addEventListener("click", () => discard(r, key));
      acts.append(d);
    }
    const st = el("button", "x-icon x-icon--text", r.staged ? "−" : "+"); st.type = "button";
    st.title = r.staged ? "ステージ解除" : "ステージ"; st.setAttribute("aria-label", `${name}: ${st.title}`);
    st.addEventListener("click", () => stage([r.path], !r.staged));
    acts.append(st);
    const code = el("span", `x-code x-code--${r.conflict ? "C" : r.code}`, r.conflict ? "!" : r.code);
    code.title = ({ M: "変更", A: "追加", D: "削除", R: "名前の変更", C: "コピー", U: "追跡されていない", T: "種類の変更" })[r.code] || (r.conflict ? "競合" : "");
    li.append(b, acts, code);
    return li;
  }

  async function stage(paths, on) {
    try { await inv("scm_stage", { paths, stage: on }); }
    catch (e) { flash(`${on ? "ステージ" : "ステージ解除"}できません: ${String(e?.message || e)}`); }
    S.key = ""; await refresh();
  }
  async function discard(r, key) {
    if (S.armed !== key) {
      S.armed = key; rerender();
      setTimeout(() => { if (S.armed === key) { S.armed = ""; rerender(); } }, 4000);
      return;
    }
    S.armed = "";
    try { await inv("scm_discard", { path: r.path, kind: r.kind }); flash(r.kind === "untracked" ? `${r.path} を削除しました` : `${r.path} の変更を破棄しました`); }
    catch (e) { flash(`破棄できません: ${String(e?.message || e)}`); }
    if (S.sel && S.sel.path === r.path) { S.sel = null; S.diff = null; }
    S.key = ""; S.filesAt = 0; await refresh();
  }

  /* ファイルを開く。メインのウィンドウでは app.js の openFile（Markdown・表・画像・PDF を描ける）。別のウィンドウでは文字の表示。 */
  function openPath(path) {
    // ★文字のファイルはエディター（Monaco）で開く。画像・PDF・Office などは今までの表示（メインは app.js の openFile）。
    if (NOT_TEXT.test(String(path))) { fallbackOpen(path); return; }
    openEditor(path);
  }
  function openRepoFile(r) {
    if (r.code === "D") { flash("削除されたファイルは開けません"); return; }
    const top = S.st?.top || "";
    openPath(top ? `${top}/${r.path}` : r.path);
  }

  /* ── 差分（VS Code の差分エディターの代わり。パネル全体に出す）── */
  async function openDiff(r) {
    S.sel = r; S.diff = null; S.diffErr = "";
    if (DOCKED && typeof state !== "undefined" && state.files?.open && typeof closeFile === "function") { state.files.open = ""; }
    if (DOCKED && typeof setDrawer === "function") { setDrawer(true); setDrawerTab("files"); }
    rerender();
    try { S.diff = await inv("scm_diff", { path: r.path, staged: !!r.staged, untracked: r.kind === "untracked" }); }
    catch (e) { S.diffErr = String(e?.message || e); }
    if (S.sel === r) rerender();
  }
  function closeDiff() { S.sel = null; S.diff = null; rerender(); }

  function diffRows(text) {
    const rows = [];
    let o = 0, n = 0;
    for (const line of String(text || "").split("\n")) {
      if (line.startsWith("@@")) {
        const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$/.exec(line);
        if (m) { o = +m[1]; n = +m[2]; rows.push({ t: "hunk", s: m[3] || "" }); }
        continue;
      }
      if (/^(diff --git|index |--- |\+\+\+ |new file mode|deleted file mode|similarity index|rename from|rename to|old mode|new mode|Binary files)/.test(line)) continue;
      if (line.startsWith("+")) rows.push({ t: "add", n: n++, s: line.slice(1) });
      else if (line.startsWith("-")) rows.push({ t: "del", o: o++, s: line.slice(1) });
      else if (line.startsWith(" ")) rows.push({ t: "ctx", o: o++, n: n++, s: line.slice(1) });
      else if (line.startsWith("\\")) rows.push({ t: "note", s: "改行なしでファイルが終わります" });
    }
    return rows;
  }

  function editorHead(p, title, back, actions) {
    const head = el("div", "x-ehead");
    const b = el("button", "x-back", back); b.type = "button";
    b.addEventListener("click", () => { if (S.sel) closeDiff(); else { S.view = null; rerender(); } });
    const t = el("span", "x-etitle", title); t.title = title;
    head.append(b, t);
    for (const [icon, label, fn, text] of actions) {
      const a = el("button", text ? "x-icon x-icon--text" : "x-icon", text || ""); a.type = "button"; a.title = label; a.setAttribute("aria-label", label);
      if (!text) a.innerHTML = icon;
      a.addEventListener("click", fn);
      head.append(a);
    }
    p.append(head);
  }

  function renderDiff(p) {
    const r = S.sel;
    p.replaceChildren();
    editorHead(p, r.path, "← エクスプローラー", [
      [ICON.open, "ファイルを開く", () => openRepoFile(r)],
      ["", r.staged ? "ステージ解除" : "ステージ", async () => { await stage([r.path], !r.staged); closeDiff(); }, r.staged ? "−" : "+"],
    ]);
    p.append(el("div", "x-esub", r.staged ? "ステージ済み（HEAD ↔ インデックス）" : r.kind === "untracked" ? "追跡されていないファイル" : "作業ツリー（インデックス ↔ 作業中）"));
    if (S.diffErr) { p.append(el("p", "x-empty", S.diffErr)); return; }
    if (!S.diff) { p.append(el("p", "x-empty", "読み込んでいます")); return; }
    if (S.diff.binary) { p.append(el("p", "x-empty", "バイナリ ファイルのため差分は表示できません")); return; }
    const rows = S.diff.untracked ? String(S.diff.content || "").split("\n").map((s, i) => ({ t: "add", n: i + 1, s })) : diffRows(S.diff.diff);
    if (!rows.length) { p.append(el("p", "x-empty", "差分はありません（改行や権限だけの変更）")); return; }
    p.append(codeBox(rows, `${r.path} の差分`));
    if (rows.length > 3000 || S.diff.truncated) p.append(el("p", "x-note", "長いので、最初の部分だけを表示しています"));
  }

  /* 行の箱（差分にもファイルの中身にも使う）。列は 旧の行番号・新の行番号・印・本文。 */
  function codeBox(rows, label) {
    const box = el("div", "scmd");
    box.setAttribute("role", "table"); box.setAttribute("aria-label", label);
    for (const x of rows.slice(0, 3000)) {
      const line = el("div", `scmd__row scmd__row--${x.t}`);
      if (x.t === "hunk" || x.t === "note") { line.append(el("span", "scmd__hunk", x.t === "hunk" ? (x.s ? `… ${x.s}` : "…") : x.s)); box.append(line); continue; }
      line.append(el("span", "scmd__o num", x.o != null ? String(x.o) : ""), el("span", "scmd__n num", x.n != null ? String(x.n) : ""),
        el("span", "scmd__m", x.t === "add" ? "+" : x.t === "del" ? "−" : ""), el("code", "scmd__s", x.s));
      box.append(line);
    }
    return box;
  }

  /* ── ③ フォルダー（ファイルの木）── */
  async function loadFiles(force = false) {
    if (!T) return;
    const ws = wsNow();
    if (!force && S.files && S.filesWs === ws && Date.now() - S.filesAt < 30000) return;
    try { const r = await inv("ws_files"); S.files = r?.files || []; S.meta = Array.isArray(r?.meta) ? r.meta : []; S.filesTrunc = !!r?.truncated; }
    catch { S.files = []; S.meta = []; }
    S.filesWs = ws; S.filesAt = Date.now(); S.tree = null;
  }
  function buildIndex() {
    if (S.tree) return S.tree;
    const root = { name: "", path: "", dirs: new Map(), files: [], size: 0, mtime: 0, count: 0 };
    (S.files || []).forEach((f, k) => {
      const m = Array.isArray(S.meta?.[k]) ? S.meta[k] : null;
      const size = m && typeof m[0] === "number" ? m[0] : null, mtime = m && typeof m[1] === "number" ? m[1] : null;
      const parts = f.split("/");
      let node = root;
      const bump = (n) => { n.count++; if (size != null) n.size += size; if (mtime != null && mtime > n.mtime) n.mtime = mtime; };
      bump(root);
      for (let i = 0; i < parts.length - 1; i++) {
        const name = parts[i];
        if (!node.dirs.has(name)) node.dirs.set(name, { name, path: parts.slice(0, i + 1).join("/"), dirs: new Map(), files: [], size: 0, mtime: 0, count: 0 });
        node = node.dirs.get(name);
        bump(node);
      }
      node.files.push({ name: parts[parts.length - 1], path: f, size, mtime });
    });
    S.tree = root;
    return root;
  }
  /* git の状態をワークスペースからの相対の名前へ（リポジトリの一番上とワークスペースが違うときは、その分を外す）。 */
  function decorations() {
    const map = new Map(), dirs = new Set();
    const st = S.st;
    if (!st?.repo) return { map, dirs };
    const norm = (s) => String(s || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
    const top = norm(st.top), ws = norm(st.ws);
    const pre = ws.startsWith(top) && ws !== top ? ws.slice(top.length + 1) + "/" : "";
    for (const f of st.files || []) {
      if (f.x === "!") continue;
      if (pre && !f.path.toLowerCase().startsWith(pre)) continue;
      const rel = pre ? f.path.slice(pre.length) : f.path;
      const code = f.x === "?" ? "U" : (f.y !== " " ? f.y : f.x);
      map.set(rel, code);
      const parts = rel.split("/");
      for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/"));
    }
    return { map, dirs };
  }
  /* 大きさと更新時刻（2026-11-20 オーナー「エクスプローラーのファイルやフォルダにタイムスタンプ情報と容量も」）。
   * 行の右に短く（今日なら時刻・今年なら月日・それより前は年月日）、詳しい値は title に。フォルダは中のファイルの合計と、いちばん新しい時刻。 */
  function fmtSize(n) {
    if (n == null) return "";
    if (n < 1024) return `${n} B`;
    const u = ["KB", "MB", "GB", "TB"]; let v = n / 1024, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
  }
  function fmtTime(sec, full) {
    if (!sec) return "";
    const d = new Date(sec * 1000), now = new Date(), p2 = (x) => String(x).padStart(2, "0");
    const hm = `${p2(d.getHours())}:${p2(d.getMinutes())}`, md = `${d.getMonth() + 1}/${d.getDate()}`;
    if (full) return `${d.getFullYear()}/${p2(d.getMonth() + 1)}/${p2(d.getDate())} ${hm}`;
    if (d.toDateString() === now.toDateString()) return hm;
    if (d.getFullYear() === now.getFullYear()) return md;
    return `${d.getFullYear()}/${md}`;
  }
  function metaCell(r, size, mtime, extra, path, items) {
    if (size == null && !mtime && items == null) return;
    const m = el("span", "xt__meta");
    // フォルダーは中の数（読むのは 1 階層だけなので合計の大きさは出さない）
    const sz = el("span", "xt__size", items != null ? (items >= 10000 ? "1万+" : `${items.toLocaleString()} 個`) : fmtSize(size)), tm = el("span", "xt__time", fmtTime(mtime));
    m.append(sz, tm);
    r.append(m);
    const tip = [path || "", size != null ? `大きさ ${fmtSize(size)}（${size.toLocaleString()} バイト）` : "",
      mtime ? `更新 ${fmtTime(mtime, true)}` : "", extra || ""].filter(Boolean).join("\n");
    r.title = tip;
  }
  /* ── フォルダーの木（2026-11-21 オーナー「ファイルが多いので一部だけ…を改善」「エディタとしての UI/UX も優秀に」）──
   * 旧: ws_files で全部を受け取って組み立てていた（上限 20,000／5,000 個・超えた分は欠ける）。
   * 今: 開いたフォルダの 1 階層だけを ws_dir で読む（VS Code と同じ）。大きいフォルダは 500 件ずつ「さらに表示」。
   * キーボードは W3C の tree の決まり（↑↓・→ 開く/子へ・← 閉じる/親へ・Home/End・Enter・* 兄弟を開く・文字で飛ぶ）。F2 で名前の変更。 */
  async function loadDir(rel, more = false) {
    const cur = S.dirs.get(rel);
    if (cur && cur.loading) return;
    const offset = more && cur ? cur.entries.length : 0;
    S.dirs.set(rel, { ...(cur || { entries: [] }), loading: true });
    try {
      const r = await inv("ws_dir", { rel, offset, limit: 500 });
      const prev = more && cur ? cur.entries : [];
      S.dirs.set(rel, { entries: prev.concat(r?.entries || []), total: r?.total || 0, more: !!r?.more, loading: false, err: "" });
    } catch (e) {
      S.dirs.set(rel, { entries: cur?.entries || [], total: cur?.total || 0, more: false, loading: false, err: String(e?.message || e) });
    }
    if (shown()) rerender();
  }
  function refreshDirs() {
    for (const rel of [...S.dirs.keys()]) {
      if (rel === "" || S.open.has(rel)) { const d = S.dirs.get(rel); if (d && !d.loading) loadDir(rel); }
      else S.dirs.delete(rel);
    }
  }
  const parentOf = (rel) => rel.split("/").slice(0, -1).join("/");
  const absOf = (rel) => { const ws = wsNow(); const sep = ws.includes("\\") ? "\\" : "/"; return rel ? ws.replace(/[\\/]+$/, "") + sep + rel.split("/").join(sep) : ws; };
  function toggleDir(rel, open) {
    const now = S.open.has(rel);
    const want = open == null ? !now : open;
    if (want === now) return;
    if (want) { S.open.add(rel); if (!S.dirs.has(rel)) loadDir(rel); } else S.open.delete(rel);
    rerender();
  }
  function revealRel(rel) {
    // 編集中のファイルを木の中で見せる（VS Code の explorer.autoReveal）: 親を開き、その行に印と場所を合わせる
    if (!rel || rel === S.activeRel) return;
    S.activeRel = rel; S.focusRel = rel; S.reveal = true;
    const parts = rel.split("/");
    for (let k = 1; k < parts.length; k++) { const d = parts.slice(0, k).join("/"); S.open.add(d); if (!S.dirs.has(d)) loadDir(d); }
    if (shown()) rerender();
  }
  function focusRow(rel) {
    S.focusRel = rel;
    const list = host()?.querySelector(".xt");
    if (!list) return;
    list.querySelectorAll(".xt__row[tabindex='0']").forEach((r) => { r.tabIndex = -1; });
    const r = [...list.querySelectorAll(".xt__row[data-rel]")].find((x) => x.dataset.rel === rel);
    if (r) { r.tabIndex = 0; r.focus({ preventScroll: true }); r.scrollIntoView({ block: "nearest" }); }
  }
  function inlineName(row, ed) {
    // 行の名前を入力欄に置き換える（Enter で決める・Esc でやめる・外へ出たら決める＝VS Code と同じ）。
    // 木は裏で作り直されることがある（読み込み・数秒ごとの更新）ので、打っている途中の字と選択は S.edit に持ち、作り直した後に付け直す。
    const nameEl = row.querySelector(".xt__name");
    const inp = el("input", "xt__input"); inp.value = ed.value; inp.setAttribute("aria-label", ed.label || "名前");
    nameEl.replaceWith(inp);
    const finish = (ok) => { if (ed.done || S.edit !== ed) return; ed.done = true; S.edit = null; ed.onDone(ok ? inp.value.trim() : null); };
    inp.addEventListener("input", () => { ed.value = inp.value; ed.sel = [inp.selectionStart, inp.selectionEnd]; });
    inp.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    // 作り直しで外された時の blur は決定にしない（付いたまま焦点が外へ出た時だけ決める）
    inp.addEventListener("blur", () => setTimeout(() => { if (inp.isConnected) finish(true); }, 0));
    inp.focus({ preventScroll: true });
    if (!ed.sel) { const dot = ed.value.lastIndexOf("."); ed.sel = [0, dot > 0 ? dot : ed.value.length]; }   // 拡張子の手前まで選ぶ
    inp.setSelectionRange(ed.sel[0], ed.sel[1]);
  }
  function startCreate(dir) {
    // 置き場: 選んでいる行がフォルダーならその中・ファイルならその親・無ければ一番上
    const f = S.focusRel, fe = findEntry(f);
    const parent = fe ? (fe.dir ? f : parentOf(f)) : "";
    if (parent) { S.open.add(parent); if (!S.dirs.has(parent)) loadDir(parent); }
    S.edit = { kind: "create", parent, dir, value: "", label: dir ? "新しいフォルダーの名前" : "新しいファイルの名前", onDone: async (name) => {
      if (!name) { rerender(); return; }
      if (name.includes("..")) { flash("名前に .. は使えません"); rerender(); return; }
      const rel = (parent ? parent + "/" : "") + name.replace(/\\/g, "/").replace(/^\/+/, "");
      try {
        const made = await inv("ws_create", { rel, dir });
        S.focusRel = made;
        for (let k = 1; k < made.split("/").length; k++) { const d = made.split("/").slice(0, k).join("/"); S.open.add(d); }
        await loadDir(parent);
        if (!dir) openPath(made);
      } catch (e) { flash(String(e?.message || e)); rerender(); }
    } };
    rerender();
  }
  function findEntry(rel) {
    if (!rel) return null;
    const d = S.dirs.get(parentOf(rel));
    return d?.entries.find((e) => e.rel === rel) || null;
  }
  function startRename(rel) {
    if (!rel) return;
    const old = rel.split("/").pop();
    S.edit = { kind: "rename", rel, value: old, label: "新しい名前", onDone: async (name) => {
      if (!name || name === old) { rerender(); focusRow(rel); return; }
      if (/[\\/]/.test(name) || name === "." || name === "..") { flash("名前に / や \\ は使えません"); rerender(); return; }
      const to = (parentOf(rel) ? parentOf(rel) + "/" : "") + name;
      try {
        await inv("ws_rename", { from: rel, to });
        if (S.tabs.some((x) => x.kind === "file" && (x.rel === rel || x.rel?.startsWith(rel + "/"))))
          flash("開いているタブは古い名前のままです。新しい名前で開き直してください。");
        if (S.open.has(rel)) { S.open.delete(rel); S.open.add(to); }
        S.dirs.delete(rel); S.focusRel = to;
      } catch (e) { flash(String(e?.message || e)); }
      await loadDir(parentOf(rel));
      focusRow(S.focusRel);
    } };
    rerender();
  }
  function copyText(t, what) { navigator.clipboard?.writeText(t).then(() => flash(`${what}をコピーしました`), () => flash("コピーできません")); }
  function rowMenu(row, e, ev) {
    closeMenu();
    const m = el("div", "cmenu"); m.setAttribute("role", "menu"); m.setAttribute("aria-label", e.name);
    const item = (label, key, fn) => {
      const it = el("button", "cmenu__item"); it.type = "button"; it.setAttribute("role", "menuitem");
      it.append(el("span", "cmenu__t", label)); if (key) it.append(el("span", "cmenu__k", key));
      it.addEventListener("click", () => { closeMenu(); fn(); });
      m.append(it);
    };
    if (!e.dir) item("開く", "Enter", () => openPath(e.rel));
    item("新しいファイル", "", () => { S.focusRel = e.rel; startCreate(false); });
    item("新しいフォルダー", "", () => { S.focusRel = e.rel; startCreate(true); });
    item("名前を変更", "F2", () => startRename(e.rel));
    item("パスをコピー", "", () => copyText(absOf(e.rel), "パス"));
    item("相対パスをコピー", "", () => copyText(e.rel, "相対パス"));
    document.body.append(m);
    const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = `${Math.round(Math.max(8, Math.min(ev.clientX, window.innerWidth - w - 8)))}px`;
    m.style.top = `${Math.round(Math.max(8, Math.min(ev.clientY, window.innerHeight - h - 8)))}px`;
    m.addEventListener("keydown", (k) => {
      const items = [...m.querySelectorAll(".cmenu__item")]; const i = items.indexOf(document.activeElement);
      if (k.key === "ArrowDown" || k.key === "ArrowUp") { k.preventDefault(); items[(i + (k.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus(); }
      else if (k.key === "Escape") { k.preventDefault(); k.stopPropagation(); closeMenu(); row.focus(); }
    });
    const out = (x) => { if (!m.contains(x.target)) closeMenu(); };
    document.addEventListener("pointerdown", out, true);
    menu = { el: m, anchor: row, out };
    m.querySelector(".cmenu__item")?.focus();
  }
  let typed = "", typedAt = 0;
  // 木にフォーカスがあったか（作り直すと行が消えて body に落ちるので、別の所へ移った時だけ外す）
  let treeFocused = false;
  document.addEventListener("focusin", (e) => { const t = e.target; if (t?.closest?.(".xt")) { treeFocused = !t.classList?.contains("xt__input"); if (t.dataset?.rel != null && t.classList.contains("xt__row")) S.focusRel = t.dataset.rel; } else if (t && t !== document.body) treeFocused = false; }, true);
  function treeKeys(ev) {
    if (ev.target.tagName === "INPUT") return;
    treeFocused = true;   // 窓に焦点が無いと focusin が来ないことがあるので、キーを受けた事を印にする
    const list = ev.currentTarget;
    const rows = [...list.querySelectorAll(".xt__row[data-rel]")];
    if (!rows.length) return;
    let i = rows.indexOf(ev.target.closest?.(".xt__row")); if (i < 0) i = rows.indexOf(document.activeElement); if (i < 0) i = 0;
    const row = rows[i], rel = row.dataset.rel, isDir = row.dataset.dir === "1", open = S.open.has(rel);
    S.focusRel = rel;
    const go = (k) => { const r = rows[Math.max(0, Math.min(rows.length - 1, k))]; if (r) focusRow(r.dataset.rel); };
    const k = ev.key;
    if (k === "ArrowDown") go(i + 1);
    else if (k === "ArrowUp") go(i - 1);
    else if (k === "Home") go(0);
    else if (k === "End") go(rows.length - 1);
    else if (k === "ArrowRight") { if (isDir && !open) toggleDir(rel, true); else if (isDir && open && rows[i + 1] && parentOf(rows[i + 1].dataset.rel) === rel) go(i + 1); }
    else if (k === "ArrowLeft") { if (isDir && open) toggleDir(rel, false); else if (parentOf(rel) || rel.includes("/")) focusRow(parentOf(rel)); }
    else if (k === "Enter" || k === " ") { if (isDir) toggleDir(rel); else openPath(rel); }
    else if (k === "F2") startRename(rel);
    else if (k === "*") { const d = S.dirs.get(parentOf(rel)); (d?.entries || []).filter((e) => e.dir).forEach((e) => { S.open.add(e.rel); if (!S.dirs.has(e.rel)) loadDir(e.rel); }); rerender(); }
    else if (k.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey && /\S/.test(k)) {
      const now = Date.now(); typed = (now - typedAt < 700 ? typed : "") + k.toLowerCase(); typedAt = now;
      const order = rows.slice(i + (typed.length === 1 ? 1 : 0)).concat(rows.slice(0, i + (typed.length === 1 ? 1 : 0)));
      const hit = order.find((r) => (r.querySelector(".xt__name")?.textContent || "").toLowerCase().startsWith(typed));
      if (hit) focusRow(hit.dataset.rel);
    } else return;
    ev.preventDefault();
  }
  function buildTree(p) {
    const root = S.dirs.get("");
    if (!root) { p.append(el("p", "x-empty", "読み込んでいます")); loadDir(""); return; }
    const { map, dirs } = decorations();
    const hadFocus = treeFocused;
    const list = el("div", "xt"); list.setAttribute("role", "tree"); list.setAttribute("aria-label", "ワークスペースのファイル");
    let editRow = null;
    const note = (text, depth, cls = "") => { const n = el("div", "xt__note" + cls, text); n.style.setProperty("--depth", depth); return n; };
    const pendingRow = (depth) => {
      const r = el("div", "xt__row xt__row--new"); r.style.setProperty("--depth", depth);
      const ic = el("span", "xt__ic"); ic.innerHTML = S.edit.dir ? ICON.folder : ICON.file;
      r.append(el("span", "xt__chev xt__chev--none"), ic, el("span", "xt__name", ""));
      list.append(r);
      editRow = r;
    };
    const add = (rel, depth) => {
      const d = S.dirs.get(rel);
      if (S.edit?.kind === "create" && S.edit.parent === rel) pendingRow(depth);
      if (!d) { list.append(note("読み込んでいます", depth)); loadDir(rel); return; }
      if (d.err && !d.entries.length) {
        const n = note(`開けません: ${d.err} `, depth, " is-err");
        const b = el("button", "x-link", "もう一度"); b.type = "button"; b.addEventListener("click", () => loadDir(rel)); n.append(b);
        list.append(n); return;
      }
      if (!d.entries.length && !d.loading) { list.append(note(rel ? "空のフォルダー" : "ファイルはありません。新しいファイルを作るには上の ＋ を押します。", depth)); return; }
      for (const e of d.entries) {
        const isOpen = e.dir && S.open.has(e.rel);
        const code = e.dir ? "" : (map.get(e.rel) || "");
        const r = el("div", "xt__row " + (e.dir ? "xt__row--dir" : "xt__row--file") + (isOpen ? " is-open" : "")
          + (e.dir && dirs.has(e.rel) ? " is-changed" : "") + (code ? ` is-${code}` : "") + (e.ignored ? " is-ignored" : "")
          + (e.rel === S.activeRel ? " is-active" : ""));
        r.setAttribute("role", "treeitem"); r.tabIndex = -1; r.dataset.rel = e.rel; r.dataset.dir = e.dir ? "1" : "";
        r.setAttribute("aria-level", String(depth + 1));
        if (e.dir) r.setAttribute("aria-expanded", String(isOpen));
        r.setAttribute("aria-selected", String(e.rel === S.activeRel));
        r.style.setProperty("--depth", depth);
        const ic = el("span", "xt__ic"); paintIcon(ic, e.name, e.dir, isOpen);
        r.append(el("span", e.dir ? "xt__chev" : "xt__chev xt__chev--none"), ic, el("span", "xt__name", e.name));
        if (e.dir && dirs.has(e.rel)) r.append(el("span", "xt__dot", "●"));
        if (code) r.append(el("span", `x-code x-code--${code === "!" ? "C" : code}`, code));
        if (e.dir) metaCell(r, null, e.mtime, e.items != null ? `${e.items >= 10000 ? "10,000 以上" : e.items.toLocaleString()} 個の項目` : "", e.rel, e.items);
        else metaCell(r, e.size, e.mtime, "", e.rel);
        if (e.ignored) r.title = (r.title ? r.title + "\n" : "") + "git が無視するファイル（.gitignore）";
        r.addEventListener("click", () => { S.focusRel = e.rel; if (e.dir) toggleDir(e.rel); else { focusRow(e.rel); openPath(e.rel); } });
        r.addEventListener("contextmenu", (ev) => { ev.preventDefault(); focusRow(e.rel); rowMenu(r, e, ev); });
        list.append(r);
        if (isOpen) add(e.rel, depth + 1);
      }
      if (d.more) {
        const n = note("", depth);
        const b = el("button", "x-link", `さらに表示（残り ${(d.total - d.entries.length).toLocaleString()} 件）`); b.type = "button";
        b.addEventListener("click", () => loadDir(rel, true)); n.append(b); list.append(n);
      }
    };
    add("", 0);
    list.addEventListener("keydown", treeKeys);
    // 一覧に入ったら、選んでいる行か先頭の行へ（tabindex は 1 つだけ 0＝roving tabindex）
    const rows = [...list.querySelectorAll(".xt__row[data-rel]")];
    const cur = rows.find((r) => r.dataset.rel === S.focusRel) || rows[0];
    if (cur) cur.tabIndex = 0;
    p.append(list);
    if (S.edit?.kind === "rename") editRow = rows.find((r) => r.dataset.rel === S.edit.rel) || null;
    if (S.edit && editRow) { const ed = S.edit, row = editRow; queueMicrotask(() => { if (row.isConnected && S.edit === ed) inlineName(row, ed); }); }
    // 区画はまだ文書に付いていないことがある（renderSections が組んでから差し込む）ので、付いた後に合わせる
    const reveal = S.reveal && cur && cur.dataset.rel === S.activeRel;
    if (reveal) S.reveal = false;
    if (reveal || (hadFocus && cur)) queueMicrotask(() => {
      if (!cur.isConnected) return;
      if (reveal) cur.scrollIntoView({ block: "nearest" });
      if (hadFocus && !S.edit) cur.focus({ preventScroll: true });
    });
  }

  /* ── ブランチの一覧 ── */
  let menu = null;
  function closeMenu() { if (!menu) return; menu.el.remove(); document.removeEventListener("pointerdown", menu.out, true); menu = null; }
  async function branchMenu(anchor, up = true) {
    if (menu) { const same = menu.anchor === anchor; closeMenu(); if (same) return; }
    let data;
    try { data = await inv("scm_branches"); } catch (e) { flash(String(e?.message || e)); return; }
    const m = el("div", "cmenu cmenu--branch"); m.setAttribute("role", "menu"); m.setAttribute("aria-label", "ブランチ");
    const list = data?.branches || [];
    if (!list.length) m.append(el("p", "x-note", "ブランチはありません"));
    for (const b of list) {
      const it = el("button", "cmenu__item" + (b === data.current ? " is-cur" : "")); it.type = "button";
      it.setAttribute("role", "menuitemradio"); it.setAttribute("aria-checked", String(b === data.current));
      it.append(el("span", "cmenu__t", b));
      if (b === data.current) it.append(el("span", "cmenu__k", "現在"));
      it.addEventListener("click", async () => {
        closeMenu();
        if (b === data.current) return;
        if (busyHere()) { flash("実行中はブランチを切り替えられません"); return; }
        try { await inv("scm_switch", { branch: b }); flash(`${b} に切り替えました`); }
        catch (e) { flash(`切り替えられません: ${String(e?.message || e)}`); }
        if (typeof refreshWs === "function") refreshWs();
        S.key = ""; S.filesAt = 0; refresh();
      });
      m.append(it);
    }
    document.body.append(m);
    const r = anchor.getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    let top = up ? r.top - h - 6 : r.bottom + 6;
    if (top < 8) top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    m.style.left = `${Math.round(left)}px`; m.style.top = `${Math.round(top)}px`;
    m.addEventListener("keydown", (e) => {
      const items = [...m.querySelectorAll(".cmenu__item")]; const i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(); anchor.focus(); }
    });
    const out = (e) => { if (!m.contains(e.target) && !anchor.contains(e.target)) closeMenu(); };
    document.addEventListener("pointerdown", out, true);
    menu = { el: m, anchor, out };
    (m.querySelector(".cmenu__item.is-cur") || m.querySelector(".cmenu__item"))?.focus();
  }

  /* ── Quick Open のファイル（Ctrl+P）── */
  function score(path, q) {
    const p = path.toLowerCase(), base = p.slice(p.lastIndexOf("/") + 1);
    if (base.startsWith(q)) return 300 - p.length / 100;
    if (base.includes(q)) return 200 - p.length / 100;
    if (p.includes(q)) return 100 - p.length / 100;
    let i = 0;
    for (const ch of p) { if (ch === q[i]) i++; if (i === q.length) break; }
    return i === q.length ? 10 - p.length / 1000 : 0;
  }
  function qpFiles(q) {
    if (!S.files) return [];
    const words = String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
    const scored = [];
    for (const f of S.files) {
      let s = 0;
      if (words.length) { for (const w of words) { const v = score(f, w); if (!v) { s = 0; break; } s += v; } } else s = 1;
      if (s > 0) scored.push([s, f]);
      if (!words.length && scored.length >= 30) break;
    }
    scored.sort((a, b) => b[0] - a[0]);
    return scored.slice(0, 40).map(([, f]) => {
      const name = f.split("/").pop();
      return [name, "", () => { if (typeof showView === "function") showView("chat"); openPath(f); }, f.slice(0, Math.max(0, f.length - name.length - 1)), "file"];
    });
  }

  /* ── 別のウィンドウ（explorer.html）── */
  async function openStandalone(path) {
    S.sel = null; S.view = { path, pv: null, err: "" };
    renderStandalone();
    try { S.view.pv = await inv("file_preview", { path, workspace: wsNow() }); }
    catch (e) { if (S.view) S.view.err = String(e?.message || e); }
    renderStandalone();
  }
  function renderStandalone() {
    const p = host(); if (!p) return;
    layoutStandalone();
    const ws = document.getElementById("x-ws"); if (ws) ws.textContent = wsNow().split(/[\\/]/).filter(Boolean).pop() || "";
    if (S.sel) { renderDiff(p); return; }
    if (S.view) {
      const v = S.view;
      p.replaceChildren();
      editorHead(p, v.path, "← エクスプローラー", [
        [ICON.open, "既定のアプリで開く", async () => { try { await inv("file_open", { path: v.path, workspace: wsNow() }); } catch (e) { flash(String(e?.message || e)); } }],
      ]);
      if (v.err) { p.append(el("p", "x-empty", v.err)); return; }
      if (!v.pv) { p.append(el("p", "x-empty", "読み込んでいます")); return; }
      const pv = v.pv;
      if (pv.kind === "image" && pv.data) {
        const img = el("img", "x-img"); img.alt = pv.name || ""; img.src = `data:${pv.mime || "image/png"};base64,${pv.data}`;
        p.append(img); return;
      }
      if (typeof pv.text === "string") {
        p.append(codeBox(pv.text.split("\n").map((s, i) => ({ t: "ctx", n: i + 1, s })), pv.name || v.path));
        if (pv.truncated) p.append(el("p", "x-note", "長いので、最初の部分だけを表示しています"));
        return;
      }
      p.append(el("p", "x-empty", "この種類のファイルはここでは表示できません（既定のアプリで開く）"));
      return;
    }
    renderSections(p, null);
  }

  /* ══ エディター（2026-11-17 オーナー「よりVSCode機能に近づけて。エディタとしても使えるように。参考サイトとかGithubとかを確認して」）══
   * 中身は VS Code のエディターそのもの（monaco-editor 0.57・MIT・vendor/monaco・作り方は scripts/build_monaco.sh）。
   *   ・タブ（同じファイルは 1 つ・変更があれば ●・× は 2 回押すと保存せずに閉じる）。Ctrl+S で保存（Rust の editor_write）。
   *   ・保存の前にディスクの中身を読み直し、開いた後に誰か（エージェント）が書き換えていたら、上書きか読み直すかを選ばせる。
   *   ・変更していないファイルは、ディスク側が変わったら読み直す（エージェントの書いた物が見える）。
   *   ・変更の行は差分エディター（左が HEAD かステージ済み・右が作業中＝そのまま編集できる）。
   *   ・言語の色分け（約 90）・検索と置換・複数カーソル・折りたたみ・ミニマップ・括弧の色・スティッキー スクロールは Monaco の物。
   *   ・言語サーバー（補完・定義へ移動）は入れていない（重い）。読めないファイル（バイナリ・UTF-8 でない・5MB 超）は読み取り専用。 */
  const ED = { p: null, ed: null, df: null, host: null, tabsEl: null, noteEl: null, statusEl: null, models: new Map(), seq: 0 };
  S.tabs = []; S.active = null; S.mode = "explorer";
  const NOT_TEXT = /\.(png|jpe?g|gif|webp|bmp|ico|svg|pdf|docx?|xlsx?|pptx?|zip|7z|gz|tar|exe|dll|so|bin|gguf|safetensors|onnx|mp[34]|wav|ogg|ttf|otf|woff2?)$/i;
  const mBase = (() => { const s = document.currentScript?.src || location.href; return new URL("vendor/monaco/", s).href; })();
  function loadMonaco() {
    if (ED.p) return ED.p;
    ED.p = new Promise((res, rej) => {
      if (window.monaco) { res(window.monaco); return; }
      const css = document.createElement("link"); css.rel = "stylesheet"; css.href = mBase + "monaco.css"; document.head.append(css);
      const env = document.createElement("script"); env.src = mBase + "env.js";
      env.onload = () => {
        const m = document.createElement("script"); m.src = mBase + "monaco.js";
        m.onload = () => (window.monaco ? res(window.monaco) : rej(new Error("エディターを読み込めません")));
        m.onerror = () => rej(new Error("エディターを読み込めません"));
        document.head.append(m);
      };
      env.onerror = () => rej(new Error("エディターを読み込めません"));
      document.head.append(env);
    });
    ED.p.catch(() => { ED.p = null; });
    return ED.p;
  }
  const cssVar = (n, fb) => (getComputedStyle(document.documentElement).getPropertyValue(n).trim() || fb);
  function themeName() {
    const m = window.monaco; if (!m) return "vs-dark";
    const light = document.documentElement.dataset.theme === "light";
    m.editor.defineTheme("laps", {
      base: light ? "vs" : "vs-dark", inherit: true, rules: [],
      colors: {
        "editor.background": cssVar("--editor-bg", light ? "#F3F3F5" : "#1C1C20"),
        "editorGutter.background": cssVar("--editor-bg", light ? "#F3F3F5" : "#1C1C20"),
        "minimap.background": cssVar("--editor-bg", light ? "#F3F3F5" : "#1C1C20"),
        "editor.lineHighlightBackground": light ? "#0000000A" : "#FFFFFF0A",
        "editorLineNumber.foreground": light ? "#9A9AA4" : "#5F5F68",
        "editorLineNumber.activeForeground": light ? "#16161C" : "#ECECF1",
        "editorWidget.background": light ? "#F0F0F3" : "#232327",
        "diffEditor.insertedTextBackground": light ? "#1E7F3A22" : "#2EA04333",
        "diffEditor.removedTextBackground": light ? "#B42A2022" : "#F8514933",
      },
    });
    return "laps";
  }
  const langOf = (rel) => {
    const m = window.monaco; const name = rel.split("/").pop().toLowerCase();
    const ext = name.includes(".") ? "." + name.split(".").pop() : "";
    for (const l of m.languages.getLanguages()) {
      if ((l.filenames || []).some((f) => f.toLowerCase() === name)) return l.id;
      if (ext && (l.extensions || []).some((x) => x.toLowerCase() === ext)) return l.id;
    }
    return "plaintext";
  };
  const opts = () => ({
    automaticLayout: true, fontFamily: cssVar("--f-mono", "Consolas, monospace"), fontSize: 13, lineHeight: 20,
    minimap: { enabled: (ED.host?.clientWidth || 0) > 680 }, scrollBeyondLastLine: false, smoothScrolling: true,
    renderWhitespace: "selection", bracketPairColorization: { enabled: true }, stickyScroll: { enabled: true },
    guides: { bracketPairs: "active", indentation: true }, unicodeHighlight: { ambiguousCharacters: false },
    theme: themeName(), fixedOverflowWidgets: true,
  });

  /* 器（メインのウィンドウは右のパネルの #editor・別のウィンドウは #editor）。一度だけ作る。 */
  function mount() {
    const root = document.getElementById("editor");
    if (!root) return null;
    if (ED.root === root) return root;
    ED.root = root;
    root.replaceChildren();
    const bar = el("div", "xe__bar");
    const back = el("button", "x-icon xe__back"); back.type = "button"; back.title = "エクスプローラー"; back.setAttribute("aria-label", "エクスプローラーに戻る");
    back.innerHTML = svg('<path d="M10 3.5 5.5 8l4.5 4.5"/>');
    back.addEventListener("click", () => showExplorer());
    ED.tabsEl = el("div", "xe__tabs"); ED.tabsEl.setAttribute("role", "tablist"); ED.tabsEl.setAttribute("aria-label", "開いているエディター");
    const acts = el("span", "xe__acts");
    const prev = el("button", "x-icon xe__preview"); prev.type = "button"; prev.title = "プレビュー"; prev.setAttribute("aria-label", "プレビュー");
    prev.innerHTML = svg('<path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/>');
    prev.addEventListener("click", () => { const t = activeTab(); if (t && typeof openFile === "function") { showExplorer(); openFile(t.rel); } });
    ED.prevBtn = prev;
    const save = el("button", "x-icon"); save.type = "button"; save.title = "保存（Ctrl+S）"; save.setAttribute("aria-label", "保存");
    save.innerHTML = svg('<path d="M3 2.5h8l2 2v9H3z"/><path d="M5 2.5v3.5h5V2.5M5 13.5V9h6v4.5"/>');
    save.addEventListener("click", () => saveActive());
    acts.append(prev, save);
    if (DOCKED) {
      const max = el("button", "x-icon"); max.type = "button"; max.title = "パネルを広げる／戻す"; max.setAttribute("aria-label", "パネルを広げる／戻す");
      max.innerHTML = svg('<path d="M2.5 6V2.5H6M13.5 6V2.5H10M2.5 10v3.5H6M13.5 10v3.5H10"/>');
      max.addEventListener("click", () => toggleWide());
      acts.append(max);
    }
    bar.append(back, ED.tabsEl, acts);
    ED.noteEl = el("div", "xe__note"); ED.noteEl.hidden = true; ED.noteEl.setAttribute("role", "alert");
    ED.host = el("div", "xe__host");
    ED.edEl = el("div", "xe__ed"); ED.dfEl = el("div", "xe__ed"); ED.dfEl.hidden = true;
    ED.host.append(ED.edEl, ED.dfEl);
    root.append(bar, ED.noteEl, ED.host);
    if (!DOCKED) { ED.statusEl = el("div", "xe__status num"); root.append(ED.statusEl); }
    return root;
  }
  // VS Code と同じキー（2026-11-21）: Ctrl+W 閉じる・Ctrl+PageDown/PageUp 隣のタブ。右クリックに「エクスプローラーで表示」「パスをコピー」。
  function bindEditorKeys(ed) {
    const m = window.monaco;
    const step = (d) => { const i = S.tabs.findIndex((t) => t.id === S.active); if (i < 0 || S.tabs.length < 2) return; activate(S.tabs[(i + d + S.tabs.length) % S.tabs.length].id); };
    ed.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.KeyW, () => { if (S.active) closeTab(S.active); });
    ed.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.PageDown, () => step(1));
    ed.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.PageUp, () => step(-1));
    ed.addAction({ id: "bw.reveal", label: "エクスプローラーで表示", contextMenuGroupId: "9_bw", contextMenuOrder: 1,
      run: () => { const t = activeTab(); if (!t?.rel) return; S.activeRel = ""; if (DOCKED) showExplorer(); revealRel(t.rel); } });
    ed.addAction({ id: "bw.copyPath", label: "パスをコピー", contextMenuGroupId: "9_bw", contextMenuOrder: 2,
      run: () => { const t = activeTab(); if (t?.rel) copyText(absOf(t.rel), "パス"); } });
    ed.addAction({ id: "bw.copyRel", label: "相対パスをコピー", contextMenuGroupId: "9_bw", contextMenuOrder: 3,
      run: () => { const t = activeTab(); if (t?.rel) copyText(t.rel, "相対パス"); } });
  }
  function ensureEditors() {
    const m = window.monaco;
    if (!ED.ed) {
      ED.ed = m.editor.create(ED.edEl, opts());
      ED.ed.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.KeyS, () => saveActive());
      bindEditorKeys(ED.ed);
      ED.ed.onDidChangeCursorPosition(() => paintStatus());
      ED.ed.onDidChangeCursorSelection(() => { if (DOCKED) window.dispatchEvent(new Event("bw-editor-ctx")); });
    }
    if (!ED.df) {
      ED.df = m.editor.createDiffEditor(ED.dfEl, { ...opts(), renderSideBySide: (ED.host?.clientWidth || 0) > 720, originalEditable: false, ignoreTrimWhitespace: false });
      ED.df.getModifiedEditor().addCommand(m.KeyMod.CtrlCmd | m.KeyCode.KeyS, () => saveActive());
      bindEditorKeys(ED.df.getModifiedEditor());
      ED.df.getModifiedEditor().onDidChangeCursorPosition(() => paintStatus());
      ED.df.getModifiedEditor().onDidChangeCursorSelection(() => { if (DOCKED) window.dispatchEvent(new Event("bw-editor-ctx")); });
    }
  }
  const activeTab = () => S.tabs.find((t) => t.id === S.active) || null;

  /* ファイルの中身の器（同じファイルはタブと差分の右側で同じ物を使う＝どちらで直しても同じ）。 */
  async function fileModel(rel) {
    const m = window.monaco;
    let fm = ED.models.get(rel);
    if (fm && !fm.model.isDisposed()) return fm;
    const r = await inv("editor_read", { path: rel });
    if (r.binary) throw Object.assign(new Error("バイナリ ファイル"), { binary: true });
    const model = m.editor.createModel(r.text, langOf(rel), m.Uri.parse("laps:/" + encodeURI(rel)));
    fm = { rel: r.rel || rel, model, base: r.text, modified: r.modified, editable: !!r.editable, lossy: r.lossy, truncated: r.truncated, saved: model.getAlternativeVersionId(), conflict: false };
    model.onDidChangeContent(() => { paintTabs(); });
    ED.models.set(rel, fm);
    return fm;
  }
  const isDirty = (fm) => !!fm && fm.model.getAlternativeVersionId() !== fm.saved;
  const tabDirty = (t) => t.kind === "file" ? isDirty(ED.models.get(t.rel)) : (t.kind === "diff" && !t.staged ? isDirty(ED.models.get(t.rel)) : false);

  async function openEditor(path) {
    let m;
    try { m = await loadMonaco(); } catch (e) { flash(String(e?.message || e)); fallbackOpen(path); return; }
    const rel = toWsRel(path);
    if (rel == null) { fallbackOpen(path); return; }
    let t = S.tabs.find((x) => x.kind === "file" && x.rel === rel);
    if (!t) {
      try { await fileModel(rel); }
      catch (e) { if (e?.binary) { fallbackOpen(path); return; } flash(`開けません: ${String(e?.message || e)}`); return; }
      t = { id: ++ED.seq, kind: "file", rel, title: rel.split("/").pop(), dir: rel.split("/").slice(0, -1).join("/") };
      S.tabs.push(t);
    }
    activate(t.id);
  }
  function fallbackOpen(path) {
    if (DOCKED && typeof openFile === "function") { showExplorer(); openFile(path); }
    else openStandalone(path);
  }
  /* 画面から来たパス（ワークスペースからの相対・リポジトリの一番上からの相対・絶対）を、ワークスペースからの相対にする。外なら null。 */
  function toWsRel(path) {
    const norm = (s) => String(s || "").replace(/\\/g, "/").replace(/\/+$/, "");
    const p = norm(path), ws = norm(S.st?.ws || wsNow());
    if (!/^([a-zA-Z]:\/|\/)/.test(p)) return p;
    if (ws && p.toLowerCase().startsWith(ws.toLowerCase() + "/")) return p.slice(ws.length + 1);
    return null;
  }
  const repoToWs = (rpath) => {
    const top = String(S.st?.top || "").replace(/\\/g, "/").replace(/\/+$/, "");
    return toWsRel(top ? `${top}/${rpath}` : rpath);
  };

  async function openDiffTab(r) {
    let m;
    try { m = await loadMonaco(); } catch { openDiff(r); return; }
    const key = `${r.path}|${r.staged ? "s" : "w"}`;
    let t = S.tabs.find((x) => x.kind === "diff" && x.key === key);
    if (!t) {
      try {
        const left = r.kind === "untracked" ? { text: "" } : await inv("scm_show", { path: r.path, rev: r.staged ? "HEAD" : "INDEX" });
        const orig = m.editor.createModel(left.text || "", langOf(r.path));
        let modifiedModel, rel = null;
        if (r.staged) {
          const right = await inv("scm_show", { path: r.path, rev: "INDEX" });
          modifiedModel = m.editor.createModel(right.text || "", langOf(r.path));
        } else if (r.code === "D") {
          modifiedModel = m.editor.createModel("", langOf(r.path));
        } else {
          rel = repoToWs(r.path);
          if (rel != null) modifiedModel = (await fileModel(rel)).model;
          else modifiedModel = m.editor.createModel("", langOf(r.path));
        }
        t = { id: ++ED.seq, kind: "diff", key, rel, staged: !!r.staged, title: r.path.split("/").pop(), dir: r.staged ? "ステージ済み" : "作業ツリー", orig, mod: modifiedModel, own: !rel };
        S.tabs.push(t);
      } catch (e) { flash(`差分を開けません: ${String(e?.message || e)}`); return; }
    }
    activate(t.id);
  }

  /* ★この会話でエージェントが変えたファイルの差分（2026-11-17 オーナー「ツールとハーネス強化」の案 3）。
   *   左＝この会話で最後にそのファイルを変えた依頼の前の中身（本体の undo の控え・Rust の change_base が読む）、右＝今のファイル（編集できる）。
   *   右の余白の矢印でその塊だけ元に戻せる（Monaco の差分エディターの revert）→ 保存で確定＝塊ごとに採るか戻すかを決められる。 */
  async function openChangeDiff(path, session, line) {
    let m;
    try { m = await loadMonaco(); } catch (e) { flash(String(e?.message || e)); return; }
    const rel = toWsRel(path);
    if (rel == null) { flash("ワークスペースの外のファイルです"); return; }
    const key = `agent|${rel}`;
    let t = S.tabs.find((x) => x.kind === "diff" && x.key === key);
    if (t) closeTab(t.id, true);           // 開き直すたびに今の控えから作り直す（間に別の依頼が変えているかもしれない）
    const ws = String(S.st?.ws || wsNow() || "").replace(/\\/g, "/").replace(/\/+$/, "");
    let base;
    try { base = await inv("change_base", { session: String(session || ""), path: ws ? `${ws}/${rel}` : rel }); }
    catch (e) { flash(`変更の記録を読めません: ${String(e?.message || e)}`); return; }
    if (!base || !base.found) { flash("このチャットでエージェントが変更したファイルはありません"); await openAt(path, line); return; }
    try {
      const orig = m.editor.createModel(base.text || "", langOf(rel));
      const fm = await fileModel(rel);
      t = { id: ++ED.seq, kind: "diff", key, rel, staged: false, agent: true, created: !base.existed, title: rel.split("/").pop(), dir: "このチャットの前", orig, mod: fm.model, own: false };
      S.tabs.push(t);
    } catch (e) { flash(`差分を開けません: ${String(e?.message || e)}`); return; }
    activate(t.id);
    if (line) setTimeout(() => { const ed = ED.df?.getModifiedEditor(); if (ed) { ed.revealLineInCenter(line); ed.setPosition({ lineNumber: line, column: 1 }); } }, 80);
  }
  /* 開いて行へ移る（エージェントの「ここを見て」）。 */
  async function openAt(path, line) {
    await openEditor(path);
    const t = activeTab(); const n = Math.max(1, Number(line) || 1);
    if (t && t.kind === "file" && ED.ed && n > 1) { ED.ed.revealLineInCenter(n); ED.ed.setPosition({ lineNumber: n, column: 1 }); ED.ed.focus(); }
  }
  /* 依頼に添えるエディターの今（開いているファイル・カーソルの行・選んでいる範囲）。タブが無ければ null。 */
  function editorContext() {
    // ★エディターが見えている間だけ（2026-11-17: エクスプローラーへ戻った後や欄を閉じた後も、開いたままのタブを全部の依頼に添えていた）。
    if (!editorVisible()) return null;
    const t = activeTab(); if (!t || !t.rel) return null;
    const view = t.kind === "diff" ? ED.df?.getModifiedEditor() : ED.ed;
    const model = view?.getModel(); if (!model) return null;
    const pos = view.getPosition(); const sel = view.getSelection();
    const out = { rel: t.rel, line: pos ? pos.lineNumber : 1, lang: model.getLanguageId(), from: 0, to: 0, text: "" };
    if (sel && !sel.isEmpty()) {
      out.from = sel.startLineNumber; out.to = sel.endLineNumber;
      out.text = model.getValueInRange(sel).slice(0, 6000);
    }
    return out;
  }

  function activate(id) {
    const t = S.tabs.find((x) => x.id === id); if (!t) return;
    const prev = activeTab();
    if (prev && prev !== t) prev.view = prev.kind === "diff" ? ED.df?.saveViewState() : ED.ed?.saveViewState();
    S.active = id;
    showEditor();
    ensureEditors();
    if (t.kind === "file") {
      const fm = ED.models.get(t.rel);
      ED.dfEl.hidden = true; ED.edEl.hidden = false;
      ED.ed.setModel(fm.model);
      ED.ed.updateOptions({ readOnly: !fm.editable, minimap: { enabled: (ED.host?.clientWidth || 0) > 680 } });
      if (t.view) ED.ed.restoreViewState(t.view);
      ED.ed.focus();
    } else {
      ED.edEl.hidden = true; ED.dfEl.hidden = false;
      ED.df.setModel({ original: t.orig, modified: t.mod });
      const fm = t.rel ? ED.models.get(t.rel) : null;
      ED.df.updateOptions({ readOnly: t.staged || !fm || !fm.editable, renderSideBySide: (ED.host?.clientWidth || 0) > 720 });
      if (t.view) ED.df.restoreViewState(t.view);
    }
    if (ED.prevBtn) ED.prevBtn.hidden = !(DOCKED && t.kind === "file" && /\.(md|markdown|html?|csv|tsv)$/i.test(t.rel));
    if (t.kind === "file" && t.rel) revealRel(t.rel);   // 編集中のファイルを木の中で見せる（VS Code の autoReveal）
    showNote();
    paintTabs(); paintStatus();
    checkDisk(t);
  }

  function closeTab(id, force = false) {
    const i = S.tabs.findIndex((x) => x.id === id); if (i < 0) return;
    const t = S.tabs[i];
    if (!force && tabDirty(t) && !S.tabs.some((x) => x !== t && x.rel === t.rel)) {
      if (t.armed) { t.armed = false; } else { t.armed = true; paintTabs(); setTimeout(() => { if (t.armed) { t.armed = false; paintTabs(); } }, 4000); return; }
    }
    S.tabs.splice(i, 1);
    if (t.kind === "diff") { t.orig?.dispose(); if (t.own) t.mod?.dispose(); }
    if (t.rel && !S.tabs.some((x) => x.rel === t.rel)) { const fm = ED.models.get(t.rel); if (fm) { if (ED.ed?.getModel() === fm.model) ED.ed.setModel(null); if (ED.df?.getModel()?.modified === fm.model) ED.df.setModel(null); fm.model.dispose(); ED.models.delete(t.rel); } }
    if (t.kind === "diff" && ED.df?.getModel()?.original === t.orig) ED.df.setModel(null);
    if (S.active === id) {
      const next = S.tabs[i] || S.tabs[i - 1];
      if (next) activate(next.id); else { S.active = null; showExplorer(); }
    } else paintTabs();
    if (!DOCKED) renderStandalone();
  }

  function paintTabs() {
    if (!ED.tabsEl) return;
    ED.tabsEl.replaceChildren();
    for (const t of S.tabs) {
      const dirty = tabDirty(t);
      const tab = el("div", "xe__tab" + (t.id === S.active ? " is-active" : "") + (dirty ? " is-dirty" : "") + (t.kind === "diff" ? " is-diff" : ""));
      tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(t.id === S.active));
      const b = el("button", "xe__tt"); b.type = "button"; b.title = t.rel || t.key || t.title;
      b.append(el("span", "xe__tn", t.kind === "diff" ? `${t.title} ↔` : t.title));
      if (t.dir) b.append(el("span", "xe__td", t.dir));
      b.addEventListener("click", () => activate(t.id));
      b.addEventListener("auxclick", (e) => { if (e.button === 1) closeTab(t.id); });
      const x = el("button", "xe__x" + (t.armed ? " is-armed" : ""), t.armed ? "保存しない" : dirty ? "●" : "×"); x.type = "button";
      x.title = t.armed ? "保存せずに閉じる（もう一度押す）" : "閉じる"; x.setAttribute("aria-label", `${t.title} を閉じる`);
      x.addEventListener("click", (e) => { e.stopPropagation(); closeTab(t.id, t.armed); });
      tab.append(b, x);
      ED.tabsEl.append(tab);
    }
    ED.tabsEl.querySelector(".is-active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (DOCKED && document.getElementById("files")?.hidden === false) rerender();
  }

  function showNote() {
    const t = activeTab(); const n = ED.noteEl; if (!n) return;
    const fm = t?.rel ? ED.models.get(t.rel) : null;
    n.replaceChildren(); n.hidden = true;
    if (!t) return;
    if (fm && fm.conflict) {
      n.append(el("span", "", "ディスク上のファイルが、開いた後に書き換えられています。"));
      const over = el("button", "x-back", "上書き保存"); over.type = "button"; over.addEventListener("click", () => saveActive(true));
      const re = el("button", "x-back", "再読み込み（変更を破棄）"); re.type = "button"; re.addEventListener("click", () => reloadModel(fm, true));
      n.append(over, re); n.hidden = false; return;
    }
    if (fm && !fm.editable) { n.append(el("span", "", fm.truncated ? "大きいファイルのため読み取り専用です（先頭 5MB）" : fm.lossy ? "UTF-8 ではないため読み取り専用です" : "読み取り専用です")); n.hidden = false; return; }
    if (t.kind === "diff" && t.staged) { n.append(el("span", "", "ステージ済みの差分（読み取り専用）")); n.hidden = false; }
    if (t.kind === "diff" && t.agent) {
      n.append(el("span", "", t.created ? "このチャットで作ったファイルです。" : "左はこのチャットでエージェントが変更する前、右が今です。"),
        el("span", "", " 右の余白の矢印でその変更だけ元に戻せます。保存（Ctrl+S）で確定します。"));
      n.classList.add("is-info"); n.hidden = false;
    } else n.classList.remove("is-info");
  }

  async function saveActive(force = false) {
    const t = activeTab(); if (!t || !t.rel) { flash("このタブは保存できません"); return; }
    const fm = ED.models.get(t.rel); if (!fm) return;
    if (!fm.editable) { flash("読み取り専用です"); return; }
    if (t.kind === "diff" && t.staged) { flash("ステージ済みの差分は保存できません"); return; }
    try {
      if (!force) {
        const disk = await inv("editor_read", { path: fm.rel });
        if (disk.text !== fm.base) { fm.conflict = true; showNote(); flash("ディスク上のファイルが書き換えられています"); return; }
      }
      const text = fm.model.getValue();
      await inv("editor_write", { path: fm.rel, content: text });
      fm.base = text; fm.saved = fm.model.getAlternativeVersionId(); fm.conflict = false;
      const r = await inv("editor_read", { path: fm.rel }).catch(() => null); if (r) fm.modified = r.modified;
      showNote(); paintTabs(); flash(`${fm.rel} を保存しました`);
      S.key = ""; refresh();
    } catch (e) { flash(`保存できません: ${String(e?.message || e)}`); }
  }
  async function reloadModel(fm, force = false) {
    if (!fm || (!force && isDirty(fm))) return;
    try {
      const r = await inv("editor_read", { path: fm.rel });
      if (r.text !== fm.model.getValue()) fm.model.setValue(r.text);
      fm.base = r.text; fm.modified = r.modified; fm.saved = fm.model.getAlternativeVersionId(); fm.conflict = false; fm.editable = !!r.editable;
      showNote(); paintTabs();
    } catch { /* 消えた・読めない: そのまま */ }
  }
  /* 開いているファイルがディスク側で変わっていたら（エージェントが書いた）、変更していなければ読み直す。 */
  async function checkDisk(t) {
    t = t || activeTab(); if (!t?.rel) return;
    const fm = ED.models.get(t.rel); if (!fm) return;
    try {
      const r = await inv("editor_read", { path: fm.rel });
      if (r.modified === fm.modified && r.text === fm.base) return;
      if (isDirty(fm)) { if (r.text !== fm.base) { fm.conflict = true; showNote(); } return; }
      if (r.text !== fm.model.getValue()) fm.model.setValue(r.text);
      fm.base = r.text; fm.modified = r.modified; fm.saved = fm.model.getAlternativeVersionId();
      paintTabs();
    } catch { /* 読めない */ }
  }

  function paintStatus() {
    const t = activeTab();
    const view = t ? (t.kind === "diff" ? ED.df?.getModifiedEditor() : ED.ed) : null;
    const model = view?.getModel();
    const pos = view?.getPosition();
    const fm = t?.rel ? ED.models.get(t.rel) : null;
    const items = model && pos ? [
      `行 ${pos.lineNumber}、列 ${pos.column}`,
      (window.monaco.languages.getLanguages().find((l) => l.id === model.getLanguageId())?.aliases?.[0]) || model.getLanguageId(),
      model.getEOL() === "\r\n" ? "CRLF" : "LF", "UTF-8",
      ...(fm && !fm.editable ? ["読み取り専用"] : []),
    ] : [];
    if (ED.statusEl) ED.statusEl.textContent = items.join("　·　");
    if (DOCKED) window.dispatchEvent(new Event("bw-editor-ctx"));   // 入力欄の下の「エディターの今」の札（app.js paintEdCtx）
    const sb = document.getElementById("sb-editor");
    if (sb) { const on = DOCKED ? editorVisible() && items.length : false; sb.hidden = !on; const tt = sb.querySelector(".sb__t"); if (tt) tt.textContent = on ? items.join("   ") : ""; }
  }
  const editorVisible = () => DOCKED ? (typeof drawer !== "undefined" && drawer.open && drawer.tab === "files" && S.mode === "editor") : true;

  /* メインのウィンドウ: パネルの中で エクスプローラー と エディター を切り替える（狭い所では 1 つずつ・VS Code のサイドバーとエディターの代わり）。 */
  function syncDock() {
    if (!DOCKED) { layoutStandalone(); return; }
    const files = document.getElementById("files"), edEl = document.getElementById("editor");
    const tabOn = typeof drawer !== "undefined" && drawer.tab === "files";
    if (files) files.hidden = !tabOn || S.mode === "editor";
    if (edEl) edEl.hidden = !tabOn || S.mode !== "editor";
    paintStatus();
  }
  function showEditor() { S.mode = "editor"; mount(); if (DOCKED && typeof setDrawer === "function" && !drawer.open) setDrawer(true); if (DOCKED && drawer.tab !== "files") setDrawerTab("files"); syncDock(); }
  function showExplorer() { S.mode = "explorer"; syncDock(); rerender(); }
  function toggleWide() {
    if (typeof setDrawerWidth !== "function") return;
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--drawer"), 10) || 380;
    const max = Math.max(380, Math.min(1100, window.innerWidth - 560));
    if (cur < max - 20) { ED.prevW = cur; setDrawerWidth(max, false); } else setDrawerWidth(ED.prevW || 380, false);
  }
  /* 別のウィンドウ: 広ければ エクスプローラー（左）と エディター（右）を並べる。狭ければメインと同じく切り替える。 */
  function layoutStandalone() {
    const wide = window.innerWidth >= 760;
    document.body.classList.toggle("xwin--wide", wide);
    const x = document.getElementById("x-root"), e = document.getElementById("editor");
    if (x) x.hidden = !wide && S.mode === "editor";
    if (e) e.hidden = !S.tabs.length || (!wide && S.mode !== "editor");
    if (ED.dfEl && ED.df) ED.df.updateOptions({ renderSideBySide: (ED.host?.clientWidth || 0) > 720 });
  }
  window.addEventListener("resize", () => { if (!DOCKED) layoutStandalone(); });
  window.addEventListener("bw-theme", () => { if (window.monaco) window.monaco.editor.setTheme(themeName()); });
  window.addEventListener("storage", (e) => { if (e.key === "bw.theme" && window.monaco) setTimeout(() => window.monaco.editor.setTheme(themeName()), 50); });

  /* 区画「開いているエディター」（VS Code の OPEN EDITORS）。タブがあるときだけ一番上に出す。 */
  function buildOpen(p) {
    for (const t of S.tabs) {
      const dirty = tabDirty(t);
      const r = el("div", "scm__row xo__row" + (t.id === S.active ? " is-active" : ""));
      const b = el("button", "scm__file"); b.type = "button"; b.title = t.rel || t.key;
      b.append(el("span", "scm__name", t.kind === "diff" ? `${t.title} ↔` : t.title), el("span", "scm__dir", t.dir || ""));
      b.addEventListener("click", () => activate(t.id));
      const x = el("button", "x-icon x-icon--text", dirty ? "●" : "×"); x.type = "button"; x.title = "閉じる"; x.setAttribute("aria-label", `${t.title} を閉じる`);
      x.addEventListener("click", () => closeTab(t.id));
      r.append(b, x);
      p.append(r);
    }
  }

  /* 実行中に変わったファイルが早く出るように、見ている間は 6 秒ごと。見ていなくても状態バーの数は 30 秒ごと。 */
  let tick = 0;
  setInterval(() => { tick++; if (shown() || tick % 5 === 0) refresh(); }, 6000)?.unref?.();
  window.addEventListener("focus", () => refresh());

  window.bwX = {
    refresh, renderSections, renderDiff, closeDiff, branchMenu, loadFiles, qpFiles,
    diffOpen: () => !!S.sel,
    showScm: () => { S.collapsed.scm = false; saveCollapsed(); if (DOCKED && typeof setDrawer === "function") { setDrawer(true); setDrawerTab("files"); } S.key = ""; refresh(); },
    shown: () => { S.key = ""; syncDock(); refresh(); if (!S.files) loadFiles().then(() => rerender()); },
    sync: () => syncDock(),
    toExplorer: () => { if (S.mode !== "explorer") { S.mode = "explorer"; syncDock(); } },
    openEditor: (p) => openPath(p),
    openAt: (p, line) => openAt(p, line),
    openChangeDiff: (p, session, line) => openChangeDiff(p, session, line),
    editorContext: () => editorContext(),
    hasTabs: () => S.tabs.length > 0,
    _ed: () => ED,
    wsChanged: () => { S.files = null; S.tree = null; S.dirs = new Map(); S.open = new Set([""]); S.activeRel = ""; S.focusRel = ""; S.sel = null; S.diff = null; S.view = null; S.key = ""; refresh(); },
    popOut: async () => {
      try { await inv("explorer_window_open"); if (DOCKED && typeof setDrawer === "function") setDrawer(false); }
      catch (e) { flash(`ウィンドウを開けません: ${String(e?.message || e)}`); }
    },
    _state: () => S,
  };
  document.getElementById("sb-scm")?.addEventListener("click", () => window.bwX.showScm());
  document.getElementById("sb-branch")?.addEventListener("click", (e) => branchMenu(e.currentTarget, true));
  document.getElementById("drawer-pop")?.addEventListener("click", () => window.bwX.popOut());

  if (!DOCKED) {
    // 別のウィンドウ: 色はアプリの設定（localStorage の bw.theme）に合わせ、ほかのウィンドウで替えたら追う。
    const theme = () => {
      let pref = "system";
      try { const v = localStorage.getItem("bw.theme"); if (v === "dark" || v === "light") pref = v; } catch { /* 既定 */ }
      document.documentElement.dataset.theme = pref !== "system" ? pref : (window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark");
    };
    theme();
    window.addEventListener("storage", (e) => { if (e.key === "bw.theme") theme(); });
    document.getElementById("x-dock")?.addEventListener("click", async () => {
      try { await T?.event?.emit("explorer-dock", {}); } catch { /* 古い WebView */ }
      T?.window?.getCurrentWindow?.().close();
    });
    renderStandalone();
    refresh().then(() => loadFiles().then(renderStandalone));
  } else {
    // 別のウィンドウの「戻す」: 右のパネルでエクスプローラーを開き直す。
    T?.event?.listen?.("explorer-dock", () => { if (typeof setDrawer === "function") { setDrawer(true); setDrawerTab("files"); } });
    // ★エージェントの Editor 道具（2026-11-17）: ファイルを開いて行へ・または この会話の差分を見せる。
    T?.event?.listen?.("editor-agent", (ev) => {
      const a = ev?.payload || {};
      if (!a.path) return;
      if (a.diff) openChangeDiff(a.path, a.session, a.line); else openAt(a.path, a.line);
    });
    setTimeout(refresh, 1500);
  }
})();
