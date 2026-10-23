-- 023: disagreement cards. New tables only (nothing already stored is rewritten).
-- A card is one review item of kind "disagreement". Losing entries are marked
-- superseded in entry_standing (and, for two decisions, with a supersedes link).
-- They are not deleted.

CREATE TABLE IF NOT EXISTS entry_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'output')),
    label TEXT NOT NULL,
    excerpt TEXT,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    app TEXT,
    model TEXT,
    machine TEXT,
    identity_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_entry_evidence_record
    ON entry_evidence(record_type, record_id, position);

CREATE TABLE IF NOT EXISTS conflict_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    a_type TEXT NOT NULL,
    a_id INTEGER NOT NULL,
    b_type TEXT NOT NULL,
    b_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'kept_a', 'kept_b', 'kept_both', 'dismissed')),
    how TEXT NOT NULL CHECK (how IN ('flagged', 'same_component')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    review_item_id INTEGER,
    a_app TEXT,
    a_at TEXT,
    b_app TEXT,
    b_at TEXT,
    app TEXT,
    model TEXT,
    machine TEXT,
    identity_source TEXT,
    resolved_app TEXT,
    resolved_model TEXT,
    resolved_machine TEXT,
    resolved_identity_source TEXT,
    resolved_path TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conflict_cards_open
    ON conflict_cards(project_id, a_type, a_id, b_type, b_id)
    WHERE status = 'open';

CREATE TABLE IF NOT EXISTS entry_standing (
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    standing TEXT NOT NULL CHECK (standing IN ('superseded', 'rejected')),
    by_type TEXT,
    by_id INTEGER,
    card_id INTEGER,
    at TEXT NOT NULL,
    app TEXT,
    model TEXT,
    machine TEXT,
    identity_source TEXT,
    PRIMARY KEY (record_type, record_id)
);
