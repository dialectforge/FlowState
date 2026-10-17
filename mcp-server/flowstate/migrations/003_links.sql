-- 003: cross-project links (v2 Phase D). New tables only; existing tables are not altered.
-- A link joins two things: a project, a component, a record (decision = a conversations
-- row, change, learning, problem, todo) or a URL. Both ends' project ids are stored when
-- the link is made, so briefs can find "what other projects use this" without joins.
-- Removing a link sets removed_at (kept for history / undo).
CREATE TABLE IF NOT EXISTS record_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_type TEXT NOT NULL,      -- project | component | decision | change | learning | problem | todo
    from_id INTEGER NOT NULL,
    from_project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    link_type TEXT NOT NULL,      -- uses | depends_on | part_of | same_as | supersedes | subtask_of | blocks | about | url
    to_type TEXT NOT NULL,        -- as from_type, or 'url'
    to_id INTEGER,                -- NULL for url
    to_project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    url TEXT,                     -- for to_type = 'url'
    url_kind TEXT,                -- github | linear | other
    note TEXT,
    created_by TEXT,              -- MCP client name
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- UTC
    removed_at TEXT,
    removed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_record_links_from ON record_links(from_type, from_id, removed_at);
CREATE INDEX IF NOT EXISTS idx_record_links_to ON record_links(to_type, to_id, removed_at);
CREATE INDEX IF NOT EXISTS idx_record_links_projects ON record_links(from_project_id, to_project_id);

-- When start_here last ran for a project ("changed since you last looked").
CREATE TABLE IF NOT EXISTS project_views (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    last_looked_at TEXT NOT NULL,  -- UTC
    last_client TEXT
);

-- A change or decision on a shared component, noted for each project linked to it.
CREATE TABLE IF NOT EXISTS link_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,   -- who is told
    component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    source_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,   -- who logged it
    record_type TEXT NOT NULL,     -- change | decision
    record_id INTEGER,
    summary TEXT NOT NULL,
    todo_id INTEGER REFERENCES todos(id) ON DELETE SET NULL,  -- the update task (shared by repeats)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- UTC
    seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_link_alerts_unseen ON link_alerts(project_id, seen_at);
