"""ACME (RFC 8555) DNS-01 client, run on the user's computer.

The certificate's private key (tls-key.pem) and the ACME account key are generated and kept
here. The only thing that leaves is (a) signed ACME requests and the CSR (public key only) to
Let's Encrypt and (b) the DNS-01 TXT value to the relay, which publishes it at
_acme-challenge.<handle>.relay.flowstatemcp.com and removes it afterwards.

Production: https://acme-v02.api.letsencrypt.org/directory
Staging:    https://acme-staging-v02.api.letsencrypt.org/directory  (`--staging`)
FLOWSTATE_RELAY_ACME_DIRECTORY overrides both (tests point it at a local fake).
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime
from typing import Callable, Optional

from . import RelayError
from . import crypto as C
from .api import http_request

log = logging.getLogger("flowstate.relay.acme")

LE_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
RENEW_DAYS = 30


def directory_url(staging: bool) -> str:
    return os.environ.get("FLOWSTATE_RELAY_ACME_DIRECTORY") or (LE_STAGING if staging else LE_PRODUCTION)


def txt_value(token: str, account_key) -> str:
    """DNS-01 record value: base64url(SHA-256(token "." JWK thumbprint))."""
    key_auth = f"{token}.{C.jwk_thumbprint(account_key)}"
    return C.b64u(hashlib.sha256(key_auth.encode()).digest())


class AcmeClient:
    def __init__(self, directory: str, account_key, kid: Optional[str] = None, ctx=None,
                 sleep: Callable[[float], None] = time.sleep, poll_seconds: float = 2.0,
                 timeout_seconds: float = 300.0):
        self.directory_url, self.key, self.kid, self.ctx = directory, account_key, kid, ctx
        self.sleep, self.poll, self.timeout = sleep, poll_seconds, timeout_seconds
        self._dir: Optional[dict] = None
        self._nonce: Optional[str] = None

    # -------------------------------------------------------- transport
    def _http(self, method, url, body=None, headers=None):
        status, hdrs, data = http_request(method, url, body, headers, self.ctx)
        n = hdrs.get("Replay-Nonce") if hdrs else None
        if n:
            self._nonce = n
        return status, hdrs, data

    def directory(self) -> dict:
        if self._dir is None:
            status, _, data = self._http("GET", self.directory_url)
            if status != 200:
                raise RelayError(f"ACME directory answered {status}")
            self._dir = json.loads(data)
        return self._dir

    def _new_nonce(self) -> str:
        if self._nonce:
            n, self._nonce = self._nonce, None
            return n
        status, hdrs, _ = self._http("HEAD", self.directory()["newNonce"])
        n = hdrs.get("Replay-Nonce") if hdrs else None
        if not n:
            raise RelayError(f"ACME server gave no nonce ({status})")
        self._nonce = None
        return n

    def _jws(self, url: str, payload: Optional[dict], use_jwk: bool) -> bytes:
        protected = {"alg": "ES256", "nonce": self._new_nonce(), "url": url}
        if use_jwk:
            protected["jwk"] = C.jwk(self.key)
        else:
            protected["kid"] = self.kid
        p64 = C.b64u(json.dumps(protected).encode())
        b64 = "" if payload is None else C.b64u(json.dumps(payload).encode())
        sig = C.sign_es256(self.key, f"{p64}.{b64}".encode())
        return json.dumps({"protected": p64, "payload": b64, "signature": C.b64u(sig)}).encode()

    def post(self, url: str, payload: Optional[dict], use_jwk: bool = False, accept=None):
        """Signed POST (payload None = POST-as-GET). Retries once on badNonce."""
        headers = {"Content-Type": "application/jose+json"}
        if accept:
            headers["Accept"] = accept
        for attempt in (1, 2):
            status, hdrs, data = self._http("POST", url, self._jws(url, payload, use_jwk), headers)
            if status < 400:
                return status, hdrs, data
            prob = _problem(data)
            if prob.get("type", "").endswith(":badNonce") and attempt == 1:
                continue
            raise RelayError(f"ACME error {status}: {prob.get('type', '?')} "
                             f"{str(prob.get('detail', ''))[:200]}")
        raise RelayError("ACME: unreachable")  # pragma: no cover

    def _get_json(self, url):
        _, hdrs, data = self.post(url, None)
        return json.loads(data), hdrs

    # -------------------------------------------------------- flow
    def account(self) -> str:
        if self.kid:
            return self.kid
        status, hdrs, _ = self.post(self.directory()["newAccount"],
                                    {"termsOfServiceAgreed": True}, use_jwk=True)
        self.kid = hdrs.get("Location")
        if not self.kid:
            raise RelayError(f"ACME account: no account URL ({status})")
        return self.kid

    def _wait(self, url: str, done: set, what: str) -> dict:
        deadline = time.monotonic() + self.timeout
        while True:
            obj, hdrs = self._get_json(url)
            st = obj.get("status")
            if st in done:
                return obj
            if st == "invalid":
                detail = _first_error(obj)
                raise RelayError(f"ACME {what} failed" + (f": {detail}" if detail else ""))
            if time.monotonic() > deadline:
                raise RelayError(f"ACME {what} still '{st}' after {self.timeout:.0f}s")
            self.sleep(_retry_after(hdrs, self.poll))

    def issue(self, hostname: str, csr: bytes, publish: Callable[[str], None],
              unpublish: Callable[[], None], propagation_seconds: float = 0.0) -> str:
        """Run the whole DNS-01 order for `hostname`; returns the PEM certificate chain."""
        self.account()
        status, hdrs, data = self.post(self.directory()["newOrder"],
                                       {"identifiers": [{"type": "dns", "value": hostname}]})
        order_url = hdrs.get("Location")
        order = json.loads(data)
        if not order_url:
            raise RelayError("ACME order: no order URL")
        for authz_url in order.get("authorizations", []):
            authz, _ = self._get_json(authz_url)
            if authz.get("status") == "valid":
                continue
            ident = (authz.get("identifier") or {}).get("value", "").lower()
            if ident != hostname.lower():
                raise RelayError("ACME authorization is for a different name")
            chal = next((c for c in authz.get("challenges", []) if c.get("type") == "dns-01"), None)
            if not chal:
                raise RelayError("ACME offered no dns-01 challenge")
            publish(txt_value(chal["token"], self.key))
            try:
                if propagation_seconds:
                    self.sleep(propagation_seconds)
                self.post(chal["url"], {})
                self._wait(authz_url, {"valid"}, "DNS-01 check")
            finally:
                try:
                    unpublish()
                except Exception as e:  # noqa: BLE001 - removal failing must not hide the result
                    log.warning("could not remove the _acme-challenge TXT record: %s", e)
        self.post(order["finalize"], {"csr": C.b64u(csr)})
        order = self._wait(order_url, {"valid"}, "order")
        _, _, pem = self.post(order["certificate"], None, accept="application/pem-certificate-chain")
        text = pem.decode()
        if "BEGIN CERTIFICATE" not in text:
            raise RelayError("ACME returned no certificate")
        return text


def _problem(data: bytes) -> dict:
    try:
        p = json.loads(data)
        return p if isinstance(p, dict) else {}
    except ValueError:
        return {}


def _first_error(obj: dict) -> str:
    for c in obj.get("challenges", []) or []:
        if c.get("error"):
            return str(c["error"].get("detail", ""))[:200]
    return str((obj.get("error") or {}).get("detail", ""))[:200]


def _retry_after(hdrs, default: float) -> float:
    try:
        return min(max(float(hdrs.get("Retry-After")), 0.0), 60.0)
    except (TypeError, ValueError, AttributeError):
        return default


# -------------------------------------------------------------------- store glue
def needs_cert(store, now: Optional[datetime] = None) -> Optional[str]:
    """Why a new certificate is needed, or None if the current one is fine."""
    from .store import CERT, TLS_KEY
    pem, key = store.read_bytes(CERT), store.read_bytes(TLS_KEY)
    if not pem:
        return "no certificate yet"
    try:
        info = C.cert_info(pem)
    except RelayError:
        return "certificate file unreadable"
    host = store.config().get("hostname", "")
    if not any(C.covers(n, host) for n in info["names"]):
        return "certificate is for another name"
    if key and info["spki_sha256"] != C.key_spki_sha256(key):
        return "certificate does not match the local key"
    left = C.days_left(info, now)
    if left < RENEW_DAYS:
        return f"certificate expires in {left:.0f} days"
    return None


def ensure_cert(store, force: bool = False, now: Optional[datetime] = None,
                sleep: Callable[[float], None] = time.sleep,
                propagation_seconds: Optional[float] = None) -> dict:
    """Obtain or renew the certificate if needed. Returns {"renewed": bool, "reason", "info"}."""
    from . import api
    from .store import ACCOUNT, ACCOUNT_KEY, CERT, TLS_KEY
    reason = "forced" if force else needs_cert(store, now)
    if not reason:
        return {"renewed": False, "reason": None, "info": C.cert_info(store.read_bytes(CERT))}
    cfg = store.config()
    host, token = cfg["hostname"], store.token()
    tls_key = store.read_bytes(TLS_KEY)
    if not tls_key:
        tls_key = new_tls_key(store)
    acct_pem = store.read_bytes(ACCOUNT_KEY)
    if not acct_pem:
        acct_pem = C.new_key_pem()
        store.write_secret(ACCOUNT_KEY, acct_pem)
    directory = directory_url(bool(cfg.get("staging")))
    accounts = store._read_json(ACCOUNT)
    ctx = api.client_context(store.ca_file())
    client = AcmeClient(directory, C.load_key(acct_pem), kid=accounts.get(directory), ctx=ctx,
                        sleep=sleep)
    if propagation_seconds is None:
        propagation_seconds = float(os.environ.get("FLOWSTATE_RELAY_DNS_WAIT", "10"))
    log.info("requesting a certificate for %s (%s)", host, reason)
    pem = client.issue(
        host, C.csr_der(C.load_key(tls_key), host),
        publish=lambda v: api.dns_challenge(cfg["api"], token, host, v, ctx),
        unpublish=lambda: api.dns_challenge(cfg["api"], token, host, None, ctx),
        propagation_seconds=propagation_seconds)
    if accounts.get(directory) != client.kid:
        accounts[directory] = client.kid
        store._write_json(ACCOUNT, accounts)
    info = C.cert_info(pem.encode())
    if info["spki_sha256"] != C.key_spki_sha256(tls_key):
        raise RelayError("the issued certificate is not for the local key; not installed")
    store.write_secret(CERT, pem.encode())
    log.info("certificate installed for %s, valid until %s", host, info["not_after"])
    return {"renewed": True, "reason": reason, "info": info}


def new_tls_key(store) -> bytes:
    """Generate the TLS key once; remember its fingerprint for the CT watch."""
    from .store import TLS_KEY
    pem = C.new_key_pem()
    store.write_secret(TLS_KEY, pem)
    cfg = store.config()
    known = cfg.setdefault("known_spki", [])
    fp = C.key_spki_sha256(pem)
    if fp not in known:
        known.append(fp)
    store.save_config(cfg)
    return pem
