"""Address checks, redaction and escaping, kept in one file to audit."""

import html
import re
import unicodedata

ADDRESS = re.compile(r"[48][1-9A-HJ-NP-Za-km-z]{94}")
ADDRESS_RUN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{90,}")
REMOVED = "[address removed]"

# Written as code points so this file holds no invisible characters. Dropping
# them stops a title from reordering the text next to an address.
BIDI_CONTROLS = dict.fromkeys(
    [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)


def is_address(value: str) -> bool:
    return ADDRESS.fullmatch(value) is not None


def visible(text: str) -> str:
    """Return the text as it copies, without format characters or combining marks.

    An address split by them, such as by a zero-width space, still copies as one.
    """
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Mn"))


def has_address(text: str, ignore_spaces: bool = False) -> bool:
    """Return True if the text, as it copies, holds an address-like run."""
    shown = visible(text)
    if ignore_spaces:
        shown = "".join(shown.split())
    return ADDRESS_RUN.search(shown) is not None


def redact(text: str, ignore_spaces: bool = False) -> str:
    """Drop bidi controls, and replace the text with REMOVED if it holds an address."""
    text = text.translate(BIDI_CONTROLS)
    return REMOVED if has_address(text, ignore_spaces) else text


def php_escape(text: str) -> str:
    """Escape text the way ccs-back's htmlspecialchars(ENT_QUOTES) does."""
    return html.escape(text).replace("&#x27;", "&#039;")
