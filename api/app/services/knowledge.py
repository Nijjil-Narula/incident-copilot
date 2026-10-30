"""
api/app/services/knowledge.py

Knowledge ingestion and vector retrieval service for Incident Copilot.

Responsibilities:
  - Validates and streams document uploads (max 10 MB, allowed types: md, markdown, txt, pdf).
  - Normalises text, parses structure, chunks content, and computes embeddings.
  - Manages atomic document upload-replace and delete flows protected by a global ingest lock.
  - Executes semantic search joining FAISS similarity hits with PostgreSQL chunk and document metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from pathlib import PurePath
from typing import List, Optional

from fastapi import HTTPException, UploadFile, status
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.orm import Chunk, DocTypeEnum, Document
from app.models.schemas import (
    DocumentOut,
    DocumentUploadResponse,
    SearchHit,
    SearchQueryRequest,
    SearchResponse,
)
from app.services.cache import recompute_kb_version
from app.services.chunker import chunk_document, extract_pdf, normalize_text
from app.services.embeddings import get_embedding_service
from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_CHUNKS_PER_DOC = 2000
ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf"}

# Global ingest lock ensuring upload-replace and delete transactions are strictly serialized
ingest_lock = asyncio.Lock()


async def ingest_document_bytes(
    clean_filename: str,
    doc_type: DocTypeEnum,
    content_bytes: bytes,
    session: AsyncSession,
) -> DocumentUploadResponse:
    """
    Ingests and indexes document bytes directly.

    Extracts text (or PDF), chunks content, computes embeddings, and under ingest_lock,
    inserts or replaces the document and chunk records in PostgreSQL and synchronises FAISS.
    """
    if not clean_filename or len(clean_filename) > 255:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Filename must be non-empty and at most 255 characters",
        )

    ext = PurePath(clean_filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported file type '{ext}'. Supported formats: "
                f"{', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ),
        )

    if not content_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty",
        )

    sha256_hash = hashlib.sha256(content_bytes).hexdigest()

    # ── Extract & Normalise Text ───────────────────────────────────────────
    if ext == ".pdf":
        try:
            text = await asyncio.to_thread(extract_pdf, content_bytes)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    else:
        try:
            text = normalize_text(content_bytes)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    # ── Chunk Document ─────────────────────────────────────────────────────
    doc_chunks = await asyncio.to_thread(chunk_document, clean_filename, text)
    if not doc_chunks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Document contains no indexable content",
        )

    if len(doc_chunks) > MAX_CHUNKS_PER_DOC:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Document too large to index",
        )

    # ── Compute Embeddings ─────────────────────────────────────────────────
    embedding_service = get_embedding_service()
    embed_texts = [c.embed_text for c in doc_chunks]
    vectors = await embedding_service.embed_texts(embed_texts)

    # ── Under Ingest Lock: DB Insert / Replace & FAISS Sync ────────────────
    vector_store = get_vector_store()

    async with ingest_lock:
        # Check if existing document with same filename exists
        existing_doc_res = await session.execute(
            sa.select(Document).where(Document.filename == clean_filename)
        )
        old_doc = existing_doc_res.scalar_one_or_none()

        old_chunk_ids: List[int] = []
        replaced = False

        if old_doc is not None:
            replaced = True
            # Collect old chunk IDs to remove from FAISS
            old_chunks_res = await session.execute(
                sa.select(Chunk.id).where(Chunk.document_id == old_doc.id)
            )
            old_chunk_ids = list(old_chunks_res.scalars().all())

            # Delete existing document record before inserting new one
            # to prevent unique constraint collision on filename
            await session.execute(
                sa.delete(Document).where(Document.filename == clean_filename)
            )
            await session.flush()

        # Insert new Document record
        new_doc = Document(
            filename=clean_filename,
            doc_type=doc_type,
            sha256=sha256_hash,
            char_count=len(text),
            chunk_count=len(doc_chunks),
        )
        session.add(new_doc)
        await session.flush()

        # Insert Chunk records in sequential order
        db_chunks = [
            Chunk(
                document_id=new_doc.id,
                chunk_index=c.chunk_index,
                section=c.section,
                text=c.text,
            )
            for c in doc_chunks
        ]
        session.add_all(db_chunks)
        await session.flush()

        new_chunk_ids = [c.id for c in db_chunks]
        await session.commit()
        await session.refresh(new_doc)

        # Update FAISS index under vector store lock
        try:
            if old_chunk_ids:
                await vector_store.remove(old_chunk_ids)
            await vector_store.add(new_chunk_ids, vectors)
            await vector_store.save()
        except Exception as exc:
            logger.error(
                "FAISS update failed for document '%s': %s. Triggering automatic index rebuild...",
                clean_filename,
                exc,
            )
            await vector_store.rebuild_from_db(async_session_factory, embedding_service)

        await recompute_kb_version(async_session_factory)

        return DocumentUploadResponse(
            id=new_doc.id,
            filename=new_doc.filename,
            doc_type=new_doc.doc_type,
            chunk_count=new_doc.chunk_count,
            char_count=new_doc.char_count,
            replaced=replaced,
        )


async def ingest_document(
    file: UploadFile,
    doc_type: DocTypeEnum,
    session: AsyncSession,
) -> DocumentUploadResponse:
    """
    Ingests and indexes an uploaded document.

    Step 1: Validates filename, extension, and streams content enforcing 10 MB limit.
    Step 2: Delegates extraction, chunking, embedding, DB persistence, and FAISS indexing
            to ingest_document_bytes.
    """
    raw_name = file.filename or ""
    clean_filename = PurePath(raw_name).name.strip()
    if not clean_filename or len(clean_filename) > 255:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Filename must be non-empty and at most 255 characters",
        )

    ext = PurePath(clean_filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported file type '{ext}'. Supported formats: "
                f"{', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ),
        )

    chunks_data: List[bytes] = []
    total_bytes = 0

    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File size exceeds maximum allowed limit of 10 MB",
            )
        chunks_data.append(chunk)

    content_bytes = b"".join(chunks_data)
    if not content_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty",
        )

    return await ingest_document_bytes(
        clean_filename=clean_filename,
        doc_type=doc_type,
        content_bytes=content_bytes,
        session=session,
    )


async def delete_document(doc_id: uuid.UUID, session: AsyncSession) -> None:
    """
    Deletes a document and its chunks from PostgreSQL and FAISS.

    Raises:
        HTTPException 404: If the document is not found.
    """
    vector_store = get_vector_store()

    async with ingest_lock:
        doc = await session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Document not found",
            )

        # Query chunk IDs before deleting the document
        chunks_res = await session.execute(
            sa.select(Chunk.id).where(Chunk.document_id == doc.id)
        )
        chunk_ids = list(chunks_res.scalars().all())

        await session.delete(doc)
        await session.commit()

        # Remove chunk vectors from FAISS and save atomically
        if chunk_ids:
            await vector_store.remove(chunk_ids)
            await vector_store.save()

        await recompute_kb_version(async_session_factory)


async def list_documents(session: AsyncSession) -> List[DocumentOut]:
    """
    Returns all ingested documents ordered by most recently updated.
    """
    query = sa.select(Document).order_by(Document.updated_at.desc())
    result = await session.execute(query)
    documents = result.scalars().all()
    return [DocumentOut.model_validate(doc) for doc in documents]


async def search_knowledge(
    request: SearchQueryRequest,
    session: AsyncSession,
) -> SearchResponse:
    """
    Executes semantic similarity search over ingested document chunks.

    Steps:
      1. Embeds search query with query prefix on dedicated worker.
      2. Runs FAISS nearest neighbor search.
      3. Joins matching chunk IDs against PostgreSQL Chunk and Document records.
      4. Filters by doc_type if requested and returns top k hits sorted by score descending.
    """
    start_time = time.perf_counter()
    vector_store = get_vector_store()
    total_vectors = await vector_store.count()

    if total_vectors == 0:
        return SearchResponse(
            hits=[],
            search_ms=round((time.perf_counter() - start_time) * 1000, 2),
        )

    embedding_service = get_embedding_service()
    query_vector = await embedding_service.embed_query(request.query)

    # Over-fetch if doc_type filter is present to compensate for filtered-out hits
    fetch_k = min(request.k * 4, total_vectors) if request.doc_type else min(request.k, total_vectors)

    scores, ids = await vector_store.search(query_vector, fetch_k)

    # Filter out empty slots (FAISS returns -1 for empty / unindexed slots)
    valid_pairs = [(int(cid), float(sc)) for cid, sc in zip(ids, scores) if cid != -1]
    if not valid_pairs:
        return SearchResponse(
            hits=[],
            search_ms=round((time.perf_counter() - start_time) * 1000, 2),
        )

    chunk_ids = [cid for cid, _ in valid_pairs]
    score_lookup = dict(valid_pairs)

    # Join matching chunks with their parent documents in PostgreSQL
    stmt = (
        sa.select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.id.in_(chunk_ids))
    )
    rows = (await session.execute(stmt)).all()

    hits: List[SearchHit] = []
    for chunk, doc in rows:
        if request.doc_type is not None and doc.doc_type != request.doc_type:
            continue

        raw_score = score_lookup.get(chunk.id, 0.0)
        hits.append(
            SearchHit(
                chunk_id=chunk.id,
                document_id=doc.id,
                filename=doc.filename,
                doc_type=doc.doc_type,
                section=chunk.section,
                score=round(raw_score, 4),
                text=chunk.text,
            )
        )

    # Sort descending by similarity score and truncate to requested k
    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[: request.k]

    search_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return SearchResponse(hits=hits, search_ms=search_ms)


__all__ = [
    "ingest_document",
    "ingest_document_bytes",
    "delete_document",
    "list_documents",
    "search_knowledge",
    "ingest_lock",
]
