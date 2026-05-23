"""meetup-networking-agent — entry point.

Run with ``python agent.py``. The agent does three things at boot:

1. Initialises Sentry from the shared SDK.
2. Boots an aiohttp server hosting ``/healthz``, ``/readyz`` *and* the
   OAuth callback routes.
3. Verifies the Postgres connection. Only then does it flip readiness on
   — the load balancer will keep traffic away until that check passes.

The chat protocol routes incoming :class:`ChatMessage` payloads through
the onboarding path until the requester is in ``meetup_attendees``;
after that, every message is treated as a matching / refinement turn.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime

# uAgents 0.25.x constructs ``Agent(...)`` at module-import time and calls
# ``asyncio.get_event_loop_policy().get_event_loop()`` inside. On Python
# 3.12+ (and especially 3.14) that raises ``RuntimeError`` if no loop has
# been created on the current thread. Production runs under uvicorn /
# asyncio.run which always has a loop, but importing this module from a
# bare script (CLI, tests, ``python -c``) does not. We pre-create a loop
# here as a no-op for already-loop-equipped runtimes and a one-line fix
# for everything else.
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
from pathlib import Path
from uuid import UUID

from agents_shared import db as shared_db
from agents_shared import health
from agents_shared.logging import get_logger
from agents_shared.sentry import capture_exception
from agents_shared.sentry import init as sentry_init
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from callback_server import register_callback_routes
from config import get_settings
from db import fetch_attendee_by_linkedin, fetch_event
from handlers import matching, profile_fetch
from handlers.conversation import parse_refinement
from handlers.embedding import upsert_attendee_with_embedding
from handlers.onboarding import (
    build_github_oauth_url,
    build_linkedin_oauth_url,
)
from handlers.profile_fetch import merge_profiles
from models.match import MatchResult
from models.refinement import Intent
from protocols import matchmaking_protocol
from session import Session, get_session_store

_log = get_logger(__name__)

settings = get_settings()

_EVENT_ID_PATTERN = re.compile(
    r"event_id[=:]\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


# ---------------------------------------------------------------------------
# uAgent + chat protocol
# ---------------------------------------------------------------------------

def _build_agent() -> Agent:
    """Construct the Agent with the right transport for the environment.

    * ``AGENT_MAILBOX=true`` => Agentverse mailbox (Railway / Render / Fly
      deployments where we don't want to manage an inbound TLS port for
      uAgent envelopes). The local aiohttp server on ``$PORT`` still
      serves health probes + OAuth callbacks.
    * Otherwise => bind a local HTTP submit endpoint at
      ``AGENT_ENDPOINT``. This is what local dev uses.
    """

    kwargs: dict = {
        "name": settings.agent_name,
        "seed": settings.agent_seed,
        "port": settings.agent_port,
        "description": settings.agent_description,
        "publish_agent_details": True,
    }
    if settings.agent_handle:
        kwargs["handle"] = settings.agent_handle
    if settings.agent_mailbox:
        kwargs["mailbox"] = True
        kwargs["agentverse"] = settings.agentverse_base_url
    else:
        kwargs["endpoint"] = [settings.agent_endpoint]
    return Agent(**kwargs)


agent = _build_agent()

chat_proto = Protocol(spec=chat_protocol_spec)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _text_message(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=_now(),
        msg_id=uuid.uuid4(),
        content=[TextContent(type="text", text=text)],
    )


def _end_message(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=_now(),
        msg_id=uuid.uuid4(),
        content=[
            TextContent(type="text", text=text),
            EndSessionContent(type="end-session"),
        ],
    )


def _extract_event_id(text: str) -> str | None:
    match = _EVENT_ID_PATTERN.search(text)
    return match.group(1) if match else None


async def _count_peers(event_id: UUID, *, exclude_id: UUID) -> int:
    """Return how many *other* attendees are in this event."""

    pool = await shared_db.get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM meetup_attendees "
            "WHERE event_id = $1 AND id <> $2",
            event_id,
            exclude_id,
        )
    return int(n or 0)


def _format_match(idx: int, m: MatchResult) -> str:
    role_line = m.current_role or m.headline or ""
    if m.company:
        role_line = f"{role_line} @ {m.company}" if role_line else m.company
    parts = [f"{idx}. {m.display_name}"]
    if role_line:
        parts[0] += f" — {role_line}"
    parts.append(f"   {m.explanation.sentence}")
    if m.explanation.signals:
        signal_line = " · ".join(
            f"{s.label}{f' ({s.detail})' if s.detail else ''}" for s in m.explanation.signals
        )
        parts.append(f"   Signals: {signal_line}")
    if m.linkedin_url:
        parts.append(f"   {m.linkedin_url}")
    return "\n".join(parts)


def _format_matches(matches: Iterable[MatchResult]) -> str:
    items = list(matches)
    if not items:
        return (
            "I couldn't find any strong matches in the room yet — try checking "
            "back in a few minutes as more people arrive."
        )
    return "\n\n".join(_format_match(i + 1, m) for i, m in enumerate(items))


async def _ensure_event_known(ctx: Context, session: Session, text: str) -> bool:
    """Resolve and persist the event_id for the session if not already set."""

    if session.event_id:
        return True
    candidate = _extract_event_id(text)
    if candidate is None:
        return False
    try:
        event = await fetch_event(UUID(candidate))
    except Exception as exc:
        capture_exception(exc, op="agent.fetch_event", event_id=candidate)
        return False
    if event is None:
        ctx.logger.warning("agent.unknown_event %s", candidate)
        return False
    session.event_id = candidate
    return True


# ---------------------------------------------------------------------------
# Chat handlers
# ---------------------------------------------------------------------------


@chat_proto.on_message(ChatMessage)
async def on_chat_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    """Route an incoming chat turn through onboarding or matching."""

    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=_now(), acknowledged_msg_id=msg.msg_id),
    )

    store = get_session_store()
    session_id = str(msg.msg_id)
    session = store.get_or_create(sender, session_id=session_id)

    # Pull text content out of the chat payload.
    text_parts = [c.text for c in msg.content if isinstance(c, TextContent)]
    incoming_text = "\n".join(text_parts).strip()

    # Start-of-session greeting (QR scan landing).
    if any(isinstance(c, StartSessionContent) for c in msg.content):
        await _greet(ctx, sender, session, incoming_text)
        # If the greeting already resolved an attendee (dev-mode bypass
        # for a pre-seeded profile, or returning sender we recognised)
        # and the same message also carried a real request beyond the
        # event_id marker, process that request in the same turn. This
        # lets a one-shot chat client (e.g. ``scripts/dev_chat.py``)
        # walk the full journey without forcing two round-trips.
        if session.attendee_id and incoming_text:
            meaningful = _EVENT_ID_PATTERN.sub("", incoming_text).strip()
            if meaningful:
                try:
                    attendee = await _try_load_attendee(session)
                    if attendee is not None:
                        await _handle_matching(
                            ctx, sender, session, attendee, meaningful
                        )
                except Exception as exc:
                    capture_exception(exc, op="agent.greet_followthrough")
                    ctx.logger.exception("agent.greet_followthrough_failed")
        return

    if not incoming_text:
        await ctx.send(sender, _text_message("Could you re-send that? I didn't catch any text."))
        return

    session.history.append(incoming_text)

    try:
        if not session.event_id and not await _ensure_event_known(ctx, session, incoming_text):
            await _greet(ctx, sender, session, incoming_text)
            return

        attendee = await _try_load_attendee(session)
        if attendee is None:
            await _continue_onboarding(ctx, sender, session)
            return

        session.attendee_id = attendee.id
        await _handle_matching(ctx, sender, session, attendee, incoming_text)
    except Exception as exc:
        capture_exception(exc, op="agent.on_chat_message", sender=sender)
        ctx.logger.exception("agent.unhandled_error")
        await ctx.send(
            sender,
            _text_message("Sorry — something went sideways on my end. Could you try that again?"),
        )


@chat_proto.on_message(ChatAcknowledgement)
async def on_chat_ack(_ctx: Context, _sender: str, _msg: ChatAcknowledgement) -> None:
    """No-op ack handler — required by the chat protocol spec."""


# ---------------------------------------------------------------------------
# Onboarding sub-flow
# ---------------------------------------------------------------------------


async def _greet(ctx: Context, sender: str, session: Session, text: str) -> None:
    event_id_candidate = _extract_event_id(text) or session.event_id
    if event_id_candidate:
        session.event_id = event_id_candidate
        event = None
        try:
            event = await fetch_event(UUID(event_id_candidate))
        except Exception as exc:
            capture_exception(exc, op="agent.greet_fetch_event")
        event_name = (event or {}).get("event_name", "the event")

        # Dev-mode shortcut: if the requester is already seeded as an
        # attendee for this event, greet them as returning and jump
        # straight to matching. This keeps the local test loop usable
        # without real OAuth credentials.
        if (
            settings.environment != "production"
            and settings.dev_linkedin_id
        ):
            attendee = await fetch_attendee_by_linkedin(
                UUID(event_id_candidate), settings.dev_linkedin_id
            )
            if attendee is not None:
                session.attendee_id = attendee.id
                await ctx.send(
                    sender,
                    _text_message(
                        f"Welcome back to {event_name}, {attendee.display_name}! "
                        "(Dev mode — skipped LinkedIn/GitHub auth, used your "
                        "pre-seeded profile.) "
                        "Who are you hoping to meet today — a co-founder, "
                        "contributor, mentor or just friendly faces?"
                    ),
                )
                return

        await ctx.send(
            sender,
            _text_message(
                f"Welcome to {event_name}! I'm your meetup networking agent — "
                "I'll help you find a few people in the room worth talking to. "
                "I need a quick read of your LinkedIn (required) and GitHub "
                "(optional but helpful) to do that. Your data is auto-deleted "
                f"{settings.retention_days} days after the event. "
                "Ready to connect LinkedIn?"
            ),
        )
        linkedin_url = build_linkedin_oauth_url(event_id_candidate, session.session_id)
        await ctx.send(sender, _text_message(f"LinkedIn auth: {linkedin_url}"))
        return

    await ctx.send(
        sender,
        _text_message(
            "Hi! I'm the meetup networking agent. I help people at a tech event "
            "find the right person to talk to next. It looks like you got here "
            "without an event link — could you re-scan the QR code at the venue?"
        ),
    )


async def _try_load_attendee(session: Session):
    if session.attendee_id is not None:
        from db import fetch_attendee_by_id

        return await fetch_attendee_by_id(session.attendee_id)

    # Dev-mode bypass: skip OAuth entirely and resolve the requester by a
    # pre-seeded linkedin_id. The production guard means this is a no-op
    # in real deployments even if the env var leaks in.
    if (
        settings.environment != "production"
        and settings.dev_linkedin_id
        and session.event_id
    ):
        return await fetch_attendee_by_linkedin(
            UUID(session.event_id), settings.dev_linkedin_id
        )

    if not session.linkedin_tokens or not session.event_id:
        return None
    li_id = await _extract_linkedin_id(session)
    if li_id is None:
        return None
    return await fetch_attendee_by_linkedin(UUID(session.event_id), li_id)


async def _extract_linkedin_id(session: Session) -> str | None:
    """Lazily call /me once we have a LinkedIn token, just for the user id."""

    if not session.linkedin_tokens:
        return None
    try:
        profile_dict = await profile_fetch.fetch_linkedin_profile(
            session.linkedin_tokens.access_token
        )
    except Exception as exc:
        capture_exception(exc, op="agent.extract_linkedin_id")
        return None
    session.history.append(f"::linkedin_profile_cached::{profile_dict['id']}")
    session._cached_linkedin_dict = profile_dict
    return profile_dict["id"]


async def _continue_onboarding(ctx: Context, sender: str, session: Session) -> None:
    if not session.event_id:
        await ctx.send(
            sender,
            _text_message("I still need an event link — please re-scan the QR code."),
        )
        return

    if session.linkedin_tokens is None:
        url = build_linkedin_oauth_url(session.event_id, session.session_id)
        await ctx.send(
            sender,
            _text_message(
                "Let's start with LinkedIn — tap this link to authorise "
                f"(I'll fetch your name, headline, roles and skills): {url}"
            ),
        )
        return

    if session.github_tokens is None:
        url = build_github_oauth_url(session.event_id, session.session_id)
        await ctx.send(
            sender,
            _text_message(
                "Great, LinkedIn is connected. GitHub is optional but it helps me "
                "spot builders quickly — tap here to add it, or just say 'skip' to "
                f"continue without it: {url}"
            ),
        )
        return

    await _complete_checkin(ctx, sender, session)


async def _complete_checkin(ctx: Context, sender: str, session: Session) -> None:
    assert session.event_id and session.linkedin_tokens

    try:
        linkedin_dict = getattr(session, "_cached_linkedin_dict", None)
        if linkedin_dict is None:
            linkedin_dict = await profile_fetch.fetch_linkedin_profile(
                session.linkedin_tokens.access_token
            )
        github_dict = None
        if session.github_tokens is not None:
            github_dict = await profile_fetch.fetch_github_profile(
                session.github_tokens.access_token
            )
        profile = merge_profiles(linkedin_dict, github_dict)
    except Exception as exc:
        capture_exception(exc, op="agent.fetch_merge_profile")
        await ctx.send(
            sender,
            _text_message("I couldn't quite read your profile — could you re-authorise LinkedIn?"),
        )
        return

    try:
        attendee = await upsert_attendee_with_embedding(
            event_id=UUID(session.event_id),
            profile=profile,
        )
        session.attendee_id = attendee.id
    except Exception as exc:
        capture_exception(exc, op="agent.upsert_with_embedding")
        await ctx.send(
            sender,
            _text_message(
                "I had trouble saving your check-in. Please try sending any message again."
            ),
        )
        return

    detail = _greeting_detail(profile)
    await ctx.send(
        sender,
        _text_message(
            f"You're checked in, {profile.display_name.split()[0]}. {detail}"
            "\n\nWhat kind of connection are you looking for today — a co-founder, "
            "people to build with, a mentor, or just interesting humans to chat with?"
        ),
    )


def _greeting_detail(profile) -> str:
    if profile.github and profile.github.pinned_repos:
        repo = profile.github.pinned_repos[0]
        return f"Saw {repo.name} on your GitHub — nice work."
    if profile.headline:
        return f"{profile.headline} — got it."
    return "I've got the basics."


# ---------------------------------------------------------------------------
# Matching sub-flow
# ---------------------------------------------------------------------------


async def _handle_matching(
    ctx: Context,
    sender: str,
    session: Session,
    attendee,
    text: str,
) -> None:
    intent = Intent.from_text(text) if session.intent is None else Intent(session.intent)
    if session.intent is None:
        session.intent = intent.value

    refinement = None
    history_snapshot = session.history.snapshot()
    if any(_looks_like_refinement(t) for t in [text]):
        try:
            refinement = await parse_refinement(text, history_snapshot)
            if refinement.intent is not None:
                session.intent = refinement.intent.value
                intent = refinement.intent
        except Exception as exc:
            capture_exception(exc, op="agent.parse_refinement")
            refinement = None

    # Early bail-out when the requester is alone in the room — saves
    # an embedding lookup and gives a warmer "we'll catch you later"
    # message than the generic empty-results path below.
    try:
        peer_count = await _count_peers(attendee.event_id, exclude_id=attendee.id)
    except Exception as exc:
        capture_exception(exc, op="agent.count_peers")
        peer_count = None
    if peer_count == 0:
        await ctx.send(
            sender,
            _text_message(
                "You're the first to scan in here — nice! As more people "
                "check in I'll line up matches for you. In the meantime, "
                "tell me what you're hoping to find today (co-founder, "
                "contributor, mentor, friend) and I'll keep an eye out."
            ),
        )
        return

    try:
        results = await matching.get_matches(
            event_id=attendee.event_id,
            attendee_id=attendee.id,
            intent=intent,
            refinement=refinement,
        )
    except Exception as exc:
        capture_exception(exc, op="agent.get_matches")
        await ctx.send(
            sender,
            _text_message("I hit a snag pulling matches — give it a moment and try again."),
        )
        return

    if not results:
        await ctx.send(
            sender,
            _text_message(
                "You're early — no strong matches in the room yet. I'll keep "
                "watching as more people check in. Try again in a few minutes."
            ),
        )
        return

    body = _format_matches(results)
    await ctx.send(
        sender,
        _text_message(
            f"Here are {len(results)} people worth saying hi to:\n\n{body}"
            "\n\nWant me to narrow this down? Try things like 'only Rust devs', "
            "'anyone in fintech', or 'show me designers'."
        ),
    )


_REFINEMENT_HINT_PATTERN = re.compile(
    r"\b(only|show|who|any|find|looking for|narrow|filter|years?)\b", re.IGNORECASE
)


def _looks_like_refinement(text: str) -> bool:
    return bool(_REFINEMENT_HINT_PATTERN.search(text))


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    """Read the system prompt; agents that surface it to the LLM call this."""

    path = Path(__file__).parent / "prompts" / "system_prompt.txt"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _register_http_routes(app) -> None:
    """Mount OAuth callbacks AND the ``/status`` endpoint on the shared server."""

    from aiohttp import web as _web

    register_callback_routes(app)

    async def _status(_request: _web.Request) -> _web.Response:
        attendees = 0
        if settings.database_url:
            try:
                pool = await shared_db.get_pool()
                async with pool.acquire() as conn:
                    attendees = int(
                        await conn.fetchval("SELECT count(*) FROM meetup_attendees")
                        or 0
                    )
            except Exception as exc:
                capture_exception(exc, op="status.count_attendees")
        return _web.json_response(
            {
                "status": "live",
                "agent": settings.agent_name,
                "address": agent.address,
                "environment": settings.environment,
                "attendees_total": attendees,
            }
        )

    app.router.add_get("/status", _status)


@agent.on_event("startup")
async def _on_startup(ctx: Context) -> None:
    sentry_init(service=settings.agent_name, environment=settings.environment)

    await health.start_health_server(extra_routes=_register_http_routes)

    if settings.database_url:
        ok = await shared_db.healthcheck()
        if not ok:
            ctx.logger.error("agent.db_healthcheck_failed")
            return

    health.mark_ready()
    ctx.logger.info(
        json.dumps(
            {
                "event": "agent.ready",
                "agent": settings.agent_name,
                "env": settings.environment,
                "address": agent.address,
                "transport": "mailbox" if settings.agent_mailbox else "http",
                "prompt_len": len(_load_system_prompt()),
            }
        )
    )


@agent.on_event("shutdown")
async def _on_shutdown(_ctx: Context) -> None:
    health.mark_unready()
    await shared_db.close_pool()
    await health.stop_health_server()


agent.include(chat_proto, publish_manifest=True)

# The matchmaking protocol gives DeltaV / AI Engine a typed surface for
# the same capabilities (check-in + match) that the chat protocol
# exposes via free-form text. ``publish_manifest=True`` is what makes it
# discoverable by AI Engine.
agent.include(matchmaking_protocol, publish_manifest=True)


def main() -> None:
    """Process entry point — referenced by Dockerfile CMD and tests."""

    # Force UTF-8 on stdout/stderr so chat text and uAgents stdlib logs
    # survive non-UTF-8 host locales (notably Windows cp1252 consoles).
    # Our own structured logger already emits ASCII-safe JSON; this is a
    # defence-in-depth for any third-party logger that doesn't.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")

    try:
        agent.run()
    except Exception as exc:
        capture_exception(exc, op="agent.main")
        raise


if __name__ == "__main__":
    main()
