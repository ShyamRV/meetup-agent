"""Tests for refinement parsing — regex fallback path (no LLM key)."""

from __future__ import annotations

from datetime import UTC

import pytest

from handlers.conversation import apply_refinement, parse_refinement
from models.refinement import Intent


@pytest.mark.asyncio
async def test_parse_refinement_extracts_rust_or_go() -> None:
    q = await parse_refinement("find someone who knows Rust or Go")
    assert "rust" in q.required_skills
    assert "go" in q.required_skills or "golang" in q.required_skills


@pytest.mark.asyncio
async def test_parse_refinement_extracts_frontend_role() -> None:
    q = await parse_refinement("show me only frontend developers")
    assert "frontend" in q.required_skills or any("frontend" in r for r in q.roles)


@pytest.mark.asyncio
async def test_parse_refinement_extracts_fintech_domain() -> None:
    q = await parse_refinement("who here has worked in fintech")
    assert "fintech" in q.domains


@pytest.mark.asyncio
async def test_parse_refinement_extracts_min_years() -> None:
    q = await parse_refinement("show me people with 10+ years of experience")
    assert q.min_years_experience == 10


@pytest.mark.asyncio
async def test_parse_refinement_handles_negation() -> None:
    q = await parse_refinement("only python devs but not django")
    assert "python" in q.required_skills
    assert "django" in q.excluded_skills


@pytest.mark.asyncio
async def test_parse_refinement_intent_classification() -> None:
    cofounder = await parse_refinement("I'm looking for a co-founder")
    contributor = await parse_refinement("anyone wanna collaborate on open source")
    mentor = await parse_refinement("I want a mentor with more experience")
    friend = await parse_refinement("just want to meet interesting people")

    assert cofounder.intent is Intent.COFOUNDER
    assert contributor.intent is Intent.CONTRIBUTOR
    assert mentor.intent is Intent.MENTOR
    assert friend.intent is Intent.FRIEND


@pytest.mark.asyncio
async def test_parse_refinement_rejects_empty() -> None:
    with pytest.raises(ValueError):
        await parse_refinement("")


def test_apply_refinement_filters_by_required_skill() -> None:
    from datetime import date

    from handlers.matching import _Candidate
    from models.match import MatchSignal
    from models.profile import (
        LinkedInPosition,
        LinkedInProfile,
        ProfileData,
    )
    from models.refinement import RefinementQuery

    def _cand(linkedin_id: str, name: str, skills: list[str]) -> _Candidate:
        from datetime import datetime
        from uuid import uuid4

        from models.attendee import AttendeeRecord

        li = LinkedInProfile(
            id=linkedin_id,
            name=name,
            headline=f"{name} the dev",
            positions=[
                LinkedInPosition(
                    title="Engineer",
                    company="X",
                    start=date(2022, 1, 1),
                    end=None,
                )
            ],
            skills=skills,
        )
        profile = ProfileData(linkedin=li)
        now = datetime.now(tz=UTC)
        attendee = AttendeeRecord(
            id=uuid4(),
            event_id=uuid4(),
            linkedin_id=linkedin_id,
            display_name=name,
            skills=skills,
            profile_json=profile.to_storage_json(),
            checked_in_at=now,
            updated_at=now,
        )
        return _Candidate(
            attendee=attendee,
            profile=profile,
            base_score=0.7,
            rerank_score=0.7,
            signals=[MatchSignal(label="x", kind="shared")],
        )

    candidates = [
        _cand("a", "A", ["python", "rust"]),
        _cand("b", "B", ["javascript", "react"]),
        _cand("c", "C", ["go", "rust"]),
    ]
    refinement = RefinementQuery(raw_text="only rust devs", required_skills=["rust"])
    filtered = apply_refinement(candidates, refinement)

    assert {c.attendee.linkedin_id for c in filtered} == {"a", "c"}
