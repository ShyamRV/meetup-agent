"""Profile models: LinkedIn + GitHub + merged ProfileData.

All models are Pydantic v2 with ``strict=True`` so type coercion bugs
surface at parse time rather than during embedding generation, where
they would be much harder to debug.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class _StrictModel(BaseModel):
    """Base class — all profile models share strict + extra='ignore'."""

    model_config = ConfigDict(
        strict=True,
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------


class LinkedInPosition(_StrictModel):
    """One job entry from LinkedIn ``/v2/me`` ``positions``."""

    title: str
    company: str
    location: str | None = None
    start: date | None = None
    end: date | None = None
    summary: str | None = None

    @property
    def is_current(self) -> bool:
        return self.end is None


class Education(_StrictModel):
    """Education entry from LinkedIn."""

    school: str
    degree: str | None = None
    field_of_study: str | None = None
    start: date | None = None
    end: date | None = None


class LinkedInProfile(_StrictModel):
    """Minimum LinkedIn profile fields we persist."""

    id: str
    name: str
    headline: str | None = None
    summary: str | None = None
    location: str | None = None
    profile_url: HttpUrl | None = None
    positions: list[LinkedInPosition] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)

    @property
    def current_position(self) -> LinkedInPosition | None:
        return next((p for p in self.positions if p.is_current), None)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


class GitHubRepo(_StrictModel):
    """One repo summary derived from the GitHub REST API."""

    name: str
    description: str | None = None
    language: str | None = None
    stars: int = 0
    topics: list[str] = Field(default_factory=list)
    is_pinned: bool = False


class GitHubProfile(_StrictModel):
    """GitHub profile fields we persist."""

    login: str
    name: str | None = None
    bio: str | None = None
    profile_url: HttpUrl | None = None
    primary_languages: list[str] = Field(default_factory=list)
    repos: list[GitHubRepo] = Field(default_factory=list)
    contribution_count: int = 0

    @property
    def pinned_repos(self) -> list[GitHubRepo]:
        return [r for r in self.repos if r.is_pinned]


# ---------------------------------------------------------------------------
# Merged profile
# ---------------------------------------------------------------------------


class ProfileData(_StrictModel):
    """The merged profile we embed and store as ``profile_json``."""

    linkedin: LinkedInProfile
    github: GitHubProfile | None = None

    @property
    def display_name(self) -> str:
        return self.linkedin.name

    @property
    def headline(self) -> str | None:
        return self.linkedin.headline

    @property
    def current_role(self) -> str | None:
        position = self.linkedin.current_position
        if position is None:
            return None
        return f"{position.title} @ {position.company}"

    @property
    def linkedin_id(self) -> str:
        return self.linkedin.id

    @property
    def github_username(self) -> str | None:
        return self.github.login if self.github else None

    @property
    def linkedin_url(self) -> str | None:
        return str(self.linkedin.profile_url) if self.linkedin.profile_url else None

    @property
    def skills(self) -> list[str]:
        """Union of LinkedIn skills, GitHub languages, and repo topics."""

        merged: list[str] = []
        seen: set[str] = set()
        for source in (
            self.linkedin.skills,
            self.github.primary_languages if self.github else [],
            *([r.topics for r in self.github.repos] if self.github else []),
        ):
            for item in source:
                key = item.lower().strip()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(item)
        return merged

    def to_storage_json(self) -> dict[str, Any]:
        """Serialize to the JSON we write into ``profile_json`` JSONB column."""

        return self.model_dump(mode="json", by_alias=False)
