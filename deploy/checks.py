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
  checks.py build <dist> <hosts> <tag-files>
      refuse a built site holding a file type it never serves, a link to a
      host not in the <hosts> file, address-like text, or a font or image
      that isn't byte for byte a file in <tag-files>, the tag's own tree
  checks.py site <site> <verified-list>
      refuse a rendered site that holds an address the verified list lacks,
      in its text or its attributes
  checks.py expiring <keyring>...
      refuse when a signing key in the keyrings expires within 30 days, or
      has, naming each
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
import html
import json
import os
import re
import runpy
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter
from pathlib import Path

# the render's own address rule, from the same release, run from the file the
# signers read: an import would take a safety/ folder or a cached .pyc first
RENDER = Path(__file__).resolve().parent / "render"
SAFETY = runpy.run_path(str(RENDER / "safety.py"))
qr_modules = runpy.run_path(str(RENDER / "qr.py"))["modules"]
ADDRESS_RUN = SAFETY["ADDRESS_RUN"]
has_address = SAFETY["has_address"]
grouped_addresses = SAFETY["grouped_addresses"]
visible = SAFETY["visible"]

ADDRESS = SAFETY["ADDRESS"].pattern
VALUE = re.compile(
    rf"proposal [A-Za-z0-9._~%-]{{1,750}} raised=[0-9]{{1,12}}(\.[0-9]{{1,12}})?"
    rf" contributions=[0-9]{{1,9}} address=({ADDRESS}|-)"
)
GITLAB = re.compile(r"gitlab sha256=[0-9a-f]{64}")
UPDATED = re.compile(r"updated ([0-9]{10})")
LISTED = re.compile(rf"(general-fund )?{ADDRESS} [A-Za-z0-9+/=]+")
# a line of sha256sum's output
SUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]{1,200})")
COMMIT = re.compile(r"[0-9a-f]{40}")
GITLAB_URL = re.compile(r"https://[!-~]{1,500}")
GITLAB_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
VOTES = ("upvotes", "downvotes")
SNAPSHOT_FIELDS = {"iid", "title", "author", "web_url", "created_at", "adds", *VOTES}
MAX_MERGE_REQUESTS = 1_000  # mrs.py reads at most ten pages of 100
MAX_TEXT = 1_000  # GitLab allows 255 characters for a title or a username
BINARY_SUFFIXES = {".woff2", ".avif", ".webp", ".png"}
BUILD_SUFFIXES = {".html", ".css"} | BINARY_SUFFIXES
# the authority after // in a URL, absolute or protocol-relative
URL_AUTHORITY = re.compile(r"//([^/\s\"'<>()\\?#]+)")
TAG = re.compile(r"<[^>]*>")
VALUES_CAP = 1_000_000
LIST_CAP = 4_000_000
SUMS_CAP = 10_000
STATUS_CAP = 50_000_000
TAR_CAP = 200_000_000
EXPIRY_NOTICE = 30 * 86_400
NEVER = 2**62
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
    if not all(qr_modules(row.rpartition(" ")[2]) for row in rows):
        raise Reject("address list: a QR code is malformed")
    return data.decode("ascii")


def apply_list(values: str, addresses: set[str]) -> str:
    """Return the values with only the list's addresses for proposals.

    A proposal's address that isn't listed becomes -. Fields split on single
    spaces only, so a tab can't hide an address.
    """
    rows = []
    for row in values.split("\n"):
        fields = row.split(" ")
        address = fields[-1].removeprefix("address=")
        if fields[0] == "proposal" and address not in addresses:
            fields[-1] = "address=-"
        rows.append(" ".join(fields))
    return "\n".join(rows)


def check_status(data: bytes) -> str:
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
    return f"{release} {commit}"


def blank_or_matches(pattern: re.Pattern[str], value: object) -> bool:
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
        if not all(type(mr[k]) is int and 0 <= mr[k] <= 999_999_999 for k in VOTES):
            raise Reject("snapshot: a vote count is malformed")
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
    soon = []
    for block in re.split(r"\n(?=pub:)", run.stdout):
        rows = [line.split(":") for line in block.splitlines()]
        if not rows or rows[0][0] != "pub" or rows[0][1] == "r":
            continue
        keys = [row for row in rows if row[0] in ("pub", "sub") and row[1] != "r"]
        signing = [row for row in keys if "s" in row[11]]
        if not signing:
            continue
        # a signature stops verifying when its signing key or the primary key
        # expires; a rotation leaves expired subkeys behind, which sign no more
        valid = [row for row in signing if row[1] != "e"]
        until = min(int(row[6] or NEVER) for row in [rows[0], *(valid or signing)])
        if until < time.time() + EXPIRY_NOTICE:
            owner = next((row[9] for row in rows if row[0] == "uid"), "?")
            day = time.strftime("%Y-%m-%d", time.gmtime(until))
            soon.append(f"{owner}: {day}")
    if soon:
        raise Reject("keys expire: " + "; ".join(soon))


def release_name(version: int) -> str:
    return f"ccs-site-v{version}.tar"


def url_host(authority: str) -> str:
    """Return the host of a URL's authority, without user, port or case."""
    host = authority.rsplit("@", 1)[-1].lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def read_hosts(path: str) -> set[str]:
    """Return the hosts a build may name, one a line; # starts a comment."""
    rows = lines(read_capped(path, LIST_CAP, "hosts"), "hosts")
    return {row.split("#", 1)[0].strip().lower() for row in rows} - {""}


def sha256(path: Path) -> str:
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def check_build(dist: str, hosts_path: str, tag_files: str) -> None:
    """Refuse a built site that holds anything a release may not.

    The build copies fonts and images unchanged, so a dependency can't swap one
    for a picture of another address. Links are read with entities decoded, and
    addresses from the visible text without tags, so neither can hide behind an
    escape or a tag.
    """
    allowed = read_hosts(hosts_path)
    paths = Path(tag_files).rglob("*")
    tag = {sha256(p) for p in paths if p.is_file() and not p.is_symlink()}
    for path in sorted(Path(dist).rglob("*")):
        name = path.relative_to(dist)
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or path.suffix not in BUILD_SUFFIXES:
            raise Reject(f"build: {name} isn't a file type the site serves")
        if path.suffix in BINARY_SUFFIXES:
            if sha256(path) not in tag:
                raise Reject(f"build: {name} isn't byte for byte a file of the tag")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        shown = text
        if path.suffix == ".html":
            text = html.unescape(text)
            shown = TAG.sub("", text)
        for authority in URL_AUTHORITY.findall(text):
            host = url_host(authority)
            if host not in allowed:
                raise Reject(f"build: {name} names {host}, not a listed host")
        if has_address(shown, ignore_spaces=True):
            raise Reject(f"build: {name} holds address-like text")


def unlisted_run(text: str, listed: set[str]) -> str | None:
    """Return an unlisted address-like run, or valid address in groups, if any.

    Each listed address is cut out of a run first, so text next to one, such as
    a button's label once tags are dropped, isn't taken for an address.
    """
    for run in ADDRESS_RUN.findall(visible(text)):
        rest = run
        for address in listed & {run[i : i + 95] for i in range(len(run) - 94)}:
            rest = rest.replace(address, " ")
        if ADDRESS_RUN.search(rest):
            return run
    return min(grouped_addresses(visible(text)) - listed, default=None)


def check_site(site: str, list_path: str) -> None:
    """Refuse a rendered site that holds an address the signed list lacks.

    This catches a bug in the render or a template. Each file is read with its
    entities decoded, and without format characters or combining marks:
    - as it is, for its attributes;
    - without its tags, for its text as it shows.
    """
    rows = lines(read_capped(list_path, LIST_CAP, "address list"), "address list")
    listed = {row.removeprefix("general-fund ").split(" ")[0] for row in rows}
    for path in sorted(Path(site).rglob("*")):
        if not path.is_file() or path.suffix in BINARY_SUFFIXES:
            continue
        text = html.unescape(path.read_text(encoding="utf-8", errors="replace"))
        run = unlisted_run(text, listed) or unlisted_run(TAG.sub("", text), listed)
        if run:
            shown = f"{path.relative_to(site)} shows {run[:12]}..."
            raise Reject(f"site: {shown}, which isn't on the signed list")


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
                rows = lines(
                    read_capped(path, LIST_CAP, "address list"), "address list"
                )
                listed = {row.split(" ")[0] for row in rows} - {"general-fund"}
                print(apply_list(text, listed), end="")
            case ["status", path]:
                print(check_status(read_capped(path, STATUS_CAP, "status")))
            case ["snapshot", path]:
                check_snapshot(read_capped(path, STATUS_CAP, "snapshot"))
            case ["build", dist, hosts, tag_files]:
                check_build(dist, hosts, tag_files)
            case ["site", folder, list_path]:
                check_site(folder, list_path)
            case ["expiring", *keyrings] if keyrings:
                expiring(keyrings)
            case ["release", folder, keyring, version, dest]:
                install_release(folder, keyring, int(version), dest)
            case _:
                print(__doc__, file=sys.stderr, end="")
                return 2
    except Reject as refused:
        if refused.args[0]:
            print(refused.args[0], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
