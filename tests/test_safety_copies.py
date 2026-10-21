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
