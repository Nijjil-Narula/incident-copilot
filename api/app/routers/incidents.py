"""
api/app/routers/incidents.py

API router providing CRUD operations and message threads for Incident tickets.

Endpoints:
  - POST /incidents          : Create a new incident ticket.
  - GET  /incidents          : List incidents with pagination and status filtering.
  - GET  /incidents/{id}     : Retrieve detailed incident info with chronological messages.
  - POST /incidents/{id}/messages : Append a chat message to an incident investigation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from typing import List, Optional, Sequence, Set

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory, get_session
from app.models.orm import Chunk, DocTypeEnum, Document, Incident, Message, RoleEnum, StatusEnum
from app.models.schemas import (
    AskQuestionRequest,
    AskQuestionResponse,
    IncidentCreate,
    IncidentDetail,
    IncidentOut,
    MessageCreate,
    MessageOut,
    PublishPostmortemResponse,
    ResolveIncidentRequest,
    SearchHit,
)
from app.services.answer import process_question
from app.services.knowledge import ingest_document_bytes
from app.services.llm import LLMUnavailableError
from app.services.postmortem import (
    acquire_resolution_lock,
    generate_postmortem_report,
    release_resolution_lock,
    slugify_title,
)

router = APIRouter(tags=["Incidents"])


@router.post(
    "/",
    response_model=IncidentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new incident ticket",
)
@router.post(
    "",
    response_model=IncidentOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def create_incident(
    body: IncidentCreate,
    session: AsyncSession = Depends(get_session),
) -> Incident:
    """
    Creates a new incident record in the database.

    Persists the ticket, commits the transaction, and returns the created
    incident populated with its generated ID and timestamps.
    """
    incident = Incident(
        title=body.title,
        description=body.description,
        severity=body.severity,
    )
    session.add(incident)
    await session.commit()
    await session.refresh(incident)
    return incident


@router.get(
    "/",
    response_model=List[IncidentOut],
    summary="List incidents",
)
@router.get(
    "",
    response_model=List[IncidentOut],
    include_in_schema=False,
)
async def list_incidents(
    status: Optional[StatusEnum] = Query(
        default=None,
        description="Optional filter by incident status (active or resolved).",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of incidents to return (1-200).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Offset for pagination.",
    ),
    session: AsyncSession = Depends(get_session),
) -> Sequence[Incident]:
    """
    Retrieves a paginated list of incidents ordered by creation time descending.

    Optionally filters by incident status. Does not load associated messages
    to keep list retrieval lightweight.
    """
    query = select(Incident)
    if status is not None:
        query = query.where(Incident.status == status)
    query = query.order_by(Incident.created_at.desc()).offset(offset).limit(limit)

    res = await session.execute(query)
    return res.scalars().all()


@router.get(
    "/{id}",
    response_model=IncidentDetail,
    summary="Get incident details and message thread",
)
async def get_incident(
    id: uuid.UUID = Path(
        ...,
        description="Unique UUID identifier of the incident.",
    ),
    session: AsyncSession = Depends(get_session),
) -> IncidentDetail:
    """
    Retrieves a single incident by ID along with all messages in chronological order.

    Loads the incident ticket first, raising a 404 error if not found.
    Incident messages are loaded via an explicit secondary query ordered by
    created_at ascending to prevent lazy loading issues in async SQLAlchemy.
    """
    incident_res = await session.execute(select(Incident).where(Incident.id == id))
    incident = incident_res.scalar_one_or_none()
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        )

    role_order = case(
        (Message.role == RoleEnum.system, 0),
        (Message.role == RoleEnum.user, 1),
        (Message.role == RoleEnum.assistant, 2),
        else_=3,
    )
    messages_res = await session.execute(
        select(Message)
        .where(Message.incident_id == id)
        .order_by(Message.created_at.asc(), role_order.asc())
    )
    messages = messages_res.scalars().all()

    return IncidentDetail(
        **IncidentOut.model_validate(incident).model_dump(),
        messages=list(messages),
    )


@router.post(
    "/{id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Append a message to an incident thread",
)
async def create_message(
    id: uuid.UUID = Path(
        ...,
        description="Unique UUID identifier of the incident.",
    ),
    body: MessageCreate = ...,
    session: AsyncSession = Depends(get_session),
) -> Message:
    """
    Appends a new message (user prompt, assistant response, or system log) to an incident.

    Verifies the parent incident exists before inserting. Commits and returns
    the created message.
    """
    incident_res = await session.execute(select(Incident.id).where(Incident.id == id))
    if incident_res.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        )

    message = Message(
        incident_id=id,
        role=body.role,
        content=body.content,
        meta=body.meta,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


@router.post(
    "/{id}/ask",
    response_model=AskQuestionResponse,
    status_code=status.HTTP_200_OK,
    summary="Ask an incident triage question",
)
async def ask_question(
    id: uuid.UUID = Path(
        ...,
        description="Unique UUID identifier of the incident.",
    ),
    body: AskQuestionRequest = ...,
) -> AskQuestionResponse:
    """
    Asks a question within an active incident investigation.

    Execution lifecycle with disconnected database sessions:
      1. Pre-generation check: Verifies incident exists and is not resolved, then
         immediately closes the database session.
      2. LLM RAG Pipeline: Retrieves chunks, queries LLM, and verifies guardrails
         without holding any open database connection.
      3. Post-generation persistence: Uses a fresh write session to persist both
         user and assistant messages in a single transaction.
    """
    received_at = datetime.now(timezone.utc)

    # ── 1. Read check: ensure incident exists and is active ──────────────────
    async with async_session_factory() as session:
        incident = await session.get(Incident, id)
        if incident is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Incident not found",
            )
        if incident.status == StatusEnum.resolved:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot ask questions in a resolved incident",
            )

    # ── 2. Run LLM answer pipeline with disconnected DB ─────────────────────
    try:
        answer_text, meta_dict = await process_question(id, body.question)
    except LLMUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The language model is unavailable or rate limited. Try again shortly.",
        ) from exc

    # ── 3. Fresh write session: persist messages atomically ──────────────────
    async with async_session_factory() as session:
        incident = await session.get(Incident, id)
        if incident is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Incident not found",
            )

        asst_created_at = max(
            datetime.now(timezone.utc),
            received_at + timedelta(milliseconds=1),
        )
        user_msg = Message(
            incident_id=id,
            role=RoleEnum.user,
            content=body.question,
            created_at=received_at,
        )
        asst_msg = Message(
            incident_id=id,
            role=RoleEnum.assistant,
            content=answer_text,
            meta=meta_dict,
            created_at=asst_created_at,
        )
        session.add(user_msg)
        session.add(asst_msg)
        await session.commit()
        await session.refresh(user_msg)
        await session.refresh(asst_msg)

        return AskQuestionResponse(
            user_message=MessageOut.model_validate(user_msg),
            assistant_message=MessageOut.model_validate(asst_msg),
        )


@router.post(
    "/{id}/resolve",
    response_model=IncidentDetail,
    status_code=status.HTTP_200_OK,
    summary="Resolve an incident and generate post-mortem",
)
async def resolve_incident(
    id: uuid.UUID = Path(
        ...,
        description="Unique UUID identifier of the incident.",
    ),
    body: ResolveIncidentRequest = ...,
) -> IncidentDetail:
    """
    Resolves an active incident ticket and optionally generates a grounded post-mortem report.

    Lifecycle:
      1. Read check: Verifies incident exists (404) and is not already resolved (409).
         Loads all incident messages ordered chronologically.
      2. In-progress check: Acquires in-memory resolution lock for this incident (409 if active).
      3. Validation: If generate_postmortem=true, checks that there is at least one user message
         or a resolution summary (422 if nothing to summarize).
      4. Runbook chunks retrieval: Extracts cited chunk IDs from assistant messages and queries
         matching runbook chunks for guardrail verification.
      5. Disconnects read database session before calling LLM.
      6. Captures resolved_at timestamp once.
      7. Generates post-mortem report with 6-heading validation and safety guardrail verification.
         Guarantees releasing the in-progress lock in a finally block on success or failure.
      8. Fresh write session: Acquires row-level write lock (with_for_update), updates status to resolved,
         sets resolved_at, updated_at, and postmortem_md, commits and refreshes.
      9. Returns full IncidentDetail with ordered messages.
    """
    role_order = case(
        (Message.role == RoleEnum.system, 0),
        (Message.role == RoleEnum.user, 1),
        (Message.role == RoleEnum.assistant, 2),
        else_=3,
    )

    # ── 1. Read check: ensure incident exists and is active, load messages & chunks ──
    async with async_session_factory() as session:
        incident_res = await session.execute(select(Incident).where(Incident.id == id))
        incident = incident_res.scalar_one_or_none()
        if incident is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Incident not found",
            )
        if incident.status == StatusEnum.resolved:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Incident is already resolved",
            )

        messages_res = await session.execute(
            select(Message)
            .where(Message.incident_id == id)
            .order_by(Message.created_at.asc(), role_order.asc())
        )
        messages = list(messages_res.scalars().all())

        # Collect cited chunk IDs from assistant messages
        chunk_ids: Set[int] = set()
        for msg in messages:
            if msg.role == RoleEnum.assistant and msg.meta and isinstance(msg.meta, dict):
                sources = msg.meta.get("sources", [])
                if isinstance(sources, list):
                    for src in sources:
                        if isinstance(src, dict) and "chunk_id" in src:
                            try:
                                chunk_ids.add(int(src["chunk_id"]))
                            except (ValueError, TypeError):
                                pass

        runbook_chunks: List[SearchHit] = []
        if chunk_ids:
            chunks_stmt = (
                select(Chunk, Document)
                .join(Document, Chunk.document_id == Document.id)
                .where(
                    Document.doc_type == DocTypeEnum.runbook,
                    Chunk.id.in_(chunk_ids),
                )
            )
            chunks_res = await session.execute(chunks_stmt)
            for chunk_row, doc_row in chunks_res.all():
                runbook_chunks.append(
                    SearchHit(
                        chunk_id=chunk_row.id,
                        document_id=doc_row.id,
                        filename=doc_row.filename,
                        doc_type=doc_row.doc_type,
                        section=chunk_row.section,
                        score=1.0,
                        text=chunk_row.text,
                    )
                )

    # ── 2. In-progress check: ensure no concurrent resolution for this ID ────
    locked = await acquire_resolution_lock(id)
    if not locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resolution already in progress",
        )

    # ── 3. Validation: check if there is content to summarize ───────────────
    has_user_msg = any(m.role == RoleEnum.user for m in messages)
    if body.generate_postmortem and not has_user_msg and not body.resolution_summary:
        await release_resolution_lock(id)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Nothing to summarize",
        )

    # ── 5 & 6. Read session already closed; capture resolved_at once ────────
    resolved_at = datetime.now(timezone.utc)

    # ── 7. Generate report (if requested) & save with guaranteed lock release ──
    try:
        if body.generate_postmortem:
            try:
                postmortem_md = await generate_postmortem_report(
                    incident=incident,
                    messages=messages,
                    resolution_summary=body.resolution_summary,
                    runbook_chunks=runbook_chunks,
                    resolved_at=resolved_at,
                )
            except LLMUnavailableError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="The language model is unavailable or rate limited. Try again shortly.",
                ) from exc
        else:
            postmortem_md = None

        # ── 8. Save session: acquire row lock, update status and timestamps ──────
        async with async_session_factory() as session:
            stmt = select(Incident).where(Incident.id == id).with_for_update()
            res = await session.execute(stmt)
            incident_to_update = res.scalar_one_or_none()
            if incident_to_update is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Incident not found",
                )
            if incident_to_update.status == StatusEnum.resolved:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Incident is already resolved",
                )

            now = datetime.now(timezone.utc)
            incident_to_update.status = StatusEnum.resolved
            incident_to_update.resolved_at = resolved_at
            incident_to_update.updated_at = now
            incident_to_update.postmortem_md = postmortem_md

            await session.commit()
            await session.refresh(incident_to_update)

            # ── 9. Return IncidentDetail with ordered messages ───────────────────
            messages_res = await session.execute(
                select(Message)
                .where(Message.incident_id == id)
                .order_by(Message.created_at.asc(), role_order.asc())
            )
            final_messages = list(messages_res.scalars().all())

            return IncidentDetail(
                **IncidentOut.model_validate(incident_to_update).model_dump(),
                messages=final_messages,
            )
    finally:
        await release_resolution_lock(id)


@router.post(
    "/{id}/postmortem/publish",
    response_model=PublishPostmortemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Publish resolved incident post-mortem to knowledge base",
)
async def publish_postmortem(
    id: uuid.UUID = Path(
        ...,
        description="Unique UUID identifier of the incident.",
    ),
    session: AsyncSession = Depends(get_session),
) -> PublishPostmortemResponse:
    """
    Publishes a resolved incident's post-mortem report to the knowledge base as a postmortem document.

    Reuses the knowledge base ingestion pipeline to extract, chunk, embed, and index
    the post-mortem markdown report into FAISS and PostgreSQL.
    """
    # ── 1. Load incident and validate status and report content ─────────────
    incident = await session.get(Incident, id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        )

    if (
        incident.status != StatusEnum.resolved
        or not incident.postmortem_md
        or not incident.postmortem_md.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Incident is not resolved or post-mortem is empty",
        )

    # ── 2. Derive deterministic filename from ID prefix and slugified title ──
    slug = slugify_title(incident.title, max_chars=60)
    filename = f"postmortem-{str(incident.id)[:8]}-{slug}.md"

    # ── 3. Ingest post-mortem markdown bytes into knowledge base ─────────────
    upload_resp = await ingest_document_bytes(
        clean_filename=filename,
        doc_type=DocTypeEnum.postmortem,
        content_bytes=incident.postmortem_md.encode("utf-8"),
        session=session,
    )

    # ── 4. Return 201 Created with PublishPostmortemResponse ────────────────
    return PublishPostmortemResponse(
        document_id=upload_resp.id,
        filename=upload_resp.filename,
        doc_type="postmortem",
        chunk_count=upload_resp.chunk_count,
        replaced=upload_resp.replaced,
    )
