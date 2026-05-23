"""Sentry initialization wrapper.

All agents call :func:`init` once at boot and use :func:`capture_exception`
inside every ``except`` block. Sentry is a hard no-op if ``SENTRY_DSN`` is
unset, so local development never needs the SDK.
"""

from __future__ import annotations

import os
from typing import Any

from agents_shared.logging import get_logger

_log = get_logger(__name__)
_initialized = False

try:
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
except ImportError:  # pragma: no cover - sentry is optional at runtime
    sentry_sdk = None  # type: ignore[assignment]
    LoggingIntegration = None  # type: ignore[assignment]


def init(
    *,
    service: str,
    environment: str | None = None,
    release: str | None = None,
    traces_sample_rate: float = 0.05,
) -> bool:
    """Initialize Sentry. Returns ``True`` if the SDK was wired up."""

    global _initialized
    if _initialized:
        return True

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn or sentry_sdk is None:
        _log.info("sentry.disabled", service=service, reason="no_dsn_or_sdk")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=environment or os.getenv("ENVIRONMENT", "development"),
        release=release or os.getenv("RELEASE_SHA"),
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
        integrations=[LoggingIntegration(level=None, event_level=None)]
        if LoggingIntegration
        else [],
    )
    sentry_sdk.set_tag("service", service)
    _initialized = True
    _log.info("sentry.initialized", service=service)
    return True


def capture_exception(exc: BaseException, **context: Any) -> None:
    """Send an exception to Sentry with extra context (safe when disabled)."""

    if sentry_sdk is None or not _initialized:
        _log.exception("captured_exception", exc_info=exc, extra=context)
        return
    with sentry_sdk.push_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_exception(exc)


def capture_message(message: str, level: str = "info", **context: Any) -> None:
    """Send a breadcrumb message to Sentry."""

    if sentry_sdk is None or not _initialized:
        return
    with sentry_sdk.push_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_message(message, level=level)
