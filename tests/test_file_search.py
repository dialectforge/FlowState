"""manage_files search finds words in attached text files (2.4.0; it never found anything
before: nothing built the index). Files are indexed when attached; files attached before
are indexed on the first search. Binary files are skipped. Temp folders only."""

import asyncio
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session


def call(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return json.loads((await c.call_tool(name, args)).content[0].text)
    return asyncio.run(go())


def attach(S, path, **kw):
    return call(S, "manage_files", {"action": "attach", "project": "Alpha", "file_path": str(path), **kw})


def search(S, q, project="Alpha"):
    return call(S, "manage_files", {"action": "search", "project": project, "query": q})


def test_new_attachment_is_searchable_with_file_and_lines(backend, tmp_path):
    f = tmp_path / "menu.md"
    f.write_text("# Menu\nCroissant, sourdough, lemon tart.\n\n# Hours\nOpen 7-15 daily.\n", encoding="utf-8")
    att = attach(backend, f)
    hits = search(backend, "Sourdough")
    assert len(hits) == 1 and hits[0]["attachment_id"] == att["id"] and hits[0]["file_name"] == "menu.md"
    assert hits[0]["start_location"] == "1" and "sourdough" in hits[0]["excerpt"]
    assert search(backend, "lemon croissant") and not search(backend, "lemon bagel")   # every word
    assert search(backend, "7-15")[0]["start_location"] == "4"                          # its own section


def test_files_attached_before_2_4_are_indexed_on_first_search(backend, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("The oven runs at 230 degrees.\n", encoding="utf-8")
    attach(backend, f)
    db = backend.backend.db_path
    conn = sqlite3.connect(db)      # make it look like a 2.3 attachment: no index yet
    conn.execute("DELETE FROM content_locations")
    conn.execute("UPDATE attachments SET indexed_at = NULL, content_extracted = 0")
    conn.commit()
    conn.close()
    assert search(backend, "oven")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT indexed_at IS NOT NULL FROM attachments").fetchone() == (1,)
    conn.close()


def test_binary_files_are_skipped_and_external_files_work(backend, tmp_path):
    png = tmp_path / "logo.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00sourdough\x00")
    attach(backend, png)
    ext = tmp_path / "recipe.py"
    ext.write_text("HYDRATION = 0.78  # sourdough\n", encoding="utf-8")
    attach(backend, ext, copy_to_bundle=False)
    hits = search(backend, "sourdough")
    assert [h["file_name"] for h in hits] == ["recipe.py"] and hits[0]["category"] == "code"


def test_search_stays_inside_the_project(backend, tmp_path):
    f = tmp_path / "beta.md"
    f.write_text("Only in beta: sourdough.\n", encoding="utf-8")
    call(backend, "manage_files", {"action": "attach", "project": "Beta Project", "file_path": str(f)})
    assert search(backend, "sourdough") == []
    assert search(backend, "sourdough", project="Beta Project")



def test_a_removed_attachment_is_no_longer_found(backend, tmp_path):
    f = tmp_path / "oops.md"
    f.write_text("attached by mistake\n", encoding="utf-8")
    att = attach(backend, f)
    assert search(backend, "mistake")
    # approvals off (the default): the removal goes through at once, undoable
    r = call(backend, "manage_files", {"action": "remove", "project": "Alpha", "attachment_id": att["id"]})
    assert "error" not in r and r["deleted"]["record_type"] == "attachment", r
    assert search(backend, "mistake") == []
