-- ============================================================================
-- Migration: 20260522_01_meetup_networking
-- Agent    : meetup-networking-agent
-- Purpose  : tables + pgvector index supporting QR check-in flow at meetups.
-- ============================================================================
--
-- Idempotency:
--   * Every CREATE uses IF NOT EXISTS so a re-run is a no-op.
--   * Unique key on (event_id, linkedin_id) drives ON CONFLICT DO UPDATE for
--     attendee upserts.
--   * Unique key on attendee_id in meetup_embeddings drives ON CONFLICT DO
--     UPDATE for vector upserts when the user re-checks-in or refreshes
--     their profile.
--
-- Privacy:
--   * profile_json is the only place full payloads live and is wiped after
--     the 30-day retention window (see retention.sql).
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ----------------------------------------------------------------------------
-- meetup_events
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meetup_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_name      TEXT NOT NULL,
    organizer_email TEXT,
    qr_seed         TEXT UNIQUE NOT NULL,
    event_date      TIMESTAMPTZ,
    location        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_meetup_events_expires_at
    ON meetup_events (expires_at)
    WHERE expires_at IS NOT NULL;

-- ----------------------------------------------------------------------------
-- meetup_attendees
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meetup_attendees (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id        UUID NOT NULL REFERENCES meetup_events(id) ON DELETE CASCADE,
    linkedin_id     TEXT NOT NULL,
    github_username TEXT,
    display_name    TEXT NOT NULL,
    headline        TEXT,
    -- ``current_role`` is a reserved SQL keyword (resolves to the CURRENT_ROLE
    -- function), so the identifier MUST be quoted everywhere it appears in
    -- raw SQL. We pay this papercut in exchange for matching the column name
    -- requested in the agent spec.
    "current_role"  TEXT,
    skills          TEXT[] NOT NULL DEFAULT '{}',
    profile_json    JSONB NOT NULL,
    linkedin_url    TEXT,
    checked_in_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT meetup_attendees_event_linkedin_uq UNIQUE (event_id, linkedin_id)
);

CREATE INDEX IF NOT EXISTS idx_meetup_attendees_event
    ON meetup_attendees (event_id);

CREATE INDEX IF NOT EXISTS idx_meetup_attendees_skills_gin
    ON meetup_attendees USING GIN (skills);

CREATE INDEX IF NOT EXISTS idx_meetup_attendees_profile_jsonb
    ON meetup_attendees USING GIN (profile_json jsonb_path_ops);

-- ----------------------------------------------------------------------------
-- meetup_embeddings
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meetup_embeddings (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attendee_id    UUID NOT NULL UNIQUE REFERENCES meetup_attendees(id) ON DELETE CASCADE,
    event_id       UUID NOT NULL,
    embedding      vector(1536) NOT NULL,
    model_version  TEXT NOT NULL DEFAULT 'text-embedding-3-small',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_meetup_embeddings_event
    ON meetup_embeddings (event_id);

CREATE INDEX IF NOT EXISTS idx_meetup_embeddings_ivfflat
    ON meetup_embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- ----------------------------------------------------------------------------
-- meetup_connections
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meetup_connections (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id          UUID NOT NULL,
    from_attendee_id  UUID NOT NULL REFERENCES meetup_attendees(id) ON DELETE CASCADE,
    to_attendee_id    UUID NOT NULL REFERENCES meetup_attendees(id) ON DELETE CASCADE,
    status            TEXT NOT NULL DEFAULT 'shown'
        CHECK (status IN ('shown', 'accepted', 'dismissed')),
    match_score       DOUBLE PRECISION,
    match_reason      TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT meetup_connections_pair_uq UNIQUE (event_id, from_attendee_id, to_attendee_id)
);

CREATE INDEX IF NOT EXISTS idx_meetup_connections_event
    ON meetup_connections (event_id);

CREATE INDEX IF NOT EXISTS idx_meetup_connections_from
    ON meetup_connections (from_attendee_id, status);

-- ----------------------------------------------------------------------------
-- Updated-at trigger
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meetup_set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_meetup_attendees_updated_at ON meetup_attendees;
CREATE TRIGGER trg_meetup_attendees_updated_at
    BEFORE UPDATE ON meetup_attendees
    FOR EACH ROW
    EXECUTE FUNCTION meetup_set_updated_at();

COMMIT;
