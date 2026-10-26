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
that it may be readable by other users of this computer (re-audit N-F7). `flowstate tighten`
lists what it could not change and why (task 61: another account's file, such a drive, or the
system's own message), never as changed.
"""

import errno
import logging
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Union

log = logging.getLogger("flowstate.privatefs")

DIR_MODE, FILE_MODE = 0o700, 0o600

# Drives that don't keep permissions (USB sticks, exFAT / FAT, many SMB shares) refuse chmod,
# or accept it and change nothing. FlowState then carries on (the file is still made, as in
# 2.4.3) and says so once per drive (re-audit N-F7; task 57: per drive, not per process).
_warned: set = set()


class NotPrivate(OSError):
    """FlowState refused to write a private file (a symbolic link is in the way)."""


def _drive(path: Path):
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return os.stat(p).st_dev
    except OSError:
        return str(p)


def warn_no_permissions(path: Union[str, Path], err: BaseException = None) -> None:
    """Once per drive: this drive doesn't keep file permissions."""
    path = Path(path)
    where = _drive(path)
    if where in _warned:
        return
    _warned.add(where)
    log.warning("this drive doesn't support file permissions; %s (and FlowState's other files on "
                "it) may be readable by other users of this computer%s", Path(path),
                f" ({type(err).__name__})" if err else "")


NO_PERMISSIONS = "this drive doesn't support file permissions"


def _chmod(path: Union[str, Path], mode: int) -> bool:
    """chmod that never raises: False (and the warning, once) when the drive refuses it, or
    accepts it and changes nothing (some SMB mounts: the mode reads back as before, so
    FlowState doesn't claim the file is owner-only; task 56 Low)."""
    why = _chmod_why(path, mode)
    if why and why != NO_PERMISSIONS:
        log.warning("could not make %s owner-only: %s", Path(path).name, why)
    return not why


def _chmod_why(path: Union[str, Path], mode: int) -> str:
    """chmod that never raises: "" when it worked, else why not (task 61): owned by another
    account, a drive without permissions (with the warning, once), or the system's own error."""
    try:
        os.chmod(path, mode)
        if stat.S_IMODE(os.stat(path).st_mode) & 0o077 & ~mode:
            warn_no_permissions(path)
            return NO_PERMISSIONS
        return ""
    except OSError as e:
        owner = _posix_owner(path)
        if owner is not None:
            return (f"not allowed: it is owned by another account ({owner}); ask that account or "
                    f"an administrator to change it (`sudo chown`), then run `flowstate tighten` "
                    f"again")
        if e.errno in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):   # exFAT, SMB (N-F7)
            warn_no_permissions(path, e)
            return NO_PERMISSIONS
        return e.strerror or type(e).__name__


def _posix_owner(path) -> Union[str, None]:
    """The account that owns `path` when it isn't this one (macOS / Linux), else None."""
    try:
        uid = os.stat(path).st_uid
        if not hasattr(os, "geteuid") or uid == os.geteuid():
            return None
        try:
            import pwd
            return pwd.getpwuid(uid).pw_name
        except (ImportError, KeyError):
            return f"user id {uid}"
    except OSError:
        return None


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
    why = _icacls_why(path, folder)
    if why and not why.startswith(NO_PERMISSIONS):
        log.warning("could not make %s owner-only: %s", path.name, why)
    return not why


def _icacls_why(path: Path, folder: bool) -> str:
    """`_icacls` that says why not (task 61): "" when it worked. Access denied (the file is
    another account's, e.g. made from an administrator window), a drive without access lists
    (FAT, exFAT, a share without them; with the warning, once), or icacls's own message."""
    user = _account()
    if not user:
        return "could not find this account's name to give it access"
    inherit = "(OI)(CI)" if folder else ""
    cmd = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:{inherit}F",
           f"*S-1-5-18:{inherit}F", "/q"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return f"icacls could not run ({type(e).__name__})"
    if r.returncode == 0:
        return ""
    vol = _volume(path)
    if vol is not None and not vol[1]:   # FAT / exFAT / a share without access lists
        warn_no_permissions(path)
        return f"{NO_PERMISSIONS} ({vol[0]})" if vol[0] else NO_PERMISSIONS
    out = _text(r.stdout) + "\n" + _text(r.stderr)
    if r.returncode == 5 or "access is denied" in out.lower():   # ERROR_ACCESS_DENIED
        owner = _owner(path)
        mine = owner is not None and (owner[0] and "*" + owner[0] == user
                                      or owner[1].lower() == user.lower())
        what = "folder" if folder else "file"
        if owner is None:
            who = (f"this account may not change the {what}'s permissions (it is probably owned "
                   f"by another account, such as Administrators)")
        elif mine:
            who = f"this account may not change the {what}'s permissions"
        else:
            who = f"the {what} is owned by another account ({owner[1] or owner[0]})"
        return (f"access denied: {who}. Run `flowstate tighten` from an administrator window, "
                f'or take ownership first (takeown /f "{path}") and run it again')
    line = next((ln.strip() for ln in out.splitlines() if ln.strip()
                 and not ln.strip().lower().startswith("successfully processed")), "")
    return f"icacls failed: {line[:200]}" if line else f"icacls failed (exit code {r.returncode})"


def _text(b) -> str:
    if isinstance(b, str):
        return b
    if not b:
        return ""
    try:
        return b.decode("oem")   # icacls writes the console code page (Windows only codec)
    except (LookupError, UnicodeDecodeError):
        return b.decode("utf-8", "replace")


def _volume(path: Path):
    """Windows: (file system name, keeps access lists) for `path`'s drive, None if unknown."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        root = ctypes.create_unicode_buffer(261)
        if not k32.GetVolumePathNameW(str(path), root, 261):
            return None
        name, flags = ctypes.create_unicode_buffer(261), ctypes.c_ulong()
        if not k32.GetVolumeInformationW(root.value, None, 0, None, None, ctypes.byref(flags),
                                         name, 261):
            return None
        return name.value, bool(flags.value & 0x8)   # FILE_PERSISTENT_ACLS
    except Exception:  # noqa: BLE001 - not Windows, or no ctypes: unknown
        return None


def _owner(path: Path):
    """Windows: (SID, DOMAIN\\name) of `path`'s owner, None if it can't be read."""
    try:
        import ctypes
        from ctypes import wintypes
        adv = ctypes.windll.advapi32
        sid, sd = ctypes.c_void_p(), ctypes.c_void_p()
        if adv.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(sid), None, None, None,
                                     ctypes.byref(sd)):   # SE_FILE_OBJECT, OWNER_SECURITY_INFO
            return None
        try:
            text = wintypes.LPWSTR()
            sid_s = text.value if adv.ConvertSidToStringSidW(sid, ctypes.byref(text)) else ""
            if text:
                ctypes.windll.kernel32.LocalFree(text)
            name, dom = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            n, d, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
            if adv.LookupAccountSidW(None, sid, name, ctypes.byref(n), dom, ctypes.byref(d),
                                     ctypes.byref(use)):
                who = f"{dom.value}\\{name.value}" if dom.value else name.value
            else:
                who = ""
        finally:
            ctypes.windll.kernel32.LocalFree(sd)
        return (sid_s, who) if sid_s or who else None
    except Exception:  # noqa: BLE001 - not Windows, or no ctypes: unknown
        return None


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


_SUBDIRS = ("restored", "offsite-staging")


def _ours(p: Path) -> bool:
    """One of FlowState's own entries in a backups folder: a backup file (flowstate-*, a file:
    a folder of that name is someone else's), restored/ or offsite-staging/. A macOS "._name"
    metadata file an exFAT / SMB drive keeps next to one of them counts with it."""
    from .backup import PREFIX
    name = p.name[2:] if p.name.startswith("._") and p.is_file() else p.name
    if name.startswith(PREFIX):
        return p.is_file() and not p.is_symlink()
    return name in _SUBDIRS and (p.is_dir() or p.name.startswith("._"))


def _only_flowstate_files(d: Path) -> bool:
    """A folder you chose for backups (FLOWSTATE_BACKUP_DIR, `backup --dir`) that holds
    FlowState's own backups and nothing else: it is tightened too. One with anything else in
    it, or nothing at all (an empty folder isn't FlowState's yet), is left as it is."""
    try:
        entries = [p for p in d.iterdir() if p.name != ".DS_Store"]
        return bool(entries) and all(_ours(p) for p in entries) \
            and any(not p.name.startswith("._") for p in entries)
    except OSError:
        return False


def _candidates(db_path: Path, deep: bool = True, dirs=()) -> list:
    from . import paths
    db_path = Path(db_path)
    cands = []
    data = db_path.parent
    b = paths.backup_dir()
    named = lambda d: d.name.lower() == "flowstate"   # noqa: E731 - FlowState's own folder
    for d in {data, paths.data_root(), b, paths.config_dir()}:
        if d.is_dir() and (named(d) or (d == b and (named(d.parent) or _only_flowstate_files(d)))):
            cands.append(d)
    for d in dirs:   # a backups folder you chose (`backup --dir`, `tighten --dir`; N-F31)
        cands += _backup_folder(Path(d), deep, own=_only_flowstate_files(Path(d)))
    cands += [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]
    for d in (data, paths.config_dir()):
        cands += [d / "config.json", d / "update.json", d / "remote-auth.db",
                  d / "remote-auth.db-wal", d / "remote-auth.db-shm"]
    proj = data / "projects"
    if proj.is_dir() and named(data):
        cands += [proj] + ([p for p in proj.rglob("*")] if deep else [])
    cands += _backup_folder(b, deep, own=False)
    return cands


def _backup_folder(b: Path, deep: bool, own: bool) -> list:
    """A backups folder's own files: backups (files only), "._" metadata of them, restored/
    and offsite-staging/ with what is in them; the folder itself when `own`."""
    if not b.is_dir() or b.is_symlink():
        return []
    from .backup import PREFIX
    out = [b] if own else []
    out += [p for p in b.glob(PREFIX + "*") if p.is_file() and not p.is_symlink()]
    out += [p for p in b.glob("._" + PREFIX + "*") if p.is_file() and not p.is_symlink()]
    for sub in _SUBDIRS:   # restores and decrypted copies (N-F31)
        st = b / sub
        if st.is_dir() and not st.is_symlink():
            out += [st] + ([p for p in st.rglob("*") if not p.is_symlink()]
                           if deep or sub == "restored" else [])
    return out


class Tightened(list):
    """What `tighten_existing` changed (or would change), a list as before; `.failed` holds
    (path, why) for each one it could not change (task 61: never counted as changed)."""

    def __init__(self):
        super().__init__()
        self.failed = []


def tighten_existing(db_path: Path, dry_run: bool = False, deep: bool = True,
                     dirs=()) -> Tightened:
    """`flowstate tighten`: make FlowState's own existing files and folders owner-only (the
    database and its -wal / -shm, backups and restores, the data, backup and settings folders
    when they are FlowState's own, settings files, projects/ and the off-site staging folder).
    Never your home or a folder FlowState didn't name. Returns what changed (or would change);
    `.failed` lists what could not be changed and why (a drive that refuses permissions: with
    the warning, once)."""
    cands = _candidates(db_path, deep, dirs)
    out = Tightened()
    for p in dict.fromkeys(cands):
        try:
            if not p.exists() or p.is_symlink():
                continue
            if _windows():
                if p.is_dir() or p.parent not in cands:
                    why = "" if dry_run else _icacls_why(p, p.is_dir())
                else:
                    continue
            elif stat.S_IMODE(os.stat(p).st_mode) & 0o077:
                why = "" if dry_run else _chmod_why(p, DIR_MODE if p.is_dir() else FILE_MODE)
            else:
                continue
            if why:
                out.failed.append((p, why))
            else:
                out.append(p)
        except OSError as e:
            out.failed.append((p, e.strerror or type(e).__name__))
    return out


def auto_tighten(db_path: Path, dirs=()) -> list:
    """At server start and `flowstate backup` (re-audit N-F31): files an older FlowState
    (2.4.3 and before) left readable by other users of this computer are made owner-only, as
    2.4.4 promises. macOS / Linux only (on Windows the default folders are already your
    account's), cheap (a few stats; attachments under projects/ only through `flowstate
    tighten`, their folder is closed here), and it never raises."""
    if _windows():
        return []
    try:
        changed = tighten_existing(db_path, deep=False, dirs=dirs)
    except Exception as e:  # noqa: BLE001 - never stops FlowState starting
        log.warning("could not check FlowState's file permissions: %s", type(e).__name__)
        return []
    if changed:
        log.warning("made %d of FlowState's files and folders owner-only (an older version had "
                    "left them readable by other users of this computer)", len(changed))
    failed = [p for p, why in changed.failed if not why.startswith(NO_PERMISSIONS)]   # warned already
    if failed:
        log.warning("could not make %d of FlowState's files and folders owner-only; "
                    "`flowstate tighten` says why", len(failed))
    return changed
