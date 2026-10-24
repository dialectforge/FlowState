"""A v2.3.0 database upgrades cleanly to 2.4.0: built by the real v2.3.0 code (git archive
of the v2.3.0 commit into a temp folder), then migrated by this code. Only new tables
arrive (015-019, 021), plus three NULL columns on FlowState's own record_meta (020, who set a
source tag) and two on the new review_open_nonces (022, the start ticket); every other table
keeps its schema, and every table its rows."""

import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import io

import pytest

from conftest import REPO
from flowstate import migrations as M

V230 = "1ccf756"   # main at v2.3.0 (plus a test-only fix)
NEW = {"session_runs", "run_writes", "run_views", "review_open_nonces"}


def _rows(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


@pytest.fixture
def v230_db(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    try:
        tar = subprocess.run(["git", "-C", str(REPO), "archive", V230, "mcp-server", "database"],
                             capture_output=True, check=True, timeout=60).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip(f"v2.3.0 commit {V230} not in this clone")
    old = tmp_path / "v230"
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(old, filter="data")
    db = tmp_path / "v230.db"
    script = f"""
import sys; sys.path.insert(0, {str(old / 'mcp-server')!r})
from pathlib import Path
from flowstate.database import Database
from flowstate import server as S
d = Database(Path({str(db)!r}))
p = d.create_project("Alpha", "First"); c = d.create_component(p.id, "Core")
d.add_todo(p.id, "old todo", None, "high"); d.log_learning(p.id, "old lesson", "gotcha")
S.init_backend(Path({str(db)!r}))
r = S.handle_call("log_work", {{"project": "Alpha", "kind": "decision", "summary": "old decision"}}, "Old App")
assert "error" not in r, r
S.backend.tracker.touch("Old App", p.id, "read:start_here")
"""
    subprocess.run([sys.executable, "-I", "-c", script], check=True, timeout=120,
                   cwd=str(tmp_path))
    assert max(v for (v,) in _rows(db, "SELECT version FROM schema_migrations")) == 14
    return db


def _schemas(db):
    return dict(_rows(db, "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))


def test_v230_database_upgrades_with_new_tables_only(v230_db, tmp_path):
    db = v230_db
    before = _schemas(db)
    tables = [n for (n,) in _rows(db, "SELECT name FROM sqlite_master WHERE type='table' "
                                      "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'memory_fts%'")]
    data = {t: _rows(db, f'SELECT * FROM "{t}" ORDER BY rowid') for t in tables
            if t != "schema_migrations"}
    cols = lambda: [r[1:] for r in _rows(db, "PRAGMA table_info(record_meta)")]
    meta_before = cols()
    res = M.migrate(db, tmp_path / "bk")
    assert res["applied"] == ["015_session_runs", "016_write_receipts", "017_run_views",
                              "018_run_views_upto", "019_approvals", "020_source_stamps",
                              "021_review_links", "022_review_start_tickets", "023_conflict_cards"]
    assert res["backup"] and "pre-migration" in res["backup"]
    after = _schemas(db)
    changed = {k for k in before if after.get(k) != before[k]}
    assert changed == {"record_meta"}                       # nothing else altered
    now = cols()
    assert now[:-3] == meta_before                          # only added at the end
    assert [c[0] for c in now[-3:]] == ["source_set_by", "source_set_model", "source_set_at"]
    assert all(c[1] == "TEXT" and c[2] == 0 and c[3] == "NULL" for c in now[-3:])
    assert {n for n in after if n not in before} >= NEW
    assert {t: _rows(db, f'SELECT * FROM "{t}" ORDER BY rowid') for t in data} == data
    assert _rows(db, "PRAGMA integrity_check") == [("ok",)]
    assert M.migrate(db, tmp_path / "bk") == {"applied": [], "backup": None}


def test_upgraded_database_works_end_to_end(v230_db):
    from flowstate import identity, server as S
    S.init_backend(v230_db)
    try:
        assert S.backend.error is None and S.backend.migration_error is None
        assert S.backend.health is None
        ident = identity.Identity("Cursor", None, "mac-mini", "self_reported", "conn-1")
        with identity.use(ident):
            tok = identity.set_call("conn-1", "abcdef123456")
            try:
                w = S.handle_call("log_work", {"project": "Alpha", "kind": "todo",
                                               "summary": "new todo"}, "Cursor")
            finally:
                identity.reset_call(tok)
            brief = S.handle_call("start_here", {"project": "Alpha"}, "Cursor")
        assert w["receipt"] == {"change": "abcdef123456", "saved": [f"todo #{w['record']['id']}"]}
        assert "old todo" in brief and "old decision" in brief
        # the v2.3.0 session (no connection key) is left alone; the new call got its own
        assert len(_rows(v230_db, "SELECT session_id FROM auto_sessions "
                                  "WHERE closed_at IS NULL")) == 2
        undo = S.handle_call("history", {"action": "undo", "project": "Alpha",
                                         "record_type": "todo", "record_id": w["record"]["id"]},
                             "Cursor")
        assert undo["rows"][0]["now"] == "deleted", json.dumps(undo)
    finally:
        S.backend.tracker = None
