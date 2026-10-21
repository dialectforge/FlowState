"""Safety copies and the health check (2.4.0). Standard library only.

Safety copy: right before an approved change that removes or rewrites existing records (a
delete, a clean-up move or merge, a markdown edit), FlowState takes a verified backup
labelled `pre-bulk`. It is kept at least backup.SAFETY_KEEP_DAYS whatever the retention
limits say. This happens whether or not the user scheduled nightly backups; FlowState never
adds a daily or nightly copy of its own. Several approvals within COPY_REUSE_SECONDS share
one copy (each is in the edit history as well). If the copy can't be made, the change is
not made.

Health check: when the server starts, a quick SQLite check of the database. Only a problem
is shown (first in start_here), with the newest backup that passes its own check. FlowState
never repairs anything itself.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import backup, paths

BULK_KINDS = ("delete", "cleanup", "edit")
COPY_REUSE_SECONDS = 60
LABEL = "pre-bulk"


class SafetyError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.payload = {"error": "safety_copy_failed",
                        "message": f"FlowState couldn't make its safety copy before this change "
                                   f"({reason}), so nothing was changed. Tell the user."}


def _dir() -> Path:
    return paths.backup_dir()


def copy_before(db_path: Path, now: Optional[datetime] = None) -> Path:
    """A fresh pre-bulk copy (or one made in the last COPY_REUSE_SECONDS)."""
    now = now or datetime.now()
    try:
        for ts, p in backup.list_backups(_dir()):
            if backup.is_safety_copy(p) and LABEL in p.name \
                    and timedelta(0) <= now - ts < timedelta(seconds=COPY_REUSE_SECONDS):
                return p
        return backup.backup_now(db_path, _dir(), label=LABEL, now=now)["path"]
    except Exception as e:  # noqa: BLE001 - any failure means no change
        raise SafetyError(f"{type(e).__name__}: {e}"[:200]) from e


def needs_copy(item: Optional[dict]) -> bool:
    return bool(item) and item.get("kind") in BULK_KINDS


# ---------------------------------------------------------------- health check
def quick_check(db_path: Path) -> str:
    """'ok' or the first problems PRAGMA quick_check reports (read-only)."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rows = [r[0] for r in conn.execute("PRAGMA quick_check(3)")]
        return "ok" if rows == ["ok"] else "; ".join(rows[:3])
    finally:
        conn.close()


def newest_good_backup(limit: int = 3) -> Optional[Path]:
    """The newest backup (of the newest `limit`) that passes its own integrity check."""
    try:
        items = backup.list_backups(_dir())[:limit]
    except OSError:
        return None
    for _ts, p in items:
        try:
            if backup.integrity(p) == "ok":
                return p
        except Exception:  # noqa: BLE001 - a broken backup is simply skipped
            continue
    return None


def health_line(db_path: Path, migration_error: Optional[str] = None) -> Optional[str]:
    """One plain line for the brief if something is wrong, else None."""
    try:
        check = quick_check(db_path)
    except Exception as e:  # noqa: BLE001
        check = f"{type(e).__name__}: {e}"
    if check == "ok" and not migration_error:
        return None
    if check == "ok":
        return (f"WARNING: FlowState couldn't update its database ({migration_error[:120]}); "
                f"newer features are off. Tell the user; nothing was lost.")
    good = newest_good_backup()
    where = (f"Newest good backup: {good.name} (`flowstate restore` puts it in a new file)."
             if good else "No good backup was found.")
    return (f"WARNING: FlowState's database failed its health check ({check[:160]}). "
            f"Nothing was repaired. {where} Tell the user.")
