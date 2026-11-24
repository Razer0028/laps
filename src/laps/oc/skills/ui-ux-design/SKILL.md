---
name: ui-ux-design
description: Structure and craft for any user interface - web apps, dashboards, admin screens, forms, desktop apps, game menus. Covers state design (empty/loading/error), information hierarchy, spacing and type systems, color tokens with dark mode, motion, accessibility, forms, tables, and a no-screenshot audit script. Use with frontend-design (aesthetic direction) whenever you build or restyle UI.
keywords: ui, ux, ui/ux, 画面設計, ユーザー体験, 使いやすさ, 使い勝手, ダッシュボード, dashboard, 管理画面, フォーム, レイアウト
---

# UI/UX design

This skill is the structure half of design. `frontend-design` sets the visual direction (palette, typefaces, the one memorable thing); this skill makes the screen work and look finished. Load both when building UI.

Leverage order: (1) states, (2) hierarchy, (3) spacing system, (4) type, (5) color, (6) motion. Most "cheap-looking" UI is fixed by 1-3, not by changing colors.

## 1. Answer five questions before placing pixels
1. What is this screen's one job? (Two jobs -> consider two screens.)
2. Who looks at it, in what situation? (daily power user, first-time visitor, on a phone)
3. What is the primary action? Exactly one per screen; it gets the strongest button.
4. How can it fail? (empty, loading, partial failure, no permission, huge data, offline)
5. Constraints: existing design system, framework, data refresh rate, target devices.

## 2. State design - the biggest quality difference
Every surface that shows data has at least five states. Building only the ideal state is the main tell of generated UI.

| State | Rule |
|---|---|
| Empty | Say what is missing, why, and give one next action (a button). Never just "No data". |
| Loading | Skeleton that matches the final layout (no layout shift). Spinners only for < 1 s or inline actions. |
| Partial failure | Keep the rest working; show the error and a retry where the failed part would be. |
| Error | Put it next to the control that caused it. Say what happened and how to fix it. No apologies, no vague "Something went wrong". |
| Ideal | Test with the longest and shortest real values (1-char name, 60-char name, 0, 1, 10,000 items). |

Also decide: no-permission (hide vs disable with reason), very large data (paging, virtualization, search), offline/reconnecting.

## 3. Hierarchy and information architecture
- Reading order: where am I -> what is the current status -> what can I do -> details.
- Group with spacing first; add borders/boxes only when spacing cannot do it. Fewer boxes looks more expensive.
- One primary button per view; secondary actions are quieter (outline/ghost); destructive actions are separated and confirmable (or undoable).
- Numbers need context: "1,284 orders (+12% vs last week)" not "1,284".
- Progressive disclosure for advanced options, but never hide the primary action.
- Number markers (01/02/03) only for real sequences.

## 4. Spacing and layout system
Never pick pixel values ad hoc. Use a 4/8 px scale and only values from it. Copy `reference/tokens.css` as the starting point.
- Within a group < between groups < between sections (e.g. 8 / 16-24 / 48-64).
- Body text measure 45-75 characters (`max-width: 68ch`); Japanese ~35-45 full-width chars.
- Left-align by default; center only hero headings and empty states.
- Intrinsic layout before breakpoints: `grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr))`.
- Use `100dvh`, not `100vh`; respect `env(safe-area-inset-*)` on mobile.
- Grid and flex children default to `min-width: auto`: a wide table or code block inside makes the whole column grow past the screen. Put `min-width: 0` on grid/flex children that contain scroll containers.
- Start app screens from `reference/app-shell.html` (sidebar, header, KPIs, table, empty state) instead of from a blank page.
- App shells: fixed header 48-56 px, sidebar 220-280 px (collapsible), content max-width 1200-1440 px with generous side padding.

## 5. Typography
- 3-4 sizes are enough for an app UI (e.g. 12/14/16/20/28). Build hierarchy with weight and color before size.
- Body 14-16 px in apps, 16-18 px for reading. Line-height 1.5 (Latin), 1.7-1.8 (Japanese).
- `font-variant-numeric: tabular-nums` for all numbers, times, IDs, table columns.
- `text-wrap: balance` for headings, `text-wrap: pretty` for paragraphs.
- Font stacks that work everywhere, including Japanese: see `reference/tokens.css` (`--font-sans`). Do not ship a web font for an internal tool unless asked; system UI fonts look native.
- No all-caps labels with wide tracking as decoration.

## 6. Color
- Assign color to meaning, not decoration: background, surface, border, text, muted text, one accent, and status colors (danger, warning, success, info).
- One accent color per screen. Status colors only for status.
- Contrast: body text >= 4.5:1, large text and UI component boundaries >= 3:1. Never encode information with color alone (add icon/text).
- Dark mode is its own palette, not an inversion: raise surfaces with lighter grays instead of shadows, lower accent saturation slightly, never pure #000 background with pure #fff text for long reading.
- Avoid decorative gradients, glows, and glassmorphism unless the brief asks for them.
- Shadows subtle and consistent (2-3 elevation levels max); 1 px hairline borders do most of the work.

## 7. Motion
- Default: no motion unless it shows a change caused by the user (open, expand, confirm, reorder).
- Animate only `transform` and `opacity`; 120-200 ms, ease-out for entering, ease-in for leaving.
- Respect `prefers-reduced-motion` (snippet in tokens.css). Stop loops when off-screen.

## 8. Accessibility (cannot be bolted on later)
- Everything works with keyboard only (Tab, Shift+Tab, Enter, Space, Esc). Visible focus: `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }`. Never `outline: none` without a replacement.
- Semantic elements first (`button`, `a`, `label`, `nav`, `main`, `dialog`, `table`); ARIA only to fill gaps.
- Icon-only buttons need `aria-label`. Images need `alt` (empty `alt=""` if decorative).
- Heading levels in order. Hit targets >= 24x24 px (44x44 on touch).
- Do not build custom focus traps/menus from scratch when the framework has accessible primitives (Radix, React Aria, Headless UI, native `<dialog>`).

## 9. Forms
- Visible labels always (placeholder is not a label). Mark either required or optional, not both.
- Validate on blur or submit, not on every keystroke. Error text directly under the field, linked with `aria-describedby`, field gets `aria-invalid="true"`.
- Correct `type`, `autocomplete`, `inputmode`. Never block paste.
- Submit: disable + show progress to prevent double submit; keep the user's input on failure.
- Button text says what happens ("Save changes", "Create project"), and the result uses the same verb ("Saved").

## 10. Data display
- Tables: left-align text, right-align numbers (tabular-nums), sticky header for long tables, row height 36-44 px (dense) or 48-56 px (comfortable), zebra stripes or hairlines, not both. Truncate long text with a tooltip/title, never wrap IDs mid-character.
- Dashboards: the most important 3-5 numbers with comparison; not every metric in an identical card. Charts: direct labels over legends, no 3D, no dual axes unless unavoidable, color by series meaning.
- Lists over 200 rows: virtualize or paginate. Provide search/filter when items > ~20.

## 11. Desktop apps (Tauri, Electron, WPF, Qt)
- Respect the platform: system font, native window controls, standard shortcuts (Ctrl/Cmd+S, Ctrl+F, Esc closes dialogs, Ctrl+Enter submits multi-line input).
- Minimum window size that does not break the layout (e.g. 900x600); test at 125%/150% OS scaling.
- Keep the chrome quiet; content gets the contrast. Persist window size, panel widths, last view.
- Long operations: show progress in place, allow cancel, never freeze the UI thread.

## 12. Verify without looking
You may not be able to see images. Use the audit script instead of guessing:

    python scripts/ui_audit.py <url-or-html-file> [--out DIR]

It loads the page in a real browser (Edge/Chrome via Playwright; installs nothing else), checks desktop and mobile widths, and reports: low-contrast text, tiny fonts, horizontal overflow, small hit targets, unlabeled controls and images, number of distinct font sizes/colors/spacing values off the 4 px grid. It also saves screenshots for the user. Setup once: `pip install playwright` (no browser download needed when Edge or Chrome is installed). Fix every reported item or say why it is intentional.

## 13. Review checklist (before you say it is done)
- [ ] Empty, loading, error, partial, ideal states all exist
- [ ] One primary action per view; destructive actions confirmable or undoable
- [ ] Keyboard-only pass works, focus is visible
- [ ] Longest/shortest data do not break layout; no horizontal scroll at 360 px
- [ ] Contrast passes; color is never the only signal
- [ ] Spacing comes from the scale; 3-4 type sizes; one accent
- [ ] Numbers are tabular; motion only on transform/opacity and respects reduced motion
- [ ] Audit script shows no unexplained findings

When reviewing someone's UI, give each finding as: quote of the code -> why it is a problem (one sentence) -> the concrete fix.

Files: `reference/tokens.css` (design tokens, light/dark, base styles), `reference/components.css` (buttons, inputs, cards, tables, badges, toasts, dialogs, empty states with all interaction states), `scripts/ui_audit.py`.
