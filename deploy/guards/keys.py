"""Signing keys that expire soon."""

import os
import re
import subprocess
import tempfile
import time

from guards.common import Reject


EXPIRY_NOTICE = 30 * 86_400
NEVER = 2**62
# the fields of a row that gpg --with-colons prints
KIND, VALIDITY, EXPIRES, USER_ID, CAPABILITIES = 0, 1, 6, 9, 11


def expiring(keyrings: list[str]) -> None:
    """Refuse when a key in the keyrings can sign for 30 more days at most."""
    # a keyring not placed yet is left to the checks that need it
    placed = [keyring for keyring in keyrings if os.path.exists(keyring)]
    if not placed:
        return
    # the monitor has no home gpg could write to
    with tempfile.TemporaryDirectory() as home:
        command = ["gpg", "--homedir", home, "--batch", "--with-colons"]
        command += ["--show-keys", *placed]
        run = subprocess.run(command, capture_output=True, text=True, check=False)
    if run.returncode != 0:
        raise Reject("keys: gpg can't read the keyrings")
    blocks = re.split(r"\n(?=pub:)", run.stdout)
    soon = [key for key in map(expiry, blocks) if key]
    if soon:
        raise Reject("keys expire: " + "; ".join(soon))


def expiry(block: str) -> str | None:
    """Return "owner: day" for a key that can sign for 30 more days at most."""
    rows = [line.split(":") for line in block.splitlines()]
    if not rows or rows[0][KIND] != "pub" or rows[0][VALIDITY] == "r":
        return None
    keys = [row for row in rows if row[KIND] in ("pub", "sub") and row[VALIDITY] != "r"]
    signing = [row for row in keys if "s" in row[CAPABILITIES]]
    if not signing:
        return None
    # a signature stops verifying when its signing key or the primary key
    # expires. A rotation leaves expired subkeys behind, which sign no more
    valid = [row for row in signing if row[VALIDITY] != "e"]
    until = min(int(row[EXPIRES] or NEVER) for row in [rows[0], *(valid or signing)])
    if until >= time.time() + EXPIRY_NOTICE:
        return None
    owner = next((row[USER_ID] for row in rows if row[KIND] == "uid"), "?")
    day = time.strftime("%Y-%m-%d", time.gmtime(until))
    return f"{owner}: {day}"
