#!/usr/bin/python3 -I
"""Write the values the backend pushes, read from ccs-back's files in CCS_BACK."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

# no __pycache__ in the release, which the monitor compares
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# the web server's own patterns, as it refuses the whole file over one bad line
from checks import values as web  # noqa: E402

URL = re.compile(r"/proposals/(.+)\.html")


def amount(value: object) -> str | None:
    """Return the amount as plain decimals, or None if it cannot be one."""
    try:
        number = Decimal(str(0 if value is None else value)).quantize(Decimal("1e-12"))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    # abs() turns -0 into 0, which the line pattern would refuse
    return format(abs(number).normalize(), "f")


def proposals_commit(checkout: Path) -> str:
    """Return the commit of ccs-back's checkout of ccs-proposals."""
    # the checkout belongs to another user, so git is told to trust it
    trust = f"safe.directory={checkout}"
    git = ["git", "-c", trust, "-C", str(checkout), "rev-parse", "HEAD"]
    run = subprocess.run(git, capture_output=True, check=True, text=True)
    return run.stdout.strip()


def read_groups(folder: Path) -> list:
    """Return ccs-back's stage groups, open proposals first, then completed."""
    groups = []
    for name in ("proposals.json", "complete.json"):
        text = (folder / name).read_text(encoding="utf-8")
        groups += json.loads(text, parse_float=Decimal)
    return groups


def value_line(row: dict) -> str | None:
    """Return one proposal's line, or None when it has no page or does not fit."""
    slug = URL.fullmatch(str(row.get("local-url", "")))
    if not slug:
        return None
    raised = amount(row.get("amount-funded"))
    contributions = row.get("contributions")
    address = row.get("donate-address")
    if not isinstance(address, str) or not re.fullmatch(web.ADDRESS, address):
        address = "-"
    name = quote(slug[1], safe="")
    line = (
        f"proposal {name} raised={raised} contributions={contributions}"
        f" address={address}"
    )
    if not web.VALUE.fullmatch(line):
        print(f"values: left out {slug[1]}", file=sys.stderr)
        return None
    return line


def gitlab_line(commit: str, groups: list) -> str:
    """Return the "gitlab" line: a hash of the proposals commit and the Ideas stage."""
    ideas = [group for group in groups if group.get("stage") == "Ideas"]
    ideas_json = json.dumps(ideas, sort_keys=True, ensure_ascii=False, default=str)
    known = f"{commit}\n{ideas_json}"
    return f"gitlab sha256={hashlib.sha256(known.encode()).hexdigest()}"


def write_whole(path: Path, text: str) -> None:
    """Replace the file in one rename, so no reader ever sees part of it."""
    new = path.with_name(f"{path.name}.new")
    with open(new, "w", encoding="utf-8") as file:
        file.write(text)
        os.fchmod(file.fileno(), 0o644)
        os.fsync(file.fileno())
    os.replace(new, path)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: values.py <file>", file=sys.stderr)
        return 2
    folder = Path(os.environ["CCS_BACK"])
    try:
        commit = proposals_commit(folder / "proposals")
        groups = read_groups(folder)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"values: can't read ccs-back's files: {error}", file=sys.stderr)
        return 1

    rows = [row for group in groups for row in group.get("proposals", [])]
    lines = sorted(line for line in map(value_line, rows) if line)
    lines += [gitlab_line(commit, groups), f"updated {int(time.time())}"]
    write_whole(Path(argv[0]), "".join(f"{line}\n" for line in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
