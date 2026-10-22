"""Local backups and restore. Standard library only.

backup_now: SQLite online backup API from a read-only connection (safe while the
servers are running), `PRAGMA integrity_check` on the copy, then retention: the newest
backup of each of the last 14 days that have one, plus the newest of each of the last 8
ISO weeks. Safety copies (labelled pre-restore, pre-migration, pre-bulk or pre-update) are
also kept for at least SAFETY_KEEP_DAYS whatever those limits say, so a later backup the same
day can't remove the copy taken just before a restore. Only files named like FlowState backups are
ever deleted.

restore: verify the backup, copy it into a NEW file, verify the result. Writing over an
existing database needs force=True and first takes a verified pre-restore backup.
"""

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import paths

KEEP_DAILY = 14
KEEP_WEEKLY = 8
SAFETY_KEEP_DAYS = 7
SAFETY_LABELS = ("pre-restore", "pre-migration", "pre-bulk", "pre-update")
PREFIX = "flowstate-"
_BACKUP_NAME = re.compile(r"^flowstate-(\d{8})-(\d{6})(?:-([a-z0-9-]+))?\.db$")


class BackupError(Exception):
    pass


def _ro(path: Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def integrity(path: Path) -> str:
    """'ok' or the first problem integrity_check reports."""
    conn = _ro(path)
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        return "ok" if rows == ["ok"] else "; ".join(rows[:3])
    finally:
        conn.close()


def table_counts(path: Path) -> dict:
    conn = _ro(path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'memory_fts%' ORDER BY name")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


def _copy(src: Path, dst: Path) -> None:
    """Online backup src -> dst (dst may be an existing database: it is replaced)."""
    s = _ro(src)
    try:
        d = sqlite3.connect(str(dst), timeout=30)
        try:
            d.execute("PRAGMA busy_timeout = 30000")
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def backup_now(db_path: Path, dest_dir: Optional[Path] = None, prune: bool = True,
               label: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    db_path = Path(db_path)
    if not db_path.is_file():
        raise BackupError(f"database not found: {db_path}")
    dest_dir = Path(dest_dir) if dest_dir else paths.backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    name = f"{PREFIX}{now:%Y%m%d-%H%M%S}" + (f"-{label}" if label else "") + ".db"
    final = dest_dir / name
    n = 1
    while final.exists():
        final = dest_dir / name.replace(".db", f"-{n}.db")
        n += 1
    partial = final.with_name(final.name + ".partial")
    try:
        _copy(db_path, partial)
        check = integrity(partial)
    except Exception as e:  # noqa: BLE001
        partial.unlink(missing_ok=True)
        raise BackupError(f"backup failed: {type(e).__name__}: {e}") from e
    if check != "ok":
        partial.unlink(missing_ok=True)
        raise BackupError(f"backup copy failed its integrity check ({check}); nothing kept")
    partial.replace(final)
    removed = prune_backups(dest_dir, now, also_keep=final) if prune else []
    return {"path": final, "integrity": check, "size": final.stat().st_size, "removed": removed}


def list_backups(dest_dir: Path) -> list[tuple[datetime, Path]]:
    out = []
    for p in Path(dest_dir).glob(PREFIX + "*.db"):
        m = _BACKUP_NAME.match(p.name)
        if m:
            out.append((datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"), p))
    return sorted(out, reverse=True)


def is_safety_copy(path: Path) -> bool:
    """A copy taken right before a restore, a migration or a bulk / approved change."""
    m = _BACKUP_NAME.match(Path(path).name)
    label = (m.group(3) or "") if m else ""
    return any(label == s or label.startswith(s + "-") for s in SAFETY_LABELS)


def retention_keep(items: list[tuple[datetime, Path]], daily: int = KEEP_DAILY,
                   weekly: int = KEEP_WEEKLY, now: Optional[datetime] = None) -> set[Path]:
    """Which backups to keep: newest per day (last `daily` days that have backups)
    plus newest per ISO week (last `weekly` weeks that have backups), plus every safety copy
    younger than SAFETY_KEEP_DAYS."""
    now = now or datetime.now()
    keep = {p for ts, p in items
            if is_safety_copy(p) and now - ts < timedelta(days=SAFETY_KEEP_DAYS)}
    days, weeks = [], []
    for ts, p in sorted(items, reverse=True):
        d = ts.date()
        if d not in days:
            days.append(d)
            if len(days) <= daily:
                keep.add(p)
        w = ts.isocalendar()[:2]
        if w not in weeks:
            weeks.append(w)
            if len(weeks) <= weekly:
                keep.add(p)
    return keep


def prune_backups(dest_dir: Path, now: Optional[datetime] = None,
                  also_keep: Optional[Path] = None) -> list[Path]:
    """Delete backups the retention rule doesn't keep (never `also_keep`, the copy just made:
    two backups in the same second used to let the newer one remove itself)."""
    items = list_backups(dest_dir)
    keep = retention_keep(items, now=now) | ({also_keep} if also_keep else set())
    removed = []
    for _ts, p in items:
        if p not in keep:
            p.unlink()
            removed.append(p)
    return removed


def verify_db(path: Path, expected_counts: Optional[dict] = None) -> None:
    check = integrity(path)
    if check != "ok":
        raise BackupError(f"{path.name} failed its integrity check ({check})")
    counts = table_counts(path)
    if "projects" not in counts:
        raise BackupError(f"{path.name} is not a FlowState database (no projects table)")
    if expected_counts is not None and counts != expected_counts:
        diff = sorted(k for k in set(counts) | set(expected_counts)
                      if counts.get(k) != expected_counts.get(k))
        raise BackupError(f"restored copy differs from the backup in: {', '.join(diff[:8])}")


def restore(backup_file: Path, target: Path, force: bool = False,
            backup_dir: Optional[Path] = None) -> dict:
    backup_file, target = Path(backup_file), Path(target)
    if not backup_file.is_file():
        raise BackupError(f"backup not found: {backup_file}")
    if backup_file.resolve() == target.resolve():
        raise BackupError("backup and target are the same file")
    verify_db(backup_file)
    expected = table_counts(backup_file)
    pre = None
    if target.exists():
        if not force:
            raise BackupError(
                f"{target} already exists. Restore into a new file (use --to with a new "
                f"name), or add --force to replace it (a backup of it is taken first).")
        pre = backup_now(target, backup_dir, prune=False, label="pre-restore")["path"]
        _copy(backup_file, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.unlink(missing_ok=True)
        _copy(backup_file, partial)
        partial.replace(target)
    verify_db(target, expected)
    return {"restored_to": target, "pre_restore_backup": pre,
            "tables": len(expected), "projects": expected.get("projects", 0)}
