"""Rate limiting primitives.

Two implementations are provided:

- :class:`InMemoryRateLimiter` — sliding window per-key counter in the
  current process. Ideal for OAuth callback abuse protection on a single
  pod and for unit tests.
- :func:`enforce` — decorator helper that raises :class:`RateLimitExceeded`
  when the caller exceeds the configured budget.

For multi-pod deployments swap the backend to Postgres or Redis; the
public surface intentionally hides the backend.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from agents_shared.logging import get_logger

_log = get_logger(__name__)


class RateLimitExceeded(RuntimeError):
    """Raised when a key exhausts its rate budget."""

    def __init__(self, key: str, limit: int, window_s: float) -> None:
        super().__init__(f"rate limit exceeded: key={key} limit={limit} window={window_s}s")
        self.key = key
        self.limit = limit
        self.window_s = window_s


@dataclass(slots=True)
class _Bucket:
    hits: deque[float] = field(default_factory=deque)


class InMemoryRateLimiter:
    """Sliding-window rate limiter, ``limit`` hits per ``window_s`` per key."""

    def __init__(self, *, limit: int, window_s: float) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self.limit = limit
        self.window_s = window_s
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> bool:
        """Return ``True`` if the request is allowed; record the hit if so."""

        now = time.monotonic()
        cutoff = now - self.window_s
        async with self._lock:
            bucket = self._buckets.setdefault(key, _Bucket())
            while bucket.hits and bucket.hits[0] < cutoff:
                bucket.hits.popleft()
            if len(bucket.hits) >= self.limit:
                _log.warning(
                    "rate_limit.exceeded",
                    key=key,
                    limit=self.limit,
                    window_s=self.window_s,
                )
                return False
            bucket.hits.append(now)
            return True

    async def require(self, key: str) -> None:
        """Like :meth:`check` but raises on rejection."""

        if not await self.check(key):
            raise RateLimitExceeded(key, self.limit, self.window_s)


T = TypeVar("T")


def enforce(
    limiter: InMemoryRateLimiter, key_fn: Callable[..., str]
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator: gate ``func`` behind ``limiter`` keyed by ``key_fn(*args, **kwargs)``."""

    def _decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def _wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            await limiter.require(key_fn(*args, **kwargs))
            return await func(*args, **kwargs)

        _wrapper.__name__ = func.__name__
        _wrapper.__doc__ = func.__doc__
        return _wrapper

    return _decorator
