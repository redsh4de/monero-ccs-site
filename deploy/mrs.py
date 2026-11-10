#!/usr/bin/python3 -I
r"""Fetch GitLab's open merge requests and the files each one adds.

Usage: mrs.py <project-api> <out-dir> <cache-dir>

  <project-api>  GitLab's API address for ccs-proposals, GITLAB_API in lib.sh
  <out-dir>      where open.json and open.tsv go
  <cache-dir>    the files each merge request adds, kept between runs

Writes <out-dir>/open.json, every page of GitLab's list joined, and
<out-dir>/open.tsv, one "<number>\t<file>" line per file a merge request adds.
Each merge request's files are fetched once per head commit and kept in
<cache-dir>. A merge request whose files can't be fetched adds none this time,
so one merge request can't stop the rest. curl takes its proxy from ALL_PROXY.

Exit: 0 written, 1 when GitLab's list can't be read whole, 2 usage.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PAGE = 100
MAX_PAGES = 10
LIST_CAP = 100_000_000
DIFFS_CAP = 20_000_000
SHA = re.compile(r"[0-9a-f]{40}")


def get(url: str, cap: int) -> object:
    """Return the JSON at a URL, fetched with curl, at most `cap` bytes."""
    command = ["curl", "-fsS", "--max-time", "60", "--max-filesize", str(cap), url]
    command += ["--retry", "3"]
    return json.loads(subprocess.run(command, capture_output=True, check=True).stdout)


def write(path: Path, text: str) -> None:
    """Write a file whole, through a temporary name.

    It is on disk before the name appears: a later run trusts a cached file list
    by its name, so a crash must not leave an empty one.
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        file.write(text)
        os.fsync(file.fileno())
    tmp.rename(path)


def pages(url: str, cap: int) -> list:
    """Return a GitLab list whole, its pages joined, or raise ValueError."""
    entries: list = []
    for page in range(1, MAX_PAGES + 1):
        batch = get(f"{url}per_page={PAGE}&page={page}", cap)
        if not isinstance(batch, list):
            raise ValueError("GitLab didn't send a list")
        entries += batch
        if len(batch) < PAGE:
            return entries
    raise ValueError(f"more than {PAGE * MAX_PAGES} entries")


def new_files(diffs: list) -> list[str]:
    """Return the paths a merge request adds, skipping any with a tab or a line break."""
    return [
        diff["new_path"]
        for diff in diffs
        if isinstance(diff, dict)
        and diff.get("new_file") is True
        and isinstance(diff.get("new_path"), str)
        and not {"\t", "\n", "\r"} & set(diff["new_path"])
    ]


def cached_files(api: str, iid: int, path: Path) -> list[str] | None:
    """Return the files merge request `iid` adds, from the cache file or GitLab.

    The cache file is named by the merge request's head commit, so it is fetched
    again only when the merge request changes. Returns None when GitLab can't
    say, and caches nothing then.
    """
    if not path.exists():
        try:
            files = new_files(pages(f"{api}/merge_requests/{iid}/diffs?", DIFFS_CAP))
        except (subprocess.CalledProcessError, ValueError):
            return None
        write(path, "".join(f"{name}\n" for name in files))
    return path.read_text(encoding="utf-8").split("\n")[:-1]


def main(argv: list[str]) -> int:
    """Write the open merge requests and their files, and return the exit status."""
    if len(argv) != 3:
        print(__doc__, file=sys.stderr, end="")
        return 2
    api, out, cache = argv[0], Path(argv[1]), Path(argv[2])

    try:
        entries = pages(f"{api}/merge_requests?state=opened&", LIST_CAP)
    except (subprocess.CalledProcessError, ValueError) as error:
        print(f"merge requests: GitLab's list didn't load: {error}", file=sys.stderr)
        return 1

    cache.mkdir(parents=True, exist_ok=True)
    used, lines = set(), []
    for mr in entries:
        if not isinstance(mr, dict):
            continue
        iid, sha = mr.get("iid"), mr.get("sha")
        if not (isinstance(iid, int) and isinstance(sha, str) and SHA.fullmatch(sha)):
            continue
        path = cache / f"{iid}.{sha}"
        used.add(path.name)
        files = cached_files(api, iid, path)
        if files is None:
            print(f"merge requests: !{iid}'s files didn't load", file=sys.stderr)
            continue
        lines += [f"{iid}\t{name}\n" for name in files]

    for old in cache.iterdir():
        if old.name not in used:
            old.unlink()

    write(out / "open.json", json.dumps(entries, ensure_ascii=False))
    write(out / "open.tsv", "".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
