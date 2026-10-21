"""Relay agent unit tests: enrollment storage, ACME DNS-01 (fake relay API + fake ACME),
reconnect / heartbeat logic, Certificate Transparency watch, CLI. Temp folders and loopback
fakes only; nothing reaches Let's Encrypt, crt.sh, fsmcp.com or flowstatemcp.com."""

import asyncio
import json
import os
import ssl
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from flowstate.relay import RelayError  # noqa: E402
from flowstate.relay import acme, api, ct, store as S  # noqa: E402
from flowstate.relay import crypto as C  # noqa: E402
from flowstate.relay.tunnel import Backoff, TunnelAgent, client_ip  # noqa: E402
from relay_fakes import (FakeACME, FakeRelayAPI, FakeTunnelRelay, TestCA, TUNNEL_HOST,  # noqa: E402
                         parse_sni)

POSIX = os.name == "posix"


def _cli(*argv):
    from flowstate import cli
    return cli.main(list(argv))


@pytest.fixture
def ca(tmp_path):
    return TestCA(tmp_path / "ca")


@pytest.fixture
def relay_api():
    a = FakeRelayAPI().start()
    yield a
    a.stop()


@pytest.fixture
def acme_srv(ca, relay_api, monkeypatch):
    s = FakeACME(ca, relay_api).start()
    monkeypatch.setenv("FLOWSTATE_RELAY_ACME_DIRECTORY", s.url + "/directory")
    monkeypatch.setenv("FLOWSTATE_RELAY_DNS_WAIT", "0")
    yield s
    s.stop()


@pytest.fixture
def enrolled(relay_api, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_RELAY_API", relay_api.url)
    assert _cli("relay", "enroll", "--code", "TEST-CODE-1234", "--skip-cert") == 0
    return S.RelayStore()


# ---------------------------------------------------------------- enrollment storage
def test_off_by_default(capsys):
    st = S.RelayStore()
    assert not st.root.exists()
    assert _cli("relay", "status") == 0
    assert "not set up" in capsys.readouterr().out
    assert S.tool_warning() is None
    assert not st.root.exists()  # status didn't create anything


def test_enroll_stores_handle_and_token_owner_only(enrolled, relay_api, capsys):
    st = enrolled
    cfg = st.config()
    assert cfg["handle"] == "k7q3m2" and cfg["hostname"] == "k7q3m2.fsmcp.com"
    assert cfg["enabled"] is True and cfg["staging"] is False
    token = st.token()
    assert token in relay_api.tokens
    # the token is only in its own file, never in the settings file or on screen
    assert token not in st.path(S.CONFIG).read_text()
    assert token not in capsys.readouterr().out
    # key made locally; its fingerprint is remembered for the CT watch
    assert cfg["known_spki"] == [C.key_spki_sha256(st.read_bytes(S.TLS_KEY))]
    if POSIX:
        assert stat.S_IMODE(st.root.stat().st_mode) == 0o700
        for name in (S.CONFIG, S.TOKEN, S.TLS_KEY):
            assert stat.S_IMODE(st.path(name).stat().st_mode) == 0o600, name


def test_enroll_refuses_twice_and_bad_codes(enrolled, capsys):
    assert _cli("relay", "enroll", "--code", "TEST-CODE-1234") == 1
    assert "Already enrolled" in capsys.readouterr().out
    with pytest.raises(RelayError):
        api.enroll("http://127.0.0.1:9", "bad code with spaces")


def test_enroll_wrong_code_leaves_nothing_secret(relay_api, monkeypatch, capsys):
    monkeypatch.setenv("FLOWSTATE_RELAY_API", relay_api.url)
    assert _cli("relay", "enroll", "--code", "WRONG-CODE", "--skip-cert") == 1
    assert "400" in capsys.readouterr().out
    assert not S.RelayStore().enrolled()


def test_enroll_waitlisted_says_so_in_plain_words(relay_api, monkeypatch, capsys):
    monkeypatch.setenv("FLOWSTATE_RELAY_API", relay_api.url)
    until = time.time() + 3 * 86400
    relay_api.waitlist_until = until
    assert _cli("relay", "enroll", "--code", "TEST-CODE-1234", "--skip-cert") == 1
    out = capsys.readouterr().out
    when = datetime.fromtimestamp(int(until)).astimezone().strftime("%Y-%m-%d %H:%M")
    assert "FlowState Relay is full this week, you're on the waitlist, try again after" in out
    assert when in out and "not used" in out
    assert "429" not in out and "Relay: relay answered" not in out
    assert not S.RelayStore().enrolled()
    # the code was not used: once the relay has room, the same code works
    relay_api.waitlist_until = None
    assert _cli("relay", "enroll", "--code", "TEST-CODE-1234", "--skip-cert") == 0
    assert S.RelayStore().config()["hostname"] == "k7q3m2.fsmcp.com"


def test_waitlisted_falls_back_to_retry_after_header(relay_api):
    from flowstate.relay import RelayWaitlisted
    relay_api.waitlist_until = time.time() + 7200
    relay_api.waitlist_body = {"error": "waitlisted"}           # no retry_after in the body
    with pytest.raises(RelayWaitlisted) as e:
        api.enroll(relay_api.url, "TEST-CODE-1234")
    assert abs(e.value.retry_after - (time.time() + 7200)) < 10
    assert "try again after" in str(e.value)
    relay_api.waitlist_body = {"error": "waitlisted", "retry_after": "soon"}  # junk: header used
    with pytest.raises(RelayWaitlisted) as e:
        api.enroll(relay_api.url, "TEST-CODE-1234")
    assert e.value.retry_after is not None
    relay_api.waitlist_body = {"error": "rate_limited"}         # plain 429: not "waitlisted"
    with pytest.raises(RelayError) as e:
        api.enroll(relay_api.url, "TEST-CODE-1234")
    assert not isinstance(e.value, RelayWaitlisted) and "try again" in str(e.value)


def test_names_derive_from_one_domain():
    import flowstate.relay as R
    assert R.RELAY_DOMAIN == "fsmcp.com" == R.HOST_SUFFIX
    assert R.API_BASE == "https://api.fsmcp.com" and R.TUNNEL_HOST == "tunnel.fsmcp.com"
    assert {"api", "tunnel", "www", "mail", "relay", "status", "admin", "app", "docs",
            "help"} <= R.RESERVED_HANDLES


@pytest.mark.parametrize("override", [
    {"handle": "../etc", "device_token": "x" * 40},
    {"handle": "k7q3m2", "device_token": "short"},
    {"handle": "k7q3m2", "device_token": "x" * 40, "hostname": "evil.example.com"},
    {"handle": "k7q3m2", "device_token": "x" * 40, "hostname": "k7q3m2.relay.flowstatemcp.com"},
    {"handle": "status", "device_token": "x" * 40, "hostname": "status.fsmcp.com"},
    {"handle": "admin", "device_token": "x" * 40},
])
def test_enroll_validates_relay_answer(relay_api, override):
    relay_api.response_override = override
    with pytest.raises(RelayError):
        api.enroll(relay_api.url, "TEST-CODE-1234")


def test_api_refuses_plain_http_off_loopback():
    with pytest.raises(RelayError):
        api.check_url("http://api.fsmcp.com/api/enroll")
    assert api.check_url("https://api.fsmcp.com/api/enroll")
    assert api.check_url("http://127.0.0.1:8/api/enroll")


def test_refuses_relay_dir_inside_git(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(RelayError, match="git"):
        S.RelayStore(repo / "relay").ensure_dir()


def test_disable_enable_forget(enrolled, capsys):
    st = enrolled
    assert _cli("relay", "disable") == 0 and st.config()["enabled"] is False
    assert _cli("relay", "enable") == 0 and st.config()["enabled"] is True
    assert _cli("relay", "forget", "--yes") == 0
    assert not st.root.exists()
    assert _cli("relay", "status") == 0
    assert "not set up" in capsys.readouterr().out


def test_status_shows_handle_url_and_hides_token(enrolled, capsys):
    capsys.readouterr()
    assert _cli("relay", "status") == 0
    out = capsys.readouterr().out
    assert "Handle: k7q3m2" in out and "https://k7q3m2.fsmcp.com/mcp" in out
    assert "Tunnel: not running" in out and "CT check: not run yet" in out
    assert enrolled.token() not in out


# ---------------------------------------------------------------- ACME
def test_acme_dns01_issues_cert_for_local_key(enrolled, acme_srv, relay_api):
    st = enrolled
    r = acme.ensure_cert(st, sleep=lambda s: None)
    assert r["renewed"] and r["reason"] == "no certificate yet"
    info = C.cert_info(st.read_bytes(S.CERT))
    assert info["names"] == ["k7q3m2.fsmcp.com"]
    assert info["spki_sha256"] == C.key_spki_sha256(st.read_bytes(S.TLS_KEY))
    # the relay was asked to publish exactly one TXT value for its own name, then remove it
    assert relay_api.calls == [("set", "_acme-challenge.k7q3m2.fsmcp.com"),
                               ("clear", "_acme-challenge.k7q3m2.fsmcp.com")]
    assert relay_api.txt == {}
    # private keys never left: the fake ACME only ever saw the public key in the CSR
    leaf = x509.load_pem_x509_certificate(acme_srv.issued[0])
    assert isinstance(leaf.public_key(), ec.EllipticCurvePublicKey)
    if POSIX:
        for name in (S.TLS_KEY, S.ACCOUNT_KEY):
            assert stat.S_IMODE(st.path(name).stat().st_mode) == 0o600
    # second run: nothing to do, account reused
    assert acme.ensure_cert(st)["renewed"] is False
    assert len(acme_srv.accounts) == 1


def test_acme_renews_30_days_before_expiry(enrolled, acme_srv):
    st = enrolled
    acme.ensure_cert(st, sleep=lambda s: None)
    na = C.cert_info(st.read_bytes(S.CERT))["not_after"]
    assert acme.needs_cert(st, now=na - timedelta(days=31)) is None
    assert "expires" in acme.needs_cert(st, now=na - timedelta(days=29))
    r = acme.ensure_cert(st, now=na - timedelta(days=29), sleep=lambda s: None)
    assert r["renewed"] and len(acme_srv.issued) == 2
    assert len(acme_srv.accounts) == 1  # same ACME account


def test_acme_same_key_across_renewals(enrolled, acme_srv):
    st = enrolled
    key = st.read_bytes(S.TLS_KEY)
    acme.ensure_cert(st, sleep=lambda s: None)
    acme.ensure_cert(st, force=True, sleep=lambda s: None)
    assert st.read_bytes(S.TLS_KEY) == key
    assert len(st.config()["known_spki"]) == 1


def test_acme_retries_bad_nonce(enrolled, ca, relay_api, monkeypatch):
    s = FakeACME(ca, relay_api, bad_nonce_once=True).start()
    try:
        monkeypatch.setenv("FLOWSTATE_RELAY_ACME_DIRECTORY", s.url + "/directory")
        assert acme.ensure_cert(enrolled, sleep=lambda x: None, propagation_seconds=0)["renewed"]
    finally:
        s.stop()


def test_acme_wrong_txt_fails_and_still_clears(enrolled, acme_srv, relay_api, monkeypatch):
    real = api.dns_challenge

    def wrong(base, token, host, value, ctx=None):
        return real(base, token, host, None if value is None else "not-the-right-value", ctx)
    monkeypatch.setattr(api, "dns_challenge", wrong)
    with pytest.raises(RelayError, match="DNS-01"):
        acme.ensure_cert(enrolled, sleep=lambda s: None)
    assert relay_api.calls[-1][0] == "clear" and relay_api.txt == {}
    assert not enrolled.path(S.CERT).exists()


def test_staging_flag_selects_staging_directory(enrolled, monkeypatch):
    assert acme.directory_url(True) == acme.LE_STAGING
    assert acme.directory_url(False) == acme.LE_PRODUCTION
    monkeypatch.setenv("FLOWSTATE_RELAY_ACME_DIRECTORY", "http://127.0.0.1:1/directory")
    assert acme.directory_url(True) == "http://127.0.0.1:1/directory"


def test_enroll_with_staging_records_it(relay_api, acme_srv, monkeypatch, capsys):
    monkeypatch.setenv("FLOWSTATE_RELAY_API", relay_api.url)
    assert _cli("relay", "enroll", "--code", "TEST-CODE-1234", "--staging") == 0
    out = capsys.readouterr().out
    assert "STAGING" in out and "Certificate obtained" in out
    assert S.RelayStore().config()["staging"] is True


# ---------------------------------------------------------------- reconnect logic
def test_backoff_doubles_with_jitter_and_caps():
    b = Backoff(base=1, cap=8, rand=lambda: 1.0)
    assert [b.next() for _ in range(6)] == [1, 2, 4, 8, 8, 8]
    b.reset()
    assert b.next() == 1
    lo = Backoff(base=1, cap=8, rand=lambda: 0.0)
    assert [lo.next() for _ in range(4)] == [0.5, 1, 2, 4]


def test_client_ip_parsing():
    assert client_ip("203.0.113.5:443") == "203.0.113.5"
    assert client_ip("[2001:db8::1]:443") == "2001:db8::1"
    assert client_ip("garbage") == "relay-unknown"


def _agent(relay, token, **kw):
    ctx = ssl.create_default_context(cafile=str(relay._ca_file))
    states = []
    a = TunnelAgent(host="127.0.0.1", port=relay.tunnel_port, sni=TUNNEL_HOST, token=token,
                    handle="k7q3m2", client_ctx=ctx, server_ctx=lambda: None,
                    local_port=lambda: None, peer_map={},
                    on_state=lambda s, d: states.append(s), min_heartbeat=0.1,
                    backoff=kw.pop("backoff", Backoff(base=0.05, cap=0.2, rand=lambda: 1.0)), **kw)
    return a, states


def _run_agent(agent, until, timeout=10):
    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(agent.run(stop))
        t0 = time.monotonic()
        while not until() and time.monotonic() - t0 < timeout and not task.done():
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, 5)
    asyncio.run(main())


@pytest.fixture
def tunnel(ca):
    made = []

    def make(**kw):
        r = FakeTunnelRelay(ca, {"tok-" + "a" * 40: "k7q3m2"}, **kw).start()
        r._ca_file = ca.cert_file
        made.append(r)
        return r
    yield make
    for r in made:
        r.stop()


def test_reconnects_with_backoff_when_relay_drops(tunnel):
    relay = tunnel(mode="drop")
    agent, states = _agent(relay, "tok-" + "a" * 40, stable_after=999)
    _run_agent(agent, lambda: len(agent.delays) >= 4)
    assert relay.connects >= 4
    assert agent.delays[:4] == [0.05, 0.1, 0.2, 0.2]  # doubling, capped
    assert "connected" in states and "reconnecting" in states and states[-1] == "stopped"
    assert relay.hellos[0]["handle"] == "k7q3m2" and relay.hellos[0]["proto"] == "flowstate-relay/1"


def test_backoff_resets_after_a_stable_connection(tunnel):
    relay = tunnel(mode="drop")
    agent, _ = _agent(relay, "tok-" + "a" * 40, stable_after=0)
    _run_agent(agent, lambda: len(agent.delays) >= 3)
    assert agent.delays[:3] == [0.05, 0.05, 0.05]


def test_dead_relay_detected_by_missing_heartbeat(tunnel):
    relay = tunnel(mode="silent", heartbeat=0.1)
    agent, states = _agent(relay, "tok-" + "a" * 40)
    t0 = time.monotonic()
    _run_agent(agent, lambda: relay.connects >= 2)
    assert relay.connects >= 2 and time.monotonic() - t0 < 5
    assert states.count("connected") >= 2


def test_heartbeat_keeps_healthy_connection(tunnel):
    relay = tunnel(heartbeat=0.1)
    agent, states = _agent(relay, "tok-" + "a" * 40)
    end = time.monotonic() + 1.0
    _run_agent(agent, lambda: time.monotonic() > end)
    assert relay.connects == 1 and agent.delays == []


def test_rejected_token_stops_without_retrying(tunnel):
    relay = tunnel()
    agent, states = _agent(relay, "wrong-token-" + "b" * 40)
    _run_agent(agent, lambda: False, timeout=2)
    assert states[-1] == "unauthorized" and relay.connects == 1


def test_open_without_certificate_is_rejected(tunnel):
    relay = tunnel(heartbeat=5)
    agent, _ = _agent(relay, "tok-" + "a" * 40)
    seen = {}

    def until():
        if relay.controls and "sent" not in seen:
            seen["sent"] = True
            w = relay.controls["k7q3m2"]
            relay.loop.call_soon_threadsafe(
                w.write, b'{"type":"open","stream":"abcdefghijklmnop1234","client":"1.2.3.4:5"}\n')
        return bool(relay.rejects)
    _run_agent(agent, until)
    assert relay.rejects[0]["reason"] == "no_certificate"


def test_sni_parser_reads_python_client_hello():
    import socket
    a, b = socket.socketpair()
    ctx = ssl.create_default_context()
    out = []

    def client():
        try:
            ctx.wrap_socket(a, server_hostname="k7q3m2.fsmcp.com")
        except (ssl.SSLError, OSError):
            pass
    t = threading.Thread(target=client)
    t.start()
    head = b""
    while len(head) < 5:
        head += b.recv(5 - len(head))
    n = int.from_bytes(head[3:5], "big")
    body = b""
    while len(body) < n:
        body += b.recv(n - len(body))
    out.append(parse_sni(head + body))
    b.close()
    t.join(5)
    a.close()
    assert out == ["k7q3m2.fsmcp.com"]


# ---------------------------------------------------------------- Certificate Transparency
def _crtsh(entries: dict):
    """fetch() stub: entries {id: (names, pem)} answer both crt.sh queries."""
    calls = []

    def fetch(url):
        calls.append(url)
        if "output=json" in url:
            return json.dumps([{"id": i, "name_value": "\n".join(n), "issuer_name": "x"}
                               for i, (n, _) in entries.items()]).encode()
        cid = int(url.rsplit("d=", 1)[1])
        return entries[cid][1]
    fetch.calls = calls
    return fetch


def _local_cert(st, ca, names=None):
    key = serialization.load_pem_private_key(st.read_bytes(S.TLS_KEY), None)
    return ca.issue(key.public_key(), names or [st.config()["hostname"]])


def _foreign_cert(ca, names):
    return ca.issue(ec.generate_private_key(ec.SECP256R1()).public_key(), names)


def test_ct_ok_when_all_certs_use_local_key(enrolled, ca):
    st = enrolled
    own = ["tunnel.fsmcp.com", "api.fsmcp.com"]
    fetch = _crtsh({101: (["k7q3m2.fsmcp.com"], _local_cert(st, ca)),
                    102: (["other.fsmcp.com"], _foreign_cert(ca, ["other.fsmcp.com"])),
                    103: (own, _foreign_cert(ca, own)),          # the relay's own certificate
                    104: (["k7q3m2.relay.flowstatemcp.com"],     # the old domain: not ours now
                          _foreign_cert(ca, ["k7q3m2.relay.flowstatemcp.com"]))})
    assert st.config()["hostname"] == "k7q3m2.fsmcp.com"
    assert ct.queries(st.config()["hostname"]) == ["k7q3m2.fsmcp.com", "*.fsmcp.com"]
    r = ct.run_check(st, fetch)
    assert r["ok"] and r["alerts"] == [] and r["certificates"] == 1 and r["error"] is None
    assert any("q=k7q3m2.fsmcp.com" in u for u in fetch.calls)
    assert any("q=%2A.fsmcp.com" in u for u in fetch.calls)
    assert not any("flowstatemcp" in u for u in fetch.calls)
    for other in ("d=102", "d=103", "d=104"):           # other names' certs not downloaded
        assert not any(other in u for u in fetch.calls)
    assert S.tool_warning(st) is None


def test_ct_alerts_on_foreign_key_loudly(enrolled, ca, caplog, capsys):
    st = enrolled
    host = st.config()["hostname"]
    fetch = _crtsh({101: ([host], _local_cert(st, ca)),
                    202: ([host], _foreign_cert(ca, [host])),
                    303: (["*.fsmcp.com"], _foreign_cert(ca, ["*.fsmcp.com"]))})
    with caplog.at_level("ERROR"):
        r = ct.run_check(st, fetch)
    assert not r["ok"] and sorted(a["crtsh_id"] for a in r["alerts"]) == [202, 303]
    assert "RELAY SECURITY WARNING" in caplog.text
    w = S.tool_warning(st)
    assert w and "202" in w and "303" in w and host in w
    capsys.readouterr()
    assert _cli("relay", "status") == 2
    assert "!! WARNING" in capsys.readouterr().out
    # a second pass does not download again and keeps the alerts
    n = len(fetch.calls)
    r2 = ct.run_check(st, fetch)
    assert len(r2["alerts"]) == 2 and len(fetch.calls) == n + 2
    # acknowledging clears the warning
    assert ct.acknowledge(st, 202) and ct.acknowledge(st, 303)
    assert S.tool_warning(st) is None


def test_ct_warning_is_tool_visible_in_start_here(enrolled, ca, backend):
    st = enrolled
    host = st.config()["hostname"]
    ct.run_check(st, _crtsh({7: ([host], _foreign_cert(ca, [host]))}))
    text = backend.handle_call("start_here", {"project": "Alpha"}, client="t")
    assert text.startswith("!! FLOWSTATE RELAY SECURITY WARNING")


def test_start_here_unchanged_without_relay(backend):
    text = backend.handle_call("start_here", {"project": "Alpha"}, client="t")
    assert "RELAY" not in text


def test_ct_network_error_keeps_previous_alerts(enrolled, ca):
    st = enrolled
    host = st.config()["hostname"]
    ct.run_check(st, _crtsh({9: ([host], _foreign_cert(ca, [host]))}))

    def broken(url):
        raise RelayError("could not reach crt.sh")
    r = ct.run_check(st, broken)
    assert r["error"] and len(r["alerts"]) == 1 and not r["ok"]


def test_ct_due_daily(enrolled):
    st = enrolled
    assert ct.due(st)
    st.save_ct({"last_attempt": int(time.time())})
    assert not ct.due(st)
    assert ct.due(st, now=time.time() + 24 * 3600 + 1)


def test_covers():
    assert C.covers("*.fsmcp.com", "k7q3m2.fsmcp.com")
    assert not C.covers("*.fsmcp.com", "a.k7q3m2.fsmcp.com")
    assert not C.covers("*.flowstatemcp.com", "k7q3m2.fsmcp.com")
    assert not C.covers("*.relay.flowstatemcp.com", "k7q3m2.fsmcp.com")
    assert not C.covers("tunnel.fsmcp.com", "k7q3m2.fsmcp.com")   # the relay's own cert
    assert C.covers("K7Q3M2.fsmcp.com.", "k7q3m2.fsmcp.com")


def test_jws_signature_is_raw_64_bytes(enrolled):
    key = C.load_key(enrolled.read_bytes(S.TLS_KEY))
    assert len(C.sign_es256(key, b"x")) == 64
    assert len(C.jwk_thumbprint(key)) == 43


def test_relay_modules_not_imported_by_plain_server():
    import subprocess
    from conftest import SERVER_DIR
    code = ("import sys, flowstate.server, flowstate.cli; "
            "print(any(m.startswith('flowstate.relay') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(SERVER_DIR), capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip() == "False", out.stderr
