-- 0008: notifications (Stufe 1.2, 2026-09-03).
--
-- Every notification about a waiting gate is a row here before anything
-- is sent: "new" when the gate opens, "reminder" after the waiting
-- threshold, "escalation" after the escalation threshold or the gate's
-- own deadline. The shell shows them, a failed send stays visible with
-- its error instead of vanishing. Content-free: subject and body carry
-- template, mandant and time - never document content. Rows expire with
-- the content-trail retention. Additive migration (ADR-0013).

CREATE TABLE notifications (
    id         TEXT PRIMARY KEY,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,                 -- new | reminder | escalation
    case_id    TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL,                 -- pending | sent | failed | unsent
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_at    TEXT,
    error      TEXT
);

CREATE INDEX notifications_gate ON notifications (case_id, step_id, kind);
