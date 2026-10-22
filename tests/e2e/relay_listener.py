"""TEST-ONLY (e2e S15): FlowState's HTTP app as the relay agent builds it (relay=True, so every
request is marked as coming through the relay), on 127.0.0.1 with the sandbox's shared token
instead of the tunnel and OAuth. No tunnel, no certificate, no network beyond loopback.

    python relay_listener.py <port>      (env: the e2e sandbox, FS_E2E_TOKEN)
"""
import os
import sys
from pathlib import Path

import uvicorn

from flowstate.server import create_http_app

if __name__ == "__main__":
    port = int(sys.argv[1])
    app = create_http_app(os.environ["FS_E2E_TOKEN"], db_path=Path(os.environ["FLOWSTATE_DB"]),
                          relay=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
