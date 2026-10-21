-- 015: one automatic session per connection (2.4.0). New table only; auto_sessions is not
-- altered. Before, an automatic session was keyed by app name + project, so two windows of
-- the same app (two stdio server processes, or two HTTP MCP sessions) shared one session.
-- A row here ties a session to the connection that opened it; the app keeps its name.
--   conn_key         the connection: stdio = the server process, HTTP = the MCP session id
--   app / model / machine / identity_source   who the session belongs to (identity.py)
-- It is also the run ledger's per-session row (start / end are the session's own times).
-- Sessions opened before 2.4.0 have no row and keep the old app + project matching.
CREATE TABLE IF NOT EXISTS session_runs (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    conn_key TEXT NOT NULL,
    app TEXT,
    model TEXT,
    machine TEXT,
    identity_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_runs_conn ON session_runs(conn_key);
