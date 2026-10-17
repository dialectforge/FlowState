"""Routing of the older (v1.5) tools to their implementations in tools/.

Split out of server.py (v2 Phase D). Phase A-D tools (start_here, log_work, review_*,
link, history, ...) are handled in server.py.
"""

from typing import Any

from . import tools


# ============================================================
# ROUTING (existing tools)
# ============================================================

def route_tool(name: str, args: dict, db_path) -> Any:
    """Route tool calls to implementations."""
    
    # Direct tools (no action routing needed)
    if name == "list_projects":
        return tools.list_projects(args.get("status"))
    
    elif name == "create_project":
        return tools.create_project(args["name"], args.get("description"))
    
    elif name == "update_project":
        return tools.update_project(
            args["project_id"],
            args.get("name"),
            args.get("description"),
            args.get("status")
        )
    
    elif name == "get_context":
        lean = args.get("lean", True)
        if lean:
            return tools.get_project_context_lean(args["project_name"], args.get("hours", 48))
        else:
            return tools.get_project_context(args["project_name"], args.get("hours", 48))
    
    elif name == "create_component":
        return tools.create_component(
            args["project_id"],
            args["name"],
            args.get("description"),
            args.get("parent_component_id")
        )
    
    elif name == "list_components":
        return tools.list_components(args["project_id"])
    
    elif name == "update_component":
        return tools.update_component(
            args["component_id"],
            args.get("name"),
            args.get("description"),
            args.get("status")
        )
    
    elif name == "log_learning":
        return tools.log_learning(
            args["project_id"],
            args["insight"],
            args.get("category"),
            args.get("context"),
            args.get("component_id"),
            args.get("source", "experience")
        )
    
    elif name == "get_learnings":
        return tools.get_learnings(
            args.get("project_id"),
            args.get("category"),
            args.get("verified_only", False),
            args.get("compact", False)
        )
    
    elif name == "log_change":
        return tools.log_change(
            args["component_id"],
            args["field_name"],
            args.get("old_value"),
            args.get("new_value"),
            args.get("change_type"),
            args.get("reason")
        )
    
    elif name == "get_recent_changes":
        return tools.get_recent_changes(
            args.get("project_id"),
            args.get("component_id"),
            args.get("hours", 24),
            args.get("compact", False)
        )
    
    elif name == "search":
        from . import meaning
        return meaning.search(db_path, args["query"], args.get("project_id"),
                              args.get("content_types"), args.get("limit", 10),
                              args.get("mode") or "auto")
    
    # ----------------------------------------
    # CONSOLIDATED TOOL ROUTING
    # ----------------------------------------
    
    elif name == "problem":
        action = args["action"]
        if action == "log":
            return tools.log_problem(
                args["component_id"],
                args["title"],
                args.get("description"),
                args.get("severity", "medium")
            )
        elif action == "list":
            return tools.get_open_problems(
                args.get("project_id"),
                args.get("component_id"),
                args.get("compact", False)
            )
        elif action == "solve":
            return tools.mark_problem_solved(
                args["problem_id"],
                args.get("winning_attempt_id"),
                args["summary"],
                args.get("key_insight"),
                args.get("code_snippet")
            )
        elif action == "get_tree":
            return tools.get_problem_tree(args["problem_id"])
    
    elif name == "attempt":
        action = args["action"]
        if action == "log":
            return tools.log_attempt(
                args["problem_id"],
                args["description"],
                args.get("parent_attempt_id")
            )
        elif action == "outcome":
            return tools.mark_attempt_outcome(
                args["attempt_id"],
                args["outcome"],
                args.get("notes"),
                args.get("confidence", "attempted")
            )
    
    elif name == "todo":
        action = args["action"]
        if action == "add":
            return tools.add_todo(
                args["project_id"],
                args["title"],
                args.get("description"),
                args.get("priority", "medium"),
                args.get("component_id"),
                args.get("due_date")
            )
        elif action == "update":
            return tools.update_todo(
                args["todo_id"],
                args.get("status"),
                args.get("priority"),
                args.get("title"),
                args.get("description")
            )
        elif action == "list":
            return tools.get_todos(
                args["project_id"],
                args.get("status"),
                args.get("priority"),
                args.get("compact", False)
            )
    
    elif name == "session":
        action = args["action"]
        if action == "start":
            return tools.start_session(
                args["project_id"],
                args.get("focus_component_id"),
                args.get("focus_problem_id")
            )
        elif action == "end":
            return tools.end_session(
                args["session_id"],
                args.get("summary"),
                args.get("outcomes")
            )
        elif action == "current":
            return tools.get_current_session(args.get("project_id"))
        elif action == "log_conversation":
            return tools.log_conversation(
                args["project_id"],
                args["user_prompt_summary"],
                args.get("assistant_response_summary"),
                args.get("key_decisions"),
                args.get("session_id")
            )
        elif action == "list_conversations":
            return tools.get_conversations(
                args["project_id"],
                args.get("session_id"),
                args.get("limit", 20)
            )
        elif action == "list_sessions":
            return tools.get_sessions(
                args["project_id"],
                args.get("limit", 20)
            )
    
    elif name == "file":
        action = args["action"]
        if action == "attach":
            return tools.attach_file(
                args["project_id"],
                args["file_path"],
                args.get("component_id"),
                args.get("problem_id"),
                args.get("user_description"),
                args.get("tags"),
                args.get("copy_to_bundle", True)
            )
        elif action == "list":
            return tools.get_attachments(
                args.get("project_id"),
                args.get("component_id"),
                args.get("problem_id")
            )
        elif action == "remove":
            return tools.remove_attachment(
                args["attachment_id"],
                args.get("delete_file", False)
            )
        elif action == "search":
            return tools.search_file_content(
                args["query"],
                args.get("project_id"),
                args.get("file_types"),
                args.get("limit", 10)
            )
    
    elif name == "git":
        action = args["action"]
        if action == "init":
            return tools.git_init()
        elif action == "status":
            return tools.git_status()
        elif action == "sync":
            return tools.git_sync(args.get("commit_message"))
        elif action == "set_remote":
            return tools.git_set_remote(args["remote_url"])
        elif action == "clone":
            return tools.git_clone(args["remote_url"], args.get("local_path"))
        elif action == "history":
            return tools.git_history(args.get("limit", 20))
    
    elif name == "variable":
        action = args["action"]
        if action == "create":
            return tools.create_project_variable(
                project_id=args["project_id"],
                name=args["name"],
                value=args.get("value"),
                description=args.get("description"),
                category=args.get("category", "custom"),
                is_secret=args.get("is_secret", False)
            )
        elif action == "list":
            return tools.get_project_variables(
                args["project_id"],
                args.get("category")
            )
        elif action == "update":
            return tools.update_project_variable(
                variable_id=args["variable_id"],
                name=args.get("name"),
                value=args.get("value"),
                description=args.get("description"),
                category=args.get("category"),
                is_secret=args.get("is_secret")
            )
        elif action == "delete":
            return tools.delete_project_variable(args["variable_id"])
    
    elif name == "method":
        action = args["action"]
        if action == "create":
            return tools.create_project_method(
                args["project_id"],
                args["name"],
                args["description"],
                args.get("category"),
                args.get("steps"),
                args.get("code_example"),
                args.get("related_component_id")
            )
        elif action == "list":
            return tools.get_project_methods(
                args["project_id"],
                args.get("category")
            )
        elif action == "update":
            return tools.update_project_method(
                args["method_id"],
                args.get("name"),
                args.get("description"),
                args.get("category"),
                args.get("steps"),
                args.get("code_example"),
                args.get("related_component_id")
            )
        elif action == "delete":
            return tools.delete_project_method(args["method_id"])
    
    elif name == "self_improve":
        action = args["action"]
        if action == "learn_skill":
            return tools.learn_skill(
                skill=args["skill"],
                skill_type=args["skill_type"],
                context=args.get("context"),
                tool_name=args.get("tool_name"),
                project_id=args.get("project_id"),
                source_session_id=args.get("source_session_id"),
                source_type=args.get("source_type", "learned")
            )
        elif action == "get_skills":
            return tools.get_skills(
                project_id=args.get("project_id"),
                skill_type=args.get("skill_type"),
                tool_name=args.get("tool_name"),
                min_confidence=args.get("min_confidence", 0),
                promoted_only=args.get("promoted_only", False)
            )
        elif action == "confirm_skill":
            return tools.confirm_skill(args["skill_id"], args.get("increment_session", True))
        elif action == "save_state":
            return tools.save_state(
                project_id=args["project_id"],
                state_type="checkpoint",
                focus_summary=args.get("focus_summary"),
                key_facts=args.get("key_facts"),
                pending_decisions=args.get("pending_decisions"),
                active_problem_ids=args.get("active_problem_ids"),
                active_component_ids=args.get("active_component_ids"),
                previous_state_id=args.get("previous_state_id"),
                tool_calls_this_session=args.get("tool_calls_this_session", 0),
                estimated_tokens=args.get("estimated_tokens")
            )
        elif action == "get_state":
            return tools.get_latest_state(args["project_id"])
        elif action == "log_metric":
            return tools.log_metric(
                metric_type=args["metric_type"],
                project_id=args.get("project_id"),
                session_state_id=args.get("session_state_id"),
                context=args.get("context"),
                action_taken=args.get("action_taken"),
                outcome=args.get("outcome"),
                effectiveness_score=args.get("effectiveness_score"),
                should_adjust=args.get("should_adjust"),
                suggested_adjustment=args.get("suggested_adjustment"),
                user_feedback=args.get("user_feedback")
            )
        elif action == "get_metrics":
            return tools.get_metrics(
                args.get("project_id"),
                args.get("metric_type"),
                args.get("session_state_id"),
                args.get("limit", 100)
            )
    
    else:
        return {"error": f"Unknown tool: {name}"}
