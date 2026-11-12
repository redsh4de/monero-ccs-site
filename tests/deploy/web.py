"""A web server's folders and actions, as update.sh sees them."""

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

from scripts import DEPLOY, EXAMPLE, REPO, replaced, run

DIST = REPO / "dist"
COMMIT = "0" * 39 + "7"
A = next(
    line.rpartition("address=")[2]
    for line in (EXAMPLE / "values").read_text().splitlines()
    if not line.endswith("address=-")
)
B = "8" + "B" * 94
C = "8" + "C" * 94


class WebServer:
    """A web server laid out as fetch.sh leaves it, with its releases in lib/."""

    def __init__(self, root: Path):
        self.ccs = root / "ccs"
        self.lib = root / "lib"
        self.current = self.ccs / "current"
        self.pushed = int(time.time()) - 2000
        tree = self.ccs / "fetch/trees" / COMMIT
        merge_requests = self.ccs / "fetch/mr-0000000000000000"
        shutil.copytree(EXAMPLE / "proposals", tree / "proposals")
        (tree / "mr").mkdir()
        shutil.copy(EXAMPLE / "mr/merged.tsv", tree / "mr")
        merge_requests.mkdir()
        for name in ("open.json", "open.tsv"):
            shutil.copy(EXAMPLE / "mr" / name, merge_requests)
        (self.ccs / "in").mkdir()
        (self.ccs / "in/repo").symlink_to(tree)
        (self.ccs / "in/mr").symlink_to(merge_requests)

    def install(self, version: int, dist: Path = DIST) -> None:
        """Lay out a release as install.sh does, and make it the current one."""
        release = self.lib / "releases" / str(version)
        shutil.rmtree(release, ignore_errors=True)
        shutil.copytree(dist, release / "dist")
        pycache = shutil.ignore_patterns("__pycache__")
        shutil.copytree(DEPLOY, release / "deploy", ignore=pycache)
        link = self.lib / "current"
        link.unlink(missing_ok=True)
        link.symlink_to(release)

    def push(self, raised: str = "10", edit: Callable[[str], str] | None = None):
        """Push the example values, one second newer than the last push."""
        self.pushed += 1
        text = replaced(
            (EXAMPLE / "values").read_text(),
            "proposal example-funding raised=10 ",
            f"proposal example-funding raised={raised} ",
        )
        text += f"gitlab sha256={'0' * 64}\nupdated {self.pushed}\n"
        (self.ccs / "in/values").write_text(edit(text) if edit else text)

    def update(self, ok: bool = True, **env: str) -> CompletedProcess[str]:
        script = self.lib / "current/deploy/web/update.sh"
        return run(script, ok=ok, env={"CCS_DIR": str(self.ccs), **env})

    def applied(self) -> int:
        values = (self.ccs / "state/values").read_text().splitlines()
        return int(next(line for line in values if line.startswith("updated "))[8:])

    def served(self, key: str) -> object:
        return json.loads(self.page("status.json"))[key]

    def raised(self) -> float:
        rows = json.loads(self.page("index.php/projects"))["data"]
        title = "Example funding &amp; fees"
        return next(row["raised_amount"] for row in rows if row["title"] == title)

    def tree(self) -> str:
        return os.readlink(self.current)

    def trees(self) -> int:
        return len(list((self.ccs / "site").iterdir()))

    def open_boxes(self) -> int:
        pages = (self.current / "proposals").glob("*.html")
        return sum("/qr/" in page.read_text() for page in pages)

    def page(self, path: str) -> str:
        return (self.current / path).read_text()
