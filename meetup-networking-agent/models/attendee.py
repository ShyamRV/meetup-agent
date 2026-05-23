"""DB-row Pydantic model for ``meetup_attendees``."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from models.profile import ProfileData


class AttendeeRecord(BaseModel):
    """In-memory representation of one ``meetup_attendees`` row."""

    model_config = ConfigDict(strict=True, extra="ignore")

    id: UUID
    event_id: UUID
    linkedin_id: str
    github_username: str | None = None
    display_name: str
    headline: str | None = None
    current_role: str | None = None
    skills: list[str] = Field(default_factory=list)
    profile_json: dict[str, Any]
    linkedin_url: str | None = None
    checked_in_at: datetime
    updated_at: datetime

    def to_profile_data(self) -> ProfileData:
        """Re-hydrate the structured profile from ``profile_json``.

        We go via :meth:`ProfileData.model_validate_json` so ISO-string
        dates round-trip cleanly under ``strict=True`` — Pydantic's JSON
        mode is the canonical way to read JSON payloads strictly.
        """

        return ProfileData.model_validate_json(json.dumps(self.profile_json))
