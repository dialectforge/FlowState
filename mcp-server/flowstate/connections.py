"""Per-connection answers (2.4.0): which session is this window's. Each stdio server
process and each HTTP MCP session is one connection (identity.conn()); two windows of one
app are two connections and never see each other's session as their own."""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from . import dbconn
from . import identity


def current_session(db_path: Path, tracker, client: str, pid: Optional[int]) -> dict:
    """manage_sessions current: this connection's open session, not the newest one."""
    own = tracker.own(client, pid, identity.conn()[0]) if tracker else None
    if own is None:
        return {"session": None, "note": "No session is open for this window yet; FlowState "
                                         "opens one with your first call that names a project."}
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (own["session_id"],)).fetchone()
    finally:
        conn.close()
    out = dict(row) if row else {"id": own["session_id"], "project_id": own["project_id"]}
    if isinstance(out.get("outcomes"), str):
        try:
            out["outcomes"] = json.loads(out["outcomes"])
        except ValueError:
            pass
    out.update(automatic=True, client=own["client"], last_activity=own["last_activity"])
    return out
