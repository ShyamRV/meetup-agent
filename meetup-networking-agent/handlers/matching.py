"""Matching: vector retrieval → intent re-ranking → human explanation.

The retrieval/ranking pipeline:

1. Pull the requester's embedding from ``meetup_embeddings``.
2. ``VectorStore.similarity_search`` returns the top ``top_k`` rows
   restricted to the same event_id, excluding the requester.
3. Each candidate is scored by a deterministic intent re-ranker built
   from the profile JSON — boosts complementary or shared signals based
   on ``Intent``.
4. The top 5 candidates get a one-sentence LLM-generated explanation.
5. Shown matches are persisted in ``meetup_connections`` with
   ``status='shown'`` via an idempotent ``ON CONFLICT`` insert.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from agents_shared import security_fetch
from agents_shared.db import get_pool
from agents_shared.logging import get_logger
from agents_shared.sentry import capture_exception
from agents_shared.vector import SimilarityHit, VectorStore

from config import get_settings
from db import (
    fetch_attendee_by_id,
    insert_connection,
    list_event_attendees,
)
from models.attendee import AttendeeRecord
from models.match import MatchExplanation, MatchResult, MatchSignal
from models.profile import ProfileData
from models.refinement import Intent, RefinementQuery

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# get_matches
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Candidate:
    attendee: AttendeeRecord
    profile: ProfileData
    base_score: float  # 1 - cosine_distance from vector store
    rerank_score: float  # base_score + intent-specific boosts
    signals: list[MatchSignal]


async def get_matches(
    *,
    event_id: UUID,
    attendee_id: UUID,
    intent: Intent | str,
    top_k: int = 20,
    final_k: int = 5,
    refinement: RefinementQuery | None = None,
    store: VectorStore | None = None,
) -> list[MatchResult]:
    """Run the full matching pipeline and return the top ``final_k`` matches."""

    if isinstance(intent, str):
        intent = Intent.from_text(intent)

    requester = await fetch_attendee_by_id(attendee_id)
    if requester is None:
        raise ValueError(f"requesting attendee {attendee_id} not found")

    settings = get_settings()
    vec_store = store or VectorStore(
        model=settings.embedding_model, api_key=settings.openai_api_key
    )

    query_vector = await _fetch_query_vector(attendee_id)
    if query_vector is None:
        _log.warning("matching.no_query_vector", attendee_id=str(attendee_id))
        return []

    hits = await vec_store.similarity_search(
        event_id=str(event_id),
        query_embedding=query_vector,
        exclude_attendee_id=str(attendee_id),
        top_k=top_k,
    )
    if not hits:
        return []

    candidates = await _build_candidates(requester, hits)
    if not candidates:
        return []

    requester_profile = requester.to_profile_data()
    candidates = _rerank(requester_profile, candidates, intent)

    if refinement is not None and refinement.has_filters():
        from handlers.conversation import apply_refinement

        candidates = apply_refinement(candidates, refinement)

    top = candidates[:final_k]
    explanations = await asyncio.gather(
        *[explain_match(requester_profile, c.profile, c.rerank_score) for c in top]
    )

    results: list[MatchResult] = []
    for cand, explanation in zip(top, explanations, strict=True):
        results.append(_to_match_result(cand, explanation))

    await store_shown_matches(
        from_id=attendee_id,
        to_results=results,
        event_id=event_id,
    )
    _log.info(
        "matching.completed",
        event_id=str(event_id),
        from_id=str(attendee_id),
        intent=intent.value,
        count=len(results),
    )
    return results


async def _fetch_query_vector(attendee_id: UUID) -> list[float] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT embedding FROM meetup_embeddings WHERE attendee_id = $1",
            attendee_id,
        )
    if row is None:
        return None
    embedding = row["embedding"]
    if hasattr(embedding, "tolist"):
        return list(embedding.tolist())
    return list(embedding)


async def _build_candidates(
    requester: AttendeeRecord, hits: list[SimilarityHit]
) -> list[_Candidate]:
    by_id = {str(hit.attendee_id): hit for hit in hits}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await _fetch_attendees_by_ids(conn, list(by_id.keys()))

    candidates: list[_Candidate] = []
    for row in rows:
        attendee = _row_to_attendee(row)
        try:
            profile = attendee.to_profile_data()
        except Exception as exc:
            capture_exception(exc, op="matching.profile_validate", attendee_id=str(attendee.id))
            continue
        hit = by_id[str(attendee.id)]
        candidates.append(
            _Candidate(
                attendee=attendee,
                profile=profile,
                base_score=hit.score,
                rerank_score=hit.score,
                signals=[],
            )
        )

    candidates.sort(key=lambda c: by_id[str(c.attendee.id)].score, reverse=True)
    return candidates


async def _fetch_attendees_by_ids(conn: asyncpg.Connection, ids: list[str]) -> list[asyncpg.Record]:
    if not ids:
        return []
    return await conn.fetch(
        "SELECT * FROM meetup_attendees WHERE id = ANY($1::uuid[])",
        ids,
    )


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
# Intent re-ranking
# ---------------------------------------------------------------------------


_TECHNICAL_KEYWORDS = {
    "engineer",
    "developer",
    "founder",
    "cto",
    "architect",
    "scientist",
    "researcher",
    "designer",
    "data",
    "ml",
    "ai",
}
_BUSINESS_KEYWORDS = {
    "founder",
    "ceo",
    "coo",
    "product",
    "marketing",
    "sales",
    "growth",
    "ops",
    "business",
    "partnerships",
}


def _classify_axis(role_text: str | None) -> str:
    if not role_text:
        return "unknown"
    lowered = role_text.lower()
    tech_hit = any(k in lowered for k in _TECHNICAL_KEYWORDS)
    biz_hit = any(k in lowered for k in _BUSINESS_KEYWORDS)
    if tech_hit and not biz_hit:
        return "technical"
    if biz_hit and not tech_hit:
        return "business"
    if tech_hit and biz_hit:
        return "hybrid"
    return "other"


def _estimate_years_experience(profile: ProfileData) -> int:
    """Crude experience estimate from earliest position start year."""

    starts = [p.start for p in profile.linkedin.positions if p.start]
    if not starts:
        return 0
    from datetime import date as _date

    earliest = min(starts)
    today = _date.today()
    return max(0, today.year - earliest.year)


def rerank_candidates(
    requester: ProfileData,
    candidates: Iterable[_Candidate],
    intent: Intent,
) -> list[_Candidate]:
    """Apply intent-specific boosts and return candidates sorted desc."""

    requester_skills = {s.lower() for s in requester.skills}
    requester_langs = {
        lang.lower() for lang in (requester.github.primary_languages if requester.github else [])
    }
    requester_axis = _classify_axis(requester.current_role)
    requester_years = _estimate_years_experience(requester)

    enriched: list[_Candidate] = []
    for cand in candidates:
        cand_skills = {s.lower() for s in cand.profile.skills}
        cand_langs = {
            lang.lower()
            for lang in (cand.profile.github.primary_languages if cand.profile.github else [])
        }
        cand_axis = _classify_axis(cand.profile.current_role)
        cand_years = _estimate_years_experience(cand.profile)

        shared_skills = requester_skills & cand_skills
        shared_langs = requester_langs & cand_langs

        boost = 0.0
        signals: list[MatchSignal] = []

        if intent is Intent.COFOUNDER:
            if requester_axis != cand_axis and "unknown" not in {
                requester_axis,
                cand_axis,
            }:
                boost += 0.20
                signals.append(
                    MatchSignal(
                        label=(f"complementary background ({requester_axis} + {cand_axis})"),
                        detail=None,
                        kind="complementary",
                    )
                )
            if shared_skills:
                boost += 0.05 * min(len(shared_skills), 3)
                signals.append(
                    MatchSignal(
                        label="overlapping core skills",
                        detail=", ".join(sorted(shared_skills)[:3]),
                        kind="shared",
                    )
                )
        elif intent is Intent.CONTRIBUTOR:
            if shared_langs:
                boost += 0.10 * min(len(shared_langs), 3)
                signals.append(
                    MatchSignal(
                        label="shared GitHub languages",
                        detail=", ".join(sorted(shared_langs)[:3]),
                        kind="shared",
                    )
                )
            if shared_skills:
                boost += 0.05 * min(len(shared_skills), 3)
                signals.append(
                    MatchSignal(
                        label="overlapping tech stack",
                        detail=", ".join(sorted(shared_skills)[:3]),
                        kind="shared",
                    )
                )
            if cand.profile.github and cand.profile.github.contribution_count > 5:
                boost += 0.05
                signals.append(
                    MatchSignal(
                        label="active open-source contributor",
                        detail=f"{cand.profile.github.contribution_count} recent events",
                        kind="domain",
                    )
                )
        elif intent is Intent.MENTOR:
            delta = cand_years - requester_years
            if delta >= 5:
                boost += min(0.25, 0.04 * delta)
                signals.append(
                    MatchSignal(
                        label=f"~{delta}+ years of additional experience",
                        detail=None,
                        kind="experience",
                    )
                )
            if shared_skills:
                boost += 0.05
                signals.append(
                    MatchSignal(
                        label="works in your domain",
                        detail=", ".join(sorted(shared_skills)[:3]),
                        kind="domain",
                    )
                )
        else:  # FRIEND / general
            if shared_skills:
                boost += 0.03 * min(len(shared_skills), 5)
                signals.append(
                    MatchSignal(
                        label="shared interests",
                        detail=", ".join(sorted(shared_skills)[:3]),
                        kind="shared",
                    )
                )

        cand.rerank_score = cand.base_score + boost
        cand.signals = signals[:3]
        enriched.append(cand)

    enriched.sort(key=lambda c: c.rerank_score, reverse=True)
    return enriched


def _rerank(
    requester: ProfileData,
    candidates: list[_Candidate],
    intent: Intent,
) -> list[_Candidate]:
    return rerank_candidates(requester, candidates, intent)


# ---------------------------------------------------------------------------
# explain_match
# ---------------------------------------------------------------------------


async def explain_match(
    user_profile: ProfileData,
    candidate_profile: ProfileData,
    score: float,
) -> MatchExplanation:
    """Produce a one-sentence explanation via the LLM (falls back gracefully)."""

    signals_text = _format_signals_for_prompt(user_profile, candidate_profile)
    prompt = (
        "You are a meetup networking assistant. Write ONE warm, specific "
        "sentence (≤ 240 chars, no emojis) explaining why these two attendees "
        "should meet. Lead with the strongest shared or complementary signal. "
        "Don't be salesy.\n\n"
        f"Requester: {user_profile.display_name} — {user_profile.headline or ''}. "
        f"Role: {user_profile.current_role or 'n/a'}.\n"
        f"Candidate: {candidate_profile.display_name} — {candidate_profile.headline or ''}. "
        f"Role: {candidate_profile.current_role or 'n/a'}.\n"
        f"Signals: {signals_text}\n"
        f"Cosine score: {score:.3f}.\n"
    )

    sentence = await _llm_complete(prompt)
    if not sentence:
        sentence = _fallback_sentence(user_profile, candidate_profile)
    sentence = sentence.strip().strip('"').strip()[:280] or _fallback_sentence(
        user_profile, candidate_profile
    )
    return MatchExplanation(sentence=sentence, signals=[])


def _format_signals_for_prompt(a: ProfileData, b: ProfileData) -> str:
    shared = set(s.lower() for s in a.skills) & set(s.lower() for s in b.skills)
    return ", ".join(sorted(shared)[:5]) or "n/a"


def _fallback_sentence(a: ProfileData, b: ProfileData) -> str:
    shared = sorted(set(s.lower() for s in a.skills) & set(s.lower() for s in b.skills))
    if shared:
        return (
            f"{b.display_name} works with {', '.join(shared[:3])} — a strong "
            f"overlap with your background."
        )
    return f"{b.display_name}'s background looks complementary to yours; worth a quick chat."


async def _llm_complete(prompt: str) -> str:
    """Call ASI:One (OpenAI-compatible) for a short completion."""

    settings = get_settings()
    if not settings.asi1_api_key:
        return ""

    try:
        resp = await security_fetch.post(
            f"{settings.asi1_api_base.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.asi1_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.asi1_model,
                "messages": [
                    {"role": "system", "content": "You write concise networking blurbs."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.5,
                "max_tokens": 90,
            },
        )
    except Exception as exc:
        capture_exception(exc, op="matching.llm_complete")
        return ""

    if resp.status >= 400:
        _log.warning("matching.llm_non_200", status=resp.status)
        return ""
    try:
        body = resp.json()
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError):
        return ""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _to_match_result(cand: _Candidate, explanation: MatchExplanation) -> MatchResult:
    current = cand.profile.linkedin.current_position
    explanation_with_signals = MatchExplanation(
        sentence=explanation.sentence,
        signals=cand.signals[:3],
    )
    return MatchResult(
        attendee_id=cand.attendee.id,
        display_name=cand.attendee.display_name,
        current_role=current.title if current else cand.attendee.current_role,
        company=current.company if current else None,
        headline=cand.attendee.headline,
        linkedin_url=cand.attendee.linkedin_url,
        score=min(1.0, max(0.0, cand.base_score)),
        rerank_score=max(0.0, cand.rerank_score),
        explanation=explanation_with_signals,
    )


async def store_shown_matches(
    *,
    from_id: UUID,
    to_results: list[MatchResult],
    event_id: UUID,
) -> None:
    """Persist each shown match (idempotent per pair)."""

    for result in to_results:
        try:
            await insert_connection(
                event_id=event_id,
                from_attendee_id=from_id,
                to_attendee_id=result.attendee_id,
                match_score=result.rerank_score,
                match_reason=result.explanation.sentence,
                status="shown",
            )
        except Exception as exc:
            capture_exception(
                exc,
                op="matching.store_shown",
                from_id=str(from_id),
                to_id=str(result.attendee_id),
            )


# Convenience: list all event attendees (used by the conversation handler).
async def list_attendees(event_id: UUID, exclude_id: UUID) -> list[AttendeeRecord]:
    return await list_event_attendees(event_id, exclude_id=exclude_id)


# Used in tests to inspect intent normalisation.
_ROLE_NORMALIZE = re.compile(r"\s+")
