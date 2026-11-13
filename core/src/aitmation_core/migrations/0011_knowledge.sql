-- 0011: knowledge spaces (ADR-0031).
--
-- A space is an entity of its own with a data class and an owner;
-- templates bind to spaces by id (n:m), fail-closed. Documents keep
-- their original as a file under <data>/knowledge/<space>/ (never in the
-- database); the rows hold the current version and its chunks. Chunks
-- carry an optional embedding as JSON text - cosine in Python, no vector
-- extension (SQLite everywhere, ADR-0005). Additive migration.

CREATE TABLE knowledge_spaces (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,           -- JSON: multilingual or plain string
    description TEXT,                    -- JSON: multilingual or plain string
    data_class  TEXT NOT NULL,           -- the class of the most sensitive content
    owner       TEXT,                    -- who answers for the content
    source      TEXT NOT NULL,           -- local | app:<id>
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE knowledge_documents (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL REFERENCES knowledge_spaces (id),
    title       TEXT NOT NULL,
    file_name   TEXT NOT NULL,           -- original name as uploaded
    stored_path TEXT NOT NULL,           -- current original, relative to the knowledge dir
    version     INTEGER NOT NULL,
    sha256      TEXT,
    size        INTEGER,
    status      TEXT NOT NULL,           -- indexed | failed
    error       TEXT,
    method      TEXT,                    -- how the text was read (text_layer, ocr:..., transcript:...)
    chunk_count INTEGER NOT NULL DEFAULT 0,
    embedded    INTEGER NOT NULL DEFAULT 0,  -- 1 when the chunks carry embeddings
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL,
    deleted_at  TEXT
);

CREATE INDEX idx_knowledge_documents_space ON knowledge_documents (space_id, deleted_at);

CREATE TABLE knowledge_chunks (
    id          INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES knowledge_documents (id),
    space_id    TEXT NOT NULL,
    ord         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   TEXT                     -- JSON array of floats, or NULL
);

CREATE INDEX idx_knowledge_chunks_space ON knowledge_chunks (space_id);
CREATE INDEX idx_knowledge_chunks_document ON knowledge_chunks (document_id);
