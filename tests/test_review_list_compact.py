"""2.4.0 (task 35): manage_review list answers one line per item, so a whole page (limit 100)
fits in one reply; item_id=<id> gives that one item in full. Temp DBs only (conftest.py)."""

import asyncio
import json

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import review, waiting
from flowstate.database import Database

BUDGET = 16_000   # the whole JSON answer at limit=100, as the app receives it


def text_of(S, args, app="claude-code"):
    """The answer's text exactly as the app gets it."""
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool("manage_review", args)
    r = asyncio.run(go())
    return r.content[0].text, r.isError


def call(S, args):
    return json.loads(text_of(S, args)[0])


LONG = ("Résumé of a very long session — naïve café notes 🙂 " * 60).strip()   # escapes in JSON


def long_pile(db, n=102):
    """`n` items with long summaries, details and payloads, in a project with a long name."""
    pid = Database(db).create_project("Ünïcödé project " + "x" * 120, "long").id
    return [review.add_item(db, pid, "session_summary", f"{i} {LONG}", LONG * 3,
                            {"blob": LONG * 5, "n": i}, "cursor") for i in range(n)]


def test_a_full_page_of_102_long_items_fits_one_reply(backend):
    S, db = backend, backend.backend.db_path
    ids = long_pile(db)
    text, err = text_of(S, {"action": "list", "limit": 100})
    assert not err and len(text) < BUDGET, len(text)
    out = json.loads(text)
    assert out["total"] == 102 and len(out["items"]) == 100
    assert all(isinstance(line, str) and "\n" not in line for line in out["items"])
    assert all(len(json.dumps(line)) - 2 <= waiting.LINE_MAX for line in out["items"])
    assert [int(line.split(" ")[0][1:]) for line in out["items"]] == ids[:100]
    assert out["items"][0].startswith(f"#{ids[0]} (Ünïcödé project") and "session summary" in out["items"][0]
    assert out["items"][0].endswith("...")
    assert out["showing"] == f"100 of 102, oldest first: #{ids[0]} to #{ids[99]}"
    assert out["next_page"] == f'manage_review(action="list", after={ids[99]})'
    assert 'item_id=<id>' in out["full_item"]
    for gone in ("details", "payload", "blob", "more"):
        assert f'"{gone}"' not in text
    page2 = call(S, {"action": "list", "limit": 100, "after": ids[99]})
    assert page2["total"] == 102 and len(page2["items"]) == 2 and "next_page" not in page2
    assert [int(line.split(" ")[0][1:]) for line in page2["items"]] == ids[100:]


def test_status_all_is_compact_too_and_shows_each_status(backend):
    S, db = backend, backend.backend.db_path
    ids = long_pile(db)
    review._claim(db, ids[0], "approved", "test", "user said yes")
    review._claim(db, ids[1], "rejected", "test", "user said no", "nope")
    text, _ = text_of(S, {"action": "list", "status": "all", "limit": 100})
    assert len(text) < BUDGET, len(text)
    out = json.loads(text)
    assert out["total"] == 102 and out["status"] == "all" and len(out["items"]) == 100
    assert ", approved): " in out["items"][0] and ", rejected): " in out["items"][1]
    assert ", pending): " in out["items"][2]
    rejected = call(S, {"action": "list", "status": "rejected"})
    assert rejected["total"] == 1 and rejected["items"][0].startswith(f"#{ids[1]} ")
    pending = call(S, {"action": "list"})   # pending lines don't repeat their status
    assert pending["total"] == 100 and ", pending)" not in pending["items"][0]


def test_item_id_gives_one_item_in_full(backend):
    S, db = backend, backend.backend.db_path
    ids = long_pile(db, 3)
    out = call(S, {"action": "list", "item_id": ids[1]})
    it = out["item"]
    assert it["id"] == ids[1] and it["summary"] == f"1 {LONG}" and it["details"] == LONG * 3
    assert json.loads(it["payload"]) == {"blob": LONG * 5, "n": 1}
    assert it["project"].startswith("Ünïcödé project") and it["status"] == "pending"
    assert it["kind"] == "session_summary" and it["app"]
    # any status, and the named project must match
    review._claim(db, ids[2], "rejected", "test", "user said no", "nope")
    assert call(S, {"action": "list", "item_id": ids[2]})["item"]["reject_reason"] == "nope"
    proj = it["project"]
    assert call(S, {"action": "list", "item_id": ids[1], "project": proj})["item"]["id"] == ids[1]
    wrong = call(S, {"action": "list", "item_id": ids[1], "project": "Alpha"})
    assert wrong["error"] == "record_not_found" and "'Alpha'" in wrong["message"]


def test_unknown_item_id_is_a_clear_error(backend):
    S = backend
    text, err = text_of(S, {"action": "list", "item_id": 999999})
    out = json.loads(text)
    assert err and out["error"] == "record_not_found"
    assert "No review item #999999" in out["message"] and 'status="all"' in out["message"]


def test_the_walk_list_stays_compact(backend):
    S, db = backend, backend.backend.db_path
    long_pile(db, 25)
    text, _ = text_of(S, {"action": "walk"}, app="grok")
    out = json.loads(text)
    assert out["waiting"] == 25 and len(out["items"]) == waiting.WALK_MAX
    assert all(len(json.dumps(line)) - 2 <= waiting.LINE_MAX for line in out["items"])
    assert '"details"' not in text and '"payload"' not in text and len(text) < 8_000


def test_fit_counts_escaped_characters():
    line = waiting._fit("é" * 500, 120)
    assert len(json.dumps(line)) - 2 <= 120 and line.endswith("...")
    assert waiting._fit("  short \n text ", 120) == "short text"
