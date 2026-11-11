"""A donation QR code is drawn only from a signed code that read back."""

import runpy
import struct
import zlib
from pathlib import Path

import pytest

import qr

ADDRESS = Path("example-data/general-fund").read_text().strip()
OTHER = "4" + "B" * 94
TOOL = runpy.run_path("deploy/address-list.py")


def drawn(png: bytes) -> list[list[bool]]:
    """Return the pixels of a PNG from qr.png, light as True."""
    side = struct.unpack(">I", png[16:20])[0]
    length = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + length])
    width = -(-side // 8) + 1
    rows = [raw[i * width + 1 : (i + 1) * width] for i in range(side)]
    return [
        [bool(byte >> (7 - b) & 1) for byte in row for b in range(8)][:side]
        for row in rows
    ]


def test_the_served_png_reads_back_as_the_signed_address():
    png = qr.png(qr.modules(TOOL["code"](ADDRESS)))
    assert TOOL["read_back"](drawn(png)) == [f"monero:{ADDRESS}"]


def test_planted_qrencode_fails_the_signers_read_back(tmp_path, monkeypatch):
    planted = tmp_path / "bin" / "qrencode"
    planted.parent.mkdir()
    planted.write_text(
        f'#!/bin/sh\nexec /usr/bin/qrencode -t ASCII -m 0 "monero:{OTHER}"\n'
    )
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted.parent}:/usr/bin:/bin")
    with pytest.raises(ValueError, match="doesn't read back"):
        TOOL["code"](ADDRESS)
