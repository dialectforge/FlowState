"""Intelligence layer tools (v1.3) - tool tracking, patterns, metrics."""

from .utils import get_db

def register_tool(
    mcp_server: str,
    tool_name: str,
    effective_for: list = None,
    common_parameters: dict = None,
    gotchas: list = None,
    pairs_with: list = None
) -> dict:
    """
    Register or update a tool in the registry.
    
    The tool registry tracks available tools and their effectiveness.
    
    Args:
        mcp_server: Name of the MCP server providing this tool
        tool_name: Name of the tool
        effective_for: List of task types this tool is good for
        common_parameters: Common parameter patterns that work well
        gotchas: Known issues or pitfalls with this tool
        pairs_with: Tools that work well in combination with this one
    
    Returns:
        The tool registry record
    """
    db = get_db()
    return db.register_tool(
        mcp_server=mcp_server,
        tool_name=tool_name,
        effective_for=effective_for,
        common_parameters=common_parameters,
        gotchas=gotchas,
        pairs_with=pairs_with
    )


def get_tools(
    mcp_server: str = None,
    min_success_rate: float = 0.0
) -> list[dict]:
    """
    Get tools from registry with optional filters.
    
    Args:
        mcp_server: Filter by MCP server
        min_success_rate: Minimum success rate threshold
    
    Returns:
        List of tool registry records
    """
    db = get_db()
    return db.get_tools(mcp_server=mcp_server, min_success_rate=min_success_rate)


def update_tool_stats(mcp_server: str, tool_name: str, succeeded: bool) -> dict:
    """
    Update usage stats for a tool after use.
    
    Args:
        mcp_server: Name of the MCP server
        tool_name: Name of the tool
        succeeded: Whether the tool use was successful
    
    Returns:
        Updated tool registry record
    """
    db = get_db()
    return db.update_tool_stats(mcp_server, tool_name, succeeded)


def get_tool_recommendation(task_type: str, project_id: int = None) -> list[dict]:
    """
    Get recommended tools for a task type based on past success.
    
    Args:
        task_type: Type of task (e.g., 'file_creation', 'search', 'debugging')
        project_id: Optional project context
    
    Returns:
        List of recommended tools with their stats
    """
    db = get_db()
    return db.get_tool_recommendation(task_type, project_id)


# -------------------- TOOL USAGE LOGGING --------------------


def log_tool_use(
    mcp_server: str,
    tool_name: str,
    project_id: int = None,
    session_state_id: int = None,
    parameters: dict = None,
    result_size: int = None,
    execution_time_ms: int = None,
    task_type: str = None,
    trigger: str = None,
    preceding_tool_id: int = None
) -> dict:
    """
    Log a tool usage for pattern analysis.
    
    Args:
        mcp_server: Name of the MCP server
        tool_name: Name of the tool used
        project_id: Project context
        session_state_id: Current session state
        parameters: Parameters passed to the tool
        result_size: Size of the result (for tracking)
        execution_time_ms: How long the tool took
        task_type: What kind of task this was for
        trigger: What triggered this tool use
        preceding_tool_id: ID of the previous tool use (for sequences)
    
    Returns:
        The tool usage record
    """
    db = get_db()
    return db.log_tool_use(
        mcp_server=mcp_server,
        tool_name=tool_name,
        project_id=project_id,
        session_state_id=session_state_id,
        parameters=parameters,
        result_size=result_size,
        execution_time_ms=execution_time_ms,
        task_type=task_type,
        trigger=trigger,
        preceding_tool_id=preceding_tool_id
    )


def rate_tool_use(usage_id: int, was_useful: bool, user_correction: str = None) -> dict:
    """
    Rate a tool usage as useful or not.
    
    This feedback improves tool recommendations over time.
    
    Args:
        usage_id: ID of the tool usage to rate
        was_useful: Whether the tool use was helpful
        user_correction: Optional correction or feedback
    
    Returns:
        Updated tool usage record
    """
    db = get_db()
    return db.rate_tool_use(usage_id, was_useful, user_correction)


def get_usage_patterns(
    project_id: int = None,
    mcp_server: str = None,
    tool_name: str = None,
    limit: int = 100
) -> list[dict]:
    """
    Get tool usage patterns for analysis.
    
    Args:
        project_id: Filter by project
        mcp_server: Filter by MCP server
        tool_name: Filter by specific tool
        limit: Maximum records to return
    
    Returns:
        List of tool usage records
    """
    db = get_db()
    return db.get_usage_patterns(
        project_id=project_id,
        mcp_server=mcp_server,
        tool_name=tool_name,
        limit=limit
    )


# -------------------- BEHAVIOR PATTERNS --------------------


def record_pattern(
    pattern_type: str,
    pattern_name: str,
    trigger_conditions: dict = None,
    actions: list = None,
    project_id: int = None,
    source: str = "learned"
) -> dict:
    """
    Record a new behavior pattern.
    
    Patterns are sequences or approaches that work well in certain situations.
    
    Args:
        pattern_type: Type - 'tool_sequence', 'response_style', 
                     'checkpoint_trigger', 'task_approach'
        pattern_name: Descriptive name for the pattern
        trigger_conditions: When this pattern should be applied
        actions: What actions make up this pattern
        project_id: Project this pattern is specific to (None for global)
        source: How pattern was created - 'default', 'learned', 'user_defined'
    
    Returns:
        The created pattern record
    """
    db = get_db()
    return db.record_pattern(
        pattern_type=pattern_type,
        pattern_name=pattern_name,
        trigger_conditions=trigger_conditions,
        actions=actions,
        project_id=project_id,
        source=source
    )


def get_patterns(
    project_id: int = None,
    pattern_type: str = None,
    promoted_only: bool = False,
    min_confidence: float = 0.0
) -> list[dict]:
    """
    Get behavior patterns with optional filters.
    
    Args:
        project_id: Filter to project-specific + global patterns
        pattern_type: Filter by type
        promoted_only: Only return promoted (proven) patterns
        min_confidence: Minimum confidence threshold
    
    Returns:
        List of pattern records
    """
    db = get_db()
    return db.get_patterns(
        project_id=project_id,
        pattern_type=pattern_type,
        promoted_only=promoted_only,
        min_confidence=min_confidence
    )


def apply_pattern(pattern_id: int, succeeded: bool) -> dict:
    """
    Record that a pattern was applied and whether it succeeded.
    
    Updates the pattern's confidence score based on outcome.
    
    Args:
        pattern_id: ID of the pattern that was applied
        succeeded: Whether applying the pattern led to success
    
    Returns:
        Updated pattern record
    """
    db = get_db()
    return db.apply_pattern(pattern_id, succeeded)


def confirm_pattern(pattern_id: int, increment_session: bool = True) -> dict:
    """
    Confirm a pattern worked in this session, potentially promoting it.
    
    Patterns are promoted when they work across 3+ sessions with 80%+ success.
    
    Args:
        pattern_id: ID of the pattern to confirm
        increment_session: Whether to count this as a new session for the pattern
    
    Returns:
        Updated pattern record (may now be promoted)
    """
    db = get_db()
    return db.confirm_pattern(pattern_id, increment_session)


# -------------------- ALGORITHM METRICS --------------------


def log_metric(
    metric_type: str,
    context: str = None,
    action_taken: str = None,
    outcome: str = None,
    effectiveness_score: float = None,
    user_feedback: str = None,
    should_adjust: bool = None,
    suggested_adjustment: str = None,
    project_id: int = None,
    session_state_id: int = None
) -> dict:
    """
    Log an algorithm metric for self-improvement tracking.
    
    Metrics track how well Claude's behaviors are working.
    
    Args:
        metric_type: Type - 'checkpoint_timing', 'tool_choice', 
                    'response_quality', 'prediction_accuracy', 'user_satisfaction'
        context: Context where this metric was recorded
        action_taken: What action was taken
        outcome: What the outcome was
        effectiveness_score: 0.0-1.0 score
        user_feedback: Any feedback from the user
        should_adjust: Whether behavior should be adjusted
        suggested_adjustment: What adjustment to make
        project_id: Project context
        session_state_id: Session state context
    
    Returns:
        The created metric record
    """
    db = get_db()
    return db.log_metric(
        metric_type=metric_type,
        context=context,
        action_taken=action_taken,
        outcome=outcome,
        effectiveness_score=effectiveness_score,
        user_feedback=user_feedback,
        should_adjust=should_adjust,
        suggested_adjustment=suggested_adjustment,
        project_id=project_id,
        session_state_id=session_state_id
    )


def get_metrics(
    project_id: int = None,
    metric_type: str = None,
    session_state_id: int = None,
    limit: int = 100
) -> list[dict]:
    """
    Get algorithm metrics with optional filters.
    
    Args:
        project_id: Filter by project
        metric_type: Filter by type
        session_state_id: Filter by session
        limit: Maximum records to return
    
    Returns:
        List of metric records
    """
    db = get_db()
    return db.get_metrics(
        project_id=project_id,
        metric_type=metric_type,
        session_state_id=session_state_id,
        limit=limit
    )


def get_tuning_suggestions(project_id: int = None) -> list[dict]:
    """
    Get suggested tuning adjustments based on metrics.
    
    Returns adjustments that have been suggested multiple times.
    
    Args:
        project_id: Filter by project
    
    Returns:
        List of suggested adjustments with occurrence counts
    """
    db = get_db()
    return db.get_tuning_suggestions(project_id)


# -------------------- v1.3 SESSION MANAGEMENT --------------------
