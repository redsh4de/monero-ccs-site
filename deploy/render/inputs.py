"""Reading the render's input folder.

The folder holds proposals/, mr/open.json, mr/open.tsv, mr/merged.tsv, values,
with its QR lines, and general-fund. This module only parses them; the rules
live in proposals.py and content.py.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from safety import is_address


@dataclass(frozen=True)
class Wallet:
    """One proposal's checked values from the backend."""

    raised: str = "0"
    contributions: str = "0"
    address: str = ""


@dataclass(frozen=True)
class Inputs:
    proposals: dict[str, str]  # File name without .md, to the file's text
    wallets: dict[str, Wallet]  # Proposal id, to its values
    merged: dict[str, str]  # File name, to the merge request that added it
    added: dict[str, list[str]]  # Open merge request number, to the files it adds
    merge_requests: list[dict]  # GitLab's list of open merge requests
    general_fund: str  # The General Fund's address, or ""
    qr_hashes: dict[str, str]  # Address, to the hash the backend read back for its code


def read(folder: Path) -> Inputs:
    added: dict[str, list[str]] = {}
    for number, name in read_pairs(folder / "mr" / "open.tsv"):
        added.setdefault(number, []).append(name)

    return Inputs(
        proposals={
            path.stem: read_text(path) for path in proposal_files(folder / "proposals")
        },
        wallets=read_wallets(folder / "values"),
        merged={
            name: number for number, name in read_pairs(folder / "mr" / "merged.tsv")
        },
        added=added,
        merge_requests=read_merge_requests(folder / "mr" / "open.json"),
        general_fund=read_general_fund(folder / "general-fund"),
        qr_hashes=read_qr_hashes(folder / "values"),
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def proposal_files(folder: Path) -> list[Path]:
    """Return the root .md files that can become pages.

    Symlinks are skipped, so a link in the repo can't pull a server file into a
    page, and so are names that aren't valid UTF-8 or are too long to end in
    .html.
    """
    return sorted(
        path
        for path in folder.glob("*.md")
        if path.is_file() and not path.is_symlink() and usable_name(path.stem)
    )


def usable_name(stem: str) -> bool:
    try:
        return len(stem.encode()) <= 250
    except UnicodeEncodeError:
        return False


def read_wallets(path: Path) -> dict[str, Wallet]:
    """Read the values file, one line per proposal.

    Each line is `<id> <raised> <contributions> <address or ->`. An address that
    isn't well formed counts as none.
    """
    wallets = {}
    for line in read_text(path).splitlines():
        parts = line.split(" ")
        if len(parts) == 4:
            ident, raised, contributions, address = parts
            wallets[ident] = Wallet(
                raised, contributions, address if is_address(address) else ""
            )
    return wallets


def read_qr_hashes(path: Path) -> dict[str, str]:
    """Read the values file's QR lines, each `qr <hash> <address>`."""
    hashes = {}
    for line in read_text(path).splitlines():
        parts = line.split(" ")
        if len(parts) == 3 and parts[0] == "qr" and is_address(parts[2]):
            hashes[parts[2]] = parts[1]
    return hashes


def read_pairs(path: Path) -> Iterator[tuple[str, str]]:
    """Yield the number and file name from each line of a merge request .tsv."""
    for line in read_text(path).splitlines():
        number, _, name = line.partition("\t")
        if number.isdigit() and name:
            yield number, name


def read_merge_requests(path: Path) -> list[dict]:
    """Read GitLab's list of open merge requests.

    Anything else, such as an error object, counts as an empty list.
    """
    entries = json.loads(read_text(path))
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def read_general_fund(path: Path) -> str:
    if not path.is_file():
        return ""
    address = read_text(path).strip()
    return address if is_address(address) else ""


def proposal_id(slug: str) -> str:
    """Return the id a proposal's line in the values file starts with."""
    return sha256(slug.encode()).hexdigest()[:16]
