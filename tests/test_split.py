"""The tool-definition split (Phase D4m) changed nothing a client can see: the tool list
(names, descriptions, schemas, labels), the instructions and the prompt are identical to the
snapshot taken from server.py before the split. Regenerate only on purpose:
    python tests/test_split.py --write
"""

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "data" / "client_view_snapshot.json"


def client_view() -> dict:
    from mcp import types
    from mcp.shared.memory import create_connected_server_and_client_session
    from flowstate import server as S

    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="snapshot", version="1")) as c:
            tools = (await c.list_tools()).tools
            prompts = (await c.list_prompts()).prompts
            return {"tools": [t.model_dump(mode="json", exclude_none=True) for t in tools],
                    "prompts": [p.model_dump(mode="json", exclude_none=True) for p in prompts]}
    view = asyncio.run(go())
    view["instructions"] = S.server.instructions
    return view


def test_client_view_unchanged(backend):
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    got = json.loads(json.dumps(client_view()))
    assert [t["name"] for t in got["tools"]] == [t["name"] for t in want["tools"]]
    for g, w in zip(got["tools"], want["tools"]):
        assert g == w, g["name"]
    assert got == want


def test_split_modules_are_small():
    root = HERE.parent / "mcp-server" / "flowstate"
    for name in ("server.py", "tooldefs.py", "routing.py"):
        n = len((root / name).read_text(encoding="utf-8").splitlines())
        assert n < 1300, (name, n)


if __name__ == "__main__" and "--write" in sys.argv:
    sys.path.insert(0, str(HERE.parent / "mcp-server"))
    import tempfile
    from flowstate import server as S
    from flowstate.database import Database
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "s.db"
        Database(db)
        S.init_backend(db)
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(client_view(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        S.backend.tracker = None
    print("wrote", SNAPSHOT)
