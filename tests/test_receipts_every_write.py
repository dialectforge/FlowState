"""Every write's receipt names what it saved (2.4.0, bug 9 of the e2e run): rules, links,
file attachments, imports, skills, sessions and the review mode used to answer
"saved": [] because their answers nest the id or are the saved row itself."""

import asyncio
import json

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session


def _calls(tmp_path):
    f = tmp_path / "n.md"
    f.write_text("hello world\n", encoding="utf-8")
    A = {"project": "Alpha"}
    return [
        ("manage_rules", {**A, "action": "add", "rule": "Never log card numbers."}, "rule #1"),
        ("manage_links", {**A, "action": "add", "link_type": "uses", "target_project": "Beta Project"}, "link #"),
        ("manage_links", {**A, "action": "add", "link_type": "url", "url": "https://example.com/spec"}, "link #"),
        ("manage_files", {**A, "action": "attach", "file_path": str(f)}, "attachment #1"),
        ("manage_memory", {**A, "action": "import", "text": "- User likes tea\n- Deploys on Fridays"}, "review_item #"),
        ("manage_skills", {**A, "action": "learn", "skill_type": "approach", "skill": "Use WAL"}, "skill #1"),
        ("manage_skills", {**A, "action": "confirm", "skill_id": 1}, "skill #1"),
        ("manage_skills", {**A, "action": "save_state", "focus_summary": "working on x"}, "state #1"),
        ("manage_skills", {**A, "action": "log_metric", "metric_type": "tool_choice",
                           "effectiveness_score": 0.5}, "metric #1"),
        ("manage_memory", {**A, "action": "mark_checked", "record_type": "learning", "record_id": 1}, "learning #1"),
        ("manage_memory", {**A, "action": "set_source", "record_type": "learning", "record_id": 1,
                           "source": "user_stated"}, "learning #1"),
        ("manage_rules", {**A, "action": "remove", "rule_id": 1, "user_request": "User: drop that rule"}, "rule #1"),
        ("manage_review", {"action": "set_mode", "mode": "everything", "user_request": "User: hold all"},
         "review_mode_change #"),
    ]


def test_every_write_names_what_it_saved(backend, tmp_path):
    async def go():
        out = []
        async with create_connected_server_and_client_session(
                backend.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            for name, args, want in _calls(tmp_path):
                r = await c.call_tool(name, args)
                body = json.loads(r.content[0].text)
                out.append((name, args["action"], r.isError, body.get("receipt"), want, body))
        return out
    for name, action, err, receipt, want, body in asyncio.run(go()):
        assert not err, (name, action, body)
        assert receipt and any(s.startswith(want) for s in receipt["saved"]), (name, action, receipt)


@pytest.mark.parametrize("result, tool, action, want", [
    ({"rule": {"id": 4}}, "rules", "add", ["rule:4"]),
    ({"link": {"id": 9, "type": "uses"}}, "link", "add", ["link:9"]),
    ({"id": 3, "file_name": "a.md"}, "file", "attach", ["attachment:3"]),
    ({"item_ids": [5, 6], "queued": 2}, "import_memory", None, ["review_item:5", "review_item:6"]),
    ({"id": 2}, "self_improve", "learn_skill", ["skill:2"]),
    ({"id": 2}, "unknown_tool", None, []),            # an unknown row shape names nothing
])
def test_ids_found_in_nested_and_row_answers(result, tool, action, want):
    from flowstate import receipts
    assert receipts._named(result, tool, action) == want
