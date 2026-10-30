"""Core project and component tools."""

from typing import Optional
from .utils import get_db, _compact_list
from .files import get_attachments

def list_projects(status: Optional[str] = None) -> list[dict]:
    """
    List all projects with their status and stats.
    
    Args:
        status: Optional filter ('active', 'paused', 'completed', 'archived')
    
    Returns:
        List of projects with basic info
    """
    db = get_db()
    projects = db.list_projects(status)
    return [p.model_dump() for p in projects]


def create_project(name: str, description: Optional[str] = None) -> dict:
    """
    Create a new project.
    
    Args:
        name: Project name (must be unique)
        description: Optional description
    
    Returns:
        The created project
    """
    db = get_db()
    project = db.create_project(name, description)
    return project.model_dump()


def get_project(project_id: int) -> Optional[dict]:
    """
    Get project details by ID.
    
    Args:
        project_id: The project ID
    
    Returns:
        Project details or None if not found
    """
    db = get_db()
    project = db.get_project(project_id)
    return project.model_dump() if project else None


def update_project(
    project_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None
) -> Optional[dict]:
    """
    Update project fields.
    
    Args:
        project_id: The project ID
        name: New name (optional)
        description: New description (optional)
        status: New status (optional)
    
    Returns:
        Updated project or None if not found
    """
    db = get_db()
    project = db.update_project(project_id, name=name, description=description, status=status)
    return project.model_dump() if project else None


def get_project_context(project_name: str, hours: int = 48, include_files: bool = True) -> Optional[dict]:
    """
    THE KILLER TOOL: Get everything needed to work on a project.
    
    Returns:
    - Project details
    - All components
    - Active problems (open, investigating)
    - Recent changes (last 24-48 hours)
    - High-priority todos
    - Recent learnings
    - Current session if any
    - File attachments with content locations (v1.1)
    
    Args:
        project_name: Name of the project
        hours: How many hours back to look for changes (default 48)
        include_files: Include file attachments in context (default True)
    
    Returns:
        Full project context or None if project not found
    """
    db = get_db()
    ctx = db.get_project_context(project_name, hours)
    if ctx is None:
        return None
    
    result = {
        "project": ctx.project.model_dump(),
        "components": [c.model_dump() for c in ctx.components],
        "open_problems": [p.model_dump() for p in ctx.open_problems],
        "recent_changes": [c.model_dump() for c in ctx.recent_changes],
        "high_priority_todos": [t.model_dump() for t in ctx.high_priority_todos],
        "recent_learnings": [l.model_dump() for l in ctx.recent_learnings],
        "current_session": ctx.current_session.model_dump() if ctx.current_session else None
    }
    
    # Add file attachments if requested (v1.1)
    if include_files:
        attachments = get_attachments(project_id=ctx.project.id)
        result["attachments"] = attachments
        
        # Get content locations for attachments
        conn = db._conn()
        try:
            for att in attachments:
                locations = conn.execute(
                    "SELECT * FROM content_locations WHERE attachment_id = ?",
                    (att['id'],)
                ).fetchall()
                att['content_locations'] = [dict(l) for l in locations]
        finally:
            conn.close()
    
    return result


def create_component(
    project_id: int,
    name: str,
    description: Optional[str] = None,
    parent_component_id: Optional[int] = None
) -> dict:
    """
    Create a new component in a project.
    
    Args:
        project_id: The project ID
        name: Component name
        description: Optional description
        parent_component_id: Optional parent for nesting
    
    Returns:
        The created component
    """
    db = get_db()
    component = db.create_component(project_id, name, description, parent_component_id)
    return component.model_dump()


def list_components(project_id: int) -> list[dict]:
    """
    List all components in a project.
    
    Args:
        project_id: The project ID
    
    Returns:
        List of components
    """
    db = get_db()
    components = db.list_components(project_id)
    return [c.model_dump() for c in components]


def update_component(
    component_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None
) -> Optional[dict]:
    """
    Update component fields.
    
    Args:
        component_id: The component ID
        name: New name (optional)
        description: New description (optional)
        status: New status (optional)
    
    Returns:
        Updated component or None
    """
    db = get_db()
    component = db.update_component(component_id, name=name, description=description, status=status)
    return component.model_dump() if component else None


def log_change(
    component_id: int,
    field_name: str,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    change_type: str = "code",
    reason: Optional[str] = None
) -> dict:
    """
    Log a change to a component.
    
    Args:
        component_id: The component that changed
        field_name: What field/aspect changed
        old_value: Previous value
        new_value: New value
        change_type: Type of change ('config', 'code', 'architecture', 'dependency', 'documentation', 'other')
        reason: Why the change was made
    
    Returns:
        The logged change
    """
    db = get_db()
    change = db.log_change(component_id, field_name, old_value, new_value, change_type, reason)
    
    # Index for search
    search_text = f"{field_name} {old_value or ''} {new_value or ''} {reason or ''}"
    component = db.get_component(component_id)
    if component:
        db.index_for_search("change", change.id, component.project_id, search_text)
    
    return change.model_dump()


def get_recent_changes(
    project_id: Optional[int] = None,
    component_id: Optional[int] = None,
    hours: int = 24,
    compact: bool = False
) -> list[dict]:
    """
    Get recent changes.
    
    Args:
        project_id: Filter by project (optional)
        component_id: Filter by component (optional)
        hours: How far back to look (default 24)
        compact: Return minimal fields to save tokens (default False)
    
    Returns:
        List of recent changes
    """
    db = get_db()
    changes = db.get_recent_changes(project_id, component_id, hours)
    result = [c.model_dump() for c in changes]
    return _compact_list(result) if compact else result


def search(
    query: str,
    project_id: Optional[int] = None,
    content_types: Optional[list[str]] = None,
    limit: int = 10
) -> list[dict]:
    """
    Search across all content.
    
    Args:
        query: Search query
        project_id: Optional project filter
        content_types: Optional list of types to search ('problem', 'solution', 'learning', 'change')
        limit: Max results (default 10)
    
    Returns:
        List of search results with snippets
    """
    db = get_db()
    return db.search(query, project_id, content_types, limit)


def get_component_history(
    component_id: int,
    days: int = 30
) -> dict:
    """
    Get full history for a component over time.
    
    Args:
        component_id: The component ID
        days: How many days back to look (default 30)
    
    Returns:
        Component with all changes, problems, solutions, learnings
    """
    db = get_db()
    return db.get_component_history(component_id, days)


def link_items(
    source_type: str,
    source_id: int,
    target_type: str,
    target_id: int,
    relationship: str,
    source_project_id: Optional[int] = None,
    target_project_id: Optional[int] = None,
    notes: Optional[str] = None
) -> dict:
    """
    Create a cross-reference between items.
    
    Args:
        source_type: Type of source item ('problem', 'solution', 'learning', 'component', 'change')
        source_id: ID of source item
        target_type: Type of target item
        target_id: ID of target item
        relationship: How they're related ('similar_to', 'derived_from', 'contradicts', 'depends_on', 'supersedes', 'related_to')
        source_project_id: Project ID of source (auto-detected if not provided)
        target_project_id: Project ID of target (auto-detected if not provided)
        notes: Optional notes about the relationship
    
    Returns:
        The created cross-reference
    """
    db = get_db()
    
    # Auto-detect project IDs if not provided
    if source_project_id is None:
        source_project_id = _get_project_id_for_item(db, source_type, source_id)
    if target_project_id is None:
        target_project_id = _get_project_id_for_item(db, target_type, target_id)
    
    return db.link_items(
        source_project_id, source_type, source_id,
        target_project_id, target_type, target_id,
        relationship, notes
    )


def _get_project_id_for_item(db, item_type: str, item_id: int) -> int:
    """Helper to get project_id for any item type."""
    if item_type == 'component':
        comp = db.get_component(item_id)
        return comp.project_id if comp else 0
    elif item_type == 'problem':
        prob = db.get_problem(item_id)
        if prob:
            comp = db.get_component(prob.component_id)
            return comp.project_id if comp else 0
    elif item_type == 'solution':
        sol = db.get_solution(item_id)
        if sol:
            prob = db.get_problem(sol.problem_id)
            if prob:
                comp = db.get_component(prob.component_id)
                return comp.project_id if comp else 0
    elif item_type == 'learning':
        learnings = db.get_learnings()
        for l in learnings:
            if l.id == item_id:
                return l.project_id
    elif item_type == 'change':
        # Need to trace through component
        pass
    return 0


def find_related(
    item_type: str,
    item_id: int
) -> list[dict]:
    """
    Find all items related to this one.
    
    Args:
        item_type: Type of item ('problem', 'solution', 'learning', 'component', 'change')
        item_id: ID of the item
    
    Returns:
        List of cross-references (both incoming and outgoing)
    """
    db = get_db()
    return db.find_related(item_type, item_id)


def get_cross_references(
    project_id: Optional[int] = None,
    source_type: Optional[str] = None,
    source_id: Optional[int] = None
) -> list[dict]:
    """
    Get cross-references (links between items).
    
    Args:
        project_id: Optional filter by project
        source_type: Optional filter by source type ('problem', 'solution', etc.)
        source_id: Optional filter by specific source item
    
    Returns:
        List of cross-references
    """
    db = get_db()
    conn = db._conn()
    try:
        query = "SELECT * FROM cross_references WHERE 1=1"
        params = []
        if project_id:
            query += " AND (source_project_id = ? OR target_project_id = ?)"
            params.extend([project_id, project_id])
        if source_type:
            query += " AND source_type = ?"
            params.append(source_type)
        if source_id:
            query += " AND source_id = ?"
            params.append(source_id)
        query += " ORDER BY created_at DESC"
        
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_project_context_v11(
    project_name: str, 
    hours: int = 48,
    include_files: bool = True
) -> Optional[dict]:
    """
    v1.1 KILLER TOOL: Get everything needed to work on a project.
    Now includes file attachments!
    
    Returns:
    - Project details
    - All components
    - Active problems (open, investigating)
    - Recent changes (last 24-48 hours)
    - High-priority todos
    - Recent learnings
    - Current session if any
    - File attachments with key locations (NEW in v1.1)
    
    Args:
        project_name: Name of the project
        hours: How many hours back to look for changes (default 48)
        include_files: Include file attachments in context (default True)
    
    Returns:
        Full project context or None if project not found
    """
    db = get_db()
    ctx = db.get_project_context(project_name, hours)
    if ctx is None:
        return None
    
    result = {
        "project": ctx.project.model_dump(),
        "components": [c.model_dump() for c in ctx.components],
        "open_problems": [p.model_dump() for p in ctx.open_problems],
        "recent_changes": [c.model_dump() for c in ctx.recent_changes],
        "high_priority_todos": [t.model_dump() for t in ctx.high_priority_todos],
        "recent_learnings": [l.model_dump() for l in ctx.recent_learnings],
        "current_session": ctx.current_session.model_dump() if ctx.current_session else None
    }
    
    # Add file attachments if requested (v1.1)
    if include_files:
        attachments = get_attachments(project_id=ctx.project.id)
        result["attachments"] = attachments
        
        # Get content locations for attachments
        conn = db._conn()
        try:
            for att in attachments:
                locations = conn.execute(
                    "SELECT * FROM content_locations WHERE attachment_id = ?",
                    (att['id'],)
                ).fetchall()
                att['content_locations'] = [dict(l) for l in locations]
        finally:
            conn.close()
    
    return result


def get_project_context_lean(
    project_name: str,
    hours: int = 48
) -> Optional[dict]:
    """
    LEAN VERSION: Get project context with minimal token footprint.
    
    Returns summary stats instead of full objects. Use this at session start
    to avoid context overflow. Call specific tools (get_open_problems, etc.)
    only when you need full details.
    
    Args:
        project_name: Name of the project
        hours: How many hours back for changes (default 48)
    
    Returns:
        Compact context with:
        - project: id, name, status only
        - stats: counts of components, problems, todos, etc.
        - recent_focus: last problem/component worked on
        - blocking: any critical/high priority open items
        - quick_todos: up to 3 pending todo titles
    """
    db = get_db()
    ctx = db.get_project_context(project_name, hours)
    if ctx is None:
        return None
    
    # Build lean response
    project = ctx.project
    
    # Get blocking items (high/critical open problems)
    blocking = [
        {"id": p.id, "title": p.title[:60]}
        for p in ctx.open_problems 
        if p.severity in ('critical', 'high')
    ][:3]
    
    # Get top 3 pending todos (title only)
    quick_todos = [
        t.title[:50] for t in ctx.high_priority_todos[:3]
    ]
    
    # Get recent focus from changes
    recent_focus = None
    if ctx.recent_changes:
        last_change = ctx.recent_changes[0]
        recent_focus = f"Component {last_change.component_id}: {last_change.field_name}"
    
    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "status": project.status
        },
        "stats": {
            "components": len(ctx.components),
            "open_problems": len(ctx.open_problems),
            "pending_todos": len(ctx.high_priority_todos),
            "recent_changes": len(ctx.recent_changes),
            "learnings": len(ctx.recent_learnings)
        },
        "blocking": blocking,
        "quick_todos": quick_todos,
        "recent_focus": recent_focus,
        "has_active_session": ctx.current_session is not None
    }
