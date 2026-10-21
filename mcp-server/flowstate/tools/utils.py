"""Utility functions for FlowState tools."""

from typing import Optional
from ..database import Database
from pathlib import Path


# Global database instance (initialized by server)
_db: Optional[Database] = None

def _compact_dict(d: dict, max_text_len: int = 100) -> dict:
    """
    Create a compact version of a dict:
    - Remove None values
    - Remove empty strings/lists
    - Truncate long text fields
    - Remove timestamp fields unless essential
    """
    if not isinstance(d, dict):
        return d
    
    result = {}
    skip_fields = {'created_at', 'updated_at', 'solved_at', 'completed_at', 
                   'promoted_at', 'last_used', 'started_at', 'ended_at'}
    truncate_fields = {'description', 'insight', 'context', 'reason', 
                       'notes', 'summary', 'code_snippet', 'key_insight',
                       'old_value', 'new_value', 'skill', 'focus_summary'}
    
    for k, v in d.items():
        # Skip None and empty values
        if v is None or v == '' or v == []:
            continue
        # Skip timestamp fields
        if k in skip_fields:
            continue
        # Truncate long text
        if k in truncate_fields and isinstance(v, str) and len(v) > max_text_len:
            result[k] = v[:max_text_len] + '...'
        elif isinstance(v, dict):
            result[k] = _compact_dict(v, max_text_len)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            result[k] = [_compact_dict(item, max_text_len) for item in v]
        else:
            result[k] = v
    
    return result


def _compact_list(items: list, max_text_len: int = 100) -> list:
    """Compact a list of dicts."""
    return [_compact_dict(item, max_text_len) for item in items]


def get_db() -> Database:
    """Get the database instance."""
    global _db
    if _db is None:
        _db = Database()
    return _db


def data_dir() -> Path:
    """The data folder (project files, data-git): the folder of the database in use, so
    `flowstate --db <file>` keeps attachments next to that file."""
    return Path(get_db().db_path).parent


def set_db(db: Database) -> None:
    """Set the database instance (for testing)."""
    global _db
    _db = db
