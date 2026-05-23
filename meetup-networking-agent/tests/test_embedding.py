"""Tests for the embedding handler — profile_to_text + embed + upsert."""

from __future__ import annotations

from uuid import uuid4

import pytest

from handlers.embedding import (
    embed_profile,
    profile_to_text,
    upsert_attendee_with_embedding,
)


def test_profile_to_text_produces_non_empty_rich_text(sample_profile) -> None:
    text = profile_to_text(sample_profile)

    assert text, "profile_to_text must produce non-empty text"
    # The blob should reference identity, role, key skills, and GitHub work.
    assert "Ada Lovelace" in text
    assert "Founding Engineer" in text
    assert "Babbage Labs" in text
    assert "Rust" in text
    assert "agent-runtime" in text
    # It should be more than one sentence — embedding wants context.
    assert text.count("\n") >= 3
    assert len(text) > 200


def test_profile_to_text_handles_minimal_profile() -> None:
    from models.profile import LinkedInProfile, ProfileData

    profile = ProfileData(
        linkedin=LinkedInProfile(id="x", name="Grace Hopper"),
        github=None,
    )
    text = profile_to_text(profile)
    assert "Grace Hopper" in text


@pytest.mark.asyncio
async def test_embed_profile_returns_1536_floats(fake_vector_store, sample_profile) -> None:
    text = profile_to_text(sample_profile)
    vector = await embed_profile(text, store=fake_vector_store)

    assert isinstance(vector, list)
    assert len(vector) == 1536
    assert all(isinstance(v, float) for v in vector)
    # The fake store unit-normalises; the magnitude should be ~1.
    magnitude = sum(v * v for v in vector) ** 0.5
    assert 0.99 < magnitude < 1.01


@pytest.mark.asyncio
async def test_upsert_is_idempotent_on_duplicate_call(
    fake_db, fake_vector_store, sample_profile
) -> None:
    event_id = uuid4()

    first = await upsert_attendee_with_embedding(
        event_id=event_id,
        profile=sample_profile,
        store=fake_vector_store,
    )
    second = await upsert_attendee_with_embedding(
        event_id=event_id,
        profile=sample_profile,
        store=fake_vector_store,
    )

    assert first.id == second.id, "upsert must return the same attendee on retry"
    assert len(fake_db.attendees) == 1
    assert len(fake_db.embeddings) == 1
    embedding_row = next(iter(fake_db.embeddings.values()))
    assert len(embedding_row["embedding"]) == 1536


@pytest.mark.asyncio
async def test_upsert_persists_skills_and_profile_json(
    fake_db, fake_vector_store, sample_profile
) -> None:
    event_id = uuid4()

    attendee = await upsert_attendee_with_embedding(
        event_id=event_id,
        profile=sample_profile,
        store=fake_vector_store,
    )

    row = fake_db.attendees[attendee.id]
    assert "Rust" in row["skills"]
    assert row["linkedin_id"] == sample_profile.linkedin_id
    assert row["github_username"] == sample_profile.github_username
