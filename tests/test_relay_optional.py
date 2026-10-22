"""The relay agent is optional: with no enrollment, FlowState behaves exactly as before.

Runs a plain FlowState in a fresh interpreter (so sys.modules is clean) with the network and
listening sockets watched, uses the tools and builds the HTTP app, then checks that no relay
module was imported, no outbound connection or listening socket was made, and no relay folder
was created. Needs no `cryptography`, so it always runs."""

import json
import os
import re
import subprocess
import sys

from conftest import SERVER_DIR

PROBE = r"""
import json, os, socket, sys

connects, listens = [], []
_connect, _connect_ex, _listen = socket.socket.connect, socket.socket.connect_ex, socket.socket.listen
_create_connection = socket.create_connection

def connect(self, addr, *a):
    connects.append(repr(addr)); return _connect(self, addr, *a)
def connect_ex(self, addr, *a):
    connects.append(repr(addr)); return _connect_ex(self, addr, *a)
def listen(self, *a):
    listens.append(repr(self.getsockname())); return _listen(self, *a)
def create_connection(addr, *a, **k):
    connects.append(repr(addr)); return _create_connection(addr, *a, **k)
socket.socket.connect, socket.socket.connect_ex, socket.socket.listen = connect, connect_ex, listen
socket.create_connection = create_connection

from pathlib import Path
db = Path(sys.argv[1])
from flowstate.database import Database
Database(db).create_project("Alpha", "First project")

from flowstate import server as S, cli
S.init_backend(db)
assert S.backend.error is None, S.backend.error
out = [S.handle_call("start_here", {"project": "Alpha"}, client="probe"),
       S.handle_call("log_work", {"project": "Alpha", "kind": "change",
                                          "summary": "probe change"}, client="probe"),
       S.handle_call("search", {"query": "probe"}, client="probe")]
S.create_http_app("x" * 40, db_path=db)
cli.build_parser()

print(json.dumps({
    "relay_modules": sorted(m for m in sys.modules if m.startswith("flowstate.relay")
                            and m != "flowstate.relay"),
    "relay_pkg_side_effects": "flowstate.relay" in sys.modules,
    "connects": connects, "listens": listens,
    "relay_dir": os.path.exists(os.environ["FLOWSTATE_RELAY_DIR"]),
    "mentions_relay": any("RELAY" in str(o) for o in out),
}))
"""


def _probe(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLOWSTATE_")}
    env.update(FLOWSTATE_DB=str(tmp_path / "default-fallback.db"),
               FLOWSTATE_BACKUP_DIR=str(tmp_path / "backups"),
               FLOWSTATE_CONFIG_DIR=str(tmp_path / "config"),
               FLOWSTATE_RELAY_DIR=str(tmp_path / "relay"), FLOWSTATE_REVIEW_PORT="0")
    out = subprocess.run([sys.executable, "-c", PROBE, str(tmp_path / "fs.db")],
                         cwd=str(SERVER_DIR), env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_no_relay_code_runs_without_enrollment(tmp_path):
    r = _probe(tmp_path)
    # registering `flowstate relay` in the CLI parser loads only the tiny relay/cli.py stub
    # (argparse wiring, no I/O); nothing that talks to the network, makes keys or starts loops
    assert set(r["relay_modules"]) <= {"flowstate.relay.cli"}, r["relay_modules"]
    for m in ("store", "api", "acme", "ct", "crypto", "tunnel", "runner"):
        assert f"flowstate.relay.{m}" not in r["relay_modules"]
    assert r["connects"] == [], r["connects"]     # no outbound connection of any kind
    assert r["listens"] == [], r["listens"]       # no new port
    assert r["relay_dir"] is False                # nothing written for the relay
    assert r["mentions_relay"] is False           # tool output unchanged


def test_relay_only_starts_from_its_own_command():
    """Nothing outside flowstate/relay/ starts the agent: the only references are the CLI
    registration, the start_here alert check and the phone approval link (approvals.py: the
    handle and domain); the last two read relay state only if it exists."""
    pkg = SERVER_DIR / "flowstate"
    hits = {}
    for p in pkg.rglob("*.py"):
        if "relay" in p.relative_to(pkg).parts:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if re.search(r"from \.?relay|import relay|flowstate\.relay|\.relay\.", line):
                hits.setdefault(p.name, []).append(line.strip())
    assert set(hits) <= {"cli.py", "server.py", "approvals.py"}, hits
    assert not any("runner" in l or "tunnel" in l for ls in hits.values() for l in ls), hits
    setup = (SERVER_DIR.parent / "setup_flowstate.py").read_text(encoding="utf-8")
    assert "relay" not in setup.lower()            # the installer never enrolls or auto-starts
