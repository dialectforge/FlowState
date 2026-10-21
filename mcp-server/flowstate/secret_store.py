"""Where the HTTP bearer token lives: the operating system's credential store, never the
(git-synced, backed-up) data folder.

Lookup order for the server: FLOWSTATE_TOKEN env var -> an explicit --token-file
(legacy) -> the OS store:
    Windows  Credential Manager (generic credential, target 'flowstate:http-token')
    macOS    Keychain (generic password, service 'flowstate-http-token')
    Linux    a 0600 file: ~/.config/flowstate/http-token
No extra packages: Windows uses ctypes (advapi32), macOS the `security` tool with the
token on stdin (not on the command line), Linux plain file permissions.
Nothing here ever prints or logs a token.
"""

import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

from . import paths

TARGET = "flowstate:http-token"       # Windows target name
SERVICE = "flowstate-http-token"      # macOS service name
ACCOUNT = "flowstate"
MIN_LEN = 16


class StoreError(Exception):
    pass


# ---------------------------------------------------------------- Windows
class WindowsStore:
    name = "Windows Credential Manager"

    def __init__(self, target: str = TARGET):
        self.target = target

    def _api(self):
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]

        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        adv.CredWriteW.restype = wintypes.BOOL
        adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        adv.CredReadW.restype = wintypes.BOOL
        adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        adv.CredDeleteW.restype = wintypes.BOOL
        adv.CredFree.argtypes = [ctypes.c_void_p]
        return ctypes, CREDENTIAL, adv

    def set(self, token: str) -> None:
        ctypes, CREDENTIAL, adv = self._api()
        blob = token.encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = CREDENTIAL(Flags=0, Type=1, TargetName=self.target,
                          Comment="FlowState HTTP bearer token", CredentialBlobSize=len(blob),
                          CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
                          Persist=2, AttributeCount=0, Attributes=None, TargetAlias=None,
                          UserName=ACCOUNT)
        if not adv.CredWriteW(ctypes.byref(cred), 0):
            raise StoreError(f"Credential Manager refused the write (error {ctypes.get_last_error()})")

    def get(self) -> Optional[str]:
        ctypes, CREDENTIAL, adv = self._api()
        out = ctypes.POINTER(CREDENTIAL)()
        if not adv.CredReadW(self.target, 1, 0, ctypes.byref(out)):
            return None
        try:
            c = out.contents
            raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            adv.CredFree(out)

    def delete(self) -> bool:
        ctypes, _C, adv = self._api()
        return bool(adv.CredDeleteW(self.target, 1, 0))


# ---------------------------------------------------------------- macOS
class MacStore:
    name = "macOS Keychain"

    def __init__(self, service: str = SERVICE, run=subprocess.run):
        self.service, self.run = service, run

    def set(self, token: str) -> None:
        if any(c in token for c in "\"\\\n\r "):
            raise StoreError("token contains characters the Keychain command can't take")
        # `security -i` reads the command from stdin, so the token never appears in argv.
        cmd = f'add-generic-password -U -a {ACCOUNT} -s {self.service} -w "{token}"\n'
        r = self.run(["security", "-i"], input=cmd, text=True, capture_output=True)
        if r.returncode != 0:
            raise StoreError("Keychain refused the write")

    def get(self) -> Optional[str]:
        r = self.run(["security", "find-generic-password", "-a", ACCOUNT, "-s", self.service,
                      "-w"], text=True, capture_output=True)
        return r.stdout.strip() or None if r.returncode == 0 else None

    def delete(self) -> bool:
        r = self.run(["security", "delete-generic-password", "-a", ACCOUNT, "-s", self.service],
                     text=True, capture_output=True)
        return r.returncode == 0


# ---------------------------------------------------------------- Linux / other
class FileStore:
    name = "private token file"

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else paths.config_dir() / "http-token"

    def set(self, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(self.path, 0o600)

    def get(self) -> Optional[str]:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def delete(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


def default_store():
    if os.environ.get("FLOWSTATE_TOKEN_STORE_FILE"):   # tests and sandboxes: a private file
        return FileStore(Path(os.environ["FLOWSTATE_TOKEN_STORE_FILE"]).expanduser())
    system = platform.system()
    if system == "Windows":
        return WindowsStore()
    if system == "Darwin":
        return MacStore()
    return FileStore()


# ---------------------------------------------------------------- server use
def _read_token_file(path: str, warn=None) -> str:
    p = Path(path).expanduser()
    repo = paths.inside_git_worktree(p.parent)
    if repo and warn:
        warn(f"the token file is inside a git folder ({repo}); move it to the credential "
             f"store with: flowstate token migrate --from \"{p}\"")
    return p.read_text(encoding="utf-8").strip()


def load_token(token_file: Optional[str] = None, store=None, warn=None,
               fallback_file: Optional[str] = None) -> tuple[str, str]:
    """(token, where-it-came-from). Empty token if none is configured.

    `token_file` (legacy --token-file) wins over the store; `fallback_file` is read only
    when neither FLOWSTATE_TOKEN nor the store has a token, so a launcher can name the
    old file and switch to the store automatically once `token migrate` has run."""
    tok = os.environ.get("FLOWSTATE_TOKEN", "").strip()
    if tok:
        return tok, "FLOWSTATE_TOKEN"
    path = token_file or os.environ.get("FLOWSTATE_TOKEN_FILE")
    if path:
        return _read_token_file(path, warn), "token file"
    store = store or default_store()
    try:
        tok = store.get() or ""
    except Exception:  # noqa: BLE001 - a broken store means "no token"
        tok = ""
    if not tok and fallback_file and Path(fallback_file).expanduser().is_file():
        return _read_token_file(fallback_file, warn), "fallback token file"
    return tok, store.name


def migrate_token(src: Path, store=None) -> dict:
    """Copy the token in `src` into the credential store and read it back. `src` is left
    in place (delete it yourself once the server works without it)."""
    src = Path(src).expanduser()
    tok = src.read_text(encoding="utf-8").strip()
    if len(tok) < MIN_LEN:
        raise StoreError(f"{src.name} does not hold a token of at least {MIN_LEN} characters")
    store = store or default_store()
    store.set(tok)
    if store.get() != tok:
        raise StoreError(f"the token read back from {store.name} does not match; not migrated")
    return {"stored_in": store.name, "length": len(tok), "source_left_in_place": str(src)}
