-- 024: who set each hard rule (2.4.4, data security audit D2). One nullable column on
-- FlowState's own v2 table; existing rows are left as they are (NULL = an app added it
-- without the user's OK, as every version before 2.4.4 allowed).
-- set_by_user: how the user set or approved the rule ("terminal", "review page", "phone",
-- ...). Rules an app asks for now wait in the review queue (kind "rule") until the user
-- approves them there.
ALTER TABLE project_rules ADD COLUMN set_by_user TEXT DEFAULT NULL;
