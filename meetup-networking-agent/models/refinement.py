"""Parsed natural-language refinement filters."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Intent(StrEnum):
    """What kind of connection the user is looking for."""

    COFOUNDER = "co-founder"
    CONTRIBUTOR = "contributor"
    MENTOR = "mentor"
    FRIEND = "friend"

    @classmethod
    def from_text(cls, text: str) -> Intent:
        """Best-effort intent classification from free text."""

        lowered = text.lower()
        if any(k in lowered for k in ("co-founder", "cofounder", "founder")):
            return cls.COFOUNDER
        if any(k in lowered for k in ("mentor", "advisor", "guidance", "more experience")):
            return cls.MENTOR
        if any(k in lowered for k in ("contributor", "collaborator", "open source", "hack")):
            return cls.CONTRIBUTOR
        return cls.FRIEND


class RefinementQuery(BaseModel):
    """Filters extracted from a user refinement message."""

    model_config = ConfigDict(strict=True, extra="ignore")

    raw_text: str = Field(..., min_length=1)
    intent: Intent | None = None
    required_skills: list[str] = Field(default_factory=list)
    excluded_skills: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    min_years_experience: int | None = Field(default=None, ge=0, le=60)
    free_text_hint: str | None = None

    def has_filters(self) -> bool:
        """``True`` if any filter beyond ``raw_text`` was extracted."""

        return bool(
            self.required_skills
            or self.excluded_skills
            or self.domains
            or self.roles
            or self.min_years_experience is not None
        )
