"""Learning and skill tracking tools."""

from typing import Optional
from .utils import get_db, _compact_list

def log_learning(
    project_id: int,
    insight: str,
    category: Optional[str] = None,
    context: Optional[str] = None,
    component_id: Optional[int] = None,
    source: str = "experience"
) -> dict:
    """
    Capture a learning/insight.
    
    Args:
        project_id: Which project
        insight: The learning
        category: 'pattern', 'gotcha', 'best_practice', 'tool_tip', 'architecture', 'performance', 'security', 'other'
        context: When/where this applies
        component_id: Optional related component
        source: 'experience', 'documentation', 'conversation', 'error', 'research'
    
    Returns:
        The logged learning
    """
    db = get_db()
    learning = db.log_learning(project_id, insight, category, context, component_id, source)
    
    # Index for search
    search_text = f"{insight} {context or ''} {category or ''}"
    db.index_for_search("learning", learning.id, project_id, search_text)
    
    return learning.model_dump()


def get_learnings(
    project_id: Optional[int] = None,
    category: Optional[str] = None,
    verified_only: bool = False,
    compact: bool = False
) -> list[dict]:
    """
    Get learnings with optional filters.
    
    Args:
        project_id: Filter by project (optional)
        category: Filter by category (optional)
        verified_only: Only return verified learnings
        compact: Return minimal fields to save tokens (default False)
    
    Returns:
        List of learnings
    """
    db = get_db()
    learnings = db.get_learnings(project_id, category, verified_only)
    result = [l.model_dump() for l in learnings]
    return _compact_list(result) if compact else result


def learn_skill(
    skill_type: str,
    skill: str,
    context: str = None,
    project_id: int = None,
    tool_name: str = None,
    source_type: str = None,
    source_session_id: int = None
) -> dict:
    """
    Record a new learned skill.
    
    Skills are behaviors or approaches that Claude learns work well.
    They can be tool-specific, project-specific, or general.
    
    Args:
        skill_type: Type of skill - 'tool_capability', 'user_preference', 
                   'approach', 'gotcha', 'project_specific'
        skill: The skill description (what was learned)
        context: When/where this skill applies
        project_id: Project this skill relates to (None for global)
        tool_name: Tool this skill relates to (if tool-specific)
        source_type: How it was learned - 'default', 'learned', 'user_defined'
        source_session_id: Session where this was learned
    
    Returns:
        The created skill record
    """
    db = get_db()
    return db.learn_skill(
        skill_type=skill_type,
        skill=skill,
        context=context,
        project_id=project_id,
        tool_name=tool_name,
        source_type=source_type,
        source_session_id=source_session_id
    )


def get_skills(
    project_id: int = None,
    skill_type: str = None,
    promoted_only: bool = False,
    tool_name: str = None,
    min_confidence: float = 0.0
) -> list[dict]:
    """
    Get learned skills with optional filters.
    
    Args:
        project_id: Filter to project-specific + global skills
        skill_type: Filter by type
        promoted_only: Only return promoted (proven) skills
        tool_name: Filter by tool
        min_confidence: Minimum confidence threshold
    
    Returns:
        List of skill records
    """
    db = get_db()
    return db.get_skills(
        project_id=project_id,
        skill_type=skill_type,
        promoted_only=promoted_only,
        tool_name=tool_name,
        min_confidence=min_confidence
    )


def apply_skill(skill_id: int, succeeded: bool) -> dict:
    """
    Record that a skill was applied and whether it succeeded.
    
    Updates the skill's confidence score based on outcome.
    
    Args:
        skill_id: ID of the skill that was applied
        succeeded: Whether applying the skill led to success
    
    Returns:
        Updated skill record
    """
    db = get_db()
    return db.apply_skill(skill_id, succeeded)


def confirm_skill(skill_id: int, increment_session: bool = True) -> dict:
    """
    Confirm a skill worked in this session, potentially promoting it.
    
    Skills are promoted when they work across 3+ sessions with 80%+ success.
    
    Args:
        skill_id: ID of the skill to confirm
        increment_session: Whether to count this as a new session for the skill
    
    Returns:
        Updated skill record (may now be promoted)
    """
    db = get_db()
    return db.confirm_skill(skill_id, increment_session)


def promote_skills(min_sessions: int = 3, min_success_rate: float = 0.8) -> list[dict]:
    """
    Promote all skills that meet criteria.
    
    Promoted skills are considered proven and get higher priority.
    
    Args:
        min_sessions: Minimum sessions where skill was used
        min_success_rate: Minimum success rate required
    
    Returns:
        List of newly promoted skills
    """
    db = get_db()
    return db.promote_skills(min_sessions, min_success_rate)


# -------------------- SESSION STATE --------------------


def save_state(
    project_id: int,
    state_type: str,
    focus_summary: str = None,
    active_problem_ids: list = None,
    active_component_ids: list = None,
    pending_decisions: list = None,
    key_facts: list = None,
    previous_state_id: int = None,
    tool_calls_this_session: int = 0,
    estimated_tokens: int = None
) -> dict:
    """
    Save Claude's session state for later restoration.
    
    State types:
    - 'start': Beginning of a session
    - 'checkpoint': Mid-session save point
    - 'handoff': End of session, ready for next session
    - 'end': Final state
    
    Args:
        project_id: Project this state belongs to
        state_type: Type of state checkpoint
        focus_summary: What the session was focused on
        active_problem_ids: IDs of problems being worked on
        active_component_ids: IDs of components being worked on
        pending_decisions: Decisions that need to be made
        key_facts: Important facts from this session
        previous_state_id: ID of the previous state (for chaining)
        tool_calls_this_session: Number of tool calls so far
        estimated_tokens: Estimated token usage
    
    Returns:
        The created state record
    """
    db = get_db()
    return db.save_state(
        project_id=project_id,
        state_type=state_type,
        focus_summary=focus_summary,
        active_problem_ids=active_problem_ids,
        active_component_ids=active_component_ids,
        pending_decisions=pending_decisions,
        key_facts=key_facts,
        previous_state_id=previous_state_id,
        tool_calls_this_session=tool_calls_this_session,
        estimated_tokens=estimated_tokens
    )


def get_latest_state(project_id: int) -> Optional[dict]:
    """
    Get the most recent session state for a project.
    
    Args:
        project_id: Project to get state for
    
    Returns:
        Latest state record or None
    """
    db = get_db()
    return db.get_latest_state(project_id)


def get_state_chain(state_id: int, max_depth: int = 5) -> list[dict]:
    """
    Get chain of previous states for walkback.
    
    Useful for understanding the history of a session.
    
    Args:
        state_id: Starting state ID
        max_depth: Maximum number of states to retrieve
    
    Returns:
        List of states from newest to oldest
    """
    db = get_db()
    return db.get_state_chain(state_id, max_depth)


def restore_state(state_id: int) -> Optional[dict]:
    """
    Get a specific state by ID for restoration.
    
    Args:
        state_id: ID of state to restore
    
    Returns:
        State record or None
    """
    db = get_db()
    return db.restore_state(state_id)


# -------------------- TOOL REGISTRY --------------------
