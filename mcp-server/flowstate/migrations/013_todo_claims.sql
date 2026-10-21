-- 013: to-do claims with an expiry (2.3.0). New tables only; the v1 todos table is untouched.
-- One row per claimed todo. A row whose expires_at has passed counts as no claim (it is
-- overwritten by the next claim); release, handoff and done / cancelled delete the row.
--   holder           app + machine + session, the key that says "the same claimer"
--   session          stdio: this server process; HTTP: the MCP session id (if any)
-- Times are UTC 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS todo_claims (
    todo_id INTEGER PRIMARY KEY REFERENCES todos(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    holder TEXT NOT NULL,
    app TEXT,
    model TEXT,
    machine TEXT,
    identity_source TEXT,
    session TEXT,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todo_claims_project ON todo_claims(project_id, expires_at);

-- Every claim event, kept (shown by manage_history who): claim, renew, release, handoff,
-- override (someone changed a todo another agent held), cleared (done / cancelled).
CREATE TABLE IF NOT EXISTS todo_claim_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id INTEGER NOT NULL,
    project_id INTEGER,
    event TEXT NOT NULL,
    summary TEXT NOT NULL,
    at TEXT NOT NULL,
    app TEXT,
    model TEXT,
    machine TEXT,
    identity_source TEXT,
    session TEXT
);
CREATE INDEX IF NOT EXISTS idx_todo_claim_events_at ON todo_claim_events(at);
