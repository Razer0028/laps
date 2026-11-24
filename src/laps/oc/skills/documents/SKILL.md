---
name: documents
description: Make business documents with the Doc tool - reports, proposals, specifications, manuals and minutes in Word or PDF, slide decks in PowerPoint, and spreadsheets in Excel - including charts, tables and company templates. Covers planning the structure before writing, Japanese business conventions, slide design rules, Excel conventions (numbers as numbers, formulas, units), reading existing files to update them, and checking the result.
keywords: 資料, 報告書, 提案書, 企画書, 仕様書, 手順書, マニュアル, 議事録+作, スライド, パワポ, パワーポイント, プレゼン, エクセル, 表計算, word, ワード, pdf+作, docx, pptx, xlsx, slide deck, presentation, spreadsheet, report
---

# Documents (Doc tool)

## Pick the format
- Word (.docx): a document others will edit or comment on (reports, proposals, specs, manuals, minutes).
- PDF (.pdf): the final version to hand out; nobody edits it. Make it from the same Markdown.
- PowerPoint (.pptx): a talk or a meeting. Excel (.xlsx): numbers people will sort, filter or recalculate.
- If the user only says "資料", ask what it is for (who reads it, where it is shown) only when the answer changes the format; otherwise choose and say which.

## Plan first
1. Write the outline (headings or slide titles) and check it covers what the user asked. For a long document, show the outline to the user before filling it.
2. Gather the facts from files, the web, or the user. Never invent numbers, names, dates or quotes; leave a visible placeholder like 【要確認: 前年比】 and list them in your reply.
3. Then write the content in one Doc call.

## Writing (Japanese business documents)
- Conclusion first: the first paragraph (or the first slide after the title) says what the reader must know or decide.
- Headings are short nouns or statements (「現状の課題」「提案: 〇〇を導入する」). One topic per section.
- Prefer lists and tables to long paragraphs; keep sentences short; use です・ます unless the user's material uses だ・である.
- Put the date, the author and the version near the title when the document will be passed around.
- Numbers: units in the header or after the number (万円, 件, %), the period they cover, and the source.

## Slides
- One message per slide; the title states it (「売上は前年比 12% 増」 rather than 「売上」).
- At most about 6 bullets, each one line; split a crowded slide into two. Use `---` between slides.
- Show numbers as a chart (```chart) and comparisons as a table; text and a chart on the same slide are placed side by side.
- Put the talk track in `<!-- notes: ... -->` instead of on the slide.
- theme: {"accent": "#hex"} for the brand color, {"dark": true} for a dark deck. With the user's company template, pass template="…pptx".

## Excel
- One header row, one record per row, no merged cells, no blank rows inside the table.
- Values as numbers (not "1,234円"); put the unit in the header (「金額（円）」) and the display format in number_formats ({"C": "#,##0"}).
- Totals and ratios as formulas ("=SUM(B2:B13)", "=C2/B2"), so they stay correct when edited.
- A chart: {"type": "column", "categories": "A", "series": ["B", "C"], "title": "..."} on the sheet that holds the data.
- Several tables: one sheet each, named by content (「月次」「顧客別」).

## Charts (```chart in Markdown, or "chart" on a slide)
- column/bar: compare categories; line/area: change over time; pie: parts of one whole (at most 5-6 slices); scatter: two measures per item (points: [[x, y], ...]).
- One axis only. Title says what the chart shows; keep categories in a natural order (time, size).

## Updating an existing document
- Read it first: Doc(action="read", path=...). Then write the new version as a new file (report_v2.docx) unless the user asked to replace it (overwrite=true; the old file is backed up for /rollback).
- Doc rewrites the whole file. To keep a document's look, pass the original as template (its styles, headers and footers are kept, its text is replaced).

## Check before you finish
- Read the result back with Doc(action="read") and compare it with the outline: every section present, no placeholder left unintentionally, numbers match the sources.
- Report any Warnings from Doc (missing images, chart errors) and the 【要確認】 items to the user, with the file path.
