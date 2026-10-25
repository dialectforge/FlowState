"""Encryption for the off-site GitHub copy (2.4.4, data security audit D8).

The nightly copy is encrypted on this computer before it is pushed; GitHub only ever holds the
encrypted file. AES-256-GCM from the `cryptography` package (it comes with `mcp`; FlowState lists it as its
`offsite` extra and checks for it in tests; the relay uses it too). Without it the push is
refused: an unencrypted copy is never pushed.

The key: 32 random bytes made on this computer the first time, kept in the OS credential store
(Windows Credential Manager `flowstate:offsite-key`, macOS Keychain `flowstate-offsite-key`,
Linux a 0600 file `~/.config/flowstate/offsite-key`). The recovery code is the same key written
for people (`flowstate offsite key`): save it in a password manager. Without it, a lost or
replaced computer can't read the copy; that is the point.

File format (`.fsenc`): MAGIC, 8-byte key id (start of SHA-256 of the key), 8-byte random nonce
prefix, 4-byte chunk size, and (format 2, MAGIC2) the 8-byte time the copy was made (Unix
seconds); then 1 MiB chunks, each sealed with nonce = prefix + 4-byte counter and the header +
counter + a last-chunk flag as associated data, so chunks can't be reordered, dropped or cut off
without the check failing, and the time in the header can't be changed. A file with no chunk at
all (cut down to its header) is refused. Format-1 files (no time) are still read.

The key is made only when the store says definitely "there is none"; a store that can't be
read stops the push, and an existing key is never written over (re-audit N-F6).
"""

import base64
import hashlib
import os
import secrets
import struct
from pathlib import Path
from typing import Optional

MAGIC = b"FSENC1\0"
MAGIC2 = b"FSENC2\0"   # + the time the copy was made, sealed with the chunks
CHUNK = 1 << 20
_HEADER = len(MAGIC) + 8 + 8 + 4
_HEADER2 = _HEADER + 8
SUFFIX = ".fsenc"


class CryptoError(Exception):
    pass


def _aes(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise CryptoError("encrypting the off-site copy needs the `cryptography` package "
                          "(one of FlowState's own packages). Nothing was "
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


_DAMAGED = ("the off-site key stored on this computer is damaged, so FlowState can't use it. "
            "It was not replaced (that would lose your earlier copies); nothing was pushed. If "
            "you saved its recovery code, you can still restore those copies with it.")


def _decode(text: str) -> bytes:
    try:
        t = text.strip()
        raw = base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))
    except (ValueError, TypeError):
        raise CryptoError(_DAMAGED) from None
    if len(raw) != 32:
        raise CryptoError(_DAMAGED)
    return raw


def get_key(store=None) -> Optional[bytes]:
    """The key, or None only when the store says there is none. A store that can't be read
    raises CryptoError (never "no key")."""
    store = store or _store()
    try:
        v = store.get()
    except Exception as e:  # noqa: BLE001 - locked Keychain, denied prompt, unreadable file
        raise CryptoError(
            f"couldn't read the off-site key from {getattr(store, 'name', 'the credential store')} "
            f"({e if str(e) else type(e).__name__}). Nothing was pushed and no new key was made; "
            f"unlock it (or allow FlowState) and try again.") from None
    return _decode(v) if v else None


def get_or_create_key(store=None) -> tuple:
    """(key, made_now). A new key only after a definite "not found", and only with an
    add-only write: an existing item is never overwritten."""
    from . import secret_store as SS
    store = store or _store()
    key = get_key(store)
    if key is not None:
        return key, False
    key = secrets.token_bytes(32)
    text = base64.urlsafe_b64encode(key).decode().rstrip("=")
    try:
        if hasattr(store, "create"):
            store.create(text)
        else:   # a store without an add-only write: check right before writing
            if store.get() is not None:
                raise SS.AlreadyStored("an off-site key appeared meanwhile; it was not replaced")
            store.set(text)
    except SS.AlreadyStored:
        raise CryptoError("an off-site key was already stored (made by another FlowState at "
                          "the same moment?); it was not replaced. Nothing was pushed; try "
                          "again.") from None
    except CryptoError:
        raise
    except Exception as e:  # noqa: BLE001
        raise CryptoError(f"the credential store refused the new off-site key "
                          f"({type(e).__name__}); nothing was pushed") from None
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


def encrypt_file(src: Path, dst: Path, key: bytes, created: Optional[int] = None) -> None:
    """`created`: when the copy was made (Unix seconds; default now), sealed in the header."""
    import time
    aes = _aes(key)
    prefix = secrets.token_bytes(8)
    when = int(time.time()) if created is None else int(created)
    header = (MAGIC2 + bytes.fromhex(key_id(key)) + prefix + struct.pack(">I", CHUNK)
              + struct.pack(">Q", when))
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
            return f.read(len(MAGIC)) in (MAGIC, MAGIC2)
    except OSError:
        return False


def file_key_id(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(_HEADER)
    return head[len(MAGIC):len(MAGIC) + 8].hex()


def decrypt_file(src: Path, dst: Path, key: bytes) -> dict:
    """Decrypt into `dst` (private). Returns {"created": Unix seconds the copy was made, from
    the sealed header, or None for a format-1 file}; only trustworthy because it returned."""
    from cryptography.exceptions import InvalidTag
    aes = _aes(key)
    with open(src, "rb") as f:
        header = f.read(_HEADER)
        magic = header[:len(MAGIC)]
        if len(header) != _HEADER or magic not in (MAGIC, MAGIC2):
            raise CryptoError("not a FlowState encrypted backup")
        created = None
        if magic == MAGIC2:
            extra = f.read(8)
            if len(extra) != 8:
                raise CryptoError("this encrypted copy is cut short or damaged (its header is "
                                  "incomplete); nothing was restored")
            header += extra
            created = struct.unpack(">Q", extra)[0]
        if header[len(MAGIC):len(MAGIC) + 8].hex() != key_id(key):
            raise CryptoError("this backup was encrypted with a different key (another "
                              "computer's, or one from before a key reset): use its recovery code")
        prefix = header[len(MAGIC) + 8:len(MAGIC) + 16]
        size = struct.unpack(">I", header[len(MAGIC) + 16:len(MAGIC) + 20])[0] + 16
        from .privatefs import private_file
        dst.unlink(missing_ok=True)
        private_file(dst)
        try:
            with open(dst, "wb") as out:
                n, chunk = 0, f.read(size)
                if not chunk:   # cut down to its header: not an empty database
                    raise CryptoError("this encrypted copy is cut short or damaged (it has no "
                                      "data after its header); nothing was restored")
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
    return {"created": created}
