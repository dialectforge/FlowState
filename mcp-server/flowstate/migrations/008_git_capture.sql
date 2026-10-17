-- 008: opt-in capture of git commit subjects into a project's changes (v2 Phase D).
-- New table only. No row = off (the default). Only the commit subject line is kept,
-- never authors, e-mail addresses or diffs.
CREATE TABLE IF NOT EXISTS git_capture (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    repo_path TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_commit TEXT,             -- newest commit already captured
    last_run_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    updated_by TEXT               -- cli
);
