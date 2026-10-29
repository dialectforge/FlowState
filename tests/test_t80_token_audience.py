"""Task 80: token audience validation the way mcp 1.30 intends (GHSA-w4fh-qvv9-3v23).

The public listener builds `AuthSettings(resource_server_url=<resource>,
validate_token_resource=True)` and its bearer check is the SDK's `BearerAuthBackend`; the
token verifier (TokenChecker) reports each token's audience as `AccessToken.resource`. A
token for another resource is refused; FlowState's own tokens, including ones issued before
this change (same rows, same `resource` string), still open /mcp. Runs the whole HTTP app
in-process (no network, temp auth DBs); the MCP session manager behind the gate is replaced
by a stub that records who got in."""

import asyncio
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

from test_t78_oauth_issuer import PASS, REDIRECT, _run

OTHER = "https://other.example/mcp"
# (configured public URL, relay?)
ADDRESSES = [("https://abc123.fsmcp.com", True),       # relay device address
             ("https://flowstate.example.com/", False),  # own tunnel, typed with a slash
             ("http://127.0.0.1:8766", False),          # local listener
             ("https://FlowState.Example.com:443", False)]  # unusual spelling (task 79)


def _app(db, tmp_path, public_url, relay=False):
    """(gate, auth store); /mcp behind the gate answers 200 and keeps the authenticated token,
    every other route is FlowState's own."""
    from flowstate.http_app import create_http_app
    from flowstate.remote_auth import AuthStore
    auth_db = tmp_path / f"auth-{secrets.token_hex(4)}.db"
    store = AuthStore(auth_db)
    store.set_passphrase(PASS)
    gate = create_http_app(None, db_path=db, public_url=public_url, auth_db=auth_db,
                           relay=relay, trust_proxy=False if relay else None)
    gate.seen, inner = [], gate.app

    async def stub(scope, receive, send):
        if scope["path"].rstrip("/") != "/mcp":
            return await inner(scope, receive, send)
        gate.seen.append(scope["user"].access_token)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    gate.app = stub
    return gate, store


def _resource(public_url):
    # what FlowState publishes and puts in every token: the configured address + /mcp
    return public_url.rstrip("/") + "/mcp"


def _base(public_url):
    u = urlparse(public_url)
    return f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port not in (None, 443, 80) else "")


def _mcp(gate, base, token):
    async def fn(c):
        return await c.post("/mcp", headers={"Authorization": f"Bearer {token}"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    return _run(gate, base, fn)


def _oauth_client(store, name="Old app"):
    from mcp.shared.auth import OAuthClientInformationFull
    cid = "oc_" + secrets.token_hex(6)
    store.save_oauth_client(OAuthClientInformationFull(
        client_id=cid, client_name=name, redirect_uris=[REDIRECT],
        token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
        response_types=["code"]))
    with store._conn() as c:
        c.execute("UPDATE clients SET approved_at = created_at WHERE id = ?", (cid,))
    return cid


def _row_token(store, resource, kind="access"):
    """A token row written exactly as FlowState's provider has always written it
    (FlowStateOAuthProvider._pair: store.issue(..., resource_url))."""
    from flowstate.remote_auth import ACCESS_TTL, SCOPES
    return store.issue(kind, _oauth_client(store), SCOPES, "fam_" + secrets.token_hex(8),
                       ACCESS_TTL, resource)


def _full_flow(gate, base, resource):
    """Register, authorize, approve on the consent page, exchange the code: a normal token."""
    v = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")

    async def fn(c):
        reg = await c.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "Test app",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        cid = reg.json()["client_id"]
        r = await c.get("/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st",
            "resource": resource})
        assert r.status_code == 302, r.text
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        r = await c.post("/consent", data={"req": req, "decision": "approve",
                                           "passphrase": PASS, "access": "write"})
        assert r.status_code == 302, r.text
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        t = await c.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": cid, "code_verifier": v, "resource": resource})
        assert t.status_code == 200, t.text
        prm = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
        return t.json()["access_token"], prm["resource"]
    return _run(gate, base, fn)


@pytest.mark.parametrize("public_url,relay", ADDRESSES)
def test_normal_flowstate_token_passes_with_its_audience(seeded_db, tmp_path, public_url, relay):
    gate, _ = _app(seeded_db, tmp_path, public_url, relay)
    base = _base(public_url)
    token, published = _full_flow(gate, base, _resource(public_url))
    r = _mcp(gate, base, token)
    assert r.status_code == 200, r.text
    # the verifier reports the token's audience; it is the published resource exactly
    assert gate.seen[-1].resource == _resource(public_url)
    if public_url.lower() == public_url:
        assert gate.seen[-1].resource == published


@pytest.mark.parametrize("public_url,relay", ADDRESSES)
def test_token_issued_before_this_change_still_passes(seeded_db, tmp_path, public_url, relay):
    from flowstate.remote_auth import AuthStore
    # the row is in the auth DB before the app (with validation on) is even built
    auth_db = tmp_path / "old-auth.db"
    old = _row_token(AuthStore(auth_db), _resource(public_url))
    from flowstate.http_app import create_http_app
    gate = create_http_app(None, db_path=seeded_db, public_url=public_url, auth_db=auth_db,
                           relay=relay, trust_proxy=False if relay else None)
    seen = []

    async def stub(scope, receive, send):
        seen.append(scope["user"].access_token)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    gate.app = stub
    r = _mcp(gate, _base(public_url), old)
    assert r.status_code == 200, r.text
    assert seen[-1].client_name == "Old app" and seen[-1].resource == _resource(public_url)


@pytest.mark.parametrize("public_url,relay", ADDRESSES)
@pytest.mark.parametrize("audience", [OTHER, None, "https://abc123.fsmcp.com/other",
                                      "not a url"])
def test_token_for_another_resource_is_refused(seeded_db, tmp_path, public_url, relay, audience):
    gate, store = _app(seeded_db, tmp_path, public_url, relay)
    tok = _row_token(store, audience)
    r = _mcp(gate, _base(public_url), tok)
    assert r.status_code == 401, r.text
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert gate.seen == []


def test_refusal_is_the_sdk_resource_check(seeded_db, tmp_path):
    """The verifier itself accepts the foreign token and reports its audience; the SDK's
    bearer backend (validate_token_resource) is what refuses it."""
    from flowstate.remote_auth import TokenChecker
    gate, store = _app(seeded_db, tmp_path, "https://flowstate.example.com")
    tok = _row_token(store, OTHER)
    seen = asyncio.run(gate.checker.verify_token(tok))
    assert isinstance(gate.checker, TokenChecker) and seen.resource == OTHER
    assert str(gate.bearer.resource_server_url) == "https://flowstate.example.com/mcp"


def test_per_client_and_owner_tokens_carry_this_servers_audience(seeded_db, tmp_path):
    """`flowstate remote add-client` tokens (and the owner token when allowed) are made by
    this server for this server; they report its resource and keep working."""
    from flowstate.http_app import create_http_app
    from flowstate.remote_auth import AuthStore, SCOPES
    auth_db = tmp_path / "auth.db"
    _, static = AuthStore(auth_db).add_token_client("Script", SCOPES)
    owner = "o" * 32
    gate = create_http_app(owner, db_path=seeded_db, public_url="https://flowstate.example.com",
                           auth_db=auth_db, allow_master_token=True)
    seen = []

    async def stub(scope, receive, send):
        seen.append(scope["user"].access_token)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    gate.app = stub
    for tok in (static, owner):
        assert _mcp(gate, "https://flowstate.example.com", tok).status_code == 200
    assert [t.resource for t in seen] == ["https://flowstate.example.com/mcp"] * 2


def test_lan_listener_has_no_audience_check_and_no_oauth(seeded_db, tmp_path):
    from flowstate.http_app import create_http_app
    from flowstate.remote_auth import AuthStore
    auth_db = tmp_path / "auth.db"
    store = AuthStore(auth_db)
    oauth = _row_token(store, "http://127.0.0.1:8766/mcp")
    gate = create_http_app("t" * 32, db_path=seeded_db, auth_db=auth_db)
    gate.app = lambda *a: None
    assert gate.bearer.resource_server_url is None
    # OAuth tokens never open the LAN listener, whatever their audience (as before)
    assert _mcp(gate, "http://127.0.0.1:8766", oauth).status_code == 401
