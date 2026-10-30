"""
api/app/services/__init__.py

Package marker for the services module.

Exports services for:
  - Document chunking and normalization (chunker.py)
  - SentenceTransformers embeddings (embeddings.py)
  - In-memory & persisted FAISS vector index (vector_store.py)
  - Knowledge ingestion, deletion, listing, and semantic search (knowledge.py)
"""

from app.services.answer import process_question
from app.services.cache import (
    get_cached_answer,
    get_kb_version,
    recompute_kb_version,
    set_cached_answer,
    single_flight_lock,
)
from app.services.chunker import DocumentChunk, chunk_document, extract_pdf, normalize_text
from app.services.embeddings import EmbeddingService, embedding_service, get_embedding_service
from app.services.guardrail import (
    GuardrailResult,
    extract_runbook_commands,
    is_verified_command,
    verify_guardrail,
)
from app.services.knowledge import delete_document, ingest_document, list_documents, search_knowledge
from app.services.llm import (
    GeminiProvider,
    LLMProvider,
    LLMResult,
    LLMUnavailableError,
    OllamaProvider,
    get_llm_provider,
)
from app.services.prompts import PROMPT_VERSION, SYSTEM_PROMPT, format_user_prompt
from app.services.vector_store import VectorStore, get_vector_store, vector_store

__all__ = [
    "DocumentChunk",
    "normalize_text",
    "extract_pdf",
    "chunk_document",
    "EmbeddingService",
    "embedding_service",
    "get_embedding_service",
    "VectorStore",
    "vector_store",
    "get_vector_store",
    "ingest_document",
    "delete_document",
    "list_documents",
    "search_knowledge",
    "process_question",
    "get_cached_answer",
    "set_cached_answer",
    "get_kb_version",
    "recompute_kb_version",
    "single_flight_lock",
    "verify_guardrail",
    "GuardrailResult",
    "extract_runbook_commands",
    "is_verified_command",
    "LLMUnavailableError",
    "LLMResult",
    "LLMProvider",
    "GeminiProvider",
    "OllamaProvider",
    "get_llm_provider",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "format_user_prompt",
]
