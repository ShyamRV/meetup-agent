# agents-shared

Shared SDK for all agents in `innovation-lab-agents`. Provides:

| Module                          | Purpose                                                          |
| ------------------------------- | ---------------------------------------------------------------- |
| `agents_shared.sentry`          | Sentry init + safe `capture_exception` wrapper.                  |
| `agents_shared.health`          | aiohttp `/healthz` + `/readyz` probes with `mark_ready()`.       |
| `agents_shared.security_fetch`  | SSRF-safe HTTP client with retries, timeouts, body size cap.     |
| `agents_shared.vector`          | `VectorStore` — embedding generation + pgvector I/O.             |
| `agents_shared.rate_limit`      | Sliding-window in-memory limiter + decorator.                    |
| `agents_shared.db`              | `asyncpg` pool singleton with pgvector codec registration.       |
| `agents_shared.logging`         | Structured JSON logger factory.                                  |

The SDK is **always** the entry point for these concerns — agents should
never call `aiohttp` / `asyncpg` / `sentry_sdk` directly.
