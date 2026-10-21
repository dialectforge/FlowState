"""2.4.0: safety copies (pre-restore, pre-migration, pre-bulk) survive retention for at least
SAFETY_KEEP_DAYS, even when a later backup the same day would have taken the day's slot."""

from datetime import datetime, timedelta

from flowstate import backup as B


def _names(bk):
    return sorted(p.name for _ts, p in B.list_backups(bk))


def test_pre_restore_copy_survives_a_later_same_day_backup(seeded_db, tmp_path):
    bk = tmp_path / "bk"
    morning = datetime(2026, 10, 16, 9, 0)
    pre = B.backup_now(seeded_db, bk, prune=False, label="pre-restore", now=morning)["path"]
    # the nightly (or any later) backup the same day prunes with the normal rule
    B.backup_now(seeded_db, bk, now=morning + timedelta(hours=3))
    assert pre.exists(), _names(bk)


def test_every_safety_label_is_protected(seeded_db, tmp_path):
    bk = tmp_path / "bk"
    t = datetime(2026, 10, 16, 8, 0)
    kept = [B.backup_now(seeded_db, bk, prune=False, label=lab, now=t + timedelta(minutes=i))["path"]
            for i, lab in enumerate(B.SAFETY_LABELS)]
    B.backup_now(seeded_db, bk, now=t + timedelta(hours=5))
    assert all(p.exists() for p in kept), _names(bk)


def test_safety_copies_kept_seven_days_regardless_of_count_limits(seeded_db, tmp_path):
    bk = tmp_path / "bk"
    now = datetime(2026, 10, 16, 2, 30)
    # many safety copies within the protection window, all on days that also have nightlies
    safety = []
    for d in range(B.SAFETY_KEEP_DAYS):
        B.backup_now(seeded_db, bk, prune=False, now=now - timedelta(days=d))
        safety.append(B.backup_now(seeded_db, bk, prune=False, label="pre-bulk",
                                   now=now - timedelta(days=d, hours=1))["path"])
    old = B.backup_now(seeded_db, bk, prune=False, label="pre-restore",
                       now=now - timedelta(days=B.SAFETY_KEEP_DAYS + 3, hours=1))["path"]
    B.backup_now(seeded_db, bk, prune=False, now=now - timedelta(days=B.SAFETY_KEEP_DAYS + 3))
    # pretend the daily limit is tiny: the protection still holds
    items = B.list_backups(bk)
    keep = B.retention_keep(items, daily=1, weekly=1, now=now + timedelta(minutes=5))
    assert all(p in keep for p in safety)
    # after the window, a safety copy is an ordinary backup again (that day's nightly is newer)
    assert old not in keep


def test_unlabelled_same_day_backups_still_pruned_to_one(seeded_db, tmp_path):
    bk = tmp_path / "bk"
    t = datetime(2026, 10, 16, 8, 0)
    first = B.backup_now(seeded_db, bk, prune=False, now=t)["path"]
    B.backup_now(seeded_db, bk, now=t + timedelta(hours=2))
    assert not first.exists()


def test_force_restore_copy_survives_next_backup(seeded_db, tmp_path):
    bk = tmp_path / "bk"
    src = B.backup_now(seeded_db, bk)["path"]
    r = B.restore(src, seeded_db, force=True, backup_dir=bk)
    B.backup_now(seeded_db, bk)          # the same day, with the normal prune
    assert r["pre_restore_backup"].exists()


# ---------------------------------------------------------------- copies before approved changes
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402

from mcp import types  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from flowstate import safety  # noqa: E402

ASK = "User: yes, go ahead and delete it"


def _bk():
    return Path(os.environ["FLOWSTATE_BACKUP_DIR"])


def _call(S, name, args, app="Claude Desktop"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    r = asyncio.run(go())
    return json.loads(r.content[0].text)


def _count(db, sql):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _delete_item(S):
    r = _call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                                   "record_id": 1})
    return r["delete_requested"]


def test_approved_delete_takes_a_safety_copy_first(backend):
    S = backend
    item = _delete_item(S)
    out = _call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                     "user_request": ASK})
    assert "error" not in out, out
    copies = [p for _ts, p in B.list_backups(_bk()) if B.is_safety_copy(p)]
    assert len(copies) == 1 and out["safety_copy"] == copies[0].name
    assert "pre-bulk" in copies[0].name
    assert _count(copies[0], "SELECT COUNT(*) FROM todos WHERE id = 1") == 1   # from before
    assert _count(S.backend.db_path, "SELECT COUNT(*) FROM todos WHERE id = 1") == 0


def test_no_copy_no_change(backend, monkeypatch):
    S = backend
    item = _delete_item(S)

    def boom(*a, **k):
        raise B.BackupError("disk full")
    monkeypatch.setattr(B, "backup_now", boom)
    out = _call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                     "user_request": ASK})
    assert out["error"] == "safety_copy_failed" and "nothing was changed" in out["message"]
    assert _count(S.backend.db_path, "SELECT COUNT(*) FROM todos WHERE id = 1") == 1
    assert _count(S.backend.db_path, f"SELECT COUNT(*) FROM review_items WHERE id = {item} "
                                     f"AND status = 'pending'") == 1


def test_approvals_in_quick_succession_share_one_copy(backend):
    S = backend
    for rtype in ("todo", "learning"):
        r = _call(S, "manage_memory", {"action": "delete", "project": "Alpha",
                                       "record_type": rtype, "record_id": 1})
        out = _call(S, "manage_review", {"action": "approve", "project": "Alpha",
                                         "item_id": r["delete_requested"], "user_request": ASK})
        assert "error" not in out, out
    assert len([p for _ts, p in B.list_backups(_bk()) if B.is_safety_copy(p)]) == 1


def test_no_daily_snapshot_when_the_user_has_no_backups(backend):
    """FlowState adds no daily copy of its own: with no nightly backups scheduled, everyday
    use (brief, writes, approving a suggestion) leaves the backups folder empty."""
    S = backend
    _brief(S)
    for args in ({"kind": "learning", "summary": "a"}, {"kind": "todo", "summary": "b"}):
        _call(S, "log_work", {"project": "Alpha", **args})
    sug = _call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "c",
                                "suggested": True}, app="Cursor")
    _call(S, "manage_review", {"action": "approve", "project": "Alpha",
                               "item_id": sug["queued_for_review"], "user_request": "approve"})
    S.init_backend(S.backend.db_path)                # a restart
    _brief(S)
    assert B.list_backups(_bk()) == []


# ---------------------------------------------------------------- health check
def _brief(S):
    async def go():
        async with create_connected_server_and_client_session(S.server) as c:
            return (await c.call_tool("start_here", {"project": "Alpha"})).content[0].text
    return asyncio.run(go())


def test_healthy_database_shows_nothing(backend):
    assert backend.backend.health is None
    assert "WARNING" not in _brief(backend)


def test_failed_health_check_is_shown_first_with_the_newest_good_backup(backend, monkeypatch):
    S = backend
    good = B.backup_now(S.backend.db_path, _bk())["path"]
    monkeypatch.setattr(safety, "quick_check", lambda p: "row 3 missing from index idx_x")
    S.init_backend(S.backend.db_path)
    text = _brief(S)
    first = text.splitlines()[0]
    assert first.startswith("WARNING: FlowState's database failed its health check")
    assert "row 3 missing" in first and good.name in first and "Nothing was repaired" in first
    assert "FlowState brief: Alpha" in text            # the brief still comes


def test_failed_migration_is_shown(backend, monkeypatch):
    S = backend
    from flowstate import migrations as M
    monkeypatch.setattr(M, "migrate", lambda *a, **k: 1 / 0)
    S.init_backend(S.backend.db_path)
    assert _brief(S).startswith("WARNING: FlowState couldn't update its database")
