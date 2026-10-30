"""
api/app/main.py

Entry point for the Incident Copilot FastAPI application.

Responsibilities:
  - Defines a lifespan context manager for startup table creation, embedding model
    loading, vector store consistency checks, and shutdown resource cleanup.
  - Creates the FastAPI app instance and registers routers.
  - Registers global exception handling for unexpected server errors.

The lifespan pattern (contextlib.asynccontextmanager) is the modern FastAPI
approach and replaces the deprecated @app.on_event("startup") decorator.

Nothing beyond app wiring belongs here; business logic lives in services/.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db.session import async_session_factory, engine
from app.models.orm import Base
from app.routers import health as health_router
from app.routers import incidents as incidents_router
from app.routers import knowledge as knowledge_router
from app.services.cache import close_redis_client, recompute_kb_version
from app.services.embeddings import get_embedding_service
from app.services.llm import get_llm_provider
from app.services.vector_store import get_vector_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """
    FastAPI lifespan context manager.

    Startup:
      1. Verifies / creates database tables with retry loop.
      2. Initialises EmbeddingService singleton (loads SentenceTransformers model).
      3. Synchronises VectorStore with PostgreSQL chunks (runs ensure_consistent).

    Shutdown:
      1. Disposes database engine.
      2. Shuts down background embedding executor.
    """
    # ── Startup ────────────────────────────────────────────────────────────
    logger.info("Initializing database tables...")
    for attempt in range(1, 6):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables verified/created successfully.")
            break
        except Exception as exc:
            logger.warning("DB connection attempt %d/5 failed: %s", attempt, exc)
            if attempt == 5:
                logger.error("Failed to connect to database after 5 attempts.")
                raise
            await asyncio.sleep(2)

    # Initialise SentenceTransformers embedding model
    embedding_svc = get_embedding_service()
    embedding_svc.initialize()
    settings = get_settings()
    logger.info(
        "Embedding model '%s' loaded successfully (dimension: %d)",
        settings.embedding_model,
        embedding_svc.dimension,
    )

    # Synchronise FAISS index with database chunks
    vstore = get_vector_store()
    await vstore.ensure_consistent(async_session_factory, embedding_svc)

    # Suppress verbose third-party logging to prevent key / prompt leakage
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)

    # Configure LangSmith observability environment variables
    os.environ["LANGSMITH_TRACING"] = "true" if settings.langsmith_tracing else "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "true" if settings.langsmith_tracing else "false"
    if settings.langsmith_api_key:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    if settings.langsmith_project:
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project

    # Compute initial in-memory knowledge base version
    initial_kb = await recompute_kb_version(async_session_factory)
    logger.info("Initial knowledge base version: %s", initial_kb)

    # Initialize LLM provider singleton (logs startup warning if key missing)
    get_llm_provider()
    logger.info("LLM provider '%s' initialized.", settings.llm_provider)

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────
    logger.info("Closing Redis connection...")
    await close_redis_client()

    logger.info("Disposing database engine...")
    await engine.dispose()
    logger.info("Database engine disposed.")

    embedding_svc.shutdown()


def create_app() -> FastAPI:
    """Factory function that assembles and returns the configured FastAPI application."""
    settings = get_settings()

    application = FastAPI(
        title=f"{settings.company_name} — Incident Copilot",
        description=(
            "RAG-powered incident response and post-mortem copilot. "
            "Triage incidents, search your team's runbooks, and generate post-mortems."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Global Exception Handler ───────────────────────────────────────────
    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, (HTTPException, StarletteHTTPException)):
            return await http_exception_handler(request, exc)
        if isinstance(exc, RequestValidationError):
            return await request_validation_exception_handler(request, exc)
        logger.exception("Unhandled server error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # ── Routers ────────────────────────────────────────────────────────────
    application.include_router(health_router.router, tags=["Health"])
    application.include_router(incidents_router.router, prefix="/incidents", tags=["Incidents"])
    application.include_router(knowledge_router.router, prefix="/knowledge", tags=["Knowledge"])

    return application


app = create_app()
