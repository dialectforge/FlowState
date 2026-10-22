"""TEST-ONLY approval-grant keys and signer (approvals.py, approve_page.py).

The key pairs are made fresh in every test run, in memory: no private key, not even a fake
one, is ever written to the repository. They stand in for flowstatemcp.com's signing key;
the real public keys are pinned in approvals.FSA_PUBLIC_KEYS by the web task.
"""
import base64
import json
import time


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_key():
    """(private key, public JWK) of a throwaway P-256 key."""
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    n = key.public_key().public_numbers()
    return key, {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")),
                 "y": b64u(n.y.to_bytes(32, "big"))}


def sign(key, claims: dict, kid: str = "fsa-1", header: dict = None) -> str:
    """A compact JWS, ES256 (raw r||s), as the site makes it."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    head = header or {"alg": "ES256", "kid": kid, "typ": "fsa+jwt"}
    signing = b64u(json.dumps(head).encode()) + "." + b64u(json.dumps(claims).encode())
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    return signing + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def claims(item: int, nonce: str, handle: str = "testbox", domain: str = "fsmcp.com", **over) -> dict:
    t = int(time.time())
    c = {"iss": "https://flowstatemcp.com", "aud": f"{handle}.{domain}", "sub": "test-user-0001",
         "handle": handle, "item": item, "nonce": nonce, "action": "approve_review",
         "iat": t, "exp": t + 300}
    c.update(over)
    return c
