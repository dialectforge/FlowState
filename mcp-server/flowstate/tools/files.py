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
        attachment_id = cursor.lastrowid
        # Index its text for manage_files search (same transaction: attached = searchable)
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        _index(conn, row, project.name)
        conn.commit()

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


# ---------------------------------------------------------------- text index (2.4.0)
# Attached text files are split into sections of lines and stored in content_locations
# (snippet = the section's text), so manage_files search finds words in them. A file is
# indexed when attached; files attached before 2.4 are indexed on the first search that
# could find them. attachments.indexed_at marks a file as done (also binary or too big ones,
# which get no rows). Standard library only.
INDEX_MAX_BYTES = 2_000_000
SECTION_LINES = 40
SNIPPET_MAX = 4000
_TEXT_TYPES = {"txt", "md", "markdown", "rst", "csv", "tsv", "json", "jsonl", "yaml", "yml", "toml",
               "ini", "cfg", "conf", "xml", "html", "htm", "css", "log", "sql", "sh", "bash", "zsh",
               "ps1", "bat", "py", "js", "jsx", "ts", "tsx", "mjs", "swift", "rs", "go", "java", "kt",
               "c", "h", "cpp", "hpp", "cs", "rb", "php", "pl", "lua", "r", "m", "scala", "env.example",
               "tex", "org", "adoc", "properties", "gradle", "vue", "svelte", "dart", "ex", "exs"}
_CODE_TYPES = _TEXT_TYPES - {"txt", "md", "markdown", "rst", "csv", "tsv", "log", "tex", "org", "adoc"}


def _file_of(row, project_name: str) -> Path:
    if row["is_external"]:
        return Path(row["file_path"])
    return _get_project_bundle_path(project_name) / row["file_path"]


def _read_text(path: Path, file_type: str) -> Optional[str]:
    """The file's text, or None for a missing, binary or too-big file."""
    try:
        if not path.is_file() or path.stat().st_size > INDEX_MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192] and (file_type or "").lower() not in _TEXT_TYPES:
        return None
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    if (file_type or "").lower() in _TEXT_TYPES:
        return raw.decode("utf-8", errors="replace")
    return None


def _sections(text: str, headings: bool = False):
    """(first line, last line, text) of each section: every SECTION_LINES lines, and in
    markdown a heading starts one."""
    lines = text.splitlines()
    start, buf = 1, []
    for n, line in enumerate(lines, 1):
        heading = headings and line.startswith("#") and any(b.strip() for b in buf)
        if buf and (len(buf) >= SECTION_LINES or heading):
            yield start, n - 1, "\n".join(buf)
            start, buf = n, []
        buf.append(line)
    if buf and any(b.strip() for b in buf):
        yield start, len(lines), "\n".join(buf)


def _index(conn, row, project_name: str) -> int:
    """Index one attachment's text (inside the caller's transaction). Returns sections."""
    text = _read_text(_file_of(row, project_name), row["file_type"])
    conn.execute("DELETE FROM content_locations WHERE attachment_id = ? AND location_type = 'line' "
                 "AND related_problem_id IS NULL AND related_learning_id IS NULL", (row["id"],))
    n = 0
    if text:
        category = "code" if (row["file_type"] or "").lower() in _CODE_TYPES else "reference"
        md = (row["file_type"] or "").lower() in ("md", "markdown")
        for a, b, chunk in _sections(text, md):
            if not chunk.strip():
                continue
            head = next((ln.strip().lstrip("#").strip() for ln in chunk.splitlines() if ln.strip()), "")
            conn.execute(
                """INSERT INTO content_locations (attachment_id, description, category, location_type,
                   start_location, end_location, snippet) VALUES (?, ?, ?, 'line', ?, ?, ?)""",
                (row["id"], f"{row['file_name']} lines {a}-{b}: {head[:80]}", category, str(a), str(b),
                 chunk[:SNIPPET_MAX]))
            n += 1
    conn.execute("UPDATE attachments SET indexed_at = CURRENT_TIMESTAMP, content_extracted = ? "
                 "WHERE id = ?", (bool(n), row["id"]))
    return n


def index_pending(conn, project_id: Optional[int] = None, limit: int = 50) -> int:
    """Index attachments not indexed yet (attached before 2.4). Returns how many."""
    sql = ("SELECT a.*, p.name AS project_name FROM attachments a JOIN projects p ON p.id = a.project_id "
           "WHERE a.indexed_at IS NULL")
    params: list = []
    if project_id is not None:
        sql += " AND a.project_id = ?"
        params.append(project_id)
    rows = conn.execute(sql + " ORDER BY a.id LIMIT ?", (*params, limit)).fetchall()
    for r in rows:
        _index(conn, r, r["project_name"])
    if rows:
        conn.commit()
    return len(rows)


def _excerpt(snippet: str, words: list, width: int = 240) -> str:
    """The part of a section around the first word found, on one line."""
    flat = " ".join((snippet or "").split())
    low = flat.lower()
    at = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
    start = max(0, at - width // 3)
    out = flat[start:start + width]
    return ("..." if start else "") + out + ("..." if start + width < len(flat) else "")


def search_file_content(
    query: str,
    project_id: Optional[int] = None,
    file_types: Optional[list[str]] = None,
    limit: int = 10
) -> list[dict]:
    """
    Search the text of attached files (content_locations). Every word of the query must
    appear in a section (case-insensitive for ASCII); each hit names the file and lines.

    Args:
        query: Search query
        project_id: Optional project filter
        file_types: Optional list of file types to search
        limit: Max results

    Returns:
        List of hits: attachment id, file name and type, lines, a short excerpt
    """
    db = get_db()
    conn = db._conn()
    try:
        index_pending(conn, project_id)
        words = [w.lower() for w in (query or "").split() if w.strip()][:8]
        if not words:
            return []
        sql = """
            SELECT cl.id, cl.attachment_id, cl.description, cl.category, cl.location_type,
                   cl.start_location, cl.end_location, cl.snippet,
                   a.file_name, a.file_path, a.file_type, a.project_id
            FROM content_locations cl
            JOIN attachments a ON cl.attachment_id = a.id
            WHERE 1 = 1
        """
        params: list = []
        for w in words:
            sql += " AND (cl.snippet LIKE ? ESCAPE '\\' OR cl.description LIKE ? ESCAPE '\\')"
            pat = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [pat, pat]
        if project_id is not None:
            sql += " AND a.project_id = ?"
            params.append(project_id)

        if file_types:
            placeholders = ",".join(["?"] * len(file_types))
            sql += f" AND a.file_type IN ({placeholders})"
            params.extend(file_types)

        sql += " ORDER BY a.id DESC, cl.id LIMIT ?"
        params.append(max(1, min(int(limit or 10), 50)))

        out = []
        for row in conn.execute(sql, params).fetchall():
            hit = dict(row)
            hit["excerpt"] = _excerpt(hit.pop("snippet"), words)
            out.append(hit)
        return out
    finally:
        conn.close()
