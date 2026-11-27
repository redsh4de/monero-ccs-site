"""What the checks share: capped reading, refusals, signatures and hosts."""

import os
import re
import subprocess
from html.entities import html5

LIST_CAP = 4_000_000

BINARY_SUFFIXES = {".woff2", ".avif", ".png"}
# browsers and html.unescape agree only on a reference that ends in ; and names a
# character. A browser leaves &gt6 in an attribute as is, and a template could
# finish &[##]#x202E; as it renders
REFERENCE = re.compile(r"&(?:#[0-9]+;|#[xX][0-9a-fA-F]+;|([A-Za-z][A-Za-z0-9]*;))?")

# gpgv status lines that start or settle one signature's verdict
VERDICTS = {"NEWSIG", "GOODSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "BADSIG", "ERRSIG"}


class Reject(Exception):
    """An input is refused. The message, if not None, goes to stderr."""


def read_capped(path: str, cap: int, what: str) -> bytes:
    """Return a file's bytes, refusing one over the cap."""
    with open(path, "rb") as file:
        data = file.read(cap + 1)
    if len(data) > cap:
        raise Reject(f"{what}: over {cap:,} bytes")
    return data


def lines(data: bytes, what: str) -> list[str]:
    """Return the lines of strict ASCII text, split on newlines only."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Reject(f"{what}: a byte isn't ASCII") from None
    rows = text.split("\n")
    if rows[-1] == "":
        rows.pop()
    return rows


def signers(keyring: str, signature: str, signed: bytes) -> set[str] | None:
    """Return the primary fingerprints of the keyring's keys that signed the bytes.

    - A bad signature from a key in the keyring returns None: the bytes changed.
    - A signature from any other key, or an expired or revoked one, is skipped.
    """
    # gpgv looks up a bare name in ~/.gnupg
    keyring = os.path.abspath(keyring)
    command = ["gpgv", "--status-fd", "1", "--keyring", keyring, signature, "-"]
    # gpgv fails when any one signature cannot be checked, so its status lines decide
    run = subprocess.run(command, input=signed, capture_output=True, check=False)
    good, found = False, set()
    for line in run.stdout.decode("ascii", "replace").split("\n"):
        fields = line.split(" ")
        if len(fields) < 2 or fields[0] != "[GNUPG:]":
            continue
        if fields[1] == "BADSIG":
            return None
        if fields[1] in VERDICTS:
            good = fields[1] == "GOODSIG"
        elif fields[1] == "VALIDSIG" and good:
            # the twelfth field is the primary key's fingerprint
            if len(fields) < 12:
                return None
            found.add(fields[11])
    return found


def url_host(authority: str) -> str:
    """Return the host of a URL's authority, without user, port or case."""
    host = authority.rsplit("@", 1)[-1].lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def read_hosts(path: str) -> set[str]:
    """Return the hosts the site may name, one a line; # starts a comment."""
    rows = lines(read_capped(path, LIST_CAP, "hosts"), "hosts")
    return {row.split("#", 1)[0].strip().lower() for row in rows} - {""}


def loose_ampersand(text: str) -> bool:
    """Return True if an & starts no reference that ends in ; and names a character."""
    return any(
        ref[0] == "&" or ref[1] and ref[1] not in html5
        for ref in REFERENCE.finditer(text)
    )
