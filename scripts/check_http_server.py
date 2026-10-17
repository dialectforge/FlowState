"""Check a running FlowState HTTP server the way a remote client sees it.

    <venv python> scripts/check_http_server.py --url http://127.0.0.1:8766/mcp \
        [--token-fallback-file <path-to>\\http-token.txt] [--min-projects 1]

Token lookup is the server's own (FLOWSTATE_TOKEN, credential store, fallback file); the
token is never printed. Checks: no token -> 401, wrong token -> 401, real token ->
initialize, `start_here` listed and answers, `list_projects` returns >= --min-projects.
Exit 0 only if every check passes.
"""

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

import httpx  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from flowstate.secret_store import load_token  # noqa: E402

PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def status_without(url: str, auth: str | None) -> int:
    headers = dict(HDR, **({"Authorization": auth} if auth else {}))
    req = urllib.request.Request(url, data=PING, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


async def authed(url: str, token: str, min_projects: int) -> list[tuple[str, bool, str]]:
    out = []
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with http, streamable_http_client(url, http_client=http) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            out.append(("initialize", True, f"server {init.serverInfo.name} {init.serverInfo.version}"))
            names = [t.name for t in (await s.list_tools()).tools]
            out.append(("start_here is listed", "start_here" in names, f"{len(names)} tools"))
            brief = await s.call_tool("start_here", {})
            text = brief.content[0].text if brief.content else ""
            out.append(("start_here answers", not brief.isError and bool(text),
                         text.splitlines()[0][:80] if text else "(empty)"))
            lp = await s.call_tool("list_projects", {})
            raw = lp.content[0].text if lp.content else "[]"
            try:
                data = json.loads(raw)
                projects = data if isinstance(data, list) else data.get("projects", [])
            except json.JSONDecodeError:
                projects = []
            out.append((f"list_projects >= {min_projects}", not lp.isError and len(projects) >= min_projects,
                        f"{len(projects)} projects"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token-fallback-file", default=None)
    ap.add_argument("--min-projects", type=int, default=1)
    a = ap.parse_args()
    token, source = load_token(fallback_file=a.token_fallback_file)
    if len(token) < 16:
        print("FAIL: no token found (FLOWSTATE_TOKEN, credential store, fallback file)")
        return 1
    print(f"token source: {source} (value not shown)")
    checks = [("no token is refused (401)", status_without(a.url, None) == 401, ""),
              ("wrong token is refused (401)",
               status_without(a.url, "Bearer not-the-token-not-the-token") == 401, "")]
    try:
        checks += asyncio.run(authed(a.url, token, a.min_projects))
    except Exception as e:  # noqa: BLE001
        checks.append(("authenticated MCP session", False, f"{type(e).__name__}: {e}"))
    for name, ok, detail in checks:
        print(f"{'OK  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
