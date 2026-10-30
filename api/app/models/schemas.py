"""
api/app/models/schemas.py

Pydantic v2 request and response schemas for the Incident Copilot API.

Defines schemas for:
  - Incident tickets and Message records.
  - Document upload, metadata listing, and vector similarity search.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.orm import DocTypeEnum, RoleEnum, SeverityEnum, StatusEnum


class IncidentCreate(BaseModel):
    """Payload for creating a new incident."""

    title: str = Field(
        ...,
        min_length=3,
        max_length=200,
        description="Incident title (3-200 characters).",
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional detailed description of the incident.",
    )
    severity: SeverityEnum = Field(
        default=SeverityEnum.medium,
        description="Severity classification of the incident.",
    )

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: Any) -> Any:
        """Strip leading and trailing whitespace from the incident title."""
        if isinstance(v, str):
            v = v.strip()
        return v


class IncidentOut(BaseModel):
    """Response schema representing an incident ticket."""

    id: uuid.UUID
    title: str
    description: Optional[str] = None
    severity: SeverityEnum
    status: StatusEnum
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None
    postmortem_md: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MessageCreate(BaseModel):
    """Payload for appending a message to an incident investigation thread."""

    role: RoleEnum = Field(
        ...,
        description="Role of the message sender (user, assistant, or system).",
    )
    content: str = Field(
        ...,
        min_length=1,
        description="Message content (non-empty string).",
    )
    meta: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional structured metadata (e.g., token usage, retrieved source docs).",
    )

    @field_validator("content", mode="before")
    @classmethod
    def strip_content(cls, v: Any) -> Any:
        """Strip leading and trailing whitespace from the message content."""
        if isinstance(v, str):
            v = v.strip()
        return v


class MessageOut(BaseModel):
    """Response schema representing a chat message."""

    id: uuid.UUID
    incident_id: uuid.UUID
    role: RoleEnum
    content: str
    meta: Optional[Dict[str, Any]] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class IncidentDetail(IncidentOut):
    """Detailed response schema for an incident including its full message thread."""

    messages: List[MessageOut] = Field(
        default_factory=list,
        description="Chronological list of messages in this incident thread.",
    )

    model_config = ConfigDict(from_attributes=True)


class DocumentOut(BaseModel):
    """Response schema for a single ingested document summary."""

    id: uuid.UUID
    filename: str
    doc_type: DocTypeEnum
    chunk_count: int
    char_count: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentUploadResponse(BaseModel):
    """Response schema returned after successfully uploading and indexing a document."""

    id: uuid.UUID
    filename: str
    doc_type: DocTypeEnum
    chunk_count: int
    char_count: int
    replaced: bool = Field(
        default=False,
        description="Whether an existing document with the same filename was replaced",
    )


class SearchQueryRequest(BaseModel):
    """Request payload for semantic similarity search across ingested knowledge documents."""

    query: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Query text to search for relevant document chunks.",
    )
    k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of top results to retrieve (1-20).",
    )
    doc_type: Optional[DocTypeEnum] = Field(
        default=None,
        description="Optional document type filter.",
    )


class SearchHit(BaseModel):
    """A single retrieved chunk matching a search query."""

    chunk_id: int
    document_id: uuid.UUID
    filename: str
    doc_type: DocTypeEnum
    section: Optional[str] = None
    score: float
    text: str


class SearchResponse(BaseModel):
    """Response payload for semantic search containing ranked hits and search timing."""

    hits: List[SearchHit] = Field(default_factory=list)
    search_ms: float


class AskQuestionRequest(BaseModel):
    """Payload for asking a triage question within an incident investigation."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Triage question",
    )

    @field_validator("question", mode="before")
    @classmethod
    def strip_question(cls, v: Any) -> Any:
        """Strip leading and trailing whitespace from the question."""
        if isinstance(v, str):
            v = v.strip()
        return v


class AskQuestionResponse(BaseModel):
    """Response schema returned after answering an incident triage question."""

    user_message: MessageOut
    assistant_message: MessageOut


class KnowledgeVersionResponse(BaseModel):
    """Response schema returning the current knowledge base version hash."""

    kb_version: str


class ResolveIncidentRequest(BaseModel):
    """Payload for resolving an incident and triggering post-mortem generation."""

    resolution_summary: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Optional resolution summary from engineer",
    )
    generate_postmortem: bool = Field(
        default=True,
        description="Whether to generate an automated post-mortem report",
    )

    @field_validator("resolution_summary", mode="before")
    @classmethod
    def clean_resolution_summary(cls, v: Any) -> Optional[str]:
        """Strips leading/trailing whitespace and returns None if empty."""
        if isinstance(v, str):
            stripped = v.strip()
            return stripped if stripped else None
        return v


class PublishPostmortemResponse(BaseModel):
    """Response schema returned after publishing a post-mortem to the knowledge base."""

    document_id: uuid.UUID
    filename: str
    doc_type: str = "postmortem"
    chunk_count: int
    replaced: bool


__all__ = [
    "DocTypeEnum",
    "IncidentCreate",
    "IncidentOut",
    "IncidentDetail",
    "MessageCreate",
    "MessageOut",
    "DocumentOut",
    "DocumentUploadResponse",
    "SearchQueryRequest",
    "SearchHit",
    "SearchResponse",
    "AskQuestionRequest",
    "AskQuestionResponse",
    "KnowledgeVersionResponse",
    "ResolveIncidentRequest",
    "PublishPostmortemResponse",
]
