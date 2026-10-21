"""End to end over a fake relay that only does SNI routing and byte splicing.

  client (TLS to k7q3m2.fsmcp.com, trusts only the test CA)
    -> FakeTunnelRelay public port: peeks SNI, splices bytes, holds no user key
    -> agent data connection (TLS to the relay's own tunnel cert)
    -> agent terminates the client's TLS with the locally generated key
    -> FlowState public-mode HTTP app (OAuth 2.1 + PKCE + passphrase consent)

Enrollment and the certificate go through FakeRelayAPI + FakeACME, as in real use. Checks:
the OAuth flow, read-only, revoke and MCP work unchanged; the client saw the agent's
certificate; the relay never loaded the user's key and every byte it carried was ciphertext.
"""

import asyncio
import base64
import hashlib
import http.client
import json
import logging
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

from flowstate.relay import store as S  # noqa: E402
from relay_fakes import FakeACME, FakeRelayAPI, FakeTunnelRelay, TestCA, spki_sha256  # noqa: E402

HOST = "k7q3m2.fsmcp.com"
PASS = "correct horse battery staple"
REDIRECT = "http://127.0.0.1:9/callback"


class _Conn(http.client.HTTPSConnection):
    """HTTPS to HOST (SNI, Host header, certificate check) but dialled at the fake relay."""

    def __init__(self, relay_port, ctx):
        super().__init__(HOST, 443, context=ctx, timeout=20)
        self._relay_port = relay_port

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self._relay_port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=HOST)


class Client:
    def __init__(self, relay_port, ca_file):
        self.port, self.ctx = relay_port, ssl.create_default_context(cafile=str(ca_file))
        self.peer_certs = []

    def req(self, method, path, body=None, headers=None, form=None, js=None):
        h = dict(headers or {})
        if form is not None:
            body = urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if js is not None:
            body = json.dumps(js).encode()
            h["Content-Type"] = "application/json"
        c = _Conn(self.port, self.ctx)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
            self.peer_certs.append(c.sock.getpeercert(binary_form=True))
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            c.close()


class RelayThread:
    def __init__(self, store, db):
        self.store, self.db = store, db
        self.ready = threading.Event()
        self.error = None

    def start(self):
        from flowstate.relay import runner

        async def main():
            self.loop = asyncio.get_running_loop()
            self.stop_ev = asyncio.Event()
            try:
                await runner.run(self.store, db_path=self.db, port=0, stop=self.stop_ev,
                                 ct_fetch=False, acme_sleep=lambda s: None,
                                 on_ready=lambda p: self.ready.set())
            except BaseException as e:  # noqa: BLE001
                self.error = e
                self.ready.set()
        self.t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
        self.t.start()
        assert self.ready.wait(30) and self.error is None, self.error
        deadline = time.time() + 30
        while time.time() < deadline:
            st = self.store.status()
            if st.get("tunnel") == "connected" and st.get("cert_not_after"):
                return self
            time.sleep(0.05)
        raise AssertionError(f"relay agent did not come up: {self.store.status()}")

    def stop(self):
        self.loop.call_soon_threadsafe(self.stop_ev.set)
        self.t.join(30)


@pytest.fixture
def world(tmp_path, seeded_db, monkeypatch):
    from flowstate import cli
    from flowstate import remote_auth as RA
    from flowstate import server as SV
    ca = TestCA(tmp_path / "ca")
    api = FakeRelayAPI().start()
    acme_srv = FakeACME(ca, api).start()
    monkeypatch.setenv("FLOWSTATE_RELAY_API", api.url)
    monkeypatch.setenv("FLOWSTATE_RELAY_ACME_DIRECTORY", acme_srv.url + "/directory")
    monkeypatch.setenv("FLOWSTATE_RELAY_DNS_WAIT", "0")
    monkeypatch.setenv("FLOWSTATE_RELAY_CA_FILE", str(ca.cert_file))
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(tmp_path / "config" / "remote-auth.db"))
    relay = None
    agent = None
    try:
        # enroll + certificate, exactly as a user would
        relay_tokens = {}
        relay = FakeTunnelRelay(ca, relay_tokens).start()
        monkeypatch.setenv("FLOWSTATE_RELAY_TUNNEL", f"127.0.0.1:{relay.tunnel_port}")
        assert cli.main(["relay", "enroll", "--code", "TEST-CODE-1234"]) == 0
        store = S.RelayStore()
        relay_tokens[store.token()] = "k7q3m2"
        RA.AuthStore().set_passphrase(PASS)
        agent = RelayThread(store, seeded_db).start()
        yield {"ca": ca, "api": api, "acme": acme_srv, "relay": relay, "store": store,
               "client": Client(relay.public_port, ca.cert_file)}
    finally:
        if agent:
            agent.stop()
        if relay:
            relay.stop()
        acme_srv.stop()
        api.stop()
        SV.backend.tracker = None


def _pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


def _oauth(c: Client, access="write"):
    base = f"https://{HOST}"
    s, _, body = c.req("POST", "/register", js={
        "redirect_uris": [REDIRECT], "client_name": "Relay test app", "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert s == 201, body
    cid = json.loads(body)["client_id"]
    verifier, challenge = _pkce()
    q = urlencode({"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                   "code_challenge": challenge, "code_challenge_method": "S256",
                   "state": "st-1", "resource": base + "/mcp"})
    s, h, body = c.req("GET", "/authorize?" + q)
    assert s == 302, body
    consent = h["location"]
    assert consent.startswith(base + "/consent?req=")
    s, h, page = c.req("GET", consent[len(base):])
    assert s == 200 and b"Relay test app" in page and h["x-frame-options"] == "DENY"
    req = parse_qs(urlparse(consent).query)["req"][0]
    s, h, _ = c.req("POST", "/consent", form={"req": req, "decision": "approve",
                                              "passphrase": "wrong passphrase!!", "access": access})
    assert s == 403  # passphrase consent still enforced
    s, h, _ = c.req("POST", "/consent", form={"req": req, "decision": "approve",
                                              "passphrase": PASS, "access": access})
    assert s == 302
    code = parse_qs(urlparse(h["location"]).query)["code"][0]
    s, _, body = c.req("POST", "/token", form={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": cid, "code_verifier": verifier, "resource": base + "/mcp"})
    assert s == 200, body
    return json.loads(body), cid


MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _rpc(c, token, method, params=None, sid=None, id_=1):
    h = dict(MCP_HEADERS, Authorization=f"Bearer {token}")
    if sid:
        h["Mcp-Session-Id"] = sid
        h["Mcp-Protocol-Version"] = "2025-06-18"
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id_ is not None:
        msg["id"] = id_
    return c.req("POST", "/mcp", js=msg, headers=h)


def _mcp_session(c, token):
    s, h, body = _rpc(c, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "relay-test", "version": "1"}})
    assert s == 200, body
    sid = h["mcp-session-id"]
    s, _, _ = _rpc(c, token, "notifications/initialized", sid=sid, id_=None)
    assert s == 202
    return sid


def test_full_flow_over_relay_and_relay_sees_only_ciphertext(world, caplog):
    c, store, relay = world["client"], world["store"], world["relay"]
    with caplog.at_level(logging.INFO, logger="flowstate.access"):
        s, _, body = c.req("GET", "/health", headers={"CF-Connecting-IP": "198.51.100.1"})
    assert s == 200 and json.loads(body)["ok"] is True

    # the client talked TLS with the agent's certificate, made from the local key
    leaf = x509.load_der_x509_certificate(c.peer_certs[-1])
    local_key = serialization.load_pem_private_key(store.read_bytes(S.TLS_KEY), None)
    assert spki_sha256(leaf.public_key()) == spki_sha256(local_key.public_key())

    # unauthenticated MCP -> 401 pointing at the relay hostname's metadata
    s, h, _ = c.req("POST", "/mcp", js={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=MCP_HEADERS)
    assert s == 401 and f"https://{HOST}/.well-known/oauth-protected-resource" in h["www-authenticate"]
    s, _, body = c.req("GET", "/.well-known/oauth-authorization-server")
    assert s == 200 and json.loads(body)["issuer"].rstrip("/") == f"https://{HOST}"

    # OAuth 2.1 + PKCE + passphrase consent, then MCP
    tokens, cid = _oauth(c)
    sid = _mcp_session(c, tokens["access_token"])
    s, _, body = _rpc(c, tokens["access_token"], "tools/list", sid=sid, id_=2)
    assert s == 200 and b"start_here" in body
    s, _, body = _rpc(c, tokens["access_token"], "tools/call",
                      {"name": "start_here", "arguments": {"project": "Alpha"}}, sid=sid, id_=3)
    assert s == 200 and b"Alpha" in body

    # read-only option still enforced
    ro, ro_cid = _oauth(c, access="read")
    assert ro["scope"].split() == ["flowstate:read"]
    ro_sid = _mcp_session(c, ro["access_token"])
    s, _, body = _rpc(c, ro["access_token"], "tools/call",
                      {"name": "log_work", "arguments": {"project": "Alpha", "kind": "learning",
                                                         "summary": "refused for read-only"}},
                      sid=ro_sid, id_=4)
    assert s == 200 and b"read_only_client" in body

    # per-app revoke still works
    from flowstate import remote_auth as RA
    assert RA.AuthStore().revoke_client(cid)
    s, _, _ = _rpc(c, tokens["access_token"], "tools/list", sid=sid, id_=5)
    assert s == 401

    # rate limits / access log use the client address the relay reported, not a header
    assert "ip=203.0.113.7" in caplog.text and "198.51.100.1" not in caplog.text

    # ---- what the relay saw ----
    seen = bytes(relay.captured)
    assert len(seen) > 2000
    assert relay.loaded_key_files == [str(relay.key_file)]   # only its own tunnel key
    assert set(relay.sni_seen) == {HOST}                      # it learns the hostname, nothing more
    for secret in (b"GET /health", b"HTTP/1.1", b"/mcp", b"Bearer", b"\"service\"",
                   b"start_here", b"Alpha", b"Relay test app", PASS.encode(),
                   tokens["access_token"].encode(), tokens["refresh_token"].encode(),
                   store.token().encode()):
        assert secret not in seen, secret
    der_key = local_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
    scalar = local_key.private_numbers().private_value.to_bytes(32, "big")
    for blob in (der_key, scalar, store.read_bytes(S.TLS_KEY)):
        assert blob not in seen
    # TLS 1.3: even the certificate travels encrypted
    assert leaf.public_bytes(serialization.Encoding.DER) not in seen


def test_disable_stops_running_agent(world):
    from flowstate import cli
    store = world["store"]
    assert cli.main(["relay", "disable"]) == 0
    deadline = time.time() + 20
    while time.time() < deadline and store.status().get("tunnel") != "stopped":
        time.sleep(0.1)
    assert store.status().get("tunnel") == "stopped"
    with pytest.raises((ConnectionError, ssl.SSLError, OSError)):
        world["client"].req("GET", "/health")
