"""Encryption for the off-site GitHub copy (2.4.4, data security audit D8).

The nightly copy is encrypted on this computer before it is pushed; GitHub only ever holds the
encrypted file. AES-256-GCM from the `cryptography` package (it already comes with FlowState's
`mcp` dependency, through `pyjwt[crypto]`; the relay uses it too). Without it the push is
refused: an unencrypted copy is never pushed.

The key: 32 random bytes made on this computer the first time, kept in the OS credential store
(Windows Credential Manager `flowstate:offsite-key`, macOS Keychain `flowstate-offsite-key`,
Linux a 0600 file `~/.config/flowstate/offsite-key`). The recovery code is the same key written
for people (`flowstate offsite key`): save it in a password manager. Without it, a lost or
replaced computer can't read the copy; that is the point.

File format (`.fsenc`): MAGIC, 8-byte key id (start of SHA-256 of the key), 8-byte random nonce
prefix, 4-byte chunk size; then 1 MiB chunks, each sealed with nonce = prefix + 4-byte counter
and the header + counter + a last-chunk flag as associated data, so chunks can't be reordered,
dropped or cut off without the check failing.
"""

import base64
import hashlib
import os
import secrets
import struct
from pathlib import Path
from typing import Optional

MAGIC = b"FSENC1\0"
CHUNK = 1 << 20
_HEADER = len(MAGIC) + 8 + 8 + 4
SUFFIX = ".fsenc"


class CryptoError(Exception):
    pass


def _aes(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise CryptoError("encrypting the off-site copy needs the `cryptography` package "
                          "(it normally comes with FlowState's `mcp` dependency). Nothing was "
                          "pushed; reinstall FlowState's packages and try again.") from None
    return AESGCM(key)


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


# ---------------------------------------------------------------- the key
def _store():
    from . import secret_store as SS
    if os.environ.get("FLOWSTATE_TOKEN_STORE_FILE"):   # tests and sandboxes: a private file
        return SS.FileStore(Path(os.environ["FLOWSTATE_TOKEN_STORE_FILE"]).expanduser()
                            .with_name("offsite-key"))
    import platform
    system = platform.system()
    if system == "Windows":
        return SS.WindowsStore(target="flowstate:offsite-key")
    if system == "Darwin":
        return SS.MacStore(service="flowstate-offsite-key")
    from . import paths
    return SS.FileStore(paths.config_dir() / "offsite-key")


def _decode(text: str) -> bytes:
    raw = base64.urlsafe_b64decode(text.strip() + "=" * (-len(text.strip()) % 4))
    if len(raw) != 32:
        raise CryptoError("the stored off-site key is damaged")
    return raw


def get_key(store=None) -> Optional[bytes]:
    v = (store or _store()).get()
    return _decode(v) if v else None


def get_or_create_key(store=None) -> tuple:
    """(key, made_now)."""
    store = store or _store()
    key = get_key(store)
    if key is not None:
        return key, False
    key = secrets.token_bytes(32)
    store.set(base64.urlsafe_b64encode(key).decode().rstrip("="))
    if get_key(store) != key:
        raise CryptoError("the credential store didn't keep the new off-site key; nothing was "
                          "pushed")
    return key, True


def recovery_code(key: bytes) -> str:
    """The key for people: 52 letters and digits in groups of four."""
    b32 = base64.b32encode(key).decode().rstrip("=")
    return "-".join(b32[i:i + 4] for i in range(0, len(b32), 4))


def key_from_code(code: str) -> bytes:
    s = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    try:
        key = base64.b32decode(s + "=" * (-len(s) % 8))
    except ValueError:
        raise CryptoError("that recovery code isn't valid (letters A-Z and digits 2-7)") from None
    if len(key) != 32:
        raise CryptoError("that recovery code is the wrong length")
    return key


# ---------------------------------------------------------------- files
def _aad(header: bytes, n: int, last: bool) -> bytes:
    return header + struct.pack(">I?", n, last)


def encrypt_file(src: Path, dst: Path, key: bytes) -> None:
    aes = _aes(key)
    prefix = secrets.token_bytes(8)
    header = MAGIC + bytes.fromhex(key_id(key)) + prefix + struct.pack(">I", CHUNK)
    from .privatefs import private_file
    private_file(dst)
    with open(src, "rb") as f, open(dst, "wb") as out:
        out.write(header)
        n, chunk = 0, f.read(CHUNK)
        while True:
            nxt = f.read(CHUNK)
            last = not nxt
            out.write(aes.encrypt(prefix + struct.pack(">I", n), chunk, _aad(header, n, last)))
            if last:
                break
            n, chunk = n + 1, nxt


def is_encrypted(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def file_key_id(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(_HEADER)
    return head[len(MAGIC):len(MAGIC) + 8].hex()


def decrypt_file(src: Path, dst: Path, key: bytes) -> None:
    from cryptography.exceptions import InvalidTag
    aes = _aes(key)
    with open(src, "rb") as f:
        header = f.read(_HEADER)
        if len(header) != _HEADER or header[:len(MAGIC)] != MAGIC:
            raise CryptoError("not a FlowState encrypted backup")
        if header[len(MAGIC):len(MAGIC) + 8].hex() != key_id(key):
            raise CryptoError("this backup was encrypted with a different key (another "
                              "computer's, or one from before a key reset): use its recovery code")
        prefix = header[len(MAGIC) + 8:len(MAGIC) + 16]
        size = struct.unpack(">I", header[-4:])[0] + 16
        from .privatefs import private_file
        dst.unlink(missing_ok=True)
        private_file(dst)
        try:
            with open(dst, "wb") as out:
                n, chunk = 0, f.read(size)
                while chunk:
                    nxt = f.read(size)
                    try:
                        out.write(aes.decrypt(prefix + struct.pack(">I", n), chunk,
                                              _aad(header, n, not nxt)))
                    except InvalidTag:
                        raise CryptoError("the encrypted backup is damaged or was changed "
                                          "(its check failed); nothing was restored") from None
                    n, chunk = n + 1, nxt
        except Exception:
            dst.unlink(missing_ok=True)
            raise
