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
    
    write_gitignore()
    
    # Create README
    readme_content = """# FlowState Data

This repository contains your FlowState development memory.

**Do not manually edit these files.**

Manage through the FlowState app or MCP server.
"""
    readme_path = utils.data_dir() / "README.md"
    readme_path.write_text(readme_content)
    
    # Initial commit: only what may ever be committed (see ALLOWED_TOP)
    _run_git_command(["add", "-A"])
    enforce_index()
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
# The database and its side files, settings and FlowState's own state: never committed, even
# inside projects/ (2.4.4, audit D1). The database goes into data-git only as the markdown
# export `flowstate data-git sync` writes into export/.
_NEVER = re.compile(
    r"(\.db$|\.db-(wal|shm|journal)$|\.sqlite3?(-(wal|shm|journal))?$|-wal$|-shm$|-journal$|"
    r"^remote-auth|^config\.json$|^update\.json$|^relay\.json$|^device-token|\.fsenc$)", re.I)
# Only these top-level entries are ever committed: the rest of the data folder (the database,
# backups/, relay/, config.json, remote-auth.db, update.json, review-page/, ...) stays out.
ALLOWED_TOP = (".gitignore", "README.md", "projects", "export")

GITIGNORE = """# Written by FlowState. Only README.md, projects/ (attachments) and export/ (the markdown
# export `flowstate data-git sync` writes) are ever committed. The database, its -wal/-shm
# files, backups/, relay/ (device token, TLS key), config.json, remote-auth.db and everything
# else in this folder stay out. FlowState rewrites this file and checks every commit.
/*
!/.gitignore
!/README.md
!/projects/
!/export/
*.db
*.db-*
*.sqlite*
*-wal
*-shm
*-journal
*.pem
*.key
*.env
*.log
.DS_Store
Thumbs.db
*.tmp
*.bak
"""


def is_secret_file(path: str) -> bool:
    """File names that must never be committed from the data folder."""
    return bool(_SECRET_FILE.search(path.replace("\\", "/").rsplit("/", 1)[-1]))


def may_commit(path: str) -> bool:
    """Only README.md, .gitignore and files under projects/ or export/, and never a database,
    settings, key, token or log file wherever it sits (audit D1)."""
    path = path.replace("\\", "/").strip().strip("/")
    name = path.rsplit("/", 1)[-1]
    top = path.split("/", 1)[0]
    if top not in ALLOWED_TOP or (top in (".gitignore", "README.md") and path != top):
        return False
    return not (is_secret_file(path) or _NEVER.search(name))


def write_gitignore() -> None:
    (utils.data_dir() / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def enforce_index() -> list:
    """Take everything that may not be committed out of git's index (it stays on disk), so
    an older repository that tracked the database stops doing so with the next commit.
    Returns the paths taken out."""
    ok, out, _ = _run_git_command(["ls-files", "-z"])
    bad = [p for p in (out.split("\0") if ok else []) if p and not may_commit(p)]
    for i in range(0, len(bad), 100):
        _run_git_command(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *bad[i:i + 100]])
    return bad


# -------------------------------------------------------------- remotes (the user's own)
USER_ONLY = ("Only the user can {what}: it sends FlowState data off this computer. Tell the "
             "user to run `flowstate data-git {cmd}` in their own terminal (it asks them to "
             "confirm). Nothing was changed.")

_GITHUB = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                     r"([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")


def github_repo(url: str) -> Optional[str]:
    """owner/name for a github.com remote URL, else None."""
    m = _GITHUB.match((url or "").strip())
    return m.group(1) if m and ".." not in m.group(1) else None


class RemoteRefused(Exception):
    pass


def check_remote(url: str, allow_unchecked: bool = False, checker=None) -> dict:
    """Is `url` a remote data-git may push to? A github.com repository must be private (the
    off-site backup's check: gh, or the GitHub API with the user's own credential). Any other
    remote can't be checked, so only with allow_unchecked (the user's own --unchecked)."""
    from .. import offsite
    repo = github_repo(url)
    if repo:
        try:
            offsite.require_private(repo, checker)
        except offsite.OffsiteError as e:
            raise RemoteRefused(str(e).replace("push a backup", "sync FlowState data")) from None
        return {"remote": url.strip(), "checked": "github-private", "repo": repo}
    if not allow_unchecked:
        raise RemoteRefused(
            "FlowState can only check that a github.com repository is private. For any other "
            "remote, run the command again with --unchecked once you are sure only you can "
            "read it.")
    return {"remote": url.strip(), "checked": "user-said-private"}


def _saved_remote() -> dict:
    from .. import paths
    return paths.load_config().get("data_git") or {}


def git_set_remote(remote_url: str, allow_unchecked: bool = False, checker=None) -> dict:
    """Set the remote (the user's own command, `flowstate data-git remote`; refused from
    tools). Checked first (check_remote) and saved in config.json, which sync compares with
    git's own remote every time."""
    git_dir = utils.data_dir() / ".git"
    if not git_dir.exists():
        return {"success": False, "error": "Not a git repository. Run git_init first."}
    try:
        entry = check_remote(remote_url, allow_unchecked, checker)
    except RemoteRefused as e:
        return {"success": False, "refused": True, "error": str(e)}

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
    from .. import paths
    paths.update_config(lambda cfg: cfg.__setitem__("data_git", entry))
    return {"success": True, "remote_url": remote_url, "checked": entry["checked"]}


def git_sync(commit_message: Optional[str] = None, checker=None) -> dict:
    """
    Perform full sync: add all, commit, pull --rebase, push. The user's own command
    (`flowstate data-git sync`); refused from tools.

    Only README.md, projects/ and export/ are ever committed (may_commit). A push goes only to
    the remote the user set with `flowstate data-git remote`, re-checked as private first.

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
    write_gitignore()
    
    # Add all changes
    success, stdout, stderr = _run_git_command(["add", "-A"])
    if not success:
        return {"success": False, "step": "add", "error": stderr}
    untracked = enforce_index()

    # Never commit tokens, keys, .env files or logs, even if .gitignore misses them.
    _, staged, _ = _run_git_command(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    risky = [p for p in staged.splitlines() if p.strip() and is_secret_file(p.strip())]
    if risky:
        _run_git_command(["reset", "-q", "--", *risky])
        return {"success": False, "step": "add", "refused": risky,
                "error": "These files look like secrets or logs and were not committed. Move "
                         "them out of the data folder or add them to .gitignore, then sync again."}

    # Check if there's anything to commit
    success, stdout, _ = _run_git_command(["diff", "--cached", "--name-only"])
    has_changes = bool(stdout.strip())
    
    # Commit if there are changes
    if has_changes:
        msg = commit_message or f"FlowState sync - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        success, stdout, stderr = _run_git_command(["commit", "-m", msg])
        if not success:
            return {"success": False, "step": "commit", "error": stderr}
    extra = {"no_longer_tracked": untracked} if untracked else {}
    
    if not status.get("has_remote"):
        return {"success": True, "committed": has_changes, "pushed": False, **extra,
                "message": (commit_message or "FlowState sync") + " (no remote set: committed locally only)"}

    # Only to the remote the user set and FlowState checked, and only while it is still private
    local_only = {"committed": has_changes, "pushed": False, **extra}
    saved = _saved_remote()
    ok, url, _ = _run_git_command(["remote", "get-url", "origin"])
    url = url.strip()
    if not ok or not saved.get("remote") or url != saved["remote"]:
        return {"success": False, "step": "remote", "refused": True, **local_only,
                "error": "This remote wasn't set (or was changed) outside `flowstate data-git "
                         "remote`, so nothing was pushed. Set it again with that command; it "
                         "checks that the repository is private."}
    try:
        check_remote(url, saved.get("checked") == "user-said-private", checker)
    except RemoteRefused as e:
        return {"success": False, "step": "remote", "refused": True, **local_only, "error": str(e)}

    # The remote's side, without changing anything: reachable? does the branch exist yet?
    branch = status.get("branch") or "main"
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
        # what came in may not be committed here either; it stays out of the next commit
        enforce_index()
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
        **extra,
        "message": commit_message or "FlowState sync"
    }


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "no details")[:200]


def _rebasing() -> bool:
    g = utils.data_dir() / ".git"
    return (g / "rebase-merge").exists() or (g / "rebase-apply").exists()


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
