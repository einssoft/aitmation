-- 0007: declared records (ADR-0026).
--
-- Values of a record schema (company profile, ...) as appended versions:
-- the current state is the highest version per schema_id, earlier rows
-- stay so a revisor can see who changed which master data when
-- (principle 5). Schemas themselves are declarations in bundles or the
-- local bundle, never in the database. Additive migration (ADR-0013).

CREATE TABLE records (
    id          INTEGER PRIMARY KEY,
    schema_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    saved_at    TEXT NOT NULL,
    saved_by    TEXT NOT NULL,
    UNIQUE (schema_id, version)
);
