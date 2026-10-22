"""
FlowState Story Module - Narrative generation from project data.

This module provides:
1. Project story generation (full narrative of a project's journey)
2. Problem journey maps (decision tree visualization)
3. Milestone detection
4. Export formatting (markdown, HTML)
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

from . import dbconn


@dataclass
class StoryChapter:
    """A chapter in the project story."""
    title: str
    date: datetime
    content: str
    chapter_type: str  # 'genesis', 'milestone', 'challenge', 'breakthrough', 'current'
    components: list[str] = field(default_factory=list)
    problems_solved: int = 0
    key_insights: list[str] = field(default_factory=list)


@dataclass
class ProjectStory:
    """The complete project story."""
    project_name: str
    project_description: str
    chapters: list[StoryChapter]
    stats: dict
    generated_at: datetime


class StoryGenerator:
    """
    Generates narrative stories from project data.
    """
    
    def __init__(self, db_path: str):
        """
        Initialize the story generator.
        
        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path
    
    def _conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = dbconn.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def generate_project_story(self, project_id: int) -> Optional[ProjectStory]:
        """
        Generate a narrative story for a project.
        
        Args:
            project_id: The project ID
            
        Returns:
            ProjectStory object or None if project not found
        """
        conn = self._conn()
        try:
            # Get project
            project = conn.execute(
                "SELECT * FROM projects WHERE id = ?",
                (project_id,)
            ).fetchone()
            
            if not project:
                return None
            
            # Get all related data
            components = conn.execute(
                "SELECT * FROM components WHERE project_id = ? ORDER BY created_at",
                (project_id,)
            ).fetchall()
            
            problems = conn.execute(
                """SELECT p.*, c.name as component_name 
                   FROM problems p 
                   JOIN components c ON p.component_id = c.id 
                   WHERE c.project_id = ? 
                   ORDER BY p.created_at""",
                (project_id,)
            ).fetchall()
            
            solutions = conn.execute(
                """SELECT s.*, p.title as problem_title
                   FROM solutions s
                   JOIN problems p ON s.problem_id = p.id
                   JOIN components c ON p.component_id = c.id
                   WHERE c.project_id = ?
                   ORDER BY s.created_at""",
                (project_id,)
            ).fetchall()
            
            learnings = conn.execute(
                "SELECT * FROM learnings WHERE project_id = ? ORDER BY created_at",
                (project_id,)
            ).fetchall()
            
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE project_id = ? ORDER BY started_at",
                (project_id,)
            ).fetchall()
            
            # Generate chapters
            chapters = self._generate_chapters(
                project, components, problems, solutions, learnings, sessions
            )
            
            # Calculate stats
            stats = {
                "total_components": len(components),
                "total_problems": len(problems),
                "solved_problems": len([p for p in problems if p["status"] == "solved"]),
                "total_learnings": len(learnings),
                "total_sessions": len(sessions),
                "duration_days": self._calculate_duration(project, sessions)
            }
            
            return ProjectStory(
                project_name=project["name"],
                project_description=project["description"] or "",
                chapters=chapters,
                stats=stats,
                generated_at=datetime.now()
            )
            
        finally:
            conn.close()
    
    def _generate_chapters(
        self,
        project,
        components,
        problems,
        solutions,
        learnings,
        sessions
    ) -> list[StoryChapter]:
        """Generate story chapters from project data."""
        chapters = []
        
        # Chapter 1: Genesis
        genesis = StoryChapter(
            title="Genesis",
            date=datetime.fromisoformat(project["created_at"]) if project["created_at"] else datetime.now(),
            content=f"The project '{project['name']}' was born.",
            chapter_type="genesis",
            components=[c["name"] for c in components[:3]]  # First 3 components
        )
        if project["description"]:
            genesis.content += f"\n\n{project['description']}"
        chapters.append(genesis)
        
        # Group events by time periods (weeks)
        all_events = []
        
        # Add problems as events
        for p in problems:
            all_events.append({
                "type": "problem",
                "date": datetime.fromisoformat(p["created_at"]) if p["created_at"] else datetime.now(),
                "data": dict(p)
            })
        
        # Add solutions as events
        for s in solutions:
            all_events.append({
                "type": "solution",
                "date": datetime.fromisoformat(s["created_at"]) if s["created_at"] else datetime.now(),
                "data": dict(s)
            })
        
        # Add key learnings as events
        for l in learnings:
            if l["category"] in ["pattern", "best_practice", "architecture"]:
                all_events.append({
                    "type": "learning",
                    "date": datetime.fromisoformat(l["created_at"]) if l["created_at"] else datetime.now(),
                    "data": dict(l)
                })
        
        # Sort by date
        all_events.sort(key=lambda x: x["date"])
        
        # Group into chapters by week
        if all_events:
            current_week_start = all_events[0]["date"].replace(hour=0, minute=0, second=0)
            week_events = []
            
            for event in all_events:
                event_week = event["date"].replace(hour=0, minute=0, second=0)
                
                # If we've moved to a new week, create chapter for previous week
                if (event_week - current_week_start).days >= 7:
                    if week_events:
                        chapter = self._create_chapter_from_events(week_events, current_week_start)
                        if chapter:
                            chapters.append(chapter)
                    
                    current_week_start = event_week
                    week_events = []
                
                week_events.append(event)
            
            # Don't forget the last week
            if week_events:
                chapter = self._create_chapter_from_events(week_events, current_week_start)
                if chapter:
                    chapters.append(chapter)
        
        # Final chapter: Current state
        open_problems = len([p for p in problems if p["status"] in ["open", "investigating"]])
        current_chapter = StoryChapter(
            title="Current State",
            date=datetime.now(),
            content=self._generate_current_state_content(project, components, open_problems),
            chapter_type="current"
        )
        chapters.append(current_chapter)
        
        return chapters
    
    def _create_chapter_from_events(self, events: list, week_start: datetime) -> Optional[StoryChapter]:
        """Create a chapter from a week's worth of events."""
        if not events:
            return None
        
        solutions = [e for e in events if e["type"] == "solution"]
        problems = [e for e in events if e["type"] == "problem"]
        learnings = [e for e in events if e["type"] == "learning"]
        
        # Determine chapter type
        if len(solutions) >= 2:
            chapter_type = "breakthrough"
            title = "Breakthroughs"
        elif len(problems) >= 3:
            chapter_type = "challenge"
            title = "Challenges"
        else:
            chapter_type = "milestone"
            title = "Progress"
        
        # Generate content
        content_parts = []
        
        if solutions:
            content_parts.append(f"Solved {len(solutions)} problems:")
            for s in solutions:
                content_parts.append(f"- {s['data'].get('problem_title', 'Unknown')}: {s['data'].get('summary', '')[:100]}")
        
        if problems:
            new_problems = [p for p in problems if p["data"].get("status") in ["open", "investigating"]]
            if new_problems:
                content_parts.append(f"\nEncountered {len(new_problems)} new challenges.")
        
        if learnings:
            content_parts.append("\nKey insights:")
            for l in learnings[:3]:  # Top 3 learnings
                content_parts.append(f"- {l['data'].get('insight', '')[:100]}")
        
        return StoryChapter(
            title=title,
            date=week_start,
            content="\n".join(content_parts),
            chapter_type=chapter_type,
            problems_solved=len(solutions),
            key_insights=[l["data"].get("insight", "")[:100] for l in learnings[:3]]
        )
    
    def _generate_current_state_content(self, project, components, open_problems: int) -> str:
        """Generate content for the current state chapter."""
        status = project["status"]
        comp_count = len(components)
        
        if status == "completed":
            return f"The project is complete! Built with {comp_count} components."
        elif status == "paused":
            return f"Currently paused with {open_problems} open problems to address."
        else:
            if open_problems == 0:
                return f"Actively developing with {comp_count} components. No open problems!"
            else:
                return f"Actively developing. {open_problems} problems being investigated."
    
    def _calculate_duration(self, project, sessions) -> int:
        """Calculate project duration in days."""
        start = datetime.fromisoformat(project["created_at"]) if project["created_at"] else datetime.now()
        
        if sessions:
            last_session = max(sessions, key=lambda s: s["started_at"] or "")
            end = datetime.fromisoformat(last_session["started_at"]) if last_session["started_at"] else datetime.now()
        else:
            end = datetime.now()
        
        return (end - start).days + 1
    
    def export_to_markdown(self, story: ProjectStory) -> str:
        """
        Export a project story to markdown format.
        
        Args:
            story: The ProjectStory to export
            
        Returns:
            Markdown string
        """
        lines = [
            f"# The {story.project_name} Story",
            "",
            f"*Generated: {story.generated_at.strftime('%B %d, %Y')}*",
            "",
        ]
        
        if story.project_description:
            lines.extend([story.project_description, ""])
        
        lines.extend([
            "---",
            "",
        ])
        
        # Chapters
        for i, chapter in enumerate(story.chapters, 1):
            lines.extend([
                f"## Chapter {i}: {chapter.title}",
                f"*{chapter.date.strftime('%B %d, %Y')}*",
                "",
                chapter.content,
                "",
            ])
            
            if chapter.key_insights:
                lines.append("**Key Insights:**")
                for insight in chapter.key_insights:
                    lines.append(f"- {insight}")
                lines.append("")
        
        # Stats
        lines.extend([
            "---",
            "",
            "## Statistics",
            "",
            f"- **Duration:** {story.stats.get('duration_days', 0)} days",
            f"- **Components:** {story.stats.get('total_components', 0)}",
            f"- **Problems Encountered:** {story.stats.get('total_problems', 0)}",
            f"- **Problems Solved:** {story.stats.get('solved_problems', 0)}",
            f"- **Learnings Captured:** {story.stats.get('total_learnings', 0)}",
            f"- **Work Sessions:** {story.stats.get('total_sessions', 0)}",
        ])
        
        return "\n".join(lines)
    
    def export_to_html(self, story: ProjectStory) -> str:
        """
        Export a project story to HTML format.
        
        Args:
            story: The ProjectStory to export
            
        Returns:
            HTML string
        """
        html_parts = [
            "<!DOCTYPE html>",
            "<html>",
            "<head>",
            f"<title>The {story.project_name} Story</title>",
            "<style>",
            "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 0 auto; padding: 2rem; }",
            ".chapter { margin: 2rem 0; padding: 1.5rem; border-left: 4px solid #3b82f6; background: #f8fafc; }",
            ".chapter.genesis { border-color: #10b981; }",
            ".chapter.challenge { border-color: #ef4444; }",
            ".chapter.breakthrough { border-color: #8b5cf6; }",
            ".chapter.current { border-color: #f59e0b; }",
            ".stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }",
            ".stat { text-align: center; padding: 1rem; background: #f1f5f9; border-radius: 8px; }",
            ".stat-value { font-size: 2rem; font-weight: bold; color: #3b82f6; }",
            "</style>",
            "</head>",
            "<body>",
            f"<h1>The {story.project_name} Story</h1>",
            f"<p><em>Generated: {story.generated_at.strftime('%B %d, %Y')}</em></p>",
        ]
        
        if story.project_description:
            html_parts.append(f"<p>{story.project_description}</p>")
        
        # Chapters
        for i, chapter in enumerate(story.chapters, 1):
            html_parts.extend([
                f'<div class="chapter {chapter.chapter_type}">',
                f"<h2>Chapter {i}: {chapter.title}</h2>",
                f"<p><em>{chapter.date.strftime('%B %d, %Y')}</em></p>",
                f"<p>{chapter.content.replace(chr(10), '<br>')}</p>",
            ])
            
            if chapter.key_insights:
                html_parts.append("<p><strong>Key Insights:</strong></p><ul>")
                for insight in chapter.key_insights:
                    html_parts.append(f"<li>{insight}</li>")
                html_parts.append("</ul>")
            
            html_parts.append("</div>")
        
        # Stats
        html_parts.extend([
            '<h2>Statistics</h2>',
            '<div class="stats">',
            f'<div class="stat"><div class="stat-value">{story.stats.get("duration_days", 0)}</div><div>Days</div></div>',
            f'<div class="stat"><div class="stat-value">{story.stats.get("total_components", 0)}</div><div>Components</div></div>',
            f'<div class="stat"><div class="stat-value">{story.stats.get("solved_problems", 0)}/{story.stats.get("total_problems", 0)}</div><div>Problems Solved</div></div>',
            '</div>',
            "</body>",
            "</html>",
        ])
        
        return "\n".join(html_parts)


# Singleton instance
_story_generator: Optional[StoryGenerator] = None


def get_story_generator(db_path: str) -> StoryGenerator:
    """Get or create the story generator singleton."""
    global _story_generator
    if _story_generator is None or _story_generator.db_path != db_path:
        _story_generator = StoryGenerator(db_path)
    return _story_generator
