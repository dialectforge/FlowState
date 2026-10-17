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


def load_config() -> dict:
    """FlowState's own settings (no secrets ever go in here)."""
    p = config_file()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> Path:
    p = config_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def inside_git_worktree(path: Path) -> Optional[Path]:
    """The git work tree containing `path`, if any (checks for a .git entry upwards)."""
    p = Path(path).resolve()
    for d in [p] + list(p.parents):
        if (d / ".git").exists():
            return d
    return None
