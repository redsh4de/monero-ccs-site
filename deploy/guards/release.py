"""A signed release, checked and then unpacked."""

import hashlib
import os
import re
import tarfile

from guards.common import Reject, lines, read_capped, signers

SUMS_CAP = 10_000
TAR_CAP = 200_000_000
SUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]{1,200})")


def release_name(version: int) -> str:
    return f"ccs-site-v{version}.tar"


def signed_digest(folder: str, keyring: str, version: int) -> str:
    """Return the sha256 of release `version`'s tar, as two signers vouch for it."""
    sums_path = os.path.join(folder, "SHA256SUMS")
    sums = read_capped(sums_path, SUMS_CAP, "release")
    who = signers(keyring, sums_path + ".asc", sums)
    if who is None:
        raise Reject("release: a release signer's signature is bad")
    if len(who) < 2:
        raise Reject("release: needs good signatures from two release signers")
    texts = lines(sums, "release")
    rows = [row for row in map(SUM_LINE.fullmatch, texts) if row]
    if not rows or len(rows) < len(texts):
        raise Reject("release: SHA256SUMS isn't sha256sum's output")
    # the signed name holds the version, so an old release cannot pass as a newer one
    digests = [row[1] for row in rows if row[2] == release_name(version)]
    if len(digests) != 1:
        raise Reject(f"release: this isn't release {version}")
    return digests[0]


def refuse_unsafe_members(members: list[tarfile.TarInfo]) -> None:
    for member in members:
        parts = member.name.split("/")
        unsafe = member.name.startswith("/") or ".." in parts
        if unsafe or not (member.isreg() or member.isdir()):
            raise Reject(
                "release: the archive holds a link, a device or an unsafe path"
            )
    if sum(member.size for member in members if member.isreg()) > TAR_CAP:
        raise Reject("release: the archive is too big")


def install_release(folder: str, keyring: str, version: int, dest: str) -> None:
    """Verify that the folder holds release `version`, and unpack it into dest.

    The caller must:
    - keep the tar in a folder only the caller can write, as the tar is read twice
    - give a dest that does not exist yet, and remove it on a refusal
    """
    digest = signed_digest(folder, keyring, version)
    with open(os.path.join(folder, release_name(version)), "rb") as tar_file:
        if hashlib.file_digest(tar_file, "sha256").hexdigest() != digest:
            raise Reject("release: the archive doesn't match its hash")
        tar_file.seek(0)
        try:
            with tarfile.open(fileobj=tar_file, mode="r:") as tar:
                members = tar.getmembers()
                refuse_unsafe_members(members)
                os.makedirs(dest)
                tar.extractall(dest, members=members, filter="data")
        except tarfile.TarError:
            raise Reject("release: the archive doesn't list") from None
