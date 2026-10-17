-- 009: per-project hard rules ("never do this") (v2 Phase D, E2). New table only.
-- Shown first in every start_here brief, never truncated. Removal is a soft delete.
CREATE TABLE IF NOT EXISTS project_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    rule TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    created_by TEXT,              -- app name
    removed_at TEXT,
    removed_by TEXT,
    removed_request TEXT          -- the user's words asking for the removal
);
CREATE INDEX IF NOT EXISTS idx_project_rules_project ON project_rules(project_id, removed_at);
