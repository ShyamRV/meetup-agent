"""Embedding generation + pgvector persistence.

``VectorStore`` is the single entry point: agents call ``embed(text)`` to
turn arbitrary text into a 1536-dim float vector and ``similarity_search``
to retrieve nearest neighbours. The backing model is OpenAI's
``text-embedding-3-small`` by default; swapping models is a config knob,
not a code change — ``model_version`` is written alongside each vector so
mismatched generations can be detected and re-embedded.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agents_shared import security_fetch
from agents_shared.db import get_pool
from agents_shared.logging import get_logger
from agents_shared.sentry import capture_exception

_log = get_logger(__name__)


@dataclass(slots=True)
class SimilarityHit:
    """One row returned by :meth:`VectorStore.similarity_search`."""

    attendee_id: str
    distance: float
    score: float  # 1 - cosine_distance, higher = more similar


class VectorStore:
    """Pluggable embedding generator + pgvector reader/writer."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str = "https://api.openai.com/v1",
        dim: int = 1536,
    ) -> None:
        self.model = model or os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.api_base = api_base.rstrip("/")
        self.dim = dim

    async def embed(self, text: str) -> list[float]:
        """Generate a unit-normalised embedding for ``text``."""

        if not text or not text.strip():
            raise ValueError("cannot embed empty text")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        try:
            resp = await security_fetch.post(
                f"{self.api_base}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "input": text},
            )
        except Exception as exc:
            capture_exception(exc, op="vector.embed", model=self.model)
            raise

        if resp.status >= 400:
            raise RuntimeError(
                f"embedding request failed: status={resp.status} body={resp.text[:200]}"
            )

        payload = resp.json()
        vector = payload["data"][0]["embedding"]
        if len(vector) != self.dim:
            raise RuntimeError(f"unexpected embedding dimension: got {len(vector)} want {self.dim}")
        return list(map(float, vector))

    async def upsert_embedding(
        self,
        *,
        attendee_id: str,
        event_id: str,
        embedding: Sequence[float],
        table: str = "meetup_embeddings",
    ) -> None:
        """Upsert an embedding row keyed by ``attendee_id`` (idempotent)."""

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {table} (attendee_id, event_id, embedding, model_version)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (attendee_id)
                DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    model_version = EXCLUDED.model_version,
                    created_at = NOW()
                """,
                attendee_id,
                event_id,
                list(embedding),
                self.model,
            )

    async def similarity_search(
        self,
        *,
        event_id: str,
        query_embedding: Sequence[float],
        exclude_attendee_id: str | None = None,
        top_k: int = 20,
        table: str = "meetup_embeddings",
    ) -> list[SimilarityHit]:
        """Cosine-similarity search scoped to a single event."""

        pool = await get_pool()
        params: list[Any] = [list(query_embedding), event_id]
        exclusion = ""
        if exclude_attendee_id is not None:
            params.append(exclude_attendee_id)
            exclusion = "AND attendee_id <> $3"
        params.append(top_k)
        limit_idx = len(params)

        sql = f"""
            SELECT attendee_id::text AS attendee_id,
                   embedding <=> $1 AS distance
            FROM {table}
            WHERE event_id = $2 {exclusion}
            ORDER BY embedding <=> $1
            LIMIT ${limit_idx}
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            SimilarityHit(
                attendee_id=row["attendee_id"],
                distance=float(row["distance"]),
                score=max(0.0, 1.0 - float(row["distance"])),
            )
            for row in rows
        ]
