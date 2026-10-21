"""Optional relay agent: lets claude.ai / ChatGPT / Cursor reach this computer's FlowState
at https://<handle>.fsmcp.com without the relay being able to read the traffic.

OFF by default. Nothing in here runs, imports `cryptography` or touches the network until
the user runs `flowstate relay ...`. Local FlowState needs no account and makes no network
calls.

    store.py   relay state on disk (owner-only files in the FlowState data folder)
    crypto.py  ECDSA P-256 keys, CSR, JWS signatures, certificate parsing (lazy `cryptography`)
    api.py     the relay's HTTPS API: enroll, publish / remove the ACME TXT value
    acme.py    ACME (RFC 8555) DNS-01 client for Let's Encrypt, run on this computer
    ct.py      Certificate Transparency watch (crt.sh)
    tunnel.py  outbound control + data connections; TLS is terminated HERE with the user's key
    runner.py  `flowstate relay run`: public-mode HTTP app + tunnel + renew + CT loops
    cli.py     `flowstate relay enroll|run|status|renew|enable|disable|forget|ct-check|ct-ack`

Wire protocol: docs/relay-protocol.md.
"""

import logging
import os
import re

# The relay's own domain (nothing else lives there: no website, no mail). Every relay name is
# derived from it: <handle>.<domain>, api.<domain>, tunnel.<domain>. FLOWSTATE_RELAY_DOMAIN
# overrides it for relay developers only.
DEFAULT_DOMAIN = "fsmcp.com"
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def _domain() -> str:
    raw = (os.environ.get("FLOWSTATE_RELAY_DOMAIN") or "").strip().lower().rstrip(".")
    if raw and not _DOMAIN_RE.match(raw):
        logging.getLogger("flowstate.relay").warning(
            "ignoring FLOWSTATE_RELAY_DOMAIN (not a domain name); using %s", DEFAULT_DOMAIN)
        raw = ""
    return raw or DEFAULT_DOMAIN


RELAY_DOMAIN = _domain()
HOST_SUFFIX = RELAY_DOMAIN
API_BASE = f"https://api.{RELAY_DOMAIN}"
TUNNEL_HOST = f"tunnel.{RELAY_DOMAIN}"
TUNNEL_PORT = 443
# never a device handle (the relay refuses them too; docs/relay-protocol.md section 1)
RESERVED_HANDLES = frozenset({"api", "tunnel", "www", "mail", "relay", "status", "admin", "app",
                              "docs", "help"})
PROTOCOL = "flowstate-relay/1"


class RelayError(Exception):
    """A relay step failed; the message is safe to show (never holds a token or key)."""


class RelayWaitlisted(RelayError):
    """The relay is full (signup cap); the enrollment code was not used and can be retried."""

    def __init__(self, msg: str, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after
