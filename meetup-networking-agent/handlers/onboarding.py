"""Onboarding handler — LinkedIn + GitHub OAuth 2.0 (Authorization Code).

Flow:

1. ``build_linkedin_oauth_url`` returns the URL the user clicks. The
   ``state`` parameter is an HMAC over ``(event_id, session_id, platform)``
   signed with ``SESSION_SECRET``. We never trust the raw values that
   come back from the provider — they must match the HMAC.
2. After consent the provider redirects to ``LINKEDIN_REDIRECT_URI`` with
   ``code`` + ``state``.
3. ``handle_oauth_callback`` validates the ``state`` and exchanges the
   ``code`` for an access token via the provider's token endpoint.

All HTTP calls go through ``agents_shared.security_fetch`` so the SSRF
policy and retries apply uniformly. Tokens are returned in-memory only —
the caller hashes them before any persistence per the project rule
"never store raw OAuth tokens beyond the session".
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

from agents_shared import security_fetch
from agents_shared.logging import get_logger
from agents_shared.rate_limit import InMemoryRateLimiter
from agents_shared.sentry import capture_exception

from config import get_settings

_log = get_logger(__name__)

Platform = Literal["linkedin", "github"]

LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
# LinkedIn deprecated the legacy ``r_liteprofile`` family in 2023. New
# apps can only get "Sign In with LinkedIn using OpenID Connect", which
# uses these three scopes and the ``/v2/userinfo`` endpoint. If you
# have a grandfathered legacy app, set ``LINKEDIN_SCOPES`` in env.
LINKEDIN_SCOPES = (
    os.getenv("LINKEDIN_SCOPES", "openid profile email").split()
)

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_SCOPES = ["read:user", "user:email"]

_STATE_TTL_SECONDS = 600  # 10 minutes — covers typical user delays
_CALLBACK_LIMITER = InMemoryRateLimiter(limit=10, window_s=60)


# ---------------------------------------------------------------------------
# State signing
# ---------------------------------------------------------------------------


def _sign_state(*, event_id: str, session_id: str, platform: Platform, nonce: str, ts: int) -> str:
    settings = get_settings()
    payload = f"{platform}|{event_id}|{session_id}|{nonce}|{ts}"
    sig = hmac.new(
        settings.session_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}|{sig}"


def _verify_state(state: str, *, platform: Platform, expected_session_id: str) -> tuple[str, str]:
    """Validate the signed state and return ``(event_id, nonce)``.

    Raises :class:`OAuthError` if the signature is invalid, the session
    doesn't match, or the state has expired.
    """

    settings = get_settings()
    try:
        platform_part, event_id, session_id, nonce, ts_str, sig = state.split("|")
        ts = int(ts_str)
    except (ValueError, AttributeError) as exc:
        raise OAuthError("malformed state") from exc

    if platform_part != platform:
        raise OAuthError("platform mismatch")
    if session_id != expected_session_id:
        raise OAuthError("session_id mismatch")
    if time.time() - ts > _STATE_TTL_SECONDS:
        raise OAuthError("state expired")

    payload = f"{platform_part}|{event_id}|{session_id}|{nonce}|{ts}"
    expected_sig = hmac.new(
        settings.session_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise OAuthError("invalid state signature")
    return event_id, nonce


def peek_session_id_from_state(state: str) -> str | None:
    """Return the ``session_id`` embedded in ``state`` without verifying it.

    This is purely a routing helper used by the HTTP callback handler so
    it knows which in-memory session to update. The real HMAC check
    still happens inside :func:`handle_oauth_callback` before any side
    effects are applied — a tampered state will be rejected there.
    """

    try:
        _platform, _event, session_id, _nonce, _ts, _sig = state.split("|")
    except ValueError:
        return None
    return session_id or None


class OAuthError(RuntimeError):
    """Raised when an OAuth state/code is rejected."""


@dataclass(slots=True)
class OAuthTokens:
    """In-memory token result. Hash before persisting."""

    platform: Platform
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None

    def access_token_fingerprint(self) -> str:
        """SHA-256 of the access token — safe to log / persist as audit trail."""

        return hashlib.sha256(self.access_token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def build_linkedin_oauth_url(event_id: str, session_id: str) -> str:
    """Return the LinkedIn authorization URL with a signed ``state``."""

    settings = get_settings()
    if not settings.linkedin_client_id or not settings.linkedin_redirect_uri:
        raise RuntimeError("LinkedIn OAuth is not configured")

    state = _sign_state(
        event_id=event_id,
        session_id=session_id,
        platform="linkedin",
        nonce=secrets.token_urlsafe(12),
        ts=int(time.time()),
    )
    params = {
        "response_type": "code",
        "client_id": settings.linkedin_client_id,
        "redirect_uri": settings.linkedin_redirect_uri,
        "state": state,
        "scope": " ".join(LINKEDIN_SCOPES),
    }
    return f"{LINKEDIN_AUTH_URL}?{urlencode(params)}"


def build_github_oauth_url(event_id: str, session_id: str) -> str:
    """Return the GitHub authorization URL with a signed ``state``."""

    settings = get_settings()
    if not settings.github_client_id or not settings.github_redirect_uri:
        raise RuntimeError("GitHub OAuth is not configured")

    state = _sign_state(
        event_id=event_id,
        session_id=session_id,
        platform="github",
        nonce=secrets.token_urlsafe(12),
        ts=int(time.time()),
    )
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_redirect_uri,
        "state": state,
        "scope": " ".join(GITHUB_SCOPES),
        "allow_signup": "false",
    }
    return f"{GITHUB_AUTH_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------


async def handle_oauth_callback(
    platform: Platform,
    code: str,
    state: str,
    *,
    session_id: str,
) -> tuple[str, OAuthTokens]:
    """Validate ``state`` + exchange ``code`` for tokens. Returns ``(event_id, tokens)``."""

    if not await _CALLBACK_LIMITER.check(f"{platform}:{session_id}"):
        raise OAuthError("rate limit exceeded for callback")

    event_id, _nonce = _verify_state(state, platform=platform, expected_session_id=session_id)

    if platform == "linkedin":
        tokens = await _exchange_linkedin_code(code)
    elif platform == "github":
        tokens = await _exchange_github_code(code)
    else:  # pragma: no cover - exhaustive
        raise OAuthError(f"unsupported platform: {platform}")

    _log.info(
        "onboarding.token_exchanged",
        platform=platform,
        event_id=event_id,
        session_id=session_id,
        token_fp=tokens.access_token_fingerprint()[:12],
    )
    return event_id, tokens


async def _exchange_linkedin_code(code: str) -> OAuthTokens:
    settings = get_settings()
    try:
        resp = await security_fetch.post(
            LINKEDIN_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.linkedin_redirect_uri,
                    "client_id": settings.linkedin_client_id,
                    "client_secret": settings.linkedin_client_secret,
                }
            ),
        )
    except Exception as exc:
        capture_exception(exc, op="onboarding.linkedin_token")
        raise OAuthError("linkedin token endpoint failed") from exc

    if resp.status >= 400:
        raise OAuthError(f"linkedin token endpoint returned {resp.status}: {resp.text[:200]}")
    body = resp.json()
    return OAuthTokens(
        platform="linkedin",
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=body.get("expires_in"),
        scope=body.get("scope"),
    )


async def _exchange_github_code(code: str) -> OAuthTokens:
    settings = get_settings()
    try:
        resp = await security_fetch.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.github_redirect_uri,
            },
        )
    except Exception as exc:
        capture_exception(exc, op="onboarding.github_token")
        raise OAuthError("github token endpoint failed") from exc

    if resp.status >= 400:
        raise OAuthError(f"github token endpoint returned {resp.status}: {resp.text[:200]}")
    body = resp.json()
    if "error" in body:
        raise OAuthError(f"github error: {body['error_description'] or body['error']}")
    return OAuthTokens(
        platform="github",
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=body.get("expires_in"),
        scope=body.get("scope"),
    )
