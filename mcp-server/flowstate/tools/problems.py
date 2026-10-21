"""Problem tracking and solution tools."""

from typing import Optional
from .utils import get_db, _compact_list

def log_problem(
    component_id: int,
    title: str,
    description: Optional[str] = None,
    severity: str = "medium"
) -> dict:
    """
    Log a new problem.
    
    Args:
        component_id: Which component has the problem
        title: Brief problem title
        description: Detailed description
        severity: 'low', 'medium', 'high', 'critical'
    
    Returns:
        The logged problem
    """
    db = get_db()
    problem = db.log_problem(component_id, title, description, severity)
    
    # Index for search
    search_text = f"{title} {description or ''}"
    component = db.get_component(component_id)
    if component:
        db.index_for_search("problem", problem.id, component.project_id, search_text)
    
    return problem.model_dump()


def get_open_problems(
    project_id: Optional[int] = None,
    component_id: Optional[int] = None,
    compact: bool = False
) -> list[dict]:
    """
    Get all open/investigating problems.
    
    Args:
        project_id: Filter by project (optional)
        component_id: Filter by component (optional)
        compact: Return minimal fields to save tokens (default False)
    
    Returns:
        List of open problems
    """
    db = get_db()
    problems = db.get_open_problems(project_id, component_id)
    result = [p.model_dump() for p in problems]
    return _compact_list(result) if compact else result


def log_attempt(
    problem_id: int,
    description: str,
    parent_attempt_id: Optional[int] = None
) -> dict:
    """
    Log a solution attempt.
    
    Args:
        problem_id: Which problem this attempts to solve
        description: What we're trying
        parent_attempt_id: Optional parent attempt (for branching)
    
    Returns:
        The logged attempt
    """
    db = get_db()
    attempt = db.log_attempt(problem_id, description, parent_attempt_id)
    return attempt.model_dump()


def mark_attempt_outcome(
    attempt_id: int,
    outcome: str,
    notes: Optional[str] = None,
    confidence: str = "attempted"
) -> Optional[dict]:
    """
    Record the outcome of an attempt.
    
    Args:
        attempt_id: The attempt ID
        outcome: 'success', 'failure', 'partial', 'abandoned'
        notes: What we learned
        confidence: 'attempted', 'worked_once', 'verified', 'proven', 'deprecated'
    
    Returns:
        Updated attempt
    """
    db = get_db()
    attempt = db.mark_attempt_outcome(attempt_id, outcome, notes, confidence)
    return attempt.model_dump() if attempt else None


def mark_problem_solved(
    problem_id: int,
    winning_attempt_id: Optional[int],
    summary: str,
    key_insight: Optional[str] = None,
    code_snippet: Optional[str] = None
) -> dict:
    """
    Mark a problem as solved.
    
    Args:
        problem_id: The problem ID
        winning_attempt_id: Which attempt worked
        summary: Brief solution summary
        key_insight: The key learning/insight
        code_snippet: Optional code that solved it
    
    Returns:
        The solution record
    """
    db = get_db()
    solution = db.mark_problem_solved(problem_id, winning_attempt_id, summary, key_insight, code_snippet)
    
    # Index for search
    problem = db.get_problem(problem_id)
    component = db.get_component(problem.component_id) if problem else None
    if component:
        search_text = f"{summary} {key_insight or ''}"
        db.index_for_search("solution", solution.id, component.project_id, search_text)
    
    return solution.model_dump()


def get_problem_tree(problem_id: Optional[int]) -> dict:
    """
    Get full decision tree of all attempts for a problem.
    
    Args:
        problem_id: The problem ID
    
    Returns:
        Problem with all attempts and solution if solved
    """
    if problem_id is None:
        return {"error": "problem_id_required", "message": "Give problem_id (#id in the brief)."}
    db = get_db()
    problem = db.get_problem(problem_id)
    if not problem:
        return {"error": "record_not_found", "message": f"No problem with id {problem_id}."}

    attempts = db.get_attempts_for_problem(problem_id)
    solution = db.get_solution(problem_id)
    
    return {
        "problem": problem.model_dump(),
        "attempts": [a.model_dump() for a in attempts],
        "solution": solution.model_dump() if solution else None
    }
