-- 016: write receipts and the run ledger's writes (2.4.0). New table only.
-- One row per write when it ends: ids and a hash only, never the text. A write that timed
-- out but saved later is reported from here on its connection's next answer; the same
-- arguments from the same connection within 2 minutes return the saved row's receipt
-- instead of saving a copy.
--   change_id     12 hex characters; the edit history uses it as its edit_id
--   conn_key      the connection (stdio server process + client connection, or the HTTP
--                 MCP session id); session_id: the automatic session (sessions.id), if any
--   app / machine who made it (identity.py)
--   content_hash  first 32 hex of sha256(tool + arguments): spots an exact repeat
--   status        saved | failed (error: the error code only)
--   records       JSON list of "type:id" the write saved, e.g. ["decision:31"]
--   told_at       when the caller was told how a timed-out write ended (or got its receipt
--                 again for a repeat)
-- Times are UTC 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS run_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL UNIQUE,
    conn_key TEXT,
    session_id INTEGER,
    project_id INTEGER,
    app TEXT,
    machine TEXT,
    tool TEXT NOT NULL,
    action TEXT,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('saved', 'failed')),
    records TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    told_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_writes_repeat ON run_writes(conn_key, content_hash, started_at);
CREATE INDEX IF NOT EXISTS idx_run_writes_session ON run_writes(session_id);
CREATE INDEX IF NOT EXISTS idx_run_writes_project ON run_writes(project_id, finished_at);
