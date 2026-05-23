"""SSRF-safe HTTP client used by every outbound integration.

Hardening applied on top of ``aiohttp``:

- Resolves the destination host once and refuses private / link-local
  / loopback / reserved IP ranges. This blocks SSRF pivots to cloud
  metadata services (169.254.169.254), Docker networks, RFC1918, etc.
- Enforces an HTTPS-only allowlist in production (``ENVIRONMENT=production``).
- Hard timeouts on connect + read.
- Exponential backoff retries on transient 5xx / network errors.
- A capped response body size so we never page memory.
- Sanitised User-Agent identifying the agent.

This is the *only* approved way to call an external API.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from agents_shared.logging import get_logger
from agents_shared.sentry import capture_exception

_log = get_logger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
DEFAULT_MAX_BYTES = 4 * 1024 * 1024  # 4 MiB
DEFAULT_USER_AGENT = "innovation-lab-agents/1.0 (+https://fetch.ai)"


class SecurityFetchError(RuntimeError):
    """Raised when a request is denied or fails after retries."""


@dataclass(slots=True)
class FetchResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _is_safe_host(host: str) -> bool:
    """Reject literal IPs that fall inside any reserved range."""

    try:
        candidates = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for family, _type, _proto, _canon, sockaddr in candidates:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
        if family == socket.AF_INET6 and ip.ipv4_mapped is not None:
            mapped = ip.ipv4_mapped
            if mapped.is_private or mapped.is_loopback:
                return False
    return True


def _validate_url(url: str) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityFetchError(f"unsupported scheme: {parsed.scheme!r}")
    if os.getenv("ENVIRONMENT", "development") == "production" and parsed.scheme != "https":
        raise SecurityFetchError("https required in production")
    if not parsed.hostname:
        raise SecurityFetchError("missing host")
    if not _is_safe_host(parsed.hostname):
        raise SecurityFetchError(f"host blocked by SSRF policy: {parsed.hostname}")


async def fetch(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    data: Any = None,
    json: Any = None,
    timeout: aiohttp.ClientTimeout | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    retries: int = 3,
    backoff_base: float = 0.5,
) -> FetchResponse:
    """Perform a hardened HTTP request and return a :class:`FetchResponse`."""

    _validate_url(url)

    merged_headers: dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout or DEFAULT_TIMEOUT) as session,
                session.request(
                    method.upper(),
                    url,
                    headers=merged_headers,
                    params=params,
                    data=data,
                    json=json,
                    allow_redirects=False,
                ) as resp,
            ):
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise SecurityFetchError(f"response exceeded max_bytes={max_bytes}")

                if 500 <= resp.status < 600 and attempt < retries:
                    raise aiohttp.ClientResponseError(
                        resp.request_info,
                        resp.history,
                        status=resp.status,
                        message=resp.reason or "server error",
                    )

                return FetchResponse(
                    status=resp.status,
                    headers=dict(resp.headers.items()),
                    body=bytes(body),
                    url=str(resp.url),
                )
        except SecurityFetchError:
            raise
        except (TimeoutError, aiohttp.ClientError, ConnectionError) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            sleep_for = backoff_base * (2**attempt)
            _log.warning(
                "security_fetch.retry",
                url=url,
                method=method,
                attempt=attempt + 1,
                sleep=sleep_for,
                err=str(exc),
            )
            await asyncio.sleep(sleep_for)

    assert last_exc is not None
    capture_exception(last_exc, url=url, method=method)
    raise SecurityFetchError(f"failed after {retries + 1} attempts: {last_exc}") from last_exc


async def get(url: str, **kwargs: Any) -> FetchResponse:
    return await fetch("GET", url, **kwargs)


async def post(url: str, **kwargs: Any) -> FetchResponse:
    return await fetch("POST", url, **kwargs)
