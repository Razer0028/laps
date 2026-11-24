//! Blackwall の中のブラウザ（2026-10-18 オーナー「Blackwall 用ブラウザとかもつけたら幅が広がっていいかも」→ 順番は
//! Files → 見るためのブラウザ（これ）→ エージェントが操作するブラウザ。「画面分離にも対応させたい」→「両方」）。
//!
//! - 同じ窓: メインの窓に子の webview（label "browser"）を重ね、画面（app.js の bz*）が右の欄の置き場所の位置と大きさを送る。
//!   ★子の webview は Tauri の unstable の機能（Window::add_child）。
//! - 別の窓: 「Pop out」で WebviewWindow（label "browser-win"）へ。窓を閉じるか「Dock」で、同じ URL のまま欄へ戻す。
//! ★開けるのは http と https だけ（file: や javascript: は通さない）。外のページは Tauri の IPC を呼べない
//!   （capabilities は app の中のページだけに与えている・remote の URL は書いていない）。

use serde_json::json;
use tauri::{AppHandle, Emitter, Manager, Url};

const CHILD: &str = "browser";
const POP: &str = "browser-win";

/// 最後に開いていた URL（別の窓が消えた後でも Dock で同じページへ戻すため）。
static LAST_URL: std::sync::Mutex<String> = std::sync::Mutex::new(String::new());

fn remember(url: &str) {
    if let Ok(mut g) = LAST_URL.lock() {
        *g = url.to_string();
    }
}

/// 雛形の置き場（`__NAME__`）を 1 回の走査で埋める。埋めた値の中に別の置き場の名前があっても、もう一度は置き換えない。
fn fill(tpl: &str, vals: &[(&str, String)]) -> String {
    let mut out = String::with_capacity(tpl.len() + 64);
    let mut rest = tpl;
    'outer: while let Some(ch) = rest.chars().next() {
        if ch == '_' {
            for (k, v) in vals {
                if rest.starts_with(k) {
                    out.push_str(v);
                    rest = &rest[k.len()..];
                    continue 'outer;
                }
            }
        }
        out.push(ch);
        rest = &rest[ch.len_utf8()..];
    }
    out
}

/// 打たれた文字を URL に。スキームが無ければ https（localhost と 127.* は http）、空白を含むか点が無ければ検索。
pub fn to_url(input: &str) -> Result<Url, String> {
    let t = input.trim();
    if t.is_empty() {
        return Err("Enter an address".into());
    }
    let s = if t.contains("://") {
        t.to_string()
    } else if t.starts_with("localhost") || t.starts_with("127.") || t.starts_with("[::1]") {
        format!("http://{t}")
    } else if !t.contains(' ') && t.split('/').next().map(|h| h.contains('.') || h.contains(':')).unwrap_or(false) {
        format!("https://{t}")
    } else {
        let mut u = Url::parse("https://duckduckgo.com/").unwrap();
        u.query_pairs_mut().append_pair("q", t);
        return Ok(u);
    };
    let u = Url::parse(&s).map_err(|e| format!("Not a valid address: {e}"))?;
    match u.scheme() {
        "http" | "https" => Ok(u),
        other => Err(format!("Only http and https pages can be opened (not {other}:)")),
    }
}

fn nav_event(app: &AppHandle, url: &Url) {
    remember(url.as_str());
    let _ = app.emit_to("main", "browser-nav", json!({ "url": url.as_str() }));
}

/// 今どちらで開いているか（別の窓が優先）。
fn current(app: &AppHandle) -> Option<tauri::Webview> {
    if let Some(w) = app.get_webview_window(POP) {
        return Some(w.as_ref().clone());
    }
    app.get_webview(CHILD)
}

/// 同じ窓の欄に出す（無ければ作る）。url を渡せばそこへ移る。返すのは今の URL（まだ何も開いていなければ空）。
#[tauri::command]
pub async fn browser_show(app: AppHandle, url: Option<String>, x: f64, y: f64, w: f64, h: f64) -> Result<String, String> {
    use tauri::{LogicalPosition, LogicalSize};
    if app.get_webview_window(POP).is_some() {
        return Ok(String::new()); // 別の窓で開いている間は欄に出さない
    }
    let (w, h) = (w.max(1.0), h.max(1.0));
    let target = match url.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        Some(s) => Some(to_url(s)?),
        None => None,
    };
    if let Some(v) = app.get_webview(CHILD) {
        let _ = v.set_position(LogicalPosition::new(x, y));
        let _ = v.set_size(LogicalSize::new(w, h));
        let _ = v.show();
        raise_child(app.clone());
        // 同じ URL なら移らない（Dock で戻したときに読み直さない）。
        if let Some(u) = target.filter(|u| v.url().map(|c| c != *u).unwrap_or(true)) {
            v.navigate(u).map_err(|e| format!("Can't open the page: {e}"))?;
        }
        return Ok(v.url().map(|u| u.to_string()).unwrap_or_default());
    }
    let Some(u) = target else { return Ok(String::new()) };
    let win = app.get_window("main").ok_or("The main window is missing")?;
    let a = app.clone();
    let builder = tauri::webview::WebviewBuilder::new(CHILD, tauri::WebviewUrl::External(u.clone()))
        .on_navigation(move |to| {
            nav_event(&a, to);
            true
        });
    win.add_child(builder, LogicalPosition::new(x, y), LogicalSize::new(w, h))
        .map_err(|e| format!("Can't open the browser: {e}"))?;
    raise_child(app.clone());
    Ok(u.to_string())
}

/// 見え方の診断（2026-10-18 オーナーの画面で、アドレスは入ったのにページが欄に出なかった）。
/// 子の webview の実際の位置と大きさ（物理の画素）・窓の倍率・送った位置（論理）を 1 行で返す。画面はこれを
/// 置き場所の一番下（子の裏）に小さく書く＝**読めたら子がそこを覆っていない**。
#[tauri::command]
pub async fn browser_diag(app: AppHandle, x: f64, y: f64, w: f64, h: f64) -> Result<String, String> {
    let Some(v) = app.get_webview(CHILD) else { return Ok("no child webview".into()) };
    let scale = app.get_window("main").and_then(|w| w.scale_factor().ok()).unwrap_or(0.0);
    let b = v.bounds().map_err(|e| e.to_string())?;
    let pos = b.position.to_physical::<i32>(scale);
    let size = b.size.to_physical::<u32>(scale);
    Ok(format!("child at {},{} {}x{} px · asked {:.0},{:.0} {:.0}x{:.0} (css) · scale {:.2}",
               pos.x, pos.y, size.width, size.height, x, y, w, h, scale))
}

/// 子の webview を一番上へ上げ、メインと子の窓に WS_CLIPSIBLINGS を付ける（兄弟の窓の上へ描かせない）。
/// ★wry は子もメインも WS_CHILD | WS_CLIPCHILDREN だけで作る＝重なった兄弟の窓の描き直しが互いの上に乗りうる。
///   メインの画面は背景が絶えず動いて描き直している（synapse）ので、子の上に描いて見えなくしていたと見ている。
#[cfg(windows)]
fn raise_child(app: AppHandle) {
    use windows61::Win32::Foundation::HWND;
    use windows61::Win32::UI::WindowsAndMessaging::{
        GetWindowLongPtrW, SetWindowLongPtrW, SetWindowPos, GWL_STYLE, HWND_TOP, SWP_FRAMECHANGED, SWP_NOACTIVATE,
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, WS_CLIPSIBLINGS,
    };
    fn clip(h: HWND, top: bool) {
        unsafe {
            let st = GetWindowLongPtrW(h, GWL_STYLE);
            if st & (WS_CLIPSIBLINGS.0 as isize) == 0 {
                SetWindowLongPtrW(h, GWL_STYLE, st | WS_CLIPSIBLINGS.0 as isize);
            }
            let flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED | if top { Default::default() } else { SWP_NOZORDER };
            let _ = SetWindowPos(h, Some(HWND_TOP), 0, 0, 0, 0, flags);
        }
    }
    for (label, top) in [("main", false), (CHILD, true)] {
        if let Some(v) = app.get_webview(label) {
            let _ = v.with_webview(move |pw| unsafe {
                let mut h = HWND::default();
                if pw.controller().ParentWindow(&mut h).is_ok() && !h.is_invalid() {
                    clip(h, top);
                }
            });
        }
    }
}

#[cfg(not(windows))]
fn raise_child(_app: AppHandle) {}

/// 欄から隠す（ページは残す＝戻れば同じ所）。
#[tauri::command]
pub async fn browser_hide(app: AppHandle) -> Result<(), String> {
    if let Some(v) = app.get_webview(CHILD) {
        let _ = v.hide();
    }
    Ok(())
}

/// 戻る・進む・読み直す・移る（action = back | forward | reload | go）。返すのは今の URL。
#[tauri::command]
pub async fn browser_nav(app: AppHandle, action: String, url: Option<String>) -> Result<String, String> {
    let v = current(&app).ok_or("No page is open")?;
    match action.as_str() {
        "back" => v.eval("history.back()").map_err(|e| e.to_string())?,
        "forward" => v.eval("history.forward()").map_err(|e| e.to_string())?,
        "reload" => v.reload().map_err(|e| e.to_string())?,
        "go" => v.navigate(to_url(url.as_deref().unwrap_or(""))?).map_err(|e| format!("Can't open the page: {e}"))?,
        other => return Err(format!("Unknown action: {other}")),
    }
    Ok(v.url().map(|u| u.to_string()).unwrap_or_default())
}

/// 別の窓へ切り離す（同じ URL で開き、欄の子は**隠すだけ**）。窓が消えたら browser-docked で欄へ戻す。
/// ★2026-10-18 オーナー「ブラウザを Dock にしたときにアプリ側で見えませんね」: 旧は子を close し、Dock で作り直していた。
///   閉じ終わる前の古い子を get_webview が返し、そこへ位置と表示を送って何も出なかった＋窓の CloseRequested の時点で
///   戻ったと知らせ、画面は「まだ別の窓がある」と判断して欄に出さなかった。子は残して隠し、窓が消えてから知らせる。
#[tauri::command]
pub async fn browser_popout(app: AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window(POP) {
        let _ = w.set_focus();
        return Ok(());
    }
    let child = app.get_webview(CHILD).ok_or("No page is open")?;
    let u = child.url().map_err(|e| e.to_string())?;
    remember(u.as_str());
    let _ = child.hide();
    let a = app.clone();
    let win = tauri::WebviewWindowBuilder::new(&app, POP, tauri::WebviewUrl::External(u.clone()))
        .title("L.A.P.S — Browser")
        .inner_size(1100.0, 800.0)
        .min_inner_size(480.0, 360.0)
        .on_navigation(move |to| {
            nav_event(&a, to);
            true
        })
        .build()
        .map_err(|e| format!("Can't open the window: {e}"))?;
    let a2 = app.clone();
    win.on_window_event(move |e| {
        // ★消え切ってから知らせる（CloseRequested の時点ではまだ窓があり、欄に出せなかった）。
        if let tauri::WindowEvent::Destroyed = e {
            let url = LAST_URL.lock().map(|g| g.clone()).unwrap_or_default();
            let _ = a2.emit_to("main", "browser-docked", json!({ "url": url }));
        }
    });
    let _ = app.emit_to("main", "browser-popped", json!({ "url": u.as_str() }));
    Ok(())
}

/// 別の窓を閉じて欄へ戻す（戻ったという知らせ browser-docked は、窓が消え切ったとき Destroyed が送る）。
#[tauri::command]
pub async fn browser_dock(app: AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window(POP) {
        if let Ok(u) = w.url() {
            remember(u.as_str());
        }
        let _ = w.destroy();
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// エージェントが操作する（Browser 道具・3 段目）
// ---------------------------------------------------------------------------
// 本体（serve.py）が app_call {op:"browser", args:{action,…}} を出す → sidecar.rs が agent_call を呼ぶ → 答えを app_reply で返す。
// ページの中身は WebView2 の ExecuteScript で JS を動かして受け取る（Tauri の eval は結果を返さない）。
// ★待ち受けの口（CDP のポート）は作らない（sidecar.rs の「待ち受けポートを作らない」と同じ決まり）。
// ★利用者に見えるように、頼まれたら右の欄の Browser を開かせる（browser-agent を画面へ）。
// ★2026-11-03: ページの読み書きは本体（oc/browsercdp.py）へ移した。ここは DevTools の命令と事象を運ぶだけ（cdp・cdp_events・go・show）。
//   下の READ_JS などの JS は、新しいアプリに古い本体が繋いだときのためだけに残してある。

const READ_JS: &str = r#"(() => {
  const max = __MAX__;
  const vis = (e) => !!(e.offsetParent || e.getClientRects().length);
  const t = (document.body ? document.body.innerText : "") || "";
  const links = [...document.querySelectorAll("a[href]")].filter(vis).slice(0, 80)
    .map(a => ({ text: (a.innerText || a.title || a.getAttribute("aria-label") || "").trim().slice(0, 80), href: a.href }));
  const fields = [...document.querySelectorAll("input,textarea,select,button")].filter(vis).slice(0, 60).map(e => ({
    tag: e.tagName.toLowerCase(), type: e.type || "", name: e.name || "", id: e.id || "", placeholder: e.placeholder || "",
    label: ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute("aria-label") ||
            (e.tagName === "BUTTON" ? e.innerText : "") || (e.type === "submit" ? e.value : "") || "").trim().slice(0, 60) }));
  return { url: location.href, title: document.title, text: t.slice(0, max), truncated: t.length > max, links, fields };
})()"#;

// ★目（2026-10-19 オーナー「Blackwall の目を強化したい。視覚的にブラウザ操作ができるように」）: 見えている押せる物・入力欄に
//   番号の札（赤）を描き、data-bw-ref に番号を付ける。撮った後に札だけ外す（番号は残す＝click/type の ref で指せる）。
const MARK_JS: &str = r#"(() => {
  const old = document.getElementById("__bw_marks"); if (old) old.remove();
  document.querySelectorAll("[data-bw-ref]").forEach(e => e.removeAttribute("data-bw-ref"));
  const W = innerWidth, H = innerHeight;
  const q = "a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],[role=tab],[role=checkbox],[role=menuitem],[onclick],[contenteditable=true]";
  const box = document.createElement("div"); box.id = "__bw_marks";
  box.style.cssText = "position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none";
  const marks = []; let n = 0;
  for (const e of document.querySelectorAll(q)) {
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.right < 0 || r.top > H || r.left > W) continue;
    const st = getComputedStyle(e); if (st.visibility === "hidden" || st.display === "none" || st.opacity === "0") continue;
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), W - 1), cy = Math.min(Math.max(r.top + r.height / 2, 0), H - 1);
    const top = document.elementFromPoint(cx, cy); if (top && top !== e && !e.contains(top) && !top.contains(e)) continue;
    n += 1; e.setAttribute("data-bw-ref", String(n));
    const tag = e.tagName.toLowerCase();
    const kind = tag === "a" ? "link" : tag === "input" ? ("input " + (e.type || "text")) : tag === "textarea" ? "textarea" : tag === "select" ? "select" : (e.getAttribute("role") || tag);
    const label = ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute("aria-label") || e.innerText || e.value || e.placeholder || e.title || e.name || "").trim().replace(/\s+/g, " ").slice(0, 60);
    const t = document.createElement("div");
    t.textContent = String(n);
    t.style.cssText = "position:fixed;left:" + Math.max(r.left - 2, 0) + "px;top:" + Math.max(r.top - 14, 0) + "px;background:#e11d48;color:#fff;font:bold 12px/14px Arial,sans-serif;padding:0 3px;border-radius:3px";
    const o = document.createElement("div");
    o.style.cssText = "position:fixed;left:" + r.left + "px;top:" + r.top + "px;width:" + r.width + "px;height:" + r.height + "px;outline:2px solid #e11d48";
    box.appendChild(o); box.appendChild(t);
    marks.push({ ref: n, kind, label, x: Math.round(cx * 1000 / W), y: Math.round(cy * 1000 / H) });
    if (n >= 150) break;
  }
  document.documentElement.appendChild(box);
  return { url: location.href, title: document.title, marks, more_below: scrollY + H < document.documentElement.scrollHeight - 8, w: W, h: H };
})()"#;

const UNMARK_JS: &str = r#"(() => { const b = document.getElementById("__bw_marks"); if (b) b.remove(); return true; })()"#;

const SCROLL_JS: &str = r#"((dir) => { scrollBy(0, (dir === "up" ? -1 : 1) * Math.round(innerHeight * 0.85)); return { ok: true }; })(__DIR__)"#;

const KEY_JS: &str = r#"((key) => {
  const el = document.activeElement || document.body;
  const codes = { Enter: 13, Tab: 9, Escape: 27, ArrowDown: 40, ArrowUp: 38, ArrowLeft: 37, ArrowRight: 39, Backspace: 8, Space: 32 };
  const o = { key, code: key, keyCode: codes[key] || 0, which: codes[key] || 0, bubbles: true, cancelable: true };
  const go = el.dispatchEvent(new KeyboardEvent("keydown", o));
  if (go && key === "Enter" && el.form && el.tagName === "INPUT") { if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit(); }
  if (go && key === "Tab") {
    const f = [...document.querySelectorAll("a[href],button,input,textarea,select,[tabindex]")].filter(e => !e.disabled && e.tabIndex >= 0 && (e.offsetParent || e.getClientRects().length));
    const i = f.indexOf(el); if (f.length) f[(i + 1) % f.length].focus();
  }
  el.dispatchEvent(new KeyboardEvent("keyup", o));
  return { ok: true };
})(__KEY__)"#;

const CLICK_JS: &str = r#"((sel, txt, ref, px, py) => {
  let el = null;
  if (ref !== null && ref !== undefined && ref !== "") {
    el = document.querySelector('[data-bw-ref="' + String(ref).replace(/[^0-9]/g, "") + '"]');
    if (!el) return { ok: false, error: "no element has number " + ref + " (look again; numbers change after the page changes)" };
  }
  if (!el && px !== null && py !== null) {
    el = document.elementFromPoint(px * innerWidth / 1000, py * innerHeight / 1000);
    if (!el) return { ok: false, error: "nothing is at that position" };
  }
  if (!el && sel) { try { el = document.querySelector(sel); } catch (e) { return { ok: false, error: "bad selector: " + e.message }; } }
  if (!el && txt) {
    const want = txt.trim().toLowerCase();
    const lab = (e) => (e.innerText || e.value || e.getAttribute("aria-label") || e.title || "").trim().toLowerCase();
    const c = [...document.querySelectorAll("a,button,input[type=submit],input[type=button],[role=button],[role=link],summary,label")]
      .filter(e => !!(e.offsetParent || e.getClientRects().length));
    el = c.find(e => lab(e) === want) || c.find(e => lab(e).includes(want));
  }
  if (!el) return { ok: false, error: "no element matched" + (sel ? " selector " + sel : "") + (txt ? " text \"" + txt + "\"" : "") };
  el.scrollIntoView({ block: "center" });
  el.click();
  return { ok: true, clicked: (el.innerText || el.value || el.getAttribute("aria-label") || el.tagName).trim().slice(0, 80) };
})(__SEL__, __TXT__, __REF__, __PX__, __PY__)"#;

const TYPE_JS: &str = r#"((sel, val, submit, ref) => {
  let el = null;
  if (ref !== null && ref !== undefined && ref !== "") {
    el = document.querySelector('[data-bw-ref="' + String(ref).replace(/[^0-9]/g, "") + '"]');
    if (!el) return { ok: false, error: "no element has number " + ref + " (look again; numbers change after the page changes)" };
  }
  try { if (!el) el = sel ? document.querySelector(sel) : document.activeElement; } catch (e) { return { ok: false, error: "bad selector: " + e.message }; }
  if (!el || !("value" in el)) return { ok: false, error: "no input field matched" + (sel ? " selector " + sel : " (give a selector)") };
  el.focus();
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  const set = Object.getOwnPropertyDescriptor(proto, "value");
  if (set && set.set) set.set.call(el, val); else el.value = val;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  if (submit) {
    const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true };
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    if (el.form) { if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit(); }
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
  }
  return { ok: true };
})(__SEL__, __VAL__, __SUBMIT__, __REF__)"#;

const INFO_JS: &str = r#"({ url: location.href, title: document.title, ready: document.readyState })"#;

/// 本体の頼みを行う（sidecar.rs から）。答えは必ず {ok, …}（黙ると本体が時間切れまで待つ）。
pub async fn agent_call(app: AppHandle, op: String, args: serde_json::Value) -> serde_json::Value {
    // ★Editor 道具（2026-11-17）: エディターでファイルを開く・この会話の差分を見せる。画面（explorer.js）が editor-agent を受けて開く。
    if op == "editor" {
        let path = args.get("path").and_then(|v| v.as_str()).unwrap_or("");
        if path.is_empty() {
            return json!({ "ok": false, "error": "path is required" });
        }
        return match app.emit_to("main", "editor-agent", args.clone()) {
            Ok(()) => json!({ "ok": true }),
            Err(e) => json!({ "ok": false, "error": format!("the editor is not available: {e}") }),
        };
    }
    // ★ふだんの Chrome（2026-11-18）: 拡張機能へ中継する（chromebridge.rs）。
    if op == "chrome" {
        return crate::chromebridge::call(&args).await;
    }
    if op != "browser" {
        return json!({ "ok": false, "error": format!("this app does not handle '{op}'") });
    }
    match agent_browser(&app, &args).await {
        Ok(v) => v,
        Err(e) => json!({ "ok": false, "error": e }),
    }
}

async fn agent_browser(app: &AppHandle, args: &serde_json::Value) -> Result<serde_json::Value, String> {
    let s = |k: &str| args.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let action = s("action");
    let url = s("url");
    // 利用者に見せる: 右の欄の Browser を開く（まだページが無ければ画面がこの URL で作る）。
    // ★命令 1 つごとに知らせない（1 操作で何十回も呼ぶ）。本体が操作の頭で show か go を 1 回呼ぶ。
    if !action.starts_with("cdp") {
        let shown = if action == "go" { "open" } else { action.as_str() };
        let _ = app.emit_to("main", "browser-agent", json!({ "action": shown, "url": url }));
    }
    match action.as_str() {
        "show" => Ok(json!({ "ok": true })),
        // 移るだけ（読み込みは本体が待つ。ここで待つと alert の出るページで固まる）。
        "go" => {
            let u = to_url(&url)?;
            match current(app) {
                Some(v) => v.navigate(u).map_err(|e| format!("Can't open the page: {e}"))?,
                None => {
                    wait_webview(app, 10).await.ok_or(
                        "The browser panel did not open. Ask the user to open the Browser tab in the side panel.")?;
                }
            }
            Ok(json!({ "ok": true }))
        }
        "cdp" => {
            let v = current(app).ok_or("No page is open. Use action=open first.")?;
            let method = s("method");
            // ★開く先の決まり（http と https だけ）は go / open だけでなく、生の命令でも守る（2026-11-13 のレビュー: Page.navigate に
            //   file:/// を渡せば手元のファイルを開けた。いまの呼び手は Python 側で同じ決まりを通すが、この口だけ決まりが無かった）。
            if method == "Page.navigate" || method == "Target.createTarget" {
                let to = args.get("params").and_then(|p| p.get("url")).and_then(|u| u.as_str()).unwrap_or("");
                let low = to.trim().to_ascii_lowercase();
                if !(low.is_empty() || low.starts_with("http://") || low.starts_with("https://") || low.starts_with("about:")) {
                    return Err("Only http and https pages can be opened".into());
                }
            }
            let params = args.get("params").map(|p| p.to_string()).unwrap_or_else(|| "{}".into());
            let secs = args.get("timeout").and_then(|x| x.as_f64()).unwrap_or(10.0).clamp(1.0, 60.0);
            let (res, view) = cdp_call(&v, &method, &params, &s("session"), secs).await?;
            Ok(json!({ "ok": true, "result": res, "view": view }))
        }
        "cdp_events" => {
            let events: Vec<serde_json::Value> = CDP_QUEUE.lock().map(|mut q| q.drain(..).collect()).unwrap_or_default();
            Ok(json!({ "ok": true, "events": events }))
        }
        "open" => {
            let u = to_url(&url)?;
            let v = match current(app) {
                Some(v) => {
                    v.navigate(u).map_err(|e| format!("Can't open the page: {e}"))?;
                    v
                }
                None => wait_webview(app, 10).await.ok_or(
                    "The browser panel did not open. Ask the user to open the Browser tab in the side panel.")?,
            };
            wait_loaded(&v).await;
            info(&v).await
        }
        "read" => {
            let v = current(app).ok_or("No page is open. Use action=open first.")?;
            let max = args.get("max_chars").and_then(|x| x.as_u64()).unwrap_or(12000).clamp(1000, 60000);
            let mut r = exec_js(&v, READ_JS.replace("__MAX__", &max.to_string())).await?;
            r["ok"] = json!(true);
            Ok(r)
        }
        "look" => {
            // 札を描く → 撮る → 札を外す（番号は残す）。撮るのは WebView2 の中の Page.captureScreenshot（待ち受けの口は作らない）。
            let v = current(app).ok_or("No page is open. Use action=open first.")?;
            let mut r = exec_js(&v, MARK_JS.to_string()).await?;
            tokio::time::sleep(std::time::Duration::from_millis(120)).await;
            let shot = cdp(&v, "Page.captureScreenshot", r#"{"format":"png"}"#).await;
            let _ = exec_js(&v, UNMARK_JS.to_string()).await;
            let shot = shot?;
            let png = shot.get("data").and_then(|d| d.as_str()).unwrap_or("").to_string();
            if png.is_empty() {
                return Err("the page picture came back empty".into());
            }
            r["png"] = json!(png);
            r["ok"] = json!(true);
            Ok(r)
        }
        "scroll" | "key" => {
            let v = current(app).ok_or("No page is open. Use action=open first.")?;
            let js = if action == "scroll" {
                SCROLL_JS.replace("__DIR__", &json!(if s("direction") == "up" { "up" } else { "down" }).to_string())
            } else {
                KEY_JS.replace("__KEY__", &json!(s("key")).to_string())
            };
            exec_js(&v, js).await?;
            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
            info(&v).await
        }
        "click" | "type" => {
            let v = current(app).ok_or("No page is open. Use action=open first.")?;
            let num = |k: &str| args.get(k).and_then(|x| x.as_f64()).map(|f| json!(f).to_string()).unwrap_or_else(|| "null".into());
            // ★置き場は 1 回の走査で埋める（fill）。続けて replace すると、打つ文字や selector の中の `__REF__` などが
            //   後の置き換えで書き換わる（2026-11-13: テンプレートのコードをフォームに打つと数字に化けるか、文が壊れた）。
            let js = if action == "click" {
                fill(CLICK_JS, &[("__SEL__", json!(s("selector")).to_string()), ("__TXT__", json!(s("text")).to_string()),
                                 ("__REF__", num("ref")), ("__PX__", num("x")), ("__PY__", num("y"))])
            } else {
                let submit = args.get("submit").and_then(|x| x.as_bool()).unwrap_or(false);
                fill(TYPE_JS, &[("__SEL__", json!(s("selector")).to_string()), ("__VAL__", json!(s("text")).to_string()),
                                ("__SUBMIT__", (if submit { "true" } else { "false" }).to_string()), ("__REF__", num("ref"))])
            };
            let r = exec_js(&v, js).await?;
            if r.get("ok").and_then(|x| x.as_bool()) != Some(true) {
                return Ok(r);
            }
            wait_loaded(&v).await;
            let mut i = info(&v).await?;
            if let Some(c) = r.get("clicked") {
                i["clicked"] = c.clone();
            }
            Ok(i)
        }
        "back" | "forward" | "reload" => {
            let v = current(app).ok_or("No page is open. Use action=open first.")?;
            match action.as_str() {
                "back" => v.eval("history.back()").map_err(|e| e.to_string())?,
                "forward" => v.eval("history.forward()").map_err(|e| e.to_string())?,
                _ => v.reload().map_err(|e| e.to_string())?,
            }
            wait_loaded(&v).await;
            info(&v).await
        }
        other => Err(format!("Unknown browser action: {other}")),
    }
}

async fn info(v: &tauri::Webview) -> Result<serde_json::Value, String> {
    let mut r = exec_js(v, INFO_JS.to_string()).await?;
    r["ok"] = json!(true);
    Ok(r)
}

/// 画面が子の webview を作るのを待つ（最大 secs 秒）。
async fn wait_webview(app: &AppHandle, secs: u64) -> Option<tauri::Webview> {
    for _ in 0..secs * 4 {
        if let Some(v) = current(app) {
            return Some(v);
        }
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
    }
    None
}

/// 読み込みが終わるのを待つ（移った直後は古いページが complete を返すので、少し置いてから見る・最大 15 秒）。
async fn wait_loaded(v: &tauri::Webview) {
    tokio::time::sleep(std::time::Duration::from_millis(600)).await;
    for _ in 0..30 {
        if let Ok(r) = exec_js(v, "document.readyState".to_string()).await {
            if r.as_str() == Some("complete") {
                return;
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
}

/// ページで JS を動かして値を受け取る（WebView2 の ExecuteScript・答えは JSON）。
#[cfg(windows)]
async fn exec_js(v: &tauri::Webview, js: String) -> Result<serde_json::Value, String> {
    let (tx, rx) = tokio::sync::oneshot::channel::<Result<String, String>>();
    let tx = std::sync::Arc::new(std::sync::Mutex::new(Some(tx)));
    let send = move |r: Result<String, String>| {
        if let Some(t) = tx.lock().ok().and_then(|mut g| g.take()) {
            let _ = t.send(r);
        }
    };
    let send2 = send.clone();
    v.with_webview(move |pw| {
        use webview2_com::ExecuteScriptCompletedHandler;
        let res = unsafe {
            pw.controller().CoreWebView2().and_then(|core| {
                let js_h = windows_core::HSTRING::from(js.as_str());
                let cb = send2.clone();
                core.ExecuteScript(
                    &js_h,
                    &ExecuteScriptCompletedHandler::create(Box::new(move |err, res| {
                        cb(err.map(|_| res).map_err(|e| e.to_string()));
                        Ok(())
                    })),
                )
            })
        };
        if let Err(e) = res {
            send2(Err(e.to_string()));
        }
    })
    .map_err(|e| e.to_string())?;
    let got = tokio::time::timeout(std::time::Duration::from_secs(20), rx)
        .await
        .map_err(|_| "the page did not answer in time".to_string())?
        .map_err(|_| "the page gave no answer".to_string())??;
    let _ = &send;
    serde_json::from_str(&got).map_err(|e| format!("unreadable answer from the page: {e}"))
}

#[cfg(not(windows))]
async fn exec_js(_v: &tauri::Webview, _js: String) -> Result<serde_json::Value, String> {
    Err("the in-app browser runs only on Windows".into())
}

/// 届いた DevTools の事象（本体が cdp_events で取りに来る。溢れたら古い方を捨てる）。
static CDP_QUEUE: std::sync::Mutex<std::collections::VecDeque<serde_json::Value>> =
    std::sync::Mutex::new(std::collections::VecDeque::new());
/// どの webview に事象の受け口を付けたか（label → 本体の id。作り直されたら付け直す）。
#[cfg(windows)]
static CDP_HOOKED: std::sync::Mutex<Option<std::collections::HashMap<String, usize>>> = std::sync::Mutex::new(None);
/// 本体の EVENTS（oc/browsercdp.py）と揃える。
#[cfg(windows)]
const CDP_EVENTS: &[&str] = &[
    "Page.javascriptDialogOpening", "Page.javascriptDialogClosed", "Page.fileChooserOpened",
    "Runtime.exceptionThrown", "Runtime.consoleAPICalled", "Runtime.executionContextCreated",
    "Runtime.executionContextDestroyed", "Runtime.executionContextsCleared", "Log.entryAdded",
    "Target.attachedToTarget", "Target.detachedFromTarget",
];

/// 事象の受け口を 1 度だけ付ける。返すのはこの webview の名札（変わったら本体は enable し直す）。
#[cfg(windows)]
fn cdp_hook(core: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2, label: &str) -> String {
    use webview2_com::DevToolsProtocolEventReceivedEventHandler;
    use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2DevToolsProtocolEventReceivedEventArgs2;
    use windows_core::Interface;
    // ★COM の同一性は IUnknown で見る。
    let id = core.cast::<windows_core::IUnknown>().map(|u| u.as_raw() as usize).unwrap_or(0);
    let view = format!("{label}:{id:x}");
    let Ok(mut guard) = CDP_HOOKED.lock() else { return view };
    let map = guard.get_or_insert_with(Default::default);
    if map.get(label) == Some(&id) {
        return view;
    }
    map.insert(label.to_string(), id);
    drop(guard);
    for name in CDP_EVENTS {
        let method = name.to_string();
        let handler = DevToolsProtocolEventReceivedEventHandler::create(Box::new(move |_, args| {
            let Some(args) = args else { return Ok(()) };
            let mut p = windows_core::PWSTR::null();
            unsafe { args.ParameterObjectAsJson(&mut p)? };
            let params = webview2_com::take_pwstr(p);
            let session = args
                .cast::<ICoreWebView2DevToolsProtocolEventReceivedEventArgs2>()
                .ok()
                .and_then(|a| {
                    let mut q = windows_core::PWSTR::null();
                    unsafe { a.SessionId(&mut q).ok()? };
                    Some(webview2_com::take_pwstr(q))
                })
                .unwrap_or_default();
            if let Ok(mut q) = CDP_QUEUE.lock() {
                if q.len() >= 400 {
                    q.pop_front();
                }
                let params: serde_json::Value = serde_json::from_str(&params).unwrap_or(serde_json::Value::Null);
                q.push_back(json!({ "method": method, "params": params, "session": session }));
            }
            Ok(())
        }));
        unsafe {
            let n = windows_core::HSTRING::from(*name);
            if let Ok(rcv) = core.GetDevToolsProtocolEventReceiver(&n) {
                let mut token = 0i64;
                let _ = rcv.add_DevToolsProtocolEventReceived(&handler, &mut token);
            }
        }
    }
    view
}

/// WebView2 の中で DevTools の命令を 1 つ呼ぶ（CallDevToolsProtocolMethod・待ち受けの口は作らない）。
/// session は別プロセスの iframe の線（空なら本体）。答えは (JSON, webview の名札)。
#[cfg(windows)]
async fn cdp_call(v: &tauri::Webview, method: &str, params: &str, session: &str, secs: f64) -> Result<(serde_json::Value, String), String> {
    let (tx, rx) = tokio::sync::oneshot::channel::<Result<String, String>>();
    let tx = std::sync::Arc::new(std::sync::Mutex::new(Some(tx)));
    let send = move |r: Result<String, String>| {
        if let Some(t) = tx.lock().ok().and_then(|mut g| g.take()) {
            let _ = t.send(r);
        }
    };
    let send2 = send.clone();
    let (method, params, session) = (method.to_string(), params.to_string(), session.to_string());
    let label = v.label().to_string();
    let view = std::sync::Arc::new(std::sync::Mutex::new(String::new()));
    let view2 = view.clone();
    v.with_webview(move |pw| {
        use webview2_com::CallDevToolsProtocolMethodCompletedHandler;
        use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2_11;
        use windows_core::Interface;
        let res = unsafe {
            pw.controller().CoreWebView2().and_then(|core| {
                if let Ok(mut g) = view2.lock() {
                    *g = cdp_hook(&core, &label);
                }
                let m = windows_core::HSTRING::from(method.as_str());
                let p = windows_core::HSTRING::from(params.as_str());
                let cb = send2.clone();
                // ★命令の失敗は err に本文は無く、本文（res）に {"code":…,"message":…} が入る。本文を捨てない。
                let handler = CallDevToolsProtocolMethodCompletedHandler::create(Box::new(move |err, res| {
                    cb(match err {
                        Ok(_) => Ok(res),
                        Err(e) => Err(if res.trim().is_empty() { e.to_string() } else { res }),
                    });
                    Ok(())
                }));
                if session.is_empty() {
                    core.CallDevToolsProtocolMethod(&m, &p, &handler)
                } else {
                    let s = windows_core::HSTRING::from(session.as_str());
                    core.cast::<ICoreWebView2_11>()?.CallDevToolsProtocolMethodForSession(&s, &m, &p, &handler)
                }
            })
        };
        if let Err(e) = res {
            send2(Err(e.to_string()));
        }
    })
    .map_err(|e| e.to_string())?;
    let got = tokio::time::timeout(std::time::Duration::from_secs_f64(secs), rx)
        .await
        .map_err(|_| "the page command timed out".to_string())?
        .map_err(|_| "the page gave no answer".to_string())??;
    let _ = &send;
    let value = serde_json::from_str(&got).map_err(|e| format!("unreadable answer from the page: {e}"))?;
    let name = view.lock().map(|g| g.clone()).unwrap_or_default();
    Ok((value, name))
}

#[cfg(not(windows))]
async fn cdp_call(_v: &tauri::Webview, _method: &str, _params: &str, _session: &str, _secs: f64) -> Result<(serde_json::Value, String), String> {
    Err("the in-app browser runs only on Windows".into())
}

/// 古い本体のための写真の撮影（look）。
async fn cdp(v: &tauri::Webview, method: &str, params: &str) -> Result<serde_json::Value, String> {
    cdp_call(v, method, params, "", 30.0).await.map(|(r, _)| r)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn addresses() {
        assert_eq!(to_url("example.com").unwrap().as_str(), "https://example.com/");
        assert_eq!(to_url("localhost:5173/app").unwrap().as_str(), "http://localhost:5173/app");
        assert_eq!(to_url("http://relay.example:18400/").unwrap().as_str(), "http://relay.example:18400/");
        assert!(to_url("rust ownership rules").unwrap().as_str().starts_with("https://duckduckgo.com/?q=rust+ownership+rules"));
        assert!(to_url("file:///C:/Windows/win.ini").is_err());
        assert!(to_url("javascript:alert(1)").is_err());
        assert!(to_url("  ").is_err());
    }
}
