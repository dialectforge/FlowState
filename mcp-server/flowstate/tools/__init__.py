"""FlowState tools package - re-exports all tool functions."""

# Utilities (internal, but exposed for server.py)
from .utils import get_db, set_db, _compact_dict, _compact_list

# Core project and component tools
from .core import (
    list_projects, create_project, get_project, update_project,
    get_project_context, create_component, list_components, update_component,
    log_change, get_recent_changes, search, get_component_history,
    link_items, find_related, get_cross_references,
    get_project_context_v11, get_project_context_lean
)

# Problem tracking tools
from .problems import (
    log_problem, get_open_problems, log_attempt,
    mark_attempt_outcome, mark_problem_solved, get_problem_tree
)

# Session and conversation tools
from .sessions import (
    start_session, end_session, get_current_session,
    log_conversation, get_conversations, get_sessions,
    get_conversation_history, initialize_session_v13, finalize_session_v13
)

# Todo tools
from .todos import add_todo, update_todo, get_todos

# Learning and skill tools
from .learning import (
    log_learning, get_learnings, learn_skill, get_skills,
    apply_skill, confirm_skill, promote_skills,
    save_state, get_latest_state, get_state_chain, restore_state
)

# Project variables and methods
from .variables import (
    create_project_variable, get_project_variables,
    update_project_variable, delete_project_variable,
    create_project_method, get_project_methods,
    update_project_method, delete_project_method
)

# File attachment tools
from .files import (
    attach_file, get_attachments, remove_attachment, search_file_content
)

# Git operations
from .git_ops import (
    git_init, git_status, git_sync, git_set_remote, git_clone, git_history
)

# Intelligence layer (v1.3)
from .intelligence import (
    register_tool, get_tools, update_tool_stats, get_tool_recommendation,
    log_tool_use, rate_tool_use, get_usage_patterns,
    record_pattern, get_patterns, apply_pattern, confirm_pattern,
    log_metric, get_metrics, get_tuning_suggestions
)

# Story generation
from .story import (
    generate_project_story, generate_problem_journey, generate_architecture_diagram
)


__all__ = [
    # Utilities
    'get_db', 'set_db',
    
    # Core
    'list_projects', 'create_project', 'get_project', 'update_project',
    'get_project_context', 'create_component', 'list_components', 'update_component',
    'log_change', 'get_recent_changes', 'search', 'get_component_history',
    'link_items', 'find_related', 'get_cross_references',
    'get_project_context_v11', 'get_project_context_lean',
    
    # Problems
    'log_problem', 'get_open_problems', 'log_attempt',
    'mark_attempt_outcome', 'mark_problem_solved', 'get_problem_tree',
    
    # Sessions
    'start_session', 'end_session', 'get_current_session',
    'log_conversation', 'get_conversations', 'get_sessions',
    'get_conversation_history', 'initialize_session_v13', 'finalize_session_v13',
    
    # Todos
    'add_todo', 'update_todo', 'get_todos',
    
    # Learning
    'log_learning', 'get_learnings', 'learn_skill', 'get_skills',
    'apply_skill', 'confirm_skill', 'promote_skills',
    'save_state', 'get_latest_state', 'get_state_chain', 'restore_state',
    
    # Variables
    'create_project_variable', 'get_project_variables',
    'update_project_variable', 'delete_project_variable',
    'create_project_method', 'get_project_methods',
    'update_project_method', 'delete_project_method',
    
    # Files
    'attach_file', 'get_attachments', 'remove_attachment', 'search_file_content',
    
    # Git
    'git_init', 'git_status', 'git_sync', 'git_set_remote', 'git_clone', 'git_history',
    
    # Intelligence
    'register_tool', 'get_tools', 'update_tool_stats', 'get_tool_recommendation',
    'log_tool_use', 'rate_tool_use', 'get_usage_patterns',
    'record_pattern', 'get_patterns', 'apply_pattern', 'confirm_pattern',
    'log_metric', 'get_metrics', 'get_tuning_suggestions',
    
    # Story
    'generate_project_story', 'generate_problem_journey', 'generate_architecture_diagram',
]
