"""Donation QR codes, each read back before the site shows it.

qrencode works out each code, then zbar, an unrelated decoder, reads it back.
A bug or a planted change in qrencode, which both machines share, then stops
the render instead of showing a code for another address. The PNG is written
here, from the pixels zbar read, so no PNG is ever parsed.
"""

import struct
import subprocess
import zlib
from pathlib import Path

import zbar

# pixels a module, so that in a 1-bit PNG each module is exactly one byte
SCALE = 8


def write(address: str, path: Path) -> None:
    """Draw the address's QR code into path, unless it reads back wrong."""
    text = f"monero:{address}"
    qrencode = ["qrencode", "-t", "ASCII", "-m", "4", text]
    run = subprocess.run(qrencode, check=True, capture_output=True, text=True)
    # each module is two characters, "##" dark or "  " light; a byte of 255
    # is eight white pixels in the PNG, and 0 eight black
    lines = run.stdout.splitlines()
    modules = [bytes(255 * (c == " ") for c in line[::2]) for line in lines]
    side = len(modules) * SCALE
    rows = [row for row in modules for _ in range(SCALE)]
    grey = b"".join(bytes(v for v in row for _ in range(SCALE)) for row in rows)
    # zbar takes a buffer of the wrong size without complaint
    if any(len(row) * SCALE != side for row in modules) or read(side, grey) != [text]:
        raise ValueError(f"the QR code for {address} doesn't read back")
    path.write_bytes(png(side, rows))


def read(side: int, grey: bytes) -> list[str]:
    """Return the text of every QR code zbar finds in a square of 8-bit grey pixels."""
    image = zbar.Image(side, side, "Y800", grey)
    scanner = zbar.ImageScanner()
    scanner.parse_config("disable")
    scanner.parse_config("qrcode.enable")
    scanner.scan(image)
    return [symbol.data for symbol in image]


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
