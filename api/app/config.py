"""
api/app/config.py

Centralised settings management for the Incident Copilot API.

All configuration is read from environment variables (or a .env file mounted at
runtime). Using pydantic-settings ensures values are type-validated at startup,
so the app fails fast on bad configuration rather than at the first usage site.

Usage:
    from app.config import get_settings
    settings = get_settings()
    print(settings.company_name)
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Pydantic settings model.

    Each field maps 1-to-1 to an environment variable (case-insensitive).
    Sensitive fields (keys, passwords) are intentionally excluded from any
    public API response — see routers/health.py for the safe /config endpoint.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",        # silently ignore unknown env vars
        env_ignore_empty=True, # treat empty-string env vars as unset; fall back to field defaults
    )

    # ── Display ─────────────────────────────────────────────────────────────
    company_name: str = Field(default="Your Company", description="Display name shown in the UI.")

    # ── LLM ─────────────────────────────────────────────────────────────────
    llm_provider: str = Field(default="gemini", description="Active LLM provider: 'gemini' or 'ollama'.")
    gemini_api_key: str = Field(default="", description="Gemini API key. NEVER exposed via API.")
    gemini_model: str = Field(default="gemini-3.5-flash", description="Active Gemini model id.")
    llm_timeout_s: float = Field(default=30.0)
    llm_max_retries: int = Field(default=2)
    llm_max_output_tokens: int = Field(default=4096)
    llm_temperature: float = Field(default=0.1)
    postmortem_max_output_tokens: int = Field(default=4096)
    postmortem_timeout_s: float = Field(default=90.0)
    ollama_base_url: str = Field(
        default="http://host.docker.internal:11434",
        description=(
            "Ollama server URL. Uses host.docker.internal so the container can reach "
            "Ollama running on the Windows host without network=host."
        ),
    )

    # ── Embeddings & Retrieval ──────────────────────────────────────────────
    embedding_model: str = Field(
        default="BAAI/bge-small-en-v1.5",
        description="HuggingFace model id used for local embeddings.",
    )
    embedding_query_prefix: Optional[str] = Field(
        default=None,
        description="Optional override prefix for query embeddings.",
    )
    retrieval_k: int = Field(default=6)
    retrieval_min_score: float = Field(default=0.45)

    # ── PostgreSQL ───────────────────────────────────────────────────────────
    postgres_user: str = Field(default="copilot_user")
    postgres_password: str = Field(default="", description="DB password. NEVER exposed via API.")
    postgres_db: str = Field(default="incident_copilot")
    postgres_host: str = Field(default="postgres")
    postgres_port: int = Field(default=5432)

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://redis:6379/0")
    answer_cache_ttl_s: int = Field(default=86400)

    # ── LangSmith ────────────────────────────────────────────────────────────
    langsmith_tracing: bool = Field(default=False)
    langsmith_api_key: str = Field(default="", description="LangSmith API key. NEVER exposed via API.")
    langsmith_project: str = Field(default="incident-copilot")

    # ── Abuse & Rate Limits ──────────────────────────────────────────────────
    rate_ask_per_min: int = Field(default=30, description="Max ask triage questions per minute (global).")
    rate_resolve_per_min: int = Field(default=5, description="Max incident resolutions per minute (global).")
    rate_upload_per_min: int = Field(default=10, description="Max document uploads per minute (global).")
    daily_llm_call_cap: int = Field(default=500, description="Max real LLM calls per UTC day across the app.")

    @property
    def database_url(self) -> str:
        """
        Constructs the async SQLAlchemy PostgreSQL connection string.

        Uses the asyncpg driver (postgresql+asyncpg) so that all database I/O
        is non-blocking and compatible with FastAPI's async request handlers.
        The individual POSTGRES_* env vars are the single source of truth;
        DATABASE_URL is never set separately in .env.
        """
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Returns a cached singleton Settings instance.

    The lru_cache ensures the .env file is parsed only once per process,
    which is safe because settings do not change at runtime.
    """
    return Settings()
