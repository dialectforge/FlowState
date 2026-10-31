"""Task 76: a clean-up's exact list and out-of-date warning in two more places.

a. manage_review list item_id=<id> (the one-item view) shows a waiting clean-up's exact list
   (the review page's "Approving changes exactly these records: ..." text) and, when the list
   no longer matches, the review page's warning. Erased records never show text there.
b. The MCP Apps review card for a waiting clean-up shows that warning too.
Other items, and decided clean-ups, are unchanged. Temp databases and the in-process server
only (conftest.py)."""

import asyncio
import html
import json

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import cleanup, conflict_cards, review, review_cards, waiting
from flowstate.review_card_ui import CARD_HTML

from test_cleanup import _j, _q, messy  # noqa: F401
from test_cleanup import call as ccall
from test_t74_cleanup_bound import _approve, _dup, _payload, _stale, _x
from test_t75_followups import _erase

LIST = "Approving changes exactly these records: "
WARN = "This list is not the one the suggestion was made with"


def _one(S, iid):
    out = _j(ccall(S, "manage_review", {"action": "list", "project": "FlowState",
                                        "item_id": iid}, client="Grok"))
    assert "item" in out, out
    return out


def _item(db, iid):
    return [x for x in review.list_items(db, status="all")["items"] if x["id"] == iid][0]


def _title(db, tid):
    return _q(db, "SELECT title FROM todos WHERE id = ?", tid)[0][0]


def _gone(S, db, tid):
    out = _j(ccall(S, "manage_memory", {"action": "delete", "project": "FlowState",
                                        "record_type": "todo", "record_id": tid}, client="Cursor"))
    assert out.get("deleted"), out
    _erase(S, db, f"todo:{tid}")


@pytest.fixture
def ui(monkeypatch):
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: True)
    monkeypatch.setattr(review_cards, "client_has_ui", lambda: True)


def _card(S, iid):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Claude Desktop", version="1")) as c:
            return await c.call_tool("show_review_card", {"project": "FlowState", "item_id": iid})
    r = asyncio.run(go())
    assert not r.isError, r.content[0].text
    return r.structuredContent["cards"][0], r.content[0].text


# ---------------------------------------------------------------- a. the one-item view
def test_one_item_shows_the_exact_list_and_no_warning_when_up_to_date(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    it = _one(S, iid)["item"]
    assert it["would_change"].startswith(LIST)
    assert f"todo #{old}" in it["would_change"] and f"todo #{new}" in it["would_change"]
    assert "out_of_date" not in it and WARN not in json.dumps(it)
    # word for word what the review page lists
    assert it["would_change"] == cleanup.preview(db, _item(db, iid))["text"]


def test_one_item_warns_when_a_listed_row_changed(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    it = _one(S, iid)["item"]
    assert it["out_of_date"].startswith(WARN) and f"todo #{old} was edited" in it["out_of_date"]
    assert it["would_change"].startswith(LIST) and it["would_change"].endswith(it["out_of_date"])
    # the same sentence the review page shows in bold
    assert f"<strong>{html.escape(it['out_of_date'])}</strong>" in cleanup.html_for(db, _item(db, iid))


def test_one_item_warns_for_an_older_clean_up_without_a_list(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    p = _payload(db, iid)
    p.pop("bound")
    _x(db, "UPDATE review_items SET payload = ? WHERE id = ?", json.dumps(p), iid)
    assert "queued by an older FlowState" in _one(S, iid)["item"]["out_of_date"]


def test_one_item_about_an_erased_record_warns_without_its_text(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    gone = _title(db, new)
    _gone(S, db, new)
    out = _one(S, iid)
    it = out["item"]
    assert it["status"] == "pending"
    assert cleanup.ERASED_WHY in it["out_of_date"] and it["would_change"].startswith(LIST)
    assert f"todo #{old}" in it["would_change"]          # the live one is still listed
    assert gone not in json.dumps(out) and f"todo #{new}" not in it["would_change"]


def test_one_item_whose_only_record_was_erased(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    gone = _title(db, ids["kern"])
    _gone(S, db, ids["kern"])
    out = _one(S, iid)
    it = out["item"]
    assert it["would_change"] == LIST + cleanup.NONE_LEFT + ". " + it["out_of_date"]
    assert cleanup.ERASED_WHY in it["out_of_date"]
    assert gone not in json.dumps(out)
    # the review page reads the same, with no empty list
    page = cleanup.html_for(db, _item(db, iid))
    assert "<ul></ul>" not in page and html.escape(cleanup.NONE_LEFT) in page


def test_a_decided_clean_up_has_no_list_or_warning(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    _approve(S, iid)   # voided and closed
    it = _one(S, iid)["item"]
    assert it["status"] == "rejected"
    assert "would_change" not in it and "out_of_date" not in it


def test_other_items_in_the_one_item_view_are_unchanged(messy):
    S, db, ids = messy
    out = _j(ccall(S, "log_work", {"project": "FlowState", "kind": "decision",
                                   "summary": "Keep the queue in SQLite", "suggested": True}))
    iid = out["queued_for_review"]
    got = _one(S, iid)
    assert not {"would_change", "out_of_date"} & set(got)
    assert "would_change" not in got["item"] and "out_of_date" not in got["item"]
    assert set(got["item"]) == set(waiting._item_view(db, iid))


# ---------------------------------------------------------------- b. the review card
def test_card_of_an_up_to_date_clean_up_has_no_warning(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    card, text = _card(S, iid)
    assert "warning" not in card and WARN not in text


def test_card_warns_when_a_listed_row_changed(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    card, text = _card(S, iid)
    want = cleanup.preview(db, _item(db, iid))["warning"]
    assert card["warning"] == want and f"todo #{old} was edited" in want
    assert want in text                                  # the plain text says it too


def test_card_about_an_erased_record_warns_without_its_text(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    gone = _title(db, new)
    _gone(S, db, new)
    card, text = _card(S, iid)
    assert cleanup.ERASED_WHY in card["warning"]
    assert gone not in json.dumps(card) and gone not in text


def test_card_of_a_decided_clean_up_and_other_kinds_have_no_warning(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    _approve(S, iid)
    card, _ = _card(S, iid)
    assert card["resolved"] and "warning" not in card
    out = _j(ccall(S, "log_work", {"project": "FlowState", "kind": "decision",
                                   "summary": "Keep the queue in SQLite", "suggested": True}))
    card, text = _card(S, out["queued_for_review"])
    assert "warning" not in card and WARN not in text


def test_the_card_document_draws_the_warning():
    assert 'view.warning' in CARD_HTML and '"warn"' in CARD_HTML and ".warn{" in CARD_HTML
