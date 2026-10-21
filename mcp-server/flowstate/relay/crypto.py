"""Key and certificate helpers for the relay. Uses the `cryptography` package, imported only
here and only when a relay command runs (it is not a declared FlowState dependency yet; today
it arrives with `mcp` via `pyjwt[crypto]`). Without it the relay commands say so and the rest
of FlowState is unaffected.

Every private key is generated on this computer and only ever written to an owner-only file.
"""

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from . import RelayError


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _lib():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.x509.oid import NameOID
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise RelayError("the relay needs the Python package 'cryptography' "
                         "(not installed in this environment)") from e
    return x509, hashes, serialization, ec, decode_dss_signature, NameOID


def available() -> bool:
    try:
        _lib()
        return True
    except RelayError:
        return False


# ---------------------------------------------------------------- keys
def new_key_pem() -> bytes:
    """A fresh ECDSA P-256 private key, PKCS#8 PEM (unencrypted; the file is 0600)."""
    _, _, ser, ec, _, _ = _lib()
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())


def load_key(pem: bytes):
    _, _, ser, ec, _, _ = _lib()
    key = ser.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise RelayError("relay key is not an ECDSA P-256 key")
    return key


def spki_sha256(public_key) -> str:
    """Hex SHA-256 of the DER SubjectPublicKeyInfo (what the CT watch compares)."""
    _, _, ser, _, _, _ = _lib()
    der = public_key.public_bytes(ser.Encoding.DER, ser.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def key_spki_sha256(pem: bytes) -> str:
    return spki_sha256(load_key(pem).public_key())


# ---------------------------------------------------------------- JWS (ACME)
def jwk(key) -> dict:
    nums = key.public_key().public_numbers()
    return {"crv": "P-256", "kty": "EC", "x": b64u(nums.x.to_bytes(32, "big")),
            "y": b64u(nums.y.to_bytes(32, "big"))}


def jwk_thumbprint(key) -> str:
    """RFC 7638 thumbprint: SHA-256 over the required members in lexicographic order."""
    j = jwk(key)
    canon = json.dumps({k: j[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"))
    return b64u(hashlib.sha256(canon.encode()).digest())


def sign_es256(key, data: bytes) -> bytes:
    """ES256 JWS signature: raw r || s, 32 bytes each (RFC 7518 3.4)."""
    _, hashes, _, ec, decode_dss, _ = _lib()
    r, s = decode_dss(key.sign(data, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def csr_der(key, hostname: str) -> bytes:
    x509, hashes, ser, _, _, NameOID = _lib()
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
           .sign(key, hashes.SHA256()))
    return csr.public_bytes(ser.Encoding.DER)


# ---------------------------------------------------------------- certificates
def _names(cert) -> list:
    x509 = _lib()[0]
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return [n.lower() for n in san.get_values_for_type(x509.DNSName)]
    except x509.ExtensionNotFound:
        return []


def cert_info(pem: bytes) -> dict:
    """First certificate of a PEM chain (or one DER certificate): names, expiry, issuer and
    key fingerprint."""
    x509 = _lib()[0]
    try:
        cert = (x509.load_pem_x509_certificate(pem) if b"-----BEGIN" in pem
                else x509.load_der_x509_certificate(pem))
    except ValueError as e:
        raise RelayError(f"not a PEM certificate ({e})") from e
    return {"names": _names(cert), "not_after": cert.not_valid_after_utc,
            "not_before": cert.not_valid_before_utc, "issuer": cert.issuer.rfc4514_string(),
            "serial": format(cert.serial_number, "x"), "spki_sha256": spki_sha256(cert.public_key())}


def days_left(info: dict, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    return (info["not_after"] - now).total_seconds() / 86400


def covers(pattern: str, hostname: str) -> bool:
    """Does a certificate name (possibly `*.parent`) cover `hostname`?"""
    pattern, hostname = pattern.lower().rstrip("."), hostname.lower().rstrip(".")
    if pattern == hostname:
        return True
    if pattern.startswith("*."):
        head, _, rest = hostname.partition(".")
        return bool(head) and rest == pattern[2:]
    return False
