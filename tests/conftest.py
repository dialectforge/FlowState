"""Shared fixtures. Every test uses its own temp DB or a temp copy of testdata/; nothing
here touches a live database or a real client config.

Real-data guard (set up below, before FlowState is imported): HOME and every FlowState
path (database, backups, config, token store, auth DB, relay, project files, data-git)
point into a session temp folder; `guard_site/sitecustomize.py` makes this process and
every Python process the tests start refuse to touch the real folders; and the session
fails if anything was refused or any FlowState path resolves into the real folders."""

import atexit
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO / "mcp-server"
GUARD_SITE = Path(__file__).resolve().parent / "guard_site"
sys.path.insert(0, str(SERVER_DIR))

from flowstate import paths  # noqa: E402

# The real per-user folders (worked out from the real HOME before it is replaced).
REAL_HOME = Path.home()
LIVE_DATA = paths.data_root(home=REAL_HOME) if os.name != "nt" else paths.data_root()
LIVE_DIRS = sorted({str(p) for p in (
    LIVE_DATA, paths.config_dir(home=REAL_HOME) if os.name != "nt" else paths.data_root().parent / "flowstate",
    REAL_HOME / ".flowstate", REAL_HOME / ".config" / "flowstate", REAL_HOME / ".local" / "share" / "flowstate",
    REAL_HOME / "Library" / "Application Support" / "flowstate",
    REAL_HOME / ".agents" / "skills" / "flowstate", REAL_HOME / ".claude" / "skills" / "flowstate")})
if os.name == "nt":
    LIVE_DIRS = sorted(set(LIVE_DIRS) | {str(Path(os.environ[v]) / "flowstate")
                                         for v in ("LOCALAPPDATA", "APPDATA") if os.environ.get(v)})

SESSION = Path(tempfile.mkdtemp(prefix="fs-tests-")).resolve()
atexit.register(shutil.rmtree, SESSION, True)   # also when pytest stops before sessionfinish
GUARD_LOG = SESSION / "guard-refusals.log"
_SESSION_ENV = {
    "HOME": SESSION / "home", "USERPROFILE": SESSION / "home",
    "FLOWSTATE_DB": SESSION / "data" / "session-fallback.db",
    "FLOWSTATE_HOME": SESSION / "data",
    "FLOWSTATE_BACKUP_DIR": SESSION / "backups", "FLOWSTATE_CONFIG_DIR": SESSION / "config",
    "FLOWSTATE_RELAY_DIR": SESSION / "relay", "FLOWSTATE_AUTH_DB": SESSION / "config" / "remote-auth.db",
    "FLOWSTATE_TOKEN_STORE_FILE": SESSION / "config" / "http-token",
    "FLOWSTATE_GUARD_DIRS": os.pathsep.join(LIVE_DIRS), "FLOWSTATE_GUARD_LOG": GUARD_LOG,
    "FLOWSTATE_REVIEW_PORT": "0",   # the in-process review page (waiting.py): any free port
    "GIT_AUTHOR_NAME": "FlowState Tests", "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "FlowState Tests", "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}
if os.name == "nt":
    _SESSION_ENV.update({"LOCALAPPDATA": SESSION / "localappdata", "APPDATA": SESSION / "appdata"})
for _k, _v in _SESSION_ENV.items():
    os.environ[_k] = str(_v)
(SESSION / "home").mkdir()
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(GUARD_SITE), str(SERVER_DIR)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])
sys.path.insert(0, str(GUARD_SITE))
import sitecustomize as _guard  # noqa: E402  (this process: the audit hook goes in now)
if "FLOWSTATE_GUARD_DIRS" not in getattr(_guard, "__doc__", "") or not _guard._DIRS:
    # a different sitecustomize was already imported: load ours under its own name
    import importlib.util
    _spec = importlib.util.spec_from_file_location("fs_guard_site", GUARD_SITE / "sitecustomize.py")
    _guard = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_guard)


def resolved_flowstate_paths() -> dict:
    """Every place FlowState would write right now, as the code resolves it."""
    from flowstate import database, remote_auth
    from flowstate.relay import store
    from flowstate.tools import utils
    from flowstate.secret_store import default_store
    st = default_store()
    out = {"db": database.default_db_path(), "backups": paths.backup_dir(),
           "config": paths.config_dir(), "auth_db": remote_auth.auth_db_path(),
           "relay": store.relay_dir(), "data (files, data-git)": (Path(utils._db.db_path).parent if utils._db else database.default_db_path().parent),
           "data_root": paths.data_root()}
    if getattr(st, "path", None):
        out["token"] = st.path
    else:
        out["token store"] = st.name
    return out


def live_hits(found: dict) -> list:
    return [f"{k} -> {v}" for k, v in found.items()
            if _guard.forbidden(v) or not str(v).startswith(str(SESSION)) and k != "token store"]


def pytest_sessionstart(session):
    bad = live_hits(resolved_flowstate_paths())
    if bad:
        pytest.exit("real-data guard: FlowState paths resolve outside the test sandbox: " + "; ".join(bad),
                    returncode=3)


def pytest_sessionfinish(session, exitstatus):
    refused = GUARD_LOG.read_text(encoding="utf-8").strip() if GUARD_LOG.exists() else ""
    if refused:
        sys.stderr.write("\n*** REAL-DATA GUARD: something tried to touch real FlowState data ***\n"
                         + refused + "\n")
        session.exitstatus = 3
    shutil.rmtree(SESSION, ignore_errors=True)


@pytest.fixture(autouse=True)
def _never_live(monkeypatch, tmp_path):
    # Belt and braces: any code path that falls back to the default DB lands in tmp.
    monkeypatch.setenv("FLOWSTATE_DB", str(tmp_path / "default-fallback.db"))
    # Backups, settings and the Linux token file also land in tmp, never in the real
    # per-user folders.
    monkeypatch.setenv("FLOWSTATE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "config"))
    # relay state (keys, device token) too
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(tmp_path / "config" / "remote-auth.db"))
    monkeypatch.setenv("FLOWSTATE_TOKEN_STORE_FILE", str(tmp_path / "config" / "http-token"))
    for k in ("FLOWSTATE_RELAY_API", "FLOWSTATE_RELAY_TUNNEL", "FLOWSTATE_RELAY_TUNNEL_SNI",
              "FLOWSTATE_RELAY_CA_FILE", "FLOWSTATE_RELAY_ACME_DIRECTORY", "FLOWSTATE_RELAY_DOMAIN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("FLOWSTATE_TOKEN", raising=False)
    monkeypatch.delenv("FLOWSTATE_TOKEN_FILE", raising=False)
    bad = [h for h in (f"{k} -> {v}" for k, v in resolved_flowstate_paths().items()) if _guard.forbidden(h.split(" -> ", 1)[1])]
    assert not bad, f"real-data guard: {bad}"
    yield


def _assert_not_live(p: Path):
    assert not _guard.forbidden(p), f"refusing to use live data: {p}"


@pytest.fixture
def fresh_db(tmp_path) -> Path:
    from flowstate.database import Database
    db = tmp_path / "fs.db"
    Database(db)
    _assert_not_live(db)
    return db


@pytest.fixture
def seeded_db(fresh_db) -> Path:
    from flowstate.database import Database
    d = Database(fresh_db)
    a = d.create_project("Alpha", "First project")
    b = d.create_project("Beta Project", "Second project")
    ca = d.create_component(a.id, "Core")
    d.create_component(b.id, "Web")
    d.add_todo(a.id, "Alpha todo", None, "high")
    d.add_todo(b.id, "Beta todo", None, "low")
    d.log_problem(ca.id, "Alpha bug", "it breaks", "critical")
    d.log_learning(a.id, "Alpha lesson", "gotcha")
    d.log_change(ca.id, "config", None, "new", "config", "because")
    return fresh_db


@pytest.fixture
def real_db_copy(tmp_path) -> Path:
    src = REPO / "testdata" / "flowstate.db"
    if not src.exists():
        pytest.skip("testdata/flowstate.db (copy of the real DB) not present")
    dst = tmp_path / "real-copy.db"
    shutil.copy2(src, dst)
    return dst


@pytest.fixture
def backend(seeded_db):
    from flowstate import server as S
    S.init_backend(seeded_db)
    assert S.backend.error is None
    yield S
    S.backend.tracker = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_env(db: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLOWSTATE_")}
    env.update({"FLOWSTATE_DB": str(db), "PYTHONPATH": os.pathsep.join([str(GUARD_SITE), str(SERVER_DIR)]),
                "FLOWSTATE_BACKUP_DIR": str(Path(db).parent / "backups"),
                "FLOWSTATE_CONFIG_DIR": str(Path(db).parent / "config"),
                "FLOWSTATE_AUTH_DB": str(Path(db).parent / "config" / "remote-auth.db"),
                "FLOWSTATE_RELAY_DIR": str(Path(db).parent / "relay"),
                "FLOWSTATE_TOKEN_STORE_FILE": str(Path(db).parent / "config" / "http-token"),
                "FLOWSTATE_REVIEW_PORT": "0",   # never the user's 8770
                "FLOWSTATE_GUARD_DIRS": os.environ["FLOWSTATE_GUARD_DIRS"],
                "FLOWSTATE_GUARD_LOG": os.environ["FLOWSTATE_GUARD_LOG"]})
    env.update({k: str(v) for k, v in extra.items()})
    return env
