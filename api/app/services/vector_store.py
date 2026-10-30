"""
api/app/services/vector_store.py

FAISS vector store service for Incident Copilot.

CRITICAL ARCHITECTURE NOTE:
  Uvicorn MUST run with a single worker process (--workers 1).
  The FAISS IndexIDMap2 and the asyncio.Lock protecting it reside in process memory.
  Multiple uvicorn worker processes would result in split-brain vector stores and
  race conditions during indexing and search.

Manages:
  - faiss.IndexIDMap2 wrapping IndexFlatIP(dimension) for cosine similarity (with normalized vectors).
  - Atomic persistence to index.faiss and index.meta.json.
  - Startup consistency check comparing FAISS vector IDs against PostgreSQL chunks.id.
  - Automatic rebuild from PostgreSQL on mismatch, model change, or missing files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import faiss
import numpy as np
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models.orm import Chunk, Document
from app.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


def _get_store_dir() -> Path:
    """
    Returns the FAISS store directory path.

    Defaults to /app/faiss_store (mounted volume in Docker).
    Falls back to a local 'faiss_store' directory if running in local environments
    where /app is not writable.
    """
    env_dir = os.environ.get("FAISS_STORE_DIR")
    if env_dir:
        p = Path(env_dir)
    else:
        p = Path("/app/faiss_store")
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            p = Path("faiss_store")

    p.mkdir(parents=True, exist_ok=True)
    return p


class VectorStore:
    """
    Singleton vector store managing an in-memory FAISS IndexIDMap2 index.

    Every index operation is protected by an asyncio.Lock to guarantee thread
    safety and prevent concurrent write corruption.
    """

    def __init__(self, dimension: int = 384) -> None:
        self._dimension = dimension
        self._index: Optional[faiss.IndexIDMap2] = None
        self._lock = asyncio.Lock()
        self._store_dir = _get_store_dir()

    @property
    def lock(self) -> asyncio.Lock:
        """Exposes the vector store lock for compound atomic operations."""
        return self._lock

    @property
    def dimension(self) -> int:
        return self._dimension

    def _create_empty_index(self) -> faiss.IndexIDMap2:
        """Instantiates a new empty IndexIDMap2 over an IndexFlatIP index."""
        sub_index = faiss.IndexFlatIP(self._dimension)
        return faiss.IndexIDMap2(sub_index)

    def _save_unlocked(self) -> None:
        """
        Saves the FAISS index and metadata atomically using temporary files.

        MUST be called while holding self._lock.
        """
        if self._index is None:
            return

        settings = get_settings()
        index_path = self._store_dir / "index.faiss"
        index_tmp = self._store_dir / "index.faiss.tmp"
        meta_path = self._store_dir / "index.meta.json"
        meta_tmp = self._store_dir / "index.meta.json.tmp"

        meta_data = {
            "embedding_model": settings.embedding_model,
            "dimension": self._dimension,
        }

        # Write index to temp file
        faiss.write_index(self._index, str(index_tmp))

        # Write metadata to temp file with fsync
        with open(meta_tmp, "w", encoding="utf-8") as f:
            json.dump(meta_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        # Atomically replace files
        os.replace(index_tmp, index_path)
        os.replace(meta_tmp, meta_path)

    async def save(self) -> None:
        """Saves the FAISS index and metadata to disk under lock."""
        async with self._lock:
            self._save_unlocked()

    async def count(self) -> int:
        """Returns the total number of vectors in the index."""
        async with self._lock:
            return self._index.ntotal if self._index is not None else 0

    async def add(self, ids: List[int], vectors: np.ndarray) -> None:
        """
        Adds vectors with associated integer IDs to the index under lock.

        IDs directly map to Chunk.id in PostgreSQL.
        """
        if not ids:
            return

        async with self._lock:
            if self._index is None:
                self._index = self._create_empty_index()

            vec_arr = np.ascontiguousarray(vectors, dtype=np.float32)
            id_arr = np.ascontiguousarray(ids, dtype=np.int64)
            self._index.add_with_ids(vec_arr, id_arr)

    async def remove(self, ids: List[int]) -> None:
        """
        Removes vectors by their integer chunk IDs from the index under lock.
        """
        if not ids:
            return

        async with self._lock:
            if self._index is None or self._index.ntotal == 0:
                return

            id_arr = np.ascontiguousarray(ids, dtype=np.int64)
            self._index.remove_ids(id_arr)

    async def search(
        self, query_vector: np.ndarray, k: int
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Performs cosine similarity search against the index under lock.

        Returns:
            Tuple of (scores, chunk_ids) as 1D numpy arrays.
        """
        async with self._lock:
            if self._index is None or self._index.ntotal == 0 or k <= 0:
                return np.empty((0,), dtype=np.float32), np.empty((0,), dtype=np.int64)

            q_vec = np.ascontiguousarray(query_vector, dtype=np.float32)
            if q_vec.ndim == 1:
                q_vec = q_vec.reshape(1, -1)

            scores, ids = self._index.search(q_vec, k)
            return scores[0], ids[0]

    async def rebuild_from_db(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_service: EmbeddingService,
    ) -> int:
        """
        Reconstructs the entire FAISS index from chunk records in PostgreSQL.

        Embeds in batches of 32 chunks, updates in-memory index, and atomically saves.
        Must be called under self._lock.
        """
        settings = get_settings()
        self._dimension = embedding_service.dimension
        new_index = self._create_empty_index()

        async with session_factory() as session:
            query = (
                sa.select(Chunk.id, Chunk.section, Chunk.text, Document.filename)
                .join(Document, Chunk.document_id == Document.id)
                .order_by(Chunk.id)
            )
            rows = (await session.execute(query)).all()

        total_chunks = len(rows)
        if total_chunks > 0:
            batch_size = 32
            for i in range(0, total_chunks, batch_size):
                batch_rows = rows[i : i + batch_size]
                batch_ids = [r.id for r in batch_rows]
                batch_texts: List[str] = []
                for r in batch_rows:
                    if r.section:
                        batch_texts.append(f"{r.filename} | {r.section}\n{r.text}")
                    else:
                        batch_texts.append(f"{r.filename}\n{r.text}")

                vectors = await embedding_service.embed_texts(batch_texts)
                vec_arr = np.ascontiguousarray(vectors, dtype=np.float32)
                id_arr = np.ascontiguousarray(batch_ids, dtype=np.int64)
                new_index.add_with_ids(vec_arr, id_arr)

        self._index = new_index
        self._save_unlocked()
        logger.info("rebuilt %d vectors", total_chunks)
        return total_chunks

    async def ensure_consistent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_service: EmbeddingService,
    ) -> None:
        """
        Validates consistency between on-disk FAISS index and PostgreSQL chunks.

        If index files are missing, corrupt, using mismatched models, or have a
        mismatched set of vector IDs, an automatic full rebuild is triggered.
        """
        async with self._lock:
            settings = get_settings()
            self._dimension = embedding_service.dimension
            index_path = self._store_dir / "index.faiss"
            meta_path = self._store_dir / "index.meta.json"

            index_valid = False
            loaded_index: Optional[faiss.IndexIDMap2] = None
            faiss_ids_set = set()

            if index_path.exists() and meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)

                    if (
                        meta.get("embedding_model") == settings.embedding_model
                        and meta.get("dimension") == self._dimension
                    ):
                        loaded_index = faiss.read_index(str(index_path))
                        if loaded_index is not None:
                            arr = faiss.vector_to_array(loaded_index.id_map)
                            faiss_ids_set = set(arr.tolist())
                            index_valid = True
                    else:
                        logger.warning(
                            "FAISS metadata mismatch (model=%s vs %s, dim=%s vs %s). Rebuild required.",
                            meta.get("embedding_model"),
                            settings.embedding_model,
                            meta.get("dimension"),
                            self._dimension,
                        )
                except Exception as exc:
                    logger.warning("Failed to load existing FAISS index: %s", exc)
                    index_valid = False

            # Query all chunk IDs from database
            async with session_factory() as session:
                result = await session.execute(sa.select(Chunk.id))
                db_chunk_ids_set = set(result.scalars().all())

            # Check exact match of count and ID set
            if (
                index_valid
                and loaded_index is not None
                and len(faiss_ids_set) == len(db_chunk_ids_set)
                and faiss_ids_set == db_chunk_ids_set
            ):
                self._index = loaded_index
                logger.info("loaded %d vectors", len(faiss_ids_set))
            else:
                logger.info(
                    "FAISS index out of sync with DB (index=%d, db=%d). Rebuilding...",
                    len(faiss_ids_set),
                    len(db_chunk_ids_set),
                )
                await self.rebuild_from_db(session_factory, embedding_service)


# Singleton vector store instance
vector_store = VectorStore()


def get_vector_store() -> VectorStore:
    """Returns the singleton VectorStore instance."""
    return vector_store


__all__ = ["VectorStore", "vector_store", "get_vector_store"]
