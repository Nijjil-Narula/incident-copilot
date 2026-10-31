# Data Flow & Privacy Architecture

This document defines the strict data boundaries, storage locations, and external network transmissions for **Incident Copilot**.

---

## 1. What Leaves the Server (External Transmissions)

Incident Copilot is designed with minimal outbound egress. Only two potential external destinations exist:

### 1.1 Google Gemini API (`generativelanguage.googleapis.com`)
When an engineer asks a question or generates an incident post-mortem:
- **What is transmitted**:
  1. The user's prompt / triage question text.
  2. The text content of retrieved relevant runbook/architecture chunks (typically top 6 chunks).
  3. Formatted incident transcripts and resolution summaries (for post-mortems only).
- **What is NEVER transmitted**:
  - Raw database passwords or database connection strings.
  - Internal network IP addresses or infrastructure topology outside cited chunks.
  - Unrelated incident tickets or document files not selected by semantic retrieval.

> **CRITICAL LEGAL & PRIVACY NOTICE**:
> If you utilize Google AI Studio free-tier API keys, Google's terms may permit human review or use of prompts to train future models.
> **The instance owner must review and verify Google's current API terms of service and data-use policies before uploading real proprietary company data, passwords, or customer PII.**
> For strict zero-data-retention compliance, enterprise teams must use Google Cloud Vertex AI with data logging disabled or self-hosted offline LLM backends (such as Ollama).

### 1.2 LangSmith Observability (`api.smith.langchain.com`)
- **Default State**: **DISABLED** (`LANGSMITH_TRACING=false`).
- **When Enabled**: Only if `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` is explicitly configured.
- **What is transmitted**:
  - LLM input prompts and model completion texts.
  - Retrieval hit scores, latency timings, and metadata tags (`kb_version`, `guardrail:pass`).
  - Stack traces for failed pipeline executions.

---

## 2. What Stays on the Server (Local Isolation)

All core data processing and retrieval pipelines remain completely contained on the self-hosted virtual machine:

### 2.1 Local CPU Vector Embeddings
- The embedding model (`BAAI/bge-small-en-v1.5`) runs **100% locally** on the server's ARM CPU via PyTorch and SentenceTransformers.
- Uploaded architectural specifications, runbooks, and manuals are **never sent to a third-party embedding API** (such as OpenAI or Cohere). All text chunking and vector calculations happen in local RAM.

### 2.2 Local Relational & Cache Storage
- **PostgreSQL 16**: Incident titles, descriptions, investigation chat messages, document hashes, and chunk texts are stored solely in the local named Docker volume `incident_copilot_postgres_data`.
- **Redis 7**: Q&A cache responses are cached locally with SHA-256 keys in `incident_copilot_redis_data` and are never shared externally.
- **FAISS Vector Store**: Vector indices and ID mappings are written to `incident_copilot_faiss_index` on the host filesystem.

---

## 3. Data Flow Diagram

```mermaid
flowchart TD
    Client([User Browser]) -->|HTTPS + Basic Auth| Caddy[Caddy Reverse Proxy :443]
    Caddy -->|Internal HTTP| UI[Streamlit UI :8501]
    UI -->|Internal REST| API[FastAPI Core :8000]

    subgraph Local Server Boundary
        API -->|Local Text Chunking| Chunker[Chunker Engine]
        API -->|Local CPU Inference| BGE["SentenceTransformers (BAAI/bge-small-en-v1.5)"]
        API -->|Local Vector Search| FAISS[(FAISS Index)]
        API -->|Durable Persistence| PG[(PostgreSQL 16)]
        API -->|Answer Cache| Redis[(Redis 7)]
    end

    subgraph External Cloud Services
        API -.->|Prompts + Retrieved Chunks| Gemini["Google Gemini API (gemini-3.5-flash)"]
        API -.->|Traces (Only if LANGSMITH_TRACING=true)| LS["LangSmith Observability"]
    end
```
