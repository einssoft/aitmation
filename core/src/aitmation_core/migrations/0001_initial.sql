-- 0001: initial schema.
--
-- Rules that apply to this and every later migration:
--   * Two-release rule (ADR-0013): the PREVIOUS application version must
--     still run against the migrated schema. Adding is fine; renaming or
--     dropping happens in two steps, two releases apart.
--   * Portable schema (ADR-0005): no SQLite-only constructs in tables or
--     columns. The append-only triggers below are the one deliberate
--     exception - they are re-implemented per backend when the Postgres
--     escalation tier is built, because trigger syntax is not portable.
--   * Domain-neutral: the core stores cases, steps and audit entries.
--     It does not know what an invoice is.

-- A case ("Vorgang") is one running instance of a process template.
CREATE TABLE cases (
    id               TEXT PRIMARY KEY,
    template_id      TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    status           TEXT NOT NULL,             -- running | completed | failed | cancelled
    current_step_id  TEXT,                      -- step definition id, NULL once closed
    gate_passed_at   TEXT,                      -- set when an approving HumanGate outcome
                                                -- is recorded; runtime guard for ADR-0007
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    closed_at        TEXT
);

CREATE INDEX idx_cases_status ON cases (status);

-- One row per step instance a case has reached. Results are stored in a
-- comparable form (ADR-0013): structured result, the model reference that
-- produced it and a digest of the input, so a later model change can be
-- replayed against past decisions.
CREATE TABLE steps (
    id           TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES cases (id),
    step_id      TEXT NOT NULL,                 -- id within the process definition
    kind         TEXT NOT NULL,                 -- trigger | agent_step | human_gate
                                                -- | system_action | timer
    status       TEXT NOT NULL,                 -- pending | waiting | completed | failed
    outcome      TEXT,                          -- transition key on completion
    result       TEXT,                          -- JSON, structured step result
    model_ref    TEXT,                          -- model identifier for agent steps
    input_digest TEXT,                          -- digest of the step input
    deadline_at  TEXT,                          -- timers and gate escalation
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE INDEX idx_steps_case ON steps (case_id);
CREATE INDEX idx_steps_due ON steps (status, deadline_at);

-- Append-only audit log (ARCHITECTURE §7): every entry carries the
-- application version and the model reference involved, otherwise a
-- decision cannot be explained after the fact (ADR-0013).
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases (id),
    seq         INTEGER NOT NULL,               -- per-case sequence, gapless
    at          TEXT NOT NULL,
    actor_type  TEXT NOT NULL,                  -- human | agent | system
    actor_id    TEXT NOT NULL,
    event       TEXT NOT NULL,
    details     TEXT,                           -- JSON
    app_version TEXT NOT NULL,
    model_ref   TEXT,
    UNIQUE (case_id, seq)
);

-- Not even an administrator edits history: reject UPDATE and DELETE at
-- the database layer. (Re-implement equivalently on Postgres later.)
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
