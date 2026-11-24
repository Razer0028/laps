---
name: game-dev
description: General game development - architecture (game loop, scenes, state machines, events), save data, input, audio, UI for games, performance budgets, asset pipelines, scenario/flag design for story games, Live2D-style character rigs, and web games (Phaser, PixiJS, three.js). Use when building or fixing a game in any engine; load unity or godot too for engine specifics.
keywords: ゲーム開発, ゲームを作, ゲーム制作, game loop, ゲームループ, セーブデータ, save data, ゲームの仕組み, ステートマシン
---

# Game development

## Decide early (expensive to change later)
- Engine and target platforms (PC/mobile/web/console) - they fix the input model, performance budget and build pipeline.
- Save format and versioning: put a `version` field in every save; write migrations instead of breaking old saves. Save atomically (write temp file, then rename).
- Resolution strategy: design resolution + scaling mode (pixel-perfect integer scaling for pixel art; letterbox or expand for others). Test 16:9, 16:10, 21:9, 4:3 and portrait if mobile.
- Content in data, not code: dialogue, items, enemies, levels in JSON/CSV/YAML/ScriptableObjects/Resources so designers can change them and you can hot-reload.
- Localization keys from day one (see localization-i18n skill).

## Architecture that scales
- Fixed timestep for simulation (physics, gameplay logic), variable for rendering; interpolate between states for smooth display. Never multiply movement by frame count; use delta time.
- Explicit state machines for game flow (Boot -> Title -> Loading -> Playing -> Paused -> GameOver) and for characters (Idle/Run/Jump/Attack). Each state has enter/update/exit; transitions in one place.
- Communicate through events/signals for decoupled systems (UI listens to `HealthChanged`, it does not poll the player).
- Composition over deep inheritance: small components (Health, Mover, Hitbox) attached to entities.
- Object pools for bullets, particles, enemies, damage numbers. Never allocate per frame in hot paths.
- One source of truth for game state; UI reads it, never owns it.

## Input
- Map actions, not keys ("jump", "confirm") so rebinding and gamepads work. Support keyboard+mouse and gamepad from the start on PC.
- Buffer inputs (accept a jump pressed ~100 ms before landing) and add coyote time (~100 ms after leaving a ledge) for platformers.
- UI must be fully usable with gamepad/keyboard: visible focus, predictable navigation order.

## Game UI/UX
- Readability first: HUD text at least 24 px at 1080p, outlines/shadows on text over gameplay, safe areas on TVs and phones.
- Feedback for every action within ~100 ms: sound + visual (hit flash, screen shake used sparingly, particles).
- Menus: one primary action highlighted, back/cancel always works (Esc / B button), settings apply immediately with preview.
- Accessibility: subtitles with speaker names and background, remappable controls, colorblind-safe indicators (shape + color), adjustable text size, toggle instead of hold options.

## Performance budgets
- 60 fps = 16.6 ms per frame (CPU and GPU each). 30 fps = 33.3 ms. Measure with the engine profiler before optimizing.
- Usual wins in order: stop per-frame allocations (GC spikes), reduce draw calls (batching, atlases, instancing), cull off-screen work, LOD, texture compression and sizes, fewer physics bodies/collision layers.
- Load asynchronously with a loading screen; stream big levels.

## Assets
- Naming convention and folder structure documented before production (e.g. `chr_hero_run_01.png`, `sfx_ui_confirm.wav`).
- Source files (PSD, Blend, FLA) in LFS or a separate store; exported game-ready assets in the project.
- Audio: 48 kHz WAV sources; compressed (Vorbis/Opus) for music and long voice, ADPCM/PCM for short SFX that play often. Normalize loudness (e.g. -16 LUFS music, consistent voice levels).
- Sprites: power-of-two atlases where the engine benefits; keep padding to avoid bleeding.

## Story and visual-novel games
- Flags with meaningful names in one registry (`met_rival`, `route_a_lock`), never `flag[37]`.
- Branch conditions as data (tables of conditions/targets), not nested if-statements in scripts.
- Decide early: read/unread tracking, skip behavior, backlog, save slots with screenshots, compatibility of saves across script updates.
- Scenario text outside code; for large scripts (over ~10,000 characters) use external script files with a parser rather than embedded strings.

## Character rigs (Live2D-style 2D animation)
- Cut the illustration into layers per moving part (front/back hair, brows, eyes, pupils, eyelids, mouth, face, body, arms), draw hidden areas underneath, name layers in ASCII (`hair_front_L`), separate left/right parts.
- Merge layer masks and clipping before import; some importers crash on masked PSD layers.
- Live2D Cubism licensing depends on revenue and distribution: check the official terms, do not answer from memory. Open-source alternatives: Inochi2D and its fork nijigenerate (BSD-2-Clause; nijigenerate has an optional local MCP/HTTP automation server - disable it after use).

## Web games
- Phaser 3 for 2D games with scenes/physics/input built in; PixiJS for a fast 2D renderer when you want your own architecture; three.js (or Babylon.js) for 3D. Use Vite for dev server and builds.
- Handle `devicePixelRatio` for crisp canvases, pause on `visibilitychange`, unlock audio on first user gesture (browsers block autoplay).
- Ship assets compressed (WebP/AVIF, Ogg/Opus with MP3/AAC fallback for Safari), preload per scene.

## Licenses
Fonts, music, sound effects, engine plugins and AI-generated assets all have terms. Check the actual license text before shipping; record the source and license of every third-party asset in a credits file.
