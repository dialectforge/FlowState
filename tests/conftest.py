"""Shared fixtures. Every test uses its own temp DB or a temp copy of testdata/; nothing
here touches a live database or a real client config."""

import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO / "mcp-server"
sys.path.insert(0, str(SERVER_DIR))

from flowstate.paths import data_root  # noqa: E402

# The real per-user data folder; tests must never write inside it.
LIVE_DATA = data_root()


@pytest.fixture(autouse=True)
def _never_live(monkeypatch, tmp_path):
    # Belt and braces: any code path that falls back to the default DB lands in tmp.
    monkeypatch.setenv("FLOWSTATE_DB", str(tmp_path / "default-fallback.db"))
    # Backups, settings and the Linux token file also land in tmp, never in the real
    # per-user folders.
    monkeypatch.setenv("FLOWSTATE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("FLOWSTATE_TOKEN", raising=False)
    yield


def _assert_not_live(p: Path):
    assert LIVE_DATA not in Path(p).resolve().parents, f"refusing to use live data: {p}"


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
    env.update({"FLOWSTATE_DB": str(db), "PYTHONPATH": str(SERVER_DIR),
                "FLOWSTATE_BACKUP_DIR": str(Path(db).parent / "backups"),
                "FLOWSTATE_CONFIG_DIR": str(Path(db).parent / "config")})
    env.update({k: str(v) for k, v in extra.items()})
    return env
