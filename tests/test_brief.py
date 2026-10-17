"""The start_here brief: content, project matching, and the 4,000-character cap."""

import sqlite3

import pytest

from flowstate.brief import BRIEF_CAP, build_brief, candidates_text, resolve_project
from flowstate.database import Database


def test_brief_has_todos_changes_learnings(seeded_db):
    p = resolve_project(seeded_db, "alpha")["project"]
    text = build_brief(seeded_db, p)
    assert text.startswith("FlowState brief: Alpha")
    for needle in ("Alpha todo", "Alpha bug", "Alpha lesson", "config"):
        assert needle in text
    assert "Beta todo" not in text


def test_resolution_rules(seeded_db):
    assert resolve_project(seeded_db, 2)["project"]["name"] == "Beta Project"
    assert resolve_project(seeded_db, "#2")["project"]["name"] == "Beta Project"
    assert resolve_project(seeded_db, "beta-project")["project"]["name"] == "Beta Project"
    assert resolve_project(seeded_db, None, folder=r"C:\code\Beta_Project")["project"]["id"] == 2
    assert resolve_project(seeded_db, "beta")["project"]["id"] == 2            # fuzzy for reads
    assert "candidates" in resolve_project(seeded_db, "beta", fuzzy=False)    # strict for writes
    miss = resolve_project(seeded_db, "Gamma")
    assert set(miss["candidates"]) == {"Alpha", "Beta Project"}
    assert "Alpha" in candidates_text(miss)


def test_cap_on_huge_synthetic_project(fresh_db):
    d = Database(fresh_db)
    p = d.create_project("Huge", "d" * 5000)
    c = d.create_component(p.id, "C" * 300)
    for i in range(400):
        d.add_todo(p.id, f"todo {i} " + "t" * 400, None, "high")
        d.log_problem(c.id, f"problem {i} " + "p" * 400, None, "critical")
        d.log_change(c.id, "f" * 300, None, "v" * 500, "code", "r" * 500)
        d.log_learning(p.id, "l" * 900, "gotcha")
    d.log_conversation(p.id, "x" * 900, None, ["d" * 900])
    s = d.start_session(p.id)
    d.end_session(s.id, "s" * 5000)
    text = build_brief(fresh_db, resolve_project(fresh_db, "Huge")["project"],
                       {"session_id": 99})
    assert len(text) <= BRIEF_CAP
    for section in ("Open todos", "Open problems", "Recent changes", "Recent decisions", "Key learnings"):
        assert section in text


def test_cap_on_every_project_of_real_db_copy(real_db_copy):
    conn = sqlite3.connect(real_db_copy)
    ids = [r[0] for r in conn.execute("SELECT id FROM projects")]
    conn.close()
    assert ids
    worst = 0
    for pid in ids:
        p = resolve_project(real_db_copy, pid)["project"]
        text = build_brief(real_db_copy, p, {"session_id": 1})
        worst = max(worst, len(text))
        assert len(text) <= BRIEF_CAP, (pid, len(text))
    print(f"largest real brief: {worst} chars over {len(ids)} projects")
