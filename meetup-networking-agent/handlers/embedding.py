"""Profile → text → embedding → DB upsert.

The text we feed the embedding model is the single most important
heuristic in this whole agent: if the blob captures *what someone wants*
and *what they bring*, cosine similarity does the rest of the work.

Weighting strategy:

- Headline + current role appear twice — these dominate identity.
- Recent (current) positions are emitted before historical ones.
- Skills are repeated once for technical skills (a stand-alone list and
  a sentence fragment) to nudge them up the term weighting.
- GitHub languages + pinned repo descriptions ride alongside skills so
  builders show up even if their LinkedIn skills section is empty.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from agents_shared.logging import get_logger
from agents_shared.sentry import capture_exception
from agents_shared.vector import VectorStore

from config import get_settings
from db import upsert_attendee
from models.attendee import AttendeeRecord
from models.profile import ProfileData

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# profile_to_text
# ---------------------------------------------------------------------------


def profile_to_text(profile: ProfileData) -> str:
    """Render a rich, embedding-friendly description of one attendee."""

    li = profile.linkedin
    gh = profile.github

    parts: list[str] = []

    # 1. Identity line (weighted by repetition + position).
    name = li.name.strip()
    headline = (li.headline or "").strip()
    if headline:
        parts.append(f"{name} — {headline}.")
        parts.append(f"Headline: {headline}.")
    else:
        parts.append(f"{name}.")

    # 2. Current role gets two appearances.
    current = li.current_position
    if current:
        role_line = f"Currently {current.title} at {current.company}"
        if current.location:
            role_line += f" in {current.location}"
        parts.append(role_line + ".")
        parts.append(f"Role focus: {current.title}.")

    # 3. Past roles, most recent first.
    past_positions = [p for p in li.positions if not p.is_current]
    past_positions.sort(key=lambda p: p.end or p.start or _Zero(), reverse=True)
    for position in past_positions[:5]:
        line = f"Previously {position.title} at {position.company}"
        if position.summary:
            line += f": {position.summary.strip()[:200]}"
        parts.append(line + ".")

    # 4. Education.
    for ed in li.education[:3]:
        line = f"Studied {ed.field_of_study or ed.degree or 'their field'} at {ed.school}"
        parts.append(line.strip() + ".")

    # 5. Summary / bio.
    if li.summary:
        parts.append(li.summary.strip())
    if gh and gh.bio:
        parts.append(f"GitHub bio: {gh.bio.strip()}")

    # 6. Skills — emitted twice (a flat list and a sentence) so technical
    #    terms get repeated naturally.
    skills = profile.skills
    if skills:
        parts.append("Skills: " + ", ".join(skills[:25]) + ".")
        parts.append("They work with " + ", ".join(skills[:10]) + ".")

    # 7. GitHub: languages + pinned repo descriptions.
    if gh:
        if gh.primary_languages:
            parts.append("Primary GitHub languages: " + ", ".join(gh.primary_languages[:8]) + ".")
        for repo in gh.pinned_repos[:6]:
            desc = repo.description or ""
            topics = ", ".join(repo.topics[:6])
            line = f"Open-source project '{repo.name}'"
            if repo.language:
                line += f" ({repo.language})"
            if desc:
                line += f": {desc.strip()}"
            if topics:
                line += f" [topics: {topics}]"
            if repo.stars:
                line += f" with {repo.stars} stars"
            parts.append(line + ".")
        if gh.contribution_count:
            parts.append(f"Recent activity: {gh.contribution_count} public events.")

    text = "\n".join(p for p in parts if p).strip()
    if not text:
        raise ValueError("profile produced empty embedding text")
    return text


class _Zero:
    """Sentinel that sorts before any date — used to pin items lacking dates last."""

    def __lt__(self, other: object) -> bool:
        return True

    def __gt__(self, other: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# embed_profile
# ---------------------------------------------------------------------------


async def embed_profile(text: str, *, store: VectorStore | None = None) -> list[float]:
    """Generate a 1536-dim embedding for ``text``."""

    settings = get_settings()
    vec_store = store or VectorStore(
        model=settings.embedding_model, api_key=settings.openai_api_key
    )
    try:
        return await vec_store.embed(text)
    except Exception as exc:
        capture_exception(exc, op="embedding.embed_profile")
        raise


# ---------------------------------------------------------------------------
# upsert_attendee_with_embedding
# ---------------------------------------------------------------------------


async def upsert_attendee_with_embedding(
    *,
    event_id: UUID,
    profile: ProfileData,
    embedding: Iterable[float] | None = None,
    store: VectorStore | None = None,
) -> AttendeeRecord:
    """End-to-end idempotent write: attendee row + embedding row.

    If ``embedding`` is ``None``, it is generated on the fly from
    :func:`profile_to_text` — this is the canonical path used in
    production. Re-running with the same ``ProfileData`` for the same
    ``event_id`` results in two ``UPDATE``s and zero net writes.
    """

    settings = get_settings()

    if embedding is None:
        text = profile_to_text(profile)
        embedding = await embed_profile(text, store=store)

    attendee = await upsert_attendee(
        event_id=event_id,
        linkedin_id=profile.linkedin_id,
        display_name=profile.display_name,
        profile_json=profile.to_storage_json(),
        github_username=profile.github_username,
        headline=profile.headline,
        current_role=profile.current_role,
        skills=profile.skills,
        linkedin_url=profile.linkedin_url,
    )

    vec_store = store or VectorStore(
        model=settings.embedding_model, api_key=settings.openai_api_key
    )
    await vec_store.upsert_embedding(
        attendee_id=str(attendee.id),
        event_id=str(event_id),
        embedding=list(embedding),
    )

    _log.info(
        "embedding.upserted",
        event_id=str(event_id),
        attendee_id=str(attendee.id),
        linkedin_id=profile.linkedin_id,
    )
    return attendee
