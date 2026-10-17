-- 010: handoff notes (v2 Phase D, E3). New table only.
-- An AI stopping mid-task leaves "where I was / next step"; the next AI sees it first in
-- start_here (after the hard rules) until it is marked picked up.
CREATE TABLE IF NOT EXISTS handoff_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    where_i_was TEXT NOT NULL,
    next_step TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    created_by TEXT,              -- app name
    picked_up_at TEXT,
    picked_up_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_handoff_open ON handoff_notes(project_id, picked_up_at);
