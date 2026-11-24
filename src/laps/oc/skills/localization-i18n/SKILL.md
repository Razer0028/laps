---
name: localization-i18n
description: Internationalization and localization of apps, websites and games - string externalization, ICU message format and plurals, locale-aware dates/numbers, Japanese/CJK text handling (encodings, fonts, line breaking, vertical text, IME), translation workflows (PO, XLIFF, JSON, CSV), machine translation review, and testing with pseudo-localization. Use when adding languages, translating content, fixing mojibake, or preparing a game/app for Japanese or English.
keywords: 翻訳, ローカライズ, ローカライゼーション, localization, localize, i18n, l10n, 多言語, 日本語化, 英語化
---

# Localization and i18n

## Externalize everything
- No user-facing strings in code: keys in resource files (`messages/en.json`, `ja.json`; gettext `.po`; Android `strings.xml`; Unity Localization tables; Godot CSV/PO translations).
- Keys describe meaning, not text: `settings.audio.volume_label`, not `volume`.
- Never concatenate translated fragments (`"You have " + n + " items"`). Use whole messages with placeholders and plural rules (ICU MessageFormat):
  `{count, plural, =0 {No items} one {# item} other {# items}}` - Japanese has only `other`, others have many forms.
- Give translators context: screenshots or comments, max length, where the string appears, what placeholders mean.

## Formatting
- Dates, numbers, currency with the platform locale APIs (`Intl.DateTimeFormat`, `Intl.NumberFormat`, Python Babel, .NET `CultureInfo`, ICU). Store times in UTC, display in the user's time zone.
- Japanese date conventions: `2026年10月6日(火)`, `YYYY/MM/DD`; 24-hour time common in apps.
- Sorting/comparison with locale collation (`Intl.Collator('ja')`), not byte order.

## Japanese and CJK text
- Use UTF-8 everywhere. Mojibake recipes: UTF-8 bytes read as CP932 gives "縺" style garbage; CP932 read as UTF-8 gives replacement characters. Detect, then re-decode from the original bytes (do not "fix" already-garbled text by round-tripping).
- Legacy Windows tools and old games often use Shift-JIS/CP932; convert explicitly (`iconv -f CP932 -t UTF-8`, Python `encoding="cp932"`).
- Fonts: include glyph coverage for kanji (Noto Sans/Serif JP, BIZ UD, M PLUS). Game engines need dynamic fonts or atlases that contain all used characters - generate the character set from the translated text.
- Line breaking: CJK breaks between most characters but not before closing punctuation (。、」）) or after opening ones (「（). Browsers handle it (`line-break: strict`, `word-break: normal`, `overflow-wrap: anywhere` for long Latin); game text renderers often need kinsoku rules implemented.
- Text length: Japanese is shorter in characters but needs larger font sizes; English and German expand 30-50% from Japanese. Design UI to grow (no fixed-width buttons with text).
- Ruby (furigana): HTML `<ruby>漢字<rt>かんじ</rt></ruby>`; games need their own markup.
- Full-width vs half-width: normalize user input with NFKC when matching (search, IDs), but never rewrite display text silently.
- IME: do not trigger actions on Enter while composing (`event.isComposing` / `keyCode 229` in browsers).

## Translation workflow
1. Extract strings (i18next-parser, xgettext/Babel, `dotnet`/Android tooling, engine exporters) into one source file per language.
2. Translate (human, or machine translation as a draft) keeping placeholders, tags and line breaks intact; keep a glossary for names and game terms and a style guide (polite/plain form, tone per character).
3. Validate automatically: same keys in every language, same placeholders, no untranslated empty strings, length limits.
4. Review in context (screenshots or a running build), not only in a spreadsheet.
5. Pseudo-localize to find hard-coded strings and clipping: replace text with accented, 40% longer versions (`[!!! Ŝéţţîñĝš !!!]`).

## Games specifically
- Separate text from scripts/scenes; support font fallback per language; allow per-language voice files and subtitle timing.
- Check that save files and file paths handle non-ASCII user names (Windows profile paths can contain Japanese and full-width spaces).
