#!/usr/bin/python3 -I
"""Fetch the proposals repository and the open merge requests from GitLab.

Exit: 0 when the content is current, 1 when a fetch failed.
"""

import os
import re
import shutil
import time
from pathlib import Path

# the physical path, so a run that overlaps an install stays in one release
MODULES = Path(__file__).resolve().parents[1] / "modules.py"
exec(compile(MODULES.read_bytes(), MODULES, "exec"), {"__file__": str(MODULES)})
import lib  # noqa: E402
from alerts import Alerts  # noqa: E402

IN_DIR = lib.CCS_DIR / "in"
FETCH_DIR = lib.CCS_DIR / "fetch"
# 55 minutes, so each hourly run calls GitLab despite the timer's random delay
REFETCH_AFTER = 3300
GITLAB_LINE = re.compile(r"gitlab sha256=[0-9a-f]{64}")


def drop_cut_off_pushes(now: int) -> None:
    """Delete the temporary files of pushes cut off midway.

    A file ten minutes old is no push in flight.
    """
    for path in IN_DIR.glob("values.??????"):
        if not path.is_dir() and now - path.lstat().st_mtime > 600:
            path.unlink()


def check_values_age(alerts: Alerts) -> None:
    """Alert when the values applied last are over 10 minutes old.

    update.py counts values as applied only once the site shows them.
    """
    updated = 0
    for line in lib.read(lib.CCS_DIR / "state/values").splitlines():
        if line.startswith("updated "):
            updated = int(line.removeprefix("updated "))
            break
    stale = alerts.now - updated > 600
    text = "ccs: no values from the backend applied for over 10 minutes"
    alerts.check("stale-values", stale, hold=0, repeat=3000, text=text)


def is_fetch_due(gitlab: str, now: int) -> bool:
    """Return whether the gitlab line changed, or the last try is old enough."""
    if gitlab != lib.read(FETCH_DIR / "gitlab"):
        return True
    tried_at = int(lib.read(FETCH_DIR / "last-fetch") or 0)
    return now - tried_at >= REFETCH_AFTER


def point(link: Path, target: Path, prefix: str) -> None:
    """Point the link at target, and drop every other target but the old one."""
    old = os.readlink(link) if link.is_symlink() else ""
    if old != str(target):
        lib.swap(target, link)
        lib.keep(prefix, target, old)


def fetch_proposals() -> None:
    """Fetch the proposals repository, and export each new commit once.

    Each export holds the merge request behind each file, and in/repo points
    at the newest.
    """
    repo = FETCH_DIR / "proposals.git"
    if not repo.is_dir():
        lib.run("git", "init", "-q", "--bare", repo)
    # a stall fails in a minute; the timeouts stay inside the unit's limit, so
    # the "fetch" alert still runs
    stall = "-c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60".split()
    fetch = ["timeout", "300", "git", "-C", repo, *stall, "fetch", "-q"]
    lib.run(*fetch, lib.PROPOSALS_GIT, "+refs/heads/master:refs/heads/master")
    commit = lib.output("git", "-C", repo, "rev-parse", "master").strip()
    tree = FETCH_DIR / "trees" / commit
    if not tree.is_dir():
        temporary = Path(f"{tree}.tmp")
        shutil.rmtree(temporary, ignore_errors=True)
        lib.export_tree(repo, commit, temporary)
        lib.rename_synced(temporary, tree)
    point(IN_DIR / "repo", tree, f"{FETCH_DIR}/trees/")


def fetch_merge_requests() -> None:
    """Fetch the open merge requests, and point in/mr at a set of them.

    A set is new only when the merge requests changed.
    """
    new = FETCH_DIR / "mr.tmp"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    # the changes fetched so far stay cached, so the next run goes further
    mrs = lib.DEPLOY / "mrs.py"
    lib.run("timeout", "420", mrs, lib.GITLAB_API, new, FETCH_DIR / "changes")
    digest = lib.sha256((new / "open.json").read_bytes())[:16]
    merge_requests = FETCH_DIR / f"mr-{digest}"
    if not merge_requests.is_dir():
        lib.rename_synced(new, merge_requests)
    point(IN_DIR / "mr", merge_requests, f"{FETCH_DIR}/mr-")


def read_gitlab_line() -> str:
    """Return the push's last gitlab line, or "".

    The push is unchecked here, and only decides when to call GitLab.
    """
    gitlab = ""
    for line in lib.read(IN_DIR / "values").splitlines():
        if GITLAB_LINE.fullmatch(line):
            gitlab = line
    return gitlab


def fetch(alerts: Alerts) -> int:
    drop_cut_off_pushes(alerts.now)
    check_values_age(alerts)
    gitlab = read_gitlab_line()
    if not is_fetch_due(gitlab, alerts.now):
        return 0
    lib.write(FETCH_DIR / "last-fetch", str(alerts.now))
    try:
        fetch_proposals()
        fetch_merge_requests()
        lib.write(FETCH_DIR / "gitlab", gitlab)
        failed = False
    except lib.FAILED as error:
        lib.log(f"fetch: content failed: {error}")
        failed = True
    # under an hour, so the hourly tries alone raise and repeat the alert
    text = "ccs: content fetches have failed for 50 minutes"
    alerts.check("fetch", failed, hold=3000, repeat=3000, text=text)
    return 1 if failed else 0


def main() -> int:
    # in memory, so a full disk can still alert
    alerts = Alerts(Path(os.environ["RUNTIME_DIRECTORY"], "alerts"), int(time.time()))
    try:
        return fetch(alerts)
    finally:
        alerts.send()


if __name__ == "__main__":
    lib.main(main)
