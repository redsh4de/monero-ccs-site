#!/usr/bin/python3 -I
"""Decide whether the deploy's untrusted inputs are accepted.

Usage:
  checks.py values <pushed> <last-updated>
      print the pushed values, checked; <last-updated> is the "updated" time
      of the values applied last, or 0
  checks.py addresses <keyring> <list>
      print the address list, once a key in <keyring> signed it in <list>.asc
  checks.py apply <values> <verified-list>
      print the checked values with every address not in the verified list as -
  checks.py status <status.json>
      print the release and commit that the site's status.json names
  checks.py snapshot <status.json>
      refuse a merge request snapshot that GitLab itself couldn't have sent
  checks.py release <dir> <keyring> <version> <dest>
      verify that <dir>, holding ccs-site-v<version>.tar, SHA256SUMS and
      SHA256SUMS.asc, is that release, and unpack it into <dest>, which must
      not exist yet

Each pushed or signed input is read once, with a size cap, and the decision is
made on those bytes: gpgv checks the signatures over the same bytes, and a
release unpacks through the same open file that was hashed. Lines split on
newlines only and must be ASCII.

Exit: 0 accepted, 1 refused (with the reason on stderr), 2 usage.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from collections import Counter

ADDRESS = r"[48][1-9A-HJ-NP-Za-km-z]{94}"
VALUE = re.compile(
    rf"[0-9a-f]{{16}} [0-9]{{1,12}}(\.[0-9]{{1,12}})? [0-9]{{1,9}} ({ADDRESS}|-)"
)
SEEN = re.compile(r"seen [0-9a-f]{64}")
UPDATED = re.compile(r"updated ([0-9]{10})")
LISTED = re.compile(rf"{ADDRESS}|general-fund {ADDRESS}")
# a line of sha256sum's output: the hash, two spaces, the file name
SUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]{1,200})")
COMMIT = re.compile(r"[0-9a-f]{40}")
GITLAB_URL = re.compile(r"https://[!-~]{1,500}")
GITLAB_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
SNAPSHOT_FIELDS = {"iid", "title", "author", "web_url", "created_at", "adds"}
MAX_MERGE_REQUESTS = 1_000  # mrs.py reads at most ten pages of 100
MAX_TEXT = 1_000  # GitLab allows 255 characters for a title or a username
VALUES_CAP = LIST_CAP = 1_000_000
SUMS_CAP = 10_000
STATUS_CAP = 50_000_000
TAR_CAP = 200_000_000
# gpgv status lines that start or settle one signature's verdict
VERDICTS = {"NEWSIG", "GOODSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "BADSIG", "ERRSIG"}


class Reject(Exception):
    """An input is refused. The message, if not None, goes to stderr."""


def read_capped(path: str, cap: int, what: str) -> bytes:
    """Return a file's bytes, refusing one over the cap; `what` names it in messages."""
    with open(path, "rb") as file:
        data = file.read(cap + 1)
    if len(data) > cap:
        raise Reject(f"{what}: over {cap:,} bytes")
    return data


def lines(data: bytes, what: str) -> list[str]:
    """Return the lines of strict ASCII text, split on newlines only.

    `what` names the input in the refusal message.
    """
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

    The signature file may hold signatures from many builders, as Bitcoin Core's
    SHA256SUMS.asc does. One from a key the keyring lacks is skipped, so a new
    builder's signature never breaks a check, and so is one from an expired or
    revoked key. A bad signature from a key in the keyring means the bytes
    changed, so then it returns None.

    A signature counts only when gpgv calls it GOODSIG, then names its primary
    key in VALIDSIG. gpgv fails whenever one signature can't be checked, so its
    status lines decide, not its exit status. The keyring path is made absolute,
    since gpgv looks up a bare name in ~/.gnupg.
    """
    keyring = os.path.abspath(keyring)
    command = ["gpgv", "--status-fd", "1", "--keyring", keyring, signature, "-"]
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


def check_values(data: bytes, last: int, now: int) -> str:
    """Return the pushed values with every repeated id dropped.

    The file must end with an updated time newer than the last one applied and at
    most five minutes ahead, hold only whole-line matches, at most one seen line and
    exactly one updated line. A file no newer than the last is refused silently.
    """
    rows = lines(data, "values")
    updated = UPDATED.fullmatch(rows[-1]) if rows else None
    if not updated or int(updated[1]) > now + 300:
        raise Reject("values: the updated line is missing or in the future")
    if int(updated[1]) <= last:
        raise Reject(None)
    patterns = (VALUE, SEEN, UPDATED)
    matched = all(any(p.fullmatch(row) for p in patterns) for row in rows)
    seen = sum(row.startswith("seen ") for row in rows)
    times = sum(row.startswith("updated ") for row in rows)
    if not matched or seen > 1 or times != 1:
        raise Reject("values: a line doesn't match")
    ids = Counter(row.split(" ")[0] for row in rows if VALUE.fullmatch(row))
    kept = [r for r in rows if not VALUE.fullmatch(r) or ids[r.split(" ")[0]] == 1]
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
    return data.decode("ascii")


def apply_list(values: str, listed: set[str]) -> str:
    """Return the values with every address that isn't a line of the list as -.

    Fields split on single spaces only, so a tab can't hide an address.
    """
    rows = []
    for row in values.split("\n"):
        fields = row.split(" ")
        if len(fields) == 4 and fields[3] not in listed:
            fields[3] = "-"
        rows.append(" ".join(fields))
    return "\n".join(rows)


def check_status(data: bytes) -> list[str]:
    """Return status.json's release and commit, each checked."""
    try:
        status = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Reject("status: not JSON") from None
    if not isinstance(status, dict):
        raise Reject("status: not an object")
    release, commit = status.get("release"), status.get("commit")
    if (
        type(release) is not int
        or not 1 <= release <= 999_999
        or not isinstance(commit, str)
        or not COMMIT.fullmatch(commit)
    ):
        raise Reject("status: a field is missing or malformed")
    return [str(release), commit]


def blank_or_matches(pattern: re.Pattern[str], value: object) -> bool:
    """Return whether the value is "", or a string the whole pattern matches."""
    return isinstance(value, str) and (value == "" or bool(pattern.fullmatch(value)))


def check_snapshot(data: bytes) -> None:
    """Refuse a merge request snapshot that GitLab itself couldn't have sent.

    The monitor renders the snapshot in status.json on the backend. Anyone can
    open a merge request with any title, so the text itself is no more than what
    GitLab already serves; this bounds the rest: the fields, their types, their
    lengths, and the shape of each address and time.
    """
    try:
        snapshot = json.loads(data.decode("utf-8")).get("merge_requests")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        raise Reject("snapshot: not a JSON object") from None
    if not isinstance(snapshot, list) or len(snapshot) > MAX_MERGE_REQUESTS:
        raise Reject("snapshot: not a list of at most 1,000 merge requests")
    for mr in snapshot:
        if not isinstance(mr, dict) or set(mr) != SNAPSHOT_FIELDS:
            raise Reject("snapshot: a merge request has other fields")
        if type(mr["iid"]) is not int or not 1 <= mr["iid"] <= 999_999_999:
            raise Reject("snapshot: a merge request number is malformed")
        adds = mr["adds"]
        if not isinstance(adds, list) or len(adds) > MAX_MERGE_REQUESTS:
            raise Reject("snapshot: a merge request's files are malformed")
        texts = [mr["title"], mr["author"], *adds]
        if not all(isinstance(text, str) and len(text) <= MAX_TEXT for text in texts):
            raise Reject("snapshot: a text field is too long, or not text")
        if not blank_or_matches(GITLAB_URL, mr["web_url"]):
            raise Reject("snapshot: a merge request address is malformed")
        if not blank_or_matches(GITLAB_TIME, mr["created_at"]):
            raise Reject("snapshot: a merge request time is malformed")


def release_name(version: int) -> str:
    """Return the file name of release `version`'s archive."""
    return f"ccs-site-v{version}.tar"


def signed_digest(folder: str, keyring: str, version: int) -> str:
    """Return the sha256 of release `version`'s archive, as two signers vouch for it.

    SHA256SUMS is sha256sum's own output. It must carry good signatures from two
    distinct keys in the keyring, and name this release's archive on exactly one
    line: the version is in the signed file name, so an older release can't pass
    as a newer one.
    """
    sums_path = os.path.join(folder, "SHA256SUMS")
    sums = read_capped(sums_path, SUMS_CAP, "release")
    who = signers(keyring, sums_path + ".asc", sums)
    if who is None:
        raise Reject("release: a release signer's signature is bad")
    if len(who) < 2:
        raise Reject("release: needs good signatures from two release signers")
    rows = [SUM_LINE.fullmatch(row) for row in lines(sums, "release")]
    if not rows or not all(rows):
        raise Reject("release: SHA256SUMS isn't sha256sum's output")
    digests = [row[1] for row in rows if row[2] == release_name(version)]
    if len(digests) != 1:
        raise Reject(f"release: this isn't release {version}")
    return digests[0]


def install_release(folder: str, keyring: str, version: int, dest: str) -> None:
    """Verify that the folder holds release `version`, and unpack it into dest.

    The tar must match the signed hash, and hold only regular files and folders,
    at most 200 MB in all. It is hashed, then read again through the same open
    file: callers keep it in a folder only they can write. Dest must not exist
    yet; on a refusal, the caller drops it.
    """
    digest = signed_digest(folder, keyring, version)
    with open(os.path.join(folder, release_name(version)), "rb") as tar_file:
        if hashlib.file_digest(tar_file, "sha256").hexdigest() != digest:
            raise Reject("release: the archive doesn't match its hash")
        tar_file.seek(0)
        try:
            with tarfile.open(fileobj=tar_file, mode="r:") as tar:
                members = tar.getmembers()
                for member in members:
                    parts = member.name.split("/")
                    unsafe = member.name.startswith("/") or ".." in parts
                    if unsafe or not (member.isreg() or member.isdir()):
                        raise Reject(
                            "release: the archive holds a link, a device or an unsafe path"
                        )
                if sum(m.size for m in members if m.isreg()) > TAR_CAP:
                    raise Reject("release: the archive is too big")
                os.makedirs(dest)
                tar.extractall(dest, members=members, filter="data")
        except tarfile.TarError:
            raise Reject("release: the archive doesn't list") from None


def main(argv: list[str]) -> int:
    """Run one command and return its exit status."""
    try:
        match argv:
            case ["values", path, last]:
                if not os.path.exists(path):
                    raise Reject(None)
                data = read_capped(path, VALUES_CAP, "values")
                print(check_values(data, int(last), int(time.time())), end="")
            case ["addresses", keyring, path]:
                if not os.path.exists(path):
                    raise Reject(None)
                print(check_addresses(keyring, path), end="")
            case ["apply", values, path]:
                text = read_capped(values, VALUES_CAP, "values").decode("ascii")
                data = read_capped(path, LIST_CAP, "address list")
                listed = lines(data, "address list")
                print(apply_list(text, set(listed)), end="")
            case ["status", path]:
                fields = check_status(read_capped(path, STATUS_CAP, "status"))
                print(" ".join(fields))
            case ["snapshot", path]:
                check_snapshot(read_capped(path, STATUS_CAP, "snapshot"))
            case ["release", folder, keyring, version, dest]:
                install_release(folder, keyring, int(version), dest)
            case _:
                print(__doc__, file=sys.stderr, end="")
                return 2
    except Reject as refused:
        if refused.args and refused.args[0]:
            print(refused.args[0], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
