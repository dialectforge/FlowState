"""Per-OS folders FlowState uses besides the database itself. Standard library only.

    backups:  FLOWSTATE_BACKUP_DIR, else Windows %LOCALAPPDATA%\\flowstate\\backups,
              macOS ~/Library/Application Support/flowstate/backups,
              Linux ~/.local/share/flowstate/backups
    config:   FLOWSTATE_CONFIG_DIR, else Windows %APPDATA%\\flowstate,
              macOS ~/Library/Application Support/flowstate, Linux ~/.config/flowstate

None of these is inside the (possibly git-synced) data folder unless the user says so.
"""

import json
import os
import platform
import threading
from pathlib import Path
from typing import Optional


def _system() -> str:
    return platform.system()


def data_root(home: Optional[Path] = None, system: Optional[str] = None) -> Path:
    system, h = system or _system(), Path(home) if home else Path.home()
    if system == "Windows":
        base = Path(os.environ["LOCALAPPDATA"]) if (not home and os.environ.get("LOCALAPPDATA")) \
            else h / "AppData" / "Local"
        return base / "flowstate"
    if system == "Darwin":
        return h / "Library" / "Application Support" / "flowstate"
    return h / ".local" / "share" / "flowstate"


def backup_dir(home: Optional[Path] = None, system: Optional[str] = None) -> Path:
    if not home and os.environ.get("FLOWSTATE_BACKUP_DIR"):
        return Path(os.environ["FLOWSTATE_BACKUP_DIR"]).expanduser()
    return data_root(home, system) / "backups"


def config_dir(home: Optional[Path] = None, system: Optional[str] = None) -> Path:
    if not home and os.environ.get("FLOWSTATE_CONFIG_DIR"):
        return Path(os.environ["FLOWSTATE_CONFIG_DIR"]).expanduser()
    system, h = system or _system(), Path(home) if home else Path.home()
    if system == "Windows":
        base = Path(os.environ["APPDATA"]) if (not home and os.environ.get("APPDATA")) \
            else h / "AppData" / "Roaming"
        return base / "flowstate"
    if system == "Darwin":
        return h / "Library" / "Application Support" / "flowstate"
    return h / ".config" / "flowstate"


# ---------------------------------------------------------------- settings file
def config_file() -> Path:
    return config_dir() / "config.json"


class ConfigUnreadable(Exception):
    """config.json exists but isn't a JSON object FlowState can read (a hand-edit typo, or a
    file another program is half-way through writing)."""

    def __init__(self, path: Path):
        super().__init__(f"FlowState's settings file {path} can't be read (it isn't valid JSON). "
                         f"Nothing was changed. Fix or delete that file, then try again.")
        self.path = path


def _read_config(p: Path) -> dict:
    """The settings, {} when there is no file; ConfigUnreadable when there is one that doesn't
    parse. On Windows a reader can be refused for a moment while a writer replaces the file,
    so a refused read is tried again briefly first."""
    import time
    deadline = time.monotonic() + 1.0
    while True:
        try:
            text = p.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return {}
        except PermissionError:
            if time.monotonic() > deadline:
                raise ConfigUnreadable(p) from None
            time.sleep(0.02)
        except OSError:
            raise ConfigUnreadable(p) from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigUnreadable(p) from None
    if not isinstance(data, dict):
        raise ConfigUnreadable(p)
    return data


def load_config(strict: bool = False) -> dict:
    """FlowState's own settings (no secrets ever go in here). A file that can't be read counts
    as no settings, unless `strict` (then ConfigUnreadable): a setting that must fail safe
    (updates.self_update_allowed, the name sent to the relay) and every writer use strict."""
    try:
        return _read_config(config_file())
    except ConfigUnreadable:
        if strict:
            raise
        return {}


def replace_file(tmp: Path, p: Path, wait: float = 2.0) -> None:
    """os.replace(tmp, p), the last step of an atomic write. On Windows the replace is refused
    (access denied) while another process has `p` open, e.g. `flowstate relay status` reading
    the file the relay agent rewrites; readers hold it for moments, so try again briefly."""
    import time
    deadline = time.monotonic() + wait
    while True:
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def save_config(data: dict) -> Path:
    """Write the whole settings file atomically (a temp file of its own, then replace). Use
    update_config to change one setting: it keeps the others."""
    import secrets
    from .privatefs import private_dir, write_private
    p = config_file()
    private_dir(p.parent)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        write_private(tmp, json.dumps(data, indent=2) + "\n")   # owner-only (audit D9)
        replace_file(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()
    return p


LOCK_WAIT = 5.0      # seconds a settings writer waits for another one


def _try_lock(fd: int) -> bool:
    """An OS lock on the open lock file, without waiting: True if we have it, False if another
    process (or thread) holds it. The OS drops it when the holder exits, so a writer that
    crashed never blocks the next one. A filesystem without locks counts as "have it"."""
    import errno
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK):
            return False
        return True   # e.g. ENOLCK / ENOTSUP on an odd filesystem: go ahead as before 2.4.4


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


_thread_lock = threading.Lock()   # threads of one process take turns too (and wait less)


def _quiet_unlink(f: Path) -> None:
    try:
        f.unlink()
    except OSError:
        pass


def _lock(p: Path):
    """An OS file lock (flock / msvcrt) on a lock file next to config.json, held while one
    writer reads, changes and writes the settings, so two writers at once can't lose each
    other's change. Nothing to go stale: the OS releases it when its holder exits (audit
    N-F23: a lock left by a crashed writer no longer blocks for 30 s, and two waiting
    writers can't both "break" one). After locking, the file must still be the one at the
    path (the last holder removes it when done), else it starts over."""
    import contextlib
    import time

    @contextlib.contextmanager
    def held():
        lock = p.with_name(p.name + ".lock")
        deadline = time.monotonic() + LOCK_WAIT

        def busy():
            raise TimeoutError(f"Another FlowState command is changing {p}; try again "
                               f"in a moment.") from None
        if not _thread_lock.acquire(timeout=LOCK_WAIT):
            busy()
        try:
            while True:
                fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
                if _try_lock(fd):
                    try:
                        same = os.path.samestat(os.fstat(fd), os.stat(str(lock)))
                    except FileNotFoundError:
                        same = False
                    if same:
                        break
                    _unlock(fd)   # its holder removed it as we opened it: take the new one
                os.close(fd)
                if time.monotonic() > deadline:
                    busy()
                time.sleep(0.02)
            try:
                yield
            finally:
                # POSIX: remove it while still holding it, so whoever has it open next sees a
                # file that is no longer at the path and starts over. Windows can't remove an
                # open file: unlock and close first; if another writer opened it meanwhile, the
                # removal is refused and that writer keeps the same file.
                if os.name != "nt":
                    _quiet_unlink(lock)
                _unlock(fd)
                os.close(fd)
                if os.name == "nt":
                    _quiet_unlink(lock)
        finally:
            _thread_lock.release()
    return held()


def update_config(change) -> Path:
    """Change the settings: `change(cfg)` edits the dict in place, under the lock, on a strict
    read (a file that exists but can't be read raises ConfigUnreadable and is left as it is,
    so one typo can't wipe the other settings)."""
    from .privatefs import private_dir
    p = config_file()
    private_dir(p.parent)   # owner-only when FlowState makes it (audit D9)
    with _lock(p):
        cfg = _read_config(p)
        change(cfg)
        return save_config(cfg)


def inside_git_worktree(path: Path) -> Optional[Path]:
    """The git work tree containing `path`, if any (checks for a .git entry upwards)."""
    p = Path(path).resolve()
    for d in [p] + list(p.parents):
        if (d / ".git").exists():
            return d
    return None
