-- 004: user-controlled review mode (v2 Phase D). New tables only.
-- scope 'install' = this FlowState database as a whole; 'project:<id>' = an override.
-- No row = the default, auto_only (only auto-captured items wait for review).
CREATE TABLE IF NOT EXISTS review_settings (
    scope TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('off', 'auto_only', 'types', 'everything')),
    kinds TEXT NOT NULL DEFAULT '[]',   -- JSON list, for mode 'types'
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    updated_by TEXT
);

-- Every change of mode: who (which app), when, old and new, and the user's words.
CREATE TABLE IF NOT EXISTS review_mode_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    old_mode TEXT,
    old_kinds TEXT,
    new_mode TEXT,                -- NULL when a project override was cleared
    new_kinds TEXT,
    client TEXT,                  -- MCP client name, or 'review-page' / 'cli'
    caller TEXT,                  -- HTTP client id, or 'stdio'
    user_request TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP   -- UTC
);
