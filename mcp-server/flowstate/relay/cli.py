"""`flowstate relay ...` commands. Plain-language output; never prints the device token or a key.

    flowstate relay enroll --code CODE [--staging] [--skip-cert]
    flowstate relay run    [--db FILE] [--port 8768]
    flowstate relay status
    flowstate relay renew  [--force] [--staging | --production]
    flowstate relay enable | disable
    flowstate relay forget [--yes]
    flowstate relay ct-check
    flowstate relay ct-ack --id CRTSH_ID

For relay developers only: FLOWSTATE_RELAY_API, FLOWSTATE_RELAY_TUNNEL (host:port),
FLOWSTATE_RELAY_TUNNEL_SNI, FLOWSTATE_RELAY_CA_FILE, FLOWSTATE_RELAY_ACME_DIRECTORY and
FLOWSTATE_RELAY_DIR point the agent somewhere else (https only, except loopback).
"""

import os
import time
from datetime import datetime
from pathlib import Path

from . import API_BASE, TUNNEL_HOST, TUNNEL_PORT, RelayError


def say(msg: str) -> None:
    print(msg, flush=True)


def _store():
    from .store import RelayStore
    return RelayStore()


def _when(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def _tunnel_env():
    raw = os.environ.get("FLOWSTATE_RELAY_TUNNEL") or f"{TUNNEL_HOST}:{TUNNEL_PORT}"
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise RelayError("FLOWSTATE_RELAY_TUNNEL must be host:port")
    return host, int(port), os.environ.get("FLOWSTATE_RELAY_TUNNEL_SNI") or TUNNEL_HOST


# ---------------------------------------------------------------- commands
def cmd_enroll(a) -> int:
    from . import acme, api
    from . import crypto as C
    store = _store()
    if not C.available():
        say("The relay needs the Python package 'cryptography', which is not installed here. "
            "Nothing was changed.")
        return 1
    if store.enrolled():
        cfg = store.config()
        say(f"Already enrolled as {cfg.get('hostname')}. To start over: flowstate relay forget")
        return 1
    base = os.environ.get("FLOWSTATE_RELAY_API") or API_BASE
    api.check_url(base)
    host, port, sni = _tunnel_env()
    ca = os.environ.get("FLOWSTATE_RELAY_CA_FILE")
    store.ensure_dir()  # refuses a git folder before anything goes over the network
    got = api.enroll(base, a.code, api.client_context(ca))
    store.save_enrollment(got["handle"], got["device_token"], api=base, tunnel_host=host,
                          tunnel_port=port, tunnel_sni=sni, staging=a.staging, ca_file=ca)
    acme.new_tls_key(store)
    say(f"Enrolled. Your relay address: https://{got['hostname']}/mcp")
    say(f"Device token and keys saved (owner-only) in {store.root}. The TLS key was made on "
        f"this computer and never leaves it.")
    if a.skip_cert:
        say("Certificate: not requested yet (flowstate relay renew, or it happens on `relay run`).")
    else:
        rc = _renew(store, force=False)
        if rc:
            say("Enrollment is kept; `flowstate relay run` will try the certificate again.")
    say("Next: set the owner passphrase if you haven't (flowstate remote passphrase), then "
        "start it with: flowstate relay run")
    return 0


def _renew(store, force: bool) -> int:
    from . import acme
    try:
        r = acme.ensure_cert(store, force=force)
    except RelayError as e:
        say(f"Certificate NOT obtained: {e}")
        return 1
    info = r["info"]
    staging = " (Let's Encrypt STAGING: browsers and AI apps will not trust it)" \
        if store.config().get("staging") else ""
    if r["renewed"]:
        say(f"Certificate obtained for {', '.join(info['names'])}, valid until "
            f"{info['not_after']:%Y-%m-%d}{staging}.")
    else:
        say(f"Certificate is fine until {info['not_after']:%Y-%m-%d}{staging}; nothing to do.")
    return 0


def cmd_renew(a) -> int:
    store = _store()
    if not store.enrolled():
        say("The relay is not set up (flowstate relay enroll --code <code>).")
        return 1
    force = a.force
    if a.staging or a.production:
        cfg = store.config()
        want = bool(a.staging)
        if bool(cfg.get("staging")) != want:
            cfg["staging"] = want
            store.save_config(cfg)
            force = True
    return _renew(store, force)


def cmd_run(a) -> int:
    import asyncio
    import logging
    from . import runner
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db = Path(a.db).expanduser() if a.db else None
    try:
        asyncio.run(runner.run(db_path=db, port=a.port))
    except KeyboardInterrupt:
        say("Relay stopped.")
    return 0


def cmd_status(a) -> int:
    from . import crypto as C
    from .store import CERT, STATUS_FRESH_SECONDS, active_alerts
    store = _store()
    cfg = store.config()
    if not cfg.get("handle"):
        say("Relay: not set up (off). Local FlowState works without it.")
        return 0
    say(f"Relay: {'ON' if cfg.get('enabled') else 'OFF (flowstate relay enable)'}")
    say(f"Handle: {cfg['handle']}")
    say(f"URL: https://{cfg['hostname']}/mcp")
    say(f"Device token: {'stored' if store.read_bytes('device-token') else 'MISSING'} (not shown)")
    pem = store.read_bytes(CERT)
    if pem and C.available():
        info = C.cert_info(pem)
        say(f"Certificate: valid until {info['not_after']:%Y-%m-%d %H:%M} UTC "
            f"({C.days_left(info):.0f} days; renews 30 days before)"
            + (" [Let's Encrypt STAGING]" if cfg.get("staging") else ""))
    else:
        say("Certificate: none yet (flowstate relay renew)")
    st = store.status()
    fresh = st.get("updated") and time.time() - st["updated"] < STATUS_FRESH_SECONDS
    if fresh:
        say(f"Tunnel: {st.get('tunnel')} {st.get('detail') or ''}".rstrip()
            + f" (agent pid {st.get('pid')}, local port {st.get('port')})")
    else:
        say("Tunnel: not running" + (f" (last seen {_when(st.get('updated'))})" if st.get("updated") else ""))
    if st.get("cert_error"):
        say(f"Last certificate problem: {st['cert_error']}")
    ctd = store.ct()
    if ctd.get("last_attempt"):
        say(f"CT check: last {_when(ctd.get('last_check'))}, "
            f"{ctd.get('certificates', 0)} certificate(s) seen"
            + (f"; last attempt failed: {ctd['error']}" if ctd.get("error") else ""))
    else:
        say("CT check: not run yet (flowstate relay ct-check)")
    alerts = active_alerts(ctd)
    for al in alerts:
        say(f"!! WARNING: certificate crt.sh id {al['crtsh_id']} for {', '.join(al['names'])} "
            f"(issuer {al['issuer']}, from {al['not_before'][:10]}) was NOT made with this "
            f"computer's key. Someone else can impersonate your relay address. Check "
            f"https://crt.sh/?id={al['crtsh_id']}, run `flowstate relay disable`, and contact "
            f"support. If you know it is yours: flowstate relay ct-ack --id {al['crtsh_id']}")
    from .. import remote_auth as RA
    if not RA.AuthStore().has_passphrase():
        say("Owner passphrase: NOT set; apps can't be approved (flowstate remote passphrase)")
    return 2 if alerts else 0


def cmd_enable(a) -> int:
    store = _store()
    store.set_enabled(True)
    say("Relay turned on. Start it with: flowstate relay run")
    return 0


def cmd_disable(a) -> int:
    store = _store()
    store.set_enabled(False)
    say("Relay turned off. A running `flowstate relay run` stops within a few seconds. Your "
        "enrollment and keys are kept (flowstate relay forget deletes them).")
    return 0


def cmd_forget(a) -> int:
    store = _store()
    if not store.root.exists():
        say("Nothing to forget: the relay was never set up here.")
        return 0
    if not a.yes:
        ans = input(f"Delete the relay device token, keys and certificate in {store.root}? "
                    f"Type 'forget' to confirm: ")
        if ans.strip() != "forget":
            say("Nothing deleted.")
            return 1
    gone = store.forget()
    say(f"Deleted: {', '.join(gone) or 'nothing'}. A running relay stops within a few seconds. "
        f"The handle stays reserved at the relay until you remove it in your account.")
    return 0


def cmd_ct_check(a) -> int:
    from . import ct
    from .store import active_alerts
    store = _store()
    r = ct.run_check(store)
    if r.get("error"):
        say(f"CT check could not finish: {r['error']}")
    alerts = active_alerts(r)
    if alerts:
        say(f"!! {len(alerts)} certificate(s) for {r['hostname']} use a key that is not this "
            f"computer's. See: flowstate relay status")
        return 2
    say(f"CT check: {r['certificates']} certificate(s) for {r['hostname']}, all made with this "
        f"computer's key." if not r.get("error") else "No mismatches found so far.")
    return 1 if r.get("error") else 0


def cmd_ct_ack(a) -> int:
    from . import ct
    ok = ct.acknowledge(_store(), a.id)
    say(f"Alert for crt.sh id {a.id} acknowledged." if ok else f"No open alert for crt.sh id {a.id}.")
    return 0 if ok else 1


# ---------------------------------------------------------------- parser
def register(sub) -> None:
    p = sub.add_parser("relay", help="optional relay: reach this FlowState from cloud AIs "
                                     "(end-to-end TLS; off by default)")
    rs = p.add_subparsers(dest="relay_action", required=True)
    q = rs.add_parser("enroll", help="exchange a one-time code for a handle and device token")
    q.add_argument("--code", required=True)
    q.add_argument("--staging", action="store_true", help="use Let's Encrypt staging (testing)")
    q.add_argument("--skip-cert", action="store_true", help="don't request the certificate now")
    q.set_defaults(fn=_guard(cmd_enroll))
    q = rs.add_parser("run", help="run the relay agent (with FlowState's public listener)")
    q.add_argument("--db")
    q.add_argument("--port", type=int, default=8768, help="local listener port (127.0.0.1)")
    q.set_defaults(fn=_guard(cmd_run))
    q = rs.add_parser("status", help="handle, URL, certificate, tunnel, CT check")
    q.set_defaults(fn=_guard(cmd_status))
    q = rs.add_parser("renew", help="obtain / renew the certificate now if needed")
    q.add_argument("--force", action="store_true")
    g = q.add_mutually_exclusive_group()
    g.add_argument("--staging", action="store_true", help="switch to Let's Encrypt staging")
    g.add_argument("--production", action="store_true", help="switch to Let's Encrypt production")
    q.set_defaults(fn=_guard(cmd_renew))
    rs.add_parser("enable", help="turn the relay back on").set_defaults(fn=_guard(cmd_enable))
    rs.add_parser("disable", help="turn the relay off (keeps keys)").set_defaults(fn=_guard(cmd_disable))
    q = rs.add_parser("forget", help="delete the device token, keys and certificate")
    q.add_argument("--yes", action="store_true", help="don't ask")
    q.set_defaults(fn=_guard(cmd_forget))
    rs.add_parser("ct-check", help="check Certificate Transparency now").set_defaults(fn=_guard(cmd_ct_check))
    q = rs.add_parser("ct-ack", help="acknowledge a CT alert you have checked")
    q.add_argument("--id", type=int, required=True)
    q.set_defaults(fn=_guard(cmd_ct_ack))


def _guard(fn):
    def run(a) -> int:
        try:
            return fn(a)
        except RelayError as e:
            say(f"Relay: {e}")
            return 1
    return run
