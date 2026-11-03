#!/usr/bin/python3 -I
"""Print the values the backend pushes.

One line per proposal with an id, the amount raised, the contributions and the
address, then what ccs-back sees of GitLab, then the time. It reads only
ccs-back's JSON and its checkout of ccs-proposals, in the CCS_BACK folder that
systemd passes from /etc/ccs/config, and never reaches the network.

Exit: 0 printed, 1 when ccs-back's files can't be read.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

# the web server's own patterns, from the same release: it refuses the whole file
# over one line that doesn't match VALUE, so such a line is left out here.
# Nothing is cached in __pycache__, where the monitor compares releases.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks import ADDRESS, VALUE  # noqa: E402

URL = re.compile(r"/proposals/(.+)\.html")


def amount(value: object) -> str | None:
    """Return the amount as plain decimals, or None if it can't be one."""
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
    """Return one proposal's line, or None when it has no page or doesn't fit.

    The id is a hash of the proposal's page name, so the web server matches the
    line to its page without the backend naming it.
    """
    slug = URL.fullmatch(str(row.get("local-url", "")))
    if not slug:
        return None
    raised = amount(row.get("amount-funded"))
    contributions = row.get("contributions")
    address = row.get("donate-address")
    if not isinstance(address, str) or not re.fullmatch(ADDRESS, address):
        address = "-"
    ident = hashlib.sha256(slug[1].encode()).hexdigest()[:16]
    line = f"{ident} {raised} {contributions} {address}"
    if not VALUE.fullmatch(line):
        print(f"values: left out {slug[1]}", file=sys.stderr)
        return None
    return line


def seen_line(commit: str, groups: list) -> str:
    """Return the "seen" line: a hash of what ccs-back knows of GitLab.

    That is the proposals commit and the Ideas stage, which lists the open merge
    requests. The web server calls GitLab when this changes.
    """
    ideas = [group for group in groups if group.get("stage") == "Ideas"]
    ideas_json = json.dumps(ideas, sort_keys=True, ensure_ascii=False, default=str)
    seen = f"{commit}\n{ideas_json}"
    return f"seen {hashlib.sha256(seen.encode()).hexdigest()}"


def main() -> int:
    """Print the values, and return the exit status."""
    folder = Path(os.environ["CCS_BACK"])
    try:
        commit = proposals_commit(folder / "proposals")
        groups = read_groups(folder)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"values: can't read ccs-back's files: {error}", file=sys.stderr)
        return 1

    lines = []
    for group in groups:
        for row in group.get("proposals", []):
            line = value_line(row)
            if line is not None:
                lines.append(line)

    for line in sorted(lines):
        print(line)
    print(seen_line(commit, groups))
    print(f"updated {int(time.time())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
