"""Task 81: no CORS on the OAuth endpoints a browser never needs, and a working RFC 7009 /revoke.

A security scan found /token answering `Access-Control-Allow-Origin: *` to any Origin (the MCP
SDK wraps /token, /register and /revoke in a CORS middleware for browser clients; FlowState
has none: /mcp itself sends no CORS headers). The same scan's /revoke calls, with an access and
a refresh token, both got 400 `invalid_request`: the SDK's revocation request model requires a
`client_secret` form field, which a public client (`none`) or a client using HTTP Basic never
sends. Runs the whole HTTP app in-process (no network, temp auth DBs)."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

from test_t78_oauth_issuer import PASS, REDIRECT, _run
from test_t80_token_audience import ADDRESSES, _app, _base, _resource

EVIL = "https://evil.example"


def _basic(cid, secret):
    return "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()


class Client:
    """A registered OAuth client and how it authenticates, as it registered."""

    def __init__(self, cid, secret, method):
        self.cid, self.secret, self.method = cid, secret, method

    def auth(self, data, form_id=False):
        """(form data, headers) carrying this client's authentication. The SDK's /token also
        wants `client_id` in the form with Basic (form_id=True); /revoke doesn't."""
        data, headers = dict(data), {}
        if self.method == "client_secret_basic":
            headers["Authorization"] = _basic(self.cid, self.secret)
            if form_id:
                data["client_id"] = self.cid
        else:
            data["client_id"] = self.cid
            if self.method == "client_secret_post":
                data["client_secret"] = self.secret
        return data, headers


async def _register(c, method):
    r = await c.post("/register", json={
        "redirect_uris": [REDIRECT], "client_name": f"App {secrets.token_hex(3)}",
        "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert r.status_code == 201, r.text
    j = r.json()
    return Client(j["client_id"], j.get("client_secret"), method)


async def _tokens(c, client, resource):
    """Authorize, approve on the consent page, exchange the code: (access, refresh)."""
    v = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    r = await c.get("/authorize", params={
        "response_type": "code", "client_id": client.cid, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st",
        "resource": resource})
    assert r.status_code == 302, r.text
    req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
    r = await c.post("/consent", data={"req": req, "decision": "approve",
                                       "passphrase": PASS, "access": "write"})
    assert r.status_code == 302, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    data, headers = client.auth({"grant_type": "authorization_code", "code": code,
                                 "redirect_uri": REDIRECT, "code_verifier": v,
                                 "resource": resource}, form_id=True)
    t = await c.post("/token", data=data, headers=headers)
    assert t.status_code == 200, t.text
    return t.json()["access_token"], t.json()["refresh_token"]


async def _revoke(c, client, token, hint=None, **extra):
    data = {"token": token, **({"token_type_hint": hint} if hint else {}), **extra}
    data, headers = client.auth(data)
    return await c.post("/revoke", data=data, headers=headers)


async def _mcp_ok(c, token):
    r = await c.post("/mcp", headers={"Authorization": f"Bearer {token}"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    return r.status_code == 200


async def _refresh_ok(c, client, refresh):
    data, headers = client.auth({"grant_type": "refresh_token", "refresh_token": refresh},
                                form_id=True)
    r = await c.post("/token", data=data, headers=headers)
    return r.status_code == 200


def _go(seeded_db, tmp_path, fn, public_url="https://flowstate.example.com", relay=False):
    gate, _ = _app(seeded_db, tmp_path, public_url, relay)
    resource = _resource(public_url)
    return _run(gate, _base(public_url), lambda c: fn(c, resource))


# ---------------------------------------------------------------- CORS

def _acao(r):
    return r.headers.get("access-control-allow-origin")


@pytest.mark.parametrize("public_url,relay", ADDRESSES)
def test_token_answers_no_cors_to_any_origin(seeded_db, tmp_path, public_url, relay):
    async def fn(c, resource):
        r = await c.post("/token", data={"grant_type": "authorization_code", "code": "x",
                                         "client_id": "nope"}, headers={"Origin": EVIL})
        return r
    r = _go(seeded_db, tmp_path, fn, public_url, relay)
    assert r.status_code in (400, 401)
    assert _acao(r) is None
    assert not any(k.lower().startswith("access-control-") for k in r.headers)


@pytest.mark.parametrize("path", ["/token", "/register", "/revoke"])
def test_preflight_not_granted_to_arbitrary_origins(seeded_db, tmp_path, path):
    async def fn(c, resource):
        return await c.request("OPTIONS", path, headers={
            "Origin": EVIL, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, authorization"})
    r = _go(seeded_db, tmp_path, fn)
    assert r.status_code != 200
    assert _acao(r) is None
    assert "access-control-allow-methods" not in r.headers


@pytest.mark.parametrize("path,body", [("/register", {"redirect_uris": [REDIRECT]}),
                                       ("/revoke", None)])
def test_register_and_revoke_answer_no_cors(seeded_db, tmp_path, path, body):
    async def fn(c, resource):
        if body:
            return await c.post(path, json=body, headers={"Origin": EVIL})
        return await c.post(path, data={"token": "x", "client_id": "nope"},
                            headers={"Origin": EVIL})
    r = _go(seeded_db, tmp_path, fn)
    assert _acao(r) is None


def test_authorize_still_has_no_cors(seeded_db, tmp_path):
    async def fn(c, resource):
        return await c.get("/authorize", params={"client_id": "nope"}, headers={"Origin": EVIL})
    assert _acao(_go(seeded_db, tmp_path, fn)) is None


@pytest.mark.parametrize("path", ["/.well-known/oauth-authorization-server",
                                  "/.well-known/oauth-protected-resource",
                                  "/.well-known/oauth-protected-resource/mcp"])
def test_well_known_metadata_keeps_public_cors(seeded_db, tmp_path, path):
    """Public discovery documents (no secrets in or out) stay readable from any page, as the
    SDK serves them; the issuer is still the exact string (task 78)."""
    async def fn(c, resource):
        g = await c.get(path, headers={"Origin": EVIL})
        p = await c.request("OPTIONS", path, headers={"Origin": EVIL,
                                                      "Access-Control-Request-Method": "GET"})
        return g, p
    g, p = _go(seeded_db, tmp_path, fn)
    assert g.status_code == 200 and _acao(g) == "*"
    assert p.status_code == 200 and _acao(p) == "*"
    j = g.json()
    assert j.get("issuer", "https://flowstate.example.com") == "https://flowstate.example.com"


def test_metadata_still_lists_revocation_endpoint(seeded_db, tmp_path):
    async def fn(c, resource):
        return (await c.get("/.well-known/oauth-authorization-server")).json()
    j = _go(seeded_db, tmp_path, fn)
    assert j["revocation_endpoint"] == "https://flowstate.example.com/revoke"
    assert set(j["revocation_endpoint_auth_methods_supported"]) >= {
        "client_secret_post", "client_secret_basic"}


# ---------------------------------------------------------------- /revoke

METHODS = ["none", "client_secret_post", "client_secret_basic"]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("hint", [None, "access_token", "refresh_token", "something_else"])
def test_revoke_access_token_any_hint(seeded_db, tmp_path, method, hint):
    async def fn(c, resource):
        cl = await _register(c, method)
        access, refresh = await _tokens(c, cl, resource)
        assert await _mcp_ok(c, access)
        r = await _revoke(c, cl, access, hint)
        return r, await _mcp_ok(c, access), await _refresh_ok(c, cl, refresh)
    r, access_ok, refresh_ok = _go(seeded_db, tmp_path, fn)
    assert r.status_code == 200, r.text
    assert r.content == b""
    assert r.headers.get("cache-control") == "no-store"
    assert not access_ok          # the revoked token is refused afterwards
    assert not refresh_ok         # and its refresh token with it (one grant, one family)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("hint", [None, "refresh_token", "access_token"])
def test_revoke_refresh_token_any_hint(seeded_db, tmp_path, method, hint):
    async def fn(c, resource):
        cl = await _register(c, method)
        access, refresh = await _tokens(c, cl, resource)
        r = await _revoke(c, cl, refresh, hint)
        return r, await _refresh_ok(c, cl, refresh), await _mcp_ok(c, access)
    r, refresh_ok, access_ok = _go(seeded_db, tmp_path, fn)
    assert r.status_code == 200, r.text
    assert not refresh_ok
    assert not access_ok          # RFC 7009 2.1: revoking a refresh token ends its access tokens


@pytest.mark.parametrize("public_url,relay", ADDRESSES)
def test_revoke_on_every_address(seeded_db, tmp_path, public_url, relay):
    async def fn(c, resource):
        cl = await _register(c, "client_secret_basic")
        access, _ = await _tokens(c, cl, resource)
        r = await _revoke(c, cl, access, "access_token")
        return r, await _mcp_ok(c, access)
    r, ok = _go(seeded_db, tmp_path, fn, public_url, relay)
    assert r.status_code == 200, r.text
    assert not ok


@pytest.mark.parametrize("method", METHODS)
def test_revoke_unknown_token_is_200(seeded_db, tmp_path, method):
    async def fn(c, resource):
        cl = await _register(c, method)
        access, _ = await _tokens(c, cl, resource)
        rs = [await _revoke(c, cl, t) for t in ("fs_at_" + secrets.token_urlsafe(24),
                                                 "fs_rt_" + secrets.token_urlsafe(24),
                                                 "not-a-flowstate-token")]
        return rs, await _mcp_ok(c, access)
    rs, ok = _go(seeded_db, tmp_path, fn)
    assert [r.status_code for r in rs] == [200, 200, 200]
    assert ok                     # nothing else was touched


def test_revoke_already_revoked_token_is_200(seeded_db, tmp_path):
    async def fn(c, resource):
        cl = await _register(c, "client_secret_post")
        access, refresh = await _tokens(c, cl, resource)
        return [await _revoke(c, cl, t) for t in (access, access, refresh)]
    assert [r.status_code for r in _go(seeded_db, tmp_path, fn)] == [200, 200, 200]


@pytest.mark.parametrize("victim_method", METHODS)
@pytest.mark.parametrize("which", ["access", "refresh"])
def test_revoke_by_wrong_client_is_refused_and_token_stays_valid(seeded_db, tmp_path,
                                                                 victim_method, which):
    async def fn(c, resource):
        victim = await _register(c, victim_method)
        other = await _register(c, "client_secret_post")
        access, refresh = await _tokens(c, victim, resource)
        await _tokens(c, other, resource)     # the other app is a real, approved client
        r = await _revoke(c, other, access if which == "access" else refresh)
        return r, await _mcp_ok(c, access), await _refresh_ok(c, victim, refresh)
    r, access_ok, refresh_ok = _go(seeded_db, tmp_path, fn)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "unauthorized_client"
    assert access_ok and refresh_ok


@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
def test_revoke_with_wrong_secret_is_401_and_token_stays_valid(seeded_db, tmp_path, method):
    async def fn(c, resource):
        cl = await _register(c, method)
        access, _ = await _tokens(c, cl, resource)
        bad = Client(cl.cid, "0" * 64, method)
        r = await _revoke(c, bad, access)
        return r, await _mcp_ok(c, access)
    r, ok = _go(seeded_db, tmp_path, fn)
    assert r.status_code == 401, r.text
    assert r.json()["error"] == "invalid_client"
    assert ok


def test_revoke_auth_must_be_as_registered(seeded_db, tmp_path):
    """A client registered for Basic can't drop its secret by sending only client_id, and a
    secret client can't pretend to be public."""
    async def fn(c, resource):
        basic = await _register(c, "client_secret_basic")
        post = await _register(c, "client_secret_post")
        a1, _ = await _tokens(c, basic, resource)
        a2, _ = await _tokens(c, post, resource)
        r1 = await c.post("/revoke", data={"token": a1, "client_id": basic.cid})
        r2 = await c.post("/revoke", data={"token": a2, "client_id": post.cid})
        return r1, r2, await _mcp_ok(c, a1), await _mcp_ok(c, a2)
    r1, r2, ok1, ok2 = _go(seeded_db, tmp_path, fn)
    assert (r1.status_code, r2.status_code) == (401, 401)
    assert ok1 and ok2


def test_revoke_basic_with_mismatched_form_client_id_is_refused(seeded_db, tmp_path):
    async def fn(c, resource):
        a = await _register(c, "client_secret_basic")
        b = await _register(c, "client_secret_basic")
        access, _ = await _tokens(c, a, resource)
        r = await c.post("/revoke", data={"token": access, "client_id": b.cid},
                         headers={"Authorization": _basic(a.cid, a.secret)})
        return r, await _mcp_ok(c, access)
    r, ok = _go(seeded_db, tmp_path, fn)
    assert r.status_code == 401
    assert ok


def test_revoke_without_client_or_token(seeded_db, tmp_path):
    async def fn(c, resource):
        cl = await _register(c, "client_secret_post")
        access, _ = await _tokens(c, cl, resource)
        no_client = await c.post("/revoke", data={"token": access})
        unknown_client = await c.post("/revoke", data={"token": access, "client_id": "oc_nope"})
        no_token = await _revoke(c, cl, "")
        return no_client, unknown_client, no_token, await _mcp_ok(c, access)
    no_client, unknown_client, no_token, ok = _go(seeded_db, tmp_path, fn)
    assert no_client.status_code == 401 and no_client.json()["error"] == "invalid_client"
    assert unknown_client.status_code == 401
    assert no_token.status_code == 400 and no_token.json()["error"] == "invalid_request"
    assert ok


def test_revoke_does_not_touch_per_client_tokens(seeded_db, tmp_path):
    """A `flowstate remote add-client` token is not an OAuth client's; only the CLI revokes it."""
    gate, store = _app(seeded_db, tmp_path, "https://flowstate.example.com")
    from flowstate.remote_auth import SCOPES
    _, static = store.add_token_client("Static app", SCOPES)

    async def fn(c):
        cl = await _register(c, "client_secret_post")
        await _tokens(c, cl, "https://flowstate.example.com/mcp")
        r = await _revoke(c, cl, static)
        return r, await _mcp_ok(c, static)
    r, ok = _run(gate, "https://flowstate.example.com", fn)
    assert r.status_code == 400 and r.json()["error"] == "unauthorized_client"
    assert ok


def test_revoke_other_families_untouched(seeded_db, tmp_path):
    """Revoking one grant's tokens leaves the same app's other grant working."""
    async def fn(c, resource):
        cl = await _register(c, "client_secret_post")
        a1, _ = await _tokens(c, cl, resource)
        a2, r2 = await _tokens(c, cl, resource)
        r = await _revoke(c, cl, a1)
        return r, await _mcp_ok(c, a1), await _mcp_ok(c, a2), await _refresh_ok(c, cl, r2)
    r, ok1, ok2, ok_r2 = _go(seeded_db, tmp_path, fn)
    assert r.status_code == 200
    assert not ok1 and ok2 and ok_r2
