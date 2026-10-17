-- 005: edit history (v2 Phase D). New table only.
-- Before FlowState edits or deletes a record, the old row is kept here (JSON) with the new
-- one, so a person can see what changed and undo the last change. One edit can touch
-- several rows (e.g. solving a problem also adds a solution): they share edit_id.
CREATE TABLE IF NOT EXISTS record_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edit_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    project_id INTEGER,
    tool TEXT,                    -- e.g. todo/update, history/undo, cleanup/approve
    old_row TEXT,                 -- JSON; NULL = the row did not exist before
    new_row TEXT,                 -- JSON; NULL = the row was deleted
    client TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    undone_at TEXT,
    undone_by_edit TEXT,          -- edit_id of the undo
    undo_of TEXT                  -- for an undo: the edit_id it reverted
);
CREATE INDEX IF NOT EXISTS idx_record_history_record ON record_history(table_name, record_id, id);
CREATE INDEX IF NOT EXISTS idx_record_history_edit ON record_history(edit_id);
