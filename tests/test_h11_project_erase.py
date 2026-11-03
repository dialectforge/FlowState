"""H11: erasing a project leaves no row anywhere that refers to it (activity log, run ledger,
review items, conflict pairs, links, settings, edit history...), checked by a
generic scan of every table so future tables are covered too; and manage_methods create
without a description is a clear error, not a KeyError. Temp DBs only."""

import json
import sqlite3

import pytest

from flowstate import erase
from test_session_contents import _q, call

MARK = "Zqxvortex"          # in every text of the erased project
NAME = f"{MARK} Erase Target"


def _ok(r):
    assert isinstance(r, dict) and "error" not in r, r
    return r


def _touch_everything(S, db):
    """One project using every feature that writes rows (H10 scenario list)."""
    p = NAME
    _ok(call(S, "manage_projects", {"action": "create", "name": p,
                                    "description": f"{MARK} project"}))
    _ok(call(S, "manage_projects", {"action": "add_component", "project": p,
                                    "name": f"{MARK} core"}))
    for kind in ("decision", "change", "problem", "learning", "todo"):
        _ok(call(S, "log_work", {"project": p, "kind": kind,
                                 "summary": f"{MARK} {kind} one about the exporter",
                                 "details": f"{MARK} details",
                                 "evidence": [f"src/{MARK}.py:1"],
                                 "files": [f"src/{MARK}.py"]}))
    # possible-conflict pair (review item + conflict rows)
    call(S, "log_work", {"project": p, "kind": "decision",
                         "summary": f"{MARK} decision one about the exporter, reversed"})
    probs = _q(db, "SELECT p.id FROM problems p JOIN components c ON c.id = p.component_id "
                   "JOIN projects pr ON pr.id = c.project_id WHERE pr.name = ?", p)
    if probs:
        pid_ = probs[0][0]
        call(S, "manage_problems", {"action": "add_attempt", "project": p, "problem_id": pid_,
                                    "description": f"{MARK} attempt"})
        call(S, "manage_problems", {"action": "solve", "project": p, "problem_id": pid_,
                                    "summary": f"{MARK} solved"})
    call(S, "manage_todos", {"action": "add", "project": p, "title": f"{MARK} todo two"})
    todos = _q(db, "SELECT t.id FROM todos t JOIN projects pr ON pr.id = t.project_id "
                   "WHERE pr.name = ?", p)
    for (tid,) in todos[:1]:
        call(S, "manage_todos", {"action": "claim", "project": p, "todo_id": tid})
        call(S, "manage_todos", {"action": "update", "project": p, "todo_id": tid,
                                 "title": f"{MARK} todo renamed"})
    _ok(call(S, "manage_rules", {"action": "add", "project": p, "rule": f"{MARK} never push"}))
    from conftest import approve_rules
    approve_rules(db)
    _ok(call(S, "manage_handoffs", {"action": "leave", "project": p,
                                    "where_i_was": f"{MARK} half way", "next_step": "finish"}))
    _ok(call(S, "manage_variables", {"action": "create", "project": p, "name": f"{MARK}_port",
                                     "value": "1", "description": f"{MARK} var"}))
    _ok(call(S, "manage_methods", {"action": "create", "project": p, "name": f"{MARK} deploy",
                                   "description": f"{MARK} how", "steps": ["a", "b"]}))
    call(S, "manage_links", {"action": "add", "project": p, "link_type": "uses",
                             "target_project": "Alpha"})
    call(S, "manage_links", {"action": "add", "project": "Beta Project", "link_type": "uses",
                             "target_project": p})
    call(S, "manage_skills", {"action": "learn", "project": p, "skill": f"{MARK} skill",
                              "skill_type": "gotcha"})
    call(S, "manage_skills", {"action": "save_state", "project": p,
                              "focus_summary": f"{MARK} focus"})
    call(S, "manage_sessions", {"action": "start", "project": p, "focus": f"{MARK} focus"})
    call(S, "manage_sessions", {"action": "end", "project": p, "summary": f"{MARK} end"})
    call(S, "start_here", {"project": p})
    call(S, "search", {"project": p, "query": MARK})
    rec = _q(db, "SELECT l.id FROM learnings l JOIN projects pr ON pr.id = l.project_id "
                 "WHERE pr.name = ?", p)
    for (lid,) in rec[:1]:
        call(S, "manage_memory", {"action": "mark_checked", "project": p,
                                  "record_type": "learning", "record_id": lid})
        call(S, "manage_memory", {"action": "set_source", "project": p,
                                  "record_type": "learning", "record_id": lid,
                                  "source": "user_stated"})
    call(S, "manage_memory", {"action": "files_changed", "project": p,
                              "files": [f"src/{MARK}.py"]})
    pid = _q(db, "SELECT id FROM projects WHERE name = ?", p)[0][0]
    from flowstate import approvals, review_mode
    approvals.set_on(db, False, pid)           # the project's own settings
    review_mode.set_mode(db, "off", project_id=pid, user_request="User: no review here")
    approvals.change_popup_clients(db, "claude-code", True)   # shared, per app: stays
    return pid


def _keys(db, pid) -> tuple:
    """(type, id) of every record of the project, under each type name used for it."""
    from flowstate.deletes import TYPES
    from flowstate.erase import _FTS_TYPES, _MEANING_TYPES
    ids = {}
    for t, sql in (("projects", "SELECT id FROM projects WHERE id = ?"),
                   ("components", "SELECT id FROM components WHERE project_id = ?"),
                   ("conversations", "SELECT id FROM conversations WHERE project_id = ?"),
                   ("learnings", "SELECT id FROM learnings WHERE project_id = ?"),
                   ("todos", "SELECT id FROM todos WHERE project_id = ?"),
                   ("changes", "SELECT ch.id FROM changes ch JOIN components c ON "
                               "c.id = ch.component_id WHERE c.project_id = ?"),
                   ("problems", "SELECT p.id FROM problems p JOIN components c ON "
                                "c.id = p.component_id WHERE c.project_id = ?"),
                   ("review_items", "SELECT id FROM review_items WHERE project_id = ?")):
        ids[t] = {r[0] for r in _q(db, sql, pid)}
    keys = set()
    for t, rids in ids.items():
        names = {k for k, v in TYPES.items() if v[0] == t} | set(_FTS_TYPES.get(t, ()))
        names |= {_MEANING_TYPES[t]} if t in _MEANING_TYPES else set()
        keys |= {(k, str(r)) for k in names for r in rids}
    return keys, ids


_PAIRS = (("record_type", "record_id"), ("content_type", "content_id"), ("from_type", "from_id"),
          ("to_type", "to_id"), ("a_type", "a_id"), ("b_type", "b_id"), ("by_type", "by_id"),
          ("source_type", "source_id"), ("target_type", "target_id"))


def _id_refs(db, keys, ids) -> dict:
    """Rows naming one of the project's records by type and id, by table and id, by review
    item or todo id: rows that hold no text of it are found too."""
    conn = sqlite3.connect(db)
    out = {}
    try:
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%'").fetchall():
            cols = {c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')}
            n = 0
            rows = None
            for tc, ic in _PAIRS + (("table_name", "record_id"), ("result_table", "result_id")):
                if {tc, ic} <= cols:
                    rows = rows or conn.execute(f'SELECT * FROM "{t}"').fetchall()
                    names = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]
                    a, b = names.index(tc), names.index(ic)
                    for r in rows:
                        k = (r[a], str(r[b]))
                        if k in keys or (tc in ("table_name", "result_table")
                                         and r[b] in ids.get(r[a], ())):
                            n += 1
            for col, pool in (("item_id", ids["review_items"]), ("todo_id", ids["todos"])):
                if col in cols and pool:
                    n += conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{col}" IN '
                                      f'({",".join("?" * len(pool))})', tuple(pool)).fetchone()[0]
            if n:
                out[t] = n
    finally:
        conn.close()
    return out


def _refs(db, pid) -> dict:
    """Every row in any table that still names the project: a project_id(-like) column equal
    to it, or any text/blob holding its marker. Generic, so new tables are covered."""
    conn = sqlite3.connect(db)
    out = {}
    try:
        virtual = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'")]
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            if any(t != v and t.startswith(v + "_") for v in virtual):
                continue   # FTS shadow tables: covered through the virtual table itself
            cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]
            conds, args = [], []
            for c in cols:
                if c in ("project_id", "source_project_id", "target_project_id",
                         "from_project_id", "to_project_id") or c.endswith("_project_id"):
                    conds.append(f'CAST("{c}" AS TEXT) = ?')
                    args.append(str(pid))
                conds.append(f'CAST("{c}" AS TEXT) LIKE ?')
                args.append(f"%{MARK}%")
            if conds:
                n = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE ' + " OR ".join(conds),
                                 args).fetchone()[0]
                if n:
                    out[t] = n
    finally:
        conn.close()
    return out


def test_project_erase_leaves_no_row_referring_to_it(backend):
    S, db = backend, backend.backend.db_path
    before_other = _q(db, "SELECT COUNT(*) FROM todos WHERE project_id IN "
                          "(SELECT id FROM projects WHERE name != ?)", NAME)
    pid = _touch_everything(S, db)
    touched = _refs(db, pid)
    keys, ids = _keys(db, pid)
    assert _id_refs(db, keys, ids)   # the id scan sees the project's rows before the erase
    # the project really reached the shared tables the bug was about
    for t in ("activity_log", "record_history", "review_items"):
        assert t in touched, (t, touched)
    S.backend.tracker.close_instance()
    r = call(S, "manage_projects", {"action": "delete", "project": NAME})
    assert r.get("deleted"), r
    S.backend.tracker.close_instance()
    S.backend.tracker = None
    n = erase.run(db, erase.plan(db, [f"project:{pid}"]))
    assert n["vacuumed"]
    assert _refs(db, pid) == {}
    assert _id_refs(db, keys, ids) == {}
    assert not _q(db, "SELECT 1 FROM approval_settings WHERE key LIKE ?", f"%project:{pid}")
    assert not _q(db, "SELECT 1 FROM review_settings WHERE scope = ?", f"project:{pid}")
    assert not _q(db, "SELECT 1 FROM review_mode_log WHERE scope LIKE ?", f"%project:{pid}")
    # shared rows that are not project data stay: per-app settings, the schema version
    assert _q(db, "SELECT 1 FROM approval_settings WHERE value LIKE '%claude-code%'")
    assert _q(db, "SELECT COUNT(*) FROM schema_migrations")[0][0] > 0
    # other projects untouched
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE project_id IN "
                  "(SELECT id FROM projects WHERE name != ?)", NAME) == before_other
    assert {r[0] for r in _q(db, "SELECT name FROM projects")} == {"Alpha", "Beta Project"}


def test_method_create_without_description_is_a_clear_error(backend):
    S, db = backend, backend.backend.db_path
    r = call(S, "manage_methods", {"action": "create", "project": "Alpha", "name": "deploy"})
    assert isinstance(r, dict) and r.get("error") == "bad_request", r
    assert "description" in r["message"]
    assert _q(db, "SELECT COUNT(*) FROM project_methods")[0][0] == 0
    ok = call(S, "manage_methods", {"action": "create", "project": "Alpha", "name": "deploy",
                                    "description": "how to deploy"})
    assert "error" not in ok, ok
