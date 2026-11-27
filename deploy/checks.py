#!/usr/bin/python3 -I
"""Decide whether the deploy's untrusted inputs are accepted.

Usage:
  checks.py values <pushed> <applied> <verified-list>
      print the pushed values, then their pins. <applied> holds the values
      applied last, with their pins. Refuse values that:
      - hold a malformed line, or a time in the future
      - break a pin
      - are missing or no newer than <applied>, with no message
  checks.py addresses <keyring> <list>
      print the address list, once a key in <keyring> signed it in <list>.asc
  checks.py apply <values> <verified-list>
      print the checked values without pins, with each address not on the
      verified list as -
  checks.py status <status.json>
      print the release and commit that the site's status.json names
  checks.py snapshot <status.json>
      refuse a merge request snapshot that GitLab itself could not have sent
  checks.py build <dist> <hosts> <tag-files>
      refuse a built site that holds:
      - a file type the site never serves
      - an odd name or character
      - a host not in <hosts>
      - address-like text
      - a stylesheet that could show text of its own
      - a font or image that <tag-files> lacks
  checks.py lockfile <pnpm-lock.yaml>
      refuse a lockfile unless it takes each package from the registry, by
      its name and version
  checks.py site <site> <verified-list> <hosts>
      refuse a rendered site that holds:
      - an address the verified list lacks, JSON values included
      - more than plain HTML of the site's own elements
      - outside the bodies that <site>.spans places, a link off the site,
        unless it goes to a host in <hosts> or a wallet on the list
  checks.py expiring <keyring>...
      refuse when a signing key in the keyrings has expired or expires within
      30 days, and name each
  checks.py release <dir> <keyring> <version> <dest>
      unpack <dir>/ccs-site-v<version>.tar into <dest>, which must not exist
      yet. Refuse unless:
      - two keys in <keyring> signed <dir>/SHA256SUMS, in SHA256SUMS.asc
      - SHA256SUMS gives the tar's hash on one line, and the tar matches it
      - the tar holds only files and folders, of 200 MB at most in all

checks.py reads each pushed or signed input once, with a size cap, and decides
on those bytes:
- gpgv checks the signatures over the same bytes
- a release unpacks through the same open file that was hashed

Lines split on newlines only and must be ASCII.

Exit: 0 accepted, 1 refused (with the reason on stderr), 2 usage.
"""

import os
import sys
from pathlib import Path
from types import ModuleType

DEPLOY = Path(__file__).resolve().parent
# each runs from its own source, which the signers read. An import would take a
# folder or a cached .pyc of the same name first. Each comes before its importers
RUNS = {
    "safety": "render/safety.py",
    "qr": "render/qr.py",
    "guards": "guards/__init__.py",
    "guards.common": "guards/common.py",
    "guards.style": "guards/style.py",
    "guards.values": "guards/values.py",
    "guards.snapshot": "guards/snapshot.py",
    "guards.build": "guards/build.py",
    "guards.lockfile": "guards/lockfile.py",
    "guards.site": "guards/site.py",
    "guards.keys": "guards/keys.py",
    "guards.release": "guards/release.py",
}


def load(name: str, file: str) -> None:
    """Run one of RUNS as the module that the others import by its name."""
    module = sys.modules[name] = ModuleType(name)
    module.__file__ = str(DEPLOY / file)
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, module)
    code = compile((DEPLOY / file).read_bytes(), module.__file__, "exec")
    exec(code, vars(module))


for name, file in RUNS.items():
    load(name, file)
from guards import build, keys, lockfile, release, site, snapshot, values  # noqa: E402
from guards.common import Reject, read_capped  # noqa: E402


def main(argv: list[str]) -> int:
    try:
        match argv:
            case ["values", path, applied, list_path]:
                print(values.checked(path, applied, list_path), end="")
            case ["addresses", keyring, path]:
                if not os.path.exists(path):
                    raise Reject(None)
                print(values.check_addresses(keyring, path), end="")
            case ["apply", path, list_path]:
                text = read_capped(path, values.VALUES_CAP, "values").decode("ascii")
                print(values.apply_list(text, values.read_listed(list_path)), end="")
            case ["status", path]:
                status = read_capped(path, snapshot.STATUS_CAP, "status")
                print(snapshot.check_status(status))
            case ["snapshot", path]:
                status = read_capped(path, snapshot.STATUS_CAP, "snapshot")
                snapshot.check_snapshot(status)
            case ["build", dist, hosts, tag_files]:
                build.check_build(dist, hosts, tag_files)
            case ["lockfile", path]:
                lockfile.check_lockfile(path)
            case ["site", folder, list_path, hosts]:
                site.check_site(folder, list_path, hosts)
            case ["expiring", *keyrings] if keyrings:
                keys.expiring(keyrings)
            case ["release", folder, keyring, version, dest]:
                release.install_release(folder, keyring, int(version), dest)
            case _:
                print(__doc__, file=sys.stderr, end="")
                return 2
    except Reject as refused:
        if refused.args[0]:
            # a refusal may quote an input, so it is escaped for the terminal
            print(ascii(refused.args[0])[1:-1], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
