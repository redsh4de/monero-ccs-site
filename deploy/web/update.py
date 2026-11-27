#!/usr/bin/python3 -I
"""Render the fetched content and pushed values into the site nginx serves.

Exit: 0 when the site is current, 1 on a failure; the last good site stays.
"""

import os
import shutil
import tempfile
from pathlib import Path

# the physical path, so a run that overlaps an install stays in one release
MODULES = Path(__file__).resolve().parents[1] / "modules.py"
exec(compile(MODULES.read_bytes(), MODULES, "exec"), {"__file__": str(MODULES)})
import lib  # noqa: E402

# DEPLOY is releases/<N>/deploy, so the release's folder name is its number
RELEASE_DIR = lib.DEPLOY.parent
VERSION = RELEASE_DIR.name
IN_DIR = lib.CCS_DIR / "in"
STATE_DIR = lib.CCS_DIR / "state"
WORK_DIR = lib.CCS_DIR / "work"
CACHE = lib.CCS_DIR / "cache"


def check_new_values() -> None:
    """Write state/values.new from the push when checks.py accepts it.

    Otherwise it holds the values applied last.
    """
    applied, new = STATE_DIR / "values", STATE_DIR / "values.new"
    applied.touch()
    with open(new, "w") as file:
        pushed = IN_DIR / "values"
        verified = WORK_DIR / "addresses"
        accepted = lib.accepts("values", pushed, applied, verified, stdout=file)
    if not accepted:
        shutil.copyfile(applied, new)


def link_inputs(repo: Path, merge_requests: Path) -> None:
    """Link the fetched content into the render's input."""
    (WORK_DIR / "in/proposals").symlink_to(repo / "proposals")
    (WORK_DIR / "in/mr/open.json").symlink_to(merge_requests / "open.json")
    (WORK_DIR / "in/mr/merged.tsv").symlink_to(repo / "mr/merged.tsv")


def render_site(commit: str) -> None:
    """Render the site and its status.json into WORK_DIR/out."""
    out, dist = WORK_DIR / "out", RELEASE_DIR / "dist"
    lib.run("rsync", "-a", "--exclude=/templates/", f"{dist}/", f"{out}/")
    python = ["python3", "-I", "-B"]
    render = [*python, lib.DEPLOY / "render/render.py", WORK_DIR / "in"]
    if not lib.succeeds("timeout", "300", *render, dist / "templates", out, CACHE):
        raise lib.Stop("the render failed, or took more than 300 s")
    status = [*python, lib.DEPLOY / "render/status.py", "write", WORK_DIR / "in"]
    with open(out / "status.json", "w") as file:
        lib.run(*status, VERSION, commit, stdout=file)
    hosts = lib.DEPLOY / "site-hosts"
    if not lib.accepts("site", out, WORK_DIR / "addresses", hosts):
        raise lib.Stop(
            "the render shows an address or a link it may not, so the site stays"
        )


def switch_site() -> None:
    """Point nginx's link at a new copy of WORK_DIR/out.

    Unchanged files stay hard links to the current site, so they keep their time.
    """
    current = os.path.realpath(lib.CCS_DIR / "current")
    new = Path(tempfile.mkdtemp(dir=lib.CCS_DIR / "site"))
    new.chmod(0o755)
    link = f"--link-dest={current}"
    lib.run("rsync", "-a", "--checksum", link, f"{WORK_DIR}/out/", f"{new}/")
    # on disk before it goes live, so a crash cannot serve an empty file
    lib.run("sync", "-f", new)
    lib.swap(new, lib.CCS_DIR / "current")
    lib.keep(f"{lib.CCS_DIR}/site/", new, current)


def update() -> None:
    for folder in [STATE_DIR, lib.CCS_DIR / "site", CACHE]:
        folder.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    (WORK_DIR / "in/mr").mkdir(parents=True)

    lib.verify_list(WORK_DIR / "addresses")
    check_new_values()

    if not (IN_DIR / "repo").is_symlink() or not (IN_DIR / "mr").is_symlink():
        lib.log("no content fetched yet")
        return
    repo = Path(os.path.realpath(IN_DIR / "repo"))
    merge_requests = Path(os.path.realpath(IN_DIR / "mr"))
    # in/repo points at fetch/trees/<commit>
    commit = repo.name
    link_inputs(repo, merge_requests)
    try:
        values_sum = lib.render_values(
            STATE_DIR / "values.new", WORK_DIR / "in", WORK_DIR / "addresses"
        )
    except lib.FAILED as error:
        raise lib.Stop("can't build the render's values") from error

    inputs = f"{VERSION}\n{repo}\n{merge_requests}\n{values_sum}\n"
    inputs_hash = lib.sha256(inputs.encode())
    if inputs_hash != lib.read(STATE_DIR / "rendered"):
        render_site(commit)
        switch_site()
        lib.write(STATE_DIR / "rendered", inputs_hash)

    # the values count as applied only now, so a failing render trips
    # fetch.py's stale-values alert
    (STATE_DIR / "values.new").rename(STATE_DIR / "values")
    shutil.rmtree(WORK_DIR)


if __name__ == "__main__":
    lib.main(update)
