# FlowState relay: agent <-> relay wire protocol (version 1)

Status: draft for the first relay server build. The agent side is implemented in
`mcp-server/flowstate/relay/` (MIT); the relay server is a separate, private project and must
implement exactly what is written here. "MUST", "SHOULD" and "MAY" are as in RFC 2119.

## 0. Goal and trust model

A user's FlowState runs on their own computer. Cloud AI apps (claude.ai, ChatGPT, Cursor, ...)
reach it at `https://<handle>.fsmcp.com/mcp`. The relay forwards connections
but **cannot decrypt them**:

- The TLS key for `<handle>.fsmcp.com` is an ECDSA P-256 key generated on the
  user's computer. It is never sent anywhere. The certificate comes from Let's Encrypt through
  ACME DNS-01, run by the agent itself; the relay only publishes a TXT value.
- The relay routes client connections by the SNI in the TLS ClientHello and splices raw TCP
  bytes. It MUST NOT terminate TLS for any `<handle>.fsmcp.com`.
- Sign-in (OAuth 2.1 + PKCE, owner passphrase consent, per-app revoke, read-only) is done
  by FlowState on the user's computer, inside the end-to-end TLS session.

What the relay can see: the hostname (SNI), client IP addresses and ports, connection times,
byte counts and timing. TLS 1.3 encrypts the server certificate, so the relay does not even
see that.

What the relay could do if it turned malicious: because it controls DNS for
`fsmcp.com`, it could obtain its own certificate for a user's hostname and
intercept. That certificate would be in public Certificate Transparency logs; the agent checks
CT daily and alerts the user (section 6). The relay operator MUST NOT obtain any certificate
covering `*.fsmcp.com` or any `<handle>.fsmcp.com` (its own service certificates are for
`tunnel.fsmcp.com` and `api.fsmcp.com` only), so any such certificate in CT is, by
definition, an alert.

`fsmcp.com` is the relay's own domain: nothing else (website, mail) lives there, so the
relay's DNS access can't reach `flowstatemcp.com`, and Let's Encrypt's per-domain limit for
`fsmcp.com` is separate from `flowstatemcp.com`. (Protocol v1 drafts used
`<handle>.relay.flowstatemcp.com`, `relay.flowstatemcp.com` and `tunnel.flowstatemcp.com`;
nothing was ever deployed on those names.) Every name below is derived from that one
domain: `RELAY_DOMAIN` in `flowstate/relay/__init__.py` (and `RELAY_DOMAIN` on the relay).

## 1. Endpoints

| Name | Address | TLS terminated by |
|---|---|---|
| API | `https://api.fsmcp.com/api/...` | relay (its own cert) |
| Tunnel | `tunnel.fsmcp.com:443` | relay (its own cert) |
| Public | `<handle>.fsmcp.com:443` | **the user's agent**, never the relay |

The tunnel and public endpoints MAY share an IP address and port 443; the relay then
dispatches on SNI (`tunnel.fsmcp.com` and `api.fsmcp.com` -> terminate locally;
`<handle>.fsmcp.com` for a valid, unreserved handle -> splice; anything else -> close). The
agent sends no ALPN on the tunnel; the relay MUST NOT require it there.

Identifiers:

- **handle**: `^[a-z0-9]{4,32}$`, random, assigned by the relay (e.g. `k7q3m2`). The public
  hostname is exactly `<handle>.fsmcp.com`. Because the relay's own names sit next to the
  handles, these labels are **reserved** and MUST never be a handle (the relay never issues,
  splices or publishes a challenge for them; the agent refuses them on enroll):
  `api tunnel www mail relay status admin app docs help`. The relay MAY reserve more.
  Handles are never reused after revoke/unenroll.
- **device token**: opaque, 32 to 512 printable non-space ASCII characters, at least 256 bits
  of randomness. The relay SHOULD store only a hash. One token per enrolled device.
- **stream id**: `^[A-Za-z0-9_-]{16,128}$`, at least 128 bits of randomness, chosen by the
  relay per client connection, single use.

## 2. HTTPS API

JSON in, JSON out, `Content-Type: application/json`. Errors are 4xx/5xx with
`{"error": "<code>"}`; the agent shows `<code>` (truncated to 80 characters) to the user, so
it MUST NOT contain secrets. The agent never follows redirects.

### 2.1 `POST /api/enroll`

No authentication; the one-time code is the credential. Request:

```json
{"code": "ABCD-1234", "agent": "flowstate/2.3.0", "protocol": 1}
```

`code` matches `^[A-Za-z0-9-]{4,64}$`. The relay MUST make codes single use, short lived
(suggested 15 minutes) and rate limited per IP.

Success `200`:

```json
{"handle": "k7q3m2", "device_token": "<token>", "hostname": "k7q3m2.fsmcp.com"}
```

`hostname` is optional; if present it MUST equal `<handle>.fsmcp.com`, otherwise
the agent refuses the enrollment. Errors: `400 invalid_code`, `429 rate_limited`,
`429 waitlisted` (below).

**Signup cap (`429 waitlisted`).** The relay MAY limit enrollment (the hosted relay allows at
most 40 enrolled devices during the beta, and at most 40 new enrollments in any rolling 7 days
to stay under Let's Encrypt's 50 new certificates per registered domain per week). When over
a cap it answers:

```
HTTP/1.1 429 Too Many Requests
Retry-After: <seconds>

{"error": "waitlisted", "reason": "weekly" | "active", "retry_after": <unix time>,
 "position": <place in line, 1 = next>, "message": "<plain words for people>"}
```

and MUST NOT use the code. The relay records the code (only its hash, with timestamps and its
place in line, nothing personal) on a waitlist.

*First come, first served.* When a slot opens (the 7-day window rolls, or a beta slot is
freed by a revoke or unenroll; a suspended device keeps its slot), the relay reserves it for
the oldest code in line. That code has a claim window (hosted relay: 48 hours,
`RELAY_WAITLIST_CLAIM_HOURS`) to retry and enroll; during the window every other code stays
waitlisted, however often it retries. If the window passes unused, the reservation lapses
and the slot goes to the next oldest. A lapsed code keeps its original place: if it is
retried while still valid, it is back in line ahead of newer codes. Several open slots are
reserved the same way, in order. `position` counts the reserved codes first, then the
waiting ones, oldest first.

*Code validity.* A waitlisted code stays valid until 7 days after its `retry_after` or its
reservation expiry, whichever is later. After that it is expired and the user needs a new
code (`400 invalid_code`).

*`retry_after`.* For `reason: "weekly"` it is when this code's slot is expected to open (the
enrollment that frees it turns 7 days old). For `reason: "active"` (the beta device cap,
which only opens when a device is removed) it is a one-day hint. `reason` is `"weekly"`
whenever the weekly cap is what blocks the code, and `"active"` only when the beta cap alone
does.

*What the agent says* (then saves nothing and exits non-zero):

- Beta cap full (`reason: "active"`): "The FlowState Relay beta is full right now. You're on
  the waitlist (number N). Your code wasn't used, so try again later and you'll get in when
  it's your turn."
- Weekly cap full (`reason: "weekly"`, or anything else): "FlowState Relay is full this week,
  you're on the waitlist (number N), try again after <local time of retry_after>. Your
  enrollment code was not used, so keep it and run the same command again then."

N is `position`; if it is missing or not a sensible number, "(number N)" is left out. If
`retry_after` is missing or unusable the agent uses the `Retry-After` header. Certificate
renewals (section 2.2 for an existing handle) are never capped. Any other `429` is shown as
"busy, try again later".

### 2.2 `POST /api/dns-challenge`

`Authorization: Bearer <device token>`. Publishes or removes the ACME DNS-01 TXT record for
the token's own handle only.

```json
{"action": "set",   "name": "_acme-challenge.k7q3m2.fsmcp.com", "value": "<43 chars base64url>"}
{"action": "clear", "name": "_acme-challenge.k7q3m2.fsmcp.com"}
```

The relay MUST:

- answer `401 unauthorized` for an unknown or revoked token;
- answer `403 not_your_name` unless `name` is exactly `_acme-challenge.<handle of this token>.fsmcp.com`
  (in the `fsmcp.com` zone; never a reserved label and never any `flowstatemcp.com` name);
- accept only `value` matching `^[A-Za-z0-9_-]{43}$` (a base64url SHA-256), else `400 bad_value`;
- on `set`, replace any previous value for that name (one TXT value at a time) with a short TTL
  (60 s suggested), and answer `200 {"ok": true}` **only after** the record is served by all
  authoritative name servers for `fsmcp.com`;
- on `clear`, remove the record (idempotent: clearing a missing record is `200`);
- remove any TXT record left behind after 1 hour;
- rate limit (suggested: 20 `set` per handle per hour).

After `set` returns, the agent waits `FLOWSTATE_RELAY_DNS_WAIT` seconds (default 10) and then
asks Let's Encrypt to validate. It always sends `clear` afterwards, whether validation passed
or failed.

## 3. Tunnel: control connection

The agent opens TCP to `tunnel.fsmcp.com:443` and does a normal TLS 1.2+ handshake as a
client, SNI `tunnel.fsmcp.com`, verifying the relay's certificate against the system
trust store. Over that TLS session both sides exchange **messages: one UTF-8 JSON object per
line**, terminated by `\n`, at most 65 536 bytes including the newline. Every message has a
string `type`. Unknown types MUST be ignored by both sides (forward compatibility); unknown
fields MUST be ignored.

### 3.1 Handshake

Agent -> relay, first line:

```json
{"type": "hello", "v": 1, "role": "control", "proto": "flowstate-relay/1",
 "handle": "k7q3m2", "token": "<device token>", "agent": "flowstate/2.3.0"}
```

Relay -> agent, within 15 s:

```json
{"type": "welcome", "v": 1, "heartbeat": 25}
```

or `{"type": "error", "code": "<code>"}` and close. Codes `unauthorized`, `revoked` and
`unknown_device` make the agent stop for good (state `unauthorized`; the user must re-enroll).
Any other code (`busy`, `upgrade_required`, ...) is treated as a temporary failure.

The relay MUST check that `token` is valid and belongs to `handle`. One control connection per
handle: when a new one is welcomed, the relay SHOULD send `{"type": "goaway"}` on the old one
and close it.

### 3.2 Heartbeats

`heartbeat` (seconds) from `welcome` is clamped by the agent to 5..120 (default 25).

- The agent sends `{"type": "ping", "seq": <n>}` every `heartbeat` seconds; the relay answers
  `{"type": "pong", "seq": <n>}`.
- The relay MAY send `ping`; the agent answers `pong` with the same `seq`.
- If the agent receives nothing at all for `3 x heartbeat` seconds it closes the connection and
  reconnects. The relay SHOULD do the same (drop the agent after `3 x heartbeat` of silence).

### 3.3 Reconnect

After any disconnect (network error, TLS error, silence, `goaway`, `bye`, temporary `error`)
the agent waits and reconnects. Delay for attempt n (starting at 0): `d = min(60, 1 x 2^n)`
seconds, actual wait uniformly random in `[d/2, d]`. The attempt counter resets when the
previous connection had been welcomed at least 30 s before it ended. On shutdown the agent
sends `{"type": "bye"}` and closes.

### 3.4 New client: `open` / `reject`

When a client connects to `<handle>.fsmcp.com:443`, the relay reads the first TLS
record (the ClientHello), extracts SNI, finds the control connection for that handle and sends:

```json
{"type": "open", "stream": "<stream id>", "client": "203.0.113.5:51234"}
```

`client` is the client's IP and port as the relay sees it (`[v6]:port` for IPv6). FlowState
uses the IP for its per-address rate limit before sign-in and in its access log. It does not
trust any client-sent header for this.

The agent either opens a data connection (3.5) or answers on the control connection:

```json
{"type": "reject", "stream": "<stream id>", "reason": "no_certificate" | "busy" | "not_ready"}
```

The relay then closes the client connection. If no data connection arrives within 10 s, the
relay closes the client connection and forgets the stream id.

If no control connection exists for the handle, or SNI is missing or not exactly
`<handle>.fsmcp.com` for a valid, unreserved handle, the relay closes the client connection. It MUST NOT answer with its
own certificate.

## 4. Tunnel: data connection

For each `open` it accepts, the agent opens a **new** TCP + TLS connection to
`tunnel.fsmcp.com:443` (same verification as the control connection) and sends exactly
one line:

```json
{"type": "attach", "v": 1, "stream": "<stream id>", "token": "<device token>"}
```

The relay MUST check that the token is valid, that the stream id is pending, and that it was
created for this token's handle; otherwise it closes the connection without answering. The
relay sends **no** reply line. Immediately after the newline the connection becomes a raw byte
pipe:

1. The relay writes the client's buffered bytes (the full ClientHello record and anything after
   it) unchanged, then copies bytes in both directions between the client's TCP connection and
   this data connection, unchanged, until either side closes.
2. Half-close: when one side sends EOF, the relay SHOULD close both sides (HTTP over this pipe
   does not rely on half-close).
3. The relay MUST NOT parse, modify, buffer beyond normal socket buffers, or log the spliced
   bytes. It MAY count bytes for quotas.

Inside that pipe the agent acts as the **TLS server** for `<handle>.fsmcp.com`,
using the user's certificate and key, TLS 1.2 minimum, ALPN `http/1.1` only (clients that offer
only `h2` fall back to no ALPN), no renegotiation. The decrypted HTTP/1.1 goes to FlowState's
public-mode listener on `127.0.0.1` on the user's computer. The agent caps concurrent streams at
64 (`reject` with `busy` beyond that).

The data connection is TLS inside TLS: the relay's own TLS session wraps the client's
end-to-end TLS session. The relay can remove the outer layer only.

## 5. Certificate lifecycle (agent side, for reference)

- Key: ECDSA P-256, generated at enrollment, kept across renewals, file mode 0600.
- ACME account key: separate ECDSA P-256 key, local, 0600. Account URL kept per directory.
- Directory: Let's Encrypt production, or staging with `--staging`.
- Order: one DNS identifier, `<handle>.fsmcp.com`. Challenge: `dns-01`, TXT value
  `base64url(SHA-256(token "." JWK-thumbprint(account key)))`, published through 2.2.
- CSR: CN and SAN = the hostname, signed with the local key.
- Checked at start and every 12 h; renewed when fewer than 30 days remain. A failed renewal is
  retried with backoff (5 min doubling to 6 h) while the old certificate keeps serving.
- The relay is never asked for, and never receives, a CSR, key or certificate.

## 6. Certificate Transparency watch (agent side, for reference)

Daily, the agent queries `https://crt.sh/?q=<hostname>&output=json` and
`https://crt.sh/?q=*.fsmcp.com&output=json`, keeps entries whose names cover the
hostname, downloads each new certificate (`https://crt.sh/?d=<id>`, at most 50 per run) and
compares the SHA-256 of its SubjectPublicKeyInfo with the local key(s). Any mismatch is an
alert: an ERROR log line, `flowstate relay status` (exit code 2), and a warning line at the top
of every `start_here` result until the user runs `flowstate relay ct-ack --id <crt.sh id>`.

## 7. Limits and timeouts (summary)

| Item | Value |
|---|---|
| Control line max | 65 536 bytes |
| TCP connect + TLS handshake to tunnel | 15 s |
| `welcome` after `hello` | 15 s |
| Heartbeat | from `welcome`, clamped 5..120 s, default 25 s |
| Dead after silence | 3 x heartbeat |
| Reconnect backoff | 1 s doubling to 60 s, equal jitter, reset after 30 s healthy |
| Data connection after `open` (relay waits) | 10 s |
| Client TLS handshake (agent waits) | 20 s |
| Concurrent streams per agent | 64 |

## 8. Agent environment overrides (development only)

These are for people developing the relay itself, never for normal use.

`FLOWSTATE_RELAY_DOMAIN` (default `fsmcp.com`; every name below derives from it; when it is
set to anything else, every `flowstate relay` command first prints a warning on stderr that
the agent is not using the FlowState relay and that the setting is for developers only),
`FLOWSTATE_RELAY_API` (API base URL, default `https://api.fsmcp.com`), `FLOWSTATE_RELAY_TUNNEL`
(`host:port` to dial, default `tunnel.fsmcp.com:443`),
`FLOWSTATE_RELAY_TUNNEL_SNI` (SNI / name to verify, default `tunnel.fsmcp.com`),
`FLOWSTATE_RELAY_CA_FILE` (extra CA bundle for API, tunnel and ACME),
`FLOWSTATE_RELAY_ACME_DIRECTORY`, `FLOWSTATE_RELAY_DNS_WAIT`, `FLOWSTATE_RELAY_DIR`.
Non-https URLs are refused except for `127.0.0.1`, `localhost` and `::1`.

A reference fake relay (SNI peek, splice, tunnel endpoint, API, ACME) used by the tests is in
`tests/relay_fakes.py`; its `parse_sni` shows the ClientHello parsing the relay needs.

## 9. Versioning

`v` in `hello`, `welcome` and `attach` is the protocol version (1). A relay that cannot speak
the agent's version answers `{"type": "error", "code": "upgrade_required"}`. New optional
fields and new message types do not change the version.
