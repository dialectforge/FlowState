"""Names of keys, tokens, OAuth apps and the names apps send: one way to compare them.

Who wrote what is matched by a sign-in's name (identity.same_sign_in), pause switches are kept
by name, and pages show "Grok Bot (via Cursor)". So two names a person can't tell apart must
count as the same name, and an app may not send a name that imitates FlowState's own marks.

  tidy(text)       NFKC (full-width and other compatibility forms become plain ones) and
                   without invisible characters (zero-width, direction marks and other format
                   characters, variation selectors, fillers). What is kept of a name an app
                   sends, and of an OAuth app's own name.
  skeleton(text)   the comparison key: tidy, accents and other combining marks dropped, case
                   folded, and Cyrillic / Greek letters (and 0, 1, |) that look like Latin
                   ones replaced by those, so "Cursor", "CURSOR", "Сursor" (Cyrillic С) and
                   "Cur​sor" are one name. Over-strict on purpose ("i" and "l" count as one
                   letter, since a capital I looks like an l): a refused name only means
                   "pick another".
  invisible(text)  True if the text holds invisible characters (new key / token names are
                   refused with them rather than silently changed).
  reserved(name)   True for names FlowState uses for itself or for the user ("unknown",
                   "phone", "user", "relay", "shared token", ...).
  strip_marks(t)   the text without anything that reads as "(via ...)" or "(signed in as
                   ...)", also when written with lookalike brackets or letters.

Pure Python, standard library only (unicodedata); a small hand-made table, not the full
Unicode confusables list.
"""

import re
import unicodedata

# characters with no width that the Unicode categories below don't all catch
_INVISIBLE_EXTRA = {"ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀", "͏", "឴",
                    "឵", "᠋", "᠌", "᠍", "᠎", "᠏"}


def _is_invisible(ch: str) -> bool:
    if ch in _INVISIBLE_EXTRA:
        return True
    if "︀" <= ch <= "️" or "\U000e0100" <= ch <= "\U000e01ef":   # variation selectors
        return True
    return unicodedata.category(ch) in ("Cf", "Cc", "Co", "Cs") and ch not in "\t\n\r"


def invisible(text: str) -> bool:
    return any(_is_invisible(ch) for ch in text or "")


def tidy(text: str) -> str:
    """NFKC, invisible characters removed, runs of spaces made one."""
    text = "".join(ch for ch in unicodedata.normalize("NFKC", text or "") if not _is_invisible(ch))
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


# lower-case lookalikes (the text is case folded first) -> the Latin letter they imitate
_LOOKS = {
    # Cyrillic
    "а": "a", "в": "b", "д": "d", "е": "e", "ё": "e", "ѐ": "e", "и": "u",
    "к": "k", "м": "m", "н": "h", "о": "o", "п": "n", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "ш": "w", 
    "ѕ": "s", "і": "l", "ї": "l", "ј": "j", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ү": "y", "ұ": "y",
    "һ": "h", "ӏ": "l", "ɡ": "g", "ҽ": "e", "ө": "o", "ѵ": "v", "ԍ": "g", "ԃ": "d",
    # Greek
    "α": "a", "β": "b", "γ": "y", "δ": "d", "ε": "e", "ζ": "z", "η": "n", "ι": "l", "κ": "k",
    "μ": "u", "ν": "v", "ο": "o", "ρ": "p", "ς": "c", "σ": "o", "τ": "t", "υ": "u", "χ": "x",
    "ω": "w", "ϲ": "c", "ϳ": "j", "ϱ": "p",
    # Latin and digits that pass for other letters
    "ı": "l", "i": "l", "1": "l", "|": "l", "ǀ": "l", "ɩ": "l", "0": "o", "ɑ": "a", "ʋ": "u",
    "ⅰ": "l", "ꞵ": "b", "ɢ": "g", "ʜ": "h", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p",
    "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ᴢ": "z", "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d",
    "ᴇ": "e", "ꜰ": "f", "ᴋ": "k", "ʀ": "r", "ꜱ": "s", "ʏ": "y",
}
# brackets and dashes that read as the plain ones
_BRACKETS = {ch: "(" for ch in "[{⟨⟦⟮⦅〔〖〘〚❨❪❬❮❰❲❴⁽₍﹙（［｛⸨"}
_BRACKETS.update({ch: ")" for ch in "]}⟩⟧⟯⦆〕〗〙〛❩❫❭❯❱❳❵⁾₎﹚）］｝⸩"})


def _one(ch: str) -> str:
    """One character -> exactly one character (so positions in mask() match the text)."""
    low = ch.lower()
    low = low if len(low) == 1 else ch
    return _BRACKETS.get(low) or _LOOKS.get(low, low)


def mask(text: str) -> str:
    """Per character (same length as `text`): lower case, lookalikes and brackets made plain."""
    return "".join(_one(ch) for ch in text)


def skeleton(text: str) -> str:
    base = unicodedata.normalize("NFKD", tidy(text))
    base = "".join(ch for ch in base if unicodedata.category(ch) not in ("Mn", "Me"))
    folded = unicodedata.normalize("NFKC", base.casefold())
    return " ".join("".join(_LOOKS.get(ch, ch) for ch in folded).split())


# names FlowState shows for itself, the user and callers it doesn't know (who did what, the
# review queue's "decided by", the pause list); compared by skeleton
_RESERVED = ("unknown", "relay", "tunnel", "lan", "phone", "user", "the user", "you", "owner",
             "owner (lan token)", "shared token", "shared token (legacy)", "card", "cli",
             "terminal", "review page", "flowstate", "local", "system", "self-reported",
             "relay (unknown app)", "tunnel (unknown app)", "unnamed app")
RESERVED = frozenset(skeleton(n) for n in _RESERVED)
# FlowState's own "phone (...)", "card (<app>)", "user (...)" labels
_RESERVED_PREFIX = tuple(skeleton(n) + " (" for n in ("phone", "card", "user", "owner", "relay",
                                                       "tunnel", "lan"))


def reserved(name: str) -> bool:
    key = skeleton(name)
    return key in RESERVED or key.startswith(_RESERVED_PREFIX)


# "(via ...)" and "(signed in as ...)": how FlowState shows a key's or sign-in's name next to
# the app's; matched on mask(), so lookalike brackets and letters count too
_MARK = re.compile(r"\s*\(\s*(?:vla|slgned\s*ln\s*as)\b[^)]*\)?")


def has_marks(text: str) -> bool:
    return bool(_MARK.search(mask(tidy(text))))


def strip_marks(text: str) -> str:
    text = tidy(text)
    m = mask(text)
    out, last = [], 0
    for hit in _MARK.finditer(m):
        out.append(text[last:hit.start()])
        last = hit.end()
    out.append(text[last:])
    return " ".join("".join(out).split())
