# Step 2 Plan — Database Layer + Incident API + Minimal UI (Final, with Amendments)

## Files Created / Edited

| Action | File | Notes |
|--------|------|-------|
| CREATE | `api/app/db/__init__.py` | package marker |
| CREATE | `api/app/db/session.py` | async engine, sessionmaker (`expire_on_commit=False`), `get_session` dependency (yields, rolls back on error, does NOT commit) |
| CREATE | `api/app/models/orm.py` | `Base`, `Incident`, `Message` ORM models |
| CREATE | `api/app/models/schemas.py` | Pydantic v2 schemas with `from_attributes=True` |
| DELETE | `api/app/models/placeholder.py` | replaced by orm.py |
| KEEP   | `api/app/services/placeholder.py` | NOT deleted |
| CREATE | `api/app/routers/incidents.py` | 4 incident endpoints |
| EDIT   | `api/app/routers/health.py` | adds `GET /health/ready` |
| EDIT   | `api/app/main.py` | lifespan DB init (5-try retry), incidents router, global 500 handler |
| EDIT   | `ui/app/main.py` | sidebar list, create form, chat view |

## Table Schemas

### `incidents`
| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID | PK, `default=uuid4()` in Python |
| `title` | VARCHAR(200) | NOT NULL, stripped + 3–200 chars |
| `description` | TEXT | nullable |
| `severity` | ENUM `low\|medium\|high\|critical` | NOT NULL, default `medium` |
| `status` | ENUM `active\|resolved` | NOT NULL, default `active` |
| `created_at` | TIMESTAMPTZ | NOT NULL, `server_default=now()` |
| `updated_at` | TIMESTAMPTZ | NOT NULL, `server_default=now()`, `onupdate=now()` |
| `resolved_at` | TIMESTAMPTZ | nullable |
| `postmortem_md` | TEXT | nullable (Step 3+) |

### `messages`
| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID | PK, `default=uuid4()` in Python |
| `incident_id` | UUID | FK → `incidents.id` ON DELETE CASCADE, indexed |
| `role` | ENUM `user\|assistant\|system` | NOT NULL |
| `content` | TEXT | NOT NULL, min 1 char |
| `meta` | JSONB | nullable (response_ms, source_docs, cache_status — Step 3+). Named `meta`, never `metadata`. |
| `created_at` | TIMESTAMPTZ | NOT NULL, `server_default=now()` |

## API Contract

| Method | Path | Body | Response | Notes |
|--------|------|------|----------|-------|
| POST | `/incidents` | `{title, description?, severity?}` | 201 + IncidentOut | title stripped, 3–200 chars |
| GET | `/incidents` | — (query: `status?`, `limit=50`, `offset=0`) | list[IncidentOut], newest first | limit 1–200, offset ≥ 0 |
| GET | `/incidents/{id}` | — | IncidentDetail (+ messages asc) | 404 if missing; 422 if malformed UUID |
| POST | `/incidents/{id}/messages` | `{role, content, meta?}` | 201 + MessageOut | 404 if incident missing; content non-empty |
| GET | `/health` | — | `{status, service}` | unchanged liveness probe |
| GET | `/health/ready` | — | `{status, db}` | 200 ok / 503 unreachable |

## Key Amendments Applied

1. `expire_on_commit=False` on sessionmaker; `await session.refresh(obj)` after every commit that inserts/updates.
2. GET `/{id}` messages loaded via a separate ordered query (no lazy loading anywhere).
3. `get_session` only yields + rolls back. Endpoints commit explicitly.
4. `services/placeholder.py` kept; only `models/placeholder.py` deleted.
5. UI: `+ New Incident` button in sidebar; `timeout=10` + `try/except` on all API calls; `st.rerun()` after create.
6. Validation: `limit` 1–200, `offset ≥ 0`, `status` optional enum, UUID path params, title stripped.
7. Enums: lowercase names = values, stored as native Postgres ENUMs.
8. Global `Exception` handler → 500 `{"detail": "Internal server error"}`, logged server-side.
9. Saved as `docs/step2-plan.md`.
