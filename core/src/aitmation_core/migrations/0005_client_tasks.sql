-- 0005: client task queue.
--
-- A SystemAction with exec = "client" is not executed by the runtime -
-- the case pauses while a task is queued here; an external actuator
-- (typically a browser extension in the user's authenticated session)
-- claims it, executes it and posts the result, which then completes the
-- step on the next tick. The actuator is deliberately exchangeable: a
-- later MCP connector replaces only the claimer, never the queue, the
-- workflow or the gates. Additive migration (two-release rule, ADR-0013).

CREATE TABLE client_tasks (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases (id),
    step_id     TEXT NOT NULL,
    action      TEXT NOT NULL,
    params      TEXT,                 -- JSON, from the step config
    status      TEXT NOT NULL,        -- open | claimed | done | error
    claimed_by  TEXT,
    result      TEXT,                 -- JSON, posted by the actuator
    created_at  TEXT NOT NULL,
    claimed_at  TEXT,
    finished_at TEXT
);

CREATE INDEX idx_client_tasks_status ON client_tasks (status);
CREATE INDEX idx_client_tasks_case ON client_tasks (case_id, step_id);
