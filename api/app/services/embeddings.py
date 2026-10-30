"""
api/app/services/embeddings.py

SentenceTransformers embedding service for Incident Copilot.

Runs embedding inference locally on CPU via a dedicated single-worker ThreadPoolExecutor.
Ensures document embeddings have no prefix while query embeddings apply the BGE search prefix
(or configured override).
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import numpy as np

from app.config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Singleton service managing SentenceTransformer model inference on CPU.

    Encapsulates dedicated single-threaded execution to prevent CPU thrashing
    and thread contention under async FastAPI event loop.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="embedding_worker",
        )
        self._model = None
        self._dimension: int = 384
        self._query_prefix: str = ""

    @property
    def dimension(self) -> int:
        """Returns the embedding vector dimension."""
        return self._dimension

    @property
    def query_prefix(self) -> str:
        """Returns the active query prefix string."""
        return self._query_prefix

    def initialize(self) -> None:
        """
        Loads the SentenceTransformer model on CPU.

        Called during FastAPI lifespan startup. Fails visibly with a clear error
        if the model cannot be loaded from the configured HuggingFace cache.
        """
        settings = get_settings()
        hf_home = os.environ.get("HF_HOME", "/app/hf_cache")
        logger.info(
            "Loading embedding model '%s' (HF_HOME=%s)...",
            settings.embedding_model,
            hf_home,
        )

        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(settings.embedding_model, device="cpu")
        except Exception as exc:
            logger.error(
                "CRITICAL: Failed to load embedding model '%s' from HF_HOME='%s': %s",
                settings.embedding_model,
                hf_home,
                exc,
            )
            raise

        self._dimension = int(self._model.get_sentence_embedding_dimension())

        # Determine query prefix
        if settings.embedding_query_prefix is not None and settings.embedding_query_prefix != "":
            self._query_prefix = settings.embedding_query_prefix
        elif "bge" in settings.embedding_model.lower():
            self._query_prefix = "Represent this sentence for searching relevant passages: "
        else:
            self._query_prefix = ""

        logger.info(
            "Embedding model '%s' ready. Dimension=%d, Query prefix=%r",
            settings.embedding_model,
            self._dimension,
            self._query_prefix,
        )

    async def embed_texts(self, texts: List[str]) -> np.ndarray:
        """
        Computes normalised float32 embeddings for a batch of texts on the dedicated worker.

        Document chunks are passed directly without any prefix.
        Returns a 2D numpy array of shape (N, dimension).
        """
        if not texts:
            return np.empty((0, self._dimension), dtype=np.float32)

        if self._model is None:
            raise RuntimeError("EmbeddingService has not been initialized.")

        loop = asyncio.get_running_loop()
        embeddings: np.ndarray = await loop.run_in_executor(
            self._executor,
            lambda: self._model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=32,
            ).astype(np.float32),
        )
        return embeddings

    async def embed_query(self, query: str) -> np.ndarray:
        """
        Computes a normalised 1D float32 embedding for a search query.

        Prepends the query prefix if configured or default for BGE models.
        Returns a 1D numpy array of shape (dimension,).
        """
        if self._model is None:
            raise RuntimeError("EmbeddingService has not been initialized.")

        prefixed_query = f"{self._query_prefix}{query}" if self._query_prefix else query
        loop = asyncio.get_running_loop()

        emb: np.ndarray = await loop.run_in_executor(
            self._executor,
            lambda: self._model.encode(
                prefixed_query,
                normalize_embeddings=True,
            ).astype(np.float32),
        )

        if emb.ndim > 1:
            emb = emb.squeeze(0)
        return emb

    def shutdown(self) -> None:
        """Gracefully shuts down the background embedding thread executor."""
        logger.info("Shutting down embedding executor...")
        self._executor.shutdown(wait=True)
        logger.info("Embedding executor stopped.")


# Singleton instance shared across the application
embedding_service = EmbeddingService()


def get_embedding_service() -> EmbeddingService:
    """Returns the singleton EmbeddingService instance."""
    return embedding_service


__all__ = ["EmbeddingService", "embedding_service", "get_embedding_service"]
