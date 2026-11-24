/* 別の窓の Files（2026-10-18 オーナー「画面分離にも対応させたい」→「両方」）。
 * 中身はメインの窓が自分の描き方で作った HTML（文字は描く前に esc 済み）か、PDF の base64。ここでは描かない。
 * 開いた直後はまだ事象が届かないので、Rust に預けてある中身を viewer_pending で取りに行く。開いた後は viewer-show で届く。 */
(() => {
  const T = window.__TAURI__;
  const $ = (s) => document.querySelector(s);
  let cur = null, blob = "";

  function show(p) {
    if (!p || typeof p !== "object") return;
    cur = p;
    document.title = `${p.name || "Files"} — L.A.P.S`;
    $("#v-name").textContent = p.name || "";
    $("#v-path").textContent = p.path || "";
    $("#v-open").hidden = !p.can_open;
    $("#v-msg").textContent = "";
    const body = $("#v-body");
    if (blob) { URL.revokeObjectURL(blob); blob = ""; }
    if (p.kind === "pdf") {
      const bin = atob(p.data || ""), u8 = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
      blob = URL.createObjectURL(new Blob([u8], { type: "application/pdf" }));
      const f = document.createElement("iframe");
      f.className = "fv__frame fv__frame--pdf"; f.src = blob; f.title = p.name || "PDF";
      body.replaceChildren(f);
    } else {
      body.innerHTML = p.html || "";
    }
  }
  async function act(cmd) {
    if (!cur) return;
    try { await T.core.invoke(cmd, { path: cur.path, workspace: cur.workspace || "" }); $("#v-msg").textContent = ""; }
    catch (e) { $("#v-msg").textContent = String(e?.message || e); }
  }
  $("#v-reload").addEventListener("click", () => { if (cur) T.event.emit("viewer-reload", { path: cur.path }); });
  $("#v-reveal").addEventListener("click", () => act("file_reveal"));
  $("#v-open").addEventListener("click", () => act("file_open"));
  $("#v-dock").addEventListener("click", async () => {
    if (cur) await T.event.emit("viewer-dock", { path: cur.path });
    T.window.getCurrentWindow().close();
  });
  // コードの「copy」はメインの窓で付けた押し下げの処理が HTML に乗らないので、ここで拾う。
  document.addEventListener("click", (e) => {
    const b = e.target.closest?.(".md-code__copy");
    if (!b) return;
    const code = b.closest(".md-code")?.querySelector("code, pre")?.textContent || "";
    navigator.clipboard?.writeText(code).then(() => { b.textContent = "copied"; setTimeout(() => { b.textContent = "copy"; }, 1200); })
      .catch(() => {});
  });
  if (!T) { $("#v-body").innerHTML = `<p class="empty">Open this page inside the app.</p>`; return; }
  T.event.listen("viewer-show", (ev) => show(ev.payload));
  T.core.invoke("viewer_pending").then(show).catch((e) => { $("#v-msg").textContent = String(e?.message || e); });
})();
