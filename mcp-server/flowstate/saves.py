"""How a write call is saved (2.4.0): one commit (dbconn.py).

The call's own work, the created record's history row, its receipt and run-ledger row
(receipts.py) and its activity-log row are one save: committed together, or, if any of
them fails, rolled back together; the answer is then an error, and the failure itself is
still recorded in the run ledger (outside the rolled-back save).

Some writes keep committing step by step (server._one_commit): those that take a safety
copy first (deletes, approvals) or run git, which must not hold the write lock, and undo,
which restores rows with foreign-key checks off and then looks for orphans itself.
"""

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import activity, dbconn, history, identity, receipts

log = logging.getLogger("flowstate")


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001 - bookkeeping outside a save never breaks a call
        log.warning("%s skipped: %s", getattr(fn, "__name__", "bookkeeping"), e)
        return None


def _bookkeeping(keep: Callable, db_path: Path, pending, result, meta: dict, client: str):
    """The created record's history row and the write's receipt (None if no receipt)."""
    if pending is not None:
        return keep(receipts.finish, db_path, pending, result, meta.get("project_id"),
                    meta.get("session_id"), identity.current(), meta.get("record"), client)
    if meta.get("record"):   # a created record goes into the edit history (undo takes it back)
        keep(history.record_created, db_path, *meta["record"], "create", client)
    return None


def run(db_path: Path, handle: Callable, name: str, args: dict, client: str, meta: dict,
        pending, one_commit: bool) -> Any:
    """handle(name, args, client, meta) as one save (or step by step); the answer with
    its receipt."""
    if not one_commit:
        result = None
        try:
            result = handle(name, args, client, meta)
        finally:
            receipt = _bookkeeping(_safe, db_path, pending, result, meta, client)
        return receipts.attach(result, receipt)
    try:
        with dbconn.save(db_path):
            result = handle(name, args, client, meta)
            receipt = _bookkeeping(lambda fn, *a: fn(*a), db_path, pending, result, meta, client)
            _activity(db_path, args, result, meta)
    except BaseException:
        meta.pop("activity_saved", None)
        if pending is not None:   # nothing of the save is left; how it ended is recorded
            _safe(receipts.finish, db_path, pending, None, meta.get("project_id"), None,
                  identity.current(), None, client)
        raise
    return receipts.attach(result, receipt)


def error_code(result: Any) -> Optional[str]:
    """The error code the answer will carry (server._ok makes it an error), else None."""
    if isinstance(result, dict) and "error" in result and len(result) <= 3:
        return result.get("error")
    return None


def _activity(db_path: Path, args: dict, result: Any, meta: dict) -> None:
    """The write's activity-log row, in its save; server._call_internal then skips its own.
    Not when the call already timed out or the answer still waits for the user."""
    act = meta.get("activity")
    if act is None or meta.get("gave_up") or (isinstance(result, dict) and "_ask_user" in result):
        return
    err = error_code(result)
    activity.record(db_path, act["app"], act["tool"], args.get("action"), meta.get("project_id"),
                    True, err is None, err, int((time.monotonic() - act["t0"]) * 1000),
                    act["caller"], meta.get("record"))
    meta["activity_saved"], meta["activity_error"] = True, err
