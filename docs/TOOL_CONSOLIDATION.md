# FlowState v1.5 - Consolidated Tools
# Reduces 79 tools → ~21 tools while preserving all functionality

## Tool Consolidation Map

### KEEP AS-IS (Core tools - 12 tools)
These are already optimal single-purpose tools:

1. `list_projects` - List projects
2. `create_project` - Create project  
3. `update_project` - Update project
4. `get_context` - Lean context (merge get_project_context + get_project_context_lean)
5. `create_component` - Create component
6. `list_components` - List components
7. `update_component` - Update component
8. `log_learning` - Capture insights
9. `get_learnings` - Retrieve learnings
10. `search` - Search all content
11. `log_change` - Log changes
12. `get_recent_changes` - Get changes

### CONSOLIDATE (Action-based tools - 9 tools)

13. `problem` - Actions: log, list, solve, get_tree
    - Replaces: log_problem, get_open_problems, mark_problem_solved, get_problem_tree

14. `attempt` - Actions: log, outcome
    - Replaces: log_attempt, mark_attempt_outcome

15. `todo` - Actions: add, update, list
    - Replaces: add_todo, update_todo, get_todos

16. `session` - Actions: start, end, current, log_conversation, get_conversations, get_history
    - Replaces: start_session, end_session, get_current_session, log_conversation, get_conversations, get_sessions, get_conversation_history

17. `file` - Actions: attach, list, remove, search
    - Replaces: attach_file, get_attachments, remove_attachment, search_file_content

18. `git` - Actions: init, status, sync, set_remote, clone, history
    - Replaces: git_init, git_status, git_sync, git_set_remote, git_clone, git_history

19. `variable` - Actions: create, list, update, delete
    - Replaces: create_project_variable, get_project_variables, update_project_variable, delete_project_variable

20. `method` - Actions: create, list, update, delete
    - Replaces: create_project_method, get_project_methods, update_project_method, delete_project_method

21. `self_improve` - Actions: learn_skill, get_skills, confirm_skill, save_state, get_state, log_metric, get_metrics
    - Replaces: learn_skill, get_skills, apply_skill, confirm_skill, promote_skills, save_state, get_latest_state, get_state_chain, restore_state, log_metric, get_metrics, get_tuning_suggestions

### REMOVE (Redundant/over-engineered - 0 kept)
- record_pattern, get_patterns, apply_pattern, confirm_pattern (overlaps with skills)
- register_tool, get_tools_registry, update_tool_stats, get_tool_recommendation (over-engineered)
- log_tool_use, rate_tool_use, get_usage_patterns (over-engineered)
- initialize_session_v13, finalize_session_v13 (can use session tool)
- link_items, find_related, get_cross_references (rarely used, can add back if needed)
- get_component_history (can use search)
- generate_project_story, generate_problem_journey, generate_architecture_diagram (visual-story features)

## Final Count: 21 tools


---

# FlowState v2.1 - Public tool surface

v2.0 listed 35 tools (the 21 above plus 14 v2 tools) under three naming styles. v2.1 lists
**12** by default, and every name starts with a verb: `start_here`, `log_work`, `search`,
and `manage_<things>` (with an `action`) for tools that do several things.

**Every v2.0 name still works.** The older names are the internal operation ids
(`mcp-server/flowstate/surface.py` maps each public call onto them), so an older call runs
the same code and returns the same first content block, plus one text block naming the new
tool. They are never listed.

| Profile (`FLOWSTATE_TOOLS`) | Listed |
|---|---|
| unset | the 12 tools below |
| `core` | start_here, log_work, search, manage_todos, manage_review, manage_handoffs |
| `full` (or `all`) | the 12 plus manage_sessions, manage_files, manage_data_git, manage_variables, manage_methods, manage_skills |

## Old -> new

| v2.0 call | v2.1 call |
|---|---|
| `list_projects(status)` | `manage_projects(action="list", status)` or `start_here()` |
| `create_project(name, description)` | `manage_projects(action="create", name, description)` |
| `update_project(project_id, ...)` | `manage_projects(action="update", project, ...)` |
| `get_context(project_name, lean, hours)` | `start_here(project, size="deep")` |
| `create_component(project_id, name, ...)` | `manage_projects(action="add_component", project, name, ...)` |
| `list_components(project_id)` | `manage_projects(action="list_components", project)` |
| `update_component(component_id, ...)` | `manage_projects(action="update_component", project, component_id, ...)` |
| `log_learning(insight, category, context)` | `log_work(kind="learning", summary, category, details)` |
| `get_learnings(project_id)` | `search(content_types=["learning"], project)` (category filter: older name only) |
| `log_change(component_id, field_name, ...)` | `log_work(kind="change", summary, details, component)` |
| `get_recent_changes(project_id, hours)` | `search(content_types=["change"], project, since_hours)` |
| `ask(question, project, limit)` | `search(query=question, answer=true, project, limit)` |
| `problem(action="log")` | `log_work(kind="problem")` |
| `problem(action="list" / "solve" / "get_tree")` | `manage_problems(action="list" / "solve" / "tree")` |
| `attempt(action="log" / "outcome")` | `manage_problems(action="add_attempt" / "end_attempt")` |
| `todo(action=...)` | `manage_todos(action=...)` (same actions) |
| `review_list` / `review_approve` / `review_reject` | `manage_review(action="list" / "approve" / "reject")` |
| `review_mode(action="get" / "set")` | `manage_review(action="get_mode" / "set_mode")` |
| `link`, `rules`, `handoff`, `history` (action=...) | `manage_links`, `manage_rules`, `manage_handoffs`, `manage_history` (same actions) |
| `cleanup_suggest(kinds)` | `manage_memory(action="cleanup", kinds)` |
| `import_memory(text, format, source, kind)` | `manage_memory(action="import", text, format, origin, kind)` |
| `verify(action="stale" / "mark" / "source")` | `manage_memory(action="stale" / "mark_checked" / "set_source")` |
| `session(action=...)` | `manage_sessions(action=...)`: `list_sessions` -> `list`, `list_conversations` -> `conversations`; `log_conversation` -> `log_work(kind="decision")` |
| `file`, `variable`, `method` (action=...) | `manage_files`, `manage_variables`, `manage_methods` (same actions) |
| `git(action=...)` | `manage_data_git(action=...)` |
| `self_improve(action=...)` | `manage_skills`: `learn_skill` -> `learn`, `get_skills` -> `list`, `confirm_skill` -> `confirm`, `get_metrics` -> `list_metrics`, others the same |

`start_here`, `log_work` and `search` kept their names.
