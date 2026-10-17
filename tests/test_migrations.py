"""Numbered migrations: fresh DBs, Phase A DBs, idempotence, backup first, additive only."""

import sqlite3

import pytest

from flowstate import migrations as M


def _tables(db):
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _schema_of(db, names):
    conn = sqlite3.connect(db)
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN (%s)" % ",".join("?" * len(names)),
            names)}
    finally:
        conn.close()


def test_files_are_numbered_and_parse():
    found = M.available()
    assert [v for v, _n, _p in found] == list(range(1, len(found) + 1))
    for _v, _n, p in found:
        assert M._statements(p.read_text(encoding="utf-8"))


def test_fresh_db_is_fully_migrated(fresh_db):
    assert {"auto_sessions", "review_items", "schema_migrations"} <= _tables(fresh_db)
    assert M.pending(fresh_db) == []
    assert M.applied(fresh_db) == {v for v, _n, _p in M.available()}


def test_phase_a_db_gets_new_tables_only_and_a_backup(seeded_db, tmp_path):
    # Make it look like a Phase A database: no migration bookkeeping, no review table,
    # one auto-captured summary waiting.
    conn = sqlite3.connect(seeded_db)
    conn.execute("DROP TABLE schema_migrations")
    conn.execute("DROP TABLE review_items")
    conn.execute("INSERT INTO sessions (project_id, ended_at, summary) VALUES "
                 "(1, '2026-10-16 10:00:00', '[auto-captured, needs review] X, 3 calls')")
    conn.commit()
    conn.close()
    existing = ["projects", "todos", "sessions", "auto_sessions", "learnings"]
    before = _schema_of(seeded_db, existing)
    counts = {t: sqlite3.connect(seeded_db).execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in existing}

    res = M.migrate(seeded_db, tmp_path / "bk")
    assert res["applied"][:2] == ["001_auto_sessions", "002_review_queue"]
    assert res["applied"] == [f"{v:03d}_{n}" for v, n, _p in M.available()]
    assert res["backup"] and (tmp_path / "bk").exists()
    assert _schema_of(seeded_db, existing) == before          # nothing altered
    for t, n in counts.items():
        assert sqlite3.connect(seeded_db).execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n
    row = sqlite3.connect(seeded_db).execute(
        "SELECT kind, status, session_id FROM review_items").fetchall()
    assert row and row[0][:2] == ("session_summary", "pending")  # backfilled into the queue
    assert M.migrate(seeded_db, tmp_path / "bk") == {"applied": [], "backup": None}


def test_failed_backup_means_no_migration(seeded_db, tmp_path, monkeypatch):
    conn = sqlite3.connect(seeded_db)
    conn.execute("DROP TABLE schema_migrations")
    conn.commit()
    conn.close()
    from flowstate import backup as B

    def boom(*a, **k):
        raise B.BackupError("disk full")
    monkeypatch.setattr(B, "backup_now", boom)
    with pytest.raises(B.BackupError):
        M.migrate(seeded_db, tmp_path / "bk")
    assert "schema_migrations" not in _tables(seeded_db)


def test_server_start_migrates_and_survives_migration_failure(seeded_db, monkeypatch):
    from flowstate import server as S
    conn = sqlite3.connect(seeded_db)
    conn.execute("DROP TABLE schema_migrations")
    conn.execute("DROP TABLE review_items")
    conn.commit()
    conn.close()
    monkeypatch.setattr(M, "migrate", lambda *a, **k: 1 / 0)
    S.init_backend(seeded_db)
    assert S.backend.error is None and S.backend.migration_error  # still serving
    assert "review_items" not in _tables(seeded_db)
    monkeypatch.undo()
    S.init_backend(seeded_db)
    assert S.backend.migration_error is None and "review_items" in _tables(seeded_db)
    S.backend.tracker = None


def test_real_db_copy_migrates_additively(real_db_copy, tmp_path):
    existing = sorted(_tables(real_db_copy))
    before = _schema_of(real_db_copy, existing)
    M.migrate(real_db_copy, tmp_path / "bk")
    after = _schema_of(real_db_copy, existing)
    assert before == after
    assert {"review_items", "schema_migrations"} <= _tables(real_db_copy)
    assert M.pending(real_db_copy) == []


def test_cli_migrate(seeded_db, tmp_path, capsys):
    from flowstate import cli
    conn = sqlite3.connect(seeded_db)
    conn.execute("DELETE FROM schema_migrations WHERE version = 2")
    conn.commit()
    conn.close()
    assert cli.main(["migrate", "--db", str(seeded_db), "--dry-run"]) == 0
    assert "002_review_queue" in capsys.readouterr().out
    assert cli.main(["migrate", "--db", str(seeded_db), "--dir", str(tmp_path / "b")]) == 0
    assert cli.main(["migrate", "--db", str(seeded_db)]) == 0
    assert "up to date" in capsys.readouterr().out
