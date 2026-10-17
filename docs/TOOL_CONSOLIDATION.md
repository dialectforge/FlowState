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

