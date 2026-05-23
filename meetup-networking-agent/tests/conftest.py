"""Test fixtures: an in-memory DB pool stand-in and a deterministic vector store.

These fakes are *not* a re-implementation of Postgres — they only support
the queries this agent actually runs. The goal is to validate the agent's
SQL semantics (idempotent upserts, pgvector similarity, top-k limits)
without booting a container in unit tests.
"""

from __future__ import annotations

import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(WORKSPACE))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LINKEDIN_CLIENT_ID", "test-li")
os.environ.setdefault("LINKEDIN_CLIENT_SECRET", "test-li-secret")
os.environ.setdefault("LINKEDIN_REDIRECT_URI", "https://example.com/oauth/linkedin/callback")
os.environ.setdefault("GITHUB_CLIENT_ID", "test-gh")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-gh-secret")
os.environ.setdefault("GITHUB_REDIRECT_URI", "https://example.com/oauth/github/callback")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-please-change")

from agents_shared import db as shared_db  # noqa: E402  (env must be set first)

# ---------------------------------------------------------------------------
# Fake DB — implements only the queries the agent actually issues.
# ---------------------------------------------------------------------------


class _Row(dict):
    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


class FakeConnection:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    async def __aenter__(self) -> FakeConnection:
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def fetchval(self, sql: str, *_args) -> Any:
        if "SELECT 1" in sql:
            return 1
        return None

    async def fetchrow(self, sql: str, *args) -> _Row | None:
        return self._db.fetchrow(sql, args)

    async def fetch(self, sql: str, *args) -> list[_Row]:
        return self._db.fetch(sql, args)

    async def execute(self, sql: str, *args) -> None:
        self._db.execute(sql, args)


class _AcquireCM:
    def __init__(self, db: FakeDB) -> None:
        self._db = db
        self._conn: FakeConnection | None = None

    async def __aenter__(self) -> FakeConnection:
        self._conn = FakeConnection(self._db)
        return self._conn

    async def __aexit__(self, *_exc) -> None:
        return None


class FakeDB:
    """Tiny in-memory store with the surface our SQL needs."""

    def __init__(self) -> None:
        self.events: dict[UUID, dict[str, Any]] = {}
        self.attendees: dict[UUID, dict[str, Any]] = {}
        self.embeddings: dict[UUID, dict[str, Any]] = {}
        self.connections: dict[tuple[UUID, UUID, UUID], dict[str, Any]] = {}

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self)

    def get_min_size(self) -> int:
        return 1

    def get_max_size(self) -> int:
        return 10

    async def close(self) -> None:
        self.events.clear()
        self.attendees.clear()
        self.embeddings.clear()
        self.connections.clear()

    # ------------------------- write paths -------------------------

    def execute(self, sql: str, args: tuple) -> None:
        if "INSERT INTO meetup_embeddings" in sql:
            attendee_id = UUID(str(args[0]))
            event_id = UUID(str(args[1]))
            self.embeddings[attendee_id] = {
                "attendee_id": attendee_id,
                "event_id": event_id,
                "embedding": list(args[2]),
                "model_version": args[3],
            }

    def fetchrow(self, sql: str, args: tuple) -> _Row | None:
        if "INSERT INTO meetup_events" in sql:
            event_name, qr_seed, organizer_email, location, event_date, expires_at = args
            existing = next(
                (e for e in self.events.values() if e["qr_seed"] == qr_seed),
                None,
            )
            if existing is None:
                eid = uuid4()
                self.events[eid] = {
                    "id": eid,
                    "event_name": event_name,
                    "qr_seed": qr_seed,
                    "organizer_email": organizer_email,
                    "location": location,
                    "event_date": event_date,
                    "expires_at": expires_at,
                    "created_at": datetime.now(tz=UTC),
                }
                return _Row(id=eid)
            existing.update(
                event_name=event_name,
                organizer_email=organizer_email,
                location=location,
                event_date=event_date,
                expires_at=expires_at,
            )
            return _Row(id=existing["id"])

        if "FROM meetup_events" in sql and "WHERE id" in sql:
            event_id = UUID(str(args[0]))
            row = self.events.get(event_id)
            return _Row(**row) if row else None

        if "INSERT INTO meetup_attendees" in sql:
            (
                event_id,
                linkedin_id,
                github_username,
                display_name,
                headline,
                current_role,
                skills,
                profile_json,
                linkedin_url,
            ) = args
            event_id = UUID(str(event_id))
            existing = next(
                (
                    a
                    for a in self.attendees.values()
                    if a["event_id"] == event_id and a["linkedin_id"] == linkedin_id
                ),
                None,
            )
            now = datetime.now(tz=UTC)
            if existing is None:
                aid = uuid4()
                self.attendees[aid] = {
                    "id": aid,
                    "event_id": event_id,
                    "linkedin_id": linkedin_id,
                    "github_username": github_username,
                    "display_name": display_name,
                    "headline": headline,
                    "current_role": current_role,
                    "skills": list(skills or []),
                    "profile_json": profile_json,
                    "linkedin_url": linkedin_url,
                    "checked_in_at": now,
                    "updated_at": now,
                }
                return _Row(**self.attendees[aid])
            existing.update(
                github_username=github_username,
                display_name=display_name,
                headline=headline,
                current_role=current_role,
                skills=list(skills or []),
                profile_json=profile_json,
                linkedin_url=linkedin_url,
                updated_at=now,
            )
            return _Row(**existing)

        if "FROM meetup_attendees" in sql and "WHERE event_id = $1 AND linkedin_id" in sql:
            event_id, linkedin_id = args
            event_id = UUID(str(event_id))
            row = next(
                (
                    a
                    for a in self.attendees.values()
                    if a["event_id"] == event_id and a["linkedin_id"] == linkedin_id
                ),
                None,
            )
            return _Row(**row) if row else None

        if "FROM meetup_attendees" in sql and "WHERE id" in sql and "= $1" in sql:
            attendee_id = UUID(str(args[0]))
            row = self.attendees.get(attendee_id)
            return _Row(**row) if row else None

        if "FROM meetup_embeddings" in sql and "WHERE attendee_id = $1" in sql:
            attendee_id = UUID(str(args[0]))
            row = self.embeddings.get(attendee_id)
            return _Row(**row) if row else None

        if "INSERT INTO meetup_connections" in sql:
            (
                event_id,
                from_id,
                to_id,
                status,
                score,
                reason,
            ) = args
            event_id = UUID(str(event_id))
            from_id = UUID(str(from_id))
            to_id = UUID(str(to_id))
            key = (event_id, from_id, to_id)
            cid = self.connections.get(key, {}).get("id", uuid4())
            self.connections[key] = {
                "id": cid,
                "event_id": event_id,
                "from_attendee_id": from_id,
                "to_attendee_id": to_id,
                "status": status,
                "match_score": score,
                "match_reason": reason,
            }
            return _Row(id=cid)

        return None

    def fetch(self, sql: str, args: tuple) -> list[_Row]:
        if "FROM meetup_embeddings" in sql and "ORDER BY embedding <=>" in sql:
            query_vec = list(args[0])
            event_id = UUID(str(args[1]))
            exclude_id: UUID | None = None
            limit = args[-1]
            if "AND attendee_id <> $3" in sql:
                exclude_id = UUID(str(args[2]))

            rows = [
                {
                    "attendee_id": str(e["attendee_id"]),
                    "distance": _cosine_distance(query_vec, e["embedding"]),
                }
                for e in self.embeddings.values()
                if e["event_id"] == event_id
                and (exclude_id is None or e["attendee_id"] != exclude_id)
            ]
            rows.sort(key=lambda r: r["distance"])
            return [_Row(**r) for r in rows[:limit]]

        if "FROM meetup_attendees" in sql and "WHERE id = ANY" in sql:
            ids = {UUID(str(x)) for x in args[0]}
            return [_Row(**a) for aid, a in self.attendees.items() if aid in ids]

        if "FROM meetup_attendees" in sql and "WHERE event_id = $1" in sql:
            event_id = UUID(str(args[0]))
            exclude_id: UUID | None = None
            if "AND id <> $2" in sql:
                exclude_id = UUID(str(args[1]))
            rows = [
                a
                for a in self.attendees.values()
                if a["event_id"] == event_id and (exclude_id is None or a["id"] != exclude_id)
            ]
            return [_Row(**r) for r in rows]

        return []


# ---------------------------------------------------------------------------
# Fake VectorStore
# ---------------------------------------------------------------------------


class FakeVectorStore:
    """Deterministic embedding store — vector depends only on text content."""

    def __init__(self, dim: int = 1536, model: str = "text-embedding-3-small") -> None:
        self.dim = dim
        self.model = model
        self._embeddings: dict[UUID, list[float]] = {}
        self._db: FakeDB | None = None
        self.embed_calls: int = 0

    def bind(self, db: FakeDB) -> None:
        self._db = db

    async def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        if not text.strip():
            raise ValueError("empty text")
        # Map distinct tokens onto a sparse-ish vector. Same text in →
        # same vector out, which is what tests need.
        tokens = [t for t in text.lower().replace("\n", " ").split() if t]
        vec = [0.0] * self.dim
        for tok in tokens:
            idx = (hash(tok) % self.dim + self.dim) % self.dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def upsert_embedding(
        self,
        *,
        attendee_id: str,
        event_id: str,
        embedding,
        table: str = "meetup_embeddings",
    ) -> None:
        aid = UUID(attendee_id)
        eid = UUID(event_id)
        if self._db is not None:
            self._db.embeddings[aid] = {
                "attendee_id": aid,
                "event_id": eid,
                "embedding": list(embedding),
                "model_version": self.model,
            }
        else:
            self._embeddings[aid] = list(embedding)

    async def similarity_search(
        self,
        *,
        event_id: str,
        query_embedding,
        exclude_attendee_id: str | None = None,
        top_k: int = 20,
        table: str = "meetup_embeddings",
    ):
        from agents_shared.vector import SimilarityHit

        target_event = UUID(event_id)
        exclude_id = UUID(exclude_attendee_id) if exclude_attendee_id else None
        store = (
            self._db.embeddings
            if self._db is not None
            else {
                aid: {"attendee_id": aid, "event_id": target_event, "embedding": vec}
                for aid, vec in self._embeddings.items()
            }
        )
        hits = []
        query_vec = list(query_embedding)
        for aid, payload in store.items():
            if payload["event_id"] != target_event:
                continue
            if exclude_id is not None and aid == exclude_id:
                continue
            dist = _cosine_distance(query_vec, payload["embedding"])
            hits.append(
                SimilarityHit(
                    attendee_id=str(aid),
                    distance=dist,
                    score=max(0.0, 1.0 - dist),
                )
            )
        hits.sort(key=lambda h: h.distance)
        return hits[:top_k]


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    db = FakeDB()

    async def _get_pool(*_args, **_kwargs):
        return db

    async def _healthcheck():
        return True

    async def _close_pool():
        await db.close()

    monkeypatch.setattr(shared_db, "get_pool", _get_pool)
    monkeypatch.setattr(shared_db, "healthcheck", _healthcheck)
    monkeypatch.setattr(shared_db, "close_pool", _close_pool)
    monkeypatch.setattr(shared_db, "_pool", db, raising=False)
    return db


@pytest.fixture
def fake_vector_store(fake_db: FakeDB) -> FakeVectorStore:
    store = FakeVectorStore()
    store.bind(fake_db)
    return store


@pytest.fixture
def sample_profile():
    from datetime import date

    from models.profile import (
        Education,
        GitHubProfile,
        GitHubRepo,
        LinkedInPosition,
        LinkedInProfile,
        ProfileData,
    )

    li = LinkedInProfile(
        id="li-ada",
        name="Ada Lovelace",
        headline="Founding engineer, distributed systems",
        summary="Building reliable infra for autonomous agents.",
        location="London, UK",
        profile_url="https://www.linkedin.com/in/ada-lovelace",
        positions=[
            LinkedInPosition(
                title="Founding Engineer",
                company="Babbage Labs",
                start=date(2022, 1, 1),
                end=None,
                summary="Lead the platform team building agent infra.",
            ),
            LinkedInPosition(
                title="Senior SWE",
                company="ACME",
                start=date(2018, 5, 1),
                end=date(2021, 12, 31),
            ),
        ],
        skills=["Python", "Rust", "Distributed Systems", "Postgres"],
        education=[Education(school="Cambridge", degree="BA", field_of_study="Mathematics")],
    )
    gh = GitHubProfile(
        login="adalovelace",
        name="Ada Lovelace",
        bio="Compilers + agents.",
        profile_url="https://github.com/adalovelace",
        primary_languages=["Rust", "Python"],
        repos=[
            GitHubRepo(
                name="agent-runtime",
                description="Lightweight runtime for autonomous agents.",
                language="Rust",
                stars=42,
                topics=["agents", "ai"],
                is_pinned=True,
            ),
        ],
        contribution_count=120,
    )
    return ProfileData(linkedin=li, github=gh)
