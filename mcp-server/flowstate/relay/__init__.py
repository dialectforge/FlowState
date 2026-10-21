"""Optional relay agent: lets claude.ai / ChatGPT / Cursor reach this computer's FlowState
at https://<handle>.relay.flowstatemcp.com without the relay being able to read the traffic.

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

HOST_SUFFIX = "relay.flowstatemcp.com"
API_BASE = "https://relay.flowstatemcp.com"
TUNNEL_HOST = "tunnel.flowstatemcp.com"
TUNNEL_PORT = 443
PROTOCOL = "flowstate-relay/1"


class RelayError(Exception):
    """A relay step failed; the message is safe to show (never holds a token or key)."""
