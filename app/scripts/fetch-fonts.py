#!/usr/bin/env python3
"""同梱フォントを取り直す。ui/fonts/ と ui/fonts.css を作る。

Google Fonts は unicode-range で分割配信する。★1つ目の subset だけ取ると日本語が欠ける
（実際に踏んだ: IBM Plex Sans JP が 3.6KB しか落ちてこず、日本語が代替フォントになった）。
全 subset を取り、同じ unicode-range を持つ @font-face を生成する。
"""
import re, pathlib, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
FAMS = [("Saira+Condensed", "600;700", "saira"), ("IBM+Plex+Sans+JP", "400;600", "plexjp"),
        ("IBM+Plex+Mono", "400", "plexmono")]
NAME = {"saira": "Saira Condensed", "plexjp": "IBM Plex Sans JP", "plexmono": "IBM Plex Mono"}
out = pathlib.Path(__file__).resolve().parent.parent / "ui" / "fonts"
out.mkdir(parents=True, exist_ok=True)
css = []
for fam, w, slug in FAMS:
    url = f"https://fonts.googleapis.com/css2?family={fam}:wght@{w}&display=swap"
    src = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode()
    for i, block in enumerate(re.findall(r"@font-face\s*\{(.*?)\}", src, re.S), 1):
        m = re.search(r"src:\s*url\((https://[^)]+\.woff2)\)", block)
        if not m: continue
        weight = (re.search(r"font-weight:\s*(\d+)", block) or [None, "400"])[1]
        rng = (re.search(r"unicode-range:\s*([^;]+);", block) or [None, None])[1]
        fn = f"{slug}-{weight}-{i}.woff2"
        (out / fn).write_bytes(urllib.request.urlopen(urllib.request.Request(m.group(1), headers=UA), timeout=60).read())
        css.append(f'@font-face{{font-family:"{NAME[slug]}";src:url("fonts/{fn}") format("woff2");'
                   f'font-weight:{weight};font-display:swap;' + (f"unicode-range:{rng};" if rng else "") + "}")
(out.parent / "fonts.css").write_text("/* 自動生成: scripts/fetch-fonts.py */\n" + "\n".join(css) + "\n")
print(f"{len(list(out.glob('*.woff2')))} files, "
      f"{sum(f.stat().st_size for f in out.glob('*.woff2'))//1024} KB")
