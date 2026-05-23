"""In-process per-sender session store.

The agent is *stateless* across pod restarts — anything that must survive
a restart lives in Postgres. The session store is a soft cache keyed by
the chat sender DID that holds transient onboarding state: which event
the user is checking into, whether LinkedIn and GitHub OAuth have been
completed, and the rolling refinement history. Once the attendee row is
written, the session can be evicted without losing any data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import UUID

from handlers.conversation import ConversationHistory
from handlers.onboarding import OAuthTokens


@dataclass
class Session:
    """Per-sender state we keep in memory between turns."""

    sender: str
    session_id: str
    event_id: str | None = None
    linkedin_tokens: OAuthTokens | None = None
    github_tokens: OAuthTokens | None = None
    attendee_id: UUID | None = None
    intent: str | None = None
    history: ConversationHistory = field(default_factory=ConversationHistory)
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def is_registered(self) -> bool:
        return self.attendee_id is not None

    def touch(self) -> None:
        self.last_seen = time.monotonic()


class SessionStore:
    """Tiny TTL-bounded session map."""

    def __init__(self, *, ttl_seconds: int = 3600) -> None:
        self._sessions: dict[str, Session] = {}
        self._by_session_id: dict[str, str] = {}
        self._ttl = ttl_seconds

    def get_or_create(self, sender: str, session_id: str) -> Session:
        self._evict_expired()
        session = self._sessions.get(sender)
        if session is None:
            session = Session(sender=sender, session_id=session_id)
            self._sessions[sender] = session
            self._by_session_id[session_id] = sender
        session.touch()
        return session

    def get_by_session_id(self, session_id: str) -> Session | None:
        sender = self._by_session_id.get(session_id)
        if not sender:
            return None
        return self._sessions.get(sender)

    def evict(self, sender: str) -> None:
        session = self._sessions.pop(sender, None)
        if session is not None:
            self._by_session_id.pop(session.session_id, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        stale = [s for s, sess in self._sessions.items() if now - sess.last_seen > self._ttl]
        for sender in stale:
            self.evict(sender)


_GLOBAL_STORE = SessionStore()


def get_session_store() -> SessionStore:
    return _GLOBAL_STORE
