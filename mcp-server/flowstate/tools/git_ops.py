"""Git operations tools."""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime
from . import utils

def _run_git_command(args: list[str], cwd: Optional[Path] = None) -> tuple[bool, str, str]:
    """Run a git command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd or utils.data_dir(),
            capture_output=True,
            text=True,
            timeout=60,
            # Never hand git the server's stdin: in a stdio server that is the MCP pipe, and on
            # Windows a child touching a pipe another thread is reading blocks until the next
            # message arrives (every data-git call then timed out).
            stdin=subprocess.DEVNULL,
            # never wait on a password prompt nobody can see: fail instead
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Git command timed out"
    except Exception as e:
        return False, "", str(e)


def git_init() -> dict:
    """
    Initialize FlowState data directory as a Git repository.
    Creates standard .gitignore for FlowState.
    
    Returns:
        Git status info
    """
    # Ensure data directory exists
    utils.data_dir().mkdir(parents=True, exist_ok=True)
    projects_dir = utils.data_dir() / "projects"
    projects_dir.mkdir(exist_ok=True)
    
    # Check if already initialized
    git_dir = utils.data_dir() / ".git"
    if git_dir.exists():
        return git_status()
    
    # Initialize git
    success, stdout, stderr = _run_git_command(["init"])
    if not success:
        return {"success": False, "error": stderr}
    
    # Create .gitignore
    gitignore_content = """# OS files
.DS_Store
Thumbs.db

# SQLite temporary/lock files (both .db and .sqlite extensions)
*.db-journal
*.db-wal
*.db-shm
*.sqlite-journal
*.sqlite-wal
*.sqlite-shm
*.tmp
*.bak

# Local backups (don't sync conflict backups)
*.local-backup-*

# Large media (optional - user can toggle)
# *.mp4
# *.mov
"""
    gitignore_path = utils.data_dir() / ".gitignore"
    gitignore_path.write_text(gitignore_content)
    
    # Create README
    readme_content = """# FlowState Data

This repository contains your FlowState development memory.

**Do not manually edit these files.**

Manage through the FlowState app or MCP server.
"""
    readme_path = utils.data_dir() / "README.md"
    readme_path.write_text(readme_content)
    
    # Initial commit
    _run_git_command(["add", "."])
    _run_git_command(["commit", "-m", "FlowState initialized"])
    
    return git_status()


def git_status() -> dict:
    """
    Get current Git repository status.
    
    Returns:
        Git status with pending changes, remote, branch info
    """
    git_dir = utils.data_dir() / ".git"
    if not git_dir.exists():
        return {
            "initialized": False,
            "has_remote": False,
            "branch": None,
            "pending_changes": 0,
            "error": "Not a git repository. Run git_init first."
        }
    
    # Get branch
    success, stdout, _ = _run_git_command(["branch", "--show-current"])
    branch = stdout.strip() if success else "unknown"
    
    # Get remote
    success, stdout, _ = _run_git_command(["remote", "-v"])
    has_remote = bool(stdout.strip())
    remote_url = None
    if has_remote:
        lines = stdout.strip().split("\n")
        if lines:
            parts = lines[0].split()
            if len(parts) >= 2:
                remote_url = parts[1]
    
    # Get status (pending changes)
    success, stdout, _ = _run_git_command(["status", "--porcelain"])
    pending_changes = len(stdout.strip().split("\n")) if stdout.strip() else 0
    
    # Get last commit info
    success, stdout, _ = _run_git_command(["log", "-1", "--format=%H|%s|%ai"])
    last_commit = None
    if success and stdout.strip():
        parts = stdout.strip().split("|")
        if len(parts) >= 3:
            last_commit = {
                "hash": parts[0][:8],
                "message": parts[1],
                "date": parts[2]
            }
    
    return {
        "initialized": True,
        "has_remote": has_remote,
        "remote_url": remote_url,
        "branch": branch,
        "pending_changes": pending_changes,
        "last_commit": last_commit
    }


_SECRET_FILE = re.compile(
    r"(token|secret|credential|password|passwd|\.env($|\.)|\.pem$|\.key$|\.p12$|\.pfx$|"
    r"^id_(rsa|dsa|ecdsa|ed25519)|\.log$)", re.I)


def is_secret_file(path: str) -> bool:
    """File names that must never be committed from the data folder."""
    return bool(_SECRET_FILE.search(path.replace("\\", "/").rsplit("/", 1)[-1]))


def git_sync(commit_message: Optional[str] = None) -> dict:
    """
    Perform full sync: add all, commit, pull --rebase, push.
    
    Args:
        commit_message: Custom commit message (default: timestamp)
    
    Returns:
        Sync result with status info
    """
    git_dir = utils.data_dir() / ".git"
    if not git_dir.exists():
        return {"success": False, "error": "Not a git repository. Run git_init first."}
    
    # Check for remote
    status = git_status()
    
    # Add all changes
    success, stdout, stderr = _run_git_command(["add", "."])
    if not success:
        return {"success": False, "step": "add", "error": stderr}

    # Never commit tokens, keys, .env files or logs, even if .gitignore misses them.
    _, staged, _ = _run_git_command(["diff", "--cached", "--name-only"])
    risky = [p for p in staged.splitlines() if p.strip() and is_secret_file(p.strip())]
    if risky:
        _run_git_command(["reset", "-q", "--", *risky])
        return {"success": False, "step": "add", "refused": risky,
                "error": "These files look like secrets or logs and were not committed. Move "
                         "them out of the data folder or add them to .gitignore, then sync again."}

    # Check if there's anything to commit
    success, stdout, _ = _run_git_command(["status", "--porcelain"])
    has_changes = bool(stdout.strip())
    
    # Commit if there are changes
    if has_changes:
        msg = commit_message or f"FlowState sync - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        success, stdout, stderr = _run_git_command(["commit", "-m", msg])
        if not success:
            return {"success": False, "step": "commit", "error": stderr}
    
    if not status.get("has_remote"):
        return {"success": True, "committed": has_changes, "pushed": False,
                "message": (commit_message or "FlowState sync") + " (no remote set: committed locally only)"}

    # The remote's side, without changing anything: reachable? does the branch exist yet?
    branch = status.get("branch") or "main"
    local_only = {"committed": has_changes, "pushed": False}
    ok, heads, err = _run_git_command(["ls-remote", "--heads", "origin", branch])
    if not ok:
        return {"success": False, "step": "connect", **local_only, "offline": True,
                "error": "Couldn't reach the remote, so nothing was pushed"
                         + (" (your changes are committed locally)" if has_changes else "")
                         + f". Sync again when it is reachable. Git said: {_last_line(err)}"}
    if heads.strip():   # the remote has this branch: bring its commits in first
        ok, out, err = _run_git_command(["pull", "--rebase", "origin", branch])
        if not ok:
            conflict = _rebasing() or "CONFLICT" in (out + err)
            if conflict:
                _run_git_command(["rebase", "--abort"])
            return {"success": False, "step": "pull", **local_only, "conflict": conflict,
                    "error": ("The remote has changes that clash with yours; nothing was pushed "
                              "and your data folder is as it was. Resolve it by hand with git."
                              if conflict else f"git pull failed: {_last_line(err)}")}
    # (an empty remote, or a new branch there: the first push creates it)
    ok, out, err = _run_git_command(["push", "-u", "origin", branch])
    if not ok:
        return {"success": False, "step": "push", **local_only,
                "error": f"git push failed, so nothing is on the remote: {_last_line(err)}"}
    return {
        "success": True,
        "committed": has_changes,
        "pushed": True,
        "first_push": not heads.strip(),
        "message": commit_message or "FlowState sync"
    }


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "no details")[:200]


def _rebasing() -> bool:
    g = utils.data_dir() / ".git"
    return (g / "rebase-merge").exists() or (g / "rebase-apply").exists()


def git_set_remote(remote_url: str) -> dict:
    """
    Add or update the remote URL.
    
    Args:
        remote_url: Git remote URL (e.g., https://github.com/user/repo.git)
    
    Returns:
        Status info
    """
    git_dir = utils.data_dir() / ".git"
    if not git_dir.exists():
        return {"success": False, "error": "Not a git repository. Run git_init first."}
    
    # Check if remote exists
    success, stdout, _ = _run_git_command(["remote", "-v"])
    has_origin = "origin" in stdout
    
    if has_origin:
        # Update existing remote
        success, stdout, stderr = _run_git_command(["remote", "set-url", "origin", remote_url])
    else:
        # Add new remote
        success, stdout, stderr = _run_git_command(["remote", "add", "origin", remote_url])
    
    if not success:
        return {"success": False, "error": stderr}
    
    return {"success": True, "remote_url": remote_url}


def git_clone(remote_url: str, local_path: Optional[str] = None) -> dict:
    """
    Clone an existing FlowState repository.
    
    Args:
        remote_url: Git remote URL to clone
        local_path: Optional local path (default: FlowState data directory)
    
    Returns:
        Clone result
    """
    target_path = Path(local_path) if local_path else utils.data_dir()
    
    # Don't clone if directory already has content
    if target_path.exists() and any(target_path.iterdir()):
        return {"success": False, "error": f"Directory not empty: {target_path}"}
    
    # Clone
    target_path.parent.mkdir(parents=True, exist_ok=True)
    success, stdout, stderr = _run_git_command(
        ["clone", remote_url, str(target_path)],
        cwd=target_path.parent
    )
    
    if not success:
        return {"success": False, "error": stderr}
    
    return {"success": True, "path": str(target_path), "remote_url": remote_url}


def git_history(limit: int = 20) -> list[dict]:
    """
    Get recent commit history.
    
    Args:
        limit: Max number of commits to return
    
    Returns:
        List of commit records
    """
    git_dir = utils.data_dir() / ".git"
    if not git_dir.exists():
        return []
    
    success, stdout, _ = _run_git_command([
        "log", f"-{limit}", "--format=%H|%s|%ai|%an"
    ])
    
    if not success or not stdout.strip():
        return []
    
    commits = []
    for line in stdout.strip().split("\n"):
        parts = line.split("|")
        if len(parts) >= 4:
            commits.append({
                "hash": parts[0][:8],
                "full_hash": parts[0],
                "message": parts[1],
                "date": parts[2],
                "author": parts[3]
            })
    
    return commits
