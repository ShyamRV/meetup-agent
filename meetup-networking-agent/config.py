"""Process-wide configuration loaded from environment variables.

Centralising config in one place keeps secrets out of import-time module
state and makes the agent trivially testable — every consumer reads from
the same ``Settings`` instance which is patched in unit tests.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    """Validated environment configuration."""

    model_config = ConfigDict(strict=False, extra="ignore")

    agent_name: str = Field(default="meetup-networking-agent")
    agent_seed: str = Field(
        default_factory=lambda: os.getenv("AGENT_SEED", "meetup-networking-agent-dev-seed")
    )
    agent_port: int = Field(default_factory=lambda: int(os.getenv("AGENT_PORT", "8000")))
    agent_endpoint: str = Field(
        default_factory=lambda: os.getenv("AGENT_ENDPOINT", "http://127.0.0.1:8000/submit")
    )

    # ------------------------------------------------------------------
    # Agentverse / mailbox
    # ------------------------------------------------------------------
    #
    # ``agent_mailbox`` enables the Agentverse mailbox transport so the
    # agent can receive messages from ASI:One and DeltaV without holding
    # an inbound public HTTP port for envelope traffic. Toggle it on in
    # any deployment where the host doesn't expose a stable public URL
    # (Railway, Render, Fly.io) or where you'd rather avoid TLS overhead.
    agent_mailbox: bool = Field(
        default_factory=lambda: os.getenv("AGENT_MAILBOX", "false").lower()
        in {"1", "true", "yes", "on"}
    )
    agent_handle: str | None = Field(
        default_factory=lambda: os.getenv("AGENT_HANDLE") or None
    )
    agent_description: str = Field(
        default_factory=lambda: os.getenv(
            "AGENT_DESCRIPTION",
            "Helps attendees at tech meetups find relevant connections. "
            "Given a meetup event ID and authenticated LinkedIn (optionally "
            "GitHub) profile, returns the top 5 attendees worth talking to, "
            "ranked by intent: co-founder, contributor, mentor, friend.",
        )
    )
    agentverse_base_url: str = Field(
        default_factory=lambda: os.getenv("AGENTVERSE_BASE_URL", "agentverse.ai")
    )

    database_url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL", ""))

    linkedin_client_id: str = Field(default_factory=lambda: os.getenv("LINKEDIN_CLIENT_ID", ""))
    linkedin_client_secret: str = Field(
        default_factory=lambda: os.getenv("LINKEDIN_CLIENT_SECRET", "")
    )
    linkedin_redirect_uri: str = Field(
        default_factory=lambda: os.getenv("LINKEDIN_REDIRECT_URI", "")
    )

    github_client_id: str = Field(default_factory=lambda: os.getenv("GITHUB_CLIENT_ID", ""))
    github_client_secret: str = Field(default_factory=lambda: os.getenv("GITHUB_CLIENT_SECRET", ""))
    github_redirect_uri: str = Field(default_factory=lambda: os.getenv("GITHUB_REDIRECT_URI", ""))

    openai_api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    embedding_model: str = Field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    )
    asi1_api_key: str = Field(default_factory=lambda: os.getenv("ASI1_API_KEY", ""))
    asi1_model: str = Field(default_factory=lambda: os.getenv("ASI1_MODEL", "asi1-mini"))
    asi1_api_base: str = Field(
        default_factory=lambda: os.getenv("ASI1_API_BASE", "https://api.asi1.ai/v1")
    )

    sentry_dsn: str = Field(default_factory=lambda: os.getenv("SENTRY_DSN", ""))
    environment: str = Field(default_factory=lambda: os.getenv("ENVIRONMENT", "development"))
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    session_secret: str = Field(
        default_factory=lambda: os.getenv("SESSION_SECRET", "change-me-in-prod")
    )
    retention_days: int = Field(default_factory=lambda: int(os.getenv("RETENTION_DAYS", "30")))

    # ------------------------------------------------------------------
    # Development-only knobs
    # ------------------------------------------------------------------
    #
    # ``dev_linkedin_id`` short-circuits the OAuth check-in flow. When
    # set (and only when ``environment`` is *not* ``production``), the
    # agent treats every incoming chat sender as if they had completed
    # LinkedIn auth and resolved to this LinkedIn id. The attendee must
    # already exist in the database for the event_id in context — use
    # ``scripts/smoke.py`` to seed one.
    #
    # The production guard is intentional belt-and-braces: even if the
    # variable is accidentally set in staging/prod it has no effect.
    dev_linkedin_id: str = Field(
        default_factory=lambda: os.getenv("DEV_LINKEDIN_ID", "")
    )

    @property
    def prompts_dir(self) -> Path:
        return Path(__file__).parent / "prompts"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton settings instance."""

    return Settings()
