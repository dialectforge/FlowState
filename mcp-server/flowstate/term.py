"""Text from apps or stored records, made safe to print in the user's terminal (task 57, NEW-7).

A rule, a review item, a project or app name is written by an app. Printed raw, an escape
sequence in it could move the cursor, clear the line or recolour text, so the user would read
(and type `y` to) something other than what is stored. safe() shows every control and
invisible formatting character as a visible escape instead: ESC as `\\x1b`, a right-to-left
override as `\\u202e`. Newlines too, unless the caller prints multi-line text on purpose.
Every CLI line that prints such text goes through safe() (cli_review.py, cli_more.py, cli.py,
cli_data.py and the other command modules).
"""

import re
import unicodedata

# C0 and C1 controls and DEL; plus the invisible formatting characters (bidi overrides and
# isolates, zero-width marks, the BOM) that change how a line reads without showing.
# Not ZWNJ / ZWJ (U+200C, U+200D): they join emoji (family, rainbow flag) and shape Persian or
# Hindi words, and can't move the cursor or hide text (task 60, L-1 / L-2).
_INVISIBLE = "\u061c\u200b\u200e\u200f\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb"
_BAD = re.compile("[\x00-\x1f\x7f-\x9f" + _INVISIBLE + "]")
_BAD_KEEP_NL = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f" + _INVISIBLE + "]")


def _show(m: re.Match) -> str:
    c = ord(m.group(0))
    return f"\\x{c:02x}" if c < 0x100 else f"\\u{c:04x}"


def safe(text, newlines: bool = False) -> str:
    """`text` as str with every control / invisible formatting character shown as an escape.
    newlines=True keeps "\\n" (for text printed as several lines on purpose)."""
    s = "" if text is None else str(text)
    return (_BAD_KEEP_NL if newlines else _BAD).sub(_show, s)


def has_controls(text) -> bool:
    """Does `text` hold a character safe() would escape (other than a plain newline / tab)?"""
    s = "" if text is None else str(text)
    return any(ch not in "\n\t" and (_BAD.match(ch) or unicodedata.category(ch) == "Cc")
               for ch in s)
