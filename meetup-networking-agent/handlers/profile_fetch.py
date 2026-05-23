"""LinkedIn + GitHub profile fetchers and merger.

The two providers expose very different JSON shapes — we normalise both
into :class:`ProfileData` so the embedding step and the matcher can
remain provider-agnostic. Every external call is funneled through
``agents_shared.security_fetch`` for SSRF safety + retries.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date
from typing import Any

from agents_shared import security_fetch
from agents_shared.logging import get_logger
from agents_shared.sentry import capture_exception

from models.profile import (
    Education,
    GitHubProfile,
    GitHubRepo,
    LinkedInPosition,
    LinkedInProfile,
    ProfileData,
)

_log = get_logger(__name__)

LINKEDIN_API_BASE = "https://api.linkedin.com/v2"
GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------


async def fetch_linkedin_profile(access_token: str) -> dict[str, Any]:
    """Fetch the LinkedIn profile + return the *parsed* dict form.

    We deliberately return a plain dict (not the Pydantic model) so this
    function can also serve as a stable shape for tests and audit logs.

    Strategy:
    * Try the OpenID Connect ``/v2/userinfo`` endpoint first. This is
      the only path available to LinkedIn apps created after 2023.
    * If that returns 401/403, fall back to the legacy ``/v2/me`` +
      enrichment endpoints for grandfathered apps with ``r_liteprofile``.

    The OIDC userinfo response is intentionally thin (no positions /
    skills / education), so the matching pipeline learns most of its
    signal from GitHub. The merged profile model accepts empty lists
    for everything except ``id`` + ``name``.
    """

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    # --- OpenID Connect ---
    try:
        oidc_resp = await security_fetch.get(
            f"{LINKEDIN_API_BASE}/userinfo", headers=headers
        )
    except Exception as exc:
        capture_exception(exc, op="profile_fetch.linkedin_userinfo")
        oidc_resp = None  # type: ignore[assignment]

    if oidc_resp is not None and oidc_resp.status < 400:
        body = oidc_resp.json()
        return {
            "id": body["sub"],
            "name": body.get("name")
            or " ".join(
                filter(
                    None,
                    [body.get("given_name"), body.get("family_name")],
                )
            )
            or body["sub"],
            "headline": None,
            "summary": None,
            "profile_url": None,
            "positions": [],
            "skills": [],
            "education": [],
            "location": body.get("locale"),
        }

    # --- Legacy /v2/me fallback (grandfathered r_liteprofile apps) ---
    legacy_headers = {**headers, "X-Restli-Protocol-Version": "2.0.0"}
    me_url = f"{LINKEDIN_API_BASE}/me"
    me_params = {
        "projection": (
            "(id,localizedFirstName,localizedLastName,localizedHeadline,vanityName,profilePicture)"
        )
    }
    try:
        me_resp = await security_fetch.get(me_url, headers=legacy_headers, params=me_params)
    except Exception as exc:
        capture_exception(exc, op="profile_fetch.linkedin_me")
        raise

    if me_resp.status >= 400:
        raise RuntimeError(
            f"linkedin /me returned {me_resp.status}: {me_resp.text[:200]} "
            "(and /v2/userinfo also failed — check your LinkedIn app scopes)"
        )
    me = me_resp.json()

    profile = {
        "id": me["id"],
        "name": " ".join(
            filter(
                None,
                [me.get("localizedFirstName"), me.get("localizedLastName")],
            )
        )
        or me.get("vanityName")
        or me["id"],
        "headline": me.get("localizedHeadline"),
        "summary": None,
        "profile_url": (
            f"https://www.linkedin.com/in/{me['vanityName']}" if me.get("vanityName") else None
        ),
        "positions": [],
        "skills": [],
        "education": [],
        "location": None,
    }

    positions = await _fetch_linkedin_positions(legacy_headers)
    if positions is not None:
        profile["positions"] = positions

    skills = await _fetch_linkedin_skills(legacy_headers)
    if skills is not None:
        profile["skills"] = skills

    education = await _fetch_linkedin_education(legacy_headers)
    if education is not None:
        profile["education"] = education

    return profile


async def _fetch_linkedin_positions(headers: dict[str, str]) -> list[dict[str, Any]] | None:
    try:
        resp = await security_fetch.get(
            f"{LINKEDIN_API_BASE}/me?projection=(positions)",
            headers=headers,
        )
    except Exception as exc:
        capture_exception(exc, op="profile_fetch.linkedin_positions")
        return None
    if resp.status >= 400:
        _log.warning("linkedin.positions_unavailable", status=resp.status)
        return None
    elements = (resp.json().get("positions") or {}).get("elements") or []
    parsed: list[dict[str, Any]] = []
    for el in elements:
        parsed.append(
            {
                "title": el.get("title", ""),
                "company": (el.get("companyName") or {}).get("localized", {}).get("en_US")
                or el.get("companyName")
                or "",
                "location": el.get("locationName"),
                "start": _ld_date(el.get("timePeriod", {}).get("startDate")),
                "end": _ld_date(el.get("timePeriod", {}).get("endDate")),
                "summary": el.get("description"),
            }
        )
    return parsed


async def _fetch_linkedin_skills(headers: dict[str, str]) -> list[str] | None:
    try:
        resp = await security_fetch.get(
            f"{LINKEDIN_API_BASE}/me/skills",
            headers=headers,
        )
    except Exception as exc:
        capture_exception(exc, op="profile_fetch.linkedin_skills")
        return None
    if resp.status >= 400:
        return None
    elements = resp.json().get("elements") or []
    return [
        e.get("name", {}).get("localized", {}).get("en_US")
        for e in elements
        if e.get("name", {}).get("localized", {}).get("en_US")
    ]


async def _fetch_linkedin_education(headers: dict[str, str]) -> list[dict[str, Any]] | None:
    try:
        resp = await security_fetch.get(
            f"{LINKEDIN_API_BASE}/me?projection=(educations)",
            headers=headers,
        )
    except Exception as exc:
        capture_exception(exc, op="profile_fetch.linkedin_education")
        return None
    if resp.status >= 400:
        return None
    elements = (resp.json().get("educations") or {}).get("elements") or []
    return [
        {
            "school": el.get("schoolName", ""),
            "degree": el.get("degreeName"),
            "field_of_study": el.get("fieldOfStudy"),
            "start": _ld_date(el.get("timePeriod", {}).get("startDate")),
            "end": _ld_date(el.get("timePeriod", {}).get("endDate")),
        }
        for el in elements
    ]


def _ld_date(payload: dict[str, Any] | None) -> date | None:
    if not payload:
        return None
    year = payload.get("year")
    if not year:
        return None
    return date(int(year), int(payload.get("month") or 1), int(payload.get("day") or 1))


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


async def fetch_github_profile(access_token: str) -> dict[str, Any]:
    """Fetch a normalised GitHub profile."""

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        user_resp = await security_fetch.get(f"{GITHUB_API_BASE}/user", headers=headers)
    except Exception as exc:
        capture_exception(exc, op="profile_fetch.github_user")
        raise

    if user_resp.status >= 400:
        raise RuntimeError(f"github /user returned {user_resp.status}: {user_resp.text[:200]}")
    user = user_resp.json()

    # Paginate the user's repos (top 30 owned, sorted by pushed).
    repos_resp, events_resp = await asyncio.gather(
        security_fetch.get(
            f"{GITHUB_API_BASE}/user/repos",
            headers=headers,
            params={"sort": "pushed", "per_page": 30, "affiliation": "owner"},
        ),
        security_fetch.get(
            f"{GITHUB_API_BASE}/users/{user['login']}/events/public",
            headers=headers,
            params={"per_page": 100},
        ),
    )

    repos_raw: list[dict[str, Any]] = repos_resp.json() if repos_resp.status < 400 else []
    events: list[dict[str, Any]] = events_resp.json() if events_resp.status < 400 else []

    # GitHub doesn't return a "pinned" flag on the REST API; we approximate
    # by sorting on stars + recency.
    sorted_repos = sorted(
        repos_raw,
        key=lambda r: (r.get("stargazers_count", 0), r.get("pushed_at", "")),
        reverse=True,
    )
    pinned_set = {r["name"] for r in sorted_repos[:6]}

    repos: list[dict[str, Any]] = []
    language_counter: Counter[str] = Counter()
    for r in sorted_repos:
        lang = r.get("language")
        if lang:
            language_counter[lang] += 1
        repos.append(
            {
                "name": r["name"],
                "description": r.get("description"),
                "language": lang,
                "stars": int(r.get("stargazers_count", 0) or 0),
                "topics": list(r.get("topics") or []),
                "is_pinned": r["name"] in pinned_set,
            }
        )

    contribution_count = sum(
        1
        for ev in events
        if ev.get("type") in ("PushEvent", "PullRequestEvent", "IssuesEvent", "CreateEvent")
    )

    return {
        "login": user["login"],
        "name": user.get("name"),
        "bio": user.get("bio"),
        "profile_url": user.get("html_url"),
        "primary_languages": [lang for lang, _ in language_counter.most_common(8)],
        "repos": repos,
        "contribution_count": contribution_count,
    }


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge_profiles(linkedin: dict[str, Any], github: dict[str, Any] | None) -> ProfileData:
    """Validate + merge raw dicts into a :class:`ProfileData` model."""

    li_model = LinkedInProfile(
        id=linkedin["id"],
        name=linkedin["name"],
        headline=linkedin.get("headline"),
        summary=linkedin.get("summary"),
        location=linkedin.get("location"),
        profile_url=linkedin.get("profile_url"),
        positions=[LinkedInPosition(**p) for p in linkedin.get("positions", [])],
        skills=list(linkedin.get("skills", [])),
        education=[Education(**e) for e in linkedin.get("education", [])],
    )

    gh_model: GitHubProfile | None = None
    if github:
        gh_model = GitHubProfile(
            login=github["login"],
            name=github.get("name"),
            bio=github.get("bio"),
            profile_url=github.get("profile_url"),
            primary_languages=list(github.get("primary_languages", [])),
            repos=[GitHubRepo(**r) for r in github.get("repos", [])],
            contribution_count=int(github.get("contribution_count", 0) or 0),
        )

    return ProfileData(linkedin=li_model, github=gh_model)
