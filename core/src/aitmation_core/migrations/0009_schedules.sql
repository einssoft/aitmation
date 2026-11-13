-- 0009: schedules (Stufe 1.3: time as a trigger source).
--
-- One row per template with a schedule trigger: the next planned start
-- and the last actual one. Planned on first sight, never retroactively;
-- a missed slot fires once. Additive migration (ADR-0013).

CREATE TABLE schedules (
    template_id   TEXT PRIMARY KEY,
    next_at       TEXT NOT NULL,
    last_fired_at TEXT
);
