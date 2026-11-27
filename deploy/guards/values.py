"""The backend's pushed values, the signed address list, and the pins."""

import os
import re
import time
from collections import Counter

import qr
import safety
from guards.common import LIST_CAP, Reject, lines, read_capped, signers


VALUES_CAP = 1_000_000

ADDRESS = safety.ADDRESS.pattern
NAME = r"[A-Za-z0-9._~%-]{1,750}"
VALUE = re.compile(
    rf"proposal {NAME} raised=[0-9]{{1,12}}(\.[0-9]{{1,12}})?"
    rf" contributions=[0-9]{{1,9}} address=({ADDRESS}|-)"
)
GITLAB = re.compile(r"gitlab sha256=[0-9a-f]{64}")
UPDATED = re.compile(r"updated ([0-9]{10})")
PIN = re.compile(rf"pin {NAME} {ADDRESS}")
LISTED = re.compile(rf"(general-fund )?{ADDRESS} [A-Za-z0-9+/=]+")


def checked(path: str, applied: str, list_path: str) -> str:
    """Return the pushed values and then their pins, or refuse them."""
    if not os.path.exists(path):
        raise Reject(None)
    last, pins = read_applied(read_capped(applied, VALUES_CAP, "applied values"))
    data = read_capped(path, VALUES_CAP, "values")
    text = check_values(data, last, int(time.time()))
    kept = keep_pins(text, read_listed(list_path), pins)
    return text + "".join(sorted(f"pin {n} {a}\n" for a, n in kept.items()))


def check_values(data: bytes, last: int, now: int) -> str:
    """Return the pushed values with every repeated proposal dropped.

    A file no newer than the last one applied is refused silently.
    """
    rows = lines(data, "values")
    updated = UPDATED.fullmatch(rows[-1]) if rows else None
    if not updated or int(updated[1]) > now + 300:
        raise Reject("values: the updated line is missing or in the future")
    if int(updated[1]) <= last:
        raise Reject(None)
    patterns = (VALUE, GITLAB, UPDATED)
    matched = all(any(p.fullmatch(row) for p in patterns) for row in rows)
    gitlab = sum(row.startswith("gitlab ") for row in rows)
    times = sum(row.startswith("updated ") for row in rows)
    if not matched or gitlab > 1 or times != 1:
        raise Reject("values: a line doesn't match")
    names = Counter(row.split(" ")[1] for row in rows if VALUE.fullmatch(row))
    kept = [r for r in rows if not VALUE.fullmatch(r) or names[r.split(" ")[1]] == 1]
    return "".join(row + "\n" for row in kept)


def check_addresses(keyring: str, path: str) -> str:
    """Return the address list if a key in the keyring signed it and it is well formed."""
    data = read_capped(path, LIST_CAP, "address list")
    if not signers(keyring, path + ".asc", data):
        raise Reject("address list: a signature is bad or missing")
    rows = lines(data, "address list")
    funds = sum(row.startswith("general-fund ") for row in rows)
    if not all(LISTED.fullmatch(row) for row in rows) or funds != 1:
        raise Reject("address list: malformed")
    if not all(qr.modules(row.rpartition(" ")[2]) for row in rows):
        raise Reject("address list: a QR code is malformed")
    return data.decode("ascii")


def read_listed(path: str) -> set[str]:
    """Return the verified list's addresses for proposals."""
    rows = lines(read_capped(path, LIST_CAP, "address list"), "address list")
    return {row.split(" ")[0] for row in rows} - {"general-fund"}


def read_applied(data: bytes) -> tuple[int, dict[str, str]]:
    """Return the applied values' time, and each pinned address with its proposal."""
    rows = lines(data, "applied values")
    times = [int(m[1]) for m in map(UPDATED.fullmatch, rows) if m]
    pins = [row.split(" ") for row in rows if row.startswith("pin ")]
    if not all(PIN.fullmatch(" ".join(pin)) for pin in pins):
        raise Reject("applied values: a pin is malformed")
    return (times or [0])[-1], {address: name for _, name, address in pins}


def keep_pins(values: str, addresses: set[str], pins: dict[str, str]) -> dict[str, str]:
    """Return the pins, with one added for each listed address the values show.

    Refuse values that show:
    - a pinned address for another proposal
    - another listed address for a pinned proposal

    A pin counts only while the list holds its address, but it stays in the file.
    """
    kept = dict(pins)
    held = {name: address for address, name in pins.items() if address in addresses}
    for row in values.split("\n"):
        fields = row.split(" ")
        address = fields[-1].removeprefix("address=")
        if fields[0] != "proposal" or address not in addresses:
            continue
        name = fields[1]
        if kept.setdefault(address, name) != name:
            raise Reject(f"values: {name} shows the address of {kept[address]}")
        if held.setdefault(name, address) != address:
            raise Reject(f"values: {name} moves off its address")
    return kept


def apply_list(values: str, addresses: set[str]) -> str:
    """Return the values without pins, with each unlisted proposal address as -."""
    rows = []
    for row in values.split("\n"):
        # single spaces only, so a tab cannot hide an address
        fields = row.split(" ")
        if fields[0] == "pin":
            continue
        address = fields[-1].removeprefix("address=")
        if fields[0] == "proposal" and address not in addresses:
            fields[-1] = "address=-"
        rows.append(" ".join(fields))
    return "\n".join(rows)
