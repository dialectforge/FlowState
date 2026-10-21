-- 017: the run ledger's reads (2.4.0). New table only. With session_runs (015: which
-- connection and app a session belongs to; its start and end are the session's own) and
-- run_writes (016: every write's ids), this records per session what was read and written,
-- stored lean: ids and hashes, never the text.
--   session_id   the automatic session (sessions.id); conn_key the connection
--   tool/action  the read (start_here, search, manage_todos list, ...)
--   answer_hash  first 16 hex of sha256 of the answer the caller got
--   records      start_here only: JSON list of "type:id" the brief showed (rules, handoffs,
--                todos, problems, changes, decisions, learnings), else NULL
-- Rows older than FLOWSTATE_ACTIVITY_DAYS (default 90) are pruned now and then, with
-- run_writes. Times are UTC 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS run_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    conn_key TEXT,
    project_id INTEGER NOT NULL,
    app TEXT,
    machine TEXT,
    tool TEXT NOT NULL,
    action TEXT,
    at TEXT NOT NULL,
    answer_hash TEXT,
    records TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_views_session ON run_views(session_id);
CREATE INDEX IF NOT EXISTS idx_run_views_visit ON run_views(project_id, app, tool, at);
