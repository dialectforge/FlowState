-- 021: the phone review list's one-use codes (2.4.0, approvals.review_link, review_phone.py).
-- New table only. A code is made on this computer, shown only in the phone link
-- (https://flowstatemcp.com/review/<handle>?n=<code>), bound to the action 'open_review' and
-- an expiry, and used up when a signed grant carrying it opens the list. Never reused.
CREATE TABLE IF NOT EXISTS review_open_nonces (
    nonce TEXT PRIMARY KEY,
    action TEXT NOT NULL DEFAULT 'open_review',
    created_at TEXT NOT NULL,     -- UTC
    expires_at TEXT NOT NULL,     -- UTC
    used_at TEXT                  -- UTC, when a grant opened the list with it
);
