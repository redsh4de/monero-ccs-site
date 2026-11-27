"""Address checks, redaction and escaping, kept in one file to audit."""

import hashlib
import html
import re
import unicodedata
from array import array
from collections.abc import Iterator

ADDRESS = re.compile(r"[48][1-9A-HJ-NP-Za-km-z]{94}")
ADDRESS_RUN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{90,}")
REMOVED = "[address removed]"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# html.unescape's own pattern, so each character it decodes keeps its place
CHARREF = re.compile(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")
TAG = re.compile(r"<[^<>]*>")

# Dropped, so a title cannot reorder the text next to an address. Written as
# code points, so this file holds no invisible characters.
BIDI_CONTROLS = dict.fromkeys(
    [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)
# What XML 1.0 can hold, also written as escapes. A feed with any other
# character, such as a vertical tab or half of a character, does not parse.
NOT_XML = re.compile(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def is_address(value: str) -> bool:
    return ADDRESS.fullmatch(value) is not None


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


def address_windows(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield each valid address in the text's base58, with its start and end."""
    # base58 only, so spaces or a hidden 0 cannot split an address
    kept = array("I", (i for i, char in enumerate(text) if char in BASE58))
    run = "".join(text[i] for i in kept)
    for start in range(len(run) - 94):
        if run[start] in "48":
            for size in (95, 106):
                address = run[start : start + size]
                if len(address) == size and is_checksummed(address):
                    yield address, kept[start], kept[start + size - 1] + 1


def decoded(page: str) -> tuple[str, array]:
    """Return the page with its entities decoded, and where each character was."""
    parts, where, last = [], array("I"), 0
    for ref in CHARREF.finditer(page):
        char = html.unescape(ref[0])
        parts += [page[last : ref.start()], char]
        where.extend(range(last, ref.start()))
        where.extend([ref.start()] * len(char))
        last = ref.end()
    parts.append(page[last:])
    where.extend(range(last, len(page)))
    return "".join(parts), where


def unlisted(
    page: str, listed: set[str], decodes: int = 1
) -> Iterator[tuple[str, int, int]]:
    """Yield each address-like run or valid address the list lacks, and where it lies.

    The page is read as it copies, with its entities decoded `decodes` times.
    """
    text, where = page, array("I", range(len(page)))
    for _ in range(decodes):
        text, inner = decoded(text)
        where = array("I", (where[i] for i in inner))
    # tags as the page holds them, as an encoded < in an attribute ends none
    tags = bytearray(len(page))
    for tag in TAG.finditer(page):
        tags[tag.start() : tag.end()] = b"\1" * len(tag[0])
    outside = array("I", (i for i in range(len(text)) if not tags[where[i]]))
    # whole for its attributes, and without tags for its text
    for view in (range(len(text)), outside):
        # a copy loses format characters and combining marks, such as a zero-width space
        kept = array(
            "I", (i for i in view if unicodedata.category(text[i]) not in ("Cf", "Mn"))
        )
        shown = "".join(text[i] for i in kept)
        places = array("I", (where[i] for i in kept))
        for run in ADDRESS_RUN.finditer(shown):
            rest = run[0]
            # so the text next to a listed address is not taken for an address
            for address in listed:
                rest = rest.replace(address, " ")
            if ADDRESS_RUN.search(rest):
                yield run[0], places[run.start()], places[run.end() - 1] + 1
        for address, start, end in address_windows(shown):
            if address not in listed:
                yield address, places[start], places[end - 1] + 1


def clean(text: str) -> str:
    """Return the text without bidi controls, or characters XML cannot hold."""
    return NOT_XML.sub("", text.translate(BIDI_CONTROLS))


def redact(text: str) -> str:
    """Clean the text, and replace it with REMOVED if it shows an address."""
    text = clean(text)
    return REMOVED if any(unlisted(text, set())) else text


def php_escape(text: str) -> str:
    """Escape text the way ccs-back's htmlspecialchars(ENT_QUOTES) does."""
    return html.escape(text).replace("&#x27;", "&#039;")
