"""
api/app/routers/knowledge.py

Knowledge base management and vector similarity search endpoints.

Endpoints:
  - POST /knowledge/documents: Ingests/replaces a document (.md, .txt, .pdf) with chunks & vectors (201).
  - GET /knowledge/documents: Lists all ingested documents ordered by update time.
  - DELETE /knowledge/documents/{id}: Deletes a document and cleans up its vector embeddings (204).
  - POST /knowledge/search: Performs semantic similarity search returning top ranked hits.
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.orm import DocTypeEnum
from app.models.schemas import (
    DocumentOut,
    DocumentUploadResponse,
    KnowledgeVersionResponse,
    SearchQueryRequest,
    SearchResponse,
)
from app.services.cache import get_kb_version
from app.services.knowledge import (
    delete_document,
    ingest_document,
    list_documents,
    search_knowledge,
)

router = APIRouter()


@router.get(
    "/version",
    response_model=KnowledgeVersionResponse,
    summary="Get knowledge base version hash",
)
async def get_knowledge_version() -> KnowledgeVersionResponse:
    """Returns the current 16-character SHA-256 hash of the knowledge base documents."""
    return KnowledgeVersionResponse(kb_version=get_kb_version())


@router.post(
    "/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and index a knowledge document",
)
async def upload_document(
    file: UploadFile = File(..., description="Document file (.md, .markdown, .txt, .pdf up to 10 MB)"),
    doc_type: DocTypeEnum = Form(..., description="Classification category: architecture, runbook, postmortem"),
    session: AsyncSession = Depends(get_session),
) -> DocumentUploadResponse:
    """
    Uploads and indexes a new document or updates an existing document with the same filename.

    - Extracts and normalises text (validates PDF page count and text extractability).
    - Chunks text respecting sections and preserving code blocks atomically.
    - Generates embeddings locally using SentenceTransformers.
    - Synchronises PostgreSQL records and the FAISS vector index atomically.
    """
    return await ingest_document(file=file, doc_type=doc_type, session=session)


@router.get(
    "/documents",
    response_model=List[DocumentOut],
    summary="List all ingested documents",
)
async def get_documents(
    session: AsyncSession = Depends(get_session),
) -> List[DocumentOut]:
    """
    Returns metadata for all ingested documents ordered newest first.
    """
    return await list_documents(session=session)


@router.delete(
    "/documents/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and its embeddings",
)
async def remove_document(
    id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """
    Deletes the document and its associated chunks from PostgreSQL and FAISS.
    """
    await delete_document(doc_id=id, session=session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Search knowledge base using semantic similarity",
)
async def search_documents(
    request: SearchQueryRequest,
    session: AsyncSession = Depends(get_session),
) -> SearchResponse:
    """
    Performs vector similarity search across ingested document chunks.

    Returns ranked chunks with similarity scores and metadata.
    """
    return await search_knowledge(request=request, session=session)


__all__ = ["router"]
