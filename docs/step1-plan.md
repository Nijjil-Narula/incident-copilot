# Step 1 — Project Scaffold Plan (Corrected)

> This is the authoritative record of what was built in Step 1.
> Corrections from the amendment pass are already incorporated.

## Final Folder Structure

```
incident-copilot/
├── .dockerignore
├── .env.example                  # Committed — placeholder values only
├── .gitignore
├── docker-compose.yml            # 4 services + 3 named volumes + healthchecks
│
├── docs/
│   └── step1-plan.md             # This file
│
├── api/
│   ├── Dockerfile                # python:3.11-slim, non-root user, no curl
│   ├── requirements.txt          # fastapi, uvicorn, asyncpg, sqlalchemy[asyncio], …
│   └── app/
│       ├── main.py               # FastAPI factory + lifespan context manager
│       ├── config.py             # Pydantic Settings; database_url uses asyncpg
│       ├── routers/
│       │   ├── __init__.py
│       │   └── health.py         # GET /health  GET /config
│       ├── services/
│       │   ├── __init__.py
│       │   └── placeholder.py
│       └── models/
│           ├── __init__.py
│           └── placeholder.py
│
└── ui/
    ├── Dockerfile                # python:3.11-slim, non-root user
    ├── requirements.txt          # streamlit, requests
    └── app/
        └── main.py               # Streamlit skeleton; reads API_URL env var
```

**Total: 17 files** (excluding this plan document)

---

## Files by Subagent

| # | File | Subagent |
|---|------|----------|
| 1 | `docker-compose.yml` | Infra |
| 2 | `.env.example` | Infra |
| 3 | `.gitignore` | Infra |
| 4 | `.dockerignore` | Infra |
| 5 | `api/Dockerfile` | API |
| 6 | `api/requirements.txt` | API |
| 7 | `api/app/main.py` | API |
| 8 | `api/app/config.py` | API |
| 9 | `api/app/routers/__init__.py` | API |
| 10 | `api/app/routers/health.py` | API |
| 11 | `api/app/services/__init__.py` | API |
| 12 | `api/app/services/placeholder.py` | API |
| 13 | `api/app/models/__init__.py` | API |
| 14 | `api/app/models/placeholder.py` | API |
| 15 | `ui/Dockerfile` | UI |
| 16 | `ui/requirements.txt` | UI |
| 17 | `ui/app/main.py` | UI |

---

## Design Decisions (Post-Amendment)

| Topic | Decision |
|---|---|
| **DB driver** | `asyncpg` + `sqlalchemy[asyncio]`; `database_url` uses `postgresql+asyncpg://` |
| **Ollama host** | `http://host.docker.internal:11434` — reaches Ollama on the Windows host from inside Docker |
| **Lifespan** | `@asynccontextmanager` lifespan passed to `FastAPI(lifespan=...)` — replaces deprecated `@app.on_event` |
| **UI env var** | `API_URL` (not `API_BASE_URL`); only injected via `environment:` block, never via `env_file:` |
| **Tab docstrings** | Replaced bare triple-quoted strings in Streamlit `with tab_*:` blocks with `#` comments |
| **Config caching** | `get_cached_config` only caches on success; exception propagates and fallback is applied at call site |
| **Healthchecks** | Use `python -c "import urllib.request; ..."` — no `curl` in `python:3.11-slim` |
| **API memory** | `API_MEM_LIMIT=2g` default (embedding model loads ~130 MB weights + runtime overhead in Step 2) |
| **UI memory** | `UI_MEM_LIMIT=512m` default (up from 256 m) |
| **Redis config** | `--maxmemory 128mb --maxmemory-policy allkeys-lru --appendonly yes` |
| **Port binding** | `127.0.0.1:PORT:PORT` for api and ui; postgres and redis never published |
| **Secrets isolation** | `ui` has no `env_file:`; gets only `API_URL` via `environment:` |
| **Non-root containers** | Both Dockerfiles add `appuser` and switch with `USER appuser` |
| **`.env.example`** | No `DATABASE_URL` key; comment warns against adding one; `POSTGRES_PASSWORD` left blank |

---

## How to Start the Stack

```bash
# 1. Copy the env template and fill in secrets
cp .env.example .env
# Edit .env: set GEMINI_API_KEY, POSTGRES_PASSWORD, etc.

# 2. Build images and start all services
docker compose up --build

# 3. Verify
#   API health : http://localhost:8000/health
#   API config : http://localhost:8000/config
#   API docs   : http://localhost:8000/docs
#   Streamlit  : http://localhost:8501
```
