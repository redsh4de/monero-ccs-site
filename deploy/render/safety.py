"""Address checks, redaction and escaping, kept in one file to audit."""

import hashlib
import html
import re
import unicodedata

ADDRESS = re.compile(r"[48][1-9A-HJ-NP-Za-km-z]{94}")
ADDRESS_RUN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{90,}")
REMOVED = "[address removed]"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# Base58 groups split by one to three other characters, such as spaces. Not by
# 0, O, I or l, which a reader would take for part of an address, nor by a tag.
GROUPS = re.compile(
    r"[1-9A-HJ-NP-Za-km-z]+(?:[^1-9A-HJ-NP-Za-km-z0OIl<>]{1,3}[1-9A-HJ-NP-Za-km-z]+)+"
)

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
    return ADDRESS_RUN.search(shown) is not None or bool(grouped_addresses(shown))


def grouped_addresses(text: str) -> set[str]:
    """Return the valid addresses written in groups, such as split by spaces."""
    found = set()
    for groups in GROUPS.findall(" ".join(text.split())):
        run = re.sub(r"[^1-9A-HJ-NP-Za-km-z]", "", groups)
        for size in (95, 106):
            for start in range(len(run) - size + 1):
                if is_checksummed(run[start : start + size]):
                    found.add(run[start : start + size])
    return found


def is_checksummed(run: str) -> bool:
    """Return True if a 95- or 106-character run decodes to a valid address."""
    data = b""
    for start in range(0, len(run), 11):
        block = run[start : start + 11]
        value = 0
        for char in block:
            value = value * 58 + BASE58.index(char)
        size = 8 if len(block) == 11 else 5
        if value >= 256**size:
            return False
        data += value.to_bytes(size, "big")
    return hashlib.new("keccak-256", data[:-4]).digest()[:4] == data[-4:]


def redact(text: str, ignore_spaces: bool = False) -> str:
    """Drop bidi controls, and replace the text with REMOVED if it holds an address."""
    text = text.translate(BIDI_CONTROLS)
    return REMOVED if has_address(text, ignore_spaces) else text


def php_escape(text: str) -> str:
    """Escape text the way ccs-back's htmlspecialchars(ENT_QUOTES) does."""
    return html.escape(text).replace("&#x27;", "&#039;")
