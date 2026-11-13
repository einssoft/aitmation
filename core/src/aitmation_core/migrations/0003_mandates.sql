-- 0003: standing mandates (ADR-0016).
--
-- A mandate is a per-rule approval: granted once by a human at a gate (or
-- during setup), it is the second legitimate source of approval next to a
-- per-case HumanGate. Additive migration; the previous application version
-- ignores this table (two-release rule, ADR-0013).
--
-- Mandates are never deleted - revocation is a timestamp, so the full
-- grant/revoke history stays queryable. The DELETE trigger enforces that;
-- updates are limited by discipline to setting revoked_by/revoked_at
-- (the engine is the only writer).

CREATE TABLE mandates (
    id          TEXT PRIMARY KEY,
    action      TEXT NOT NULL,               -- system action the mandate covers, e.g. "forward"
    target      TEXT NOT NULL,               -- action target, e.g. the addressee id
    condition   TEXT NOT NULL DEFAULT '{}',  -- JSON: envelope-field -> exact value; {} = catch-all
    comment     TEXT NOT NULL DEFAULT '',
    granted_by  TEXT NOT NULL,
    granted_at  TEXT NOT NULL,
    revoked_by  TEXT,
    revoked_at  TEXT
);

CREATE INDEX idx_mandates_action ON mandates (action, revoked_at);

CREATE TRIGGER mandates_no_delete
BEFORE DELETE ON mandates
BEGIN
    SELECT RAISE(ABORT, 'mandates are never deleted; revoke instead (ADR-0016)');
END;
