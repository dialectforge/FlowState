-- 018: the "since your last visit" line's bookmark (2.4.0). One nullable column on a v2
-- table (as 012 does): the newest run_writes.id when a brief was built, so the next brief
-- counts exactly the saves after it (seconds alone repeat a save made in the same second).
ALTER TABLE run_views ADD COLUMN upto_write INTEGER DEFAULT NULL;
