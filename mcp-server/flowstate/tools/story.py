"""Story generation tools."""

from .utils import get_db

def generate_project_story(project_id: int) -> dict:
    """
    Generate narrative storyboard data for a project.
    Returns structured data that a client can render as a visual story.
    
    Args:
        project_id: The project ID
    
    Returns:
        Complete story data with project, components, problems, solutions,
        changes, learnings, sessions, and stats
    """
    db = get_db()
    return db.get_project_story_data(project_id)


def generate_problem_journey(problem_id: int) -> dict:
    """
    Generate the journey map for a problem.
    Shows all attempts, branches, outcomes, learnings.
    
    Args:
        problem_id: The problem ID
    
    Returns:
        Problem journey with attempts tree, solution, related learnings, stats
    """
    db = get_db()
    return db.get_problem_journey_data(problem_id)


def generate_architecture_diagram(project_id: int) -> dict:
    """
    Generate component diagram data for a project.
    Shows hierarchy, relationships, problem hotspots.
    
    Args:
        project_id: The project ID
    
    Returns:
        Diagram data with nodes (components) and edges (relationships)
    """
    db = get_db()
    return db.get_architecture_data(project_id)
