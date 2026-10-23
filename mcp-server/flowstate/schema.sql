-- ============================================================
-- FLOWSTATE DATABASE SCHEMA v1.3
-- ============================================================
-- Updated: January 28, 2026
-- Changes v1.1: Added file attachments, content locations, extractions,
--          sync metadata, and settings tables
-- Changes v1.2: Added project_variables, project_methods tables
-- Changes v1.3: Added Intelligence Layer - learned_skills, session_state,
--          tool_registry, tool_usage, behavior_patterns, algorithm_metrics

-- Projects: Top-level containers
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'paused', 'completed', 'archived')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Components: Building blocks within projects (nestable)
CREATE TABLE IF NOT EXISTS components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_component_id INTEGER REFERENCES components(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'in_progress' CHECK(status IN ('planning', 'in_progress', 'testing', 'complete', 'deprecated')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, name)
);

-- Changes: Track what changed over time
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    change_type TEXT CHECK(change_type IN ('config', 'code', 'architecture', 'dependency', 'documentation', 'other')),
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Problems: Issues encountered
CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'open' CHECK(status IN ('open', 'investigating', 'blocked', 'solved', 'wont_fix')),
    severity TEXT DEFAULT 'medium' CHECK(severity IN ('low', 'medium', 'high', 'critical')),
    root_cause TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    solved_at TIMESTAMP
);

-- Solution Attempts: The decision tree of what we tried
CREATE TABLE IF NOT EXISTS solution_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    parent_attempt_id INTEGER REFERENCES solution_attempts(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    outcome TEXT CHECK(outcome IN ('success', 'failure', 'partial', 'abandoned', 'pending')),
    confidence TEXT DEFAULT 'attempted' CHECK(confidence IN ('attempted', 'worked_once', 'verified', 'proven', 'deprecated')),
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Solutions: The winning solution for a problem
CREATE TABLE IF NOT EXISTS solutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER UNIQUE NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    winning_attempt_id INTEGER REFERENCES solution_attempts(id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    code_snippet TEXT,
    key_insight TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Todos: Task tracking
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT,
    priority TEXT DEFAULT 'medium' CHECK(priority IN ('low', 'medium', 'high', 'critical')),
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'blocked', 'done', 'cancelled')),
    due_date TIMESTAMP,
    blocked_by_problem_id INTEGER REFERENCES problems(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- Conversations: Log of Claude interactions
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT,
    user_prompt_summary TEXT NOT NULL,
    assistant_response_summary TEXT,
    key_decisions TEXT, -- JSON array: ["decided X", "rejected Y"]
    problems_referenced TEXT, -- JSON array of problem IDs
    solutions_created TEXT, -- JSON array of solution IDs
    tokens_used INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Learnings: Insights and patterns
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    category TEXT CHECK(category IN ('pattern', 'gotcha', 'best_practice', 'tool_tip', 'architecture', 'performance', 'security', 'other')),
    insight TEXT NOT NULL,
    context TEXT,
    source TEXT CHECK(source IN ('experience', 'documentation', 'conversation', 'error', 'research')),
    verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cross References: Links between items across projects
CREATE TABLE IF NOT EXISTS cross_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK(source_type IN ('problem', 'solution', 'learning', 'component', 'change')),
    source_id INTEGER NOT NULL,
    target_project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK(target_type IN ('problem', 'solution', 'learning', 'component', 'change')),
    target_id INTEGER NOT NULL,
    relationship TEXT NOT NULL CHECK(relationship IN ('similar_to', 'derived_from', 'contradicts', 'depends_on', 'supersedes', 'related_to')),
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sessions: Track work sessions
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    focus_component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    focus_problem_id INTEGER REFERENCES problems(id) ON DELETE SET NULL,
    summary TEXT,
    outcomes TEXT, -- JSON
    duration_minutes INTEGER
);

-- ============================================================
-- v1.1 ADDITIONS: FILE ATTACHMENTS
-- ============================================================

-- Attachments: Files attached to projects/components/problems
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    problem_id INTEGER REFERENCES problems(id) ON DELETE SET NULL,
    
    -- File location
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL, -- Relative path within project bundle OR absolute external path
    file_type TEXT NOT NULL, -- pdf, png, jpg, swift, py, md, etc.
    file_size INTEGER, -- Bytes
    file_hash TEXT, -- SHA256 for change detection
    is_external BOOLEAN DEFAULT FALSE, -- TRUE = points to external location, FALSE = in project bundle
    
    -- User metadata
    user_description TEXT,
    tags TEXT, -- JSON array: ["spec", "architecture", "v2"]
    
    -- AI-generated metadata
    ai_description TEXT,
    ai_summary TEXT, -- Longer summary for complex docs
    content_extracted BOOLEAN DEFAULT FALSE, -- TRUE if full extraction was performed
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    indexed_at TIMESTAMP -- When AI last processed this file
);

-- Content Locations: Where key info lives in files
CREATE TABLE IF NOT EXISTS content_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    
    -- What this location contains
    description TEXT NOT NULL, -- "Protocol diagram", "Auth flow pseudocode"
    category TEXT CHECK(category IN ('diagram', 'code', 'spec', 'decision', 'data', 'reference', 'other')),
    
    -- Where in the file
    location_type TEXT NOT NULL CHECK(location_type IN ('page', 'line', 'timestamp', 'section', 'range')),
    start_location TEXT NOT NULL, -- "12" (page), "145" (line), "1:32:05" (timestamp)
    end_location TEXT, -- Optional end for ranges
    
    -- Optional: extracted content snippet
    snippet TEXT, -- Actual text/content if small enough
    
    -- Links to FlowState records this location relates to
    related_problem_id INTEGER REFERENCES problems(id) ON DELETE SET NULL,
    related_solution_id INTEGER REFERENCES solutions(id) ON DELETE SET NULL,
    related_learning_id INTEGER REFERENCES learnings(id) ON DELETE SET NULL,
    related_component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Extractions: Track what was extracted from files
CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    
    -- What was created from this extraction
    record_type TEXT NOT NULL CHECK(record_type IN ('problem', 'learning', 'todo', 'change', 'component')),
    record_id INTEGER NOT NULL, -- ID in the target table
    
    -- Source location in original file
    source_location TEXT, -- Page/line/section reference
    source_snippet TEXT, -- Original text that was extracted
    
    -- Confidence and review status
    confidence REAL, -- 0.0-1.0 AI confidence score
    user_reviewed BOOLEAN DEFAULT FALSE,
    user_approved BOOLEAN,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- v1.1 ADDITIONS: SYNC METADATA
-- ============================================================

-- Sync Status: Track sync state and history
CREATE TABLE IF NOT EXISTS sync_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name TEXT NOT NULL,
    device_id TEXT NOT NULL, -- UUID for this device
    remote_url TEXT, -- GitHub repo URL
    last_sync_at TIMESTAMP,
    last_sync_commit TEXT, -- Git commit hash
    pending_changes INTEGER DEFAULT 0,
    has_conflicts BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sync History: Log of sync operations
CREATE TABLE IF NOT EXISTS sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('push', 'pull', 'conflict', 'resolve')),
    commit_hash TEXT,
    files_changed INTEGER,
    status TEXT CHECK(status IN ('success', 'failed', 'conflict')),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- v1.1 ADDITIONS: SETTINGS
-- ============================================================

-- Settings: Key-value store for app settings
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL, -- JSON encoded
    category TEXT DEFAULT 'general' CHECK(category IN ('general', 'sync', 'ai', 'appearance', 'shortcuts')),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert default settings
INSERT OR IGNORE INTO settings (key, value, category) VALUES 
    ('auto_sync_enabled', 'true', 'sync'),
    ('auto_sync_interval_minutes', '15', 'sync'),
    ('sync_on_close', 'true', 'sync'),
    ('sync_on_open', 'true', 'sync'),
    ('ai_enabled', 'true', 'ai'),
    ('ai_auto_describe_files', 'true', 'ai'),
    ('ai_suggest_related', 'true', 'ai'),
    ('ai_model', '"claude-sonnet"', 'ai'),
    ('theme', '"system"', 'appearance'),
    ('sidebar_collapsed', 'false', 'appearance'),
    ('data_path', '""', 'general'),
    ('recent_projects', '[]', 'general');

-- ============================================================
-- FULL-TEXT SEARCH
-- ============================================================

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content_type,
    content_id UNINDEXED,
    project_id UNINDEXED,
    searchable_text,
    tokenize='porter'
);

-- ============================================================
-- SEMANTIC SEARCH (sqlite-vec)
-- Note: This requires sqlite-vec extension to be loaded
-- Falls back gracefully if not available
-- ============================================================

-- Embeddings storage table (standard SQL, always created)
-- The actual vector search uses sqlite-vec virtual table
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type TEXT NOT NULL CHECK(content_type IN ('problem', 'solution', 'learning', 'change', 'conversation', 'attachment')),
    content_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    embedding_model TEXT DEFAULT 'all-MiniLM-L6-v2',
    embedding BLOB,  -- Stored as binary for efficiency
    text_hash TEXT,  -- To detect if re-embedding needed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(content_type, content_id)
);

-- ============================================================
-- v1.2 ADDITIONS: PROJECT KNOWLEDGE BASE
-- ============================================================

-- Project Variables: Key-value store for project-specific settings
-- Use for: server IPs, ports, credentials, environment vars, config
CREATE TABLE IF NOT EXISTS project_variables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK(category IN ('server', 'credentials', 'config', 'environment', 'endpoint', 'custom')),
    name TEXT NOT NULL,
    value TEXT,
    is_secret BOOLEAN DEFAULT FALSE,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, category, name)
);

-- Project Methods: Standard approaches, patterns, and processes for this project
-- Use for: auth flows, deployment steps, testing conventions, architecture patterns
CREATE TABLE IF NOT EXISTS project_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT CHECK(category IN ('auth', 'deployment', 'testing', 'architecture', 'workflow', 'convention', 'api', 'security', 'other')),
    steps TEXT,  -- JSON array: ["step 1", "step 2", ...]
    code_example TEXT,  -- Optional code snippet
    related_component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, name)
);

-- ============================================================
-- v1.3 ADDITIONS: INTELLIGENCE LAYER
-- ============================================================
-- The algorithm that makes FlowState work - learning, efficiency, 
-- session continuity without massive prompt files.

-- Learned Skills: What Claude has learned about tools, user, and project
-- Enables: instant context, no re-explaining, knowledge that improves
CREATE TABLE IF NOT EXISTS learned_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_type TEXT NOT NULL CHECK(skill_type IN (
        'tool_capability',     -- What MCP tools can/can't do
        'user_preference',     -- How user likes things done
        'approach',            -- Effective problem-solving approaches
        'gotcha',              -- Things that went wrong, to avoid
        'project_specific'     -- Domain knowledge for a project
    )),
    skill TEXT NOT NULL,           -- The actual skill/knowledge
    context TEXT,                  -- When/where this applies
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = global
    tool_name TEXT,                -- If skill is about a specific tool
    
    -- Learning provenance
    source_type TEXT,              -- 'observation', 'correction', 'explicit', 'inference'
    source_session_id INTEGER,     -- Session where this was learned
    
    -- Confidence and validation
    confidence REAL DEFAULT 0.6,   -- 0.0-1.0, starts at 0.6
    session_count INTEGER DEFAULT 1,    -- Sessions where this applied
    times_applied INTEGER DEFAULT 0,    -- How often used
    times_succeeded INTEGER DEFAULT 0,  -- How often it worked
    
    -- Promotion to CLAUDE_SKILLS.md (permanent knowledge)
    promoted BOOLEAN DEFAULT FALSE,
    promoted_at TIMESTAMP,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Session State: Claude's working memory at checkpoints
-- Enables: session handoffs, context restoration, efficient resumption
CREATE TABLE IF NOT EXISTS session_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    
    -- State type and content
    state_type TEXT NOT NULL CHECK(state_type IN (
        'start',       -- Beginning of session
        'checkpoint',  -- Mid-session checkpoint
        'handoff',     -- Explicit handoff to next session
        'end'          -- Session end
    )),
    
    -- What Claude was focused on
    focus_summary TEXT,           -- Brief: "Implementing v1.3 schema, adding learned_skills table"
    active_problem_ids TEXT,      -- JSON: [34, 35]
    active_component_ids TEXT,    -- JSON: [30, 1]
    
    -- What needs to continue
    pending_decisions TEXT,       -- JSON: ["Whether to use BLOB or TEXT for embeddings"]
    key_facts TEXT,               -- JSON: ["User prefers explicit over implicit", "Project uses SQLite WAL mode"]
    
    -- Chain for history
    previous_state_id INTEGER REFERENCES session_state(id),
    
    -- Metrics
    tool_calls_this_session INTEGER DEFAULT 0,
    estimated_tokens INTEGER,     -- Approximate context size at this point
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tool Registry: Knowledge about available MCP tools
-- Enables: smart tool selection, effective chaining, avoiding pitfalls
CREATE TABLE IF NOT EXISTS tool_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mcp_server TEXT NOT NULL,      -- 'flowstate', 'desktop_commander', etc.
    tool_name TEXT NOT NULL,       -- 'get_project_context', 'read_file', etc.
    
    -- Learned capabilities
    effective_for TEXT,            -- JSON: ["reading files", "checking status"]
    common_parameters TEXT,        -- JSON: {"path": "usually absolute"}
    gotchas TEXT,                  -- JSON: ["Fails silently on missing files"]
    pairs_with TEXT,               -- JSON: ["write_file", "list_directory"]
    
    -- Usage stats
    times_used INTEGER DEFAULT 0,
    times_succeeded INTEGER DEFAULT 0,
    success_rate REAL GENERATED ALWAYS AS (
        CASE WHEN times_used > 0 THEN CAST(times_succeeded AS REAL) / times_used ELSE 0 END
    ) STORED,
    last_used TIMESTAMP,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(mcp_server, tool_name)
);

-- Tool Usage: Log of actual tool calls
-- Enables: pattern analysis, sequence optimization, learning from mistakes
CREATE TABLE IF NOT EXISTS tool_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    session_state_id INTEGER REFERENCES session_state(id) ON DELETE SET NULL,
    tool_registry_id INTEGER REFERENCES tool_registry(id) ON DELETE SET NULL,
    
    -- What was called
    mcp_server TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    parameters TEXT,               -- JSON of actual parameters used
    
    -- Results
    result_size INTEGER,           -- Bytes of response
    execution_time_ms INTEGER,
    
    -- Context
    task_type TEXT,                -- 'read', 'write', 'search', 'create', etc.
    trigger TEXT,                  -- 'user_request', 'chained', 'automatic'
    preceding_tool_id INTEGER REFERENCES tool_usage(id),  -- For sequence analysis
    
    -- Feedback
    was_useful BOOLEAN,            -- Did this actually help?
    user_correction TEXT,          -- If user corrected, what did they say?
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Behavior Patterns: Sequences and approaches that work well
-- Enables: efficient workflows, automatic chaining, proven approaches
CREATE TABLE IF NOT EXISTS behavior_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = global
    
    pattern_type TEXT NOT NULL CHECK(pattern_type IN (
        'tool_sequence',       -- Sequence of tool calls that work well together
        'response_style',      -- How to format responses for this user/project
        'checkpoint_trigger',  -- When to save state
        'task_approach'        -- General approach for task types
    )),
    
    pattern_name TEXT NOT NULL,    -- Human-readable name
    trigger_conditions TEXT,       -- JSON: {"task_type": "file_modification", "complexity": "high"}
    actions TEXT,                  -- JSON: ["read_file first", "create backup", "then modify"]
    
    -- Validation
    times_used INTEGER DEFAULT 0,
    times_succeeded INTEGER DEFAULT 0,
    success_rate REAL GENERATED ALWAYS AS (
        CASE WHEN times_used > 0 THEN CAST(times_succeeded AS REAL) / times_used ELSE 0 END
    ) STORED,
    
    -- Learning
    source TEXT CHECK(source IN ('default', 'learned', 'user_defined')),
    confidence REAL DEFAULT 0.5,
    session_count INTEGER DEFAULT 1,
    
    -- Promotion
    promoted BOOLEAN DEFAULT FALSE,
    promoted_at TIMESTAMP,
    
    last_used TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Algorithm Metrics: Self-tuning feedback loop
-- Enables: measuring effectiveness, adjusting thresholds, continuous improvement
CREATE TABLE IF NOT EXISTS algorithm_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    session_state_id INTEGER REFERENCES session_state(id) ON DELETE SET NULL,
    
    metric_type TEXT NOT NULL CHECK(metric_type IN (
        'checkpoint_timing',    -- Was checkpoint too early/late?
        'tool_choice',          -- Was the right tool selected?
        'response_quality',     -- Did response meet user needs?
        'prediction_accuracy',  -- Did learned patterns apply correctly?
        'skill_application',    -- Did applying a skill help?
        'promotion'             -- Was promotion to CLAUDE_SKILLS warranted?
    )),
    
    -- What happened
    context TEXT,                  -- What was being done
    action_taken TEXT,             -- What did the algorithm do
    outcome TEXT,                  -- What happened as a result
    
    -- Evaluation
    effectiveness_score REAL,      -- 0.0-1.0
    user_feedback TEXT,            -- If user commented
    
    -- Learning
    should_adjust BOOLEAN,         -- Should algorithm parameters change?
    suggested_adjustment TEXT,     -- What to adjust: {"checkpoint_interval": "+5 calls"}
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- INDEXES FOR PERFORMANCE
-- ============================================================

-- Existing indexes (v1.0)
CREATE INDEX IF NOT EXISTS idx_components_project ON components(project_id);
CREATE INDEX IF NOT EXISTS idx_changes_component ON changes(component_id);
CREATE INDEX IF NOT EXISTS idx_changes_created ON changes(created_at);
CREATE INDEX IF NOT EXISTS idx_problems_component ON problems(component_id);
CREATE INDEX IF NOT EXISTS idx_problems_status ON problems(status);
CREATE INDEX IF NOT EXISTS idx_attempts_problem ON solution_attempts(problem_id);
CREATE INDEX IF NOT EXISTS idx_todos_project ON todos(project_id);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_id);
CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_learnings_project ON learnings(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_project ON embeddings(project_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_type ON embeddings(content_type);

-- v1.1 indexes
CREATE INDEX IF NOT EXISTS idx_attachments_project ON attachments(project_id);
CREATE INDEX IF NOT EXISTS idx_attachments_component ON attachments(component_id);
CREATE INDEX IF NOT EXISTS idx_attachments_problem ON attachments(problem_id);
CREATE INDEX IF NOT EXISTS idx_attachments_type ON attachments(file_type);
CREATE INDEX IF NOT EXISTS idx_content_locations_attachment ON content_locations(attachment_id);
CREATE INDEX IF NOT EXISTS idx_extractions_attachment ON extractions(attachment_id);
CREATE INDEX IF NOT EXISTS idx_sync_history_device ON sync_history(device_id);

-- v1.2 indexes
CREATE INDEX IF NOT EXISTS idx_project_variables_project ON project_variables(project_id);
CREATE INDEX IF NOT EXISTS idx_project_variables_category ON project_variables(category);
CREATE INDEX IF NOT EXISTS idx_project_methods_project ON project_methods(project_id);
CREATE INDEX IF NOT EXISTS idx_project_methods_category ON project_methods(category);

-- v1.3 indexes
CREATE INDEX IF NOT EXISTS idx_skills_type ON learned_skills(skill_type);
CREATE INDEX IF NOT EXISTS idx_skills_project ON learned_skills(project_id);
CREATE INDEX IF NOT EXISTS idx_skills_promoted ON learned_skills(promoted);
CREATE INDEX IF NOT EXISTS idx_skills_tool ON learned_skills(tool_name);
CREATE INDEX IF NOT EXISTS idx_state_project ON session_state(project_id);
CREATE INDEX IF NOT EXISTS idx_state_type ON session_state(state_type);
CREATE INDEX IF NOT EXISTS idx_state_created ON session_state(created_at);
CREATE INDEX IF NOT EXISTS idx_registry_server ON tool_registry(mcp_server);
CREATE INDEX IF NOT EXISTS idx_registry_tool ON tool_registry(tool_name);
CREATE INDEX IF NOT EXISTS idx_usage_project ON tool_usage(project_id);
CREATE INDEX IF NOT EXISTS idx_usage_session ON tool_usage(session_state_id);
CREATE INDEX IF NOT EXISTS idx_usage_tool ON tool_usage(tool_registry_id);
CREATE INDEX IF NOT EXISTS idx_usage_created ON tool_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_patterns_project ON behavior_patterns(project_id);
CREATE INDEX IF NOT EXISTS idx_patterns_type ON behavior_patterns(pattern_type);
CREATE INDEX IF NOT EXISTS idx_patterns_promoted ON behavior_patterns(promoted);
CREATE INDEX IF NOT EXISTS idx_metrics_project ON algorithm_metrics(project_id);
CREATE INDEX IF NOT EXISTS idx_metrics_type ON algorithm_metrics(metric_type);
CREATE INDEX IF NOT EXISTS idx_metrics_session ON algorithm_metrics(session_state_id);

-- ============================================================
-- TRIGGERS FOR AUTO-UPDATE
-- ============================================================

CREATE TRIGGER IF NOT EXISTS update_project_timestamp 
AFTER UPDATE ON projects
BEGIN
    UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS update_component_timestamp 
AFTER UPDATE ON components
BEGIN
    UPDATE components SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS set_problem_solved_at
AFTER UPDATE OF status ON problems
WHEN NEW.status = 'solved' AND OLD.status != 'solved'
BEGIN
    UPDATE problems SET solved_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS set_todo_completed_at
AFTER UPDATE OF status ON todos
WHEN NEW.status = 'done' AND OLD.status != 'done'
BEGIN
    UPDATE todos SET completed_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

-- v1.1 triggers
CREATE TRIGGER IF NOT EXISTS update_attachment_timestamp 
AFTER UPDATE ON attachments
BEGIN
    UPDATE attachments SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS update_sync_status_timestamp 
AFTER UPDATE ON sync_status
BEGIN
    UPDATE sync_status SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS update_settings_timestamp 
AFTER UPDATE ON settings
BEGIN
    UPDATE settings SET updated_at = CURRENT_TIMESTAMP WHERE key = NEW.key;
END;

-- v1.2 triggers
CREATE TRIGGER IF NOT EXISTS update_project_variables_timestamp 
AFTER UPDATE ON project_variables
BEGIN
    UPDATE project_variables SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS update_project_methods_timestamp 
AFTER UPDATE ON project_methods
BEGIN
    UPDATE project_methods SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

-- v1.3 triggers
CREATE TRIGGER IF NOT EXISTS update_learned_skills_timestamp 
AFTER UPDATE ON learned_skills
BEGIN
    UPDATE learned_skills SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS update_tool_registry_timestamp 
AFTER UPDATE ON tool_registry
BEGIN
    UPDATE tool_registry SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS update_behavior_patterns_timestamp 
AFTER UPDATE ON behavior_patterns
BEGIN
    UPDATE behavior_patterns SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

-- ============================================================
-- v2 PHASE A: IMPLICIT (AUTO) SESSIONS
-- ============================================================
-- Newer v2 tables are NOT added here: they come from the numbered migrations in
-- mcp-server/flowstate/migrations/ (applied on server start, by setup_flowstate.py and
-- by `flowstate migrate`). This block equals migration 001.

-- One row per FlowState session the server opened on its own, keyed by MCP client
-- name + project. Closed after an idle timeout, on server shutdown, or explicitly.
-- Additive only: nothing existing is altered.
CREATE TABLE IF NOT EXISTS auto_sessions (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    client TEXT NOT NULL,
    server_instance TEXT NOT NULL,
    opened_at TEXT NOT NULL,      -- UTC, 'YYYY-MM-DD HH:MM:SS'
    last_activity TEXT NOT NULL,  -- UTC
    call_count INTEGER NOT NULL DEFAULT 0,
    events TEXT NOT NULL DEFAULT '[]', -- JSON list of short event labels (capped)
    closed_at TEXT,
    close_reason TEXT             -- idle | shutdown | stale | explicit
);
CREATE INDEX IF NOT EXISTS idx_auto_sessions_open ON auto_sessions(closed_at, client, project_id);
