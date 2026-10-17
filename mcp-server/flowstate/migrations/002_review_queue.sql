-- 002: review queue (v2 Phase B). New table only; existing tables are not altered.
-- Items wait here for the user's OK: auto-captured session summaries, and records an
-- agent logged with suggested=true. Approved items become normal records; rejected
-- items stay here with the reason (never deleted).
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- session_summary | decision | change | problem | learning | todo
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    summary TEXT NOT NULL,        -- the proposed text
    details TEXT,
    payload TEXT NOT NULL DEFAULT '{}', -- JSON: everything needed to create the record
    source TEXT,                  -- MCP client name, or 'auto-session'
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- UTC
    decided_at TEXT,
    decided_by TEXT,              -- client that relayed the user's decision
    user_request TEXT,            -- the user's words asking for the decision
    reject_reason TEXT,
    result_table TEXT,            -- where the approved record went
    result_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_review_items_pending ON review_items(status, project_id);

-- Summaries auto-captured before this migration join the queue (insert only).
INSERT INTO review_items (project_id, kind, summary, payload, source, session_id, created_at)
SELECT s.project_id, 'session_summary', s.summary, '{"backfilled": true}', 'auto-session',
       s.id, COALESCE(s.ended_at, s.started_at, CURRENT_TIMESTAMP)
FROM sessions s
WHERE s.project_id IS NOT NULL
  AND s.summary LIKE '[auto-captured, needs review]%'
  AND NOT EXISTS (SELECT 1 FROM review_items r
                  WHERE r.session_id = s.id AND r.kind = 'session_summary');
