"""An id that doesn't exist gets a clear error, never an empty answer that looks like
success (2.4.0, bug 12: manage_problems tree answered {} for an unknown problem)."""

import asyncio
import json

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session


def call(S, name, args):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go())


@pytest.mark.parametrize("args, code", [
    ({"action": "tree", "project": "Alpha", "problem_id": 999999}, "record_not_found"),
    ({"action": "tree", "project": "Alpha"}, "problem_id_required"),
])
def test_problem_tree_with_a_bad_id_is_an_error(backend, args, code):
    r = call(backend, "manage_problems", args)
    body = json.loads(r.content[0].text)
    assert r.isError and body["error"] == code, body


def test_problem_tree_with_a_real_id_still_works(backend):
    r = call(backend, "manage_problems", {"action": "tree", "project": "Alpha", "problem_id": 1})
    body = json.loads(r.content[0].text)
    assert not r.isError and body["problem"]["title"] == "Alpha bug"
