#!/usr/bin/python3 -I
"""Print the address list to sign: each address with its QR code.

Usage: address-list.py <subaddresses-file> <general-fund-address>

  <subaddresses-file>  the CCS wallet's first subaddresses, one on each line

Exit: 0 printed, 1 when an address is malformed or a code does not read back,
2 usage.
"""

import base64
import re
import subprocess
import sys
from pathlib import Path

import zbar  # type: ignore[import-not-found]

MODULES = Path(__file__).resolve().parents[1] / "modules.py"
exec(compile(MODULES.read_bytes(), MODULES, "exec"), {"__file__": str(MODULES)})
import qr  # noqa: E402
from safety import is_address  # noqa: E402

SCALE = 8


def read_back(pixels: list[list[bool]]) -> list[str]:
    """Return what zbar reads in the code's pixels, light as True."""
    side = len(pixels) * SCALE
    grey = b"".join(
        bytes(255 * light for light in row for _ in range(SCALE)) * SCALE
        for row in pixels
    )
    image = zbar.Image(side, side, "Y800", grey)
    scanner = zbar.ImageScanner()
    scanner.parse_config("disable")
    scanner.parse_config("qrcode.enable")
    scanner.scan(image)
    return [symbol.data for symbol in image]


def code(address: str) -> str:
    """Return the address's QR code, once zbar reads it back as the servers draw it."""
    qrencode = ["qrencode", "-t", "ASCII", "-m", "0", f"monero:{address}"]
    run = subprocess.run(qrencode, check=True, capture_output=True, text=True)
    dark = [pair == "##" for pair in re.findall("..", run.stdout.replace("\n", ""))]
    encoded = base64.b64encode(qr.pack(dark)).decode()
    if read_back(qr.framed(qr.modules(encoded) or [])) != [f"monero:{address}"]:
        raise ValueError(f"the QR code for {address} doesn't read back")
    return encoded


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr, end="")
        return 2
    subaddresses = Path(argv[0]).read_text(encoding="ascii").split()
    fund = argv[1]
    for address in [*subaddresses, fund]:
        if not is_address(address):
            print(f"address list: {address} isn't an address", file=sys.stderr)
            return 1
    try:
        lines = [f"{a} {code(a)}" for a in subaddresses]
        lines.append(f"general-fund {fund} {code(fund)}")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"address list: {error}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
