"""Match-result models returned to the chat layer."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MatchSignal(BaseModel):
    """One discrete reason two people might want to connect."""

    model_config = ConfigDict(strict=True, extra="ignore")

    label: str = Field(..., min_length=1, max_length=80)
    detail: str | None = None
    kind: str = Field(..., pattern=r"^(shared|complementary|experience|domain)$")


class MatchExplanation(BaseModel):
    """The LLM-authored one-sentence "why you should meet" line."""

    model_config = ConfigDict(strict=True, extra="ignore")

    sentence: str = Field(..., min_length=1, max_length=300)
    signals: list[MatchSignal] = Field(default_factory=list, max_length=3)


class MatchResult(BaseModel):
    """One ranked match shown to the requesting user."""

    model_config = ConfigDict(strict=True, extra="ignore")

    attendee_id: UUID
    display_name: str
    current_role: str | None = None
    company: str | None = None
    headline: str | None = None
    linkedin_url: str | None = None
    score: float = Field(..., ge=0.0, le=1.0)
    rerank_score: float = Field(..., ge=0.0)
    explanation: MatchExplanation
