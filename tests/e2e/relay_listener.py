"""TEST-ONLY (e2e S15): FlowState's HTTP app as the relay agent builds it (relay=True, so every
request is marked as coming through the relay), on 127.0.0.1 with the sandbox's shared token
instead of the tunnel and OAuth. No tunnel, no certificate, no network beyond loopback.

    python relay_listener.py <port>          (env: the e2e sandbox, FS_E2E_TOKEN)
    python relay_listener.py <port> lan-remote
    python relay_listener.py <port> public   (env also FS_E2E_PUBLIC_URL, FS_E2E_PASSPHRASE)

`lan-remote`: FlowState's LAN listener instead (relay=False), and every request looks as if it
came from another computer on the network (the ASGI client address is set to 192.0.2.51
before FlowState sees it, as uvicorn would for a real peer there). There is no second computer
in the test, so this stands in for one; it still listens on 127.0.0.1 only.

`public`: the public listener a user runs behind their own tunnel (--public-url), with the
sandbox's owner passphrase set in the sandbox's own auth database first. No tunnel: the
public URL is made up and the test talks to 127.0.0.1 (an allowed Host on that listener).
"""
import os
import sys
from pathlib import Path

import uvicorn

from flowstate.server import create_http_app

OTHER_COMPUTER = ("192.0.2.51", 50123)


def from_other_computer(app):
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=OTHER_COMPUTER)
        await app(scope, receive, send)
    return wrapped


if __name__ == "__main__":
    port = int(sys.argv[1])
    lan_remote = sys.argv[2:3] == ["lan-remote"]
    if sys.argv[2:3] == ["public"]:
        from flowstate.remote_auth import AuthStore
        AuthStore().set_passphrase(os.environ["FS_E2E_PASSPHRASE"])   # FLOWSTATE_AUTH_DB: sandbox
        app = create_http_app(None, db_path=Path(os.environ["FLOWSTATE_DB"]),
                              public_url=os.environ["FS_E2E_PUBLIC_URL"])
    else:
        app = create_http_app(os.environ["FS_E2E_TOKEN"], db_path=Path(os.environ["FLOWSTATE_DB"]),
                              relay=not lan_remote)
    if lan_remote:
        app = from_other_computer(app)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
                proxy_headers=False)
