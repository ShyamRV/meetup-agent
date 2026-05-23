"""HTTP endpoint that receives LinkedIn / GitHub OAuth callbacks.

The chat protocol can't itself host a browser redirect, so the agent
embeds a tiny aiohttp app that:

1. Accepts ``GET /oauth/{platform}/callback?code=...&state=...``
2. Validates the signed state with :func:`handle_oauth_callback`
3. Stashes the resulting tokens on the appropriate session in
   :class:`SessionStore`
4. Returns a minimal "you can return to the chat" HTML page

The same aiohttp ``web.Application`` is mounted under the existing
health-check server so a single TCP port serves both purposes — the
deployment topology remains "one container, one port".
"""

from __future__ import annotations

from agents_shared.logging import get_logger
from agents_shared.rate_limit import InMemoryRateLimiter, RateLimitExceeded
from agents_shared.sentry import capture_exception
from aiohttp import web

from handlers.onboarding import (
    OAuthError,
    handle_oauth_callback,
    peek_session_id_from_state,
)
from session import get_session_store

_log = get_logger(__name__)

_CALLBACK_LIMITER = InMemoryRateLimiter(limit=30, window_s=60)


_HTML_SUCCESS = (
    "<!doctype html>\n"
    "<html><head><title>Connected</title>\n"
    "<style>body{font-family:system-ui;text-align:center;margin-top:18vh;color:#222}\n"
    "h1{font-size:1.6rem}p{color:#555}</style></head>\n"
    "<body>\n"
    "<h1>You're connected.</h1>\n"
    "<p>Return to the chat in ASI:One \u2014 your check-in will continue automatically.</p>\n"
    "</body></html>\n"
).encode()


async def _oauth_callback(request: web.Request) -> web.Response:
    platform = request.match_info["platform"]
    if platform not in ("linkedin", "github"):
        return web.Response(status=404, text="unknown platform")

    code = request.query.get("code", "")
    state = request.query.get("state", "")
    if not code or not state:
        return web.Response(status=400, text="missing code/state")

    # The session_id is embedded in the signed state. We pull it out
    # without verifying the HMAC just to look up the session row; the
    # full signature check happens in ``handle_oauth_callback`` before
    # any side effects.
    session_id = (
        request.query.get("session_id", "") or peek_session_id_from_state(state) or ""
    )
    if not session_id:
        return web.Response(status=400, text="missing session_id in state")

    try:
        await _CALLBACK_LIMITER.require(f"{platform}:{request.remote}")
    except RateLimitExceeded:
        return web.Response(status=429, text="rate limit exceeded")

    try:
        event_id, tokens = await handle_oauth_callback(platform, code, state, session_id=session_id)
    except OAuthError as exc:
        _log.warning("callback.oauth_rejected", platform=platform, err=str(exc))
        return web.Response(status=400, text=f"oauth rejected: {exc}")
    except Exception as exc:
        capture_exception(exc, op="callback.handler", platform=platform)
        return web.Response(status=500, text="internal error")

    store = get_session_store()
    session = store.get_by_session_id(session_id)
    if session is None:
        return web.Response(status=410, text="session expired — please rescan QR")

    session.event_id = session.event_id or event_id
    if platform == "linkedin":
        session.linkedin_tokens = tokens
    else:
        session.github_tokens = tokens

    _log.info(
        "callback.tokens_stored",
        platform=platform,
        session_id=session_id,
        event_id=event_id,
    )
    return web.Response(body=_HTML_SUCCESS, content_type="text/html")


def register_callback_routes(app: web.Application) -> None:
    """Mount the callback routes onto an existing aiohttp app."""

    app.router.add_get("/oauth/{platform}/callback", _oauth_callback)
