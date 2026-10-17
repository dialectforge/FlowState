-- 012: who did what (accountability). Additive only: new NULL columns on FlowState's own
-- v2 tables (none of the v1 record tables), no data changes. Older rows keep NULL, which
-- means "not recorded". Older v1.1 / v2 servers ignore the new columns.
--   app              the app's name (from its per-app key / remote token / OAuth client if
--                    it has one, else the MCP client name it sends)
--   model            the AI model name, only when the app supplied it (never guessed)
--   machine          workspace or computer name
--   identity_source  app_key | oauth_client | remote_token | shared_token | self_reported

-- edit history / undo log
ALTER TABLE record_history ADD COLUMN app TEXT DEFAULT NULL;
ALTER TABLE record_history ADD COLUMN model TEXT DEFAULT NULL;
ALTER TABLE record_history ADD COLUMN machine TEXT DEFAULT NULL;
ALTER TABLE record_history ADD COLUMN identity_source TEXT DEFAULT NULL;

-- review queue: who queued an item, and who relayed the approve / reject
ALTER TABLE review_items ADD COLUMN app TEXT DEFAULT NULL;
ALTER TABLE review_items ADD COLUMN model TEXT DEFAULT NULL;
ALTER TABLE review_items ADD COLUMN machine TEXT DEFAULT NULL;
ALTER TABLE review_items ADD COLUMN identity_source TEXT DEFAULT NULL;
ALTER TABLE review_items ADD COLUMN decided_app TEXT DEFAULT NULL;
ALTER TABLE review_items ADD COLUMN decided_model TEXT DEFAULT NULL;
ALTER TABLE review_items ADD COLUMN decided_machine TEXT DEFAULT NULL;
ALTER TABLE review_items ADD COLUMN decided_identity_source TEXT DEFAULT NULL;

-- links: who added and who removed
ALTER TABLE record_links ADD COLUMN app TEXT DEFAULT NULL;
ALTER TABLE record_links ADD COLUMN model TEXT DEFAULT NULL;
ALTER TABLE record_links ADD COLUMN machine TEXT DEFAULT NULL;
ALTER TABLE record_links ADD COLUMN identity_source TEXT DEFAULT NULL;
ALTER TABLE record_links ADD COLUMN removed_app TEXT DEFAULT NULL;
ALTER TABLE record_links ADD COLUMN removed_model TEXT DEFAULT NULL;
ALTER TABLE record_links ADD COLUMN removed_machine TEXT DEFAULT NULL;
ALTER TABLE record_links ADD COLUMN removed_identity_source TEXT DEFAULT NULL;

-- handoff notes: who left and who picked up
ALTER TABLE handoff_notes ADD COLUMN app TEXT DEFAULT NULL;
ALTER TABLE handoff_notes ADD COLUMN model TEXT DEFAULT NULL;
ALTER TABLE handoff_notes ADD COLUMN machine TEXT DEFAULT NULL;
ALTER TABLE handoff_notes ADD COLUMN identity_source TEXT DEFAULT NULL;
ALTER TABLE handoff_notes ADD COLUMN picked_up_app TEXT DEFAULT NULL;
ALTER TABLE handoff_notes ADD COLUMN picked_up_model TEXT DEFAULT NULL;
ALTER TABLE handoff_notes ADD COLUMN picked_up_machine TEXT DEFAULT NULL;
ALTER TABLE handoff_notes ADD COLUMN picked_up_identity_source TEXT DEFAULT NULL;

-- hard rules: who added and who removed
ALTER TABLE project_rules ADD COLUMN app TEXT DEFAULT NULL;
ALTER TABLE project_rules ADD COLUMN model TEXT DEFAULT NULL;
ALTER TABLE project_rules ADD COLUMN machine TEXT DEFAULT NULL;
ALTER TABLE project_rules ADD COLUMN identity_source TEXT DEFAULT NULL;
ALTER TABLE project_rules ADD COLUMN removed_app TEXT DEFAULT NULL;
ALTER TABLE project_rules ADD COLUMN removed_model TEXT DEFAULT NULL;
ALTER TABLE project_rules ADD COLUMN removed_machine TEXT DEFAULT NULL;
ALTER TABLE project_rules ADD COLUMN removed_identity_source TEXT DEFAULT NULL;

-- review mode changes
ALTER TABLE review_mode_log ADD COLUMN app TEXT DEFAULT NULL;
ALTER TABLE review_mode_log ADD COLUMN model TEXT DEFAULT NULL;
ALTER TABLE review_mode_log ADD COLUMN machine TEXT DEFAULT NULL;
ALTER TABLE review_mode_log ADD COLUMN identity_source TEXT DEFAULT NULL;

-- per-app activity log (it already has app); record_type / record_id = the record a write
-- created, when there is one (ids only, never text)
ALTER TABLE activity_log ADD COLUMN model TEXT DEFAULT NULL;
ALTER TABLE activity_log ADD COLUMN machine TEXT DEFAULT NULL;
ALTER TABLE activity_log ADD COLUMN identity_source TEXT DEFAULT NULL;
ALTER TABLE activity_log ADD COLUMN record_type TEXT DEFAULT NULL;
ALTER TABLE activity_log ADD COLUMN record_id INTEGER DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_record_history_at ON record_history(created_at);
CREATE INDEX IF NOT EXISTS idx_activity_record ON activity_log(record_type, at);
