"""Donation QR codes, drawn on both machines as the backend read them back.

- qrencode works out each code.
- On the backend, zbar, an unrelated decoder, reads the code back, and values.py
  pushes the hash of its modules with the values.
- Each machine then draws the code itself, and closes the donation box of a code
  with another hash.

A bug or a planted change in qrencode, which both machines share, then closes
the boxes instead of showing a code for another address. The web server needs
no decoder, and no PNG is ever parsed: the PNG is written here.
"""

import hashlib
import struct
import subprocess
import zlib
from pathlib import Path

# pixels a module, so that in a 1-bit PNG each module is exactly one byte
SCALE = 8


def modules(address: str) -> list[bytes]:
    """Return the address's code from qrencode: a byte a module, 255 light, 0 dark."""
    qrencode = ["qrencode", "-t", "ASCII", "-m", "4", f"monero:{address}"]
    run = subprocess.run(qrencode, check=True, capture_output=True, text=True)
    # each module is two characters, "##" dark or "  " light
    lines = run.stdout.splitlines()
    rows = [bytes(255 * (c == " ") for c in line[::2]) for line in lines]
    if not rows or any(len(row) != len(rows) for row in rows):
        raise ValueError(f"the QR code for {address} isn't square")
    return rows


def code_hash(rows: list[bytes]) -> str:
    """Return the sha256 of a code's modules."""
    return hashlib.sha256(b"".join(rows)).hexdigest()


def read_back(address: str) -> str:
    """Return the hash of the address's code, once zbar reads it as the address."""
    # only the backend has zbar
    import zbar

    rows = modules(address)
    side = len(rows) * SCALE
    pixels = [row for row in rows for _ in range(SCALE)]
    grey = b"".join(bytes(v for v in row for _ in range(SCALE)) for row in pixels)
    image = zbar.Image(side, side, "Y800", grey)
    scanner = zbar.ImageScanner()
    scanner.parse_config("disable")
    scanner.parse_config("qrcode.enable")
    scanner.scan(image)
    if [symbol.data for symbol in image] != [f"monero:{address}"]:
        raise ValueError(f"the QR code for {address} doesn't read back")
    return code_hash(rows)


def is_read_back(address: str, expected: str | None) -> bool:
    """Return whether the address's code is the one the backend read back."""
    return code_hash(modules(address)) == expected


def write(address: str, path: Path, expected: str | None) -> None:
    """Draw the address's code into path, unless its hash isn't the expected one."""
    rows = modules(address)
    if code_hash(rows) != expected:
        raise ValueError(f"the QR code for {address} isn't the one the backend read")
    pixels = [row for row in rows for _ in range(SCALE)]
    path.write_bytes(png(len(pixels), pixels))


def png(side: int, rows: list[bytes]) -> bytes:
    """Return a square of 1-bit grey rows as a PNG, unfiltered, in one IDAT."""
    header = struct.pack(">IIBBBBB", side, side, 1, 0, 0, 0, 0)
    data = zlib.compress(b"".join(b"\0" + row for row in rows), 9)
    chunks = [chunk(b"IHDR", header), chunk(b"IDAT", data), chunk(b"IEND", b"")]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def chunk(kind: bytes, body: bytes) -> bytes:
    """Return one PNG chunk: its length, kind, body and checksum."""
    crc = zlib.crc32(kind + body)
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)
