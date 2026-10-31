"""Task 78: FlowState publishes one exact OAuth issuer, without a trailing slash.

A security reviewer's scan found the relay device's issuer published as
`https://<device>.fsmcp.com/` (Pydantic's URL type adds '/' to a bare host), which strict
issuer comparison (RFC 8414 section 3.3) treats as a different issuer from the address
without it. These tests run the whole HTTP app in-process (no network, temp auth DBs)."""

import asyncio
import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

PASS = "correct horse battery staple"   # test-only value
REDIRECT = "http://127.0.0.1:9/callback"

# (configured public URL, the issuer FlowState must publish, relay?)
CASES = [
    ("https://abc123.fsmcp.com", "https://abc123.fsmcp.com", True),      # relay device address
    ("https://abc123.fsmcp.com/", "https://abc123.fsmcp.com", True),
    ("https://flowstate.example.com", "https://flowstate.example.com", False),   # own tunnel
    ("https://flowstate.example.com/", "https://flowstate.example.com", False),
    ("http://127.0.0.1:8766", "http://127.0.0.1:8766", False),          # local listener
    ("http://127.0.0.1:8766/", "http://127.0.0.1:8766", False),
]


def _app(db, tmp_path, public_url, relay):
    from flowstate.http_app import create_http_app
    from flowstate.remote_auth import AuthStore
    auth_db = tmp_path / f"auth-{secrets.token_hex(4)}.db"
    AuthStore(auth_db).set_passphrase(PASS)
    return create_http_app(None, db_path=db, public_url=public_url, auth_db=auth_db,
                           relay=relay, trust_proxy=False if relay else None)


def _run(app, base, fn):
    async def go():
        t = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=t, base_url=base) as c:
            return await fn(c)
    return asyncio.run(go())


def _no_double_slash(url):
    return "//" not in url.split("://", 1)[1]


@pytest.mark.parametrize("public_url,issuer,relay", CASES)
def test_issuer_is_exact_and_matches_authorization_servers(seeded_db, tmp_path, public_url,
                                                           issuer, relay):
    app = _app(seeded_db, tmp_path, public_url, relay)

    async def fn(c):
        return ((await c.get("/.well-known/oauth-authorization-server")),
                (await c.get("/.well-known/oauth-protected-resource")),
                (await c.get("/.well-known/oauth-protected-resource/mcp")),
                (await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Accept": "application/json, text/event-stream"})))
    asm, prm_bare, prm_path, challenge = _run(app, issuer, fn)
    assert asm.status_code == prm_bare.status_code == prm_path.status_code == 200
    asm, prm_bare, prm_path = asm.json(), prm_bare.json(), prm_path.json()
    assert asm["issuer"] == issuer
    for prm in (prm_bare, prm_path):
        assert prm["authorization_servers"] == [issuer]
        assert prm["resource"] == issuer + "/mcp"        # unchanged: no slash was ever added
    assert asm["authorization_endpoint"] == issuer + "/authorize"
    assert asm["token_endpoint"] == issuer + "/token"
    assert asm["registration_endpoint"] == issuer + "/register"
    assert asm["revocation_endpoint"] == issuer + "/revoke"
    for v in asm.values():
        if isinstance(v, str) and "://" in v:
            assert _no_double_slash(v), v
    # the rest of the document is as before
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert set(asm["scopes_supported"]) == {"flowstate:read", "flowstate:write"}
    assert prm_bare == prm_path and prm_bare["bearer_methods_supported"] == ["header"]
    # the 401 challenge points at the path-suffixed resource metadata, no double slash
    assert challenge.status_code == 401
    rmd = re.search(r'resource_metadata="([^"]+)"', challenge.headers["www-authenticate"])[1]
    assert rmd == issuer + "/.well-known/oauth-protected-resource/mcp"


@pytest.mark.parametrize("public_url,issuer,relay", [CASES[1], CASES[3]])
def test_flow_emits_no_other_issuer(seeded_db, tmp_path, public_url, issuer, relay):
    """Registration, authorize, consent and token: every URL FlowState hands back is under the
    same issuer, and an `iss` (RFC 9207), if one is ever sent, is exactly the issuer."""
    app = _app(seeded_db, tmp_path, public_url, relay)
    v = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")

    async def fn(c):
        reg = await c.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "Test app",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert reg.status_code == 201, reg.text
        cid = reg.json()["client_id"]
        r = await c.get("/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st",
            "resource": issuer + "/mcp"})
        assert r.status_code == 302, r.text
        consent = r.headers["location"]
        assert consent.startswith(issuer + "/consent?req=") and _no_double_slash(consent)
        req = parse_qs(urlparse(consent).query)["req"][0]
        r = await c.post("/consent", data={"req": req, "decision": "approve",
                                           "passphrase": PASS, "access": "read"})
        assert r.status_code == 302, r.text
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q.get("iss", [issuer]) == [issuer]
        t = await c.post("/token", data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
            "client_id": cid, "code_verifier": v, "resource": issuer + "/mcp"})
        assert t.status_code == 200, t.text
        return t.json()
    tok = _run(app, issuer, fn)
    assert tok.get("iss", issuer) == issuer and tok["token_type"].lower() == "bearer"


def test_lan_listener_still_has_no_oauth(seeded_db, tmp_path):
    from flowstate.http_app import create_http_app
    app = create_http_app("lan-owner-token-" + "x" * 20, db_path=seeded_db,
                          auth_db=tmp_path / "lan.db")

    async def fn(c):
        return [(await c.get(p)).status_code for p in (
            "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp")]
    assert _run(app, "http://127.0.0.1:8766", fn) == [404, 404, 404]
