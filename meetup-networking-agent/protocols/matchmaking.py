"""Structured request/response protocol for the AI Engine (DeltaV).

The agent's primary surface is the ASI:One chat protocol (free-form
``ChatMessage`` turns). This second protocol exposes the same two
capabilities — check-in and matching — as typed Pydantic models so
that the AI Engine and other agents can invoke them programmatically.

The protocol manifest is published on Agentverse via
``agent.include(matchmaking_protocol, publish_manifest=True)``. The
manifest's natural-language descriptions are what DeltaV uses to decide
when to route a user prompt here.

Field names map cleanly onto our real handler signatures:

* ``CheckInRequest`` → :func:`handlers.profile_fetch.fetch_linkedin_profile`
  + :func:`handlers.profile_fetch.fetch_github_profile`
  + :func:`handlers.profile_fetch.merge_profiles`
  + :func:`handlers.embedding.upsert_attendee_with_embedding`
* ``MatchRequest`` → :func:`db.fetch_attendee_by_linkedin`
  + :func:`handlers.matching.get_matches`
* ``RefineRequest`` → re-runs :func:`handlers.matching.get_matches` with
  a ``RefinementQuery`` parsed from natural language.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from agents_shared.logging import get_logger
from agents_shared.sentry import capture_exception
from pydantic import BaseModel, ConfigDict, Field
from uagents import Context, Protocol

from db import fetch_attendee_by_linkedin
from handlers.conversation import parse_refinement
from handlers.embedding import upsert_attendee_with_embedding
from handlers.matching import get_matches
from handlers.profile_fetch import (
    fetch_github_profile,
    fetch_linkedin_profile,
    merge_profiles,
)

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


IntentLiteral = Literal["co-founder", "contributor", "mentor", "friend", "general"]


class CheckInRequest(_Model):
    """Check an attendee into an event using freshly-issued OAuth tokens.

    The agent will fetch both profiles, merge them, embed the result and
    upsert them into the attendee + embedding tables idempotently. Pass
    the access tokens fresh — they are NOT stored beyond this call.
    """

    event_id: str = Field(description="The meetup event UUID from the QR code.")
    linkedin_access_token: str = Field(
        description="Short-lived LinkedIn OAuth bearer token for /v2/userinfo (or legacy /v2/me)."
    )
    github_access_token: str | None = Field(
        default=None,
        description="Optional GitHub OAuth token; omit for LinkedIn-only check-in.",
    )


class CheckInResponse(_Model):
    success: bool
    attendee_id: str = Field(
        default="",
        description="UUID of the row written to meetup_attendees; empty on failure.",
    )
    linkedin_id: str = Field(default="", description="Resolved LinkedIn profile id.")
    total_attendees: int = Field(
        default=0,
        description="How many people have checked into this event so far.",
    )
    message: str


class MatchedPerson(_Model):
    """One match result, flattened for transport."""

    attendee_id: str
    display_name: str
    headline: str | None = None
    current_role: str | None = None
    company: str | None = None
    linkedin_url: str | None = None
    score: float
    reason: str = Field(description="One-sentence explanation of the match.")
    signals: list[str] = Field(
        default_factory=list,
        description="Short labels describing why this person matched.",
    )


class MatchRequest(_Model):
    """Return the top connections for an already-checked-in attendee.

    The attendee is resolved by ``(event_id, linkedin_id)`` — the
    ``linkedin_id`` must be the same one returned by a prior
    ``CheckInResponse`` (or by LinkedIn ``/v2/userinfo`` ``sub``).
    """

    event_id: str = Field(description="The meetup event UUID from the QR code.")
    linkedin_id: str = Field(
        description="The requester's LinkedIn profile id (returned by check-in)."
    )
    intent: IntentLiteral = Field(
        description="What kind of connection the user is looking for."
    )
    refinement: str | None = Field(
        default=None,
        description="Optional natural-language filter, e.g. 'only backend devs in fintech'.",
    )
    final_k: int = Field(default=5, ge=1, le=10)


class MatchResponse(_Model):
    matches: list[MatchedPerson] = Field(default_factory=list)
    total_attendees: int = 0
    message: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


matchmaking_protocol = Protocol(name="MeetupMatchmaking", version="1.0.0")


@matchmaking_protocol.on_message(model=CheckInRequest, replies=CheckInResponse)
async def handle_checkin(ctx: Context, sender: str, msg: CheckInRequest) -> None:
    try:
        event_uuid = UUID(msg.event_id)
    except ValueError:
        await ctx.send(
            sender,
            CheckInResponse(success=False, message="invalid event_id (must be a UUID)"),
        )
        return

    try:
        li_dict = await fetch_linkedin_profile(msg.linkedin_access_token)
        gh_dict: dict[str, Any] | None = None
        if msg.github_access_token:
            gh_dict = await fetch_github_profile(msg.github_access_token)
        profile = merge_profiles(li_dict, gh_dict)
        record = await upsert_attendee_with_embedding(
            event_id=event_uuid, profile=profile
        )
    except Exception as exc:
        capture_exception(exc, op="protocols.checkin", event_id=msg.event_id)
        ctx.logger.exception("protocols.checkin_failed")
        await ctx.send(sender, CheckInResponse(success=False, message=str(exc)))
        return

    total = await _count_event_attendees(event_uuid)
    await ctx.send(
        sender,
        CheckInResponse(
            success=True,
            attendee_id=str(record.id),
            linkedin_id=profile.linkedin_id,
            total_attendees=total,
            message=(
                f"Checked in. {total} {'person is' if total == 1 else 'people are'} "
                "at this event so far."
            ),
        ),
    )


@matchmaking_protocol.on_message(model=MatchRequest, replies=MatchResponse)
async def handle_match(ctx: Context, sender: str, msg: MatchRequest) -> None:
    try:
        event_uuid = UUID(msg.event_id)
    except ValueError:
        await ctx.send(
            sender,
            MatchResponse(message="invalid event_id (must be a UUID)"),
        )
        return

    attendee = await fetch_attendee_by_linkedin(event_uuid, msg.linkedin_id)
    if attendee is None:
        await ctx.send(
            sender,
            MatchResponse(
                message=(
                    "You're not checked into this event yet — "
                    "send a CheckInRequest first."
                )
            ),
        )
        return

    total = await _count_event_attendees(event_uuid)
    if total < 2:
        await ctx.send(
            sender,
            MatchResponse(
                total_attendees=total,
                message=(
                    "You're one of the first to scan in. Ask people around you to "
                    "scan the QR — I'll match you as the room fills up."
                ),
            ),
        )
        return

    refinement_query = None
    if msg.refinement:
        try:
            refinement_query = await parse_refinement(msg.refinement, context=None)
        except Exception as exc:
            capture_exception(exc, op="protocols.refinement_parse")
            refinement_query = None

    try:
        results = await get_matches(
            event_id=event_uuid,
            attendee_id=attendee.id,
            intent=msg.intent,
            refinement=refinement_query,
            final_k=msg.final_k,
        )
    except Exception as exc:
        capture_exception(exc, op="protocols.match", event_id=msg.event_id)
        ctx.logger.exception("protocols.match_failed")
        await ctx.send(
            sender,
            MatchResponse(total_attendees=total, message=f"matching failed: {exc}"),
        )
        return

    flat = [
        MatchedPerson(
            attendee_id=str(m.attendee_id),
            display_name=m.display_name,
            headline=m.headline,
            current_role=m.current_role,
            company=m.company,
            linkedin_url=m.linkedin_url,
            score=round(m.rerank_score, 4),
            reason=m.explanation.sentence,
            signals=[s.label for s in m.explanation.signals],
        )
        for m in results
    ]
    await ctx.send(
        sender,
        MatchResponse(
            matches=flat,
            total_attendees=total,
            message=f"Found {len(flat)} match{'es' if len(flat) != 1 else ''} for you.",
        ),
    )


async def _count_event_attendees(event_id: UUID) -> int:
    from agents_shared.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM meetup_attendees WHERE event_id = $1",
            event_id,
        )
    return int(n or 0)
