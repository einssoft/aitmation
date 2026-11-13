-- 0006: installed apps (ADR-0023, declaration format v1).
--
-- One row per bundle the instance has seen under apps_dir. "enabled"
-- is the owner's switch (format v1 enables on first sight - a documented
-- transition rule until the UI carries the gate); settings_json holds
-- the answers to the app's declared settings. Rows are never deleted:
-- a removed app keeps its history so the audit trail of past cases can
-- still name the app version that ran them. Additive migration
-- (two-release rule, ADR-0013).

CREATE TABLE installed_apps (
    id            TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    format        INTEGER NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    installed_at  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}'
);
