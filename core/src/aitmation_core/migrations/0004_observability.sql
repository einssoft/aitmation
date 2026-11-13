-- 0004: two log trails + diagnostic events.
--
-- The audit_log stays content-free (compliance trail, unlimited).
-- llm_interactions is the SEPARATE content trail: full task/input/response
-- of every model call, with tokens and frozen CHF cost - access-restricted
-- in spirit, and subject to retention (cleanup in the runtime).
-- diagnostic_events makes errors visible in the admin UI without log
-- access - for the target audience that is Grundsatz 4, not comfort.
-- Additive migration (two-release rule, ADR-0013).

CREATE TABLE llm_interactions (
    id          TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    case_id     TEXT,
    step_id     TEXT,
    task        TEXT NOT NULL,
    input       TEXT,                -- payload JSON as handed to the model
    response    TEXT,                -- response JSON (or error text)
    model_ref   TEXT,
    datenklasse TEXT,                -- declared demand of the calling step
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    kosten_chf  REAL,                -- frozen at call time (registry price)
    duration_ms INTEGER,
    status      TEXT NOT NULL,       -- ok | error
    error       TEXT
);

CREATE INDEX idx_llm_interactions_at ON llm_interactions (at);

CREATE TABLE diagnostic_events (
    id      TEXT PRIMARY KEY,
    at      TEXT NOT NULL,
    level   TEXT NOT NULL,           -- warning | error
    source  TEXT NOT NULL,           -- agent | dispatch | forward | system | web | guardrail
    event   TEXT NOT NULL,
    detail  TEXT,
    context TEXT                     -- JSON: case_id/step_id/... where known
);

CREATE INDEX idx_diagnostic_events_at ON diagnostic_events (at);
