#!/usr/bin/env python3
"""Audit a web UI without looking at it.

Opens the page in a real browser at desktop and mobile widths and reports:
  contrast   text below WCAG AA (4.5:1, or 3:1 for large text)
  tiny-text  text smaller than 12 px
  overflow   horizontal scrolling and the elements that cause it
  target     interactive elements smaller than 24x24 px
  name       buttons/links/inputs without an accessible name, images without alt
  headings   skipped heading levels
  js-error   console errors and uncaught exceptions
  stats      distinct font sizes, font families, text colors, spacing values off the 4 px grid
Screenshots (full page) are saved next to report.json for the user.

Usage:  python ui_audit.py <url | path/to/page.html> [--out DIR] [--widths 1440,390] [--wait MS]
Setup:  pip install playwright      (uses the installed Edge or Chrome; nothing else to download)
        If neither browser exists:  python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

AUDIT_JS = r"""
() => {
  const out = {findings: [], stats: {}};
  const add = (kind, el, detail) => { if (out.findings.length < 400) out.findings.push({kind, where: sel(el), ...detail}); };
  const sel = el => {
    if (!el || el.nodeType !== 1) return '';
    const parts = [];
    for (let e = el, i = 0; e && e.nodeType === 1 && i < 4; e = e.parentElement, i++) {
      let s = e.tagName.toLowerCase();
      if (e.id) { parts.unshift(s + '#' + e.id); break; }
      const cls = [...e.classList].slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      parts.unshift(s);
    }
    return parts.join(' > ');
  };
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.05;
  };
  const parse = c => {
    if (!c) return null;
    let m = c.match(/^rgba?\(([^)]+)\)/);
    if (m) {
      const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(x => x.endsWith('%') ? parseFloat(x) / 100 : parseFloat(x));
      return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
    }
    m = c.match(/^color\(srgb\s+([^)]+)\)/);
    if (m) {
      const p = m[1].split(/[\s\/]+/).filter(Boolean).map(parseFloat);
      return {r: p[0] * 255, g: p[1] * 255, b: p[2] * 255, a: p.length > 3 ? p[3] : 1};
    }
    return null;   // oklch(), lab() etc.: unknown
  };
  const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = c => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const over = (top, bot) => ({r: top.r * top.a + bot.r * (1 - top.a), g: top.g * top.a + bot.g * (1 - top.a),
                                b: top.b * top.a + bot.b * (1 - top.a), a: 1});
  const dark = matchMedia('(prefers-color-scheme: dark)').matches &&
               /dark/.test(getComputedStyle(document.documentElement).colorScheme || '');
  const canvas = dark ? {r: 18, g: 18, b: 18, a: 1} : {r: 255, g: 255, b: 255, a: 1};
  const background = el => {
    const layers = [];
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;   // image/gradient: unknown
      const c = parse(cs.backgroundColor);
      if (c === null && cs.backgroundColor && !/transparent|rgba\(0, 0, 0, 0\)/.test(cs.backgroundColor)) return null;
      if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; }
    }
    let acc = canvas;
    for (let i = layers.length - 1; i >= 0; i--) acc = over(layers[i], acc);
    return acc;
  };

  // Text: contrast, size, typography stats
  const sizes = {}, families = {}, colors = {};
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let unknownBg = 0;
  while (walker.nextNode()) {
    const t = walker.currentNode;
    if (!t.textContent.trim()) continue;
    const el = t.parentElement;
    if (!el || seen.has(el) || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(el.tagName)) continue;
    seen.add(el);
    if (!visible(el)) continue;
    const cs = getComputedStyle(el);
    const size = parseFloat(cs.fontSize), weight = parseInt(cs.fontWeight, 10) || 400;
    sizes[size] = (sizes[size] || 0) + 1;
    const fam = (cs.fontFamily || '').split(',')[0].trim().replace(/["']/g, '');
    families[fam] = (families[fam] || 0) + 1;
    colors[cs.color] = (colors[cs.color] || 0) + 1;
    const sample = t.textContent.trim().slice(0, 40);
    if (size < 12 && !['SUP', 'SUB'].includes(el.tagName)) add('tiny-text', el, {text: sample, px: size});
    const disabled = el.closest('[disabled],[aria-disabled="true"]');
    if (disabled) continue;
    const bg = background(el), fg0 = parse(cs.color);
    if (!bg || !fg0) { unknownBg++; continue; }
    const fg = fg0.a < 1 ? over(fg0, bg) : fg0;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3 : 4.5, r = ratio(fg, bg);
    if (r < need) add('contrast', el, {text: sample, ratio: +r.toFixed(2), need, color: cs.color});
  }

  // Overflow: find the elements that cause horizontal scrolling (hide one, see if the scroll goes away)
  const vw = document.documentElement.clientWidth;
  const scrolls = () => document.documentElement.scrollWidth > vw + 1;
  if (scrolls()) {
    out.stats.page_scroll_width = document.documentElement.scrollWidth;
    const clipped = el => {
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement)
        if (getComputedStyle(p).overflowX !== 'visible') return true;
      return false;
    };
    const fixesIfHidden = el => {
      const v = el.style.getPropertyValue('display'), pr = el.style.getPropertyPriority('display');
      el.style.setProperty('display', 'none', 'important');
      const ok = !scrolls();
      if (v) el.style.setProperty('display', v, pr); else el.style.removeProperty('display');
      return ok;
    };
    const culprits = [];
    const search = root => {
      for (const el of root.children) {
        if (culprits.length >= 10) return;
        if (['SCRIPT', 'STYLE'].includes(el.tagName) || !visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.right > vw + 1 && !clipped(el) && fixesIfHidden(el)) {
          const before = culprits.length;
          search(el);
          if (culprits.length === before) culprits.push(el);
        } else {
          search(el);
        }
      }
    };
    search(document.body);
    if (culprits.length) {
      for (const el of culprits) add('overflow', el, {right: Math.round(el.getBoundingClientRect().right), viewport: vw,
                                                      width: Math.round(el.getBoundingClientRect().width)});
    } else {
      add('overflow', document.body, {page_scroll_width: document.documentElement.scrollWidth, viewport: vw,
                                      note: 'several elements overflow together; no single one is the cause'});
    }
  }

  // Interactive elements: size and accessible name
  const interactive = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=switch]';
  const nameOf = el => {
    if (el.getAttribute('aria-label')?.trim()) return true;
    const lb = el.getAttribute('aria-labelledby');
    if (lb && lb.split(/\s+/).some(id => document.getElementById(id)?.textContent.trim())) return true;
    if (el.title?.trim()) return true;
    if (el.labels && [...el.labels].some(l => l.textContent.trim())) return true;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(type) && el.value) return true;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) return false;
    if (el.textContent.trim()) return true;
    return [...el.querySelectorAll('img[alt],svg[aria-label],[aria-label]')].some(x => (x.getAttribute('alt') || x.getAttribute('aria-label') || '').trim());
  };
  for (const el of document.querySelectorAll(interactive)) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    // WCAG 2.5.8 exempts targets inside a sentence (inline links, citation chips in running text)
    const inlineLink = /^inline/.test(getComputedStyle(el).display) && el.parentElement &&
                       /^(P|LI|SPAN|TD|DD|LABEL|BLOCKQUOTE)$/.test(el.parentElement.tagName) &&
                       [...el.parentElement.childNodes].some(n => n !== el && n.nodeType === 3 && n.textContent.trim());
    const box = ['checkbox', 'radio'].includes((el.getAttribute('type') || '').toLowerCase()) && el.labels?.length;
    if (!inlineLink && !box && Math.min(r.width, r.height) < 24)
      add('target', el, {w: Math.round(r.width), h: Math.round(r.height)});
    if (!nameOf(el)) add('name', el, {role: el.getAttribute('role') || el.tagName.toLowerCase()});
  }
  for (const img of document.querySelectorAll('img')) if (visible(img) && !img.hasAttribute('alt')) add('name', img, {role: 'img (no alt)'});

  // Headings
  let last = 0;
  for (const h of document.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
    if (!visible(h)) continue;
    const lv = +h.tagName[1];
    if (last && lv > last + 1) add('headings', h, {from: 'h' + last, to: 'h' + lv});
    last = lv;
  }

  // Spacing values off the 4 px grid
  const off = {};
  let spacingTotal = 0;
  for (const el of document.body.querySelectorAll('*')) {
    if (!visible(el)) continue;
    const cs = getComputedStyle(el);
    for (const p of ['marginTop', 'marginRight', 'marginBottom', 'marginLeft', 'paddingTop', 'paddingRight',
                     'paddingBottom', 'paddingLeft', 'rowGap', 'columnGap']) {
      const v = parseFloat(cs[p]);
      if (!v || v < 0) continue;
      spacingTotal++;
      if (v > 3 && Math.abs(v / 4 - Math.round(v / 4)) > 0.01) off[Math.round(v * 10) / 10] = (off[Math.round(v * 10) / 10] || 0) + 1;
    }
  }
  const top = (o, n) => Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, n);
  out.stats.font_sizes = top(sizes, 20).map(([k, v]) => `${k}px x${v}`);
  out.stats.font_families = top(families, 10).map(([k, v]) => `${k} x${v}`);
  out.stats.text_colors = Object.keys(colors).length;
  out.stats.spacing_values = spacingTotal;
  out.stats.spacing_off_grid = top(off, 10).map(([k, v]) => `${k}px x${v}`);
  out.stats.contrast_unknown = unknownBg;
  out.stats.title = document.title;
  return out;
}
"""

LIMIT_PER_KIND = 15


def _launch(p):
    last = None
    for kw in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
        try:
            return p.chromium.launch(headless=True, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
    sys.exit(f"Could not start a browser ({last}).\n"
             "Install Microsoft Edge or Google Chrome, or run: python -m playwright install chromium")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", help="URL or path to an HTML file")
    ap.add_argument("--out", default="ui-audit", help="folder for report.json and screenshots")
    ap.add_argument("--widths", default="1440,390", help="viewport widths to test, comma-separated")
    ap.add_argument("--wait", type=int, default=600, help="extra wait after load, in ms")
    ap.add_argument("--dark", action="store_true", help="emulate prefers-color-scheme: dark")
    a = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run: pip install playwright")

    target = a.target
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target):
        target = pathlib.Path(target).resolve().as_uri()
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"target": target, "runs": []}

    with sync_playwright() as p:
        browser = _launch(p)
        for w in [int(x) for x in a.widths.split(",") if x.strip()]:
            mobile = w < 768
            ctx = browser.new_context(viewport={"width": w, "height": 844 if mobile else 900},
                                      is_mobile=mobile, has_touch=mobile,
                                      color_scheme="dark" if a.dark else "light")
            page = ctx.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text[:300]) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)[:300]))
            try:
                page.goto(target, wait_until="load", timeout=45000)
            except Exception as e:  # noqa: BLE001
                errors.append(f"navigation: {e}")
            page.wait_for_timeout(a.wait)
            res = page.evaluate(AUDIT_JS)
            for m in errors:
                res["findings"].append({"kind": "js-error", "where": "console", "text": m})
            shot = out / f"screenshot-{w}{'-dark' if a.dark else ''}.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                res["screenshot"] = str(shot.resolve())
            except Exception as e:  # noqa: BLE001
                res["screenshot_error"] = str(e)
            res["width"] = w
            report["runs"].append(res)
            ctx.close()
        browser.close()

    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    total = 0
    for run in report["runs"]:
        f = run["findings"]
        total += len(f)
        kinds: dict[str, list] = {}
        for x in f:
            kinds.setdefault(x["kind"], []).append(x)
        print(f"== width {run['width']}px: {len(f)} findings  ({run.get('stats', {}).get('title', '')})")
        for k, items in sorted(kinds.items()):
            print(f"  [{k}] {len(items)}")
            for x in items[:LIMIT_PER_KIND]:
                rest = {kk: vv for kk, vv in x.items() if kk not in ("kind", "where")}
                print(f"    - {x['where']}  {json.dumps(rest, ensure_ascii=False)}")
            if len(items) > LIMIT_PER_KIND:
                print(f"    ... {len(items) - LIMIT_PER_KIND} more in report.json")
        s = run.get("stats", {})
        print(f"  stats: {len(s.get('font_sizes', []))} font sizes {s.get('font_sizes', [])[:8]}")
        print(f"         families {s.get('font_families', [])}; {s.get('text_colors')} text colors")
        print(f"         spacing off the 4px grid: {s.get('spacing_off_grid', [])} of {s.get('spacing_values')}")
        if s.get("contrast_unknown"):
            print(f"         contrast not computed for {s['contrast_unknown']} text elements (image/gradient backgrounds)")
        if run.get("screenshot"):
            print(f"  screenshot: {run['screenshot']}")
    print(f"report: {(out / 'report.json').resolve()}  total findings: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
