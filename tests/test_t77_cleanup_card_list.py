"""Task 77: the MCP Apps review card for a waiting clean-up shows its exact list.

The card (show_review_card) and its plain text carry "Approving changes exactly these records:
..." (the same list the review page shows, as `would_change`), above the out-of-date warning from
task 76. Erased records never show text or ids; with nothing left the list reads "none (...)".
Decided clean-ups and other kinds are unchanged. Temp databases and the in-process server only
(conftest.py)."""

import json
import re

from flowstate import cleanup
from flowstate.review_card_ui import CARD_HTML

from test_cleanup import _j, _q, messy  # noqa: F401
from test_cleanup import call as ccall
from test_t74_cleanup_bound import _approve, _dup, _stale, _x
from test_t76_cleanup_item_and_card import LIST, WARN, _gone, _item, _one, _title, ui  # noqa: F401
from test_t76_cleanup_item_and_card import _card as _raw_card


def _card(S, iid):
    """The card and its plain text (the lines the app shows when it draws no card)."""
    card, text = _raw_card(S, iid)
    return card, json.loads(text)["review_cards"][0]["text"]


def test_card_of_an_up_to_date_clean_up_shows_the_exact_list(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    card, text = _card(S, iid)
    page = cleanup.preview(db, _item(db, iid))
    assert card["would_change"] == page["text"] == _one(S, iid)["item"]["would_change"]
    assert card["would_change"].startswith(LIST)
    assert f"todo #{old}" in card["would_change"] and f"todo #{new}" in card["would_change"]
    assert card["would_change"] in text.splitlines()       # the plain text has it as a line
    assert "warning" not in card and WARN not in text


def test_card_of_an_out_of_date_clean_up_shows_the_list_and_the_warning(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    card, text = _card(S, iid)
    page = cleanup.preview(db, _item(db, iid))
    assert card["would_change"] == page["listed"] and card["warning"] == page["warning"]
    # the list, then the warning: together the one-item view's text, word for word
    assert card["would_change"] + " " + card["warning"] == _one(S, iid)["item"]["would_change"]
    lines = text.splitlines()
    assert lines.index(card["would_change"]) + 1 == lines.index(card["warning"])
    assert card["would_change"].count(WARN) == 0              # the warning is not drawn twice


def test_card_about_an_erased_record_lists_only_the_live_one(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    gone = _title(db, new)
    _gone(S, db, new)
    card, text = _card(S, iid)
    assert f"todo #{old}" in card["would_change"]
    assert f"#{new}" not in card["would_change"]           # (the title names the suggestion, as before)
    assert [x for x in text.splitlines() if x.startswith(LIST)] == [card["would_change"]]
    assert gone not in json.dumps(card) and gone not in text
    assert cleanup.ERASED_WHY in card["warning"]


def test_card_whose_only_record_was_erased_reads_none(messy, ui):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    gone = _title(db, ids["kern"])
    _gone(S, db, ids["kern"])
    card, text = _card(S, iid)
    assert card["would_change"] == LIST + cleanup.NONE_LEFT + "."
    assert card["would_change"] in text.splitlines()
    assert f"#{ids['kern']}" not in card["would_change"]
    assert gone not in json.dumps(card) and gone not in text
    assert cleanup.ERASED_WHY in card["warning"]


def test_card_of_a_decided_clean_up_and_other_kinds_have_no_list(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _approve(S, iid)
    card, text = _card(S, iid)
    assert card["resolved"] and "would_change" not in card and LIST not in text
    out = _j(ccall(S, "log_work", {"project": "FlowState", "kind": "decision",
                                   "summary": "Keep the queue in SQLite", "suggested": True}))
    card, text = _card(S, out["queued_for_review"])
    assert "would_change" not in card and "warning" not in card and LIST not in text


def test_card_carries_markup_in_a_title_as_text(messy, ui):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _x(db, "UPDATE todos SET title = '<b>x</b> & y' WHERE id = ?", old)
    card, _ = _card(S, iid)
    assert '"<b>x</b> & y"' in card["would_change"]           # raw text; the card escapes it


def test_the_card_document_draws_the_list_escaped():
    assert "view.would_change" in CARD_HTML and ".change{" in CARD_HTML
    line = [x for x in CARD_HTML.splitlines() if "view.would_change" in x]
    assert line and all('el("p", "change", view.would_change)' in x for x in line)
    # el() sets textContent, never HTML; nothing in the card writes HTML from a view
    assert "n.textContent = text" in CARD_HTML
    assert not re.search(r"innerHTML\s*=|insertAdjacentHTML|document\.write", CARD_HTML)
    # the list sits above the warning
    assert CARD_HTML.index("view.would_change") < CARD_HTML.index('el("p", "warn"')
