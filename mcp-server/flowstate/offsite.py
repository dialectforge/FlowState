"""Optional off-site backup to a PRIVATE GitHub repository.

Safety rules, all enforced here:
- Before every push the repository's visibility is checked (gh, or the GitHub API with
  the user's own git credential held in memory only). Anything but private: refused.
  Can't tell: refused.
- Only an allow-list of files is ever pushed: the backup database and a manifest. The
  commit's file list is compared with the allow-list before pushing.
- The push goes to a dedicated branch (default `flowstate-backup`) as a single fresh
  commit (force-pushed), so `main` is never touched and old copies don't pile up.
- FlowState never stores a GitHub token; pushes use the user's gh / git credentials.
"""

import hashlib
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import backup as B
from . import paths

ALLOWED = ("flowstate-backup.db", "manifest.json")
DEFAULT_BRANCH = "flowstate-backup"
MAX_BYTES = 95 * 1024 * 1024  # GitHub refuses files over 100 MB
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class OffsiteError(Exception):
    pass


def check_repo_name(repo: str) -> str:
    repo = (repo or "").strip()
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    repo = repo.removesuffix(".git").strip("/")
    if not _REPO.match(repo) or ".." in repo:
        raise OffsiteError(f"not a GitHub owner/name: {repo!r}")
    return repo


def _run(cmd: list, cwd: Optional[Path] = None, input: Optional[str] = None,
         timeout: int = 120) -> subprocess.CompletedProcess:
    # no input: stdin is closed, never inherited (it may be a pipe; on Windows git then blocks)
    extra = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True,
                          capture_output=True, timeout=timeout, **extra)


# ---------------------------------------------------------------- visibility
def visibility_via_gh(repo: str, run=_run) -> Optional[str]:
    if shutil.which("gh") is None:
        return None
    r = run(["gh", "repo", "view", repo, "--json", "visibility"])
    if r.returncode != 0:
        return None
    try:
        return str(json.loads(r.stdout)["visibility"]).upper()
    except (ValueError, KeyError, TypeError):
        return None


def visibility_via_api(repo: str, run=_run, opener=urllib.request.urlopen) -> Optional[str]:
    """GitHub API with the user's existing git credential (asked from git, kept in memory)."""
    if shutil.which("git") is None:
        return None
    r = run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
            timeout=30)
    token = ""
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if line.startswith("password="):
                token = line[len("password="):]
    if not token:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "flowstate-backup"})
    try:
        with opener(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError):
        return None
    finally:
        token = ""
    if "visibility" in data:
        return str(data["visibility"]).upper()
    if "private" in data:
        return "PRIVATE" if data["private"] else "PUBLIC"
    return None


def repo_visibility(repo: str) -> Optional[str]:
    return visibility_via_gh(repo) or visibility_via_api(repo)


def require_private(repo: str, checker: Optional[Callable[[str], Optional[str]]] = None) -> str:
    vis = (checker or repo_visibility)(repo)
    if vis is None:
        raise OffsiteError(
            f"could not confirm that github.com/{repo} is private (is gh logged in, or do "
            f"git credentials for github.com exist?). Nothing was pushed.")
    if vis != "PRIVATE":
        raise OffsiteError(f"github.com/{repo} is {vis.lower()}, not private. Refusing to push "
                           f"a backup there. Nothing was pushed.")
    return vis


# ---------------------------------------------------------------- push
def _git(args: list, cwd: Path, run=_run) -> str:
    r = run(["git", *args], cwd=cwd, timeout=600)
    if r.returncode != 0:
        # git's stderr can name the remote URL; it never contains a token we hold.
        raise OffsiteError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def push_backup(db_path: Path, repo: str, staging: Optional[Path] = None,
                branch: str = DEFAULT_BRANCH, remote_url: Optional[str] = None,
                checker: Optional[Callable[[str], Optional[str]]] = None,
                dry_run: bool = False, run=_run) -> dict:
    """Back up `db_path` and push it (with a manifest) to `repo`'s backup branch."""
    repo = check_repo_name(repo)
    if not re.match(r"^[A-Za-z0-9._/-]{1,100}$", branch) or branch in ("main", "master"):
        raise OffsiteError(f"refusing to push backups to branch {branch!r}")
    require_private(repo, checker)  # before anything else, every time
    if dry_run:
        return {"repo": repo, "visibility": "PRIVATE", "pushed": False, "dry_run": True}

    staging = Path(staging) if staging else paths.backup_dir() / "offsite-staging"
    staging.mkdir(parents=True, exist_ok=True)
    url = remote_url or f"https://github.com/{repo}.git"
    if not (staging / ".git").exists():
        _git(["init", "-q"], staging, run)
    # Fresh orphan state each run: the commit holds the allow-list and nothing else.
    for p in staging.iterdir():
        if p.name != ".git":
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
    dbfile = staging / ALLOWED[0]
    tmp_dir = staging / ".git" / "flowstate-tmp"
    tmp_dir.mkdir(exist_ok=True)
    made = B.backup_now(db_path, tmp_dir, prune=False)["path"]
    shutil.move(str(made), dbfile)
    for p in tmp_dir.iterdir():
        p.unlink()
    size = dbfile.stat().st_size
    if size > MAX_BYTES:
        raise OffsiteError(f"backup is {size // (1024 * 1024)} MB; GitHub's limit is 100 MB. "
                           f"Nothing was pushed.")
    manifest = {
        "service": "flowstate", "kind": "database backup",
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file": ALLOWED[0], "bytes": size, "sha256": _sha256(dbfile),
        "integrity_check": B.integrity(dbfile), "tables": B.table_counts(dbfile),
    }
    (staging / ALLOWED[1]).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    _git(["checkout", "-q", "--orphan", "flowstate-tmp-" + datetime.now().strftime("%H%M%S%f")],
         staging, run)
    _git(["read-tree", "--empty"], staging, run)  # empty index: nothing carried over
    _git(["add", "--", *ALLOWED], staging, run)
    staged = set(_git(["diff", "--cached", "--name-only"], staging, run).split())
    if staged != set(ALLOWED):
        raise OffsiteError(f"staged files are not exactly the allow-list ({sorted(staged)}); "
                           f"nothing was pushed")
    _git(["-c", "user.name=FlowState backup", "-c", "user.email=flowstate-backup@localhost",
          "commit", "-q", "-m", f"FlowState backup {manifest['created_utc']}"], staging, run)
    tree = set(_git(["ls-tree", "-r", "--name-only", "HEAD"], staging, run).split())
    if tree != set(ALLOWED):
        raise OffsiteError(f"commit contains files outside the allow-list ({sorted(tree)}); "
                           f"nothing was pushed")
    require_private(repo, checker)  # again, right before the push
    _git(["push", "--force", "-q", url, f"HEAD:refs/heads/{branch}"], staging, run)
    # Keep the staging repo small: drop the local branch history.
    head = _git(["rev-parse", "HEAD"], staging, run).strip()
    for b in _git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
                  staging, run).split():
        if b != _git(["rev-parse", "--abbrev-ref", "HEAD"], staging, run).strip():
            _git(["branch", "-q", "-D", b], staging, run)
    return {"repo": repo, "branch": branch, "visibility": "PRIVATE", "pushed": True,
            "commit": head[:12], "bytes": size, "files": list(ALLOWED)}
