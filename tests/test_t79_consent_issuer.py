"""Task 79: the consent link is built from the same issuer string FlowState publishes (task
78), so an unusual `--public-url` (mixed-case host, explicit default port) can't give the
consent page another address than the issuer. Runs the whole HTTP app in-process (no network,
temp auth DBs)."""

import base64
import hashlib
import re
import secrets

import pytest

from test_t78_oauth_issuer import REDIRECT, _app, _run

# (configured public URL, relay?)
UNUSUAL = [("https://FlowState.Example.com:443", False),
           ("https://FlowState.Example.com:443/", False)]
NORMAL = [("https://abc123.fsmcp.com", True),          # relay device address
          ("https://abc123.fsmcp.com/", True),
          ("https://flowstate.example.com", False),    # own tunnel
          ("http://127.0.0.1:8766", False),            # local listener
          ("http://127.0.0.1:8766/", False)]


def _issuer_and_consent(app, base, resource):
    v = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")

    async def fn(c):
        issuer = (await c.get("/.well-known/oauth-authorization-server")).json()["issuer"]
        reg = await c.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "Test app",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert reg.status_code == 201, reg.text
        r = await c.get("/authorize", params={
            "response_type": "code", "client_id": reg.json()["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "st", "resource": resource})
        assert r.status_code == 302, r.text
        return issuer, r.headers["location"]
    return _run(app, base, fn)


@pytest.mark.parametrize("public_url,relay", UNUSUAL)
def test_consent_link_starts_with_published_issuer(seeded_db, tmp_path, public_url, relay):
    app = _app(seeded_db, tmp_path, public_url, relay)
    # `resource` is unchanged by task 79: still the configured address + /mcp
    issuer, consent = _issuer_and_consent(app, "https://flowstate.example.com",
                                          public_url.rstrip("/") + "/mcp")
    assert issuer == "https://flowstate.example.com"
    assert consent.startswith(issuer + "/consent?req=")


@pytest.mark.parametrize("public_url,relay", NORMAL)
def test_consent_link_unchanged_for_normal_addresses(seeded_db, tmp_path, public_url, relay):
    app = _app(seeded_db, tmp_path, public_url, relay)
    base = public_url.rstrip("/")
    issuer, consent = _issuer_and_consent(app, base, base + "/mcp")
    # byte-identical to the link built before task 79 (configured address without '/')
    assert issuer == base
    assert re.fullmatch(re.escape(base) + r"/consent\?req=[A-Za-z0-9_-]+", consent), consent
