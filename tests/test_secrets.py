"""Secret scanning on writes (never echoing the value), the token store, and the data
folder git guard. Every token here is fake and built at runtime."""

import asyncio
import json
import os
import platform
import sqlite3
import subprocess
import uuid

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import secret_store as SS
from flowstate.secrets_scan import scan_args, scan_text


def fake(prefix: str, n: int, alphabet: str = "aB3dE5fG7hJ9kL2mN4pQ6rS8tV0wX") -> str:
    return prefix + (alphabet * 10)[:n]


SECRETS = {
    "private key": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    "AWS access key": "key AKIA" + "ABCDEFGHIJKLMNOP",
    "GitHub token": "use " + fake("ghp_", 36),
    "Anthropic API key": fake("sk-ant-api03-", 40),
    "OpenAI-style API key": fake("sk-proj-", 40),
    "Slack token": "xoxb-" + "1234567890-abcdefghij",
    "Stripe key": "sk_" + "live_" + "a1B2c3D4e5F6g7H8i9J0",
    "JSON web token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123456ghiJKL",
    "bearer token": "Authorization: Bearer " + fake("", 30),
    "password in a URL": "clone https://john:hunter2xyz@example.com/repo.git",
    "password": "the db password = Tr0ub4dor&3",
    "secret or key value": "api_key: " + "9f8e7d6c5b4a3f2e1d",
    "64-character hex secret": "secret " + "a1" * 32,
    "payment card number": "card 4111 1111 1111 1111 on file",
    "high-entropy token": "token is " + "Zq8vN2xLp4Rt7Wm1Ks9Hd3Fb6Jc0Gy5T",
}

CLEAN = [
    "Added WAL mode and busy_timeout 5000 to every connection",
    "password is stored in Credential Manager",
    "password: <your password>",
    "token=${FLOWSTATE_TOKEN}",
    "api_key = os.environ['OPENAI_API_KEY']",
    "token: FLOWSTATE_TOKEN",
    "commit 203a65c9b1f2e3d4c5b6a7980123456789abcdef merged",  # 40-hex git sha
    r"D:\Projects\FlowState\mcp-server\flowstate\server.py",
    "max_tokens=4096 and 47 tests passed in 62.71s",
    "Order 1234-5678 shipped on 2026-10-16 12:00:00",
    "getProjectContextLeanWithHoursAndDefaults",
    "https://github.com/dialectforge/FlowStateV1.1",
]


@pytest.mark.parametrize("kind,text", sorted(SECRETS.items()))
def test_scanner_finds_each_kind(kind, text):
    assert scan_text(text) == kind


@pytest.mark.parametrize("text", CLEAN)
def test_scanner_leaves_ordinary_text_alone(text):
    assert scan_text(text) is None


def test_scan_args_walks_nested_values():
    assert scan_args({"key_decisions": ["fine", {"x": fake("ghp_", 36)}]}) == \
        ("key_decisions[1].x", "GitHub token")


async def _call(S, name, args):
    async with create_connected_server_and_client_session(S.server) as c:
        return await c.call_tool(name, args)


@pytest.mark.parametrize("name,args", [
    ("log_work", {"project": "Alpha", "kind": "learning", "summary": "x", "details": "{s}"}),
    ("todo", {"action": "add", "project": "Alpha", "title": "rotate {s}"}),
    ("variable", {"action": "create", "project": "Alpha", "name": "k", "value": "{s}",
                  "is_secret": True}),
    ("create_project", {"name": "P", "description": "{s}"}),
    ("session", {"action": "log_conversation", "project": "Alpha", "user_prompt_summary": "q",
                 "key_decisions": ["{s}"]}),
    ("git", {"action": "sync", "commit_message": "{s}"}),
])
def test_writes_with_secrets_are_refused_without_echo(backend, seeded_db, name, args):
    secret = fake("ghp_", 36)
    args = json.loads(json.dumps(args).replace("{s}", secret))
    before = sqlite3.connect(seeded_db).execute("SELECT total_changes()").fetchone()
    r = asyncio.run(_call(backend, name, args))
    assert r.isError
    text = r.content[0].text
    assert secret not in text and secret[4:20] not in text
    body = json.loads(text)
    assert body["error"] == "secret_detected" and body["kind"] == "GitHub token"
    assert "Not saved" in body["message"]
    for t in ("learnings", "todos", "project_variables", "conversations"):
        rows = sqlite3.connect(seeded_db).execute(f"SELECT * FROM {t}").fetchall()
        assert secret not in json.dumps(rows, default=str)
    assert sqlite3.connect(seeded_db).execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2
    assert before is not None


def test_reads_are_not_blocked(backend):
    r = asyncio.run(_call(backend, "search", {"query": fake("ghp_", 36)}))
    assert not r.isError


def test_review_edits_are_scanned(backend, seeded_db):
    async def go():
        async with create_connected_server_and_client_session(backend.server) as c:
            s = await c.call_tool("log_work", {"project": "Alpha", "kind": "todo",
                                               "summary": "maybe", "suggested": True})
            item = json.loads(s.content[0].text)["queued_for_review"]
            return await c.call_tool("review_approve", {
                "project": "Alpha", "item_id": item, "user_request": "User: approve",
                "details": "password = " + "Sup3rS3cret!"})
    r = asyncio.run(go())
    assert r.isError and json.loads(r.content[0].text)["kind"] == "password"


# ---------------------------------------------------------------- token store
class MemStore:
    name = "memory"

    def __init__(self):
        self.v = None

    def set(self, t):
        self.v = t

    def get(self):
        return self.v


def test_load_token_order(tmp_path, monkeypatch):
    store = MemStore()
    store.v = "from-store-" + "s" * 16
    f = tmp_path / "tok.txt"
    f.write_text("from-file-" + "f" * 16, encoding="utf-8")
    monkeypatch.setenv("FLOWSTATE_TOKEN", "from-env-" + "e" * 16)
    assert SS.load_token(str(f), store)[1] == "FLOWSTATE_TOKEN"
    monkeypatch.delenv("FLOWSTATE_TOKEN")
    assert SS.load_token(str(f), store)[1] == "token file"
    assert SS.load_token(None, store) == (store.v, "memory")


def test_fallback_file_only_when_store_is_empty(tmp_path, monkeypatch):
    """The live launcher names the old http-token.txt as a fallback: it is used until the
    token is migrated to the store, then ignored without editing the task."""
    monkeypatch.delenv("FLOWSTATE_TOKEN", raising=False)
    monkeypatch.delenv("FLOWSTATE_TOKEN_FILE", raising=False)
    f = tmp_path / "tok.txt"
    f.write_text("from-file-" + "f" * 16, encoding="utf-8")
    store = MemStore()
    assert SS.load_token(None, store, fallback_file=str(f)) == ("from-file-" + "f" * 16,
                                                                "fallback token file")
    store.v = "from-store-" + "s" * 16
    assert SS.load_token(None, store, fallback_file=str(f)) == (store.v, "memory")
    store.v = None
    assert SS.load_token(None, store, fallback_file=str(tmp_path / "gone.txt")) == ("", "memory")


def test_token_file_in_git_folder_warns(tmp_path):
    (tmp_path / ".git").mkdir()
    f = tmp_path / "http-token.txt"
    f.write_text("t" * 20, encoding="utf-8")
    warnings = []
    SS.load_token(str(f), MemStore(), warn=warnings.append)
    assert warnings and "token migrate" in warnings[0] and "t" * 20 not in warnings[0]


def test_migrate_token_copies_verifies_and_keeps_file(tmp_path):
    f = tmp_path / "http-token.txt"
    tok = "fake-" + uuid.uuid4().hex
    f.write_text(tok + "\n", encoding="utf-8")
    store = MemStore()
    r = SS.migrate_token(f, store)
    assert store.v == tok and f.exists() and r["length"] == len(tok)
    assert tok not in json.dumps(r)
    short = tmp_path / "short.txt"
    short.write_text("abc", encoding="utf-8")
    with pytest.raises(SS.StoreError):
        SS.migrate_token(short, MemStore())


def test_cli_token_migrate_never_prints_token(tmp_path, monkeypatch, capsys):
    from flowstate import cli
    store = MemStore()
    monkeypatch.setattr(SS, "default_store", lambda: store)
    tok = "fake-" + uuid.uuid4().hex
    f = tmp_path / "http-token.txt"
    f.write_text(tok, encoding="utf-8")
    assert cli.main(["token", "migrate", "--from", str(f)]) == 0
    assert cli.main(["token", "status"]) == 0
    out = capsys.readouterr().out
    assert tok not in out and "read back OK" in out and f"({len(tok)} characters)" in out
    assert f.exists()


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows Credential Manager")
def test_windows_credential_manager_roundtrip():
    store = SS.WindowsStore(target=f"flowstate-test:{uuid.uuid4().hex}")  # test-only entry
    tok = "fake-" + uuid.uuid4().hex
    try:
        assert store.get() is None
        store.set(tok)
        assert store.get() == tok
    finally:
        store.delete()
    assert store.get() is None


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX file modes")
def test_linux_file_store_is_0600(tmp_path):
    s = SS.FileStore(tmp_path / "cfg" / "http-token")
    s.set("fake-" + "x" * 20)
    assert (os.stat(s.path).st_mode & 0o777) == 0o600 and s.get() == "fake-" + "x" * 20


def test_file_store_roundtrip(tmp_path):
    s = SS.FileStore(tmp_path / "cfg" / "http-token")
    s.set("fake-" + "y" * 20)
    assert s.get() == "fake-" + "y" * 20 and s.delete() and s.get() is None


def test_mac_store_keeps_token_off_the_command_line():
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw.get("input")))
        return subprocess.CompletedProcess(argv, 0, stdout="fake-" + "z" * 20 + "\n", stderr="")
    s = SS.MacStore(service="flowstate-test", run=run)
    tok = "fake-" + "z" * 20
    s.set(tok)
    assert calls[0][0] == ["security", "-i"] and tok in calls[0][1]
    assert all(tok not in " ".join(a) for a, _i in calls)
    assert s.get() == tok


# ---------------------------------------------------------------- data-folder git guard
def test_git_sync_refuses_token_and_log_files(tmp_path, monkeypatch):
    from flowstate.tools import git_ops
    data = tmp_path / "data"
    data.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=data, check=True)
    (data / "notes.txt").write_text("ok", encoding="utf-8")
    (data / "http-token.txt").write_text("fake-" + "q" * 20, encoding="utf-8")
    (data / "server.log").write_text("log", encoding="utf-8")
    monkeypatch.setattr(git_ops, "FLOWSTATE_DATA_DIR", data)
    r = git_ops.git_sync("test")
    assert r["success"] is False and set(r["refused"]) == {"http-token.txt", "server.log"}
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=data,
                            capture_output=True, text=True).stdout.split()
    assert "http-token.txt" not in staged and "server.log" not in staged
    log = subprocess.run(["git", "log", "--oneline"], cwd=data, capture_output=True, text=True)
    assert log.stdout.strip() == ""  # nothing committed
