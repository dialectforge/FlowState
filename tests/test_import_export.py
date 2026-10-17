"""Markdown export/import and memory import (Phase D4i/j): everything imported waits in the
review queue; edits apply only on approval (undoable); secrets are skipped, never stored.
Temp DBs only."""

import asyncio
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import cli, markdown_io, memory_import

ASK = "User: yes, approve it"


def run(coro):
    return asyncio.run(coro)


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def call(S, name, args, client="Claude Desktop"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return run(go())


GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"   # fake, shaped like a GitHub token


def test_export_is_readable_and_marked(backend, seeded_db):
    call(backend, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Use SQLite"})
    call(backend, "link", {"action": "add", "project": "Alpha", "target_project": "Beta Project"})
    md = markdown_io.export(seeded_db, 1)
    assert md.startswith("# FlowState: Alpha")
    assert "- [ ] Alpha todo (urgent) <!-- fs:todo:1 -->" in md   # stored "high", shown with the new name
    assert "Alpha bug <!-- fs:problem:1 -->" in md and "[gotcha] Alpha lesson" in md
    assert "Use SQLite <!-- fs:decision:" in md and "## Links (read-only)" in md
    assert "Beta todo" not in md


def test_import_round_trip_goes_to_review(backend, seeded_db, tmp_path, capsys):
    md = markdown_io.export(seeded_db, 1)
    md = md.replace("- [ ] Alpha todo (urgent)", "- [x] Alpha todo, renamed (high)")   # old name still read
    md = md.replace("## Problems", "## Problems\n- Export crashes on empty chapters")
    md = md.replace("## Learnings", f"## Learnings\n- [gotcha] token is {GH}\n- Keep commits small")
    md = md.replace("Alpha lesson <!-- fs:learning:1 -->", "")      # removed line: ignored
    todos_before = _q(seeded_db, "SELECT id, title, status FROM todos ORDER BY id")
    f = tmp_path / "alpha.md"
    f.write_text(md, encoding="utf-8")
    assert cli.main(["import-md", str(f), "--project", "Alpha", "--db", str(seeded_db)]) == 0
    out = capsys.readouterr().out
    assert "2 new, 1 edits" in out and "1 skipped for secrets" in out
    assert _q(seeded_db, "SELECT id, title, status FROM todos ORDER BY id") == todos_before
    assert _q(seeded_db, "SELECT COUNT(*) FROM learnings WHERE project_id = 1")[0][0] == 1  # not deleted
    dump = " ".join(r[0] for r in _q(seeded_db, "SELECT summary || COALESCE(details,'') || payload FROM review_items"))
    assert GH not in dump
    edit = _q(seeded_db, "SELECT id, summary FROM review_items WHERE kind = 'edit'")[0]
    assert "renamed" in edit[1] and "done" in edit[1]
    r = _j(call(backend, "review_approve", {"project": "Alpha", "item_id": edit[0], "user_request": ASK}))
    assert r["record"] == {"table": "todos", "id": 1}
    assert _q(seeded_db, "SELECT title, status FROM todos WHERE id = 1") == [("Alpha todo, renamed", "done")]
    call(backend, "history", {"action": "undo", "project": "Alpha", "record_type": "todo", "record_id": 1})
    assert _q(seeded_db, "SELECT title, status FROM todos WHERE id = 1") == [("Alpha todo", "pending")]


def test_import_refuses_other_projects_records(backend, seeded_db):
    md = "## Todos\n- [x] Beta todo hijacked <!-- fs:todo:2 -->\n"
    res = markdown_io.import_text(seeded_db, 1, md)
    assert res["not_found"] == 1 and res["edits"] == 0


def test_memory_import_formats_and_tool(backend, seeded_db):
    text = """# Saved memories
- Prefers short answers in plain English
- Works on Atlas VPN and Home Hub
* Prefers short answers in plain English
1. Uses Windows and a Mac
- api_key = abcdef1234567890abcdef
ok
"""
    assert memory_import.parse(text)[:2] == ["Prefers short answers in plain English",
                                             "Works on Atlas VPN and Home Hub"]
    js = json.dumps({"memories": [{"content": "Likes tests that run twice"}, "Ships on Fridays"]})
    assert memory_import.parse(js) == ["Likes tests that run twice", "Ships on Fridays"]

    r = call(backend, "import_memory", {"project": "Alpha", "text": text, "source": "ChatGPT"})
    assert not r.isError, r.content[0].text
    res = _j(r)
    assert res["queued"] == 3 and res["duplicates_skipped"] == 1 and res["secrets_skipped"] == 1
    rows = _q(seeded_db, "SELECT kind, status, source FROM review_items")
    assert set(rows) == {("learning", "pending", "import:ChatGPT")}
    assert _q(seeded_db, "SELECT COUNT(*) FROM learnings")[0][0] == 1        # nothing saved yet
    again = _j(call(backend, "import_memory", {"project": "Alpha", "text": text, "source": "ChatGPT"}))
    assert again["queued"] == 0                                               # no repeats
    big = call(backend, "import_memory", {"project": "Alpha", "text": "x" * 200_001})
    assert _j(big)["error"] == "too_large"
    nop = call(backend, "import_memory", {"text": "- something to remember"})
    assert _j(nop)["error"] == "project_required"


def test_cli_export_and_memory_import(seeded_db, tmp_path, capsys):
    out = tmp_path / "a.md"
    assert cli.main(["export-md", "--project", "Alpha", "--out", str(out), "--db", str(seeded_db)]) == 0
    assert "fs:todo:1" in out.read_text(encoding="utf-8")
    assert cli.main(["export-md", "--project", "Alpha", "--out", str(out), "--db", str(seeded_db)]) == 1
    mem = tmp_path / "claude.json"
    mem.write_text(json.dumps(["Name is Alex", "Prefers plain language"]), encoding="utf-8")
    assert cli.main(["import-memory", str(mem), "--project", "Alpha", "--source", "Claude",
                     "--db", str(seeded_db)]) == 0
    assert "2 added to the review queue" in capsys.readouterr().out
