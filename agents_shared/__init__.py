"""agents_shared — shared SDK for innovation-lab-agents.

Public modules:

- ``agents_shared.sentry``     Sentry init + capture helpers.
- ``agents_shared.health``     Liveness/readiness HTTP probes.
- ``agents_shared.security_fetch`` Hardened HTTP client (SSRF-safe, retries, timeouts).
- ``agents_shared.vector``     ``VectorStore`` — embedding generation + pgvector I/O.
- ``agents_shared.rate_limit`` In-memory + Postgres-backed rate limiters.
- ``agents_shared.logging``    Structured JSON logger factory.
- ``agents_shared.db``         asyncpg pool management.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agents-shared")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = [
    "__version__",
    "db",
    "health",
    "logging",
    "rate_limit",
    "security_fetch",
    "sentry",
    "vector",
]
