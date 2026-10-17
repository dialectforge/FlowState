"""Secret scanner for free text. A hit names the kind of secret, never the value.

Used on every write: if any text argument looks like a key, token, password, private
key or card number, the write is refused. Patterns are deliberately specific; generic
detection is limited to long high-entropy tokens and 64-character hex strings.
"""

import math
import re
from typing import Any, Iterable, Optional

# (kind, compiled pattern). Order matters: the first match names the kind.
_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}")),
    ("FlowState access token", re.compile(r"\bfs_(?:at|rt|ct|ac|lk)_[A-Za-z0-9_-]{30,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Stripe key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("password in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{3,}@", re.I)),
]

# "password = hunter2", "api_key: abc...", "token=..." (placeholders are allowed)
_ASSIGN = re.compile(
    r"\b(?P<key>passw(?:or)?d|passwd|pwd|secret|client[_-]?secret|api[_-]?key|apikey|"
    r"access[_-]?key|auth[_-]?token|access[_-]?token|token|private[_-]?key)"
    r"\s*(?:is\s*)?[:=]\s*['\"]?(?P<val>[^\s'\",;]{6,})", re.I)
_PLACEHOLDER_CI = re.compile(
    r"^(?:<.*>|\$\{.*\}|\$[A-Z_][A-Z0-9_]*|%[A-Z_]+%|\{\{.*\}\}|\*+|x{3,}|\.{3,}|"
    r"(?:your|my|the|some|example|sample|dummy|fake|test|placeholder|redacted|changeme|"
    r"none|null|true|false|env|environment|keyring|keychain|vault|credential|stored|"
    r"hidden|secret|password|token)[A-Za-z0-9_.-]*)$", re.I)
# Case-sensitive: env var names (FLOWSTATE_TOKEN) and code (os.environ[...], getenv(...)).
_PLACEHOLDER = re.compile(r"^(?:[A-Z][A-Z0-9_]+|.*(?:environ|getenv|\(|\[).*)$")

_HEX64 = re.compile(r"\b[0-9a-fA-F]{64,}\b")
_LONG = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")


def _entropy(s: str) -> float:
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _looks_random(tok: str) -> bool:
    core = tok.strip("=")
    if len(core) < 32 or "/" in core and core.count("/") > 2:  # paths, not tokens
        return False
    classes = sum(bool(re.search(p, core)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    if classes < 3 or len(re.findall(r"[0-9]", core)) < 3:
        return False
    return _entropy(core) >= 4.2


def scan_text(text: str) -> Optional[str]:
    """The kind of the first secret found in text, or None."""
    if not text or len(text) < 6:
        return None
    for kind, pat in _PATTERNS:
        if pat.search(text):
            return kind
    for m in _ASSIGN.finditer(text):
        val, key = m.group("val"), m.group("key").lower()
        if _PLACEHOLDER.match(val) or _PLACEHOLDER_CI.match(val):
            continue
        if key.startswith("pas") or key == "pwd":
            return "password"
        if len(val) >= 12 and re.search(r"[A-Za-z]", val) and re.search(r"[0-9]", val):
            return "secret or key value"
    if _HEX64.search(text):
        return "64-character hex secret"
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and digits[0] in "3456" and _luhn(digits):
            return "payment card number"
    for m in _LONG.finditer(text):
        if _looks_random(m.group(0)):
            return "high-entropy token"
    return None


def _walk(value: Any, path: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


def scan_args(args: dict, skip: Iterable[str] = ()) -> Optional[tuple[str, str]]:
    """(field, kind) of the first secret in any text argument, or None."""
    skip = set(skip)
    for field, text in _walk({k: v for k, v in args.items() if k not in skip}, ""):
        kind = scan_text(text)
        if kind:
            return field, kind
    return None


def refusal(field: str, kind: str) -> dict:
    return {
        "error": "secret_detected",
        "message": (f"Not saved: the text in '{field}' looks like it contains a {kind}. "
                    f"FlowState never stores secrets. Remove it (say where it is kept "
                    f"instead, e.g. 'token is in Credential Manager') and try again. If this "
                    f"is a checksum or an ID, shorten it."),
        "field": field, "kind": kind,
    }
