"""Reading the render's input folder, whose files render/README.md lists.

This module only parses them; the rules live in proposals.py and content.py.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from safety import is_address, redact


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
    merge_requests: list[dict]  # What the render reads of each open merge request
    general_fund: str  # The General Fund's address, or ""
    site_url: str
    qr_codes: dict[str, str]  # Address, to its QR code from the signed list


def read(folder: Path) -> Inputs:
    return Inputs(
        proposals={
            path.stem: read_text(path) for path in proposal_files(folder / "proposals")
        },
        wallets=read_wallets(folder / "values"),
        merged={
            name: number for number, name in read_pairs(folder / "mr" / "merged.tsv")
        },
        merge_requests=read_merge_requests(folder / "mr"),
        general_fund=read_general_fund(folder / "general-fund"),
        site_url=read_text(folder / "site").strip(),
        qr_codes=read_qr_codes(folder / "qr"),
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
    """Read the values file's proposal lines.

    Each is `proposal <name> raised=<n> contributions=<n> address=<address or ->`.
    An address that isn't well formed counts as none.
    """
    wallets = {}
    for line in read_text(path).splitlines():
        parts = line.split(" ")
        if len(parts) == 5 and parts[0] == "proposal":
            raised, contributions, address = (p.partition("=")[2] for p in parts[2:])
            wallets[parts[1]] = Wallet(
                raised, contributions, address if is_address(address) else ""
            )
    return wallets


def read_qr_codes(path: Path) -> dict[str, str]:
    """Read the QR codes, a line `<address> <code>` each."""
    pairs = (line.split(" ") for line in read_text(path).splitlines())
    return {p[0]: p[1] for p in pairs if len(p) == 2 and is_address(p[0])}


def read_pairs(path: Path) -> Iterator[tuple[str, str]]:
    """Yield the number and file name from each line of a merge request .tsv."""
    for line in read_text(path).splitlines():
        number, _, name = line.partition("\t")
        if number.isdigit() and name:
            yield number, name


def read_merge_requests(folder: Path) -> list[dict]:
    """Return what the render reads of each open merge request, in GitLab's order.

    Anyone can open one, so any address in its text is removed. Anything but a list
    in open.json, such as an error object, counts as none.
    """
    added: dict[str, list[str]] = {}
    for number, name in read_pairs(folder / "open.tsv"):
        added.setdefault(number, []).append(name)
    entries = json.loads(read_text(folder / "open.json"))
    if not isinstance(entries, list):
        return []
    mrs = []
    for mr in entries:
        if not isinstance(mr, dict):
            continue
        author = mr.get("author")
        username = author.get("username") if isinstance(author, dict) else ""
        mrs.append(
            {
                "iid": mr.get("iid"),
                "title": redact(str(mr.get("title") or ""), ignore_spaces=True),
                "author": redact(str(username or ""), ignore_spaces=True),
                "web_url": str(mr.get("web_url") or ""),
                "created_at": str(mr.get("created_at") or ""),
                "adds": [
                    redact(name, ignore_spaces=True)
                    for name in added.get(str(mr.get("iid")), [])
                ],
                "upvotes": mr.get("upvotes", 0),
                "downvotes": mr.get("downvotes", 0),
            }
        )
    return mrs


def read_general_fund(path: Path) -> str:
    address = read_text(path).strip()
    return address if is_address(address) else ""
