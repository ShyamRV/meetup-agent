-- ============================================================================
-- Migration: 20260522_02_meetup_retention
-- Agent    : meetup-networking-agent
-- Purpose  : 30-day TTL purge for attendee personal data.
-- ============================================================================
--
-- The retention policy is enforced by a daily cron worker (run-purge.py)
-- that calls meetup_purge_expired(). We keep this as a SQL function (vs.
-- inline app code) so the policy is explicit, auditable, and runs in a
-- single transaction.
-- ============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION meetup_purge_expired(retention_days INT DEFAULT 30)
RETURNS TABLE(events_deleted BIGINT, attendees_deleted BIGINT) AS $$
DECLARE
    ev_count BIGINT;
    at_count BIGINT;
BEGIN
    WITH deleted_attendees AS (
        DELETE FROM meetup_attendees
        WHERE checked_in_at < NOW() - (retention_days || ' days')::interval
        RETURNING id
    )
    SELECT COUNT(*) INTO at_count FROM deleted_attendees;

    WITH deleted_events AS (
        DELETE FROM meetup_events
        WHERE COALESCE(expires_at, created_at + (retention_days || ' days')::interval) < NOW()
        RETURNING id
    )
    SELECT COUNT(*) INTO ev_count FROM deleted_events;

    RETURN QUERY SELECT ev_count, at_count;
END;
$$ LANGUAGE plpgsql;

COMMIT;
