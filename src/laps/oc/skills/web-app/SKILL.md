---
name: web-app
description: Build and ship web applications end to end - choosing a stack, scaffolding (Vite, React, Next.js, Vue, SvelteKit), APIs (FastAPI, Express, Hono), databases and migrations, authentication, forms and validation, testing (Vitest, Playwright), production builds, deployment (Docker, nginx, systemd, static hosting), and a security checklist. Use for any web frontend/backend work; pair with ui-ux-design and frontend-design for the interface.
keywords: webアプリ, web アプリ, ウェブアプリ, ウェブサイト, website, web site, react, next.js, nextjs, vue, svelte, vite, フロントエンド, バックエンド, html, css
---

# Web applications

## Pick the smallest stack that fits
| Need | Default choice |
|---|---|
| Static site / docs | plain HTML+CSS, or Astro |
| Interactive SPA, separate API | Vite + React + TypeScript (or Vue/Svelte if the project already uses it) |
| SEO pages + app in one, server rendering | Next.js (App Router) or SvelteKit/Nuxt |
| Python backend / ML / data | FastAPI + Pydantic + SQLAlchemy/SQLModel |
| Node backend | Hono or Express + Zod; Fastify for heavy APIs |
| Small internal tool | one server rendering HTML (FastAPI + Jinja, or Express + templates) + htmx |
Follow what the repository already uses. Check `package.json`/`pyproject.toml` before adding anything.

## Scaffolding (non-interactive)
```
npm create vite@latest web -- --template react-ts      # then: cd web && npm install
npx create-next-app@latest app --ts --eslint --app --src-dir --use-npm --no-tailwind --yes
python -m venv .venv && .venv/bin/pip install "fastapi[standard]"   # Windows: .venv\Scripts\pip
```
Always pass flags so generators do not stop at prompts. Commit the lockfile.

## Frontend rules
- Components small and typed; server state with TanStack Query (or the framework's loaders), client state with useState/useReducer or a small store (Zustand); do not put server data in global state by hand.
- Forms: react-hook-form + zod (or native form + server validation). Validate on the server always; client validation is only for speed.
- Every data view handles loading, empty, error (see ui-ux-design).
- Environment variables: only values prefixed for the client (`VITE_`, `NEXT_PUBLIC_`) reach the browser - never put secrets there.
- Styling: CSS variables/tokens (ui-ux-design/reference/tokens.css) with CSS Modules, or Tailwind if the project uses it. Avoid mixing three systems.

## Backend rules
- Validate every input at the boundary (Pydantic/Zod). Return consistent JSON errors: `{ "error": { "code": "...", "message": "..." } }` with correct HTTP status.
- Database access through one module; migrations with Alembic (Python), Prisma/Drizzle/Knex (Node). Never edit production schema by hand.
- Config from environment variables with a typed settings object; `.env` for local only, never committed (`.env.example` is committed).
- Health endpoint `/healthz` that checks the DB connection; structured logs (JSON) with request id.
- Long work goes to a background job/queue, not the request handler.

## Auth
- Prefer a proven library (Auth.js/NextAuth, Lucia-style sessions, FastAPI Users, framework built-ins) or an identity provider (OIDC). Do not invent password storage: use argon2id/bcrypt.
- Sessions: HttpOnly, Secure, SameSite=Lax cookies. CSRF protection for cookie-authenticated forms. Rate-limit login.

## Testing
- Unit: Vitest (frontend/Node), pytest (Python). API: test through the HTTP layer (FastAPI TestClient, supertest).
- End to end: Playwright (`npx playwright test`); use role-based locators (`getByRole('button', { name: 'Save' })`).
- Run the production build locally before deploying (`npm run build && npm run preview`, `next build && next start`).

## Deploy
- Static: build output (`dist/`) behind nginx/Caddy or a static host. SPA routing needs a fallback to `index.html`.
- Server: Docker image (multi-stage, non-root user, healthcheck) or a systemd service behind nginx/Caddy reverse proxy with TLS. Example nginx location:
```
location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme;
  proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; }
```
- Uvicorn behind a proxy: `--proxy-headers --forwarded-allow-ips=127.0.0.1`; several workers via gunicorn with uvicorn workers or `--workers N`.

## Security checklist
- Parameterized queries only; escape output (frameworks do this - do not bypass with `dangerouslySetInnerHTML`/`|safe` on user data).
- Authorization check on every endpoint that reads/writes user data (not just "logged in").
- CORS: explicit origins, never `*` with credentials. Security headers: CSP, X-Content-Type-Options, Referrer-Policy, HSTS on HTTPS.
- File uploads: size limits, type check by content, store outside the web root with random names.
- Secrets in environment/secret store, never in the repo or client bundle. `npm audit` / `pip-audit` before release.
