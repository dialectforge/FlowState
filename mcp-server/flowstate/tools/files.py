"""File attachment tools."""

import os
import shutil
import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime
from .utils import get_db, data_dir

def _get_project_bundle_path(project_name: str) -> Path:
    """Get the path to a project's bundle directory."""
    return data_dir() / "projects" / f"{project_name.lower().replace(' ', '-')}.flowstate"


def _compute_file_hash(file_path: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _get_file_type(file_path: Path) -> str:
    """Determine file type category from extension."""
    ext = file_path.suffix.lower()
    if ext in ['.pdf', '.doc', '.docx', '.txt', '.md', '.rtf']:
        return 'document'
    elif ext in ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp']:
        return 'image'
    elif ext in ['.py', '.js', '.ts', '.swift', '.rs', '.go', '.java', '.c', '.cpp', '.h', '.jsx', '.tsx', '.css', '.html']:
        return 'code'
    else:
        return 'other'


def attach_file(
    project_id: int,
    file_path: str,
    component_id: Optional[int] = None,
    problem_id: Optional[int] = None,
    user_description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    copy_to_bundle: bool = True
) -> dict:
    """
    Attach a file to a project, component, or problem.
    
    Args:
        project_id: Which project to attach to
        file_path: Path to the file to attach
        component_id: Optional component to attach to
        problem_id: Optional problem to attach to
        user_description: Optional description from user
        tags: Optional list of tags
        copy_to_bundle: If True, copy file to project bundle; else reference external path
    
    Returns:
        The created attachment record
    """
    db = get_db()
    source_path = Path(file_path)
    
    if not source_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    # Get project info
    project = db.get_project(project_id)
    if not project:
        raise ValueError(f"Project not found: {project_id}")
    
    # Compute file metadata
    file_name = source_path.name
    file_type = source_path.suffix.lstrip('.').lower()
    file_size = source_path.stat().st_size
    file_hash = _compute_file_hash(source_path)
    
    # Determine storage path
    if copy_to_bundle:
        bundle_path = _get_project_bundle_path(project.name)
        attachments_dir = bundle_path / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy file to bundle
        dest_path = attachments_dir / file_name
        # Handle name collision
        counter = 1
        while dest_path.exists():
            stem = source_path.stem
            dest_path = attachments_dir / f"{stem}_{counter}{source_path.suffix}"
            counter += 1
        
        shutil.copy2(source_path, dest_path)
        stored_path = str(dest_path.relative_to(bundle_path))
        is_external = False
    else:
        stored_path = str(source_path.absolute())
        is_external = True
    
    # Insert into database
    conn = db._conn()
    try:
        cursor = conn.execute(
            """INSERT INTO attachments 
               (project_id, component_id, problem_id, file_name, file_path, 
                file_type, file_size, file_hash, is_external, user_description, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, component_id, problem_id, file_name, stored_path,
             file_type, file_size, file_hash, is_external, user_description,
             ','.join(tags) if tags else None)
        )
        conn.commit()
        attachment_id = cursor.lastrowid
        
        # Fetch and return the created attachment
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        return dict(row) if row else {"id": attachment_id}
    finally:
        conn.close()


def get_attachments(
    project_id: Optional[int] = None,
    component_id: Optional[int] = None,
    problem_id: Optional[int] = None
) -> list[dict]:
    """
    Get file attachments with optional filters.
    
    Args:
        project_id: Filter by project
        component_id: Filter by component
        problem_id: Filter by problem
    
    Returns:
        List of attachment records
    """
    db = get_db()
    conn = db._conn()
    try:
        conditions = []
        params = []
        
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if component_id is not None:
            conditions.append("component_id = ?")
            params.append(component_id)
        if problem_id is not None:
            conditions.append("problem_id = ?")
            params.append(problem_id)
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM attachments WHERE {where_clause} ORDER BY created_at DESC",
            params
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def remove_attachment(
    attachment_id: int,
    delete_file: bool = False
) -> dict:
    """
    Remove an attachment record, optionally deleting the file.
    
    Args:
        attachment_id: The attachment to remove
        delete_file: If True and file is in bundle, delete the actual file
    
    Returns:
        Success status and info
    """
    db = get_db()
    conn = db._conn()
    try:
        # Get attachment first
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        if not row:
            return {"success": False, "error": "Attachment not found"}
        
        attachment = dict(row)
        
        # Delete file if requested and not external
        if delete_file and not attachment['is_external']:
            project = db.get_project(attachment['project_id'])
            if project:
                bundle_path = _get_project_bundle_path(project.name)
                file_path = bundle_path / attachment['file_path']
                if file_path.exists():
                    os.remove(file_path)
        
        # Delete from database
        conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        conn.commit()
        
        return {"success": True, "deleted_attachment": attachment}
    finally:
        conn.close()


def search_file_content(
    query: str,
    project_id: Optional[int] = None,
    file_types: Optional[list[str]] = None,
    limit: int = 10
) -> list[dict]:
    """
    Search across indexed file content (content_locations table).
    
    Args:
        query: Search query
        project_id: Optional project filter
        file_types: Optional list of file types to search
        limit: Max results
    
    Returns:
        List of matching content locations with file info
    """
    db = get_db()
    conn = db._conn()
    try:
        # Build query
        sql = """
            SELECT cl.*, a.file_name, a.file_path, a.file_type, a.project_id
            FROM content_locations cl
            JOIN attachments a ON cl.attachment_id = a.id
            WHERE cl.description LIKE ? OR cl.snippet LIKE ?
        """
        params = [f"%{query}%", f"%{query}%"]
        
        if project_id is not None:
            sql += " AND a.project_id = ?"
            params.append(project_id)
        
        if file_types:
            placeholders = ",".join(["?"] * len(file_types))
            sql += f" AND a.file_type IN ({placeholders})"
            params.extend(file_types)
        
        sql += f" ORDER BY cl.created_at DESC LIMIT ?"
        params.append(limit)
        
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
