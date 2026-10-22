-- 019: opt-in user-only approvals (2.4.0, approvals.py). New tables only.
-- No row in approval_settings = approvals off (the default, also for upgraded databases).

-- key: 'approvals' (install), 'approvals:project:<id>' (override), 'phone_approvals',
-- 'popup_clients_add' / 'popup_clients_remove' (JSON lists of app names).
CREATE TABLE IF NOT EXISTS approval_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    updated_by TEXT
);

-- One random one-use code per user-only review item, made here and never sent anywhere but
-- the phone link. Bound to the item, the action and an expiry; used on approve AND reject.
CREATE TABLE IF NOT EXISTS approval_requests (
    item_id INTEGER PRIMARY KEY,
    nonce TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL DEFAULT 'approve_review',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

-- Phone grants seen by the approve page (a hash of the grant, never the grant itself): one
-- page load per grant; the page's bound token (hashed) must come back with the tap.
CREATE TABLE IF NOT EXISTS approval_grants (
    grant_hash TEXT PRIMARY KEY,
    item_id INTEGER NOT NULL,
    page_token_hash TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

-- Every user-only decision: how (popup / phone / terminal / review page), from which app,
-- when, and what. Shown by manage_history(action="list").
CREATE TABLE IF NOT EXISTS approval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    project_id INTEGER,
    kind TEXT,
    path TEXT NOT NULL,
    client TEXT,
    decision TEXT NOT NULL,       -- approved / rejected / declined / cancelled / timed_out / failed
    decided_at TEXT NOT NULL      -- UTC
);
CREATE INDEX IF NOT EXISTS idx_approval_log_project ON approval_log(project_id, id);
