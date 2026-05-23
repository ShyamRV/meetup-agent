"""Natural-language refinement parsing + candidate filtering.

The agent keeps a 6-turn rolling history per session so refinements like
"only frontend devs" feel like a conversation, not a fresh query. The
``parse_refinement`` function uses the LLM with a strict JSON schema so
extracted filters round-trip through ``RefinementQuery``. A deterministic
regex fallback covers the LLM-down case and the no-API-key dev path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from agents_shared import security_fetch
from agents_shared.logging import get_logger
from agents_shared.sentry import capture_exception

from config import get_settings
from models.refinement import Intent, RefinementQuery

if TYPE_CHECKING:  # pragma: no cover - typing only
    from handlers.matching import _Candidate

_log = get_logger(__name__)

MAX_HISTORY_TURNS = 6


# Conservative skill / language vocabulary used by the regex fallback. The
# LLM path is preferred — this exists for offline development and tests
# where we don't want an outbound API call. Adding terms here is cheap and
# safe.
_SKILL_VOCAB: tuple[str, ...] = (
    "python",
    "javascript",
    "typescript",
    "rust",
    "go",
    "golang",
    "ruby",
    "java",
    "kotlin",
    "swift",
    "c++",
    "c#",
    "scala",
    "elixir",
    "react",
    "next.js",
    "vue",
    "svelte",
    "angular",
    "tailwind",
    "django",
    "flask",
    "fastapi",
    "node.js",
    "express",
    "rails",
    "spring",
    "postgres",
    "postgresql",
    "mysql",
    "redis",
    "mongodb",
    "kafka",
    "rabbitmq",
    "aws",
    "gcp",
    "azure",
    "kubernetes",
    "docker",
    "terraform",
    "ansible",
    "machine learning",
    "ml",
    "deep learning",
    "nlp",
    "computer vision",
    "data science",
    "data engineering",
    "blockchain",
    "solidity",
    "web3",
    "crypto",
    "smart contracts",
    "frontend",
    "backend",
    "fullstack",
    "full-stack",
    "mobile",
    "ios",
    "android",
    "design",
    "ux",
    "ui",
    "product",
    "growth",
    "marketing",
    "sales",
)

_DOMAIN_VOCAB: tuple[str, ...] = (
    "fintech",
    "healthtech",
    "biotech",
    "edtech",
    "climate",
    "gaming",
    "robotics",
    "agtech",
    "defense",
    "logistics",
    "ecommerce",
    "saas",
    "developer tools",
    "devtools",
    "open source",
    "open-source",
    "ai",
    "agents",
    "web3",
    "crypto",
    "defi",
)

_ROLE_VOCAB: tuple[str, ...] = (
    "frontend developer",
    "backend developer",
    "fullstack developer",
    "founder",
    "co-founder",
    "cto",
    "ceo",
    "designer",
    "product manager",
    "engineer",
    "data scientist",
    "researcher",
    "ml engineer",
    "ai engineer",
)


# ---------------------------------------------------------------------------
# parse_refinement
# ---------------------------------------------------------------------------


async def parse_refinement(
    message: str,
    context: Sequence[str] | None = None,
) -> RefinementQuery:
    """Extract structured filters from a refinement message."""

    if not message or not message.strip():
        raise ValueError("empty refinement message")

    history = list(context or [])[-MAX_HISTORY_TURNS:]
    llm_query = await _parse_via_llm(message, history)
    if llm_query is not None:
        return llm_query
    return _parse_via_regex(message)


async def _parse_via_llm(message: str, history: list[str]) -> RefinementQuery | None:
    settings = get_settings()
    if not settings.asi1_api_key:
        return None

    instructions = (
        "You extract structured filters from a refinement message at a "
        "networking event. Reply with ONLY a JSON object matching this "
        "schema (no prose):\n"
        "{\n"
        '  "intent": "co-founder|contributor|mentor|friend|null",\n'
        '  "required_skills": ["string"],\n'
        '  "excluded_skills": ["string"],\n'
        '  "domains": ["string"],\n'
        '  "roles": ["string"],\n'
        '  "min_years_experience": integer|null,\n'
        '  "free_text_hint": "string|null"\n'
        "}\n"
        "Use empty arrays / null for missing fields. Skills MUST be "
        "lowercase tech / domain terms (e.g. 'rust', 'fintech')."
    )
    history_block = "\n".join(f"- {h}" for h in history) if history else "(none)"
    user_prompt = (
        f"Conversation so far:\n{history_block}\n\nLatest refinement: {message}\nReturn the JSON."
    )

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
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                "max_tokens": 250,
                "response_format": {"type": "json_object"},
            },
        )
    except Exception as exc:
        capture_exception(exc, op="conversation.parse_llm")
        return None

    if resp.status >= 400:
        _log.warning("conversation.llm_non_200", status=resp.status)
        return None

    try:
        choice = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(choice)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        capture_exception(exc, op="conversation.parse_decode")
        return None

    intent_value = parsed.get("intent")
    intent: Intent | None = None
    if intent_value:
        try:
            intent = Intent(intent_value)
        except ValueError:
            intent = Intent.from_text(intent_value)

    try:
        return RefinementQuery(
            raw_text=message,
            intent=intent,
            required_skills=[
                s.lower().strip()
                for s in parsed.get("required_skills") or []
                if isinstance(s, str) and s.strip()
            ],
            excluded_skills=[
                s.lower().strip()
                for s in parsed.get("excluded_skills") or []
                if isinstance(s, str) and s.strip()
            ],
            domains=[
                d.lower().strip()
                for d in parsed.get("domains") or []
                if isinstance(d, str) and d.strip()
            ],
            roles=[
                r.lower().strip()
                for r in parsed.get("roles") or []
                if isinstance(r, str) and r.strip()
            ],
            min_years_experience=parsed.get("min_years_experience"),
            free_text_hint=parsed.get("free_text_hint"),
        )
    except Exception as exc:
        capture_exception(exc, op="conversation.parse_validate")
        return None


_NEGATION_PATTERN = re.compile(r"\b(?:no|not|except|exclude|without)\s+([a-z0-9+#./\- ]+)")
_YEARS_PATTERN = re.compile(r"\b(\d{1,2})\+?\s*(?:years|yrs)\b", re.IGNORECASE)


def _match_vocab(message: str, vocab: Iterable[str]) -> list[str]:
    lowered = " " + message.lower() + " "
    found: list[str] = []
    for term in vocab:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
        if pattern.search(lowered):
            found.append(term)
    return found


def _parse_via_regex(message: str) -> RefinementQuery:
    """Deterministic fallback parser — used when no LLM key is configured."""

    intent = Intent.from_text(message)

    required = _match_vocab(message, _SKILL_VOCAB)
    domains = _match_vocab(message, _DOMAIN_VOCAB)
    roles = _match_vocab(message, _ROLE_VOCAB)

    excluded: list[str] = []
    for chunk in _NEGATION_PATTERN.findall(message.lower()):
        for term in _SKILL_VOCAB + _DOMAIN_VOCAB:
            if term in chunk:
                excluded.append(term)
                if term in required:
                    required.remove(term)

    years_match = _YEARS_PATTERN.search(message)
    min_years = int(years_match.group(1)) if years_match else None

    return RefinementQuery(
        raw_text=message,
        intent=intent,
        required_skills=sorted(set(required)),
        excluded_skills=sorted(set(excluded)),
        domains=sorted(set(domains)),
        roles=sorted(set(roles)),
        min_years_experience=min_years,
        free_text_hint=None,
    )


# ---------------------------------------------------------------------------
# apply_refinement
# ---------------------------------------------------------------------------


def apply_refinement(
    candidates: list[_Candidate],
    refinement: RefinementQuery,
) -> list[_Candidate]:
    """Filter + soft-rank an existing candidate pool with refinement filters."""

    if not refinement.has_filters():
        return candidates

    required = {s.lower() for s in refinement.required_skills}
    excluded = {s.lower() for s in refinement.excluded_skills}
    domains = {d.lower() for d in refinement.domains}
    roles = {r.lower() for r in refinement.roles}

    filtered: list[_Candidate] = []
    for cand in candidates:
        skills_lower = {s.lower() for s in cand.profile.skills}
        text = " ".join(
            filter(
                None,
                [
                    cand.profile.headline or "",
                    cand.profile.current_role or "",
                    cand.profile.linkedin.summary or "",
                ],
            )
        ).lower()

        if excluded and (skills_lower & excluded):
            continue

        skill_match = bool(required and (skills_lower & required))
        domain_match = bool(
            domains and (any(d in text for d in domains) or (skills_lower & domains))
        )
        role_match = bool(roles and any(r in text for r in roles))

        # A candidate passes whichever filters are *present* if it matches
        # at least one of them. Filters are additive but soft — if you say
        # "Rust devs in fintech", we accept anyone who is *either* Rust or
        # fintech and rank them higher when both fire.
        any_filter = required or domains or roles
        if any_filter and not (skill_match or domain_match or role_match):
            continue

        if refinement.min_years_experience is not None:
            from handlers.matching import _estimate_years_experience

            years = _estimate_years_experience(cand.profile)
            if years < refinement.min_years_experience:
                continue

        boost = 0.05 * len(skills_lower & required)
        cand.rerank_score = cand.rerank_score + boost
        filtered.append(cand)

    filtered.sort(key=lambda c: c.rerank_score, reverse=True)
    return filtered


# ---------------------------------------------------------------------------
# Short conversation history helper (used by agent.py)
# ---------------------------------------------------------------------------


class ConversationHistory:
    """Bounded per-session history of recent user messages."""

    def __init__(self, max_turns: int = MAX_HISTORY_TURNS) -> None:
        self._turns: list[str] = []
        self._max = max_turns

    def append(self, message: str) -> None:
        self._turns.append(message)
        if len(self._turns) > self._max:
            self._turns = self._turns[-self._max :]

    def snapshot(self) -> list[str]:
        return list(self._turns)
