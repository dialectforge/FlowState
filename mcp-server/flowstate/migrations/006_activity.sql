-- 006: per-app activity log and pause switch (v2 Phase D). New tables only.
-- One row per tool call: when, which app, which tool/action, which project, ok or the
-- error code. Never the arguments, the text or anything secret.
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    app TEXT NOT NULL,            -- MCP client name, or the per-app token's name
    caller TEXT,                  -- stdio, owner, or the HTTP client id
    tool TEXT NOT NULL,
    action TEXT,
    project_id INTEGER,
    write INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL,
    error TEXT,                   -- error code only (e.g. project_required)
    ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_activity_app ON activity_log(app, at);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_log(at);

-- The user's switch per app: a paused app may still read, but its writes are refused.
CREATE TABLE IF NOT EXISTS app_controls (
    app TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    changed_by TEXT               -- cli / review-page
);
