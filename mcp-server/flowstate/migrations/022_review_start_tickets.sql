-- 022: the phone review list's start tickets (2.4.0, approvals.start_nonce, review_phone.py).
-- Additive only: two NULL columns on FlowState's own v2 table review_open_nonces (021), as
-- 012 and 020 do, plus an index. Older rows keep NULL ("made before tickets"): they still
-- count toward the all-users ceiling, never toward one user's hourly limit.
--   sub     the flowstatemcp.com user id from the signed start ticket that made this code
--   ticket  SHA-256 of that ticket's nonce (never the ticket), so one ticket makes one code
ALTER TABLE review_open_nonces ADD COLUMN sub TEXT DEFAULT NULL;
ALTER TABLE review_open_nonces ADD COLUMN ticket TEXT DEFAULT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_open_nonces_ticket
    ON review_open_nonces(ticket) WHERE ticket IS NOT NULL;
