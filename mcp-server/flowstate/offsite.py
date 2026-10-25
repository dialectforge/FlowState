"""Optional off-site backup to a PRIVATE GitHub repository.

Safety rules, all enforced here:
- Before every push the repository's visibility is checked (gh, or the GitHub API with
  the user's own git credential held in memory only). Anything but private: refused.
  Can't tell: refused.
- Only an allow-list of files is ever pushed: the backup database, encrypted on this computer
  (offsite_crypto.py, AES-256-GCM; 2.4.4, audit D8), and a manifest. The commit's file list is
  compared with the allow-list before pushing. An unencrypted copy is never pushed.
- The staging folder (a fresh git repository each night) is deleted before and after every
  push attempt, pushed or not, and by `offsite setup` / `offsite disable`, so no extra copy of
  the database stays behind (audit D11, re-audit N-F37). Read-only files in it (git's objects;
  Windows refuses to delete those) are made writable and deleted, and the folder is checked to
  be really gone (N-F13).
- The manifest pushed beside the encrypted file says only: format, when the copy was made, the
  encrypted file's size and SHA-256 (re-audit N-F35). Nothing about the database's contents.
- A copy already on GitHub that is newer than this one is not overwritten (another computer
  pushing to the same repository, or a wrong clock) unless `replace_newer` (re-audit N-F34).
- The push goes to a dedicated branch (default `flowstate-backup`) as a single fresh
  commit (force-pushed), so `main` is never touched and old copies don't pile up.
- FlowState never stores a GitHub token; pushes use the user's gh / git credentials.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import backup as B
from . import paths

ALLOWED = ("flowstate-backup.db.fsenc", "manifest.json")
DEFAULT_BRANCH = "flowstate-backup"
MAX_BYTES = 95 * 1024 * 1024  # GitHub refuses files over 100 MB
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


MANIFEST_FORMAT = 2
log = logging.getLogger("flowstate.offsite")


class OffsiteError(Exception):
    pass


# ---------------------------------------------------------------- staging folder
def _clear_readonly(func, path, _exc):
    """rmtree error handler: git marks its object files read-only and Windows won't delete
    those; make it writable and try once more (re-audit N-F13)."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if os.path.isdir(path) else 0))
        func(path)
    except OSError:
        pass


def remove_tree(path: Path) -> bool:
    """Delete `path` completely. True if it is gone afterwards (checked)."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return True
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
        return not path.exists() and not path.is_symlink()
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly)
    else:
        shutil.rmtree(path, onerror=_clear_readonly)
    return not path.exists()


def staging_dir() -> Path:
    return paths.backup_dir() / "offsite-staging"


def remove_staging(staging: Optional[Path] = None) -> Optional[str]:
    """Delete the off-site staging folder (it may hold unencrypted copies left by 2.4.3, or by
    a crash). None when it is gone; else a warning line for the user."""
    staging = Path(staging) if staging else staging_dir()
    if remove_tree(staging):
        return None
    msg = (f"FlowState couldn't fully delete its off-site staging folder {staging}; it may "
           f"still hold an unencrypted copy of your database. Delete that folder yourself.")
    log.warning(msg)
    return msg


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
                dry_run: bool = False, run=_run, key: Optional[bytes] = None,
                replace_newer: bool = False) -> dict:
    """Back up `db_path`, encrypt it, and push it (with a manifest) to `repo`'s backup branch.
    `key`: the encryption key (default: the one in the OS credential store, made the first
    time; the answer's `new_key` says so). `replace_newer`: push even when the copy on GitHub
    is newer than this one. The answer's `key_changed` (the previous key id) says the key is
    not the one used for the last push."""
    from . import offsite_crypto as OC
    staging = Path(staging) if staging else staging_dir()
    leftover = remove_staging(staging)   # every attempt, before anything else (N-F37)
    repo = check_repo_name(repo)
    if not re.match(r"^[A-Za-z0-9._/-]{1,100}$", branch) or branch in ("main", "master"):
        raise OffsiteError(f"refusing to push backups to branch {branch!r}")
    require_private(repo, checker)  # before anything else, every time
    if dry_run:
        return {"repo": repo, "visibility": "PRIVATE", "pushed": False, "dry_run": True,
                **({"staging_left": leftover} if leftover else {})}
    made_key = False
    try:
        if key is None:
            key, made_key = OC.get_or_create_key()
        OC._aes(key)   # the package is there, before anything is copied
    except OC.CryptoError as e:
        raise OffsiteError(str(e)) from None
    except Exception as e:  # noqa: BLE001 - the credential store refused: never push unencrypted
        raise OffsiteError(f"couldn't get the off-site encryption key from the credential "
                           f"store ({type(e).__name__}); nothing was pushed") from None

    work = staging
    if leftover:   # an old folder that won't go: work in a fresh one beside it, and say so
        import secrets
        work = staging.with_name(f"{staging.name}-{secrets.token_hex(4)}")
    try:
        res = _push(db_path, repo, work, branch, remote_url, checker, run, key, made_key,
                    replace_newer)
    finally:   # never leave a copy behind, pushed or not (audit D11)
        left_now = remove_staging(work)
    leftover = leftover or left_now
    if leftover:
        res["staging_left"] = leftover
    res.update(_note_key(OC.key_id(key), res.get("created_utc")))
    return res


def _note_key(kid: str, when: Optional[str]) -> dict:
    """Remember which key the last push used; {"key_changed": old id} when it differs (N-F9)."""
    out = {}

    def change(cfg):
        off = cfg.get("offsite")
        if not isinstance(off, dict):
            return
        old = off.get("pushed_key")
        if old and old != kid:
            out["key_changed"] = old
        off["pushed_key"] = off["key_in_use"] = kid
        if when:
            off["last_push_utc"] = when
    try:
        paths.update_config(change)
    except Exception as e:  # noqa: BLE001 - only a reminder; the push itself is done
        log.warning("couldn't note the off-site key in the settings: %s", type(e).__name__)
    return out


def _remote_time(url: str, branch: str, staging: Path, run) -> Optional[str]:
    """When the copy on the backup branch was made (from its commit message), without
    downloading it (a partial fetch of the newest commit only). None: no copy there, or this
    git can't tell; the push then goes ahead as before."""
    try:
        _git(["remote", "add", "origin", url], staging, run)
        _git(["config", "extensions.partialClone", "origin"], staging, run)
        _git(["config", "remote.origin.promisor", "true"], staging, run)
        r = run(["git", "fetch", "-q", "--depth", "1", "--filter=blob:none", "--no-tags",
                 "origin", f"refs/heads/{branch}"], cwd=staging, timeout=600)
        if r.returncode != 0:
            return None
        r = run(["git", "log", "-1", "--format=%s", "FETCH_HEAD"], cwd=staging, timeout=60)
    except (OffsiteError, OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"FlowState backup (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)", r.stdout or "")
    return m.group(1) if r.returncode == 0 and m else None


def _push(db_path, repo, staging, branch, remote_url, checker, run, key, made_key,
          replace_newer=False) -> dict:
    from . import offsite_crypto as OC
    from .privatefs import private_dir
    private_dir(staging)
    url = remote_url or f"https://github.com/{repo}.git"
    _git(["init", "-q"], staging, run)
    encfile = staging / ALLOWED[0]
    tmp_dir = staging / ".git" / "flowstate-tmp"
    tmp_dir.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    there = _remote_time(url, branch, staging, run)
    if there and there > created and not replace_newer:
        raise OffsiteError(
            f"the copy on github.com/{repo} (made {there}) is newer than this one ({created}). "
            f"Another computer may be pushing to the same repository, or this computer's clock "
            f"is wrong. Nothing was pushed; to replace it anyway: flowstate offsite push "
            f"--replace-newer")
    made = B.backup_now(db_path, tmp_dir, prune=False)["path"]
    try:
        OC.encrypt_file(made, encfile, key, created=int(now.timestamp()))
    except OC.CryptoError as e:
        raise OffsiteError(str(e)) from None
    finally:
        for p in tmp_dir.iterdir():
            p.unlink()
    size = encfile.stat().st_size
    if size > MAX_BYTES:
        raise OffsiteError(f"backup is {size // (1024 * 1024)} MB; GitHub's limit is 100 MB. "
                           f"Nothing was pushed.")
    # the only unencrypted words on GitHub: format, date, size and checksum (N-F35); the
    # database is checked when it is restored (integrity check, FlowState tables)
    manifest = {"format": MANIFEST_FORMAT, "created_utc": created, "bytes": size,
                "sha256": _sha256(encfile)}
    (staging / ALLOWED[1]).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    _git(["checkout", "-q", "--orphan", "flowstate-tmp-" + datetime.now().strftime("%H%M%S%f")],
         staging, run)
    _git(["read-tree", "--empty"], staging, run)  # empty index: nothing carried over
    _git(["add", "--", *ALLOWED], staging, run)
    staged = set(_git(["diff", "--cached", "--name-only"], staging, run).split())
    if staged != set(ALLOWED):
        raise OffsiteError(f"staged files are not exactly the allow-list ({sorted(staged)}); "
                           f"nothing was pushed")
    if not OC.is_encrypted(encfile):
        raise OffsiteError("the backup file isn't encrypted; nothing was pushed")
    _git(["-c", "user.name=FlowState backup", "-c", "user.email=flowstate-backup@localhost",
          "commit", "-q", "-m", f"FlowState backup {manifest['created_utc']}"], staging, run)
    tree = set(_git(["ls-tree", "-r", "--name-only", "HEAD"], staging, run).split())
    if tree != set(ALLOWED):
        raise OffsiteError(f"commit contains files outside the allow-list ({sorted(tree)}); "
                           f"nothing was pushed")
    require_private(repo, checker)  # again, right before the push
    _git(["push", "--force", "-q", url, f"HEAD:refs/heads/{branch}"], staging, run)
    head = _git(["rev-parse", "HEAD"], staging, run).strip()
    return {"repo": repo, "branch": branch, "visibility": "PRIVATE", "pushed": True,
            "commit": head[:12], "bytes": size, "files": list(ALLOWED), "encrypted": True,
            "key_id": OC.key_id(key), "new_key": made_key, "created_utc": created}


def read_manifest(text: str) -> dict:
    """A pushed manifest, old (2.4.4 before N-F35: also key id, table counts, sizes) or new,
    as {"created_utc", "bytes", "sha256"} (what can be checked against the encrypted file)."""
    m = json.loads(text)
    if not isinstance(m, dict) or not m.get("sha256"):
        raise OffsiteError("not a FlowState off-site manifest")
    return {"created_utc": m.get("created_utc"), "bytes": m.get("bytes"), "sha256": m["sha256"]}


def check_against_manifest(encfile: Path, manifest_text: str) -> dict:
    """The encrypted file matches its manifest (size and SHA-256); else OffsiteError."""
    m = read_manifest(manifest_text)
    if m["bytes"] is not None and Path(encfile).stat().st_size != m["bytes"]:
        raise OffsiteError("the encrypted copy's size doesn't match its manifest")
    if _sha256(Path(encfile)) != m["sha256"]:
        raise OffsiteError("the encrypted copy's checksum doesn't match its manifest")
    return m


def code_needed(cfg: dict) -> bool:
    """The user hasn't seen the recovery code of the key in use (N-F9: tied to the key's id,
    not one yes/no flag). Key in use: the one the last push used, or whose code was shown."""
    if not cfg.get("repo"):
        return False
    shown = cfg.get("recovery_code_key")
    if not shown:   # an earlier 2.4.4 build kept only a flag: trusted until a push names a key
        return not (cfg.get("recovery_code_shown") and not cfg.get("key_in_use"))
    return bool(cfg.get("key_in_use")) and cfg["key_in_use"] != shown


def key_line() -> Optional[str]:
    """Off-site copies are encrypted (2.4.4) and the user hasn't seen this key's recovery code."""
    from . import paths
    cfg = paths.load_config().get("offsite") or {}
    if not code_needed(cfg):
        return None
    if cfg.get("recovery_code_key"):
        return ("FlowState: the off-site encryption key changed since the user saved its "
                "recovery code. Tell the user once: run `flowstate offsite key` in their own "
                "terminal and save the NEW recovery code (keep the old one too: copies made "
                "before need it). You can't run it for them.")
    return ("FlowState: the nightly off-site copy is now encrypted. Tell the user once: run "
            "`flowstate offsite key` in their own terminal and save the recovery code it shows "
            "(you can't run it for them; without it another computer can't read the copy).")
