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
LOCK_STALE = 30.0    # a lock file older than this was left by a writer that died


def _lock(p: Path):
    """A lock file next to config.json (O_EXCL, any OS) held while one writer reads, changes
    and writes the settings, so two writers at once can't lose each other's change."""
    import contextlib
    import time

    @contextlib.contextmanager
    def held():
        lock = p.with_name(p.name + ".lock")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE:
                        lock.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Another FlowState command is changing {p}; try again "
                                       f"in a moment.") from None
                time.sleep(0.02)
        try:
            os.close(fd)
            yield
        finally:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
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
