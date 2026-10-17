-- 007: local "search by meaning" index (v2 Phase D). New table only.
-- One vector per record per model, made on this computer by a local model (no cloud API).
-- The older `embeddings` table is left alone: its CHECK excludes todos and decisions, and
-- it was meant for the sqlite-vec extension.
CREATE TABLE IF NOT EXISTS meaning_index (
    content_type TEXT NOT NULL,   -- todo | decision | change | learning | problem | solution
    content_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,         -- float32, unit length
    text_hash TEXT NOT NULL,      -- re-embedded when the text changes
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (content_type, content_id, model)
);
CREATE INDEX IF NOT EXISTS idx_meaning_project ON meaning_index(model, project_id);
