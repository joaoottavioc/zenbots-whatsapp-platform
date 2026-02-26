# Restart Specification

## Rule: Always Restart After Codebase Changes

Whenever you make **any change** to the codebase (Python files, configs, templates, etc.), you **must** restart the affected services to apply those changes.

## Required Command Sequence

```bash
docker compose restart backend worker && docker compose up
```

Run this from the project root directory every time after modifying files.

## Why Both Commands

- `docker compose restart backend worker` — gracefully restarts the running `backend` and `worker` containers so the new code is loaded.
- `docker compose up` — ensures any stopped or dependent services (db, redis, ollama) are running and properly connected.

## Which Services to Restart

| Changed file(s) | Services to restart |
|---|---|
| `app/*.py` (any Python file) | `backend worker` |
| `app/worker.py` | `worker` |
| `app/main.py`, `app/*routes.py`, `app/whatsapp.py` | `backend` |
| `docker-compose.yml`, `Dockerfile` | Full rebuild: `docker compose up --build` |
| `requirements.txt` | Full rebuild: `docker compose up --build` |
| `alembic/` migrations | Run `alembic upgrade head` after restart |

## Full Rebuild (when needed)

If dependencies or Docker configuration changed, do a full rebuild instead:

```bash
docker compose down && docker compose up --build
```

## Quick Reference

```bash
# Standard restart after code changes
docker compose restart backend worker && docker compose up

# Full rebuild (Dockerfile or requirements.txt changed)
docker compose down && docker compose up --build

# Check logs after restart
docker compose logs -f backend
docker compose logs -f worker
```
