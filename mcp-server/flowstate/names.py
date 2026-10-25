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
# (task 57, task 56 C) Armenian, Cherokee, Lisu and Coptic letters that pass for Latin ones.
# Keyed by both the lower-case and the case-folded form (Python folds Cherokee to capitals).
_MORE_LOOKS = {
    # Armenian
    "\u0585": "o", "\u057d": "u", "\u0581": "g", "\u0570": "h", "\u0578": "n", "\u057c": "n",
    "\u0566": "q", "\u0584": "p", "\u0561": "w",
    # Cherokee (capital letters; the small ones follow by case)
    "\u13aa": "a", "\u13f4": "b", "\u13df": "c", "\u13a0": "d", "\u13ac": "e", "\u13c0": "g",
    "\u13bb": "h", "\u13a5": "l", "\u13ab": "j", "\u13e6": "k", "\u13de": "l", "\u13b7": "m",
    "\u13c1": "n", "\u13eb": "o", "\u13be": "o", "\u13e2": "p", "\u13a1": "r", "\u13da": "s",
    "\u13a2": "t", "\u13d9": "v", "\u13d4": "w", "\u13c3": "z", "\u13a9": "y",
    # Lisu
    "\ua4ee": "a", "\ua4d0": "b", "\ua4da": "c", "\ua4d3": "d", "\ua4f0": "e", "\ua4dd": "f",
    "\ua4d6": "g", "\ua4e7": "h", "\ua4f2": "l", "\ua4d9": "j", "\ua4d7": "k", "\ua4e1": "l",
    "\ua4df": "m", "\ua4e0": "n", "\ua4f3": "o", "\ua4d1": "p", "\ua4e3": "r", "\ua4e2": "s",
    "\ua4d4": "t", "\ua4f4": "u", "\ua4e6": "v", "\ua4ea": "w", "\ua4eb": "x", "\ua4ec": "y",
    "\ua4dc": "z",
    # Coptic (small letters; the capitals follow by case)
    "\u2c81": "a", "\u2c89": "e", "\u2c93": "l", "\u2c95": "k", "\u2c99": "m", "\u2c9b": "n",
    "\u2c9f": "o", "\u2ca3": "p", "\u2ca5": "c", "\u2ca7": "t", "\u2ca9": "y", "\u2cad": "x",
    "\u2cb1": "w", "\u2c8d": "z", "\u2c87": "d",
}
for _ch, _to in list(_MORE_LOOKS.items()):
    for _form in {_ch, _ch.lower(), _ch.upper(), _ch.casefold()}:
        if len(_form) == 1:
            _LOOKS.setdefault(_form, _to)
# brackets and dashes that read as the plain ones (‹ › < > « » too: task 56 C)
_BRACKETS = {ch: "(" for ch in "[{⟨⟦⟮⦅〔〖〘〚❨❪❬❮❰❲❴⁽₍﹙（［｛⸨‹<«﹤＜〈《"}
_BRACKETS.update({ch: ")" for ch in "]}⟩⟧⟯⦆〕〗〙〛❩❫❭❯❱❳❵⁾₎﹚）］｝⸩›>»﹥＞〉》"})


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
