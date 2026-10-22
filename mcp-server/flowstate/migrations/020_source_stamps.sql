-- 020: who set a record's source tag, and when (2.4.0, provenance.py). Additive only: new NULL
-- columns on FlowState's own v2 table record_meta (011), as 012 does; no data changes. Older
-- rows keep NULL, which means "not recorded".
--   source_set_by     the app that set the tag (identity.py's app name), or 'user' when the
--                     user confirmed it themselves (review page, terminal, popup, phone)
--   source_set_model  the AI model name, only when the app supplied it (never guessed)
--   source_set_at     UTC, when the tag was set
ALTER TABLE record_meta ADD COLUMN source_set_by TEXT DEFAULT NULL;
ALTER TABLE record_meta ADD COLUMN source_set_model TEXT DEFAULT NULL;
ALTER TABLE record_meta ADD COLUMN source_set_at TEXT DEFAULT NULL;
