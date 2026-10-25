"""Owner-only files and folders (2.4.4, data security audit D3, D9, D10). Standard library only.

What FlowState creates from now on (the data and backup folders, the database, backups,
restores, markdown exports, token files, settings) is readable by this account only:

- macOS / Linux: folders 0700, files 0600. SQLite gives a database's -wal / -shm files the
  database file's own mode, so they follow.
- Windows: folders FlowState creates get an access list for this account and SYSTEM only
  (`icacls /inheritance:r`); the files in them inherit it. A file FlowState writes into a
  folder it didn't make (`--db`, `restore --to`, `export-md --out`, `--to-file`) gets the same
  list itself. Windows already limits the default folders (inside %LOCALAPPDATA% / %APPDATA%)
  to your account, SYSTEM and administrators.

Existing files: `flowstate tighten` makes FlowState's own owner-only; on macOS / Linux the
server and `flowstate backup` also do it for the database, backups and FlowState's folders
(auto_tighten: an older version left them readable by others). A drive that doesn't keep
permissions (USB, exFAT, many SMB shares) never stops a file being made: FlowState says once
that it may be readable by other users of this computer (re-audit N-F7).
"""

import logging
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Union

log = logging.getLogger("flowstate.privatefs")

DIR_MODE, FILE_MODE = 0o700, 0o600

# Drives that don't keep permissions (USB sticks, exFAT / FAT, many SMB shares) refuse chmod.
# FlowState then carries on (the file is still made, as in 2.4.3) and says so once (re-audit N-F7).
_warned = False


class NotPrivate(OSError):
    """FlowState refused to write a private file (a symbolic link is in the way)."""


def warn_no_permissions(path: Union[str, Path], err: BaseException = None) -> None:
    """Once per process: this drive doesn't keep file permissions."""
    global _warned
    if _warned:
        return
    _warned = True
    log.warning("this drive doesn't support file permissions; %s (and FlowState's other files on "
                "it) may be readable by other users of this computer%s", Path(path),
                f" ({type(err).__name__})" if err else "")


def _chmod(path: Union[str, Path], mode: int) -> bool:
    """chmod that never raises: False (and the warning, once) when the drive refuses it."""
    try:
        os.chmod(path, mode)
        return True
    except OSError as e:
        warn_no_permissions(path, e)
        return False


def _windows() -> bool:
    return os.name == "nt"


_SID: list = []   # this account's SID, looked up once (Windows)


def _account() -> str:
    """Windows: this account for icacls, as `*<SID>` (re-audit N-F30: a user name can be
    ambiguous, or name a different account on a domain), else DOMAIN\\user, else user."""
    if not _SID:
        sid = ""
        try:
            r = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            m = re.search(r"S-1-[0-9]+(?:-[0-9]+)+", r.stdout or "") if r.returncode == 0 else None
            sid = m.group(0) if m else ""
        except (OSError, subprocess.SubprocessError):
            pass
        _SID.append("*" + sid if sid else "")
    if _SID[0]:
        return _SID[0]
    user, dom = os.environ.get("USERNAME"), os.environ.get("USERDOMAIN")
    return (f"{dom}\\{user}" if dom else user) if user else ""


def _icacls(path: Path, folder: bool) -> bool:
    """Windows: only this account and SYSTEM may use `path` (inherited entries removed)."""
    user = _account()
    if not user:
        return False
    inherit = "(OI)(CI)" if folder else ""
    cmd = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:{inherit}F",
           f"*S-1-5-18:{inherit}F", "/q"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not make %s owner-only: %s", path.name, type(e).__name__)
        return False


def lock(path: Union[str, Path]) -> None:
    """Make one file or folder FlowState just created owner-only. Never raises."""
    path = Path(path)
    try:
        if _windows():
            _icacls(path, path.is_dir())
        else:
            _chmod(path, DIR_MODE if path.is_dir() else FILE_MODE)
    except OSError as e:
        log.warning("could not make %s owner-only: %s", path.name, type(e).__name__)


def private_dir(path: Union[str, Path]) -> Path:
    """mkdir -p `path`; every folder this call creates is owner-only. Folders that already
    exist (your home, a folder you chose) are left as they are."""
    path = Path(path)
    missing, p = [], path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    if not missing:
        return path
    if _windows():
        path.mkdir(parents=True, exist_ok=True)
    else:
        for d in reversed(missing):
            try:
                d.mkdir(mode=DIR_MODE)
            except FileExistsError:
                continue
            except OSError as e:   # a drive that refuses the mode: make it plain (N-F7)
                if d.is_dir():
                    continue
                try:
                    d.mkdir()
                except FileExistsError:
                    continue
                warn_no_permissions(d, e)
    for d in reversed(missing):
        lock(d)
    return path


def private_file(path: Union[str, Path]) -> bool:
    """Create `path` empty and owner-only if it doesn't exist yet (before SQLite or anything
    else opens it, so it is never readable by others, not even for a moment). True if made.
    On a drive without file permissions the file is still made (with the warning, once)."""
    path = Path(path)
    private_dir(path.parent)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    except FileExistsError:
        return False
    os.close(fd)
    if _windows():
        lock(path)
    else:
        _chmod(path, FILE_MODE)   # whatever the umask
    return True


def write_private(path: Union[str, Path], data: Union[str, bytes], encoding: str = "utf-8") -> Path:
    """Write a whole file that starts owner-only. An existing file keeps its own mode,
    tightened to owner-only (it is FlowState's own output being replaced). A symbolic link
    (even one pointing nowhere) is refused, never followed (re-audit N-F32): NotPrivate."""
    path = Path(path)
    if path.is_symlink():
        raise NotPrivate(f"{path} is a symbolic link; FlowState doesn't write a private file "
                         f"through a link. Nothing was written.")
    if path.exists() and not path.is_file():
        raise NotPrivate(f"{path} is not a regular file. Nothing was written.")
    private_file(path)
    if isinstance(data, str):
        data = data.encode(encoding)
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as e:
        if path.is_symlink():   # swapped for a link in between
            raise NotPrivate(f"{path} is a symbolic link; nothing was written.") from e
        raise
    with os.fdopen(fd, "wb") as f:
        if not _windows():
            try:
                if stat.S_IMODE(os.fstat(f.fileno()).st_mode) & 0o077:
                    os.fchmod(f.fileno(), FILE_MODE)
            except OSError as e:
                warn_no_permissions(path, e)
        f.write(data)
    return path


def is_private(path: Union[str, Path]) -> bool:
    """macOS / Linux: no group or other permission bits. Windows: True (ACLs aren't mode bits)."""
    if _windows():
        return True
    return not stat.S_IMODE(os.stat(path).st_mode) & 0o077


def _only_flowstate_files(d: Path) -> bool:
    """A folder you chose for backups (FLOWSTATE_BACKUP_DIR) that holds nothing but FlowState's
    own backups: it is tightened too. One with anything else in it is left as it is."""
    from .backup import PREFIX
    try:
        return all(p.name.startswith(PREFIX) or p.name in ("restored", "offsite-staging")
                   or p.name in (".DS_Store",) for p in d.iterdir())
    except OSError:
        return False


def _candidates(db_path: Path, deep: bool = True) -> list:
    from . import paths
    db_path = Path(db_path)
    cands = []
    data = db_path.parent
    b = paths.backup_dir()
    named = lambda d: d.name.lower() == "flowstate"   # noqa: E731 - FlowState's own folder
    for d in {data, paths.data_root(), b, paths.config_dir()}:
        if d.is_dir() and (named(d) or (d == b and (named(d.parent) or _only_flowstate_files(d)))):
            cands.append(d)
    cands += [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]
    for d in (data, paths.config_dir()):
        cands += [d / "config.json", d / "update.json", d / "remote-auth.db",
                  d / "remote-auth.db-wal", d / "remote-auth.db-shm"]
    proj = data / "projects"
    if proj.is_dir() and named(data):
        cands += [proj] + ([p for p in proj.rglob("*")] if deep else [])
    if b.is_dir():
        from .backup import PREFIX
        cands += [p for p in b.glob(PREFIX + "*")]
        for sub in ("restored", "offsite-staging"):   # restores and decrypted copies (N-F31)
            st = b / sub
            if st.is_dir() and not st.is_symlink():
                cands += [st] + ([p for p in st.rglob("*")] if deep or sub == "restored" else [])
    return cands


def tighten_existing(db_path: Path, dry_run: bool = False, deep: bool = True) -> list:
    """`flowstate tighten`: make FlowState's own existing files and folders owner-only (the
    database and its -wal / -shm, backups and restores, the data, backup and settings folders
    when they are FlowState's own, settings files, projects/ and the off-site staging folder).
    Never your home or a folder FlowState didn't name. Returns what changed (or would change).
    A drive that refuses permissions is skipped (with the warning, once)."""
    cands = _candidates(db_path, deep)
    out = []
    for p in dict.fromkeys(cands):
        try:
            if not p.exists() or p.is_symlink():
                continue
            if _windows():
                if p.is_dir() or p.parent not in cands:
                    out.append(p)
                    if not dry_run:
                        _icacls(p, p.is_dir())
                continue
            if stat.S_IMODE(os.stat(p).st_mode) & 0o077:
                if dry_run or _chmod(p, DIR_MODE if p.is_dir() else FILE_MODE):
                    out.append(p)
        except OSError:
            continue
    return out


def auto_tighten(db_path: Path) -> list:
    """At server start and `flowstate backup` (re-audit N-F31): files an older FlowState
    (2.4.3 and before) left readable by other users of this computer are made owner-only, as
    2.4.4 promises. macOS / Linux only (on Windows the default folders are already your
    account's), cheap (a few stats; attachments under projects/ only through `flowstate
    tighten`, their folder is closed here), and it never raises."""
    if _windows():
        return []
    try:
        changed = tighten_existing(db_path, deep=False)
    except Exception as e:  # noqa: BLE001 - never stops FlowState starting
        log.warning("could not check FlowState's file permissions: %s", type(e).__name__)
        return []
    if changed:
        log.warning("made %d of FlowState's files and folders owner-only (an older version had "
                    "left them readable by other users of this computer)", len(changed))
    return changed
