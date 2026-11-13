-- 0014: web sources of knowledge spaces (ADR-0045).
--
-- A source is a web address a space keeps itself up to date from: the
-- crawler walks the site within the declared scope, takes the document
-- types the source lists and files every page or file as a document of
-- the space. Documents remember the source and URL they came from, so a
-- re-crawl updates them in place (new version only when the content
-- changed) and retires pages that are gone. Additive migration; the
-- previous release reads knowledge_documents unchanged (ADR-0013).

CREATE TABLE knowledge_sources (
    id             TEXT PRIMARY KEY,
    space_id       TEXT NOT NULL REFERENCES knowledge_spaces (id),
    url            TEXT NOT NULL,         -- start address
    title          TEXT,
    scope          TEXT NOT NULL,         -- page | path | site
    doc_types      TEXT NOT NULL,         -- JSON array: html, pdf, docx, ...
    max_pages      INTEGER NOT NULL,
    max_depth      INTEGER NOT NULL,
    exclude        TEXT,                  -- JSON array of URL patterns
    interval_hours INTEGER NOT NULL,      -- 0 = only on demand
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    last_run_at    TEXT,
    next_run_at    TEXT,
    last_status    TEXT,                  -- running | ok | partial | failed
    last_error     TEXT,
    last_stats     TEXT,                  -- JSON: counts of the last run
    deleted_at     TEXT
);

CREATE INDEX idx_knowledge_sources_space ON knowledge_sources (space_id, deleted_at);
CREATE INDEX idx_knowledge_sources_due ON knowledge_sources (next_run_at);

ALTER TABLE knowledge_documents ADD COLUMN source_id TEXT;
ALTER TABLE knowledge_documents ADD COLUMN url TEXT;

CREATE INDEX idx_knowledge_documents_source ON knowledge_documents (source_id, url);
