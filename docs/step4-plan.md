# Step 4 Plan — Answer Pipeline (Amended & Approved)

> Authoritative specification for Step 4 implementation incorporating all safety, runtime, and robustness amendments.

---

## 1. Overview & System Design

Step 4 connects the knowledge base to the LLM to provide on-call engineers with fast, grounded, and safe incident triage answers:
1. **Redis Cache with 30s Circuit Breaker & Single-Flight Locking**:
   - Cache key: `answer:` + SHA-256 of `f"{kb_version}|{provider}|{model}|{prompt_version}|{normalized_question}"`.
   - Redis socket timeout: 2.0s. Fail-open with a **30-second circuit breaker**: any connection error or timeout trips the breaker, bypassing Redis for 30 seconds without wasting 2 seconds per query.
   - Single-flight per-key `asyncio.Lock` with **waiter reference-counting** ensures identical concurrent questions make only one LLM call, and locks are cleaned up when no waiters remain.
2. **Knowledge Base Version (`kb_version`)**:
   - 16-char hex hash of sorted `doc_type:filename:sha256` lines.
   - Maintained in memory, recomputed on startup and after every successful upload, replace, or delete.
   - Exposed via `GET /knowledge/version`.
3. **Database Connection Lifespan During Calls**:
   - Read transaction checks incident existence and active status, then immediately releases the DB session.
   - RAG retrieval, cache check, and LLM call run **without holding any open database connection**.
   - A fresh DB session is used to persist both user and assistant messages in a single transaction. If the incident was deleted during generation, returns 404.
4. **Resilient LLM Provider (Direct REST via `httpx`)**:
   - Model ID: Default to `gemini-3.5-flash` (or `gemini-2.5-flash`), Google's current active Flash series.
   - Default `LLM_MAX_OUTPUT_TOKENS` = 2048.
   - For models starting with `gemini-2.5` or supporting `thinkingConfig`, send `generationConfig.thinkingConfig = {"thinkingBudget": 0}` to prevent thinking tokens from starving answer generation.
   - Blank `GEMINI_API_KEY` logs a warning at startup and does NOT halt the container; `/ask` returns 503 while `/knowledge/documents` and `/knowledge/search` remain fully functional.
   - Empty response or non-STOP finishReason (e.g. `SAFETY`, `MAX_TOKENS` without text) is treated as an LLM error (503), never an answer.
   - `httpx`, `httpcore`, and `google_genai` loggers set to `WARNING` to prevent credential/prompt leakage.
5. **Whole-Command Matching Guardrail**:
   - **Layer 1 (Prompt)**: Strict instruction to only output CLI commands verbatim from `RUNBOOK` chunks. Context blocks neutralize `>>>` and `<<<` sequences.
   - **Layer 2 (Deterministic Whole-Command Verification)**:
     - Candidate extraction:
       (a) Fenced code blocks (` ``` ` / `~~~`), ignoring comments and blank lines, joining backslash-continued lines.
       (b) Prose lines outside code fences: lines that (after stripping list markers like `-, *, 1., 1)`) start with a known executable token.
       (c) Inline code spans where first token is a known executable or matches the first token of a command line in retrieved runbook chunks.
     - Whole-command matching rule: A candidate is verified ONLY if it equals a complete runbook command after whitespace normalization, OR if it is a contiguous run of whole tokens that starts at a token boundary AND ends at the end of a complete runbook command (dropping leading words like `sudo` is tolerated, but truncating the end like `rm -rf /` vs `rm -rf /tmp/cache` is strictly rejected).
     - Unverified commands are replaced with `[UNVERIFIED COMMAND REMOVED]`.
     - Destructive commands (`rm -rf`, `kubectl delete`, `drop table`, `truncate`, `flushall`, etc.) flagged for caution badges.
   - Runnable self-test suite: `python -m app.services.guardrail_selftest`.
6. **Chat UI (Streamlit)**:
   - `st.chat_input` for active incidents with 90s spinner.
   - Badges for Redis hits, response latency, token usage, verified source citations, guardrail warnings, and destructive command cautions.
   - Resolved incidents display `st.info` with input disabled.

---

## 2. Files to Create or Edit

| Action | Path | Description |
|---|---|---|
| **CREATE** | `api/app/services/cache.py` | Redis connection, fail-open cache with 30s circuit breaker, `kb_version` manager |
| **CREATE** | `api/app/services/llm.py` | `LLMProvider` interface, `GeminiProvider` (REST with retries, thinking budget 0, 2048 token limit), `OllamaProvider` stub |
| **CREATE** | `api/app/services/prompts.py` | System prompt, user message formatting with `>>>` sanitization, `PROMPT_VERSION = "v2"` |
| **CREATE** | `api/app/services/guardrail.py` | Whole-command verification, prose line scan, token boundary matching, destructive command detection |
| **CREATE** | `api/app/services/guardrail_selftest.py` | Standalone verification test suite (PASS/FAIL tests) |
| **CREATE** | `api/app/services/answer.py` | Orchestrates single-flight lock with reference counting, cache, retrieval, LLM, guardrail, message saving |
| **EDIT** | `api/app/models/schemas.py` | Add `AskQuestionRequest`, `AskQuestionResponse`, `KnowledgeVersionResponse` |
| **EDIT** | `api/app/models/__init__.py` | Re-export new schemas |
| **EDIT** | `api/app/routers/incidents.py` | Add `POST /incidents/{id}/ask` with disconnected DB lifespan |
| **EDIT** | `api/app/routers/knowledge.py` | Add `GET /knowledge/version` |
| **EDIT** | `api/app/services/knowledge.py` | Trigger `recompute_kb_version` after upload, replace, or delete |
| **EDIT** | `api/app/config.py` | Add `gemini_model`, `llm_timeout_s`, `llm_max_output_tokens=2048`, `llm_temperature`, `retrieval_k`, `retrieval_min_score=0.45`, `answer_cache_ttl_s` |
| **EDIT** | `.env.example` | Add new config keys with placeholder comments |
| **EDIT** | `api/app/main.py` | Lifespan: initialize Redis & `kb_version`, initialize LLM provider (warning if no key), configure loggers to WARNING |
| **EDIT** | `ui/app/main.py` | Add chat input, meta badges (Redis cache hit, latency, tokens), source citations, guardrail warnings, resolved incident state |

---

## 3. Configuration & Model Selection

- **Gemini Model**: `gemini-3.5-flash` (or `gemini-2.5-flash`), Google's current active Flash generation.
- **Tokens**: `LLM_MAX_OUTPUT_TOKENS=4096` default.
- **Thinking Budget**: Disabled (`thinkingBudget=0`) for models starting with `gemini-2.5` or supporting it, to ensure full token allowance goes to visible answers.
- **Retrieval Threshold**: `RETRIEVAL_MIN_SCORE=0.45` default (easy to tune via `.env`).
- **PROMPT_VERSION**: Set to `"v3"` reflecting the tightened whole-command guardrail rules.
- **Zero New Dependencies**: Uses existing `httpx==0.27.*` for direct REST API calls.

---

## 4. Endpoint Contract

### `POST /incidents/{id}/ask`
- **Request**: `{"question": "string (1-4000 characters, stripped, non-empty)"}`
- **Response** (`200 OK`):
  ```json
  {
    "user_message": {
      "id": "uuid",
      "incident_id": "uuid",
      "role": "user",
      "content": "Why is database pool exhausted?",
      "meta": null,
      "created_at": "timestamp"
    },
    "assistant_message": {
      "id": "uuid",
      "incident_id": "uuid",
      "role": "assistant",
      "content": "Check connection pool metrics using...",
      "meta": {
        "cache_status": "hit | miss | bypass",
        "guardrail": "pass | redacted | no_context",
        "response_ms": 342.5,
        "retrieval_ms": 18.2,
        "llm_ms": 280.1,
        "provider": "gemini",
        "model": "gemini-3.5-flash",
        "kb_version": "a1b2c3d4e5f67890",
        "tokens_in": 1420,
        "tokens_out": 215,
        "sources": [
          {
            "n": 1,
            "chunk_id": 42,
            "filename": "database_runbook.md",
            "doc_type": "runbook",
            "section": "Runbook > Connection Pool",
            "score": 0.865
          }
        ],
        "unverified_commands": [],
        "raw_answer": null,
        "destructive_commands": []
      },
      "created_at": "timestamp"
    }
  }
  ```
- **Error States**:
  - `404`: Incident ID does not exist.
  - `409`: Incident is in `resolved` status (`"Cannot ask questions in a resolved incident"`).
  - `422`: Validation error on question.
  - `503`: `{"detail": "The language model is unavailable or rate limited. Try again shortly."}` if LLM fails or API key is not configured.

### `GET /knowledge/version`
- **Response** (`200 OK`): `{"kb_version": "a1b2c3d4e5f67890"}`

---

## 5. Amendments Checklist (Items 1–13)

1. **DONE** — Whole-Command Matching Guardrail: Replaced substring verification with whole-command matching (`is_verified_command`) requiring exact normalized match or whole-token end-anchored match. Chunks of doc_type != runbook never verify commands.
2. **DONE** — Prose Scanning: Added `_process_prose_line` to scan lines outside code fences, strip list markers (`-`, `*`, `1.`, `1)`), detect known executables, and replace unverified commands with `[UNVERIFIED COMMAND REMOVED]`.
3. **DONE** — Gemini Provider Robustness: `LLM_MAX_OUTPUT_TOKENS` defaulted to 2048; `thinkingBudget: 0` sent for `gemini-2.5` models; raises `LLMUnavailableError` (503) on `SAFETY`, `RECITATION`, `OTHER` or empty response; sets `meta["truncated"] = True` on `MAX_TOKENS`.
4. **DONE** — Active Gemini Model Verification: Model configured as `gemini-3.5-flash` in `config.py` and `.env.example`.
5. **DONE** — Disconnected DB Session Lifespan: Read session verifies incident and is closed before invoking RAG/LLM pipeline; a fresh write session saves user and assistant messages in a single transaction (404 if deleted in the meantime).
6. **DONE** — Missing API Key Graceful Degradation: Blank `GEMINI_API_KEY` logs a warning at startup and raises `LLMUnavailableError` (returning 503 on `/ask`), leaving knowledge ingestion and search endpoints fully functional.
7. **DONE** — Retrieval Score Tuning: Calibrated `RETRIEVAL_MIN_SCORE` default to 0.45 in `config.py` and `.env.example` to prevent false positive context retrievals.
8. **DONE** — Redis Circuit Breaker & Lifespan Close: Implemented 30-second circuit breaker cooldown in `cache.py` to bypass Redis on failure; cleanly closed Redis client in FastAPI lifespan shutdown.
9. **DONE** — Context Delimiter Sanitization: Neutralized `>>>` and `<<<` sequences in chunk text within `prompts.py` before building prompt.
10. **DONE** — Prompt & Cache Versioning: Set `PROMPT_VERSION = "v2"` incorporated into cache key SHA-256 so guardrail/prompt updates automatically invalidate stale cached answers.
11. **DONE** — Single-Flight Lock Waiter Cleanup: In-memory `single_flight_lock` tracks waiter count per cache key and deletes the lock entry when waiter count reaches 0.
12. **DONE** — Direct REST Route with Zero New Dependencies: Implemented via existing `httpx` with timeout and 2s retry backoff on 429/5xx, avoiding dependency version conflicts.
13. **DONE** — 12-Case Guardrail Self-Test Suite: Implemented 12 test cases in `api/app/services/guardrail_selftest.py` covering all edge cases, exiting non-zero (`sys.exit(1)`) on any failure.
