"""
api/app/models/orm.py

SQLAlchemy ORM models for Incident Copilot.

Defines the core database entities:
  - Base: Declarative base class for all ORM models.
  - SeverityEnum, StatusEnum, RoleEnum: Database enums for ticket severity,
    lifecycle status, and message sender role.
  - DocTypeEnum: Database enum for ingested document classification.
  - Incident: Represents an incident ticket, tracking status, triage metadata,
    and post-mortem documentation.
  - Message: Represents a chat exchange or system log entry associated with
    an incident.
  - Document: Ingested runbook, architecture, or postmortem document.
  - Chunk: Text chunk from a document, indexed by integer ID matching FAISS.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy ORM models."""

    pass


class SeverityEnum(str, enum.Enum):
    """Severity levels for incidents."""

    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class StatusEnum(str, enum.Enum):
    """Lifecycle status of an incident."""

    active = "active"
    resolved = "resolved"


class RoleEnum(str, enum.Enum):
    """Message sender role within an incident investigation thread."""

    user = "user"
    assistant = "assistant"
    system = "system"


class DocTypeEnum(str, enum.Enum):
    """Document classification types for runbooks, architecture, and postmortems."""

    architecture = "architecture"
    runbook = "runbook"
    postmortem = "postmortem"


class Incident(Base):
    """
    SQLAlchemy ORM model representing an incident ticket.

    Tracks incident details, severity, status, and generated post-mortem.
    """

    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    severity: Mapped[SeverityEnum] = mapped_column(
        sa.Enum(SeverityEnum, name="incident_severity", native_enum=True),
        nullable=False,
        default=SeverityEnum.medium,
    )
    status: Mapped[StatusEnum] = mapped_column(
        sa.Enum(StatusEnum, name="incident_status", native_enum=True),
        nullable=False,
        default=StatusEnum.active,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    postmortem_md: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )


class Message(Base):
    """
    SQLAlchemy ORM model representing a chat message in an incident thread.

    Stores user messages, assistant responses, and system event notifications.
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[RoleEnum] = mapped_column(
        sa.Enum(RoleEnum, name="message_role", native_enum=True),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    # CRITICAL: Column must be named 'meta', NEVER 'metadata' (which collides with ORM Base.metadata)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Document(Base):
    """
    SQLAlchemy ORM model representing an ingested knowledge document.

    Stores document metadata, hash for deduplication, and counts.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    filename: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )
    doc_type: Mapped[DocTypeEnum] = mapped_column(
        sa.Enum(DocTypeEnum, name="document_type", native_enum=True),
        nullable=False,
    )
    sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    char_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Chunk(Base):
    """
    SQLAlchemy ORM model representing a chunk of an ingested document.

    Chunk primary key is a BigInteger autoincrement ID directly matching the FAISS vector ID.
    """

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    section: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )
    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
