-- 001: implicit (auto) sessions, introduced in v2 Phase A.
-- Identical to the v2 block in database/schema.sql; IF NOT EXISTS, so a database that
-- already has the table (Phase A ran ensure_schema on it) only gets the version row.
CREATE TABLE IF NOT EXISTS auto_sessions (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    client TEXT NOT NULL,
    server_instance TEXT NOT NULL,
    opened_at TEXT NOT NULL,      -- UTC, 'YYYY-MM-DD HH:MM:SS'
    last_activity TEXT NOT NULL,  -- UTC
    call_count INTEGER NOT NULL DEFAULT 0,
    events TEXT NOT NULL DEFAULT '[]', -- JSON list of short event labels (capped)
    closed_at TEXT,
    close_reason TEXT             -- idle | shutdown | stale | explicit
);
CREATE INDEX IF NOT EXISTS idx_auto_sessions_open ON auto_sessions(closed_at, client, project_id);
