"""Typed SQL helpers — the only place this agent runs raw queries.

Every write goes through ``ON CONFLICT DO UPDATE`` so retries are safe;
every read returns ``None`` (not an exception) for "missing" so callers
can branch cleanly on the registration state.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import asyncpg
from agents_shared.db import get_pool
from agents_shared.logging import get_logger

from models.attendee import AttendeeRecord

_log = get_logger(__name__)


def _row_to_attendee(row: asyncpg.Record) -> AttendeeRecord:
    profile_json = row["profile_json"]
    if isinstance(profile_json, str):
        profile_json = json.loads(profile_json)
    return AttendeeRecord(
        id=row["id"],
        event_id=row["event_id"],
        linkedin_id=row["linkedin_id"],
        github_username=row["github_username"],
        display_name=row["display_name"],
        headline=row["headline"],
        current_role=row["current_role"],
        skills=list(row["skills"] or []),
        profile_json=profile_json,
        linkedin_url=row["linkedin_url"],
        checked_in_at=row["checked_in_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# meetup_events
# ---------------------------------------------------------------------------


async def upsert_event(
    *,
    event_name: str,
    qr_seed: str,
    organizer_email: str | None = None,
    location: str | None = None,
    event_date: Any = None,
    expires_at: Any = None,
) -> UUID:
    """Insert or update an event row keyed by ``qr_seed`` (unique)."""

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO meetup_events
                (event_name, qr_seed, organizer_email, location, event_date, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (qr_seed)
            DO UPDATE SET
                event_name = EXCLUDED.event_name,
                organizer_email = EXCLUDED.organizer_email,
                location = EXCLUDED.location,
                event_date = EXCLUDED.event_date,
                expires_at = EXCLUDED.expires_at
            RETURNING id
            """,
            event_name,
            qr_seed,
            organizer_email,
            location,
            event_date,
            expires_at,
        )
    return row["id"]


async def fetch_event(event_id: UUID) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM meetup_events WHERE id = $1",
            event_id,
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# meetup_attendees
# ---------------------------------------------------------------------------


async def upsert_attendee(
    *,
    event_id: UUID,
    linkedin_id: str,
    display_name: str,
    profile_json: dict[str, Any],
    github_username: str | None = None,
    headline: str | None = None,
    current_role: str | None = None,
    skills: Iterable[str] | None = None,
    linkedin_url: str | None = None,
) -> AttendeeRecord:
    """Insert or update an attendee. Idempotent on (event_id, linkedin_id)."""

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO meetup_attendees (
                event_id, linkedin_id, github_username, display_name,
                headline, "current_role", skills, profile_json, linkedin_url
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            ON CONFLICT (event_id, linkedin_id)
            DO UPDATE SET
                github_username = EXCLUDED.github_username,
                display_name = EXCLUDED.display_name,
                headline = EXCLUDED.headline,
                "current_role" = EXCLUDED."current_role",
                skills = EXCLUDED.skills,
                profile_json = EXCLUDED.profile_json,
                linkedin_url = EXCLUDED.linkedin_url,
                updated_at = NOW()
            RETURNING *
            """,
            event_id,
            linkedin_id,
            github_username,
            display_name,
            headline,
            current_role,
            list(skills or []),
            json.dumps(profile_json),
            linkedin_url,
        )
    _log.info(
        "db.attendee_upserted",
        event_id=str(event_id),
        linkedin_id=linkedin_id,
        attendee_id=str(row["id"]),
    )
    return _row_to_attendee(row)


async def fetch_attendee_by_linkedin(event_id: UUID, linkedin_id: str) -> AttendeeRecord | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM meetup_attendees WHERE event_id = $1 AND linkedin_id = $2",
            event_id,
            linkedin_id,
        )
    return _row_to_attendee(row) if row else None


async def fetch_attendee_by_id(attendee_id: UUID) -> AttendeeRecord | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM meetup_attendees WHERE id = $1",
            attendee_id,
        )
    return _row_to_attendee(row) if row else None


async def list_event_attendees(
    event_id: UUID, *, exclude_id: UUID | None = None
) -> list[AttendeeRecord]:
    pool = await get_pool()
    params: list[Any] = [event_id]
    extra = ""
    if exclude_id is not None:
        params.append(exclude_id)
        extra = "AND id <> $2"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM meetup_attendees WHERE event_id = $1 {extra}",
            *params,
        )
    return [_row_to_attendee(r) for r in rows]


# ---------------------------------------------------------------------------
# meetup_connections
# ---------------------------------------------------------------------------


async def insert_connection(
    *,
    event_id: UUID,
    from_attendee_id: UUID,
    to_attendee_id: UUID,
    match_score: float,
    match_reason: str,
    status: str = "shown",
) -> UUID:
    """Insert a shown-match row idempotently per (event, from, to)."""

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO meetup_connections
                (event_id, from_attendee_id, to_attendee_id, status,
                 match_score, match_reason)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (event_id, from_attendee_id, to_attendee_id)
            DO UPDATE SET
                match_score = EXCLUDED.match_score,
                match_reason = EXCLUDED.match_reason,
                status = EXCLUDED.status
            RETURNING id
            """,
            event_id,
            from_attendee_id,
            to_attendee_id,
            status,
            match_score,
            match_reason,
        )
    return row["id"]
