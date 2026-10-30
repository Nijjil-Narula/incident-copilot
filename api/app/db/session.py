"""
api/app/db/session.py

Database session management and async engine configuration for Incident Copilot.

Configures the SQLAlchemy async engine and session factory backed by asyncpg,
and exposes the `get_session` dependency for FastAPI route handlers.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
)

async_session_factory = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async database session per request.

    CRITICAL:
      `get_session` MUST NOT commit transactions automatically. Handlers are
      responsible for explicitly issuing `await session.commit()`.
      If an unhandled exception is raised during request handling, the session
      rolls back. The session is always closed in the finally block.
    """
    session: AsyncSession = async_session_factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


__all__ = ["engine", "async_session_factory", "get_session"]
