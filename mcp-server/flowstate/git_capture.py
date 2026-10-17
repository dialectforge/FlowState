"""Opt-in capture of git commit subjects into a project's changes (v2 Phase D, migration 008).

Off by default. The user turns it on per project from the command line:

    flowstate git-capture enable --project "Home Hub" --repo D:\\path\\to\\repo

From then on, new commits (since the moment it was turned on) are added as changes on the
project's "Git" component, one per commit: "commit 1a2b3c4d: <subject line>". Only the
subject is kept (no author, e-mail or diff). It runs when start_here is called for the
project and with `flowstate git-capture run`. The review mode applies (with mode
`everything` or `types` including change, they wait for review). Subjects that look like
they contain a secret are skipped.
"""

import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import secrets_scan

MAX_PER_RUN = 50
TIMEOUT = 5.0


class CaptureError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn(db_path: Path):
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='git_capture'").fetchone() is not None


def _git(repo: str, *args: str) -> str:
    exe = shutil.which("git")
    if not exe:
        raise CaptureError("git is not installed or not on PATH")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C"}
    try:
        p = subprocess.run([exe, "-C", repo, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise CaptureError("git took too long")
    if p.returncode != 0:
        raise CaptureError((p.stderr or p.stdout or "git failed").strip().splitlines()[0][:200])
    return p.stdout


def enable(db_path: Path, project_id: int, repo: str, by: str = "cli") -> dict:
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise CaptureError(f"{path} is not a folder")
    top = _git(str(path), "rev-parse", "--show-toplevel").strip()
    try:
        head = _git(str(path), "rev-parse", "HEAD").strip()
    except CaptureError:
        head = None   # empty repo: everything from the first commit on is new
    conn = _conn(db_path)
    try:
        conn.execute("""INSERT INTO git_capture (project_id, repo_path, enabled, last_commit,
                        updated_at, updated_by) VALUES (?, ?, 1, ?, ?, ?)
                        ON CONFLICT(project_id) DO UPDATE SET repo_path = excluded.repo_path,
                        enabled = 1, last_commit = COALESCE(git_capture.last_commit, excluded.last_commit),
                        updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
                     (project_id, top, head, _now(), by[:40]))
        conn.commit()
    finally:
        conn.close()
    return {"project_id": project_id, "repo": top, "starts_after": head}


def disable(db_path: Path, project_id: int, by: str = "cli") -> bool:
    conn = _conn(db_path)
    try:
        cur = conn.execute("UPDATE git_capture SET enabled = 0, updated_at = ?, updated_by = ? "
                           "WHERE project_id = ?", (_now(), by[:40], project_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def status(db_path: Path) -> list[dict]:
    conn = _conn(db_path)
    try:
        if not available(conn):
            return []
        return [dict(r) for r in conn.execute(
            """SELECT g.*, p.name project FROM git_capture g JOIN projects p ON p.id = g.project_id
               ORDER BY p.name""")]
    finally:
        conn.close()


def is_on(db_path: Path, project_id: int) -> bool:
    conn = _conn(db_path)
    try:
        return available(conn) and conn.execute(
            "SELECT 1 FROM git_capture WHERE project_id = ? AND enabled = 1", (project_id,)).fetchone() is not None
    finally:
        conn.close()


def run(db_path: Path, project_id: int, save: Callable[[int, str, str], object]) -> dict:
    """Capture new commits for one project. `save(project_id, summary, details)` records one
    change (following the review mode). Returns counts."""
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM git_capture WHERE project_id = ? AND enabled = 1",
                           (project_id,)).fetchone() if available(conn) else None
    finally:
        conn.close()
    if row is None:
        return {"captured": 0, "on": False}
    repo, last = row["repo_path"], row["last_commit"]
    head = _git(repo, "rev-parse", "HEAD").strip()
    out = {"captured": 0, "secrets_skipped": 0, "on": True, "repo": repo}
    if head != last:
        rng = f"{last}..HEAD" if last else "HEAD"
        try:
            log = _git(repo, "log", "--no-merges", f"-n{MAX_PER_RUN}", "--format=%H%x1f%s", rng)
        except CaptureError:
            log = ""   # the old commit is gone (history rewritten): start again from HEAD
            out["note"] = "history was rewritten; capture restarts from the current commit"
        for line in reversed([l for l in log.splitlines() if "\x1f" in l]):
            sha, subject = line.split("\x1f", 1)
            subject = " ".join(subject.split())[:200]
            if secrets_scan.scan_text(subject):
                out["secrets_skipped"] += 1
                continue
            save(project_id, f"commit {sha[:8]}: {subject}", f"git commit {sha} in {Path(repo).name}")
            out["captured"] += 1
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE git_capture SET last_commit = ?, last_run_at = ? WHERE project_id = ?",
                     (head, _now(), project_id))
        conn.commit()
    finally:
        conn.close()
    return out
