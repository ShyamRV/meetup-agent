"""Kubernetes-style health probe HTTP server.

Each agent boots a small aiohttp server on ``HEALTH_PORT`` (default 8080)
exposing two endpoints:

- ``GET /healthz``  — liveness. Returns 200 as soon as the process is up.
- ``GET /readyz``   — readiness. Returns 200 only after :func:`mark_ready`.

The pod stays out of the load-balancer rotation until ``readyz`` flips,
which is what we want — readiness is gated on the DB connection check.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable

from aiohttp import web

from agents_shared.logging import get_logger

_log = get_logger(__name__)

_ready = False
_started = False
_runner: web.AppRunner | None = None
ReadinessCheck = Callable[[], Awaitable[bool]]


def mark_ready() -> None:
    """Flip the readiness flag to true. Pod will start receiving traffic."""

    global _ready
    _ready = True
    _log.info("health.ready")


def mark_unready() -> None:
    """Flip readiness back to false (e.g. during graceful shutdown)."""

    global _ready
    _ready = False
    _log.info("health.unready")


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(body=b'{"status":"ok"}', content_type="application/json")


async def _readyz(_request: web.Request) -> web.Response:
    body = json.dumps({"ready": _ready}).encode()
    status = 200 if _ready else 503
    return web.Response(body=body, content_type="application/json", status=status)


async def start_health_server(
    *,
    port: int | None = None,
    host: str = "0.0.0.0",
    extra_routes: Callable[[web.Application], None] | None = None,
) -> None:
    """Start the health HTTP server in the background. Idempotent.

    ``extra_routes`` lets a caller mount additional routes on the same
    aiohttp app — useful when an agent needs a tiny HTTP surface (e.g.
    OAuth callbacks) without burning a second TCP port.
    """

    global _runner, _started
    if _started:
        return

    # ``PORT`` is the convention used by Railway / Render / Fly.io to
    # tell the container which port the platform will route traffic to.
    # Honour it before falling back to our explicit ``HEALTH_PORT``.
    env_port = os.getenv("PORT") or os.getenv("HEALTH_PORT") or "8080"
    bind_port = port if port is not None else int(env_port)
    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/readyz", _readyz)
    if extra_routes is not None:
        extra_routes(app)

    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    site = web.TCPSite(_runner, host=host, port=bind_port)
    await site.start()
    _started = True
    _log.info("health.server_started", host=host, port=bind_port)


async def stop_health_server() -> None:
    """Tear down the health server (used in tests + graceful shutdown)."""

    global _runner, _started
    if _runner is not None:
        await _runner.cleanup()
    _runner = None
    _started = False


def run_health_server_in_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Convenience launcher that schedules the server on a running loop."""

    target_loop = loop or asyncio.get_event_loop()
    target_loop.create_task(start_health_server())
