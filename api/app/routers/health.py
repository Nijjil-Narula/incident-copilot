"""
api/app/routers/health.py

Health and configuration endpoints for the Incident Copilot API.

Endpoints:
  GET /health       — Returns the operational status of the API service (liveness).
                      Used by Docker healthchecks and the Streamlit status badge.
  GET /health/ready — Returns readiness status by testing active DB connectivity.
  GET /config       — Returns non-sensitive configuration values (e.g., COMPANY_NAME).
                      The UI calls this on startup to personalise the page title.
                      IMPORTANT: secrets (API keys, passwords) are NEVER included.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()


class HealthResponse(BaseModel):
    """Schema for the /health response body."""

    status: str
    service: str


class ConfigResponse(BaseModel):
    """
    Schema for the /config response body.

    Only non-sensitive fields are included. Add fields here as the UI needs them,
    but never add keys, passwords, or connection strings.
    """

    company_name: str
    llm_provider: str
    embedding_model: str


@router.get("/health", response_model=HealthResponse, summary="Service health check")
async def health_check() -> HealthResponse:
    """
    Returns a simple liveness status.

    Docker and the Streamlit UI poll this endpoint to determine whether the API
    is up.
    """
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=f"{settings.company_name} Incident Copilot API",
    )


@router.get("/health/ready", summary="Service readiness check including database connectivity")
async def readiness_check(
    session: AsyncSession = Depends(get_session),
):
    """
    Readiness probe for container orchestrators and health checks.

    Executes a simple 'SELECT 1' against PostgreSQL to verify connectivity.
    Returns HTTP 200 with status ok and db connected if successful.
    Returns HTTP 503 with status unavailable and db disconnected on failure.
    """
    try:
        await session.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as exc:
        logger.warning("Readiness probe database check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "db": "disconnected"},
        )


@router.get("/config", response_model=ConfigResponse, summary="Public configuration values")
async def get_config() -> ConfigResponse:
    """
    Returns public (non-sensitive) configuration values to the UI.

    The Streamlit frontend calls this on startup to read COMPANY_NAME and other
    display-only settings. Secrets are intentionally excluded from this response.
    """
    settings = get_settings()
    return ConfigResponse(
        company_name=settings.company_name,
        llm_provider=settings.llm_provider,
        embedding_model=settings.embedding_model,
    )
