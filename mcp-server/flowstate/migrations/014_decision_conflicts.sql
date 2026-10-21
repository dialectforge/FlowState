-- 014: possible conflicting decisions (2.3.0). New table only; no existing table is altered.
-- When a decision is logged without replaces=, FlowState looks for current decisions in the
-- same project that read alike (keyword similarity, plus meaning when the local model is
-- installed). FlowState has no language model: a row here is a POSSIBLE conflict for the AI
-- and the user to judge, never a verdict.
--   new_id / old_id  the two decisions (conversations ids); new_id is always the newer one,
--                    so UNIQUE(new_id, old_id) keeps one row per pair
--   score            0-1 similarity that made it a candidate
--   same_component   1 when both decisions are about the same component
--   status           open | resolved_replaced (a later decision replaced either one) |
--                    dismissed_compatible (the user said they fit together)
--   confirmed_at     the user said they really conflict (the pair stays open until replaced)
--   review_item_id   the review-queue item (kind 'conflict') the user approves or rejects
-- Times are UTC 'YYYY-MM-DD HH:MM:SS'. app / model / machine / identity_source: who flagged
-- it (the caller that logged the newer decision); resolved_*: who resolved it.
CREATE TABLE IF NOT EXISTS decision_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    new_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    old_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    same_component INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved_replaced', 'dismissed_compatible')),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    resolved_at TEXT,
    resolved_by_decision INTEGER,
    review_item_id INTEGER,
    app TEXT,
    model TEXT,
    machine TEXT,
    identity_source TEXT,
    resolved_app TEXT,
    resolved_model TEXT,
    resolved_machine TEXT,
    resolved_identity_source TEXT,
    UNIQUE (new_id, old_id)
);
CREATE INDEX IF NOT EXISTS idx_decision_conflicts_open ON decision_conflicts(project_id, status);
CREATE INDEX IF NOT EXISTS idx_decision_conflicts_old ON decision_conflicts(old_id, status);
