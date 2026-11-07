-- 025: a user confirmation belongs to the words that were approved (2.4.9, provenance.py).
-- Additive only: one NULL column on FlowState's own v2 table record_meta (011).
-- confirmed_hash  sha256 of those words. NULL until FlowState fills it.
-- Confirmations made before this version are filled in when the migration runs, from the
-- words as they are now, so they stay valid until the words change. A later edit of the
-- words clears the confirmation. Older readers ignore the new column.
ALTER TABLE record_meta ADD COLUMN confirmed_hash TEXT DEFAULT NULL;
