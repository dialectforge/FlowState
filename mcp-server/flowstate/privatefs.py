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

Existing files and folders are never changed here; `flowstate tighten` does that when you ask.
"""

import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import Union

log = logging.getLogger("flowstate.privatefs")

DIR_MODE, FILE_MODE = 0o700, 0o600


def _windows() -> bool:
    return os.name == "nt"


def _icacls(path: Path, folder: bool) -> bool:
    """Windows: only this account and SYSTEM may use `path` (inherited entries removed)."""
    user = os.environ.get("USERNAME")
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
            os.chmod(path, DIR_MODE if path.is_dir() else FILE_MODE)
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
    for d in reversed(missing):
        lock(d)
    return path


def private_file(path: Union[str, Path]) -> bool:
    """Create `path` empty and owner-only if it doesn't exist yet (before SQLite or anything
    else opens it, so it is never readable by others, not even for a moment). True if made."""
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
        os.chmod(path, FILE_MODE)   # whatever the umask
    return True


def write_private(path: Union[str, Path], data: Union[str, bytes], encoding: str = "utf-8") -> Path:
    """Write a whole file that starts owner-only. An existing file keeps its own mode,
    tightened to owner-only (it is FlowState's own output being replaced)."""
    path = Path(path)
    private_file(path)
    if not _windows():
        try:
            if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                os.chmod(path, FILE_MODE)
        except OSError:
            pass
    if isinstance(data, str):
        data = data.encode(encoding)
    with open(path, "wb") as f:
        f.write(data)
    return path


def is_private(path: Union[str, Path]) -> bool:
    """macOS / Linux: no group or other permission bits. Windows: True (ACLs aren't mode bits)."""
    if _windows():
        return True
    return not stat.S_IMODE(os.stat(path).st_mode) & 0o077


def tighten_existing(db_path: Path, dry_run: bool = False) -> list:
    """`flowstate tighten`: make FlowState's own existing files and folders owner-only (the
    database and its -wal / -shm, backups, the data, backup and settings folders when they are
    FlowState's own, settings files, projects/ and the off-site staging folder). Never your
    home or a folder FlowState didn't name. Returns what changed (or would change)."""
    from . import paths
    db_path = Path(db_path)
    cands = []
    data = db_path.parent
    named = lambda d: d.name.lower() == "flowstate"   # noqa: E731 - FlowState's own folder
    for d in {data, paths.data_root(), paths.backup_dir(), paths.config_dir()}:
        if d.is_dir() and (named(d) or (d == paths.backup_dir() and named(d.parent))):
            cands.append(d)
    cands += [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]
    for d in (data, paths.config_dir()):
        cands += [d / "config.json", d / "update.json", d / "remote-auth.db",
                  d / "remote-auth.db-wal", d / "remote-auth.db-shm"]
    proj = data / "projects"
    if proj.is_dir() and named(data):
        cands += [proj] + [p for p in proj.rglob("*")]
    b = paths.backup_dir()
    if b.is_dir():
        from .backup import PREFIX
        cands += [p for p in b.glob(PREFIX + "*")]
        st = b / "offsite-staging"
        if st.is_dir():
            cands += [st] + [p for p in st.rglob("*")]
    out = []
    for p in dict.fromkeys(cands):
        if not p.exists() or p.is_symlink():
            continue
        if _windows():
            if p.is_dir() or p.parent not in cands:
                out.append(p)
                if not dry_run:
                    _icacls(p, p.is_dir())
            continue
        if stat.S_IMODE(os.stat(p).st_mode) & 0o077:
            out.append(p)
            if not dry_run:
                os.chmod(p, DIR_MODE if p.is_dir() else FILE_MODE)
    return out
