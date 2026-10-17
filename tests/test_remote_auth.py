"""Remote access (Phase C): OAuth 2.1 for claude.ai/ChatGPT-style connectors, per-client
tokens, read-only scope, revocation, rate limits, Host allow-list, access log.

The HTTP tests run the real server as a subprocess on a random local port in "public
mode" with public URL http://127.0.0.1:<port> (the tunnel's https URL in real use) and a
temp DB, temp settings folder and temp auth DB. Nothing touches Cloudflare or a live DB."""

import asyncio
import base64
import hashlib
import json
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import SERVER_DIR, free_port, server_env

MASTER = "test-master-" + "m" * 24          # test-only values
PASS = "correct horse battery staple"
REDIRECT = "http://127.0.0.1:9/callback"


def _auth_db(db: Path) -> Path:
    return Path(db).parent / "config" / "remote-auth.db"


def _store(db):
    from flowstate.remote_auth import AuthStore
    return AuthStore(_auth_db(db))


class Server:
    def __init__(self, db, public=True, **env):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = Path(db).parent / f"server-{self.port}.log"
        args = [sys.executable, "-m", "flowstate", "--transport", "http", "--host", "127.0.0.1",
                "--port", str(self.port)]
        if public:
            args += ["--public-url", self.base]
        e = server_env(db, FLOWSTATE_TOKEN=MASTER, FLOWSTATE_AUTH_DB=str(_auth_db(db)), **env)
        self._fh = open(self.log, "wb")
        self.proc = subprocess.Popen(args, env=e, cwd=str(SERVER_DIR), stdout=self._fh,
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                httpx.get(self.base + "/health", timeout=1)
                return
            except httpx.HTTPError:
                time.sleep(0.2)
        self.stop()
        pytest.fail("server did not start: " + self.log.read_text(errors="replace"))

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._fh.close()


@pytest.fixture
def public(seeded_db):
    _store(seeded_db).set_passphrase(PASS)
    s = Server(seeded_db)
    yield s
    s.stop()


@pytest.fixture
def lan(seeded_db):
    s = Server(seeded_db, public=False)
    yield s
    s.stop()


def _pkce():
    v = secrets.token_urlsafe(48)
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    return v, c


def _register(c: httpx.Client, base, redirect=REDIRECT, name="Test app"):
    return c.post(base + "/register", json={
        "redirect_uris": [redirect], "client_name": name, "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})


def _authorize(c, base, client_id, challenge, resource=None):
    return c.get(base + "/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-123",
        "resource": resource or base + "/mcp"})


def _oauth(base, access="write", passphrase=PASS):
    """Full browser + client flow. Returns (tokens dict, client_id, code, verifier)."""
    with httpx.Client(timeout=10) as c:
        reg = _register(c, base)
        assert reg.status_code == 201, reg.text
        cid = reg.json()["client_id"]
        verifier, challenge = _pkce()
        r = _authorize(c, base, cid, challenge)
        assert r.status_code == 302, r.text
        consent = r.headers["location"]
        assert consent.startswith(base + "/consent?req=")
        page = c.get(consent)
        assert page.status_code == 200 and "Test app" in page.text and "127.0.0.1" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        req = parse_qs(urlparse(consent).query)["req"][0]
        r = c.post(base + "/consent", data={"req": req, "decision": "approve",
                                            "passphrase": passphrase, "access": access})
        assert r.status_code == 302, r.text
        back = urlparse(r.headers["location"])
        q = parse_qs(back.query)
        assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT and q["state"] == ["st-123"]
        code = q["code"][0]
        t = c.post(base + "/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": cid, "code_verifier": verifier, "resource": base + "/mcp"})
        assert t.status_code == 200, t.text
        return t.json(), cid, code, verifier


async def _session(base, token, fn):
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with http, streamable_http_client(base + "/mcp", http_client=http) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def _call(base, token, name, args):
    async def fn(s):
        res = await s.call_tool(name, args)
        return res.isError, res.content[0].text
    return asyncio.run(_session(base, token, fn))


def _mcp_status(base, token=None, host=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if host:
        h["Host"] = host
    r = httpx.post(base + "/mcp", headers=h, timeout=10,
                   content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
    return r


# ---------------------------------------------------------------- discovery
def test_discovery_metadata_and_challenge(public):
    b = public.base
    r = _mcp_status(b)
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    prm_url = f"{b}/.well-known/oauth-protected-resource/mcp"
    assert f'resource_metadata="{prm_url}"' in www and "flowstate:read" in www
    prm = httpx.get(prm_url).json()
    assert prm["resource"] == b + "/mcp" and prm["authorization_servers"][0].rstrip("/") == b
    assert httpx.get(b + "/.well-known/oauth-protected-resource").json()["resource"] == b + "/mcp"
    asm = httpx.get(b + "/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"] == b + "/register"
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert set(asm["scopes_supported"]) == {"flowstate:read", "flowstate:write"}


# ---------------------------------------------------------------- OAuth flow
def test_oauth_write_client_full_flow_and_refresh_rotation(public):
    b = public.base
    tok, cid, code, verifier = _oauth(b, access="write")
    assert tok["token_type"].lower() == "bearer" and tok["scope"] == "flowstate:read flowstate:write"
    err, text = _call(b, tok["access_token"], "start_here", {"project": "Alpha"})
    assert not err and text.startswith("FlowState brief: Alpha")
    err, text = _call(b, tok["access_token"], "log_work",
                      {"project": "Alpha", "kind": "change", "summary": "via OAuth"})
    assert not err, text
    with httpx.Client(timeout=10) as c:
        # the code is single-use
        again = c.post(b + "/token", data={"grant_type": "authorization_code", "code": code,
                                            "redirect_uri": REDIRECT, "client_id": cid,
                                            "code_verifier": verifier})
        assert again.status_code == 400
        # refresh: new pair, old access token dead
        r = c.post(b + "/token", data={"grant_type": "refresh_token", "client_id": cid,
                                        "refresh_token": tok["refresh_token"]})
        assert r.status_code == 200, r.text
        new = r.json()
        assert new["refresh_token"] != tok["refresh_token"]
        assert _mcp_status(b, tok["access_token"]).status_code == 401
        assert _mcp_status(b, new["access_token"]).status_code != 401
        # replaying the old refresh token = stolen copy: the whole chain is revoked
        r = c.post(b + "/token", data={"grant_type": "refresh_token", "client_id": cid,
                                        "refresh_token": tok["refresh_token"]})
        assert r.status_code == 400
        assert _mcp_status(b, new["access_token"]).status_code == 401


def test_oauth_read_only_client(public):
    b = public.base
    tok, *_ = _oauth(b, access="read")
    assert tok["scope"] == "flowstate:read"
    err, text = _call(b, tok["access_token"], "start_here", {"project": "Alpha"})
    assert not err
    err, text = _call(b, tok["access_token"], "log_work",
                      {"project": "Alpha", "kind": "change", "summary": "should be refused"})
    assert err and json.loads(text)["error"] == "read_only_client"
    err, text = _call(b, tok["access_token"], "todo", {"action": "list", "project": "Alpha"})
    assert not err, text


def test_remote_clients_cannot_run_local_machine_tools(public):
    tok, *_ = _oauth(public.base, access="write")
    err, text = _call(public.base, tok["access_token"], "git", {"action": "status"})
    assert err and json.loads(text)["error"] == "not_allowed_remotely"
    err, text = _call(public.base, tok["access_token"], "file",
                      {"action": "attach", "project": "Alpha", "file_path": __file__})
    assert err and json.loads(text)["error"] == "not_allowed_remotely"


def test_deny_redirects_with_access_denied(public):
    b = public.base
    with httpx.Client(timeout=10) as c:
        cid = _register(c, b).json()["client_id"]
        r = _authorize(c, b, cid, _pkce()[1])
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        r = c.post(b + "/consent", data={"req": req, "decision": "deny"})
        assert r.status_code == 302 and "error=access_denied" in r.headers["location"]
        # the request is gone
        r = c.post(b + "/consent", data={"req": req, "decision": "approve", "passphrase": PASS})
        assert r.status_code == 400


def test_wrong_passphrase_then_lockout(public):
    b = public.base
    with httpx.Client(timeout=10) as c:
        cid = _register(c, b).json()["client_id"]
        r = _authorize(c, b, cid, _pkce()[1])
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        for _ in range(5):
            r = c.post(b + "/consent", data={"req": req, "decision": "approve",
                                             "passphrase": "wrong passphrase!!"})
            assert r.status_code == 403 and "Wrong passphrase" in r.text
        r = c.post(b + "/consent", data={"req": req, "decision": "approve", "passphrase": PASS})
        assert r.status_code == 429 and "Too many wrong passphrases" in r.text


def test_registration_refuses_unknown_redirect_hosts(public):
    with httpx.Client(timeout=10) as c:
        r = _register(c, public.base, redirect="https://evil.example/cb")
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
        r = _register(c, public.base, redirect="http://claude.ai/api/mcp/auth_callback")
        assert r.status_code == 400  # https only for non-loopback hosts
        r = _register(c, public.base, redirect="https://claude.ai/api/mcp/auth_callback")
        assert r.status_code == 201


def test_authorize_refuses_other_resource(public):
    b = public.base
    with httpx.Client(timeout=10) as c:
        cid = _register(c, b).json()["client_id"]
        r = _authorize(c, b, cid, _pkce()[1], resource="https://other.example/mcp")
        assert r.status_code == 302 and "error=invalid_request" in r.headers["location"]


def test_consent_without_passphrase_set_refuses(seeded_db):
    s = Server(seeded_db)  # no passphrase stored
    try:
        with httpx.Client(timeout=10) as c:
            cid = _register(c, s.base).json()["client_id"]
            r = _authorize(c, s.base, cid, _pkce()[1])
            page = c.get(r.headers["location"])
            assert page.status_code == 503 and "flowstate remote passphrase" in page.text
    finally:
        s.stop()


# ---------------------------------------------------------------- per-client tokens
def test_per_client_tokens_scoped_and_individually_revocable(public, seeded_db, monkeypatch):
    from flowstate import cli
    store = _store(seeded_db)
    ro_id, ro = store.add_token_client("Grok (read only)", ["flowstate:read"])
    rw_id, rw = store.add_token_client("Cursor", ["flowstate:read", "flowstate:write"])
    b = public.base
    assert not _call(b, ro, "start_here", {"project": "Alpha"})[0]
    err, text = _call(b, ro, "log_work", {"project": "Alpha", "kind": "todo", "summary": "x"})
    assert err and json.loads(text)["error"] == "read_only_client"
    assert not _call(b, rw, "log_work", {"project": "Alpha", "kind": "todo", "summary": "y"})[0]
    # revoke one through the CLI: it stops at once, the other keeps working
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(_auth_db(seeded_db)))
    assert cli.main(["remote", "revoke", "--client", ro_id]) == 0
    assert _mcp_status(b, ro).status_code == 401
    assert _mcp_status(b, rw).status_code != 401


def test_public_mode_refuses_owner_token_and_unknown_host(public):
    assert _mcp_status(public.base, MASTER).status_code == 401
    assert _mcp_status(public.base, host="evil.example").status_code == 421


def test_lan_mode_owner_token_and_client_tokens_no_oauth(lan, seeded_db):
    assert _mcp_status(lan.base).status_code == 401
    assert _mcp_status(lan.base, MASTER).status_code != 401
    _, tok = _store(seeded_db).add_token_client("Mac", ["flowstate:read", "flowstate:write"])
    assert _mcp_status(lan.base, tok).status_code != 401
    assert httpx.get(lan.base + "/.well-known/oauth-authorization-server").status_code == 404
    assert httpx.get(lan.base + "/authorize").status_code == 404


def test_rate_limit_on_unauthenticated_requests(seeded_db):
    s = Server(seeded_db, FLOWSTATE_RATE_LIMIT_ANON="5")
    try:
        codes = [_mcp_status(s.base).status_code for _ in range(8)]
        assert codes[:5] == [401] * 5 and 429 in codes[5:]
        assert httpx.get(s.base + "/health").status_code == 200  # never limited
    finally:
        s.stop()


def test_rate_limit_per_client(seeded_db):
    _, tok = _store(seeded_db).add_token_client("Busy", ["flowstate:read"])
    s = Server(seeded_db, FLOWSTATE_RATE_LIMIT="3")
    try:
        codes = [_mcp_status(s.base, tok).status_code for _ in range(6)]
        assert 429 in codes and codes[0] != 429
    finally:
        s.stop()


def test_access_log_has_no_secrets_queries_or_bodies(public, seeded_db):
    tok, cid, code, verifier = _oauth(public.base)
    _call(public.base, tok["access_token"], "log_work",
          {"project": "Alpha", "kind": "change", "summary": "body-marker-7731"})
    time.sleep(0.5)
    text = public.log.read_text(errors="replace")
    assert "POST /token 200" in text and "GET /authorize 302" in text
    assert f"client={cid}" in text
    for secret in (tok["access_token"], tok["refresh_token"], code, verifier, PASS, MASTER,
                   "body-marker-7731", "code_challenge", "st-123"):
        assert secret not in text, secret


# ---------------------------------------------------------------- store / scanner units
def test_store_keeps_only_hashes(tmp_path):
    from flowstate.remote_auth import AuthStore
    s = AuthStore(tmp_path / "a.db")
    s.set_passphrase(PASS)
    _, tok = s.add_token_client("x", ["flowstate:read"])
    raw = (tmp_path / "a.db").read_bytes()
    assert tok.encode() not in raw and PASS.encode() not in raw
    assert s.lookup(tok, ("static",)) is not None
    assert s.check_passphrase(PASS) and not s.check_passphrase("nope nope nope")
    with pytest.raises(ValueError):
        s.set_passphrase("short")


def test_expired_client_token_is_refused(tmp_path):
    from flowstate.remote_auth import AuthStore, TokenChecker
    s = AuthStore(tmp_path / "a.db")
    cid, tok = s.add_token_client("x", ["flowstate:read"], days=1)
    checker = TokenChecker(s, None)
    assert checker.check(tok).client_id == cid
    conn = sqlite3.connect(tmp_path / "a.db")
    conn.execute("UPDATE tokens SET expires_at = 1")
    conn.commit()
    conn.close()
    assert checker.check(tok) is None


def test_policy_owner_vs_remote():
    from flowstate.remote_auth import refusal
    full = ["flowstate:read", "flowstate:write", "flowstate:local"]
    assert refusal("git", {"action": "status"}, None) is None          # stdio
    assert refusal("git", {"action": "status"}, full) is None          # owner LAN token
    assert refusal("git", {"action": "status"}, full[:2])[0] == "not_allowed_remotely"
    assert refusal("review_approve", {"item_id": 1}, ["flowstate:read"])[0] == "read_only_client"
    assert refusal("search", {"query": "x"}, ["flowstate:read"]) is None


def test_scanner_refuses_flowstate_tokens():
    from flowstate.secrets_scan import scan_text
    assert scan_text("token fs_ct_" + "A" * 43) == "FlowState access token"


def test_cli_add_client_to_file_refuses_git_folder(tmp_path, monkeypatch, capsys):
    from flowstate import cli
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(tmp_path / "a.db"))
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert cli.main(["remote", "add-client", "--name", "x", "--to-file",
                     str(tmp_path / "repo" / "tok.txt")]) == 1
    assert not (tmp_path / "repo" / "tok.txt").exists()
    out = tmp_path / "out" / "tok.txt"
    out.parent.mkdir()
    assert cli.main(["remote", "add-client", "--name", "x", "--read-only", "--to-file", str(out)]) == 0
    tok = out.read_text().strip()
    assert tok.startswith("fs_ct_") and tok not in capsys.readouterr().out
