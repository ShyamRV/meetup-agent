"""Real-DB smoke test for the meetup networking agent.

What this script does, end-to-end, against a *real* Postgres + pgvector:

1. Creates a fresh event row.
2. Builds 6 synthetic attendees (a frontend dev, a Rust hacker, a
   designer, a senior backend engineer, an ML researcher and a
   requester).
3. Embeds each profile and upserts both the attendee row *and* the
   pgvector embedding row — exactly the path the agent runs at
   check-in time.
4. Runs the matching pipeline (vector top-k + intent re-rank +
   explanation + persistence to ``meetup_connections``) for the
   requester under each ``Intent``.
5. Pretty-prints the ranked matches per intent.

Run with::

    DATABASE_URL=postgresql://meetup:meetup@localhost:55432/meetup \
        python scripts/smoke.py

If ``OPENAI_API_KEY`` is set we use the real OpenAI embedder, otherwise
we fall back to a deterministic toy embedder that mimics the API. The
DB code paths are identical either way — only the vector source differs.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from datetime import date
from pathlib import Path

# UTF-8 stdout/stderr so user-facing chat text (which legitimately contains
# em-dashes, smart quotes, etc.) survives Windows cp1252 consoles.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        with suppress(Exception):
            _reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(WORKSPACE))

# Ensure required env defaults exist before importing the agent — config.py
# reads these at import time and we want this script to be runnable from a
# bare shell with only DATABASE_URL set.
os.environ.setdefault("SESSION_SECRET", "smoke-test-secret-please-change-me")

from agents_shared import db as shared_db  # noqa: E402
from agents_shared.vector import VectorStore  # noqa: E402

from db import upsert_event  # noqa: E402
from handlers.embedding import upsert_attendee_with_embedding  # noqa: E402
from handlers.matching import get_matches  # noqa: E402
from models.profile import (  # noqa: E402
    Education,
    GitHubProfile,
    GitHubRepo,
    LinkedInPosition,
    LinkedInProfile,
    ProfileData,
)
from models.refinement import Intent  # noqa: E402

# ---------------------------------------------------------------------------
# Deterministic offline embedder (used when OPENAI_API_KEY is unset)
# ---------------------------------------------------------------------------


class OfflineVectorStore:
    """Word-bag embedder — same shape as ``VectorStore``, no network."""

    dim = 1536
    model = "smoke-deterministic-v1"

    async def embed(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("empty text")
        vec = [0.0] * self.dim
        for token in text.lower().split():
            idx = (hash(token) % self.dim + self.dim) % self.dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def upsert_embedding(
        self,
        *,
        attendee_id: str,
        event_id: str,
        embedding: Sequence[float],
        table: str = "meetup_embeddings",
    ) -> None:
        pool = await shared_db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {table} (attendee_id, event_id, embedding, model_version)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (attendee_id)
                DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    model_version = EXCLUDED.model_version,
                    created_at = NOW()
                """,
                attendee_id,
                event_id,
                list(embedding),
                self.model,
            )

    async def similarity_search(
        self,
        *,
        event_id: str,
        query_embedding: Sequence[float],
        exclude_attendee_id: str | None = None,
        top_k: int = 20,
        table: str = "meetup_embeddings",
    ):
        from agents_shared.vector import SimilarityHit

        pool = await shared_db.get_pool()
        params: list = [list(query_embedding), event_id]
        exclusion = ""
        if exclude_attendee_id is not None:
            params.append(exclude_attendee_id)
            exclusion = "AND attendee_id <> $3"
        params.append(top_k)
        limit_idx = len(params)
        sql = f"""
            SELECT attendee_id::text AS attendee_id,
                   embedding <=> $1 AS distance
            FROM {table}
            WHERE event_id = $2 {exclusion}
            ORDER BY embedding <=> $1
            LIMIT ${limit_idx}
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [
            SimilarityHit(
                attendee_id=row["attendee_id"],
                distance=float(row["distance"]),
                score=max(0.0, 1.0 - float(row["distance"])),
            )
            for row in rows
        ]


def _pick_store():
    """Real OpenAI store if a key is set, otherwise the offline embedder."""

    if os.getenv("OPENAI_API_KEY"):
        print("[smoke] using OpenAI embeddings")
        return VectorStore()
    print("[smoke] OPENAI_API_KEY not set — using deterministic offline embedder")
    return OfflineVectorStore()


# ---------------------------------------------------------------------------
# Synthetic profile builders
# ---------------------------------------------------------------------------


def _profile(
    *,
    linkedin_id: str,
    name: str,
    headline: str,
    title: str,
    company: str,
    summary: str,
    skills: list[str],
    languages: list[str] | None = None,
    repos: list[tuple[str, str, str]] | None = None,
    start_year: int = 2022,
) -> ProfileData:
    li = LinkedInProfile(
        id=linkedin_id,
        name=name,
        headline=headline,
        summary=summary,
        location="London, UK",
        profile_url=f"https://www.linkedin.com/in/{linkedin_id}",
        positions=[
            LinkedInPosition(
                title=title,
                company=company,
                start=date(start_year, 1, 1),
                end=None,
                summary=summary,
            )
        ],
        skills=skills,
        education=[Education(school="Imperial", degree="BSc", field_of_study="CS")],
    )
    gh: GitHubProfile | None = None
    if languages:
        gh = GitHubProfile(
            login=linkedin_id,
            name=name,
            bio=summary,
            profile_url=f"https://github.com/{linkedin_id}",
            primary_languages=languages,
            repos=[
                GitHubRepo(
                    name=repo_name,
                    description=repo_desc,
                    language=repo_lang,
                    stars=12,
                    topics=[],
                    is_pinned=True,
                )
                for (repo_name, repo_desc, repo_lang) in (repos or [])
            ],
            contribution_count=80,
        )
    return ProfileData(linkedin=li, github=gh)


PROFILES: list[ProfileData] = [
    _profile(
        linkedin_id="alex-backend",
        name="Alex Reid",
        headline="Founding engineer, distributed systems",
        title="Founding Engineer",
        company="Babbage Labs",
        summary="Building reliable infra for autonomous agents. Postgres, Rust, Python.",
        skills=["python", "rust", "postgres", "distributed systems", "kubernetes"],
        languages=["Rust", "Python"],
        repos=[
            ("agent-runtime", "Lightweight runtime for autonomous agents.", "Rust"),
            ("vector-cache", "In-memory vector cache.", "Rust"),
        ],
        start_year=2020,
    ),
    _profile(
        linkedin_id="rin-rust",
        name="Rin Kobayashi",
        headline="Rust + WASM hacker",
        title="Systems Engineer",
        company="RustWorks",
        summary="Loves low-level systems work and the borrow checker.",
        skills=["rust", "wasm", "go", "kubernetes"],
        languages=["Rust", "Go"],
        repos=[
            ("wasm-runtime", "Tiny WASM runtime.", "Rust"),
        ],
        start_year=2021,
    ),
    _profile(
        linkedin_id="maya-frontend",
        name="Maya Singh",
        headline="Senior frontend engineer",
        title="Senior Frontend Engineer",
        company="DesignCo",
        summary="React + TypeScript. Builds delightful UIs.",
        skills=["typescript", "react", "frontend", "tailwind"],
        languages=["TypeScript", "JavaScript"],
        repos=[
            ("ui-kit", "Reusable React components.", "TypeScript"),
        ],
        start_year=2019,
    ),
    _profile(
        linkedin_id="omar-design",
        name="Omar Aziz",
        headline="Product designer",
        title="Senior Product Designer",
        company="StudioCo",
        summary="UX & visual design for AI products.",
        skills=["figma", "ux", "ui", "design"],
        languages=None,
        repos=None,
        start_year=2018,
    ),
    _profile(
        linkedin_id="dr-li-ml",
        name="Dr Li Chen",
        headline="ML researcher",
        title="Principal Research Scientist",
        company="ResearchLab",
        summary="20 years in NLP and recommender systems. Mentor at heart.",
        skills=["python", "pytorch", "nlp", "machine learning"],
        languages=["Python"],
        repos=[
            ("seq2seq-lab", "Research code for seq2seq experiments.", "Python"),
        ],
        start_year=2005,
    ),
]


REQUESTER: ProfileData = _profile(
    linkedin_id="me-the-requester",
    name="Sam Patel",
    headline="Backend engineer, distributed systems",
    title="Backend Engineer",
    company="HQ",
    summary=(
        "Building Postgres-heavy backend systems. Interested in Rust, agents and infra for AI."
    ),
    skills=["python", "postgres", "rust", "distributed systems"],
    languages=["Python", "Rust"],
    repos=[
        ("pg-tools", "Postgres operational tools.", "Python"),
    ],
    start_year=2022,
)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    if not os.getenv("DATABASE_URL"):
        print("ERROR: DATABASE_URL must be set", file=sys.stderr)
        sys.exit(1)

    store = _pick_store()

    print("[smoke] creating event")
    event_id = await upsert_event(
        event_name="Smoke Test Meetup",
        qr_seed="smoke-test-1",
    )
    print(f"[smoke] event_id = {event_id}")

    print(f"[smoke] upserting {len(PROFILES) + 1} attendees with embeddings")
    requester_record = await upsert_attendee_with_embedding(
        event_id=event_id, profile=REQUESTER, store=store
    )
    for p in PROFILES:
        await upsert_attendee_with_embedding(event_id=event_id, profile=p, store=store)

    print(f"[smoke] requester attendee_id = {requester_record.id}")

    for intent in (Intent.COFOUNDER, Intent.CONTRIBUTOR, Intent.MENTOR, Intent.FRIEND):
        print()
        print(f"=== intent = {intent.value} ===")
        results = await get_matches(
            event_id=event_id,
            attendee_id=requester_record.id,
            intent=intent,
            store=store,
        )
        if not results:
            print("  (no matches)")
            continue
        for i, m in enumerate(results, start=1):
            role = m.current_role or m.headline or ""
            if m.company:
                role = f"{role} @ {m.company}" if role else m.company
            print(f"  {i}. {m.display_name:18s} {m.rerank_score:6.3f}  {role}")
            print(f"     {m.explanation.sentence}")
            for s in m.explanation.signals:
                tag = f"[{s.kind}]"
                extra = f" ({s.detail})" if s.detail else ""
                print(f"     {tag:14s} {s.label}{extra}")

    # Idempotency check: re-running the upsert path should be a no-op
    # (same attendee row, same embedding row).
    print()
    print("[smoke] idempotency check — re-running upsert for requester...")
    requester_second = await upsert_attendee_with_embedding(
        event_id=event_id, profile=REQUESTER, store=store
    )
    assert requester_second.id == requester_record.id, "non-idempotent upsert!"
    print(f"[smoke] OK — attendee_id stable at {requester_second.id}")

    pool = await shared_db.get_pool()
    async with pool.acquire() as conn:
        n_attendees = await conn.fetchval(
            "SELECT count(*) FROM meetup_attendees WHERE event_id = $1", event_id
        )
        n_embeddings = await conn.fetchval(
            "SELECT count(*) FROM meetup_embeddings WHERE event_id = $1", event_id
        )
        n_connections = await conn.fetchval(
            "SELECT count(*) FROM meetup_connections WHERE event_id = $1",
            event_id,
        )
    print(
        f"[smoke] db rows — attendees={n_attendees} "
        f"embeddings={n_embeddings} connections={n_connections}"
    )
    assert n_attendees == n_embeddings == len(PROFILES) + 1, (
        "row count mismatch — idempotency broken"
    )

    await shared_db.close_pool()
    print()
    print("[smoke] PASS")


if __name__ == "__main__":
    asyncio.run(main())
