# Step 3 Plan — Knowledge Ingestion & Vector Search (Amended & Approved)

> Authoritative specification for Step 3 implementation incorporating all user amendments.

---

## 1. Architecture & Design Specifications

### Database Layer (`api/app/models/orm.py` & `api/app/models/schemas.py`)
- **Enum `DocTypeEnum`**: Native PostgreSQL enum `document_type` (`architecture`, `runbook`, `postmortem`).
- **Table `documents`**:
  - `id`: UUID primary key (`uuid.uuid4`)
  - `filename`: `VARCHAR(255)`, `UNIQUE`, `nullable=False`
  - `doc_type`: `DocTypeEnum`, `nullable=False`
  - `sha256`: `CHAR(64)`, `nullable=False` (computed on original raw file bytes)
  - `char_count`: `INTEGER`, `nullable=False`
  - `chunk_count`: `INTEGER`, `nullable=False`
  - `created_at`, `updated_at`: `TIMESTAMPTZ`, `server_default=func.now()`
- **Table `chunks`**:
  - `id`: `BIGINT` identity primary key (autoincrement). Directly matches the FAISS vector ID.
  - `document_id`: `UUID` foreign key to `documents.id` (`ondelete="CASCADE"`), indexed, `nullable=False`
  - `chunk_index`: `INTEGER`, `nullable=False`
  - `section`: `VARCHAR(500)`, nullable (e.g. `"System Architecture > Database"`)
  - `text`: `TEXT`, `nullable=False` (normalized raw chunk text verbatim from source)

### Text Normalization & Chunking (`api/app/services/chunker.py`)
- **Normalization**:
  - Decode input bytes using `utf-8-sig`. Raise HTTP 422 `"File is not valid UTF-8 text"` if decoding fails.
  - Convert `\r\n` and `\r` to `\n`. Store and chunk this normalized text.
  - Original raw bytes are used for `sha256` digest calculation.
- **Parsing Order**:
  1. Detect fenced code blocks (delimited by ` ``` ` or `~~~`) **FIRST**.
  2. Any line starting with `#` inside a code fence is strictly treated as code, never as a markdown heading.
  3. An unclosed code block reaching end-of-file is treated as code to the end.
  4. Non-code text is scanned for markdown headings (`#`, `##`, `###`, etc.) to track hierarchical section paths.
  5. Content within a section is packed greedily into chunks up to **~1200 characters**.
  6. Code blocks are **atomic**: never split inside a code block. An oversize code block becomes its own standalone chunk.
  7. Prose chunks may have an overlap of **~150 characters**. Overlap is strictly prohibited from containing any part of a code block.
  8. For plain text (`.txt`) files, split by paragraphs with the same 1200 char limit and ~150 char overlap (`section = None`).
  9. Embed text: `f"{filename} | {section}\n{text}"` (or `f"{filename}\n{text}"` if section is empty). Dropped whitespace-only chunks.

### Embeddings & Concurrency (`api/app/services/embeddings.py`)
- Singleton `EmbeddingService` wrapping `SentenceTransformer` on `device="cpu"`.
- Loaded once during FastAPI `lifespan` startup.
- Wrap model loading in `try/except` at startup: log a clear error naming the model id and `HF_HOME` path (`/app/hf_cache`), then re-raise to fail visibly.
- Query prefix: If `settings.embedding_query_prefix` is provided, use it; otherwise, if `"bge"` is in `settings.embedding_model.lower()`, default to `"Represent this sentence for searching relevant passages: "`.
- Encoding execution: All `encode` calls run on a dedicated `ThreadPoolExecutor(max_workers=1)` via `loop.run_in_executor`, ensuring only one encode runs at a time.
- Normalize embeddings: `normalize_embeddings=True`, `batch_size=32`, `float32` arrays.

### Vector Store & Consistency Sync (`api/app/services/vector_store.py`)
- `faiss.IndexIDMap2` wrapping `IndexFlatIP(dimension)`.
- Metadata file: `/app/faiss_store/index.meta.json` storing `{ "embedding_model": str, "dimension": int }`.
- Persisted atomically: write to `/app/faiss_store/index.faiss.tmp` and `index.meta.json.tmp`, then `os.replace`.
- Locking: Vector store has its own `asyncio.Lock` protecting **every** FAISS operation (including `search`, `add`, `remove`, `rebuild`).
- Single-worker comment: Document in code that uvicorn must run with a single worker process because in-memory FAISS and thread locks live in process memory.
- **Startup Consistency Check**:
  1. Inspect if `index.faiss` and `index.meta.json` exist.
  2. If present, verify `index.meta.json` has matching `embedding_model` and `dimension`.
  3. Read vector IDs using `faiss.vector_to_array(index.id_map)`.
  4. Compare vector count AND exact set of IDs against `chunks.id` in PostgreSQL.
  5. If missing or any mismatch, rebuild the entire FAISS index from PostgreSQL chunk records, write `index.faiss` and `index.meta.json`, and log `"rebuilt N vectors"`.
  6. If matching, load index and log `"loaded N vectors"`.

### Ingestion Service (`api/app/services/knowledge.py`)
- **Ingest Lock**: Module-level `asyncio.Lock` wrapping the entire upload-replace and delete flows (DB operations + FAISS operations together).
- **Upload Flow**:
  1. Stream upload in chunks; enforce 10 MB limit (HTTP 413 if exceeded).
  2. Extract text (for PDF: use `pypdf`, reject if > 300 pages with HTTP 422; reject if < 20 non-whitespace chars with HTTP 422 `"No extractable text (is this a scanned PDF?)"`). Run extraction & chunking in `asyncio.to_thread`.
  3. Chunk text; reject if chunk count > 2000 with HTTP 422 `"Document too large to index"`.
  4. Compute embeddings for all chunks via `EmbeddingService` on the dedicated executor.
  5. Database Transaction:
     - Check if document with same filename exists. If so, `select(Chunk.id).where(Chunk.document_id == old_doc.id)` to collect old chunk IDs.
     - Execute `delete(Document).where(Document.filename == clean_filename)` statement **before** inserting the new document/chunks to guarantee no UNIQUE filename constraint collision.
     - Insert new `Document` row.
     - Insert ordered list of new `Chunk` rows.
     - `await session.flush()` to populate generated chunk IDs.
     - `await session.commit()`, then `await session.refresh(document)`.
  6. FAISS Update:
     - Under vector store lock: remove old chunk IDs, add new vectors with chunk IDs, save index and metadata atomically.
     - On failure: log and execute rebuild-from-Postgres routine.
- **Delete Flow**:
  - Under ingest lock:
    - Look up document by ID. If missing, raise HTTP 404.
    - Query chunk IDs for document.
    - Delete document: `await session.delete(document)`, `await session.commit()`.
    - Under vector store lock: remove chunk IDs from FAISS and save atomically.

### API Contract (`api/app/routers/knowledge.py`)
- `POST /knowledge/documents`: Multipart form (`file`, `doc_type`). Returns 201 `{id, filename, doc_type, chunk_count, char_count, replaced: bool}`.
- `GET /knowledge/documents`: Returns list of `{id, filename, doc_type, chunk_count, char_count, updated_at}` newest first.
- `DELETE /knowledge/documents/{id}`: Returns 204 (404 if missing).
- `POST /knowledge/search`:
  - Body: `{query: str (1-1000 chars), k: int (1-20, default 5), doc_type: Optional[DocTypeEnum]}`.
  - Computes query embedding with query prefix on dedicated executor.
  - Takes vector store lock around FAISS search. Over-fetches `k * 4` if `doc_type` is specified, or `k` if not.
  - Skips any result with FAISS ID `-1`.
  - Joins against PostgreSQL chunks and documents. Skips any hit whose chunk is not found in Postgres.
  - Returns `{hits: [{chunk_id, document_id, filename, doc_type, section, score, text}], search_ms}`. Returns `hits: []` without 500 if index is empty.

### Dependencies & Environment
- `api/Dockerfile`:
  `RUN pip install --no-cache-dir torch==2.4.1 --index-url https://download.pytorch.org/whl/cpu`
  followed by `requirements.txt`.
  Create `/app/hf_cache` and chown to `appuser:appuser`.
- `api/requirements.txt`:
  ```
  fastapi==0.115.*
  uvicorn[standard]==0.30.*
  pydantic-settings==2.3.*
  sqlalchemy[asyncio]==2.0.*
  asyncpg==0.29.*
  redis==5.0.*
  langchain-core==0.2.*
  langsmith==0.1.*
  httpx==0.27.*
  numpy==1.26.4
  faiss-cpu==1.8.0.post1
  sentence-transformers==3.1.1
  transformers==4.44.2
  huggingface-hub==0.25.2
  scipy==1.13.1
  scikit-learn==1.5.2
  pypdf==5.1.0
  python-multipart==0.0.20
  ```
- `docker-compose.yml`:
  - Named volume `hf_cache` mounted at `/app/hf_cache`.
  - `HF_HOME: /app/hf_cache` in api environment.
  - `start_period: 120s` for api healthcheck.
- `.env.example` & `config.py`:
  - `EMBEDDING_QUERY_PREFIX=` (blank for default BGE prefix).

### UI Implementation (`ui/app/main.py`)
- **Admin Tab**:
  - Displays alert banner at the top if `st.session_state["admin_flash_message"]` is present, then clears it.
  - Upload section: `st.file_uploader`, `doc_type` selectbox, "Upload & Index" button with spinner. On success, sets flash message and calls `st.rerun()`.
  - Document table: Fetches `GET /knowledge/documents` freshly. Shows filename, doc_type badge, chunk count, formatted updated timestamp. Delete button per row with unique key `delete_doc_{id}`. On deletion, sets flash message and calls `st.rerun()`.
  - Search section: Test query input, `k` slider (1-10), `doc_type` filter, Search button. Displays hits in expanders with score, filename, section, and text.
