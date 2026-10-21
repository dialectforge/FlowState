"""Local fakes for the relay tests. Nothing here talks to Let's Encrypt, crt.sh or
relay.flowstatemcp.com; everything listens on 127.0.0.1 and uses a throwaway test CA.

    TestCA            a CA made per test (P-256), issues leaf certificates
    FakeRelayAPI      /api/enroll and /api/dns-challenge (plain http on loopback)
    FakeACME          a small RFC 8555 server that checks JWS signatures, nonces and the
                      DNS-01 TXT value the agent asked FakeRelayAPI to publish
    FakeTunnelRelay   the relay data path: tunnel endpoint (TLS with ITS OWN cert) plus a
                      public port that only peeks at the ClientHello's SNI and splices bytes.
                      It never loads the user's key; everything it forwards is recorded in
                      `captured` so tests can check it only ever saw ciphertext.
"""

import asyncio
import base64
import datetime as dt
import hashlib
import json
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HOST_SUFFIX = "relay.flowstatemcp.com"
TUNNEL_HOST = "tunnel.flowstatemcp.com"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def spki_sha256(public_key) -> str:
    der = public_key.public_bytes(serialization.Encoding.DER,
                                  serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


# ---------------------------------------------------------------- test CA
class TestCA:
    __test__ = False  # not a pytest class

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FlowState Test CA (throwaway)")])
        now = dt.datetime.now(dt.timezone.utc)
        ski = x509.SubjectKeyIdentifier.from_public_key(self.key.public_key())
        self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                     .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now - dt.timedelta(days=1))
                     .not_valid_after(now + dt.timedelta(days=3650))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                     .add_extension(x509.KeyUsage(False, False, False, False, False, True, True,
                                                  False, False), critical=True)
                     .add_extension(ski, critical=False)
                     .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                                    critical=False)
                     .sign(self.key, hashes.SHA256()))
        self.cert_pem = self.cert.public_bytes(serialization.Encoding.PEM)
        self.cert_file = self.folder / "test-ca.pem"
        self.cert_file.write_bytes(self.cert_pem)

    def issue(self, public_key, names, days: float = 90, start=None) -> bytes:
        now = start or dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        ski = self.cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
                .issuer_name(self.cert.subject).public_key(public_key)
                .serial_number(x509.random_serial_number())
                .not_valid_before(now).not_valid_after(now + dt.timedelta(days=days))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
                               critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False,
                                             False, False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                               critical=False)
                .sign(self.key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.PEM) + self.cert_pem

    def server_files(self, name: str):
        key = ec.generate_private_key(ec.SECP256R1())
        kf, cf = self.folder / f"{name}.key", self.folder / f"{name}.crt"
        kf.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
        cf.write_bytes(self.issue(key.public_key(), [name]))
        return cf, kf


# ---------------------------------------------------------------- tiny HTTP server base
class _Server:
    def __init__(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", headers=None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                outer.handle(self, "GET", b"")

            def do_HEAD(self):
                outer.handle(self, "HEAD", b"")

            def do_POST(self):
                outer.handle(self, "POST", self._body())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _json(h, status, obj, headers=None):
    h._send(status, json.dumps(obj).encode(), dict({"Content-Type": "application/json"}, **(headers or {})))


# ---------------------------------------------------------------- relay API
class FakeRelayAPI(_Server):
    def __init__(self, codes=None, handle="k7q3m2"):
        super().__init__()
        self.codes = dict(codes or {"TEST-CODE-1234": handle})
        self.tokens = {}           # device token -> handle
        self.txt = {}              # DNS name -> value
        self.calls = []            # (action, name)
        self.response_override = None

    def handle(self, h, method, body):
        if method != "POST":
            return _json(h, 405, {"error": "method"})
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return _json(h, 400, {"error": "bad_json"})
        if h.path == "/api/enroll":
            handle = self.codes.pop(data.get("code"), None)
            if not handle:
                return _json(h, 400, {"error": "invalid_code"})
            if self.response_override:
                return _json(h, 200, self.response_override)
            token = "fsdt_" + secrets.token_urlsafe(32)
            self.tokens[token] = handle
            return _json(h, 200, {"handle": handle, "device_token": token,
                                  "hostname": f"{handle}.{HOST_SUFFIX}"})
        if h.path == "/api/dns-challenge":
            auth = h.headers.get("Authorization", "")
            handle = self.tokens.get(auth[7:] if auth.startswith("Bearer ") else "")
            if not handle:
                return _json(h, 401, {"error": "unauthorized"})
            name = data.get("name")
            if name != f"_acme-challenge.{handle}.{HOST_SUFFIX}":
                return _json(h, 403, {"error": "not_your_name"})
            if data.get("action") == "set" and isinstance(data.get("value"), str):
                self.txt[name] = data["value"]
            elif data.get("action") == "clear":
                self.txt.pop(name, None)
            else:
                return _json(h, 400, {"error": "bad_action"})
            self.calls.append((data["action"], name))
            return _json(h, 200, {"ok": True})
        return _json(h, 404, {"error": "not_found"})


# ---------------------------------------------------------------- ACME
class FakeACME(_Server):
    def __init__(self, ca: TestCA, dns: FakeRelayAPI, days: float = 90, bad_nonce_once=False):
        super().__init__()
        self.ca, self.dns, self.days = ca, dns, days
        self.bad_nonce_once = bad_nonce_once
        self.nonces, self.accounts, self.orders, self.authzs = set(), {}, {}, {}
        self.issued = []           # PEM chains
        self.lock = threading.Lock()

    def _nonce(self):
        n = secrets.token_urlsafe(16)
        self.nonces.add(n)
        return n

    def _reply(self, h, status, obj=None, headers=None, raw=None, ctype="application/json"):
        hd = {"Replay-Nonce": self._nonce(), "Content-Type": ctype}
        hd.update(headers or {})
        h._send(status, raw if raw is not None else json.dumps(obj or {}).encode(), hd)

    def _err(self, h, status, kind, detail=""):
        self._reply(h, status, {"type": f"urn:ietf:params:acme:error:{kind}", "detail": detail},
                    ctype="application/problem+json")

    def _verify(self, h, body):
        jws = json.loads(body)
        prot = json.loads(b64d(jws["protected"]))
        with self.lock:
            if self.bad_nonce_once:
                self.bad_nonce_once = False
                self.nonces.discard(prot.get("nonce"))
                raise _Problem(400, "badNonce", "first nonce refused on purpose")
            if prot.get("nonce") not in self.nonces:
                raise _Problem(400, "badNonce", "unknown nonce")
            self.nonces.discard(prot["nonce"])
        if prot.get("url") != self.url + h.path:
            raise _Problem(401, "unauthorized", "url mismatch")
        if prot.get("alg") != "ES256":
            raise _Problem(400, "badSignatureAlgorithm")
        if "jwk" in prot:
            jwk = prot["jwk"]
        else:
            jwk = self.accounts.get(prot.get("kid"))
            if not jwk:
                raise _Problem(400, "accountDoesNotExist")
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(b64d(jwk["x"]), "big"),
                                            int.from_bytes(b64d(jwk["y"]), "big"),
                                            ec.SECP256R1()).public_key()
        sig = b64d(jws["signature"])
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        try:
            pub.verify(der, f"{jws['protected']}.{jws['payload']}".encode(), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise _Problem(400, "malformed", "bad signature") from None
        payload = json.loads(b64d(jws["payload"])) if jws["payload"] else None
        return prot, jwk, payload

    @staticmethod
    def thumbprint(jwk):
        canon = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"))
        return b64u(hashlib.sha256(canon.encode()).digest())

    def handle(self, h, method, body):
        u = self.url
        if h.path == "/directory":
            return _json(h, 200, {"newNonce": u + "/new-nonce", "newAccount": u + "/new-account",
                                  "newOrder": u + "/new-order"})
        if h.path == "/new-nonce":
            return h._send(200 if method == "HEAD" else 204, b"", {"Replay-Nonce": self._nonce()})
        if method != "POST":
            return self._err(h, 405, "malformed")
        try:
            prot, jwk, payload = self._verify(h, body)
            return self._route(h, prot, jwk, payload)
        except _Problem as p:
            return self._err(h, p.status, p.kind, p.detail)

    def _route(self, h, prot, jwk, payload):
        u, path = self.url, h.path
        if path == "/new-account":
            kid = f"{u}/acct/{len(self.accounts) + 1}"
            self.accounts[kid] = jwk
            return self._reply(h, 201, {"status": "valid"}, {"Location": kid})
        kid = prot.get("kid")
        if path == "/new-order":
            oid = secrets.token_hex(4)
            names = [i["value"] for i in payload["identifiers"]]
            authz_urls = []
            for n in names:
                aid = secrets.token_hex(4)
                self.authzs[aid] = {"status": "pending", "identifier": {"type": "dns", "value": n},
                                    "kid": kid, "order": oid,
                                    "challenges": [{"type": "http-01", "url": f"{u}/chal/x", "token": "x"},
                                                   {"type": "dns-01", "url": f"{u}/chal/{aid}",
                                                    "token": secrets.token_urlsafe(16), "status": "pending"}]}
                authz_urls.append(f"{u}/authz/{aid}")
            self.orders[oid] = {"status": "pending", "identifiers": payload["identifiers"],
                                "authorizations": authz_urls, "finalize": f"{u}/finalize/{oid}",
                                "kid": kid}
            return self._reply(h, 201, self._pub(self.orders[oid]), {"Location": f"{u}/order/{oid}"})
        kind, _, oid = path.strip("/").partition("/")
        if kind == "authz":
            return self._reply(h, 200, self._pub(self.authzs[oid]))
        if kind == "chal":
            a = self.authzs[oid]
            chal = a["challenges"][1]
            host = a["identifier"]["value"]
            want = b64u(hashlib.sha256(f"{chal['token']}.{self.thumbprint(self.accounts[kid])}".encode()).digest())
            got = self.dns.txt.get(f"_acme-challenge.{host}")
            ok = got == want
            a["status"] = chal["status"] = "valid" if ok else "invalid"
            if not ok:
                chal["error"] = {"detail": f"TXT record mismatch (got {got!r})"}
            order = self.orders[a["order"]]
            if all(self.authzs[x.rsplit("/", 1)[1]]["status"] == "valid" for x in order["authorizations"]):
                order["status"] = "ready"
            elif not ok:
                order["status"] = "invalid"
            return self._reply(h, 200, self._pub(chal))
        if kind == "finalize":
            order = self.orders[oid]
            if order["status"] != "ready":
                raise _Problem(403, "orderNotReady")
            csr = x509.load_der_x509_csr(b64d(payload["csr"]))
            assert csr.is_signature_valid
            sans = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if sorted(sans.get_values_for_type(x509.DNSName)) != sorted(i["value"] for i in order["identifiers"]):
                raise _Problem(400, "badCSR", "names differ from the order")
            pem = self.ca.issue(csr.public_key(), [i["value"] for i in order["identifiers"]], self.days)
            self.issued.append(pem)
            order["status"], order["certificate"], order["pem"] = "valid", f"{u}/cert/{oid}", pem
            return self._reply(h, 200, self._pub(order), {"Location": f"{u}/order/{oid}"})
        if kind == "order":
            return self._reply(h, 200, self._pub(self.orders[oid]))
        if kind == "cert":
            return self._reply(h, 200, raw=self.orders[oid]["pem"], ctype="application/pem-certificate-chain")
        raise _Problem(404, "malformed", "unknown path")

    @staticmethod
    def _pub(obj):
        return {k: v for k, v in obj.items() if k not in ("kid", "order", "pem")}


class _Problem(Exception):
    def __init__(self, status, kind, detail=""):
        super().__init__(detail)
        self.status, self.kind, self.detail = status, kind, detail


# ---------------------------------------------------------------- tunnel relay
def parse_sni(record: bytes):
    """server_name from a TLS record holding a ClientHello, or None."""
    if len(record) < 5 or record[0] != 0x16:
        return None
    b, i = record[5:], 0
    if b[0] != 1:
        return None
    i = 4 + 2 + 32
    i += 1 + b[i]                                        # session id
    i += 2 + int.from_bytes(b[i:i + 2], "big")           # cipher suites
    i += 1 + b[i]                                        # compression methods
    end = i + 2 + int.from_bytes(b[i:i + 2], "big")
    i += 2
    while i + 4 <= end:
        etype, elen = int.from_bytes(b[i:i + 2], "big"), int.from_bytes(b[i + 2:i + 4], "big")
        if etype == 0:
            d = b[i + 4:i + 4 + elen]
            if d[2] == 0:
                n = int.from_bytes(d[3:5], "big")
                return d[5:5 + n].decode("ascii").lower()
        i += 4 + elen
    return None


class FakeTunnelRelay:
    """See module docstring. mode: normal | drop (close right after welcome) |
    silent (never answer pings)."""

    def __init__(self, ca: TestCA, tokens: dict, heartbeat: float = 25, mode: str = "normal",
                 client_addr: str = "203.0.113.7:50123"):
        self.cert_file, self.key_file = ca.server_files(TUNNEL_HOST)
        self.loaded_key_files = []          # every private key file this relay ever loads
        self.tokens, self.heartbeat, self.mode, self.client_addr = tokens, heartbeat, mode, client_addr
        self.captured = bytearray()         # every byte spliced, both directions
        self.sni_seen, self.controls, self.pending = [], {}, {}
        self.connects = 0
        self.hellos = []
        self.rejects = []
        self._ready = threading.Event()
        self._writers = set()

    # lifecycle
    def start(self):
        self._t = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self._t.start()
        assert self._ready.wait(10)
        return self

    def stop(self):
        self.loop.call_soon_threadsafe(self._stop.set)
        self._t.join(10)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_file, self.key_file)
        self.loaded_key_files.append(str(self.key_file))
        t = await asyncio.start_server(self._tunnel, "127.0.0.1", 0, ssl=ctx)
        p = await asyncio.start_server(self._public, "127.0.0.1", 0)
        self.tunnel_port = t.sockets[0].getsockname()[1]
        self.public_port = p.sockets[0].getsockname()[1]
        self._ready.set()
        await self._stop.wait()
        for s in (t, p):
            s.close()
        for w in list(self._writers):
            w.close()

    # tunnel side (control + data), TLS with the relay's own cert
    async def _tunnel(self, reader, writer):
        self._writers.add(writer)
        try:
            msg = json.loads(await reader.readline())
            handle = self.tokens.get(msg.get("token"))
            if msg.get("type") == "hello":
                self.connects += 1
                self.hellos.append({k: v for k, v in msg.items() if k != "token"})
                if not handle or handle != msg.get("handle"):
                    writer.write(b'{"type":"error","code":"unauthorized"}\n')
                    await writer.drain()
                    return
                writer.write(json.dumps({"type": "welcome", "v": 1, "heartbeat": self.heartbeat}).encode() + b"\n")
                await writer.drain()
                if self.mode == "drop":
                    return
                self.controls[handle] = writer
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    m = json.loads(line)
                    if m["type"] == "ping" and self.mode != "silent":
                        writer.write(json.dumps({"type": "pong", "seq": m.get("seq")}).encode() + b"\n")
                    elif m["type"] == "reject":
                        self.rejects.append(m)
                        _, fut = self.pending.pop(m.get("stream"), (None, None))
                        if fut and not fut.done():
                            fut.set_exception(ConnectionError(m.get("reason")))
            elif msg.get("type") == "attach":
                owner, fut = self.pending.pop(msg.get("stream"), (None, None))
                if not handle or not fut or owner != handle:
                    return
                done = asyncio.Event()
                fut.set_result((reader, writer, done))
                await done.wait()
        except (ConnectionError, ValueError, ssl.SSLError):
            return
        finally:
            writer.close()
            self._writers.discard(writer)

    # public side: SNI peek + splice. No TLS here, no keys.
    async def _public(self, reader, writer):
        self._writers.add(writer)
        try:
            head = await reader.readexactly(5)
            hello = head + await reader.readexactly(int.from_bytes(head[3:5], "big"))
            sni = parse_sni(hello)
            self.sni_seen.append(sni)
            if not sni or not sni.endswith("." + HOST_SUFFIX):
                return
            handle = sni[: -len(HOST_SUFFIX) - 1]
            ctrl = self.controls.get(handle)
            if not ctrl:
                return
            sid = secrets.token_urlsafe(18)
            fut = self.loop.create_future()
            self.pending[sid] = (handle, fut)
            ctrl.write(json.dumps({"type": "open", "stream": sid, "client": self.client_addr}).encode() + b"\n")
            dr, dw, done = await asyncio.wait_for(fut, 10)
            self.captured += hello
            dw.write(hello)

            async def pump(src, dst):
                try:
                    while True:
                        data = await src.read(65536)
                        if not data:
                            break
                        self.captured += data
                        dst.write(data)
                        await dst.drain()
                finally:
                    dst.close()

            await asyncio.gather(pump(reader, dw), pump(dr, writer), return_exceptions=True)
            done.set()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            return
        finally:
            writer.close()
            self._writers.discard(writer)
