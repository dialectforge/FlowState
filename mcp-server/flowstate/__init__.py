# -*- coding: utf-8 -*-
"""FlowState - Development memory system.

A project-centric memory system with:
- Hierarchical organization (Project -> Component -> Changes/Problems/Solutions)
- Decision tree tracking (what failed, what worked, why)
- Conversation logging (what prompts led to what outcomes)
- Semantic search with embeddings
- Story generation for project narratives
"""

__version__ = "2.2.0"

from .database import Database, init_db
from .models import (
    Project, Component, Change, Problem, SolutionAttempt, 
    Solution, Todo, Conversation, Learning, Session, ProjectContext
)

__all__ = [
    "Database",
    "init_db",
    "Project",
    "Component", 
    "Change",
    "Problem",
    "SolutionAttempt",
    "Solution",
    "Todo",
    "Conversation",
    "Learning",
    "Session",
    "ProjectContext",
]
