"""Async Postgres pool management shared by every agent.

We pool ``asyncpg`` connections per process and register the ``pgvector``
extension type codec exactly once on connection setup. The pool is lazy —
``get_pool()`` is the only entry point; it creates the pool on first call
and reuses it forever.
"""

from __future__ import annotations

import os
from typing import Any

import asyncpg

from agents_shared.logging import get_logger

_log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def _on_connect(conn: asyncpg.Connection) -> None:
    """Connection-init hook — register pgvector codec if extension exists."""

    try:
        from pgvector.asyncpg import register_vector  # type: ignore

        await register_vector(conn)
    except Exception:
        pass


async def get_pool(dsn: str | None = None, **pool_kwargs: Any) -> asyncpg.Pool:
    """Return the singleton pool, creating it on first call."""

    global _pool
    if _pool is not None:
        return _pool

    resolved_dsn = dsn or os.getenv("DATABASE_URL")
    if not resolved_dsn:
        raise RuntimeError("DATABASE_URL is not configured")

    _pool = await asyncpg.create_pool(
        dsn=resolved_dsn,
        min_size=int(os.getenv("DB_POOL_MIN", "1")),
        max_size=int(os.getenv("DB_POOL_MAX", "10")),
        command_timeout=int(os.getenv("DB_COMMAND_TIMEOUT", "30")),
        init=_on_connect,
        **pool_kwargs,
    )
    _log.info("db.pool_created", min_size=_pool.get_min_size(), max_size=_pool.get_max_size())
    return _pool


async def close_pool() -> None:
    """Close the singleton pool. Safe to call multiple times."""

    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        _log.info("db.pool_closed")


async def healthcheck() -> bool:
    """Run ``SELECT 1`` to confirm the pool is alive — used for readiness."""

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
        return value == 1
    except Exception as exc:
        _log.error("db.healthcheck_failed", err=str(exc))
        return False
