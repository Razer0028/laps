#!/usr/bin/env python3
"""ui/ を1枚の preview.html にまとめる（Artifact で意匠を見せるため）。

Artifact は外部取得を全面的に遮断するので、CSS/JS は埋め込み、フォントは data URI にする。
★日本語 subset(246個)は載せない — 数 MB になるため。プレビューでは閲覧端末のフォントに任せ、
その旨を画面に出す。製品（Tauri）側は ui/fonts/ を丸ごと同梱するので日本語も欠けない。
"""
import re, base64, pathlib
ui = pathlib.Path(__file__).resolve().parent.parent / "ui"
picked = {}
for m in re.finditer(r'@font-face\{font-family:"([^"]+)";src:url\("fonts/([^"]+)"\)[^}]*?font-weight:(\d+);[^}]*?(?:unicode-range:([^;]+);)?\}',
                     (ui / "fonts.css").read_text()):
    fam, fn, w, rng = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    if "IBM Plex Sans JP" in fam: continue
    if rng and "U+0000-00FF" not in rng and "U+0041" not in rng: continue
    if (fam, w) in picked: continue
    picked[(fam, w)] = (f'@font-face{{font-family:"{fam}";font-weight:{w};font-display:swap;'
                        f'src:url(data:font/woff2;base64,{base64.b64encode((ui/"fonts"/fn).read_bytes()).decode()}) format("woff2");}}')
body = re.search(r"<body>(.*)</body>", (ui / "index.html").read_text(), re.S).group(1)
body = body.replace('<script src="app.js"></script>', "")
extra = "html,body{height:auto;min-height:100dvh;overflow:auto}.app{height:auto;min-height:calc(100dvh - 30px)}"
p = ui.parent / "preview.html"
p.write_text(f"<style>{''.join(picked.values())}\n{(ui/'styles.css').read_text()}\n{extra}</style>\n"
             f"{body}\n<script>{(ui/'app.js').read_text()}</script>\n")
print("preview.html", p.stat().st_size // 1024, "KB")
