"""Git operations tools."""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime
from . import utils

def _run_git_command(args: list[str], cwd: Optional[Path] = None,
                     env: Optional[dict] = None, input_text: Optional[str] = None) -> tuple[bool, str, str]:
    """Run a git command and return (success, stdout, stderr). `input_text`: what git reads on
    its stdin (else nothing)."""
    feed = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
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
            **feed,
            # never wait on a password prompt nobody can see: fail instead
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
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

    An existing README.md is left as it is, and an existing .gitignore keeps its lines (the
    ones FlowState needs are added at its end): audit N-F25.

    Returns:
        Git status info
    """
    from .. import privatefs
    # Ensure data directory exists (owner-only when FlowState makes it)
    privatefs.private_dir(utils.data_dir())
    # Check if already initialized
    git_dir = utils.data_dir() / ".git"
    if git_dir.exists():
        return git_status()
    unsafe = unsafe_entries(utils.data_dir())
    if unsafe:
        return {"success": False, "initialized": False, "unsafe": unsafe, "error": UNSAFE_HERE.format(
            paths=_list(unsafe))}
    privatefs.private_dir(utils.data_dir() / "projects")

    # Initialize git
    success, stdout, stderr = _run_git_command(["init"])
    if not success:
        return {"success": False, "error": stderr}

    write_gitignore()

    readme_path = utils.data_dir() / "README.md"
    if not os.path.lexists(readme_path):
        safe_write(readme_path, README)

    # Initial commit: only what may ever be committed (see ALLOWED_TOP)
    _run_git_command(["add", "-A"])
    enforce_index()
    _run_git_command(["commit", "-m", "FlowState initialized"])

    return git_status()


README = """# FlowState Data

This repository contains your FlowState development memory.

**Do not manually edit these files.**

Manage through the FlowState app or MCP server.
"""


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
    if name.lower() in (".gitattributes", ".gitmodules"):   # they change how git writes files
        return False                                         # here (task 56, N6)
    return not (is_secret_file(path) or _NEVER.search(name))


def _required_ignore_lines() -> list:
    return [ln for ln in GITIGNORE.splitlines() if ln.strip() and not ln.startswith("#")]


def write_gitignore() -> None:
    """Write FlowState's .gitignore. One that is already there keeps its own lines; if any line
    FlowState needs is missing, FlowState's whole list is added at its end (later lines win in
    git, so the list keeps its meaning whatever came before it). Audit N-F25."""
    path = utils.data_dir() / ".gitignore"
    if not os.path.lexists(path):
        safe_write(path, GITIGNORE)
        return
    if path.is_symlink() or not path.is_file():
        raise UnsafePath(".gitignore")
    text = gitignore_text(path.read_text(encoding="utf-8", errors="replace"))
    if text is not None:
        safe_write(path, text)


def gitignore_text(current: Optional[str]) -> Optional[str]:
    """FlowState's .gitignore made from `current` (None: there is none): FlowState's list when
    there is none, else `current` with the list added at its end when a line it needs is
    missing. None when `current` already has every line."""
    if current is None:
        return GITIGNORE
    have = {ln.strip() for ln in current.splitlines()}
    need = _required_ignore_lines()
    if all(ln in have for ln in need):
        return None
    sep = "" if current.endswith("\n") or not current else "\n"
    return (current + sep + "\n# Added by FlowState: the files it never commits "
            "(FlowState also checks every commit).\n" + "\n".join(need) + "\n")


# ------------------------------------------------- what may be in the repository (audit N-F12, N-F14)
OK_MODES = ("100644", "100755")   # plain files only: no links, no submodules


class UnsafePath(Exception):
    pass


def safe_write(path: Path, text: str, private: bool = False) -> None:
    """Write a whole file in the data folder without following a link: refused when `path` is
    a link or not a plain file; written to a new file next to it (O_EXCL | O_NOFOLLOW) and moved
    over it, so a link put there in the meantime is replaced, never written through. `private`:
    owner-only (0600; the export, audit N-F28)."""
    path = Path(path)
    if path.is_symlink() or (os.path.lexists(path) and not path.is_file()):
        raise UnsafePath(str(path))
    if path.parent.is_symlink():
        raise UnsafePath(str(path.parent))
    tmp = path.with_name(f".{path.name}.flowstate-{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) \
        | getattr(os, "O_BINARY", 0)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(str(tmp), flags, 0o600 if private else 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        if private:
            from .. import privatefs
            privatefs.lock(tmp)   # Windows: this account only; elsewhere 0600 whatever the umask
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def unsafe_entries(folder: Path) -> list:
    """Links and other non-plain entries where data-git writes or commits: README.md,
    .gitignore, export/ and projects/ (and everything under them). Relative paths; empty when
    all is well. A link there (pushed by anyone who can push to the remote) would make the next
    sync or attachment write somewhere else on this computer (audit N-F12)."""
    folder = Path(folder)
    out = []
    for top in (".gitignore", "README.md"):
        p = folder / top
        if p.is_symlink() or (os.path.lexists(p) and not p.is_file()):
            out.append(top)
    for top in ("export", "projects"):
        p = folder / top
        if not os.path.lexists(p):
            continue
        if p.is_symlink() or not p.is_dir():
            out.append(top)
            continue
        for root, dirs, files in os.walk(p, followlinks=False):
            for name in dirs + files:
                q = Path(root) / name
                if q.is_symlink() or not (q.is_dir() or q.is_file()):
                    out.append(q.relative_to(folder).as_posix())
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
    return out


def tree_entries(rev: str, cwd: Optional[Path] = None) -> list:
    """(mode, path) of every entry in commit `rev` ([] when there is no such commit)."""
    ok, out, _ = _run_git_command(["ls-tree", "-r", "-z", "--full-tree", rev], cwd=cwd)
    if not ok:
        return []
    entries = []
    for rec in out.split("\0"):
        if "\t" in rec:
            meta, path = rec.split("\t", 1)
            entries.append((meta.split()[0], path))
    return entries


def never_tracked_in(rev: str = "HEAD") -> list:
    """Files commit `rev` holds that FlowState never commits (the database, settings, keys):
    a repository from FlowState 2.4.3 or older, or something someone pushed."""
    return [p for _m, p in tree_entries(rev) if not may_commit(p)]


def enforce_index() -> list:
    """Keep everything that may not be committed out of the next commit (it stays on disk).
    A new file is taken out of git's index; a file the last commit already holds (an older
    repository's database) stays exactly as committed: removing it from git would make git
    delete it on every other computer that syncs (audit N-F14), which only `flowstate data-git
    migrate` does, when the user asks. Returns the paths taken out."""
    ok, out, _ = _run_git_command(["ls-files", "-z"])
    bad = [p for p in (out.split("\0") if ok else []) if p and not may_commit(p)]
    if not bad:
        return []
    held = set(never_tracked_in("HEAD"))
    keep = [p for p in bad if p in held]
    drop = [p for p in bad if p not in held]
    for i in range(0, len(keep), 100):
        _run_git_command(["reset", "-q", "HEAD", "--", *keep[i:i + 100]])
    for i in range(0, len(drop), 100):
        _run_git_command(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *drop[i:i + 100]])
    return drop


def _list(paths: list, n: int = 8) -> str:
    more = f" (+{len(paths) - n} more)" if len(paths) > n else ""
    return ", ".join(paths[:n]) + more


LEGACY = ("This data-git repository still holds files FlowState no longer commits, from FlowState "
          "2.4.3 or older: {paths}. Taking them out of git would make git delete them (your "
          "database) on any computer that still runs 2.4.3 and syncs this repository, so nothing "
          "was committed, pulled or pushed. First update FlowState on every computer that syncs "
          "it, then run `flowstate data-git migrate` once on each of them (it backs up the "
          "database first and keeps it on disk).")
INCOMING_NEVER = ("The remote holds files FlowState never syncs (a database, settings or keys): "
                  "{paths}. Pulling them would overwrite this computer's own, so nothing was "
                  "pulled or pushed{local}. If a computer that syncs this repository still runs "
                  "FlowState 2.4.3 or older, update it; then run `flowstate data-git migrate` "
                  "once here.")
INCOMING_UNSAFE = ("The remote holds links or other special entries: {paths}. FlowState syncs "
                   "plain files only (a link could make a later sync write somewhere else on this "
                   "computer), so nothing was pulled or pushed{local}. Remove them from the remote "
                   "repository by hand (git rm), then sync again.")
INCOMING_OVERWRITE = ("The remote holds files that would replace files on this computer that git "
                      "doesn't track: {paths}. Nothing was pulled or pushed{local}. Move those "
                      "files away (or remove them from the remote), then sync again.")
UNSAFE_HERE = ("There are links or other special entries where data-git writes: {paths}. "
               "FlowState writes plain files only (a link could make it write somewhere else on "
               "this computer), so nothing was written, committed or pushed. Look at them and "
               "remove them (and from the remote, if they came from there), then try again.")


def _incoming_problem(tip: str, local: str) -> Optional[dict]:
    """What commit `tip` (the remote's, fetched, not merged) would do here that it must not:
    bring in a database or settings file, a link, or replace a file git doesn't track here
    (git overwrites ignored files without asking, the live database among them)."""
    entries = tree_entries(tip)
    odd = [p for m, p in entries if m not in OK_MODES]
    if odd:
        return {"unsafe": odd, "error": INCOMING_UNSAFE.format(paths=_list(odd), local=local)}
    never = [p for _m, p in entries if not may_commit(p)]
    if never:
        return {"remote_tracks": never,
                "error": INCOMING_NEVER.format(paths=_list(never), local=local)}
    ok, out, _ = _run_git_command(["ls-files", "-z"])
    tracked = set(out.split("\0")) if ok else set()
    folder = utils.data_dir()
    clash = [p for _m, p in entries if p not in tracked and os.path.lexists(folder / p)]
    if clash:
        return {"would_overwrite": clash,
                "error": INCOMING_OVERWRITE.format(paths=_list(clash), local=local)}
    return None


def _safety_copy(db_path: Optional[Path]) -> Optional[str]:
    """A safety copy of the database before git changes files in its folder. None when made
    (or there is no database there); else the reason it failed."""
    if not db_path or not Path(db_path).is_file():
        return None
    from .. import backup
    try:
        backup.backup_now(Path(db_path), label="pre-bulk")
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None


def _tighten_export() -> None:
    """Exports that came in with a pull are owner-only too (audit N-F28)."""
    from .. import privatefs
    out = utils.data_dir() / "export"
    if out.is_dir() and not out.is_symlink():
        privatefs.lock(out)
        for f in out.glob("*.md"):
            if f.is_file() and not f.is_symlink():
                privatefs.lock(f)


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


def _remote_refusal(checker=None) -> Optional[dict]:
    """None when origin is the remote the user set (`flowstate data-git remote`) and it is
    still private; else the refusal."""
    saved = _saved_remote()
    ok, url, _ = _run_git_command(["remote", "get-url", "origin"])
    url = url.strip()
    if not ok or not saved.get("remote") or url != saved["remote"]:
        return {"step": "remote", "refused": True,
                "error": "This remote wasn't set (or was changed) outside `flowstate data-git "
                         "remote`, so nothing was pushed. Set it again with that command; it "
                         "checks that the repository is private."}
    try:
        check_remote(url, saved.get("checked") == "user-said-private", checker)
    except RemoteRefused as e:
        return {"step": "remote", "refused": True, "error": str(e)}
    return None


def _fetch(branch: str) -> tuple:
    """(ok, tip sha or None, error). Fetches the remote's branch without merging it."""
    ok, heads, err = _run_git_command(["ls-remote", "--heads", "origin", branch])
    if not ok:
        return False, None, err
    if not heads.strip():
        return True, None, ""
    ok, _out, err = _run_git_command(["fetch", "-q", "origin", branch])
    if not ok:
        return False, None, err
    ok, sha, err = _run_git_command(["rev-parse", "FETCH_HEAD^{commit}"])
    return (True, sha.strip(), "") if ok and sha.strip() else (False, None, err)


def git_sync(commit_message: Optional[str] = None, checker=None,
             db_path: Optional[Path] = None) -> dict:
    """
    Perform full sync: add all, commit, fetch, rebase onto the remote, push. The user's own
    command (`flowstate data-git sync`); refused from tools.

    Only README.md, projects/ and export/ are ever committed (may_commit). A push goes only to
    the remote the user set with `flowstate data-git remote`, re-checked as private first.

    Never deletes or overwrites the database on any computer (audit N-F14): a repository that
    still holds it (FlowState 2.4.3 or older) is refused until the user runs `flowstate
    data-git migrate`; the remote's commits are checked before they are merged (no database,
    settings, keys, links or replacing files git doesn't track here), and `db_path` (the
    database in this folder) gets a safety copy before git brings anything in.

    Args:
        commit_message: Custom commit message (default: timestamp)

    Returns:
        Sync result with status info
    """
    git_dir = utils.data_dir() / ".git"
    if not git_dir.exists():
        return {"success": False, "error": "Not a git repository. Run git_init first."}
    legacy = never_tracked_in("HEAD")
    if legacy:
        return {"success": False, "step": "legacy", "refused": True, "committed": False,
                "pushed": False, "still_tracked": legacy,
                "error": LEGACY.format(paths=_list(legacy))}
    unsafe = unsafe_entries(utils.data_dir())
    if unsafe:
        return {"success": False, "step": "unsafe", "refused": True, "committed": False,
                "pushed": False, "unsafe": unsafe, "error": UNSAFE_HERE.format(paths=_list(unsafe))}

    # Check for remote
    status = git_status()
    write_gitignore()

    # Add all changes
    success, stdout, stderr = _run_git_command(["add", "-A"])
    if not success:
        return {"success": False, "step": "add", "error": stderr}
    enforce_index()

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

    if not status.get("has_remote"):
        return {"success": True, "committed": has_changes, "pushed": False,
                "message": (commit_message or "FlowState sync") + " (no remote set: committed locally only)"}

    # Only to the remote the user set and FlowState checked, and only while it is still private
    local_only = {"committed": has_changes, "pushed": False}
    refused = _remote_refusal(checker)
    if refused:
        return {"success": False, **local_only, **refused}

    # The remote's side, fetched but not merged: reachable? does the branch exist yet?
    branch = status.get("branch") or "main"
    ok, tip, err = _fetch(branch)
    if not ok:
        return {"success": False, "step": "connect", **local_only, "offline": True,
                "error": "Couldn't reach the remote, so nothing was pushed"
                         + (" (your changes are committed locally)" if has_changes else "")
                         + f". Sync again when it is reachable. Git said: {_last_line(err)}"}
    if tip:   # the remote has this branch: check what it would bring, then bring it in
        local = " (your changes are committed locally)" if has_changes else ""
        problem = _incoming_problem(tip, local)
        if problem:
            return {"success": False, "step": "pull", "refused": True, **local_only, **problem}
        behind = not _run_git_command(["merge-base", "--is-ancestor", tip, "HEAD"])[0]
        if behind:
            why = _safety_copy(db_path)
            if why:
                return {"success": False, "step": "pull", "refused": True, **local_only,
                        "error": "Couldn't make a safety copy of the database before bringing in "
                                 f"the remote's changes, so nothing was pulled or pushed ({why})."}
            ok, out, err = _run_git_command(["rebase", tip])
            if not ok and _rebasing():
                # two computers wrote the same export file: this computer's own copy wins (it
                # is made from this computer's database at every sync anyway); any other clash
                # still stops (task 57, N3)
                ok = _settle_export_clashes()
            if not ok:
                conflict = _rebasing() or "CONFLICT" in (out + err)
                if conflict:
                    _run_git_command(["rebase", "--abort"])
                return {"success": False, "step": "pull", **local_only, "conflict": conflict,
                        "error": ("The remote has changes that clash with yours; nothing was pushed "
                                  "and your data folder is as it was. Resolve it by hand with git."
                                  if conflict else f"git pull failed: {_last_line(err)}")}
            _tighten_export()
            unsafe = unsafe_entries(utils.data_dir())
            if unsafe:   # (the check above makes this impossible; belt and braces)
                return {"success": False, "step": "pull", "refused": True, **local_only,
                        "unsafe": unsafe, "error": UNSAFE_HERE.format(paths=_list(unsafe))}
    # (an empty remote, or a new branch there: the first push creates it)
    ok, out, err = _run_git_command(["push", "-u", "origin", branch])
    if not ok:
        return {"success": False, "step": "push", **local_only,
                "error": f"git push failed, so nothing is on the remote: {_last_line(err)}"}
    return {
        "success": True,
        "committed": has_changes,
        "pushed": True,
        "first_push": not tip,
        "message": commit_message or "FlowState sync"
    }


def _clashing() -> list:
    ok, out, _e = _run_git_command(["diff", "--name-only", "--diff-filter=U", "-z"])
    return [p for p in out.split("\0") if p] if ok else []


def _is_export(path: str) -> bool:
    return path.startswith("export/") and path.endswith(".md") and "/" not in path[len("export/"):]


def _settle_export_clashes(rounds: int = 200) -> bool:
    """During `git rebase` onto the remote: settle every clash that is only about export/*.md
    in favour of this computer's copy (in a rebase, "theirs" is the local commit being
    replayed), and go on. False (the caller aborts) when another file clashes, or git fails."""
    for _ in range(rounds):
        if not _rebasing():
            return True
        clash = _clashing()
        if not clash or not all(_is_export(p) for p in clash):
            return False
        for p in clash:
            if _run_git_command(["checkout", "--theirs", "--", p])[0]:
                if not _run_git_command(["add", "--", p])[0]:
                    return False
            elif not _run_git_command(["rm", "-q", "-f", "--", p])[0]:   # this side removed it
                return False
        env = {"GIT_EDITOR": "true"}
        ok, out, err = _run_git_command(["rebase", "--continue"], env=env)
        if not ok and _rebasing() and not _clashing():   # the commit had nothing else left
            ok, out, err = _run_git_command(["rebase", "--skip"], env=env)
        if not ok and not _rebasing():
            return False
    return not _rebasing()


MIGRATE_MSG = ("FlowState data-git migrate: the database, settings and keys are no longer kept "
               "in git (they stay on each computer)")


def git_migrate(checker=None, db_path: Optional[Path] = None) -> dict:
    """`flowstate data-git migrate` (the user's own command, after updating every computer that
    syncs): take the files FlowState never commits (an older version's database, settings) out
    of the repository once, without touching any file on this disk.

    The new commit is the remote's newest one (or this computer's, without a remote) minus those
    files; this computer's branch moves to it without changing the working folder, files it
    holds that are missing here are restored, and it is pushed. This computer's own commits that
    weren't pushed yet are left out (they may hold the database); their files stay on disk and
    the next sync commits them. A safety copy of the database is made first."""
    git_dir = utils.data_dir() / ".git"
    if not git_dir.exists():
        return {"success": False, "error": "Not a git repository. Run `flowstate data-git init` first."}
    folder = utils.data_dir()
    unsafe = unsafe_entries(folder)
    if unsafe:
        return {"success": False, "refused": True, "unsafe": unsafe,
                "error": UNSAFE_HERE.format(paths=_list(unsafe))}
    ok, head, _ = _run_git_command(["rev-parse", "--verify", "-q", "HEAD^{commit}"])
    head = head.strip() if ok else ""
    status = git_status()
    branch = status.get("branch") or "main"
    tip = None
    if status.get("has_remote"):
        refused = _remote_refusal(checker)
        if refused:
            return {"success": False, "pushed": False, **refused}
        ok, tip, err = _fetch(branch)
        if not ok:
            return {"success": False, "step": "connect", "offline": True, "pushed": False,
                    "error": "Couldn't reach the remote, so nothing was changed. Run it again "
                             f"when it is reachable. Git said: {_last_line(err)}"}
        if tip:
            odd = [p for m, p in tree_entries(tip) if m not in OK_MODES]
            if odd:
                return {"success": False, "refused": True, "unsafe": odd, "pushed": False,
                        "error": INCOMING_UNSAFE.format(paths=_list(odd), local="")}
    base = tip or head
    if not base:
        return {"success": True, "untracked": [], "pushed": False,
                "message": "Nothing to migrate: the repository has no commits yet."}
    drop = never_tracked_in(base)
    if not drop and not never_tracked_in("HEAD"):
        return {"success": True, "untracked": [], "pushed": False,
                "message": "Nothing to migrate: the repository holds no database or settings files."}
    why = _safety_copy(db_path)
    if why:
        return {"success": False, "refused": True, "pushed": False,
                "error": f"Couldn't make a safety copy of the database first, so nothing was "
                         f"changed ({why})."}
    new = base
    if drop:   # build the commit in a separate index: the working folder is never touched
        idx = git_dir / "flowstate-migrate.index"
        env = {"GIT_INDEX_FILE": str(idx)}
        try:
            idx.unlink(missing_ok=True)
            # -f: this index is FlowState's own, freshly read from `base`; without it git compares
            # it with the files on disk and refuses when this computer is behind (task 57, N1)
            steps = [["read-tree", base]] + [
                ["rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--", *drop[i:i + 100]]
                for i in range(0, len(drop), 100)]
            for args in steps:
                ok, _o, err = _run_git_command(args, env=env)
                if not ok:
                    return {"success": False, "pushed": False, "error": f"git failed: {_last_line(err)}"}
            # FlowState's .gitignore goes into the same commit, so an older FlowState that pulls
            # it doesn't commit its database again (task 57, N2)
            why = _commit_gitignore(base, env)
            if why:
                return {"success": False, "pushed": False, "error": f"git failed: {why}"}
            ok, tree, err = _run_git_command(["write-tree"], env=env)
            if not ok:
                return {"success": False, "pushed": False, "error": f"git failed: {_last_line(err)}"}
            ok, new, err = _run_git_command(["commit-tree", tree.strip(), "-p", base, "-m", MIGRATE_MSG])
            if not ok:
                return {"success": False, "pushed": False, "error": f"git failed: {_last_line(err)}"}
            new = new.strip()
        finally:
            idx.unlink(missing_ok=True)
    # the branch moves; the index follows; the files on disk stay exactly as they are
    ok, _o, err = _run_git_command(["reset", "-q", new])
    if not ok:
        return {"success": False, "pushed": False, "error": f"git failed: {_last_line(err)}"}
    missing = [p for _m, p in tree_entries(new) if not os.path.lexists(folder / p)]
    for i in range(0, len(missing), 100):
        _run_git_command(["checkout", new, "--", *missing[i:i + 100]])
    write_gitignore()
    out = {"success": True, "untracked": drop or (never_tracked_in(head) if head else []),
           "pushed": False, "restored": missing}
    if tip and drop:
        ok, _o, err = _run_git_command(["push", "origin", f"{new}:refs/heads/{branch}"])
        if not ok:
            out.update(success=False, step="push",
                       error="The remote changed while migrating, so nothing was pushed. Run "
                             f"`flowstate data-git migrate` again. Git said: {_last_line(err)}")
            return out
        out["pushed"] = True
    return out


def _commit_gitignore(base: str, env: dict) -> Optional[str]:
    """Put FlowState's .gitignore (base's own lines kept, FlowState's list added if a line is
    missing) into the index `env` names. None when done or nothing was needed, else git's error."""
    ok, kind, _e = _run_git_command(["cat-file", "-t", f"{base}:.gitignore"])
    current = None
    if ok and kind.strip() == "blob":
        ok, current, err = _run_git_command(["cat-file", "blob", f"{base}:.gitignore"])
        if not ok:
            return _last_line(err)
    text = gitignore_text(current)
    if text is None:
        return None
    ok, sha, err = _run_git_command(["hash-object", "-w", "--stdin"], input_text=text)
    if not ok:
        return _last_line(err)
    ok, _o, err = _run_git_command(["update-index", "--add", "--cacheinfo",
                                    f"100644,{sha.strip()},.gitignore"], env=env)
    return None if ok else _last_line(err)


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
    
    # Clone without checking anything out, look at what it holds, then check it out
    from .. import privatefs
    made = not target_path.exists()
    privatefs.private_dir(target_path.parent)
    success, stdout, stderr = _run_git_command(
        ["clone", "--no-checkout", remote_url, str(target_path)],
        cwd=target_path.parent
    )

    if not success:
        return {"success": False, "error": stderr}

    out = {"success": True, "path": str(target_path), "remote_url": remote_url}
    # links (they would make FlowState write elsewhere on this computer) and .gitattributes /
    # .gitmodules are refused before anything is written (task 56, N6): nothing is checked out
    entries = tree_entries("HEAD", cwd=target_path)
    odd = [p for m, p in entries if m not in OK_MODES or
           p.rsplit("/", 1)[-1].lower() in (".gitattributes", ".gitmodules")]
    if odd:
        import shutil
        shutil.rmtree(target_path / ".git", ignore_errors=True)
        if made:
            shutil.rmtree(target_path, ignore_errors=True)
        return {"success": False, "refused": True, "unsafe": odd,
                "error": "The repository holds links or git settings files FlowState won't "
                         f"write: {_list(odd)}. Nothing was cloned; remove them from the "
                         "repository first."}
    if entries:
        ok, _o, err = _run_git_command(["checkout", "-q", "-f", "HEAD"], cwd=target_path)
        if not ok:
            return {"success": False, "error": f"git checkout failed: {_last_line(err)}"}
    # owner-only at once, not only after the first sync (task 56, N-F28 gap)
    privatefs.lock(target_path)
    for sub in ("export", "projects"):
        d = target_path / sub
        if d.is_dir() and not d.is_symlink():
            privatefs.lock(d)
            for f in d.rglob("*"):
                if not f.is_symlink():
                    privatefs.lock(f)
    return out



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
