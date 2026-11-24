---
name: docker
description: Containers with Docker and Docker Compose - writing small secure Dockerfiles (multi-stage, non-root, caching), compose services with volumes/networks/healthchecks/env, debugging containers that exit or misbehave, logs, resource limits, cleanup of disk usage, and registries. Use for any Dockerfile, compose file, or container problem.
keywords: docker, dockerfile, docker-compose, compose.yaml, compose.yml, コンテナ, container, podman
---

# Docker

## Dockerfile
```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS deps
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM base
COPY --from=deps /install /usr/local
COPY . .
RUN useradd -r -u 10001 app && chown -R app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
- Copy dependency manifests before source code so the dependency layer is cached.
- `.dockerignore`: `.git`, `node_modules`, `.venv`, `__pycache__`, build outputs, `.env`.
- Exec-form `CMD ["..."]` so signals reach the app; one process per container.
- Pin base images by tag (and digest for production). Node: `node:20-slim`, `npm ci --omit=dev`; Go/Rust: build in a full image, copy the binary into `gcr.io/distroless/static` or `debian:stable-slim`.
- Never bake secrets into images (they stay in layers). Use build secrets (`RUN --mount=type=secret,id=npm`) or runtime env.

## Compose
```yaml
services:
  app:
    build: .
    env_file: .env
    ports: ["127.0.0.1:8000:8000"]      # bind to localhost unless it must be public
    depends_on:
      db: { condition: service_healthy }
    restart: unless-stopped
  db:
    image: postgres:16
    environment: { POSTGRES_PASSWORD_FILE: /run/secrets/db_pw }
    volumes: ["dbdata:/var/lib/postgresql/data"]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U postgres"], interval: 5s, retries: 10 }
volumes: { dbdata: {} }
```
- `docker compose config` validates and shows the merged result; `docker compose up -d --build`; `docker compose logs -f --tail=100 app`.
- Services reach each other by service name (`db:5432`), not localhost.
- Published ports bypass ufw on Linux (Docker writes its own iptables rules): bind to `127.0.0.1` and put a reverse proxy in front.

## Debugging
- Container exits immediately: `docker ps -a`, `docker logs <c>`, `docker inspect <c> --format '{{.State.ExitCode}} {{.State.Error}}'`. Exit 137 = killed (often OOM: check `.State.OOMKilled`), 139 = segfault, 1/2 = app error.
- Get a shell: `docker exec -it <c> sh`; for a crashing image: `docker run --rm -it --entrypoint sh <image>`.
- Networking: `docker network inspect <net>`, test from inside (`docker exec c wget -qO- http://other:port`). App must listen on `0.0.0.0`, not `127.0.0.1`, inside the container.
- Permissions on bind mounts: match the container user's UID with the host directory owner, or use named volumes.
- Build problems: `docker build --progress=plain --no-cache .` shows every step.

## Resources and cleanup
- Limits: `deploy.resources.limits` in compose or `--memory 1g --cpus 1.5`.
- Disk: `docker system df`; clean with `docker image prune`, `docker builder prune`, `docker container prune`. `docker system prune -a --volumes` deletes volumes (data) - only when asked.
- Logs grow without limit by default: set `logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }`.

## Windows/macOS
Docker Desktop runs a Linux VM: bind mounts from the Windows filesystem are slow (keep code in WSL2 for heavy I/O), line endings in shell scripts must be LF (`.gitattributes: *.sh text eol=lf`).
