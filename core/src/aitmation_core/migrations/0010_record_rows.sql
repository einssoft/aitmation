-- 0010: collections (ADR-0026 extension, Stufe 1.4).
--
-- Rows of a record schema with kind = "collection" (contacts, accounts):
-- every save is a new version of the row, a deletion is a final version
-- flagged deleted - nothing disappears, a revisor can see who changed
-- which contact when (principle 5). Current state = highest version per
-- row_id. Additive migration (ADR-0013).

CREATE TABLE record_rows (
    id          INTEGER PRIMARY KEY,
    schema_id   TEXT NOT NULL,
    row_id      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    saved_at    TEXT NOT NULL,
    saved_by    TEXT NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (row_id, version)
);

CREATE INDEX record_rows_schema ON record_rows (schema_id, row_id);
