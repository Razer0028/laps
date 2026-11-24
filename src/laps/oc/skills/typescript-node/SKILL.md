---
name: typescript-node
description: TypeScript and Node.js - project setup (package.json, tsconfig, ESM vs CommonJS), package managers (npm, pnpm, yarn), running TS (tsx, node --experimental-strip-types), type design, error handling, async patterns, testing (Vitest), linting/formatting (ESLint, Biome, Prettier), building libraries/CLIs, and common runtime pitfalls. Use for any JS/TS code outside framework-specific UI work.
keywords: typescript, tsconfig, node.js, nodejs, package.json, npm, pnpm, yarn, deno, bun
---

# TypeScript and Node.js

## Setup
- Use the package manager the lockfile indicates (`package-lock.json` npm, `pnpm-lock.yaml` pnpm, `yarn.lock` yarn, `bun.lockb` bun). `npm ci` in CI/automation; `npm install` to change deps.
- Node LTS (20/22). Pin in `.nvmrc` and `"engines"`.
- ESM by default: `"type": "module"` in package.json; imports need file extensions in Node ESM (`./util.js` even for `.ts` sources when compiling with tsc, or use a bundler/tsx).
- tsconfig baseline for Node apps:
```json
{ "compilerOptions": { "target": "ES2022", "module": "NodeNext", "moduleResolution": "NodeNext",
  "strict": true, "noUncheckedIndexedAccess": true, "esModuleInterop": true, "skipLibCheck": true,
  "outDir": "dist", "rootDir": "src", "sourceMap": true, "declaration": true } }
```
For bundler-based frontends use `"module": "ESNext", "moduleResolution": "Bundler"`.
- Run TS directly: `npx tsx src/index.ts` (dev). Type-check separately: `npx tsc --noEmit`.

## Types and code
- `strict` on; no `any` (use `unknown` and narrow). Model data with discriminated unions; parse external data with zod/valibot at the boundary and infer types from schemas.
- `const` by default, immutable updates, small pure functions. Prefer `Map`/`Set` for lookups.
- Errors: throw `Error` subclasses with a `cause`; never swallow promise rejections. Every `async` call is awaited or explicitly handled; add `process.on('unhandledRejection', ...)` logging in services.
- Concurrency: `Promise.all` for independent work, limit fan-out (p-limit) for many network calls; `AbortController` for timeouts (`fetch(url, { signal: AbortSignal.timeout(10_000) })`).
- Paths: `node:path` and `fileURLToPath(import.meta.url)` in ESM (no `__dirname`). File I/O with `node:fs/promises`, explicit `'utf8'`.
- Child processes: `execFile`/`spawn` with argument arrays, not `exec` with interpolated strings.

## Testing and quality
- Vitest (`npx vitest run`), node:test for zero-dependency packages. Mock at module boundaries (network, time).
- Lint/format: follow the repo (ESLint flat config `eslint.config.js` + Prettier, or Biome `npx biome check --write`).

## Building
- Apps: `tsc` to `dist/` or bundle with esbuild/tsup (`npx tsup src/index.ts --format esm,cjs --dts`).
- CLIs: `"bin": {"tool": "dist/cli.js"}` with `#!/usr/bin/env node` at the top.
- Libraries: ship ESM (and CJS if needed) with `"exports"` map and types; test the packed tarball (`npm pack`).

## Pitfalls
- `npm install` without a lockfile gives different versions each time - commit the lockfile.
- Windows: scripts in package.json run in cmd.exe; use cross-platform tools (`rimraf`, `cross-env`) or node scripts instead of `rm -rf` and `VAR=x cmd`.
- Memory: `node --max-old-space-size=4096` for big builds; leaks via unbounded caches/listeners (`emitter.setMaxListeners` warnings are a symptom).
