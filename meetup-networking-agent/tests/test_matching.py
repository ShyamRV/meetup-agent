"""End-to-end matching pipeline tests against the fake DB + vector store."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from handlers.embedding import upsert_attendee_with_embedding
from handlers.matching import get_matches
from models.profile import (
    Education,
    GitHubProfile,
    GitHubRepo,
    LinkedInPosition,
    LinkedInProfile,
    ProfileData,
)
from models.refinement import Intent


def _make_profile(
    *,
    linkedin_id: str,
    name: str,
    title: str,
    company: str,
    skills: list[str],
    bio: str | None = None,
    languages: list[str] | None = None,
    start_year: int = 2022,
) -> ProfileData:
    li = LinkedInProfile(
        id=linkedin_id,
        name=name,
        headline=f"{title} @ {company}",
        summary=bio,
        positions=[
            LinkedInPosition(
                title=title,
                company=company,
                start=date(start_year, 1, 1),
                end=None,
            )
        ],
        skills=skills,
        education=[Education(school="Test U")],
    )
    gh = (
        GitHubProfile(
            login=linkedin_id,
            name=name,
            bio=bio,
            primary_languages=languages or [],
            repos=[
                GitHubRepo(
                    name="demo",
                    description=bio or "",
                    language=(languages or ["Python"])[0],
                    stars=10,
                    topics=[],
                    is_pinned=True,
                )
            ],
            contribution_count=30,
        )
        if languages
        else None
    )
    return ProfileData(linkedin=li, github=gh)


@pytest.mark.asyncio
async def test_get_matches_returns_zero_with_single_attendee(fake_db, fake_vector_store) -> None:
    event_id = uuid4()
    profile = _make_profile(
        linkedin_id="solo",
        name="Solo Dev",
        title="Engineer",
        company="A",
        skills=["python"],
    )
    attendee = await upsert_attendee_with_embedding(
        event_id=event_id, profile=profile, store=fake_vector_store
    )

    matches = await get_matches(
        event_id=event_id,
        attendee_id=attendee.id,
        intent=Intent.FRIEND,
        store=fake_vector_store,
    )

    assert matches == []
    assert len(fake_db.connections) == 0


@pytest.mark.asyncio
async def test_get_matches_returns_correct_topk_with_many(fake_db, fake_vector_store) -> None:
    event_id = uuid4()

    requester = _make_profile(
        linkedin_id="req",
        name="Requester",
        title="Backend Engineer",
        company="HQ",
        skills=["python", "postgres", "rust"],
        bio="Distributed systems builder.",
        languages=["Rust", "Python"],
    )

    others = [
        _make_profile(
            linkedin_id=f"u{i}",
            name=f"User {i}",
            title=title,
            company=f"Co{i}",
            skills=skills,
            bio=bio,
            languages=languages,
        )
        for i, (title, skills, bio, languages) in enumerate(
            [
                ("Rust Engineer", ["rust", "go"], "Loves systems programming.", ["Rust"]),
                ("Frontend Developer", ["react", "typescript"], "UI craftsman.", ["TypeScript"]),
                ("ML Engineer", ["python", "ml"], "Trains big models.", ["Python"]),
                ("Designer", ["figma", "ux"], "Designs delightful UIs.", None),
                ("Backend Dev", ["python", "postgres"], "Builds APIs.", ["Python"]),
                ("Product Manager", ["product", "growth"], "Ships product.", None),
                ("CTO", ["python", "rust", "kubernetes"], "Scaling infra.", ["Rust", "Python"]),
            ]
        )
    ]

    requester_attendee = await upsert_attendee_with_embedding(
        event_id=event_id, profile=requester, store=fake_vector_store
    )
    for p in others:
        await upsert_attendee_with_embedding(event_id=event_id, profile=p, store=fake_vector_store)

    matches = await get_matches(
        event_id=event_id,
        attendee_id=requester_attendee.id,
        intent=Intent.CONTRIBUTOR,
        store=fake_vector_store,
        top_k=10,
        final_k=5,
    )

    assert 1 <= len(matches) <= 5
    # No duplicates and the requester is excluded.
    ids = [m.attendee_id for m in matches]
    assert len(set(ids)) == len(ids)
    assert requester_attendee.id not in ids

    # The contributor intent should surface at least one strong overlap
    # in tech stack (python / rust) ahead of the unrelated profiles
    # (designer, PM).
    relevant_skills = {"python", "rust", "postgres", "go", "kubernetes"}
    matched_relevant = sum(
        1
        for m in matches
        for s in m.explanation.signals
        if any(rel in (s.detail or "").lower() for rel in relevant_skills)
        or any(rel in m.display_name.lower() for rel in {"rust", "backend", "cto", "ml"})
    )
    names = [m.display_name for m in matches]
    assert matched_relevant >= 1, f"expected at least one tech-overlap match in: {names}"

    # Shown matches are persisted with status=shown.
    assert len(fake_db.connections) == len(matches)
    for record in fake_db.connections.values():
        assert record["status"] == "shown"


@pytest.mark.asyncio
async def test_get_matches_with_intent_mentor_prefers_more_experienced(
    fake_db, fake_vector_store
) -> None:
    event_id = uuid4()

    junior = _make_profile(
        linkedin_id="junior",
        name="Junior Dev",
        title="Junior Engineer",
        company="StartCo",
        skills=["python", "django"],
        bio="Just shipping my first apps.",
        languages=["Python"],
        start_year=2023,
    )
    peer = _make_profile(
        linkedin_id="peer",
        name="Peer Dev",
        title="Engineer",
        company="PeerCo",
        skills=["python", "django"],
        bio="Couple years in, learning fast.",
        languages=["Python"],
        start_year=2022,
    )
    senior = _make_profile(
        linkedin_id="senior",
        name="Senior Dev",
        title="Principal Engineer",
        company="BigCo",
        skills=["python", "django"],
        bio="20 years building Python systems.",
        languages=["Python"],
        start_year=2005,
    )

    junior_a = await upsert_attendee_with_embedding(
        event_id=event_id, profile=junior, store=fake_vector_store
    )
    await upsert_attendee_with_embedding(event_id=event_id, profile=peer, store=fake_vector_store)
    await upsert_attendee_with_embedding(event_id=event_id, profile=senior, store=fake_vector_store)

    matches = await get_matches(
        event_id=event_id,
        attendee_id=junior_a.id,
        intent=Intent.MENTOR,
        store=fake_vector_store,
    )

    assert matches, "mentor search should return at least one candidate"
    assert matches[0].display_name == "Senior Dev"
