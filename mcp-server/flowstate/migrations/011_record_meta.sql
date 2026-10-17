-- 011: where a record came from and when it was last checked (v2 Phase D, E5/E6).
-- New table only (no columns are added to the record tables, which the v1 server shares).
-- No row = source unknown, never verified: the default for every older record.
CREATE TABLE IF NOT EXISTS record_meta (
    record_type TEXT NOT NULL,    -- todo, decision, change, learning, problem, variable, method
    record_id INTEGER NOT NULL,
    source TEXT CHECK (source IS NULL OR source IN ('user_stated', 'ai_inferred', 'from_code')),
    verified_at TEXT,             -- UTC; NULL = never verified
    verified_by TEXT,             -- app name
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (record_type, record_id)
);
CREATE INDEX IF NOT EXISTS idx_record_meta_verified ON record_meta(verified_at);
