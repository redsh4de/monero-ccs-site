#!/usr/bin/python3 -I
"""Fetch GitLab's open merge requests and the files each one adds.

Usage: mrs.py <project-api> <out-dir> <cache-dir>

  <project-api>  GitLab's API address for ccs-proposals, GITLAB_API in lib.py;
                 the list comes from the GraphQL API on the same host
  <out-dir>      where open.json goes
  <cache-dir>    the files each merge request adds, kept between runs

curl takes its proxy from ALL_PROXY.

Exit: 0 written, 1 when GitLab's list cannot be read whole, 2 usage.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

MAX_ENTRIES = 1000
# 100 merge requests, each with only the short fields asked for
LIST_CAP = 1_000_000
DIFFS_PAGE = 100
DIFFS_CAP = 20_000_000
SHA = re.compile(r"[0-9a-f]{40}")
QUERY = """query ($path: ID!, $after: String) {
  project(fullPath: $path) {
    mergeRequests(state: opened, sort: CREATED_DESC, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        iid title author { username } webUrl createdAt upvotes downvotes diffHeadSha
      }
    }
  }
}"""


def get(url: str, cap: int, data: str | None = None) -> Any:
    """Return the JSON at a URL, fetched with curl, at most `cap` bytes."""
    command = ["curl", "-fsS", "--max-time", "60", "--max-filesize", str(cap), url]
    command += ["--retry", "3"]
    if data is not None:
        command += ["-H", "Content-Type: application/json", "--data-binary", data]
    return json.loads(subprocess.run(command, capture_output=True, check=True).stdout)


def write(path: Path, text: str) -> None:
    """Write a file, synced before its name appears, as a later run trusts the name."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        file.write(text)
        os.fsync(file.fileno())
    tmp.rename(path)


def open_merge_requests(api: str) -> list:
    """Return GitLab's open merge requests, newest first, or raise ValueError."""
    base, _, project = api.partition("/api/v4/projects/")
    nodes: list = []
    after = None
    while True:
        variables = {"path": unquote(project), "after": after}
        body = json.dumps({"query": QUERY, "variables": variables})
        answer = get(f"{base}/api/graphql", LIST_CAP, body)
        try:
            found = answer["data"]["project"]["mergeRequests"]
            nodes += list(found["nodes"])
            more, after = (
                found["pageInfo"]["hasNextPage"],
                found["pageInfo"]["endCursor"],
            )
        except (KeyError, TypeError) as error:
            raise ValueError("GitLab didn't send a list") from error
        if len(nodes) > MAX_ENTRIES:
            raise ValueError(f"more than {MAX_ENTRIES} entries")
        if not more:
            return nodes


def new_files(diffs: list) -> list[str]:
    """Return the paths a merge request adds, but none with a tab or a line break."""
    return [
        diff["new_path"]
        for diff in diffs
        if isinstance(diff, dict)
        and diff.get("new_file") is True
        and isinstance(diff.get("new_path"), str)
        and not {"\t", "\n", "\r"} & set(diff["new_path"])
    ]


def added_files(api: str, iid: int) -> list[str]:
    """Return up to two paths merge request `iid` adds, or raise ValueError."""
    files: list[str] = []
    count = page = 0
    while True:
        page += 1
        url = f"{api}/merge_requests/{iid}/diffs?per_page={DIFFS_PAGE}&page={page}"
        diffs = get(url, DIFFS_CAP)
        if not isinstance(diffs, list):
            raise ValueError("GitLab didn't send a list")
        count += len(diffs)
        if count > MAX_ENTRIES:
            raise ValueError(f"more than {MAX_ENTRIES} entries")
        files += new_files(diffs)
        # the render reads a merge request's file only when it adds just one
        if len(diffs) < DIFFS_PAGE or len(files) > 1:
            return files[:2]


def cached_files(api: str, iid: int, path: Path) -> list[str]:
    """Return the files merge request `iid` adds: cached, else from GitLab, or none."""
    if not path.exists():
        try:
            files = added_files(api, iid)
        except (subprocess.CalledProcessError, ValueError):
            print(f"merge requests: !{iid}'s files didn't load", file=sys.stderr)
            return []
        write(path, "".join(f"{name}\n" for name in files))
    return path.read_text(encoding="utf-8").split("\n")[:-1]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr, end="")
        return 2
    api, out, cache = argv[0], Path(argv[1]), Path(argv[2])

    try:
        entries = open_merge_requests(api)
    except (subprocess.CalledProcessError, ValueError) as error:
        print(f"merge requests: GitLab's list didn't load: {error}", file=sys.stderr)
        return 1

    cache.mkdir(parents=True, exist_ok=True)
    used, merge_requests = set(), []
    for mr in entries:
        digits = str(mr.get("iid")) if isinstance(mr, dict) else ""
        if not (digits.isascii() and digits.isdigit()):
            continue
        iid, sha, author = int(digits), mr.get("diffHeadSha"), mr.get("author")
        adds = []
        if isinstance(sha, str) and SHA.fullmatch(sha):
            path = cache / f"{iid}.{sha}"
            used.add(path.name)
            adds = cached_files(api, iid, path)
        merge_requests.append(
            {
                "iid": iid,
                "title": mr.get("title"),
                "author": author.get("username") if isinstance(author, dict) else "",
                "web_url": mr.get("webUrl"),
                "created_at": mr.get("createdAt"),
                "adds": adds,
                "upvotes": mr.get("upvotes", 0),
                "downvotes": mr.get("downvotes", 0),
            }
        )

    for old in cache.iterdir():
        if old.name not in used:
            old.unlink()

    write(out / "open.json", json.dumps(merge_requests, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
