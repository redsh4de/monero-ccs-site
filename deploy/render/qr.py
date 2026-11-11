"""Donation QR codes, drawn from the codes in the signed address list.

A code is its modules, a bit each, dark as 1, row by row, in base64, as
address-list.py writes it. No PNG is ever parsed.
"""

import base64
import struct
import zlib

QUIET = 4


def modules(code: str) -> list[list[bool]] | None:
    """Return the code's rows of modules, dark as True, or None if it isn't a code."""
    try:
        data = base64.b64decode(code, validate=True)
    except ValueError:
        return None
    for size in range(21, 178, 4):
        if (size * size + 7) // 8 == len(data):
            bits = [bool(byte >> (7 - i) & 1) for byte in data for i in range(8)]
            return [bits[row * size : (row + 1) * size] for row in range(size)]
    return None


def framed(rows: list[list[bool]]) -> list[list[bool]]:
    """Return the code's pixels inside its quiet zone, light as True."""
    side = len(rows) + 2 * QUIET
    blank = [[True] * side] * QUIET
    light = [True] * QUIET
    return blank + [light + [not dark for dark in row] + light for row in rows] + blank


def png(rows: list[list[bool]]) -> bytes:
    """Return the code as a 1-bit PNG, a pixel a module, inside its quiet zone."""
    pixels = framed(rows)
    raw = b"".join(b"\0" + pack(row) for row in pixels)
    header = struct.pack(">IIBBBBB", len(pixels), len(pixels), 1, 0, 0, 0, 0)
    chunks = [chunk(b"IHDR", header), chunk(b"IDAT", zlib.compress(raw, 9))]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks) + chunk(b"IEND", b"")


def pack(bits: list[bool]) -> bytes:
    text = "".join("1" if bit else "0" for bit in bits)
    text = text.ljust(-(-len(text) // 8) * 8, "0")
    return int(text, 2).to_bytes(len(text) // 8, "big")


def chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body)
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)
